import logging
import os

import boto3
from botocore.exceptions import ClientError

log = logging.getLogger()
log.setLevel(logging.INFO)

# EC2 errors that mean the instance is already in the desired state.
# Re-raising these would cause spurious DLQ messages and alarm emails.
_BENIGN_EC2_ERRORS = frozenset({"IncorrectInstanceState", "InvalidInstanceState"})


def handler(event, context):
    action = event.get("action", "start")
    instance_ids = event.get("instance_ids", [])

    if not instance_ids:
        return {"statusCode": 400, "body": "No instance_ids provided"}

    try:
        ec2 = boto3.client("ec2", region_name=os.environ.get("REGION", "ap-southeast-1"))
        if action == "start":
            ec2.start_instances(InstanceIds=instance_ids)
        elif action == "stop":
            ec2.stop_instances(InstanceIds=instance_ids)
        else:
            return {"statusCode": 400, "body": f"Unknown action: {action}"}
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code in _BENIGN_EC2_ERRORS:
            log.warning("EC2 %s skipped for %s — already in target state (%s)", action, instance_ids, code)
            return {"statusCode": 200, "body": f"{action} skipped: {code}"}
        log.error("EC2 %s failed for %s: %s", action, instance_ids, e)
        raise
    except Exception as e:
        log.error("EC2 %s failed for %s: %s", action, instance_ids, e)
        raise

    log.info("%s %s OK", action, instance_ids)
    return {"statusCode": 200, "body": f"{action} {instance_ids}"}
