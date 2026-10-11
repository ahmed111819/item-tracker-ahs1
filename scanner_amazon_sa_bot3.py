# ============================================================
# CELL 2 — AMAZON SCANNER (BOT3)
# RESULT-COUNT PAGE PLANNING + SHARED CONCURRENCY
# 4 RETRIES PER PAGE
# 40%+ COUPON ALERTS + 80%+ PRICE-DROP ALERTS
# ============================================================

import asyncio
import html
import json
import os
import re
import requests

from datetime import datetime
from urllib.parse import urljoin
from playwright.async_api import async_playwright


# ------------------------------------------------------------
# TELEGRAM SETTINGS — paste your values here as usual
# ------------------------------------------------------------

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
TELEGRAM_READY = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)
if not TELEGRAM_READY:
    raise RuntimeError("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in GitHub Actions secrets.")

# ------------------------------------------------------------
# AMAZON SEARCH LINKS
# ------------------------------------------------------------

SEARCH_URLS = [
    # Link 5
    "https://www.amazon.sa/s?i=home-improvement&rh=n%3A12463390031%2Cp_36%3A-15500%2Cp_98%3A16895730031%2Cp_90%3A16641876031%2Cp_6%3AA2XPWB6MYN7ZDK%2Cp_n_deal_type%3A26902957031&s=price-asc-rank&dc&fs=true&qid=1791557877&rnid=26902955031&ref=sr_nr_p_n_deal_type_2&ds=v1%3AFMMbLv7t3xAwdQgwK0XTtXFXx3UrCOR9dsaahq4wx7Q",
    # Link 8
    "https://www.amazon.sa/s?i=videogames&rh=n%3A12463675031%2Cp_98%3A16895730031%2Cp_n_deal_type%3A26902957031&s=price-asc-rank&dc=&fs=true&qid=1791558845&rnid=16641831031&ref=sr_nr_p_36_0_0&low-price=&high-price=180",
    # Link 12
    "https://www.amazon.sa/s?i=sports&rh=n%3A12463561031%2Cp_36%3A-15500%2Cp_98%3A16895730031%2Cp_6%3AA2XPWB6MYN7ZDK%2Cp_n_deal_type%3A26902957031%2Cp_90%3A16641876031&s=price-asc-rank&dc&fs=true&qid=1791579919&rnid=28071520031&ref=sr_nr_p_n_condition-type_1&ds=v1%3A3Kcl%2BPfV9rzUm5u%2BhOoYYNqLrSpeJ8WO0gyPgIoQdZ0",
    # Link 13
    "https://www.amazon.sa/s?i=industrial&rh=n%3A16636960031%2Cp_36%3A-16500%2Cp_98%3A16895730031%2Cp_90%3A16641876031%2Cp_n_deal_type%3A26902957031%2Cp_6%3AA2XPWB6MYN7ZDK&s=price-asc-rank&dc&fs=true&qid=1791580060&rnid=16641811031&ref=sr_st_price-asc-rank&ds=v1%3A6XXBuRtsAU8%2BvjHD6jaFoad8xXFtZoJA%2FR4DFF%2Biihw",
    # Link 17
    "https://www.amazon.sa/s?i=automotive&rh=n%3A12462877031%2Cp_36%3A-16500%2Cp_98%3A16895730031%2Cp_n_deal_type%3A26902957031%2Cp_6%3AA2XPWB6MYN7ZDK&s=price-asc-rank&dc&fs=true&qid=1791580504&rnid=16641811031&ref=sr_st_price-asc-rank&ds=v1%3AS305Dy2KSZQIsNPwwBUxnW37xUnZoYhStl9rI%2BLd4%2Bc",
    # Link 18
    "https://www.amazon.sa/s?i=hpc&rh=n%3A12463276031%2Cp_36%3A-15500%2Cp_98%3A16895730031%2Cp_90%3A16641876031%2Cp_n_deal_type%3A26902957031%2Cp_6%3AA2XPWB6MYN7ZDK&s=price-asc-rank&dc&fs=true&qid=1791580592&rnid=16641811031&ref=sr_st_price-asc-rank&ds=v1%3AZb8eEwhKYV0kadD2ymTDAG2aZjfY91qaTQ9qYg04vbo",
]

SEARCH_URLS = [url.strip() for url in SEARCH_URLS if url.strip()]

HOME_URL = "https://www.amazon.sa/"

MIN_DISCOUNT_PERCENT = 40.0
MIN_PROMOTION_DISCOUNT_PERCENT = 40.0
MIN_PRICE_DROP_PERCENT = 80.0

MAX_CONCURRENT_PAGES = 10
PAGE_WORKERS_PER_SEARCH = 2
MAX_PAGES_PER_SEARCH = 200
MAX_ATTEMPTS_PER_PAGE = 4
MAX_CONCURRENT_PRODUCTS = 50
VERBOSE_PRODUCT_LOGS = False
RETRY_DELAYS_SECONDS = [4, 7, 12]


# ------------------------------------------------------------
# BOT ID — MUST BE DIFFERENT IN EACH COLAB
# ------------------------------------------------------------
BOT_ID = "bot3"

# ------------------------------------------------------------
# GitHub Actions + Google Drive persistence (no Amazon login/session)
# ------------------------------------------------------------
import io
import tempfile
import threading
from pathlib import Path
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload

GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
DRIVE_FOLDER_ID = os.environ.get("DRIVE_AMAZON_SA_FOLDER_ID", "").strip()
if not GOOGLE_SERVICE_ACCOUNT_JSON or not DRIVE_FOLDER_ID:
    raise RuntimeError("Missing GOOGLE_SERVICE_ACCOUNT_JSON secret or DRIVE_AMAZON_SA_FOLDER_ID variable.")

BOT_ID = "bot3"
DATA_DIR = os.environ.get("RUNNER_TEMP", tempfile.gettempdir())
TRACKING_NAME = f"amazon_deals_tracking_{BOT_ID}.json"
REGISTRY_NAME = "amazon_deals_sent_registry.json"
TRACKING_FILE = os.path.join(DATA_DIR, TRACKING_NAME)
LEGACY_TRACKING_FILE = os.path.join(DATA_DIR, "amazon_deals_tracking.json")
CENTRAL_ALERTS_FILE = os.path.join(DATA_DIR, REGISTRY_NAME)
CENTRAL_LOCK_FILE = os.path.join(DATA_DIR, "amazon_deals_sent_registry.lock")
os.makedirs(DATA_DIR, exist_ok=True)

_drive = None
_drive_ids = {}
_drive_io_lock = threading.Lock()
def drive_service():
    global _drive
    if _drive is None:
        info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
        credentials = service_account.Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/drive"])
        _drive = build("drive", "v3", credentials=credentials, cache_discovery=False)
    return _drive

def drive_id(name, required=False):
    escaped = name.replace("'", "\\'")
    result = drive_service().files().list(
        q=f"'{DRIVE_FOLDER_ID}' in parents and name = '{escaped}' and trashed = false",
        pageSize=100, fields="files(id,name)",
        supportsAllDrives=True, includeItemsFromAllDrives=True).execute()
    found = result.get("files", [])
    if len(found) > 1 or (required and not found):
        raise RuntimeError(f"Expected {'one' if required else 'at most one'} {name} in the shared Drive folder; found {len(found)}.")
    return found[0]["id"] if found else None

