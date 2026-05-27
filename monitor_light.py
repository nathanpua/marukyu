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
import html
import json
import logging
import os
import random
import re
import signal
import subprocess
import sys
import time
import urllib.request
import urllib.error
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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
DEFAULT_POLL_INTERVAL = 600  # 10 minutes
POLL_JITTER = 60             # ±60 s random offset to avoid mechanical periodicity
COOKIE_REFRESH_INTERVAL = 1500  # 25 minutes
CF_SOLVE_TIMEOUT = 120000
MAX_RETRIES = 3
STATE_FILE = "state.json"
HEARTBEAT_INTERVAL = 86400  # 24 hours
BURST_RESTOCK_THRESHOLD = 5   # restocks in one cycle that trigger slow-poll mode
BURST_POLL_INTERVAL = 3600    # 1 hour — poll interval for rest of day after burst
SIZE_TERMS_TTL = 3600  # seconds between refreshes of the WC size→slug mapping
SHOP_BASE_URL = "https://www.marukyu-koyamaen.co.jp/english/shop"
DAILY_REPORT_HOUR_JST = 17   # send daily report at or after this hour (JST, 24h)
_JST = timezone(timedelta(hours=9))

# Japanese product names shown alongside English in notifications.
# Format: "English Name": "日本語名"
PRODUCT_KANJI: Dict[str, str] = {
    "Tenju": "天授",
    "Kiwami Choan": "極長安",
    "Unkaku": "雲鶴",
    "Wako": "和光",
    "Choan": "長安",
    "Eiju": "永寿",
    "Kinrin": "金輪",
    "Yugen": "幽玄",
    "Chigi no Shiro": "千木の白",
    "Isuzu": "五十鈴",
    "Aoarashi": "青嵐",
    "Shin Matcha Hatsu Enishi": "新抹茶初縁",
}


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


@dataclass
class UrlConfig:
    url: str
    # None = watch every product on the page; non-empty set = only these names
    watch_names: Optional[frozenset] = None


@dataclass
class VariationStock:
    size: str
    variation_id: Optional[int]
    sku: str
    is_in_stock: bool
    availability_text: str
    price: Optional[float] = None


def _parse_url_arg(arg: str) -> UrlConfig:
    """Parse 'URL' or 'URL|Name1,Name2' into a UrlConfig."""
    if "|" in arg:
        url, names_str = arg.split("|", 1)
        watch_names: Optional[frozenset] = frozenset(n.strip() for n in names_str.split(",") if n.strip())
        if not watch_names:
            log.warning(f"'--url {arg}' has a pipe but no product names after it — watching all products on that page")
            watch_names = None
    else:
        url, watch_names = arg, None
    return UrlConfig(url=url.strip(), watch_names=watch_names)


def parse_products(html: str) -> List[Product]:
    products = []
    # Match on `product-type-*` (e.g. product-type-variable, product-type-simple)
    # which is the WooCommerce product type class. `type-product` (the old post-type
    # class) no longer appears in the HTML.
    li_pattern = re.compile(
        r'<li[^>]*class="([^"]*\bproduct-type-[^"]*)"[^>]*>(.*?)</li>',
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
        link_block = match.group(2)
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


def _telegram_post(bot_token: str, chat_id: str, text: str) -> None:
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=10)


def _format_name(name: str) -> str:
    kanji = PRODUCT_KANJI.get(name)
    escaped = html.escape(name)
    return f"{escaped} ({html.escape(kanji)})" if kanji else escaped


def notify_telegram_heartbeat(bot_token: str, chat_id: str, products: List[Product]) -> None:
    in_stock = [p for p in products if p.in_stock]
    out_stock = [p for p in products if not p.in_stock]
    if in_stock:
        stock_lines = "\n".join(
            f"  • {_format_name(p.name)}" for p in in_stock
        )
    else:
        stock_lines = "  (none currently in stock)"
    text = (
        "📡 <b>Marukyu Monitor — online</b>\n"
        f"Tracking {len(products)} products\n"
        f"In stock: {len(in_stock)} | Out of stock: {len(out_stock)}\n\n"
        f"<b>In stock:</b>\n{stock_lines}"
    )
    try:
        _telegram_post(bot_token, chat_id, text)
        log.info("Telegram heartbeat sent")
    except Exception as e:
        log.warning(f"Telegram heartbeat failed: {e}")


