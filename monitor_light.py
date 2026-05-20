"""
Marukyu Koyamaen Matcha Stock Monitor (Lightweight)

Uses Scrapling StealthyFetcher ONLY for Cloudflare challenge solving,
then uses curl_cffi for lightweight polling (~30MB RAM steady state).

Architecture:
1. Launch Chromium → solve Cloudflare → extract cookies → close Chromium
2. Poll every 60s with curl_cffi (~30MB RAM, no browser needed)
3. Every 25 min: re-open Chromium to refresh cf_clearance cookies

Usage:
    python monitor_light.py [--interval SECONDS] [--telegram-bot-token TOKEN] [--telegram-chat-id ID] [--debug]
"""

import argparse
import gc
import json
import logging
import re
import signal
import subprocess
import sys
import time
import urllib.request
import urllib.error
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from curl_cffi import requests as cffi_requests
from scrapling.fetchers.stealth_chrome import StealthySession

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("marukyu-monitor")

DEFAULT_URL = (
    "https://www.marukyu-koyamaen.co.jp/english/shop/"
    "products/catalog/matcha/principal"
)
DEFAULT_POLL_INTERVAL = 60
COOKIE_REFRESH_INTERVAL = 1500  # 25 minutes
CF_SOLVE_TIMEOUT = 120000
MAX_RETRIES = 3


@dataclass
class Product:
    name: str
    price: str
    in_stock: bool
    url: str


@dataclass
class StockChange:
    product: Product
    old_status: str
    new_status: str


@dataclass
class SessionCookies:
    cookies: Dict[str, str]
    user_agent: str
    extracted_at: float


def parse_products(html: str) -> List[Product]:
    products = []
    li_pattern = re.compile(
        r'<li[^>]*class="([^"]*product[^"]*)"[^>]*id="([^"]*)"[^>]*>(.*?)</li>',
        re.DOTALL,
    )
    link_pattern = re.compile(
        r'<a[^>]*class="woocommerce-loop-product__link"[^>]*'
        r'href="([^"]*)"[^>]*title="([^"]*)"',
    )
    price_pattern = re.compile(
        r'<span class="woocommerce-Price-amount amount"[^>]*>'
        r".*?<bdi>(.*?)</bdi>",
        re.DOTALL,
    )

    for match in li_pattern.finditer(html):
        classes = match.group(1)
        link_block = match.group(3)
        in_stock = "instock" in classes and "outofstock" not in classes

        link_match = link_pattern.search(link_block)
        product_url = link_match.group(1) if link_match else ""
        product_name = link_match.group(2) if link_match else ""

        price_match = price_pattern.search(link_block)
        price = price_match.group(1) if price_match else "N/A"
        price = re.sub(r"<[^>]+>", "", price).strip()
        price = price.replace("&yen;", "¥").replace("&yen", "¥")

        if product_name:
            products.append(
                Product(name=product_name, price=price, in_stock=in_stock, url=product_url)
            )

    return products


def detect_changes(
    current: List[Product],
    previous: Dict[str, bool],
) -> List[StockChange]:
    changes = []
    for product in current:
        old_in_stock = previous.get(product.name)
        if old_in_stock is not None and old_in_stock != product.in_stock:
            changes.append(
                StockChange(
                    product=product,
                    old_status="In Stock" if old_in_stock else "Out of Stock",
                    new_status="In Stock" if product.in_stock else "Out of Stock",
                )
            )
    return changes


def notify_macos(title: str, message: str, sound: str = "default") -> None:
    script = (
        f'display notification "{message}" with title "{title}"'
        + (f' sound name "{sound}"' if sound else "")
    )
    try:
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=5)
    except Exception as e:
        log.warning(f"Notification failed: {e}")


def notify_telegram(bot_token: str, chat_id: str, change: StockChange) -> None:
    emoji = "\u2705" if change.new_status == "In Stock" else "\u274c"
    text = (
        f"{emoji} *{change.product.name}*\n"
        f"Price: {change.product.price}\n"
        f"Status: {change.old_status} \u2192 {change.new_status}\n"
        f"[View Product]({change.product.url})"
    )
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"}
        )
        urllib.request.urlopen(req, timeout=10)
        log.info(f"Telegram notification sent for {change.product.name}")
    except Exception as e:
        log.warning(f"Telegram notification failed: {e}")