def download_drive_json(name, required=False):
    file_id = drive_id(name, required=required)
    _drive_ids[name] = file_id
    if file_id:
        request = drive_service().files().get_media(fileId=file_id, supportsAllDrives=True)
        stream = io.BytesIO()
        downloader = MediaIoBaseDownload(stream, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        return stream.getvalue()
    return None

def upload_drive_json(name, local_path):
    raw = Path(local_path).read_bytes()
    with _drive_io_lock:
        file_id = _drive_ids.get(name)
        media = MediaIoBaseUpload(io.BytesIO(raw), mimetype="application/json", resumable=False)
        if file_id:
            drive_service().files().update(fileId=file_id, media_body=media, supportsAllDrives=True).execute()
        else:
            created = drive_service().files().create(
                body={"name": name, "parents": [DRIVE_FOLDER_ID]},
                media_body=media, fields="id", supportsAllDrives=True).execute()
            _drive_ids[name] = created["id"]

tracking_raw = download_drive_json(TRACKING_NAME, required=True)
Path(TRACKING_FILE).write_bytes(tracking_raw)
registry_raw = download_drive_json(REGISTRY_NAME, required=False)
if registry_raw is not None:
    Path(CENTRAL_ALERTS_FILE).write_bytes(registry_raw)


if TELEGRAM_READY:
    print("✅ Telegram values are set.")
else:
    print("⚠️ Telegram is not configured.")
    print("   Scanning and saving will work; alerts will not be sent.")


# ------------------------------------------------------------
# TRACKING
# ------------------------------------------------------------

LEGACY_TRACKING_FILE = TRACKING_FILE


def load_tracking():
    if not os.path.exists(TRACKING_FILE):
        # On first run, initialize this bot's private tracking file from the
        # legacy history if available. Each bot subsequently writes only its own file.
        source_file = LEGACY_TRACKING_FILE if os.path.exists(LEGACY_TRACKING_FILE) else None
        if source_file:
            try:
                with open(source_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                data.setdefault("products", {})
                data.setdefault("scan_state", {})
                with open(TRACKING_FILE, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
                print("📦 Copied legacy tracking history into", TRACKING_FILE)
                return data
            except Exception as e:
                raise RuntimeError("Could not initialize this bot's tracking file from legacy history.") from e
        return {
            "products": {},
            "last_updated": None,
            "scan_state": {},
        }

    try:
        with open(TRACKING_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        data.setdefault("products", {})
        data.setdefault("scan_state", {})
        return data

    except Exception as e:
        # Do not continue with an empty history: that would resend old deals
        # and could overwrite the existing tracking file.
        raise RuntimeError(
            f"Tracking file could not be read ({TRACKING_FILE}). "
            "Fix or restore it before scanning; no new empty history was created."
        ) from e


tracking_data = load_tracking()

# Offers observed during this run. These sets are used to clear old alert
# signatures only after every configured search has completed successfully.
RUN_COUPON_SIGNATURES = {}
RUN_PRICE_DROP_SIGNATURES = {}


def save_tracking():
    """Overwrite the fixed tracking file in place."""
    tracking_data["last_updated"] = datetime.now().isoformat()

    # Avoid temp-file rename on the mounted Google Drive filesystem,
    # which can create another Drive item with the same filename.
    with open(TRACKING_FILE, "w", encoding="utf-8") as f:
        json.dump(
            tracking_data,
            f,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        f.flush()
    upload_drive_json(TRACKING_NAME, TRACKING_FILE)


# ------------------------------------------------------------
# SHARED TELEGRAM ALERT REGISTRY (Google Drive only)
# ------------------------------------------------------------
# Each Colab has its own tracking JSON, but all three share this registry.
# A short-lived lock protects read/modify/write operations as far as the
# mounted Drive filesystem supports exclusive file creation.
import time
import uuid

LOCK_TIMEOUT_SECONDS = 90
LOCK_STALE_SECONDS = 180
RESERVATION_TTL_SECONDS = 1800  # 30 minutes

def acquire_central_lock():
    deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            fd = os.open(CENTRAL_LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as lock_file:
                lock_file.write(json.dumps({
                    "bot_id": BOT_ID,
                    "created_at": datetime.now().isoformat()
                }))
            return
        except FileExistsError:
            try:
                age = time.time() - os.path.getmtime(CENTRAL_LOCK_FILE)
                if age > LOCK_STALE_SECONDS:
                    # Recover a lock left behind by a crashed Colab session.
                    os.remove(CENTRAL_LOCK_FILE)
                    continue
            except FileNotFoundError:
                continue
            time.sleep(0.25)
    raise TimeoutError(
        "Could not acquire the shared Google Drive lock. "
        "Do not send alerts without the shared lock."
    )

def release_central_lock():
    try:
        os.remove(CENTRAL_LOCK_FILE)
    except FileNotFoundError:
        pass

def read_central_registry():
    if not os.path.exists(CENTRAL_ALERTS_FILE):
        return {"alerts": {}, "updated_at": None}
    with open(CENTRAL_ALERTS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict) or not isinstance(data.get("alerts", {}), dict):
        raise RuntimeError("Central alert registry is invalid; refusing to send.")
    data.setdefault("alerts", {})
    return data

def write_central_registry(data):
    data["updated_at"] = datetime.now().isoformat()
    # Direct write while holding the shared lock; do not write without the lock.
    with open(CENTRAL_ALERTS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        f.flush()
    upload_drive_json(REGISTRY_NAME, CENTRAL_ALERTS_FILE)

def initialize_central_registry():
    acquire_central_lock()
    try:
        if os.path.exists(CENTRAL_ALERTS_FILE):
            # Validate it before scanning. Never silently reset a corrupt registry.
            read_central_registry()
            return

        registry = {"alerts": {}, "updated_at": None}
        # Seed the shared registry from the previous single-bot tracking file
        # so old alerts are not immediately sent again after migration.
        if os.path.exists(LEGACY_TRACKING_FILE):
            try:
                with open(LEGACY_TRACKING_FILE, "r", encoding="utf-8") as f:
                    old_data = json.load(f)
                for asin, record in old_data.get("products", {}).items():
                    coupon_sig = record.get("last_alert_signature")
                    if coupon_sig:
                        key = f"{asin}|coupon|{coupon_sig}"
                        registry["alerts"][key] = {
                            "status": "sent",
                            "sent_by": "legacy",
                            "sent_at": record.get("last_alert_at") or datetime.now().isoformat()
                        }
                    price_sig = record.get("last_price_drop_80_signature")
                    if price_sig:
                        key = f"{asin}|price_drop_80|{price_sig}"
                        registry["alerts"][key] = {
                            "status": "sent",
                            "sent_by": "legacy",
                            "sent_at": record.get("last_price_drop_80_at") or datetime.now().isoformat()
                        }
            except Exception as e:
                raise RuntimeError(
                    "Could not migrate the old tracking history. "
                    "Fix the old tracking file before starting the three bots."
                ) from e
        write_central_registry(registry)
        print("✅ Shared Telegram alert registry initialized:", CENTRAL_ALERTS_FILE)
    finally:
        release_central_lock()

def reserve_central_alert(alert_key):
    """Atomically reserve an alert so another bot should not send it concurrently."""
    token = str(uuid.uuid4())
    now = time.time()
    acquire_central_lock()
    try:
        registry = read_central_registry()
        record = registry["alerts"].get(alert_key)
        if record:
            status = record.get("status")
            if status == "sent":
                return None
            if status == "reserved":
                try:
                    reserved_at = datetime.fromisoformat(record.get("reserved_at", "")).timestamp()
                except Exception:
                    reserved_at = 0
                if now - reserved_at < RESERVATION_TTL_SECONDS:
                    return None

        registry["alerts"][alert_key] = {
            "status": "reserved",
            "reserved_by": BOT_ID,
            "reservation_token": token,
            "reserved_at": datetime.now().isoformat()
        }
        write_central_registry(registry)
        return token
    finally:
        release_central_lock()

def finish_central_alert(alert_key, token, success):
    acquire_central_lock()
    try:
        registry = read_central_registry()
        record = registry["alerts"].get(alert_key)
        if not record or record.get("reservation_token") != token:
            print("⚠️ Central reservation changed before finalizing:", alert_key)
            return
        if success:
            record.update({
                "status": "sent",
                "sent_by": BOT_ID,
                "sent_at": datetime.now().isoformat()
            })
            record.pop("reservation_token", None)
            record.pop("reserved_at", None)
        else:
            # Allow another attempt after a definite send failure.
            registry["alerts"].pop(alert_key, None)
        write_central_registry(registry)
    finally:
        release_central_lock()

def send_telegram_alert_once(alert_key, title, asin, price, offer, image_url):
    token = reserve_central_alert(alert_key)
    if token is None:
        print("⏭️ Duplicate or already-reserved alert skipped:", asin)
        return False
    try:
        sent = send_telegram_alert(title, asin, price, offer, image_url)
    except Exception:
        finish_central_alert(alert_key, token, False)
        raise
    finish_central_alert(alert_key, token, sent)
    return sent


# ------------------------------------------------------------
# PAGE URL BUILDER
# ------------------------------------------------------------

def build_page_url(search_url, page_number):
    """
    Keep the original search URL intact and add/replace only page=N.
    Page 1 uses the original URL exactly.
    """

    if page_number <= 1:
        return search_url

    if re.search(r"([?&])page=\d+", search_url):
        return re.sub(
            r"([?&])page=\d+",
            lambda match: f"{match.group(1)}page={page_number}",
            search_url,
            count=1,
        )

    separator = "&" if "?" in search_url else "?"
    return f"{search_url}{separator}page={page_number}"


async def extract_total_page_count(page):
    """Read Amazon's result count and estimate the required page count."""
    texts = []

    for selector in [".s-result-count", "[data-component-type='s-result-info-bar']"]:
        try:
            locator = page.locator(selector).first
            if await locator.count():
                value = (await locator.inner_text()).strip()
                if value:
                    texts.append(value)
        except Exception:
            pass

    if not texts:
        try:
            texts.append((await page.locator("body").inner_text())[:8000])
        except Exception:
            pass

    pattern = re.compile(
        r"\b[\d,]+\s*[-–]\s*[\d,]+\s+of\s+"
        r"(?:(?:over|more than)\s+)?([\d,]+)\s+results?\b",
        re.IGNORECASE,
    )

    for text in texts:
        match = pattern.search(text)
        if not match:
            continue

        total_results = parse_price(match.group(1))
        if total_results is not None and total_results > 0:
            page_count = int((int(total_results) + 47) // 48)
            return min(page_count, MAX_PAGES_PER_SEARCH)

    return None

# ------------------------------------------------------------
# PRICE HELPERS
# ------------------------------------------------------------

def parse_price(text):
    if not text:
        return None

    match = re.search(r"([\d,]+(?:\.\d{1,2})?)", text)
    if not match:
        return None

    try:
        return float(match.group(1).replace(",", ""))
    except ValueError:
        return None


def extract_reference_price(card_text, current_price):
    patterns = [
        r"\bWas\s*:?\s*SAR\s*([\d,]+(?:\.\d{1,2})?)",
        r"\bList\s+Price\s*:?\s*SAR\s*([\d,]+(?:\.\d{1,2})?)",
    ]

    for pattern in patterns:
        match = re.search(pattern, card_text, re.IGNORECASE)
        if match:
            old_price = parse_price(match.group(1))
            if old_price and current_price and old_price > current_price:
                return old_price

    return current_price


def extract_amazon_displayed_discount(card_text):
    patterns = [
        (
            r"\b(?:deal|discount|you save|save)\s*:?\s*"
            r"(\d+(?:\.\d+)?)\s*%\s*(?:off)?\b"
        ),
        r"\b(\d+(?:\.\d+)?)\s*%\s*off\b",
    ]

    for pattern in patterns:
        for match in re.finditer(
            pattern,
            card_text,
            re.IGNORECASE,
        ):
            start = max(0, match.start() - 70)
            end = min(len(card_text), match.end() + 70)
            context = card_text[start:end].lower()

            if "coupon" in context:
                continue
            if "on any" in context or "or more" in context:
                continue

            try:
                percent = float(match.group(1))
            except (ValueError, IndexError):
                continue

            if 0 < percent <= 100:
                return percent

    return None


def extract_price_drop(card_text, current_price, reference_price):
    if not current_price or current_price <= 0:
        return None

    displayed_percent = extract_amazon_displayed_discount(card_text)

    if reference_price and reference_price > current_price:
        saving = reference_price - current_price

        if displayed_percent is not None:
            discount = displayed_percent
            source = "Amazon displayed percentage"
        else:
            discount = saving / reference_price * 100
            source = "calculated from Was/current price"

        return {
            "type": "price_drop",
            "description": "Product price reduction",
            "reference_price": round(reference_price, 2),
            "current_price": round(current_price, 2),
            "saving": round(saving, 2),
            "discount_percent": round(discount, 2),
            "source": source,
        }

    if displayed_percent is not None:
        return {
            "type": "price_drop",
            "description": "Product price reduction",
            "reference_price": None,
            "current_price": round(current_price, 2),
            "saving": None,
            "discount_percent": round(displayed_percent, 2),
            "source": "Amazon displayed percentage",
        }

    return None


# ------------------------------------------------------------
# COUPON / TEXT OFFER PARSER
# ------------------------------------------------------------

def parse_offer(card_text, current_price, reference_price):
    if not card_text:
        return None

    # "You pay SAR 36.80 with coupon"
    match = re.search(
        r"You\s+pay\s+SAR\s*([\d,]+(?:\.\d{1,2})?)"
        r"\s+with\s+coupon",
        card_text,
        re.IGNORECASE,
    )

    if match and current_price and current_price > 0:
        pay_price = parse_price(match.group(1))
        saving = current_price - pay_price

        if pay_price is not None and saving > 0:
            return {
                "type": "you_pay_coupon",
                "description": "You pay with coupon",
                "pay_price": round(pay_price, 2),
                "saving": round(saving, 2),
                "discount_percent": round(
                    saving / current_price * 100, 2
                ),
                "quantity": 1,
            }

    # "SAR 12.90 off coupon applied"
    match = re.search(
        r"SAR\s*([\d,]+(?:\.\d{1,2})?)"
        r"\s*off\s+coupon\s+applied",
        card_text,
        re.IGNORECASE,
    )

    if match and current_price and current_price > 0:
        saving = parse_price(match.group(1))

        if saving is not None and 0 < saving < current_price:
            return {
                "type": "cash_coupon",
                "description": "Cash coupon",
                "pay_price": round(current_price - saving, 2),
                "saving": round(saving, 2),
                "discount_percent": round(
                    saving / current_price * 100, 2
                ),
                "quantity": 1,
            }

    # "Save 50% on any 1", "Apply 40% coupon", etc.
    percent_patterns = [
        (
            r"\b(?:save|apply|clip)\s+"
            r"(\d+(?:\.\d+)?)\s*%\s*"
            r"(?:off\s*)?"
            r"(?:with\s*)?"
            r"(?:(?:coupon\b\s+)?on\s+any\s+(?P<quantity>\d+)"
            r"(?:\s+or\s+more)?|coupon\b)?"
        ),
        r"\b(\d+(?:\.\d+)?)\s*%\s*off\s+coupon\b",
    ]

    for pattern in percent_patterns:
        match = re.search(pattern, card_text, re.IGNORECASE)
        if not match:
            continue

        matched_text = match.group(0).lower()
        has_quantity_condition = "on any" in matched_text
        has_coupon_word = "coupon" in matched_text

        if not has_quantity_condition and not has_coupon_word:
            continue

        percent = float(match.group(1))
        quantity_text = match.groupdict().get("quantity")
        quantity = int(quantity_text) if quantity_text else 1

        if percent <= 0:
            continue

        if current_price and current_price > 0:
            saving = current_price * percent / 100
            pay_price = current_price - saving
        else:
            saving = None
            pay_price = None

        return {
            "type": "percent_offer",
            "description": match.group(0).strip(),
            "pay_price": (
                round(pay_price, 2)
                if pay_price is not None
                else None
            ),
            "saving": (
                round(saving, 2)
                if saving is not None
                else None
            ),
            "discount_percent": round(percent, 2),
            "quantity": quantity,
        }

    # "Buy 3 for SAR 29.99"
    bundle_patterns = [
        (
            r"\bbuy\s+(\d+)\s+for\s+"
            r"(?:SAR\s*)?([\d,]+(?:\.\d{1,2})?)"
            r"\s*(?:SAR|ريال)?"
        ),
        (
            r"\b(\d+)\s+for\s+SAR\s*"
            r"([\d,]+(?:\.\d{1,2})?)"
        ),
    ]

    for pattern in bundle_patterns:
        match = re.search(pattern, card_text, re.IGNORECASE)
        if not match:
            continue

        quantity = int(match.group(1))
        bundle_price = parse_price(match.group(2))

        if quantity <= 0 or bundle_price is None:
            continue

        if not reference_price or reference_price <= 0:
            return {
                "type": "bundle_offer_unverified",
                "description": match.group(0).strip(),
                "pay_price": round(bundle_price, 2),
                "saving": None,
                "discount_percent": None,
                "quantity": quantity,
            }

        regular_total = reference_price * quantity
        saving = regular_total - bundle_price

        if saving <= 0:
            return {
                "type": "bundle_offer_unverified",
                "description": match.group(0).strip(),
                "pay_price": round(bundle_price, 2),
                "saving": None,
                "discount_percent": None,
                "quantity": quantity,
            }

        return {
            "type": "bundle_offer",
            "description": match.group(0).strip(),
            "pay_price": round(bundle_price, 2),
            "saving": round(saving, 2),
            "discount_percent": round(
                saving / regular_total * 100, 2
            ),
            "quantity": quantity,
        }

    return None


# ------------------------------------------------------------
# PRODUCT TITLE / PRICE / IMAGE
# ------------------------------------------------------------

async def extract_title(card, card_text):
    for selector in [
        "h2",
        "[data-cy='title-recipe']",
        ".a-size-base-plus",
        ".a-size-medium",
    ]:
        try:
            element = card.locator(selector).first
            if await element.count():
                title = (await element.inner_text()).strip()
                if len(title) > 8:
                    return title
        except Exception:
            pass

    ignored = re.compile(
        r"amazon.s choice|best seller|out of 5 stars|"
        r"bought in past month|free delivery|add to cart|"
        r"coupon|was:|sponsored|save \d+%|buy \d+ for",
        re.IGNORECASE,
    )

    for line in card_text.splitlines():
        line = re.sub(r"\s+", " ", line).strip()
        line = re.sub(
            r"^(Amazon's Choice|Best Seller)\s*",
            "",
            line,
            flags=re.IGNORECASE,
        ).strip()

        if len(line) > 25 and not ignored.search(line):
            return line[:300]

    return "Title unavailable"


async def extract_price(card, card_text):
    element = card.locator(".a-price .a-offscreen").first

    if await element.count():
        value = (await element.inner_text()).strip()
        price = parse_price(value)
        if price is not None:
            return value, price

    current_text = re.split(
        r"\bWas\s*:?",
        card_text,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]

    match = re.search(
        r"SAR\s*([\d,]+(?:\.\d{1,2})?)",
        current_text,
        re.IGNORECASE,
    )

    if match:
        value = f"SAR {match.group(1)}"
        return value, parse_price(value)

    return "Price unavailable", None


async def extract_image_url(card):
    images = card.locator("img.s-image")
    if await images.count() == 0:
        images = card.locator("img")

    for i in range(min(await images.count(), 3)):
        image = images.nth(i)

        for attribute in ("src", "data-src", "data-old-hires"):
            value = await image.get_attribute(attribute)
            if value and value.startswith("https://"):
                return value

        dynamic_images = await image.get_attribute(
            "data-a-dynamic-image"
        )

        if dynamic_images:
            try:
                image_map = json.loads(dynamic_images)
                for value in image_map:
                    if value.startswith("https://"):
                        return value
            except Exception:
                pass

    return None


# ------------------------------------------------------------
# TELEGRAM
# ------------------------------------------------------------

def build_telegram_caption(title, asin, price, offer):
    product_url = f"https://www.amazon.sa/dp/{asin}"

    if offer.get("type") == "price_drop_80":
        caption = (
            "🚨🔥 <b>80%+ PRICE DROP — NOT A COUPON</b>\n\n"
            f"<b>{html.escape(title[:300])}</b>\n"
        )

        reference_price = offer.get("reference_price")
        if reference_price is not None:
            caption += f"Was: SAR {reference_price:.2f}\n"

        caption += f"Now: SAR {price:.2f}\n"

        if offer.get("saving") is not None:
            caption += f"Price saving: SAR {offer['saving']:.2f}\n"

        caption += (
            f"Price drop: {offer['discount_percent']:.2f}%\n"
            f"Source: {html.escape(offer.get('source', ''))}\n\n"
        )

    else:
        description = html.escape(
            offer.get("description") or offer.get("type", "Offer")
        )

        caption = (
            "🔥 <b>Amazon deal</b>\n\n"
            f"<b>{html.escape(title[:300])}</b>\n"
            f"Listed price: SAR {price:.2f}\n"
            f"Offer: {description}\n"
        )

        if offer.get("type") == "bundle_offer":
            caption += (
                f"Bundle quantity: {offer['quantity']}\n"
                f"Bundle price: SAR {offer['pay_price']:.2f}\n"
                f"Estimated total saving: SAR {offer['saving']:.2f}\n"
                f"Discount: {offer['discount_percent']:.2f}%\n"
            )
        else:
            if offer.get("saving") is not None:
                caption += f"Saving: SAR {offer['saving']:.2f}\n"

            if offer.get("pay_price") is not None:
                caption += f"Pay price: SAR {offer['pay_price']:.2f}\n"

            if offer.get("discount_percent") is not None:
                caption += (
                    f"Discount: "
                    f"{offer['discount_percent']:.2f}%\n"
                )

            if offer.get("quantity", 1) > 1:
                caption += (
                    f"Minimum quantity: {offer['quantity']}\n"
                )

        caption += "\n"

    caption += (
        f'<a href="{html.escape(product_url, quote=True)}">'
        "Open product</a>"
    )
    return caption


def send_telegram_alert(title, asin, price, offer, image_url):
    caption = build_telegram_caption(title, asin, price, offer)
    base_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

    if image_url:
        try:
            response = requests.post(
                f"{base_url}/sendPhoto",
                json={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "photo": image_url,
                    "caption": caption,
                    "parse_mode": "HTML",
                },
                timeout=30,
            )
            result = response.json()

            if response.ok and result.get("ok"):
                return True

            print("⚠️ Telegram photo error:", result)

        except Exception as e:
            print("⚠️ Photo send error:", e)

    try:
        response = requests.post(
            f"{base_url}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": caption,
                "parse_mode": "HTML",
                "disable_web_page_preview": False,
            },
            timeout=30,
        )
        result = response.json()

        if response.ok and result.get("ok"):
            return True

        print("⚠️ Telegram message error:", result)
        return False

    except Exception as e:
        print("⚠️ Telegram request error:", e)
        return False


# ------------------------------------------------------------
# PROCESS PRODUCT
# ------------------------------------------------------------

product_locks = {}


CARD_SNAPSHOT_JS = r"""
elements => elements.map(element => {
    const titleElement = element.querySelector('h2')
        || element.querySelector('[data-cy="title-recipe"]')
        || element.querySelector('.a-size-base-plus')
        || element.querySelector('.a-size-medium');
    const priceElement = element.querySelector('.a-price .a-offscreen');
    const imageElement = element.querySelector('img.s-image')
        || element.querySelector('img');
    let imageUrl = null;
    if (imageElement) {
        for (const attribute of ['data-old-hires', 'src', 'data-src']) {
            const value = imageElement.getAttribute(attribute);
            if (value && /^https?:\/\//i.test(value)) {
                imageUrl = value;
                break;
            }
        }
        if (!imageUrl) {
            const dynamicImages = imageElement.getAttribute('data-a-dynamic-image');
            if (dynamicImages) {
                try {
                    const parsed = JSON.parse(dynamicImages);
                    imageUrl = Object.keys(parsed).find(value => /^https?:\/\//i.test(value)) || null;
                } catch (_) {}
            }
        }
    }
    return {
        asin: element.getAttribute('data-asin'),
        text: element.innerText || '',
        title: titleElement ? titleElement.innerText : '',
        price_text: priceElement ? priceElement.innerText : '',
        image_url: imageUrl
    };
})
"""


def fallback_title_from_text(card_text):
    ignored = re.compile(
        r"amazon.s choice|best seller|out of 5 stars|"
        r"bought in past month|free delivery|add to cart|coupon|was:|sponsored",
        re.IGNORECASE,
    )
    for line in card_text.splitlines():
        line = re.sub(r"\s+", " ", line).strip()
        line = re.sub(r"^(Amazon's Choice|Best Seller)\s*", "", line, flags=re.IGNORECASE).strip()
        if len(line) > 25 and not ignored.search(line):
            return line[:300]
    return "Title unavailable"


def fallback_price_from_text(card_text):
    current_text = re.split(r"\bWas\s*:", card_text, maxsplit=1, flags=re.IGNORECASE)[0]
    match = re.search(r"SAR\s*([\d,]+(?:\.\d{1,2})?)", current_text, re.IGNORECASE)
    if match:
        value = f"SAR {match.group(1)}"
        return value, parse_price(value)
    return "Price unavailable", None


async def process_card_locked(search_url, page_number, index, search_index, snapshot):
    card_text = snapshot.get("text", "")
    asin = snapshot.get("asin")
    if not asin:
        return False, False

    title = (snapshot.get("title") or "").strip()
    if len(title) <= 8:
        title = fallback_title_from_text(card_text)

    price_text = (snapshot.get("price_text") or "").strip()
    current_price = parse_price(price_text)
    if current_price is None:
        price_text, current_price = fallback_price_from_text(card_text)

    reference_price = extract_reference_price(card_text, current_price)
    offer = parse_offer(card_text, current_price, reference_price)
    price_drop = extract_price_drop(card_text, current_price, reference_price)
    product_url = f"https://www.amazon.sa/dp/{asin}"

    if VERBOSE_PRODUCT_LOGS:
        print(f"Search {search_index}, page {page_number}, product {index}: {title}")
        print("Price:", price_text)
        print("Link:", product_url)

    previous = tracking_data["products"].get(asin, {})
    previous_coupon_alert = previous.get("last_alert_signature")
    previous_80_alert = previous.get("last_price_drop_80_signature")
    image_url = snapshot.get("image_url") or previous.get("image_url")

    appearance = {
        "search_index": search_index,
        "search_url": search_url,
        "page": page_number,
        "seen_at": datetime.now().isoformat(),
    }
    appearances = list(previous.get("appearances", []))
    # Avoid storing the same search/page appearance more than once per run.
    if not any(
        item.get("search_url") == search_url
        and item.get("page") == page_number
        for item in appearances[-100:]
    ):
        appearances.append(appearance)
    appearances = appearances[-100:]

    record = {
        **previous,
        "asin": asin,
        "title": title,
        "url": product_url,
        "image_url": image_url,
        "current_price": current_price,
        "reference_price": reference_price,
        "price_text": price_text,
        "offer": offer,
        "price_drop": price_drop,
        "last_seen": datetime.now().isoformat(),
        "page_seen": page_number,
        "search_url": search_url,
        "appearances": appearances,
    }

    coupon_alert_sent = False
    price_drop_alert_sent = False
    if not offer:
        # Do not clear alert history from a single listing. The same ASIN may
        # appear in another search link with the coupon visible. Expiry is
        # determined once after the complete scan, below.
        pass
    else:
        if VERBOSE_PRODUCT_LOGS:
            print("Offer:", offer["description"])
        coupon_discount = offer.get("discount_percent")
        if coupon_discount is None:
            if VERBOSE_PRODUCT_LOGS:
                print("Offer saved; discount cannot be verified from the card.")
        else:
            offer_description = str(offer.get("description", "")).lower()
            is_coupon_offer = (
                "coupon" in offer_description
                or offer.get("type") in {"you_pay_coupon", "cash_coupon"}
            )
            offer_threshold = (
                MIN_DISCOUNT_PERCENT
                if is_coupon_offer
                else MIN_PROMOTION_DISCOUNT_PERCENT
            )
            if coupon_discount < offer_threshold:
                if VERBOSE_PRODUCT_LOGS:
                    offer_kind = "coupon" if is_coupon_offer else "promotion"
                    print(
                        f"{offer_kind.title()} discount: {coupon_discount:.2f}% "
                        f"(below {offer_threshold:.0f}%)."
                    )
            else:
                coupon_signature = (
                    f"{current_price}|{offer['type']}|{offer.get('pay_price')}|"
                    f"{offer.get('saving')}|{offer.get('discount_percent')}|{offer.get('quantity')}"
                )
                RUN_COUPON_SIGNATURES.setdefault(asin, set()).add(coupon_signature)
                if coupon_signature == previous_coupon_alert:
                    if VERBOSE_PRODUCT_LOGS:
                        print("Same coupon/offer already alerted.")
                elif TELEGRAM_READY:
                    offer_kind = "coupon" if is_coupon_offer else "promotion"
                    print(f"🔥 Qualifying {offer_kind} ({coupon_discount:.2f}%): Search {search_index}, page {page_number}, product {index}")
                    tracking_data["products"][asin] = record
                    sent = await asyncio.to_thread(
                        send_telegram_alert_once,
                        f"{asin}|coupon|{coupon_signature}",
                        title, asin, current_price or 0, offer, image_url
                    )
                    if sent:
                        record["last_alert_signature"] = coupon_signature
                        record["last_alert_at"] = datetime.now().isoformat()
                        coupon_alert_sent = True
                        save_tracking()
                        print("✅ Coupon/offer alert sent:", product_url)
                    else:
                        print("⚠️ Coupon/offer alert could not be sent.")
                else:
                    print("Qualifying offer found, but Telegram is not configured.")

    price_drop_discount = price_drop.get("discount_percent") if price_drop else None
    if price_drop and price_drop_discount is not None and price_drop_discount >= MIN_PRICE_DROP_PERCENT:
        price_drop_signature = f"{price_drop_discount:.2f}%"
        RUN_PRICE_DROP_SIGNATURES.setdefault(asin, set()).add(price_drop_signature)
        if price_drop_signature == previous_80_alert:
            if VERBOSE_PRODUCT_LOGS:
                print("Same 80%+ price drop already alerted.")
        elif TELEGRAM_READY:
            print(f"🚨 80%+ price drop ({price_drop_discount:.2f}%): Search {search_index}, page {page_number}, product {index}")
            price_drop_alert = {**price_drop, "type": "price_drop_80"}
            tracking_data["products"][asin] = record
            sent = await asyncio.to_thread(
                send_telegram_alert_once,
                f"{asin}|price_drop_80|{price_drop_signature}",
                title, asin, current_price or 0, price_drop_alert, image_url
            )
            if sent:
                record["last_price_drop_80_signature"] = price_drop_signature
                record["last_price_drop_80_at"] = datetime.now().isoformat()
                price_drop_alert_sent = True
                save_tracking()
                print("✅ 80%+ price-drop alert sent:", product_url)
            else:
                print("⚠️ Price-drop alert could not be sent.")
        else:
            print("Qualifying 80%+ price drop found, but Telegram is not configured.")
    # Do not clear an 80%+ alert from one listing. Another search result
    # for the same ASIN may still show the qualifying drop.

    tracking_data["products"][asin] = record
    return True, coupon_alert_sent or price_drop_alert_sent


async def process_card(snapshot, search_url, page_number, index, search_index):
    asin = snapshot.get("asin")
    if not asin:
        return None, False, False
    lock = product_locks.setdefault(asin, asyncio.Lock())
    async with lock:
        saved, alerted = await process_card_locked(
            search_url, page_number, index, search_index, snapshot
        )
    return asin, saved, alerted


# ------------------------------------------------------------
# SCAN ONE PAGE
# ------------------------------------------------------------

async def _scan_one_page(context, search_url, page_number, search_index):
    """Scan one page. A disabled Next button is strong end-of-results evidence.

    A 503, CAPTCHA, error page, or unconfirmed empty page is never treated as
    end-of-results. Such pages are retried and, if still unresolved, remain
    resumable on the next run.
    """
    page_url = build_page_url(search_url, page_number)
    page = await context.new_page()

    def empty_result(success, empty=False, end_confirmed=False, total_pages=None):
        return {
            "page_number": page_number,
            "success": success,
            "empty": empty,
            "end_confirmed": end_confirmed,
            "next_state": "unknown",
            "asins": set(),
            "total_pages": total_pages,
            "products_saved": 0,
            "alerts_sent": 0,
        }

    try:
        cards = page.locator('[data-component-type="s-search-result"]')

        for attempt in range(1, MAX_ATTEMPTS_PER_PAGE + 1):
            print(
                f"\nSearch {search_index}, page {page_number}, "
                f"attempt {attempt}/{MAX_ATTEMPTS_PER_PAGE}"
            )
            print("URL:", page_url)

            try:
                response = await page.goto(
                    page_url,
                    wait_until="commit",
                    timeout=60000,
                )
                status = response.status if response else None
                title = await page.title()
                body_text = ""
                title_lower = title.lower()
                likely_error_title = (
                    "نعتذر" in title
                    or "sorry" in title_lower
                    or "robot check" in title_lower
                    or "captcha" in title_lower
                )

                if status != 200 or likely_error_title:
                    try:
                        body_text = (await page.locator("body").inner_text(timeout=2500))[:1500]
                    except Exception:
                        pass
                    count = 0
                else:
                    try:
                        await cards.first.wait_for(timeout=9000)
                    except Exception:
                        pass
                    count = await cards.count()
                    try:
                        body_text = (await page.locator("body").inner_text(timeout=2500))[:5000]
                    except Exception:
                        body_text = ""

                # Inspect Next only on a page that loaded without an HTTP error.
                next_state = "unknown"
                next_href = ""
                next_disabled = False
                next_exists = False
                if status == 200 and not likely_error_title:
                    try:
                        next_button = page.locator("a.s-pagination-next").first
                        if await next_button.count() > 0:
                            next_exists = True
                            next_class = (await next_button.get_attribute("class") or "").lower()
                            next_aria = (await next_button.get_attribute("aria-disabled") or "").lower()
                            next_href = (await next_button.get_attribute("href") or "").strip()
                            next_disabled = (
                                "s-pagination-disabled" in next_class
                                or next_aria == "true"
                                or (not next_href and "s-pagination-disabled" in next_class)
                            )
                            next_state = "disabled" if next_disabled else ("enabled" if next_href else "unknown")
                    except Exception:
                        pass

                print("HTTP status:", status)
                print("Final URL:", page.url)
                print("Page title:", title)
                print("Product cards:", count)
                print("Next button:", next_state)

                body_lower = body_text.lower()
                is_amazon_error = (
                    "نعتذر" in body_text
                    or "sorry" in body_lower
                    or "robot check" in body_lower
                    or "captcha" in body_lower
                    or "automated access" in body_lower
                    or "request was throttled" in body_lower
                    or status in {403, 408, 425, 429, 503}
                    or (status is not None and status >= 500)
                )

                if status == 200 and count > 0 and not is_amazon_error:
                    total_pages = await extract_total_page_count(page) if page_number == 1 else None
                    asins = set()
                    products_saved = 0
                    alerts_sent = 0

                    try:
                        card_snapshots = await cards.evaluate_all(CARD_SNAPSHOT_JS)
                    except Exception as e:
                        raise RuntimeError(
                            f"Could not extract product cards: {type(e).__name__}: {e}"
                        ) from e

                    async def process_page_product(index):
                        async with PRODUCT_SEMAPHORE:
                            try:
                                snapshot = card_snapshots[index - 1]
                                asin, saved, alerted = await process_card(
                                    snapshot, search_url, page_number, index, search_index
                                )
                                return asin, saved, alerted, None
                            except Exception as e:
                                return None, False, False, e

                    card_results = await asyncio.gather(
                        *[process_page_product(index) for index in range(1, count + 1)],
                        return_exceptions=True,
                    )

                    for index, product_result in enumerate(card_results, start=1):
                        if isinstance(product_result, Exception):
                            print(
                                f"⚠️ Product parse error on page {page_number}, "
                                f"product {index}: {type(product_result).__name__}: {product_result}"
                            )
                            continue

                        asin, saved, alerted, error = product_result
                        if error is not None:
                            print(
                                f"⚠️ Product parse error on page {page_number}, "
                                f"product {index}: {type(error).__name__}: {error}"
                            )
                            continue
                        if asin:
                            asins.add(asin)
                        if saved:
                            products_saved += 1
                        if alerted:
                            alerts_sent += 1

                    print(
                        f"Search {search_index}, page {page_number}: "
                        f"{count} cards, {products_saved} saved, {alerts_sent} alerts."
                    )
                    return {
                        "page_number": page_number,
                        "success": True,
                        "empty": False,
                        "end_confirmed": next_disabled,
                        "next_state": next_state,
                        "next_href": next_href,
                        "next_exists": next_exists,
                        "asins": asins,
                        "total_pages": total_pages,
                        "products_saved": products_saved,
                        "alerts_sent": alerts_sent,
                    }

                # An empty page is only terminal if Amazon clearly confirms it.
                explicit_no_results = any(marker in body_lower for marker in [
                    "no results for",
                    "did not match any products",
                    "no results found",
                    "there are no results",
                ])

                if status == 200 and count == 0 and not is_amazon_error:
                    if next_disabled or explicit_no_results:
                        print("Confirmed empty/end page; no product cards.")
                        result = empty_result(
                            success=True,
                            empty=True,
                            end_confirmed=True,
                            total_pages=0 if page_number == 1 else None,
                        )
                        result["next_state"] = next_state
                        return result

                    print(
                        "HTTP 200 but no product cards and no reliable end marker. "
                        "This is NOT considered end-of-results; retrying."
                    )
                else:
                    print("Amazon error or incomplete results; this is NOT end-of-results.")
                    if body_text:
                        print(body_text[:500])

            except Exception as e:
                print(f"Page load/extraction error: {type(e).__name__}: {e}")

            if attempt < MAX_ATTEMPTS_PER_PAGE:
                delay = RETRY_DELAYS_SECONDS[
                    min(attempt - 1, len(RETRY_DELAYS_SECONDS) - 1)
                ]
                print(f"Retrying page after {delay} seconds...")
                await asyncio.sleep(delay)

        print(
            f"❌ Page {page_number} could not be confirmed after "
            f"{MAX_ATTEMPTS_PER_PAGE} attempts. Keeping it resumable."
        )
        return empty_result(success=False, empty=False, end_confirmed=False)

    finally:
        try:
            await page.close()
        except Exception:
            pass


# Global cap shared by all search URLs. The page worker acquires one slot
# for a full page scan, including retries and product parsing.
PAGE_SEMAPHORE = None
PRODUCT_SEMAPHORE = None


async def scan_one_page(context, search_url, page_number, search_index):
    if PAGE_SEMAPHORE is None:
        return await _scan_one_page(
            context, search_url, page_number, search_index
        )

    async with PAGE_SEMAPHORE:
        return await _scan_one_page(
            context, search_url, page_number, search_index
        )

# ------------------------------------------------------------
# SCAN PAGES FOR ONE SEARCH IN SMALL PARALLEL BATCHES
# ------------------------------------------------------------

async def scan_one_search(context, search_url, search_index):
    """Scan each search in bounded page batches and checkpoint in page order."""
    state = tracking_data["scan_state"].get(search_url, {})
    if not state.get("completed", False) and state.get("next_page"):
        page_number = max(1, int(state["next_page"]))
        print(f"Resuming search {search_index} at page {page_number}...")
    else:
        page_number = 1

    pages_scanned = 0
    products_saved = 0
    alerts_sent = 0
    seen_asins = set()
    reported_total_pages = None

    while page_number <= MAX_PAGES_PER_SEARCH:
        batch_end = min(
            page_number + PAGE_WORKERS_PER_SEARCH - 1,
            MAX_PAGES_PER_SEARCH,
        )
        batch_pages = list(range(page_number, batch_end + 1))
        print(
            f"Search {search_index}: scanning pages {page_number}-{batch_end} "
            f"with up to {PAGE_WORKERS_PER_SEARCH} page workers."
        )
        batch_results = await asyncio.gather(
            *[
                scan_one_page(context, search_url, number, search_index)
                for number in batch_pages
            ],
            return_exceptions=True,
        )

        stop_search = False
        for number, result in zip(batch_pages, batch_results):
            if isinstance(result, Exception):
                tracking_data["scan_state"][search_url] = {
                    "next_page": number,
                    "total_pages": reported_total_pages,
                    "completed": False,
                    "last_error_at": datetime.now().isoformat(),
                }
                save_tracking()
                print(
                    f"Search {search_index}: page {number} raised "
                    f"{type(result).__name__}; saved it as the resume point."
                )
                stop_search = True
                break

            if not result.get("success"):
                tracking_data["scan_state"][search_url] = {
                    "next_page": number,
                    "total_pages": reported_total_pages,
                    "completed": False,
                    "last_error_at": datetime.now().isoformat(),
                }
                save_tracking()
                print(
                    f"Search {search_index}: page {number} was not confirmed. "
                    "Saved resume position; it will retry next run."
                )
                stop_search = True
                break

            pages_scanned += 1
            products_saved += result.get("products_saved", 0)
            alerts_sent += result.get("alerts_sent", 0)
            if result.get("total_pages") is not None:
                reported_total_pages = result["total_pages"]
                print(
                    f"Amazon result-count estimate: {reported_total_pages} page(s). "
                    "Actual end detection uses the Next button / confirmed empty page."
                )
            seen_asins.update(result.get("asins", set()))

            if result.get("end_confirmed"):
                tracking_data["scan_state"][search_url] = {
                    "next_page": None,
                    "total_pages": number,
                    "completed": True,
                    "end_reason": "AMAZON_NEXT_DISABLED",
                    "completed_at": datetime.now().isoformat(),
                }
                save_tracking()
                print(
                    f"🏁 Search {search_index}: Amazon's Next button is disabled "
                    f"on page {number}. Search completed."
                )
                stop_search = True
                break

            if result.get("empty"):
                tracking_data["scan_state"][search_url] = {
                    "next_page": None,
                    "total_pages": number - 1,
                    "completed": True,
                    "end_reason": "CONFIRMED_EMPTY_END_PAGE",
                    "completed_at": datetime.now().isoformat(),
                }
                save_tracking()
                print(
                    f"🏁 Search {search_index}: confirmed end at page {number}."
                )
                stop_search = True
                break

        if stop_search:
            break

        page_number = batch_end + 1
        tracking_data["scan_state"][search_url] = {
            "next_page": page_number,
            "total_pages": reported_total_pages,
            "completed": False,
        }
        save_tracking()

    else:
        tracking_data["scan_state"][search_url] = {
            "next_page": None,
            "total_pages": MAX_PAGES_PER_SEARCH,
            "completed": False,
            "end_reason": "MAX_PAGES_SAFETY_CAP",
            "completed_at": datetime.now().isoformat(),
        }
        save_tracking()
        print(
            f"⚠️ Search {search_index}: reached safety cap "
            f"{MAX_PAGES_PER_SEARCH}; stopped to avoid unbounded scanning."
        )

    print(
        f"Search {search_index} totals — pages: {pages_scanned}, "
        f"products saved/updated: {products_saved}, alerts: {alerts_sent}, "
        f"unique ASINs seen this run: {len(seen_asins)}"
    )
    return pages_scanned, products_saved, alerts_sent


# ------------------------------------------------------------
# SCAN ALL SEARCH LINKS
# ------------------------------------------------------------

async def scan_all_searches():
    if not SEARCH_URLS:
        print("⚠️ Add at least one Amazon search URL.")
        return

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )

        context = await browser.new_context(
            locale="en-US",
            timezone_id="Asia/Riyadh",
            viewport={
                "width": 1920,
                "height": 1080,
            },
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
            },
        )

        async def block_heavy_assets(route):
            if route.request.resource_type in {"image", "font", "media"}:
                await route.abort()
            else:
                await route.continue_()

        await context.route("**/*", block_heavy_assets)

        total_pages = 0
        total_products = 0
        total_alerts = 0

        try:
            print("Opening Amazon home...")
            home_page = await context.new_page()

            try:
                home_response = await home_page.goto(
                    HOME_URL,
                    wait_until="domcontentloaded",
                    timeout=60000,
                )
                print(
                    "Home status:",
                    home_response.status if home_response else None,
                )
            finally:
                await home_page.close()

            # Keep a bounded pool of searches so page-1 requests from all
            # URLs do not queue ahead of later pages from active searches.
            global PAGE_SEMAPHORE, PRODUCT_SEMAPHORE
            PAGE_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_PAGES)
            PRODUCT_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_PRODUCTS)

            for search_index, search_url in enumerate(SEARCH_URLS, start=1):
                print("\n" + "=" * 60)
                print(f"SEARCH {search_index}/{len(SEARCH_URLS)}")
                print(search_url)
                print("Global concurrent page limit:", MAX_CONCURRENT_PAGES)
                print("Attempts per page:", MAX_ATTEMPTS_PER_PAGE)
                print("=" * 60)

            search_results = [None] * len(SEARCH_URLS)
            search_queue = asyncio.Queue()

            for queue_index, queue_url in enumerate(SEARCH_URLS):
                search_queue.put_nowait((queue_index, queue_url))

            worker_count = min(MAX_CONCURRENT_PAGES, len(SEARCH_URLS))
            for _ in range(worker_count):
                search_queue.put_nowait(None)

            async def search_worker():
                while True:
                    job = await search_queue.get()
                    try:
                        if job is None:
                            return

                        queue_index, queue_url = job
                        search_index = queue_index + 1

                        try:
                            search_results[queue_index] = await scan_one_search(
                                context,
                                queue_url,
                                search_index,
                            )
                        except Exception as e:
                            search_results[queue_index] = e
                    finally:
                        search_queue.task_done()

            workers = [
                asyncio.create_task(search_worker())
                for _ in range(worker_count)
            ]

            await search_queue.join()
            await asyncio.gather(*workers)
            for search_index, result in enumerate(search_results, start=1):
                if isinstance(result, Exception):
                    print(
                        f"⚠️ Search {search_index} failed: "
                        f"{type(result).__name__}: {result}"
                    )
                    continue

                pages, products, alerts = result
                total_pages += pages
                total_products += products
                total_alerts += alerts

            print("\n" + "=" * 60)
            print("Search links scanned:", len(SEARCH_URLS))
            print("Pages scanned this run:", total_pages)
            print("Product appearances saved:", total_products)
            print("Telegram alerts sent:", total_alerts)

            # Only a fully successful scan can confirm that a previous offer
            # has ended. Failed/incomplete searches must preserve alert state.
            all_searches_complete = (
                all(not isinstance(result, Exception) for result in search_results)
                and all(
                    tracking_data["scan_state"].get(url, {}).get("completed", False)
                    for url in SEARCH_URLS
                )
            )
            if all_searches_complete:
                cleared_alert_states = 0
                for asin, record in tracking_data["products"].items():
                    coupon_signature = record.get("last_alert_signature")
                    if (
                        coupon_signature
                        and coupon_signature not in RUN_COUPON_SIGNATURES.get(asin, set())
                    ):
                        record.pop("last_alert_signature", None)
                        record.pop("last_alert_at", None)
                        cleared_alert_states += 1

                    price_signature = record.get("last_price_drop_80_signature")
                    if (
                        price_signature
                        and price_signature not in RUN_PRICE_DROP_SIGNATURES.get(asin, set())
                    ):
                        record.pop("last_price_drop_80_signature", None)
                        record.pop("last_price_drop_80_at", None)
                        cleared_alert_states += 1

                if cleared_alert_states:
                    save_tracking()
                print("Expired alert states cleared after complete scan:", cleared_alert_states)
            else:
                print("Incomplete scan; preserving all previous alert history.")

            print("Tracking file:", TRACKING_FILE)
            print("=" * 60)

        finally:
            try:
                await context.close()
            except Exception:
                pass

            try:
                await browser.close()
            except Exception:
                pass


initialize_central_registry()

async def run_worker():
    continuous = os.environ.get("CONTINUOUS_MODE", "false").lower() == "true"
    window_seconds = int(os.environ.get("WORKER_WINDOW_MINUTES", "345")) * 60
    deadline = time.monotonic() + window_seconds
    while True:
        try:
            await scan_all_searches()
        except Exception as exc:
            print(f"Scan failed: {type(exc).__name__}: {exc}")
            if not continuous:
                raise
        if not continuous or time.monotonic() + 600 >= deadline:
            break
        print("Waiting 10 minutes after this scan finished.")
        await asyncio.sleep(600)

if __name__ == "__main__":
    asyncio.run(run_worker())