def notify_telegram_daily_report(
    bot_token: str,
    chat_id: str,
    restock_packages: Dict[str, Dict[str, bool]],
    all_state: Dict[str, bool],
    report_dt: datetime,
) -> None:
    """Send end-of-day restock summary.
    restock_packages: product → {package → currently_in_stock} (only products restocked today)
    all_state: product → currently_in_stock (all monitored products)
    """
    date_str = report_dt.strftime("%a, %d %b %Y")

    still_in_rows: List[Tuple[str, Dict[str, bool]]] = []
    cycled_rows: List[Tuple[str, Dict[str, bool]]] = []
    never_names: List[str] = []

    for name in sorted(all_state.keys()):
        if name not in restock_packages:
            never_names.append(name)
            continue
        pkgs = restock_packages[name]
        if any(v for v in pkgs.values()):
            still_in_rows.append((name, pkgs))
        else:
            cycled_rows.append((name, pkgs))

    def _pkg_str(pkgs: Dict[str, bool]) -> str:
        parts = [f"✅ {p}" for p, v in sorted(pkgs.items()) if v]
        parts += [f"🔄 {p}" for p, v in sorted(pkgs.items()) if not v]
        return " | ".join(parts)

    lines = []
    for name, pkgs in still_in_rows:
        suffix = f" : {_pkg_str(pkgs)}" if pkgs else ""
        lines.append(f"✅ {_format_name(name)}{suffix}")
    for name, pkgs in cycled_rows:
        suffix = f" : {_pkg_str(pkgs)}" if pkgs else ""
        lines.append(f"🔄 {_format_name(name)}{suffix}")
    for name in never_names:
        lines.append(f"❌ {_format_name(name)}")

    items_text = "\n".join(lines) if lines else "  (no products tracked)"
    text = f"\U0001f4ca <b>Daily Report — {html.escape(date_str)}</b>\n\n{items_text}"
    try:
        _telegram_post(bot_token, chat_id, text)
        log.info("Daily report sent")
    except Exception as e:
        log.warning(f"Daily report failed: {e}")


def _format_price(price: Optional[float]) -> str:
    if price is None:
        return ""
    return f"\u00a5{price:,.0f}"


def _normalize_avail(text: str) -> str:
    if not text or text.lower() in ("in stock", "available"):
        return ""
    if "limit" in text.lower():
        return "limited"
    return text


def _format_variation_block(variations: List[VariationStock]) -> str:
    in_stock = [v for v in variations if v.is_in_stock]
    out_of_stock = [v for v in variations if not v.is_in_stock]
    lines = []
    if in_stock:
        parts = []
        for v in in_stock:
            part = html.escape(v.size)
            price_str = _format_price(v.price)
            if price_str:
                part += f" \u2014 {price_str}"
            note = _normalize_avail(v.availability_text)
            if note:
                part += f" ({html.escape(note)})"
            parts.append(part)
        lines.append("\u2705 " + " \u00b7 ".join(parts))
    if out_of_stock:
        sizes_str = " \u00b7 ".join(html.escape(v.size) for v in out_of_stock)
        lines.append(f"\u274c {sizes_str}")
    return "\n".join(lines)


def notify_telegram_restocks(
    bot_token: str,
    chat_id: str,
    items: "List[Tuple[StockChange, Optional[List[VariationStock]]]]",
) -> None:
    if len(items) == 1:
        change, variations = items[0]
        text = f"\u2705 <b>{_format_name(change.product.name)}</b>\nOut of Stock \u2192 In Stock\n"
        if variations:
            text += "\n" + _format_variation_block(variations) + "\n"
        text += f'\n<a href="{html.escape(change.product.url)}">View Product</a>'
    else:
        text = f"\ud83d\udce6 <b>{len(items)} products restocked</b>\n"
        for change, variations in items:
            text += f'\n\u2705 <b><a href="{html.escape(change.product.url)}">{_format_name(change.product.name)}</a></b>\n'
            if variations:
                text += _format_variation_block(variations) + "\n"
    try:
        _telegram_post(bot_token, chat_id, text)
        names = ", ".join(c.product.name for c, _ in items)
        log.info(f"Telegram restock notification sent: {names}")
    except Exception as e:
        log.warning(f"Telegram restock notification failed: {e}")


