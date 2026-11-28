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
QUANTITY_FOR_PRICE = re.compile(
    r"(?:(?:get|buy)\s+(?:any\s+)?|(?:احصل\s+على|اشتر(?:ي)?)\s*)?"
    r"(\d+)\s*(?:(?:items?|قطع|قطعة)\s*)?"
    r"(?:for\s+(?:the\s+)?price\s+of|بسعر)\s*(\d+)\s*\.?",
    re.I,
)
MIN_RATE = Decimal(10)
CACHE_KEY = "promotion-digest"
CACHE_SECONDS = 3 * 60 * 60
PAGE_READ_SECONDS = 90
PROMOTION_RESULTS = "promotion-page-results"
PRODUCT_LOOKUP_LIMIT = 12
PRODUCT_LOOKUPS_PER_HINT = 2
PRODUCT_LOOKUP_SECONDS = 20
OFFER_HINT = re.compile(
    r"^(?:"
    r"(?:save|get\s+extra|وفر|خصم(?:\s+يصل\s+إلى)?)\s*(?:\d+(?:\.\d+)?\s*%|%\s*\d+)"
    r"|(?:buy|get|اشتر(?:ي)?|احصل\s+على)\s+(?:any\s+)?\d+\b"
    r")",
    re.I,
)
PRODUCT_PROMOTION_LINKS = (
    'a[id="emphasisLink"][href*="/promotion/psp/"], '
    '#promotions_feature_div a[href*="/promotion/psp/"], '
    '[id^="promoMessage"] a[href*="/promotion/psp/"]'
)
FIVE_PERCENT = r"(?:5(?:\.0+)?\s*%|%\s*5(?:\.0+)?)"
FIVE_PERCENT_TERMS = re.compile(
    rf"(?:(?:buy\s+\d+(?:\s+(?:items?|or more))?\s+(?:and\s+)?)?"
    rf"(?:save|get\s+extra)\s+{FIVE_PERCENT}(?:\s+off)?"
    rf"(?:\s+(?:at checkout|on (?:any )?\d+(?: or more)?))?"
    rf"|(?:اشتر(?:ي)?\s+\d+(?:\s+(?:أو أكثر|او اكثر))?\s+و?)?"
    rf"(?:خصم(?:\s+إضافي)?|وفر)\s+{FIVE_PERCENT}"
    rf"(?:\s+(?:عند الدفع|على (?:أي من )?\d+(?: أو أكثر)?))?)"
    r"(?:\s*(?:خصم من أمازون|الشروط)\s*)*\.?",
    re.I,
)


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


def promotion_hint(text):
    """Find a badge worth inspecting without inferring a campaign from the wording."""
    for line in (text or "").splitlines():
        label = clean_offer_title(line)
        if len(label) <= 180 and OFFER_HINT.match(label) and not FIVE_PERCENT_TERMS.fullmatch(label):
            return label.casefold()
    return ""


