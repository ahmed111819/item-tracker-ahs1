"""Collect distinct Amazon promotion pages and send one digest per scanner run."""

import html
import json
import os
import re
import subprocess
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

from deal_offer_rules import classify_offers, number
from deal_parser_v2 import normalize_text

PROMOTION_PATH = re.compile(r"(/-/[a-z]{2})?/promotion/psp/([A-Z0-9]{8,24})/?", re.I)
FLAT_RATE = re.compile(
    r"(?:(?:buy\s*&\s*)?get\s+extra|save|خصم(?:\s+إضافي)?|وفر)\s+"
    r"(\d+(?:\.\d+)?)\s*%\s*(?:off)?\.?", re.I
)
CAPPED_RATE = re.compile(
    r"(?:save|get\s+extra)\s+(\d+(?:\.\d+)?)\s*%\s*(?:off\s+)?"
    r"up\s+to\s+SAR\s*(\d+(?:\.\d+)?)\.?", re.I
)
MIN_RATE = Decimal(10)


@dataclass
class Promotion:
    url: str
    title: str = ""
    products: dict = field(default_factory=dict)
    complete: bool = False


def promotion_link(url):
    if not isinstance(url, str):
        return None
    try:
        parsed = urlsplit(url)
        match = PROMOTION_PATH.fullmatch(parsed.path)
        if parsed.scheme != "https" or parsed.netloc.lower() not in {"www.amazon.sa", "amazon.sa"} or not match:
            return None
    except ValueError:
        return None
    locale, code = match.groups()
    code = code.upper()
    return code, f"https://www.amazon.sa{(locale or '').lower()}/promotion/psp/{code}"