def notify_telegram_outofstock(
    bot_token: str,
    chat_id: str,
    change: StockChange,
) -> None:
    text = (
        f"\u274c <b>{_format_name(change.product.name)}</b>\n"
        f"In Stock \u2192 Out of Stock\n"
        f'\n<a href="{html.escape(change.product.url)}">View Product</a>'
    )
    try:
        _telegram_post(bot_token, chat_id, text)
        log.info(f"Telegram out-of-stock notification sent for {change.product.name}")
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
            user_agent = pages[0].evaluate("navigator.userAgent") or ""

        if not cookies:
            log.error("No cookies extracted from Cloudflare session — solve may have silently failed")
            return None

        if not user_agent:
            log.error("Empty User-Agent extracted from Cloudflare session — refusing to cache cookies that won't replay")
            return None

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


def _clean_html(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s).strip()


def fetch_size_terms(session_cookies: SessionCookies) -> Dict[str, str]:
    """Returns {size_name: slug} from WC Store API. Empty dict on failure."""
    try:
        resp = cffi_requests.get(
            f"{SHOP_BASE_URL}/wp-json/wc/store/v1/products/attributes/1/terms",
            cookies=session_cookies.cookies,
            headers={"User-Agent": session_cookies.user_agent},
            impersonate="chrome",
            timeout=15,
        )
        if resp.status_code != 200:
            return {}
        return {t["name"]: t["slug"] for t in resp.json() if "name" in t and "slug" in t}
    except Exception as e:
        log.warning(f"Size terms fetch failed: {e}")
        return {}


def fetch_variation_stock(
    product_url: str,
    session_cookies: SessionCookies,
    size_terms: Dict[str, str],
) -> Optional[List[VariationStock]]:
    """
    Fetch per-size stock for a product via WooCommerce AJAX (wc-ajax=get_variation).
    Fetches the product page to extract product_id and its sizes, then queries each size.
    Returns None on error; returns empty list if product has no queryable sizes.
    """
    try:
        resp = cffi_requests.get(
            product_url,
            cookies=session_cookies.cookies,
            headers={
                "User-Agent": session_cookies.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Referer": "https://www.google.com/",
            },
            impersonate="chrome",
            timeout=30,
        )
        if resp.status_code != 200:
            log.warning(f"Product page returned {resp.status_code}: {product_url}")
            return None
        page_html = resp.text
    except Exception as e:
        log.warning(f"Product page fetch failed: {e}")
        return None

    pid_m = re.search(r'id="product-(\d+)"', page_html)
    if not pid_m:
        log.warning(f"product_id not found in {product_url}")
        return None
    product_id = pid_m.group(1)

    # Extract the full <dl class="pa-size"> block, then collect every <dd> within it.
    dl_blocks = re.findall(
        r'<dl[^>]*class="[^"]*pa-size[^"]*"[^>]*>(.*?)</dl>',
        page_html,
        re.DOTALL,
    )
    size_names = [
        _clean_html(dd)
        for block in dl_blocks
        for dd in re.findall(r'<dd>(.*?)</dd>', block, re.DOTALL)
    ]
    if not size_names:
        log.warning(f"No sizes found in {product_url}")
        return None

    log.info(f"Checking {len(size_names)} variation(s) for product {product_id}: {size_names}")
    results: List[VariationStock] = []
    for size_name in size_names:
        slug = size_terms.get(size_name)
        if not slug:
            log.debug(f"No size slug for '{size_name}', skipping")
            continue
        try:
            ajax = cffi_requests.post(
                f"{SHOP_BASE_URL}/?wc-ajax=get_variation",
                data=f"product_id={product_id}&attribute_pa_size={slug}",
                cookies=session_cookies.cookies,
                headers={
                    "User-Agent": session_cookies.user_agent,
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Referer": product_url,
                },
                impersonate="chrome",
                timeout=10,
            )
            if ajax.status_code != 200:
                continue
            raw = ajax.text.strip()
            if raw in ("false", "", "null"):
                continue
            data = ajax.json()
            if not isinstance(data, dict):
                continue
            price_raw = data.get("display_price")
            results.append(VariationStock(
                size=size_name,
                variation_id=data.get("variation_id"),
                sku=data.get("sku", ""),
                is_in_stock=bool(data.get("is_in_stock", False)),
                availability_text=_clean_html(data.get("availability_html", "")),
                price=float(price_raw) if price_raw is not None else None,
            ))
        except Exception as e:
            log.debug(f"Variation AJAX failed for '{size_name}': {e}")

    return results if results else None