def solve_cloudflare(url: str) -> Optional[SessionCookies]:
    log.info("Launching Chromium to solve Cloudflare...")
    start = time.time()
    session = None
    try:
        session = StealthySession(
            headless=True,
            solve_cloudflare=True,
            timeout=CF_SOLVE_TIMEOUT,
        )
        session.start()

        response = session.fetch(
            url,
            timeout=CF_SOLVE_TIMEOUT,
            wait=5000,
            solve_cloudflare=True,
        )

        if not response or not response.html_content:
            log.error("Empty response from browser")
            return None

        all_cookies = session.context.cookies()
        cookies = {}
        for c in all_cookies:
            domain = c.get("domain", "")
            if "marukyu" in domain:
                cookies[c["name"]] = c["value"]

        pages = session.context.pages
        user_agent = ""
        if pages:
            user_agent = pages[0].evaluate("navigator.userAgent")

        elapsed = time.time() - start
        log.info(
            f"Cloudflare solved in {elapsed:.1f}s — "
            f"extracted {len(cookies)} cookies, UA: {user_agent[:60]}..."
        )

        return SessionCookies(
            cookies=cookies,
            user_agent=user_agent,
            extracted_at=time.time(),
        )

    except Exception as e:
        log.error(f"Cloudflare solve failed: {e}")
        return None
    finally:
        if session:
            try:
                session.close()
            except Exception:
                pass
        gc.collect()
        log.info("Chromium session closed, memory freed")


def fetch_lightweight(url: str, session_cookies: SessionCookies) -> Optional[str]:
    try:
        response = cffi_requests.get(
            url,
            cookies=session_cookies.cookies,
            headers={
                "User-Agent": session_cookies.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://www.google.com/",
            },
            impersonate="chrome",
            timeout=30,
        )

        if response.status_code != 200:
            log.warning(f"HTTP {response.status_code}")
            return None

        if "Principal matcha" not in response.text and "products" not in response.text.lower():
            log.warning("Response doesn't contain expected content — CF challenge likely")
            return None

        return response.text

    except Exception as e:
        log.warning(f"Lightweight fetch failed: {e}")
        return None