def record_promotion_links(bot_id, snapshots):
    folder = os.getenv("SCAN_REPORT_DIR")
    if not folder:
        return
    rows = []
    candidates = []
    asins = set()
    for card in snapshots:
        asin = card.get("asin") or ""
        if not re.fullmatch(r"[A-Z0-9]{10}", asin):
            continue
        asins.add(asin)
        title = str(card.get("title") or asin)[:300]
        price = card.get("price_text")
        has_link = False
        for item in card.get("promotion_links", []):
            url, label = (item.get("url"), item.get("label")) if isinstance(item, dict) else (item, None)
            link = promotion_link(url)
            if link:
                has_link = True
                label = clean_offer_title(label)
                confirmed_five = label if FIVE_PERCENT_TERMS.fullmatch(label) else ""
                rows.append((link[1], asin, title, price, confirmed_five))
        if not has_link and (hint := promotion_hint(card.get("text"))):
            candidates.append((asin, title, price, hint))
    if asins:
        try:
            root = Path(folder)
            root.mkdir(parents=True, exist_ok=True)
            with (root / f"observed-{bot_id}.txt").open("a", encoding="ascii") as stream:
                stream.writelines(asin + "\n" for asin in sorted(asins))
            if rows:
                with (root / f"promotions-{bot_id}.jsonl").open("a", encoding="utf-8") as stream:
                    stream.writelines(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
            if candidates:
                with (root / f"promotion-candidates-{bot_id}.jsonl").open("a", encoding="utf-8") as stream:
                    stream.writelines(json.dumps(row, ensure_ascii=False) + "\n" for row in candidates)
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
        for url, asin, title, price, *labels in json_rows(path):
            offer = register(promotions, url)
            if offer:
                if labels and labels[0] and not offer.title:
                    offer.title = labels[0]
                current = offer.products.get(asin)
                if current is None or current[1] is None:
                    offer.products[asin] = (title, parse_price(price))
    return promotions


def load_observed_asins(folder):
    return {
        asin
        for path in folder.glob("observed-bot*.txt")
        for asin in path.read_text(encoding="ascii").splitlines()
    }


def refresh_discoveries(discoveries, cache, now, results=None):
    """Reuse verified campaign membership until its cache expires."""
    recent = {
        code: saved for code, saved in cache.items()
        if now - saved["checked_at"] < CACHE_SECONDS
    }
    for code, offer in discoveries.items():
        if code in recent:
            saved = recent[code]
            offer.title = offer.title or saved["title"]
            for asin, product in saved["products"].items():
                if asin not in offer.products or offer.products[asin][1] is None:
                    offer.products[asin] = product
            offer.complete = True
    complete_discoveries(discoveries, results)
    for code, offer in discoveries.items():
        if offer.complete:
            recent[code] = {
                "checked_at": recent.get(code, {}).get("checked_at", now),
                "url": offer.url,
                "title": offer.title,
                "products": offer.products,
            }
    return recent


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
                SELECT p.url, COALESCE(NULLIF(to_jsonb(p)->>'offer_terms', ''), p.title),
                       product.asin, product.title, product.price,
                       p.scan_started_at IS NULL AND p.last_error IS NULL
                           AND p.last_checked_at >= now() - interval '3 hours'
                FROM green_deal_pages AS p
                LEFT JOIN green_deal_products AS product
                  ON product.page_id = p.id AND product.present
                 AND product.last_seen_at >= now() - interval '3 hours'
                WHERE p.active AND (p.expires_on IS NULL OR
                    p.expires_on >= (now() AT TIME ZONE 'Asia/Riyadh')::date)
            """).fetchall()
    except psycopg.Error as error:
        print(f"Could not read tracked promotion pages ({type(error).__name__}).")
        return
    for url, title, asin, product_title, price, completed in rows:
        offer = register(promotions, url)
        if offer:
            if title and not offer.title:
                offer.title = title
            if asin:
                if asin not in offer.products or offer.products[asin][1] is None:
                    offer.products[asin] = (product_title, price)
                offer.complete |= bool(completed)


def scan_page(url, output):
    from green_promotion_tracker import collect_products, parse_expiry_date
    from datetime import datetime
    from zoneinfo import ZoneInfo

    with output.open("w", encoding="utf-8") as stream:
        def write(row):
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
            stream.flush()

        def remember_title(heading, body, title):
            document_title = clean_offer_title(title)
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


def read_promotion_page(code, url, output, timeout):
    try:
        subprocess.run(
            [sys.executable, __file__, "--read-page", url, str(output)],
            check=False, timeout=timeout, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        print(f"Time limit reached while reading promotion {code}.")


def apply_promotion_page(offer, output):
    """Apply any available batches and return whether the page has expired."""
    if not output.exists():
        return False
    for row in json_rows(output):
        if row.get("expired"):
            return True
        if row.get("title"):
            offer.title = row["title"]
        offer.products.update(row.get("products", {}))
        offer.complete |= row.get("complete", False)
    return False


def cached_promotions():
    from psycopg import Error as StorageError
    from postgres_storage import load_state

    try:
        return load_state(CACHE_KEY, allow_missing=True).get("promotions", {})
    except (StorageError, RuntimeError) as error:
        print(f"Could not load promotion cache ({type(error).__name__}).")
        return None


def scan_product_links(asin):
    """Read links attached to promotions on this ASIN's own product page."""
    from playwright.sync_api import TimeoutError as PlaywrightTimeout, sync_playwright

    if not re.fullmatch(r"[A-Z0-9]{10}", asin):
        return []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(locale="en-US", timezone_id="Asia/Riyadh")
            response = page.goto(f"https://www.amazon.sa/-/en/dp/{asin}",
                                 wait_until="domcontentloaded", timeout=12000)
            address = urlsplit(page.url)
            if (not response or not response.ok or address.hostname not in {"www.amazon.sa", "amazon.sa"}
                    or not re.search(rf"/dp/{asin}(?:/|$)", address.path)):
                return []
            links = page.locator(PRODUCT_PROMOTION_LINKS)
            try:
                links.first.wait_for(state="attached", timeout=3000)
            except PlaywrightTimeout:
                return []
            return links.evaluate_all(
                "links => links.map(link => ({url: link.href, label: link.innerText}))"
            )
        finally:
            browser.close()


def read_product_links(asin):
    try:
        result = subprocess.run(
            [sys.executable, __file__, "--read-product-links", asin],
            capture_output=True, text=True, encoding="utf-8",
            timeout=PRODUCT_LOOKUP_SECONDS, check=True,
        )
        return json.loads(result.stdout)
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError) as error:
        print(f"Product promotion lookup incomplete for {asin} ({type(error).__name__}).")
        return []


