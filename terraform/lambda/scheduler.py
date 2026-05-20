import boto3
import os


def handler(event, context):
    ec2 = boto3.client("ec2", region_name=os.environ.get("REGION", "ap-southeast-1"))
    action = event.get("action", "start")
    instance_ids = event.get("instance_ids", [])

    if not instance_ids:
        return {"statusCode": 400, "body": "No instance_ids provided"}

    if action == "start":
        ec2.start_instances(InstanceIds=instance_ids)
    elif action == "stop":
        ec2.stop_instances(InstanceIds=instance_ids)
    else:
        return {"statusCode": 400, "body": f"Unknown action: {action}"}

    return {"statusCode": 200, "body": f"{action} {instance_ids}"}