class LightweightStockMonitor:
    def __init__(
        self,
        url_configs: List[UrlConfig],
        poll_interval: int = DEFAULT_POLL_INTERVAL,
        telegram_bot_token: Optional[str] = None,
        telegram_chat_id: Optional[str] = None,
        macos_notify: bool = False,
        state_file: Optional[str] = STATE_FILE,
    ):
        self.url_configs = url_configs
        self._solve_url = url_configs[0].url  # use first URL for CF challenge solving
        self.poll_interval = poll_interval
        self.telegram_bot_token = telegram_bot_token
        self.telegram_chat_id = telegram_chat_id
        self.macos_notify = macos_notify
        self.state_file = state_file
        self.previous_state: Dict[str, bool] = {}
        self.session_cookies: Optional[SessionCookies] = None
        self._last_heartbeat_at: Optional[float] = None
        self._size_terms: Dict[str, str] = {}
        self._size_terms_fetched_at: Optional[float] = None
        self._slow_poll_date: Optional[str] = None  # YYYY-MM-DD when burst slow-poll is active
        self._today_date_jst: str = datetime.now(_JST).strftime("%Y-%m-%d")
        self._today_restock_packages: Dict[str, Dict[str, bool]] = {}  # product → {package → currently_in_stock}
        self._daily_report_sent_date: Optional[str] = None  # YYYY-MM-DD (JST) when last daily report was sent
        self.running = True
        self._load_state()
        self._setup_signals()

    def _load_state(self) -> None:
        if not self.state_file:
            return
        try:
            with open(self.state_file, encoding="utf-8") as f:
                loaded = json.load(f)
        except FileNotFoundError:
            return
        except Exception as e:
            log.warning(f"Could not load state file: {e}")
            return
        if not isinstance(loaded, dict) or not all(
            isinstance(k, str) and isinstance(v, bool) for k, v in loaded.items()
        ):
            log.warning(f"State file {self.state_file} has unexpected shape, ignoring")
            return
        self.previous_state = loaded
        log.info(f"Loaded state for {len(self.previous_state)} products from {self.state_file}")

    def _save_state(self) -> None:
        if not self.state_file:
            return
        try:
            tmp = self.state_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.previous_state, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.state_file)
        except Exception as e:
            log.warning(f"Could not save state file: {e}")

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
            cookies = solve_cloudflare(self._solve_url)
            if cookies:
                self.session_cookies = cookies
                return True
            if attempt < MAX_RETRIES - 1:
                backoff = 10 * (attempt + 1)
                log.info(f"Retrying CF solve in {backoff}s...")
                time.sleep(backoff)
        return False

    def _fetch(self, url: str) -> Optional[str]:
        if self._needs_cookie_refresh():
            log.info("Cookie refresh needed")
            if not self._refresh_cookies():
                log.error("Failed to refresh cookies")
                return None

        for attempt in range(MAX_RETRIES):
            result = fetch_lightweight(url, self.session_cookies)
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

    def _effective_poll_interval(self) -> int:
        if self._slow_poll_date:
            today = datetime.now().strftime("%Y-%m-%d")
            if today == self._slow_poll_date:
                return BURST_POLL_INTERVAL
            self._slow_poll_date = None
            log.info("Burst slow-poll expired — resuming normal poll interval")
        return self.poll_interval

    def _get_size_terms(self) -> Dict[str, str]:
        now = time.time()
        if (
            self._size_terms
            and self._size_terms_fetched_at
            and now - self._size_terms_fetched_at < SIZE_TERMS_TTL
        ):
            return self._size_terms
        if not self.session_cookies:
            return {}
        terms = fetch_size_terms(self.session_cookies)
        if terms:
            self._size_terms = terms
            self._size_terms_fetched_at = now
            log.info(f"Size terms refreshed: {len(terms)} sizes")
        return self._size_terms

    def _maybe_send_heartbeat(self, products: List[Product]) -> None:
        if not self.telegram_bot_token or not self.telegram_chat_id:
            return
        now = time.time()
        if self._last_heartbeat_at is None or now - self._last_heartbeat_at >= HEARTBEAT_INTERVAL:
            notify_telegram_heartbeat(self.telegram_bot_token, self.telegram_chat_id, products)
            self._last_heartbeat_at = now

    def _check_date_rollover(self) -> None:
        today = datetime.now(_JST).strftime("%Y-%m-%d")
        if today != self._today_date_jst:
            log.info(f"Date rolled over to {today} JST — resetting daily restock tracking")
            self._today_date_jst = today
            self._today_restock_packages = {}

    def _maybe_send_daily_report(self) -> None:
        if not self.telegram_bot_token or not self.telegram_chat_id:
            return
        now_jst = datetime.now(_JST)
        today = now_jst.strftime("%Y-%m-%d")
        if now_jst.hour >= DAILY_REPORT_HOUR_JST and today != self._daily_report_sent_date:
            notify_telegram_daily_report(
                self.telegram_bot_token, self.telegram_chat_id,
                {k: dict(v) for k, v in self._today_restock_packages.items()},
                dict(self.previous_state), now_jst,
            )
            self._daily_report_sent_date = today

    def _handle_changes(self, changes: List[StockChange]) -> None:
        restock_items: List[Tuple[StockChange, Optional[List[VariationStock]]]] = []
        outofstock_changes: List[StockChange] = []

        for change in changes:
            arrow = "IN" if change.new_status == "In Stock" else "OUT"
            log.info(f"[{arrow}] {change.product.name}: {change.old_status} -> {change.new_status}")

            if change.new_status == "In Stock":
                variations: Optional[List[VariationStock]] = None
                if change.product.url and self.session_cookies:
                    try:
                        size_terms = self._get_size_terms()
                        if size_terms:
                            variations = fetch_variation_stock(
                                change.product.url, self.session_cookies, size_terms
                            )
                            if variations:
                                n_in = sum(1 for v in variations if v.is_in_stock)
                                log.info(
                                    f"Variation stock for {change.product.name}: "
                                    f"{n_in}/{len(variations)} in stock"
                                )
                            else:
                                log.warning(f"No variation data returned for {change.product.name}")
                    except Exception as e:
                        log.warning(f"Variation check failed for {change.product.name}: {e}")
                restock_items.append((change, variations))
            else:
                outofstock_changes.append(change)

        if self.telegram_bot_token and self.telegram_chat_id:
            if restock_items:
                notify_telegram_restocks(
                    self.telegram_bot_token, self.telegram_chat_id, restock_items
                )
            for change in outofstock_changes:
                notify_telegram_outofstock(
                    self.telegram_bot_token, self.telegram_chat_id, change
                )
        elif self.macos_notify:
            for change in changes:
                notify_macos(
                    "Marukyu Stock Alert",
                    f"{change.product.name} is now {change.new_status}! ({change.product.price})",
                )

        restock_count = len(restock_items)
        for change, variations in restock_items:
            pkgs = self._today_restock_packages.setdefault(change.product.name, {})
            if variations:
                for v in variations:
                    if v.is_in_stock:
                        pkgs[v.size] = True

        if restock_count > BURST_RESTOCK_THRESHOLD:
            self._slow_poll_date = datetime.now().strftime("%Y-%m-%d")
            log.info(
                f"{restock_count} items restocked in one cycle — switching to "
                f"{BURST_POLL_INTERVAL}s poll interval for the rest of today "
                f"({self._slow_poll_date})"
            )

    def run(self) -> None:
        import resource

        log.info("=" * 60)
        log.info("Marukyu Matcha Stock Monitor (Lightweight)")
        for cfg in self.url_configs:
            suffix = f" [filter: {', '.join(sorted(cfg.watch_names))}]" if cfg.watch_names else ""
            log.info(f"URL: {cfg.url}{suffix}")
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
                self._check_date_rollover()

                try:
                    all_products: List[Product] = []
                    for cfg in self.url_configs:
                        page_html = self._fetch(cfg.url)
                        if not page_html:
                            log.error(f"Failed to fetch {cfg.url}")
                            continue
                        page_products = parse_products(page_html)
                        if not page_products:
                            log.warning(f"No products found at {cfg.url}")
                            continue
                        if cfg.watch_names:
                            missing = cfg.watch_names - {p.name for p in page_products}
                            if missing:
                                log.warning(f"Watched products not found at {cfg.url}: {missing}")
                            page_products = [p for p in page_products if p.name in cfg.watch_names]
                        all_products.extend(page_products)

                    if all_products:
                        self._report_state(all_products)
                        current_state = {p.name: p.in_stock for p in all_products}
                        changes: List[StockChange] = []

                        is_initial = not self.previous_state
                        if self.previous_state:
                            changes = detect_changes(all_products, self.previous_state)
                            if changes:
                                self._handle_changes(changes)
                            else:
                                log.info("No stock changes detected")
                        else:
                            log.info("Initial state recorded")

                        self.previous_state = current_state
                        self._save_state()
                        # Mark packages as out when product goes back to out-of-stock
                        for name, pkgs in self._today_restock_packages.items():
                            if not current_state.get(name, False):
                                for size in pkgs:
                                    pkgs[size] = False
                        if not changes and not is_initial:
                            self._maybe_send_heartbeat(all_products)
                        self._maybe_send_daily_report()
                    elif self.url_configs:
                        log.warning("No products matched across all URLs")

                except Exception as e:
                    log.error(f"Unexpected error: {e}", exc_info=True)

                elapsed = time.time() - loop_start
                # Linux: ru_maxrss in KB; macOS: bytes
                _rss_divisor = 1024 * 1024 if sys.platform == "darwin" else 1024
                mem_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / _rss_divisor
                log.info(f"Fetch took {elapsed:.1f}s | RSS: {mem_mb:.0f} MB")

                jitter = random.uniform(-POLL_JITTER, POLL_JITTER)
                interval = self._effective_poll_interval()
                sleep_time = max(0, interval - elapsed + jitter)
                if self.running and sleep_time > 0:
                    slow = " [burst slow-poll]" if self._slow_poll_date else ""
                    log.info(f"Next check in {sleep_time:.0f}s{slow}")
                    sleep_until = time.time() + sleep_time
                    last_report_check = time.time()
                    while self.running and time.time() < sleep_until:
                        time.sleep(1)
                        if time.time() - last_report_check >= 60:
                            self._maybe_send_daily_report()
                            last_report_check = time.time()

        finally:
            log.info("Shutting down...")

    def run_once(self) -> None:
        """Single fetch for testing."""
        if not self._refresh_cookies():
            log.error("CF solve failed")
            return

        for cfg in self.url_configs:
            print(f"\n--- {cfg.url} ---")
            page_html = fetch_lightweight(cfg.url, self.session_cookies)
            if not page_html:
                log.error(f"Fetch failed: {cfg.url}")
                continue
            products = parse_products(page_html)
            if cfg.watch_names:
                products = [p for p in products if p.name in cfg.watch_names]
            for p in products:
                status = "IN STOCK" if p.in_stock else "OUT OF STOCK"
                print(f"  {p.name}: {p.price} - {status}")