def record_promotion_links(bot_id, snapshots):
    folder = os.getenv("SCAN_REPORT_DIR")
    if not folder:
        return
    rows = []
    for card in snapshots:
        asin = card.get("asin") or ""
        if not re.fullmatch(r"[A-Z0-9]{10}", asin):
            continue
        for item in card.get("promotion_links", []):
            link = promotion_link(item)
            if link:
                rows.append((link[1], asin, str(card.get("title") or asin)[:300], card.get("price_text")))
    if rows:
        try:
            root = Path(folder)
            root.mkdir(parents=True, exist_ok=True)
            with (root / f"promotions-{bot_id}.jsonl").open("a", encoding="utf-8") as stream:
                stream.writelines(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
        except OSError as error:
            print(f"Could not save promotion links ({type(error).__name__}).")


def json_rows(path):
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def register(promotions, url):
    link = promotion_link(url)
    if link is None:
        return None
    code, canonical = link
    return promotions.setdefault(code, Promotion(canonical))


def load_discoveries(folder):
    from deal_parser_v2 import parse_price

    promotions = {}
    for path in sorted(folder.glob("promotions-bot*.jsonl")):
        for url, asin, title, price in json_rows(path):
            offer = register(promotions, url)
            if offer:
                current = offer.products.get(asin)
                if current is None or current[1] is None:
                    offer.products[asin] = (title, parse_price(price))
    return promotions


def include_tracked_pages(promotions):
    password = os.getenv("GREEN_DEALS_DB_PASSWORD")
    if not password:
        return
    import psycopg

    try:
        with psycopg.connect(host="127.0.0.1", port=5432, dbname="green_deals_storage",
                             user="green_deals_bot", password=password, connect_timeout=8,
                             options="-c statement_timeout=15000") as conn:
            rows = conn.execute("""
                SELECT p.url, p.title, product.asin, product.title, product.price
                FROM green_deal_pages AS p
                LEFT JOIN green_deal_products AS product
                  ON product.page_id = p.id AND product.present
                 AND product.last_seen_at <= p.last_checked_at
                 AND product.last_seen_at >= p.last_checked_at - interval '3 hours'
                 AND p.last_error IS NULL AND p.scan_started_at IS NULL
                 AND p.last_checked_at >= now() - interval '3 hours'
                WHERE p.active AND (p.expires_on IS NULL OR
                    p.expires_on >= (now() AT TIME ZONE 'Asia/Riyadh')::date)
            """).fetchall()
    except psycopg.Error as error:
        print(f"Could not read tracked promotion pages ({type(error).__name__}).")
        return
    for url, title, asin, product_title, price in rows:
        offer = register(promotions, url)
        if offer:
            if title:
                offer.title = title
            if asin:
                offer.products[asin] = (product_title, price)
                offer.complete = True


def scan_page(url, output):
    from green_promotion_tracker import collect_products, parse_expiry_date
    from datetime import datetime
    from zoneinfo import ZoneInfo

    with output.open("w", encoding="utf-8") as stream:
        def write(row):
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
            stream.flush()

        def remember_title(heading, body, title):
            document_title = re.sub(r"^Amazon\.sa\s*[:|–-]\s*|\s+(?:promotion|العرض)\s*$", "", title, flags=re.I)
            expiry = parse_expiry_date(body)
            today = datetime.now(ZoneInfo("Asia/Riyadh")).date()
            offer_title = document_title if document_title and document_title.lower() != "amazon.sa" else heading
            write({"title": offer_title, "expired": bool(expiry and expiry < today)})

        def remember_products(products):
            write({"products": {asin: [item[0], float(item[1])] for asin, item in products.items()}})

        try:
            collect_products(url, remember_products, remember_title)
        except Exception as error:
            print(f"Promotion page scan incomplete ({type(error).__name__}).")
        else:
            write({"complete": True})


def complete_discoveries(promotions):
    deadline = time.monotonic() + 240

    def fetch(entry):
        code, offer = entry
        if offer.complete:
            return None
        remaining = deadline - time.monotonic()
        if remaining < 3:
            return None
        with TemporaryDirectory() as directory:
            output = Path(directory) / "page.jsonl"
            try:
                subprocess.run(
                    [sys.executable, __file__, "--read-page", offer.url, str(output)],
                    check=False, timeout=min(40, remaining), stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except subprocess.TimeoutExpired:
                print(f"Time limit reached while reading promotion {code}.")
            if output.exists():
                for row in json_rows(output):
                    if row.get("expired"):
                        return code
                    if row.get("title"):
                        offer.title = row["title"]
                    offer.products.update(row.get("products", {}))
                    offer.complete |= row.get("complete", False)
        return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        expired = list(pool.map(fetch, promotions.items()))
    for code in expired:
        if code:
            promotions.pop(code, None)


def saving_for(title, price):
    """Return an effective basket percentage and whether it applies without a basket condition."""
    title = normalize_text(title)
    flat = FLAT_RATE.fullmatch(title)
    if flat:
        rate = number(flat[1])
        if rate and rate <= 100:
            return rate, True
    unit_price = number(price)
    if not unit_price:
        return None
    capped = CAPPED_RATE.fullmatch(title)
    if capped:
        rate, cap = map(number, capped.groups())
        if rate and rate <= 100 and cap:
            return min(rate, cap / unit_price * 100), False
    deals = classify_offers(unit_price, None, title)
    options = [offer for offer in deals["offers"] if "coupon" not in offer["type"]
               and offer["type"] != "prime_price_promotion"]
    if not options or deals["unrecognized"]:
        return None
    rate = max(number(offer["effective_basket_discount_percent"]) for offer in options)
    return rate, False


def overlap_blocks(promotions):
    by_asin = defaultdict(list)
    for offer in promotions.values():
        for asin, (title, price) in offer.products.items():
            saving = saving_for(offer.title, price)
            if saving and saving[0] >= MIN_RATE:
                by_asin[asin].append((offer, title, number(price), *saving))
    blocks = []
    for asin, offers in sorted(by_asin.items()):
        if len(offers) < 2:
            continue
        title = html.escape(" ".join(offers[0][1].split())[:120] or asin)
        lines = [f'<b><a href="https://www.amazon.sa/dp/{asin}">{title}</a></b>']
        for offer, _, _, rate, _ in offers:
            lines.append(f"• {offer_anchor(offer)} · {rate:g}٪ على إجمالي العرض")
        prices = {entry[2] for entry in offers}
        if len(prices) == 1 and None not in prices and all(entry[4] for entry in offers):
            remaining = Decimal(1)
            for entry in offers:
                remaining *= 1 - entry[3] / 100
            estimate = (1 - remaining) * 100
            lines.append(f"💰 توفير تقديري إذا قبلت أمازون الدمج: {estimate:.2f}٪")
        blocks.append("\n".join(lines))
    return blocks


def offer_anchor(offer):
    label = html.escape(" ".join(offer.title.split())[:160] or offer.url.rsplit("/", 1)[-1])
    return f'<a href="{offer.url}">{label}</a>'


def paginate(title, blocks):
    pages = []
    page = ""
    for block in blocks:
        for part in block.splitlines():
            candidate = f"{page}\n{part}" if page else part
            if len(candidate) > 3300:
                pages.append(page)
                page = part
            else:
                page = candidate
        page += "\n"
    if page.strip():
        pages.append(page)
    return [f"<b>{title} · {index}/{len(pages)}</b>\n\n{page.rstrip()}" for index, page in enumerate(pages, 1)]


def build_messages(promotions, run_number, partial_run=False):
    coverage = sum(not offer.complete for offer in promotions.values())
    run_label = f" · التشغيل {html.escape(run_number)}" if run_number else ""
    notes = [f"تعذر استكمال قراءة منتجات {coverage} من صفحات العروض."] if coverage else []
    if partial_run:
        notes.append("هذا الملخص مبني على الجزء الذي اكتمل من الفحص.")
    offers = [f"{index}. {offer_anchor(offer)}" for index, offer in enumerate(promotions.values(), 1)]
    messages = paginate(f"📋 صفحات العروض ({len(offers)}){run_label}",
                        notes + (offers or ["لم تظهر روابط عروض في هذا التشغيل."]))
    overlaps = overlap_blocks(promotions)
    if overlaps:
        messages.extend(paginate(f"🎁 منتجات عليها أكثر من عرض ({len(overlaps)}){run_label}",
                                 notes + ["الدمج مرهون بشروط أمازون."] + overlaps))
    return messages


def send_messages(messages):
    import requests

    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat = os.environ["TELEGRAM_CHAT_ID"]
    thread = os.getenv("TELEGRAM_MESSAGE_THREAD_ID")
    with requests.Session() as session:
        for message in messages:
            payload = dict(chat_id=chat, text=message, parse_mode="HTML",
                           link_preview_options={"is_disabled": True})
            if thread:
                payload["message_thread_id"] = int(thread)
            for attempt in range(3):
                try:
                    response = session.post(f"https://api.telegram.org/bot{token}/sendMessage",
                                            json=payload, timeout=15)
                except requests.RequestException as error:
                    raise RuntimeError("Telegram digest request failed.") from None
                if response.status_code == 429 and attempt < 2:
                    delay = response.json().get("parameters", {}).get("retry_after", 1)
                    time.sleep(min(int(delay), 30))
                    continue
                if response.ok and response.json().get("ok"):
                    break
                raise RuntimeError(f"Telegram digest failed (HTTP {response.status_code}).")


if __name__ == "__main__":
    if sys.argv[1] == "--read-page":
        scan_page(sys.argv[2], Path(sys.argv[3]))
    else:
        offers = load_discoveries(Path(sys.argv[1]))
        include_tracked_pages(offers)
        complete_discoveries(offers)
        send_messages(build_messages(offers, os.getenv("GITHUB_RUN_NUMBER", ""),
                                     os.getenv("SCAN_OUTCOME") != "success"))