def next_product_candidate(folder, known_asins, attempted, hint_counts):
    for path in sorted(folder.glob("promotion-candidates-bot*.jsonl")):
        for asin, title, price, hint in json_rows(path):
            if (asin not in known_asins and asin not in attempted
                    and hint_counts[hint] < PRODUCT_LOOKUPS_PER_HINT):
                return asin, title, price, hint
    return None


def watch_discoveries(folder):
    """Read campaign links and a bounded sample of product pages alongside scanning."""
    results = folder / PROMOTION_RESULTS
    results.mkdir(parents=True, exist_ok=True)
    cache = cached_promotions() or {}
    attempted = set()
    attempted_products = set()
    hint_counts = defaultdict(int)
    product_discoveries = set()
    while not (results / "stop").exists():
        discoveries = load_discoveries(folder)
        for code, offer in discoveries.items():
            if (results / "stop").exists():
                return
            saved = cache.get(code)
            if (code in attempted or (saved and time.time() - saved["checked_at"] < CACHE_SECONDS)
                    or FIVE_PERCENT_TERMS.fullmatch(clean_offer_title(offer.title))):
                continue
            attempted.add(code)
            print(f"Reading discovered promotion during scanner run: {code}")
            read_promotion_page(code, offer.url, results / f"{code}.jsonl", PAGE_READ_SECONDS)
        if len(attempted_products) < PRODUCT_LOOKUP_LIMIT and not (results / "stop").exists():
            known_asins = {
                asin for offer in discoveries.values() for asin in offer.products
            } | product_discoveries
            for saved in cache.values():
                if time.time() - saved["checked_at"] < CACHE_SECONDS:
                    known_asins.update(saved["products"])
            candidate = next_product_candidate(folder, known_asins, attempted_products, hint_counts)
            if candidate is not None:
                asin, title, price, hint = candidate
                attempted_products.add(asin)
                hint_counts[hint] += 1
                links = [link for link in read_product_links(asin) if promotion_link(link.get("url"))]
                print(f"Product promotion lookup {len(attempted_products)}/{PRODUCT_LOOKUP_LIMIT}: "
                      f"{asin}, {len(links)} link(s).")
                if links:
                    product_discoveries.add(asin)
                    record_promotion_links("bot-details", [{
                        "asin": asin, "title": title, "price_text": price, "promotion_links": links,
                    }])
                continue
        time.sleep(2)


def complete_discoveries(promotions, results=None):
    deadline = time.monotonic() + 240

    def fetch(entry):
        code, offer = entry
        if offer.complete or FIVE_PERCENT_TERMS.fullmatch(clean_offer_title(offer.title)):
            return None
        prefetched = results / f"{code}.jsonl" if results is not None else None
        if prefetched is not None and prefetched.exists():
            return code if apply_promotion_page(offer, prefetched) else None
        remaining = deadline - time.monotonic()
        if remaining < 3:
            return None
        with TemporaryDirectory() as directory:
            output = Path(directory) / "page.jsonl"
            read_promotion_page(code, offer.url, output, min(PAGE_READ_SECONDS, remaining))
            return code if apply_promotion_page(offer, output) else None

    with ThreadPoolExecutor(max_workers=2) as pool:
        expired = list(pool.map(fetch, promotions.items()))
    for code in expired:
        if code:
            promotions.pop(code, None)


def clean_offer_title(title):
    return re.sub(r"^Amazon\.sa\s*[:|–-]\s*|\s+(?:promotion|العرض)\s*$",
                  "", normalize_text(title), flags=re.I).strip()


def saving_for(title, price):
    """Return an effective basket percentage and whether it applies without a basket condition."""
    title = clean_offer_title(title)
    flat = FLAT_RATE.fullmatch(title)
    if flat:
        rate = number(flat[1])
        if rate and rate <= 100:
            return rate, True
    quantity = QUANTITY_FOR_PRICE.fullmatch(title)
    if quantity:
        total, paid = map(Decimal, quantity.groups())
        if 0 < paid < total <= 1000:
            return (total - paid) / total * 100, False
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