def main():
    parser = argparse.ArgumentParser(
        description="Marukyu Matcha Stock Monitor (Lightweight)"
    )
    parser.add_argument(
        "--url", dest="url_args", action="append", default=None,
        metavar="URL[|Name1,Name2]",
        help="URL to monitor. Repeat for multiple pages. "
             "Append |Name to watch only specific products on that page.",
    )
    parser.add_argument("--interval", type=int, default=DEFAULT_POLL_INTERVAL)
    parser.add_argument("--telegram-bot-token", default=None, help="Telegram bot token (prefer SSM in production)")
    parser.add_argument("--telegram-chat-id", default=None, help="Telegram chat ID (prefer SSM in production)")
    parser.add_argument("--telegram-ssm-bot-token-param", default=None, metavar="SSM_NAME", help="SSM parameter name for bot token")
    parser.add_argument("--telegram-ssm-chat-id-param", default=None, metavar="SSM_NAME", help="SSM parameter name for chat ID")
    parser.add_argument("--aws-region", default=None, help="AWS region for SSM (default: auto-detect)")
    parser.add_argument("--macos-notify", action="store_true", help="Enable macOS notifications")
    parser.add_argument("--state-file", default=STATE_FILE, help="Path to state persistence file ('' to disable)")
    parser.add_argument("--once", action="store_true", help="Single fetch for testing")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    telegram_bot_token = args.telegram_bot_token
    telegram_chat_id = args.telegram_chat_id

    if args.telegram_ssm_bot_token_param or args.telegram_ssm_chat_id_param:
        import boto3
        ssm = boto3.client("ssm", region_name=args.aws_region)
        if args.telegram_ssm_bot_token_param and not telegram_bot_token:
            telegram_bot_token = ssm.get_parameter(
                Name=args.telegram_ssm_bot_token_param, WithDecryption=True
            )["Parameter"]["Value"]
        if args.telegram_ssm_chat_id_param and not telegram_chat_id:
            telegram_chat_id = ssm.get_parameter(
                Name=args.telegram_ssm_chat_id_param, WithDecryption=True
            )["Parameter"]["Value"]
        log.info("Telegram credentials loaded from SSM")

    url_configs = [_parse_url_arg(a) for a in (args.url_args or [DEFAULT_URL])]

    monitor = LightweightStockMonitor(
        url_configs=url_configs,
        poll_interval=args.interval,
        telegram_bot_token=telegram_bot_token,
        telegram_chat_id=telegram_chat_id,
        macos_notify=args.macos_notify,
        state_file=args.state_file or None,
    )

    if args.once:
        monitor.run_once()
    else:
        monitor.run()


if __name__ == "__main__":
    main()