class LightweightStockMonitor:
    def __init__(
        self,
        url: str = DEFAULT_URL,
        poll_interval: int = DEFAULT_POLL_INTERVAL,
        telegram_bot_token: Optional[str] = None,
        telegram_chat_id: Optional[str] = None,
        macos_notify: bool = False,
    ):
        self.url = url
        self.poll_interval = poll_interval
        self.telegram_bot_token = telegram_bot_token
        self.telegram_chat_id = telegram_chat_id
        self.macos_notify = macos_notify
        self.previous_state: Dict[str, bool] = {}
        self.session_cookies: Optional[SessionCookies] = None
        self.running = True
        self._setup_signals()

    def _setup_signals(self) -> None:
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

    def _handle_signal(self, signum, frame) -> None:
        log.info(f"Received signal {signum}, shutting down...")
        self.running = False

    def _needs_cookie_refresh(self) -> bool:
        if not self.session_cookies:
            return True
        age = time.time() - self.session_cookies.extracted_at
        return age >= COOKIE_REFRESH_INTERVAL

    def _refresh_cookies(self) -> bool:
        for attempt in range(MAX_RETRIES):
            cookies = solve_cloudflare(self.url)
            if cookies:
                self.session_cookies = cookies
                return True
            if attempt < MAX_RETRIES - 1:
                backoff = 10 * (attempt + 1)
                log.info(f"Retrying CF solve in {backoff}s...")
                time.sleep(backoff)
        return False

    def _fetch(self) -> Optional[str]:
        if self._needs_cookie_refresh():
            log.info("Cookie refresh needed")
            if not self._refresh_cookies():
                log.error("Failed to refresh cookies")
                return None

        for attempt in range(MAX_RETRIES):
            result = fetch_lightweight(self.url, self.session_cookies)
            if result:
                return result

            log.warning(f"Fetch failed, may need cookie refresh (attempt {attempt + 1})")
            if attempt < MAX_RETRIES - 1:
                if not self._refresh_cookies():
                    time.sleep(5)

        return None

    def _report_state(self, products: List[Product]) -> None:
        in_stock = [p for p in products if p.in_stock]
        out_stock = [p for p in products if not p.in_stock]
        log.info(
            f"Checked {len(products)} products: "
            f"{len(in_stock)} in stock, {len(out_stock)} out of stock"
        )

    def _handle_changes(self, changes: List[StockChange]) -> None:
        for change in changes:
            arrow = "IN" if change.new_status == "In Stock" else "OUT"
            log.info(f"[{arrow}] {change.product.name}: {change.old_status} -> {change.new_status}")

            if self.telegram_bot_token and self.telegram_chat_id:
                notify_telegram(self.telegram_bot_token, self.telegram_chat_id, change)
            elif self.macos_notify:
                notify_macos(
                    "Marukyu Stock Alert",
                    f"{change.product.name} is now {change.new_status}! ({change.product.price})",
                )

    def run(self) -> None:
        import resource

        log.info("=" * 60)
        log.info("Marukyu Matcha Stock Monitor (Lightweight)")
        log.info(f"URL: {self.url}")
        log.info(f"Poll interval: {self.poll_interval}s")
        log.info(f"Cookie refresh: {COOKIE_REFRESH_INTERVAL}s")
        log.info(f"Telegram: {'enabled' if self.telegram_bot_token else 'disabled'}")
        log.info(f"macOS notify: {self.macos_notify}")
        log.info("=" * 60)

        if not self._refresh_cookies():
            log.error("Initial Cloudflare solve failed, exiting")
            return

        try:
            while self.running:
                loop_start = time.time()

                try:
                    html = self._fetch()
                    if html:
                        products = parse_products(html)
                        if products:
                            self._report_state(products)
                            current_state = {p.name: p.in_stock for p in products}

                            if self.previous_state:
                                changes = detect_changes(products, self.previous_state)
                                if changes:
                                    self._handle_changes(changes)
                                else:
                                    log.info("No stock changes detected")
                            else:
                                log.info("Initial state recorded")

                            self.previous_state = current_state
                        else:
                            log.warning("No products found in response")
                    else:
                        log.error("Failed to fetch page")

                except Exception as e:
                    log.error(f"Unexpected error: {e}", exc_info=True)

                elapsed = time.time() - loop_start
                mem_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
                log.info(f"Fetch took {elapsed:.1f}s | RSS: {mem_mb:.0f} MB")

                sleep_time = max(0, self.poll_interval - elapsed)
                if self.running and sleep_time > 0:
                    log.info(f"Next check in {sleep_time:.0f}s")
                    sleep_until = time.time() + sleep_time
                    while self.running and time.time() < sleep_until:
                        time.sleep(1)

        finally:
            log.info("Shutting down...")

    def run_once(self) -> None:
        """Single fetch for testing."""
        if not self._refresh_cookies():
            log.error("CF solve failed")
            return

        html = fetch_lightweight(self.url, self.session_cookies)
        if html:
            products = parse_products(html)
            for p in products:
                status = "IN STOCK" if p.in_stock else "OUT OF STOCK"
                print(f"  {p.name}: {p.price} - {status}")

            # Test a second lightweight fetch without re-solving CF
            log.info("Testing second lightweight fetch (no browser)...")
            html2 = fetch_lightweight(self.url, self.session_cookies)
            if html2:
                products2 = parse_products(html2)
                log.info(f"Second fetch OK: {len(products2)} products")
            else:
                log.error("Second fetch failed")
        else:
            log.error("First fetch failed")


def main():
    parser = argparse.ArgumentParser(
        description="Marukyu Matcha Stock Monitor (Lightweight)"
    )
    parser.add_argument("--url", default=DEFAULT_URL, help="URL to monitor")
    parser.add_argument("--interval", type=int, default=DEFAULT_POLL_INTERVAL)
    parser.add_argument("--telegram-bot-token", default=None, help="Telegram bot token")
    parser.add_argument("--telegram-chat-id", default=None, help="Telegram chat ID")
    parser.add_argument("--macos-notify", action="store_true", help="Enable macOS notifications")
    parser.add_argument("--once", action="store_true", help="Single fetch for testing")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    monitor = LightweightStockMonitor(
        url=args.url,
        poll_interval=args.interval,
        telegram_bot_token=args.telegram_bot_token,
        telegram_chat_id=args.telegram_chat_id,
        macos_notify=args.macos_notify,
    )

    if args.once:
        monitor.run_once()
    else:
        monitor.run()


if __name__ == "__main__":
    main()