def overlap_blocks(promotions, diagnostics=None, observed_asins=None):
    by_asin = defaultdict(list)
    for offer in promotions.values():
        for asin, (title, price) in offer.products.items():
            if observed_asins is None or asin in observed_asins:
                by_asin[asin].append((offer, title, price))
    blocks = []
    observed = below_threshold = unreadable = 0
    for asin, appearances in sorted(by_asin.items()):
        if len(appearances) < 2:
            continue
        observed += 1
        offers = []
        unknown = 0
        for offer, title, price in appearances:
            saving = saving_for(offer.title, price)
            if saving is None:
                unknown += 1
            elif saving[0] >= MIN_RATE:
                offers.append((offer, title, number(price), *saving))
        if len(offers) < 2:
            if len(offers) + unknown >= 2:
                unreadable += 1
            else:
                below_threshold += 1
            continue
        title = html.escape(" ".join(offers[0][1].split())[:120] or asin)
        lines = [f'<b><a href="https://www.amazon.sa/dp/{asin}">{title}</a></b>']
        for offer, _, _, rate, _ in offers:
            lines.append(f"• {offer_anchor(offer)} · {rate:.2f}٪ على إجمالي العرض")
        prices = {entry[2] for entry in offers}
        if len(prices) == 1 and None not in prices and all(entry[4] for entry in offers):
            remaining = Decimal(1)
            for entry in offers:
                remaining *= 1 - entry[3] / 100
            estimate = (1 - remaining) * 100
            lines.append(f"💰 توفير تقديري إذا قبلت أمازون الدمج: {estimate:.2f}٪")
        blocks.append("\n".join(lines))
    if diagnostics is not None:
        diagnostics.update(observed=observed, below_threshold=below_threshold, unreadable=unreadable)
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


def build_messages(promotions, run_number, partial_run=False, *,
                   discovered_codes=None, observed_asins=None):
    discoveries = [
        offer for code, offer in promotions.items()
        if (discovered_codes is None or code in discovered_codes)
        and not FIVE_PERCENT_TERMS.fullmatch(clean_offer_title(offer.title))
    ]
    coverage = sum(not offer.complete for offer in discoveries)
    run_label = f" · التشغيل {html.escape(run_number)}" if run_number else ""
    notes = [f"تعذر استكمال قراءة منتجات {coverage} من صفحات العروض."] if coverage else []
    if partial_run:
        notes.append("هذا الملخص مبني على الجزء الذي اكتمل من الفحص.")
    offers = [f"{index}. {offer_anchor(offer)}" for index, offer in enumerate(discoveries, 1)]
    messages = paginate(f"📋 صفحات العروض ({len(offers)}){run_label}", notes + offers) if offers else []
    diagnostics = {}
    overlaps = overlap_blocks(promotions, diagnostics, observed_asins)
    if overlaps:
        messages.extend(paginate(f"🎁 منتجات عليها أكثر من عرض ({len(overlaps)}){run_label}",
                                 notes + ["الدمج مرهون بشروط أمازون."] + overlaps))
    print(f"Promotion digest: {len(promotions)} pages, {coverage} incomplete, "
          f"{sum(len(offer.products) for offer in promotions.values())} product-page matches, "
          f"{len(overlaps)} eligible overlapping products.")
    if discovered_codes is not None:
        print(f"Promotion links: {len(discovered_codes)} discovered, {len(offers)} listed, "
              f"{len(promotions.keys() - discovered_codes)} from background sources only.")
    print(f"Promotion overlap details: {diagnostics['observed']} before filtering, "
          f"{diagnostics['below_threshold']} below threshold, {diagnostics['unreadable']} unreadable terms.")
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
    elif sys.argv[1] == "--read-product-links":
        print(json.dumps(scan_product_links(sys.argv[2])))
    elif sys.argv[1] == "--watch":
        watch_discoveries(Path(sys.argv[2]))
    else:
        from psycopg import Error as StorageError
        from postgres_storage import save_state

        folder = Path(sys.argv[1])
        offers = load_discoveries(folder)
        observed_asins = load_observed_asins(folder)
        previous = cached_promotions()
        recent = refresh_discoveries(offers, previous or {}, time.time(), folder / PROMOTION_RESULTS)
        if previous is not None and recent != previous:
            try:
                save_state(CACHE_KEY, {"promotions": recent})
            except StorageError as error:
                print(f"Could not save promotion cache ({type(error).__name__}).")
        discovered_codes = set(offers)
        for code, saved in recent.items():
            if code not in offers and observed_asins.intersection(saved["products"]):
                offers[code] = Promotion(saved["url"], saved["title"], saved["products"], complete=True)
        include_tracked_pages(offers)
        messages = build_messages(
            offers, os.getenv("GITHUB_RUN_NUMBER", ""),
            os.getenv("SCAN_OUTCOME") != "success",
            discovered_codes=discovered_codes, observed_asins=observed_asins,
        )
        if messages:
            send_messages(messages)

