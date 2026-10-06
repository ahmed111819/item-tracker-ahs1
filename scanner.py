# GitHub Actions adapter: credentials and persistent files stay in Google Drive/GitHub Secrets.
import asyncio, io, json, os, socket, ssl, tempfile, threading, time
from datetime import datetime
from pathlib import Path
import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload
from playwright.async_api import async_playwright

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("Missing TELEGRAM_BOT_TOKEN Actions secret.")
TELEGRAM_CHAT_ID = "-1004335063743"
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
TELEGRAM_TOPIC_DISCOUNTS, TELEGRAM_TOPIC_NEW, TELEGRAM_TOPIC_MULTIPACK = 40, 2, 3
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
GOOGLE_SERVICE_ACCOUNT_JSON_FILE = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON_FILE", "").strip()
DRIVE_SESSION_FOLDER_ID = os.environ.get("DRIVE_SESSION_FOLDER_ID", "").strip()
DRIVE_TRACKING_FOLDER_ID = os.environ.get("DRIVE_TRACKING_FOLDER_ID", "").strip()
if not GOOGLE_SERVICE_ACCOUNT_JSON and not GOOGLE_SERVICE_ACCOUNT_JSON_FILE:
    raise RuntimeError("Missing Google service-account JSON; set GOOGLE_SERVICE_ACCOUNT_JSON or GOOGLE_SERVICE_ACCOUNT_JSON_FILE.")
if not DRIVE_SESSION_FOLDER_ID or not DRIVE_TRACKING_FOLDER_ID:
    raise RuntimeError("Missing Drive folder ID repository variables.")
PROJECT_DIR = os.environ.get("RUNNER_TEMP", tempfile.gettempdir())
os.makedirs(PROJECT_DIR, exist_ok=True)
SESSION_FILE = os.path.join(PROJECT_DIR, "amazon_storage_state.json")
YALLA_TRACKING_FILE = os.path.join(PROJECT_DIR, "tracking_amazonyalla.json")
_drive = None
_tracking_id = None
_drive_write_lock = threading.Lock()

def drive_service():
    global _drive
    if _drive is None:
        if GOOGLE_SERVICE_ACCOUNT_JSON:
            info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
        else:
            info = json.loads(Path(GOOGLE_SERVICE_ACCOUNT_JSON_FILE).read_text(encoding="utf-8"))
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/drive"])
        _drive = build("drive", "v3", credentials=creds, cache_discovery=False)
    return _drive

def find_drive_file(folder_id, name):
    safe_name = name.replace("'", "\\'")
    result = drive_service().files().list(
        q=f"'{folder_id}' in parents and name = '{safe_name}' and trashed = false",
        pageSize=100, fields="files(id,name,mimeType)",
        supportsAllDrives=True, includeItemsFromAllDrives=True).execute()
    files = result.get("files", [])
    if len(files) != 1:
        raise RuntimeError(f"Expected one {name} in Drive folder; found {len(files)}. Check its name and folder permissions.")
    return files[0]["id"]

def download_drive_file(file_id):
    request = drive_service().files().get_media(fileId=file_id, supportsAllDrives=True)
    stream = io.BytesIO()
    downloader = MediaIoBaseDownload(stream, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return stream.getvalue()

def upload_drive_file(file_id, raw):
    # Replacing the same Drive file with the same bytes is safe to retry if
    # the TLS connection closes after the request reaches Google.
    transient_http_codes = {429, 500, 502, 503, 504}
    for attempt in range(1, 6):
        media = MediaIoBaseUpload(io.BytesIO(raw), mimetype="application/json", resumable=False)
        try:
            drive_service().files().update(
                fileId=file_id,
                media_body=media,
                supportsAllDrives=True,
            ).execute(num_retries=2)
            print(f"✅ Drive tracking upload succeeded (attempt {attempt})")
            return
        except HttpError as exc:
            status = getattr(exc.resp, "status", None)
            if status not in transient_http_codes or attempt == 5:
                raise
            print(f"⚠️ Temporary Google Drive HTTP {status}; retry {attempt}/5")
        except (ssl.SSLError, socket.timeout, TimeoutError, ConnectionError, OSError) as exc:
            if attempt == 5:
                raise
            print(f"⚠️ Temporary Google Drive connection error ({type(exc).__name__}); retry {attempt}/5")
        time.sleep(min(2 ** attempt, 16))

def load_yalla_tracking():
    global _tracking_id
    _tracking_id = find_drive_file(DRIVE_TRACKING_FOLDER_ID, "tracking_amazonyalla.json")
    raw = download_drive_file(_tracking_id)
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("products"), dict):
        raise ValueError("Drive tracking file is invalid; no data was overwritten.")
    data.setdefault("meta", {})
    Path(YALLA_TRACKING_FILE).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data

def save_yalla_tracking(data):
    if not _tracking_id:
        raise RuntimeError("Existing Drive tracking file was not loaded; refusing to create a new baseline.")
    raw = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    temp = YALLA_TRACKING_FILE + ".tmp"
    with _drive_write_lock:
        with open(temp, "wb") as f:
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, YALLA_TRACKING_FILE)
        upload_drive_file(_tracking_id, raw)

def prepare_amazon_session():
    file_id = find_drive_file(DRIVE_SESSION_FOLDER_ID, "state.json")
    raw = download_drive_file(file_id)
    state = json.loads(raw.decode("utf-8"))
    if not isinstance(state, dict) or not isinstance(state.get("cookies"), list):
        raise ValueError("Amazon storage state is invalid; no session data was changed.")
    path = Path(SESSION_FILE)
    path.write_bytes(raw)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass

prepare_amazon_session()
tracking = load_yalla_tracking()
print("📂 Existing Drive tracking loaded:", len(tracking["products"]), "products")

def parse_yalla_price(raw_price):
    if not raw_price:
        return None
    raw_text = str(raw_price)
    if re.search(r"\bAED\b|د\.إ|درهم|dirham", raw_text, flags=re.IGNORECASE):
        return None
    match = re.search(r"\d[\d\s,.]*", raw_text)
    if not match:
        return None
    value = re.sub(r"\s", "", match.group(0)).strip(".,")
    if not value:
        return None
    # Treat the rightmost separator as decimal when both separators occur;
    # a lone separator followed by three digits is a thousands separator.
    if "," in value and "." in value:
        decimal = "," if value.rfind(",") > value.rfind(".") else "."
        thousands = "." if decimal == "," else ","
        value = value.replace(thousands, "").replace(decimal, ".")
    elif "," in value:
        tail = value.rsplit(",", 1)[1]
        value = value.replace(",", ".") if len(tail) <= 2 else value.replace(",", "")
    elif "." in value:
        tail = value.rsplit(".", 1)[1]
        if len(tail) == 3:
            value = value.replace(".", "")
    try:
        return float(value)
    except ValueError:
        return None

async def get_yalla_product_data(card):
    asin = await card.get_attribute("data-asin")
    if not asin:
        return None
    title_loc = card.locator('[data-cy="title-recipe"] h2')
    if await title_loc.count() == 0:
        title_loc = card.locator("h2")
    if await title_loc.count() == 0:
        return None
    title = (await title_loc.first.inner_text()).strip()
    if not title:
        return None
    current_loc = card.locator(".a-price .a-offscreen")
    current = parse_yalla_price(await current_loc.first.inner_text()) if await current_loc.count() else None
    old = None
    old_loc = card.locator(".a-text-price .a-offscreen")
    if await old_loc.count():
        old = parse_yalla_price(await old_loc.first.inner_text())
    discount = ((old-current)/old*100) if old and current is not None and old > current else None
    return {"asin": asin, "name": title, "price": current, "old_price": old,
            "discount_percent": discount, "url": f"https://www.amazon.sa/dp/{asin}",
            "available": current is not None}

def is_multi_pack(title):
    if not title:
        return False
    title = title.lower()
    patterns = [r"\bpack\s+of\s+(\d+)\b", r"\b(\d+)\s+count\b",
                r"\b(\d+)\s*x\s*\d+(?:[.,]\d+)?\s*(?:ml|l|g|kg)\b",
                r"\b(\d+)\s*(?:pcs|pieces)\b", r"\b(\d+)\s*[- ]pack\b",
                r"\bmulti[- ]?pack\b", r"\bcarton\b", r"\bcase\s+of\b", r"\bdozen\b"]
    for pattern in patterns:
        m = re.search(pattern, title)
        if m and (not m.groups() or int(m.group(1)) > 1):
            return True
    return False

async def get_yalla_product_image(card):
    loc = card.locator('[data-component-type="s-product-image"] img')
    if await loc.count() == 0:
        loc = card.locator("img.s-image")
    if await loc.count() == 0:
        return None
    image = loc.first
    return await image.get_attribute("src") or await image.get_attribute("data-src")

def build_alert_message(product, alerts):
    lines = []
    labels = {"new": "🆕 منتج جديد", "multi_pack": "📦 احتمال كرتون — يحتاج مراجعة",
              "discount_25": "🎉 خصم Amazon 25% أو أكثر", "strong_deal": "🔥 خصم قوي", "price_drop": "📉 انخفاض في السعر",
              "back_in_stock_strong": "🔄 عاد للتوفر + خصم قوي!"}
    lines.extend(labels[a] for a in alerts if a in labels)
    lines += ["", f"📝 {product['name']}"]
    if product.get("price") is not None:
        lines.append(f"💰 السعر الحالي: {product['price']:.2f} SAR")
    if product.get("old_price") is not None:
        lines.append(f"🏷️ السعر السابق المعروض: {product['old_price']:.2f} SAR")
    if product.get("discount_percent") is not None:
        lines.append(f"🏷️ خصم Amazon الحالي: {product['discount_percent']:.2f}%")
    if product.get("price_drop_change_percent") is not None and "price_drop" in alerts:
        lines.append(f"📉 الانخفاض منذ آخر رصد: {product['price_drop_change_percent']:.2f}%")
    if "back_in_stock_strong" in alerts:
        lines += ["", "⚡ كان غير متوفر وعاد الآن للتوفر بخصم قوي!"]
    lines += ["", f"🔗 {product['url']}"]
    return "\n".join(lines)



# ============================================================
# Unified Colab scanner — Cells 1–21 consolidated
# 4 PAGE WORKERS + RETRY + LAST SCAN PRICE
# + NORMAL AVAILABILITY TRACKING
# ============================================================

import os
import json
import re
import asyncio
import time
import requests
import threading

from datetime import datetime
from playwright.async_api import async_playwright


# ============================================================
# SETTINGS
# ============================================================

YALLA_MAX_PRICE = 16.0
FAST_MAX_PRICE = 5.0

STRONG_DISCOUNT = 50.0
DISCOUNT_ALERT_THRESHOLD = 25.0
PRICE_DROP_THRESHOLD = 25.0

YALLA_TRACKING_FILE = os.path.join(
    PROJECT_DIR,
    "tracking_amazonyalla.json"
)

YALLA_SCAN_URL = (
    "https://www.amazon.sa/s?"
    "i=amazonyalla"
    "&bbn=207266341031"
    "&rh=p_36%3A-1600"
    "&s=price-asc-rank"
    "&page={page}"
    "&language=en_SA"
)

PAGE_WORKERS = 4

PAGE_RETRY_COUNT = 4
PAGE_RETRY_DELAY = 1.5

WORKER_START_DELAY = 0.35


# ============================================================
# LOCKS
# ============================================================

AMAZON_SCAN_LOCK = asyncio.Lock()
TRACKING_SAVE_LOCK = asyncio.Lock()
ASIN_LOCK = asyncio.Lock()

TELEGRAM_SEND_LOCK = threading.Lock()

LAST_TELEGRAM_SEND = 0.0


# ============================================================
# TRACKING
# ============================================================

tracking = load_yalla_tracking()

if "products" not in tracking:
    tracking["products"] = {}

if "meta" not in tracking:
    tracking["meta"] = {}
tracking["meta"].setdefault("baseline_complete", False)

print("📂 Tracking loaded")
print(
    "📦 Products tracked:",
    len(tracking["products"])
)


# ============================================================
# TRACKING NORMALIZATION
# ============================================================

def normalize_tracking_record(old_data):

    if old_data is None:
        return None

    record = dict(old_data)

    record.setdefault("price", None)
    record.setdefault("old_price", None)
    record.setdefault("discount_percent", None)
    record.setdefault("multi_pack", False)
    if "last_discount_alert_percent" not in record:
        try:
            prior_discount = float(record.get("discount_percent"))
            record["last_discount_alert_percent"] = prior_discount if prior_discount >= DISCOUNT_ALERT_THRESHOLD else None
        except (TypeError, ValueError):
            record["last_discount_alert_percent"] = None

    record.setdefault("new_alert_sent", True)

    record.setdefault(
        "multi_pack_alert_sent",
        False
    )

    record.setdefault(
        "strong_deal_alert_sent",
        False
    )

    record.setdefault(
        "price_drop_alert_sent",
        False
    )

    # --------------------------------------------------------
    # Strong Discount state
    # --------------------------------------------------------

    if "last_strong_alert_discount" not in record:

        current_discount = record.get(
            "discount_percent"
        )

        if (
            record.get(
                "strong_deal_alert_sent",
                False
            )
            and current_discount is not None
        ):

            try:

                current_discount = float(
                    current_discount
                )

                if current_discount >= STRONG_DISCOUNT:

                    record[
                        "last_strong_alert_discount"
                    ] = current_discount

                else:

                    record[
                        "last_strong_alert_discount"
                    ] = None

            except Exception:

                record[
                    "last_strong_alert_discount"
                ] = None

        else:

            record[
                "last_strong_alert_discount"
            ] = None

    # --------------------------------------------------------
    # Price Drop state
    # --------------------------------------------------------

    record.setdefault(
        "last_price_drop_alert_percent",
        None
    )

    # --------------------------------------------------------
    # Availability state
    #
    # Existing products from older tracking files are treated
    # as available if they have previously been tracked.
    #
    # NORMAL scan will update this state.
    # --------------------------------------------------------

    record.setdefault(
        "availability_state",
        "available"
    )

    record.setdefault(
        "normal_seen",
        False
    )

    record.setdefault(
        "last_normal_scan_id",
        None
    )

    record.setdefault(
        "back_in_stock_alert_sent",
        False
    )

    # Require repeated absence from complete NORMAL scans before treating
    # search-result disappearance as an availability change.
    record.setdefault(
        "normal_missing_streak",
        0
    )

    record.setdefault("discount_below_streak", 0)
    record.setdefault("strong_discount_below_streak", 0)

    return record


# ============================================================
# FINAL ALERT LOGIC
# ============================================================

def get_final_yalla_alerts(
    product,
    old_data
):

    alerts = []

    current_price = product.get(
        "price"
    )

    discount = product.get(
        "discount_percent"
    )

    current_multi = is_multi_pack(
        product.get("name", "")
    )

    current_strong = (
        discount is not None
        and discount >= STRONG_DISCOUNT
    )

    # --------------------------------------------------------
    # NEW PRODUCT
    # --------------------------------------------------------

    if old_data is None:

        alerts.append("new")

        if current_multi:
            alerts.append("multi_pack")

        if discount is not None and discount >= DISCOUNT_ALERT_THRESHOLD:
            alerts.append("discount_25")
        if current_strong:
            alerts.append("strong_deal")

        return alerts

    old_data = normalize_tracking_record(
        old_data
    )

    # Retry the new-product topic if another topic succeeded first.
    if not old_data.get("new_alert_sent", True):
        alerts.append("new")

    # --------------------------------------------------------
    # IMPORTANT:
    # Returning from unavailable is NOT a new product.
    # --------------------------------------------------------

    # Search-result absence does not establish that the product is out of
    # stock. This scanner does not verify stock on the product page.
    was_unavailable = False

    # --------------------------------------------------------
    # BACK IN STOCK + STRONG DISCOUNT
    #
    # This is intentionally independent from the normal
    # last_strong_alert_discount state.
    #
    # Example:
    # 60% sent
    # -> unavailable
    # -> 60%
    # = SEND back_in_stock_strong
    # --------------------------------------------------------

    if (
        was_unavailable
        and current_price is not None
        and current_strong
    ):

        alerts.append(
            "back_in_stock_strong"
        )

    # --------------------------------------------------------
    # MULTI-PACK
    # --------------------------------------------------------

    old_multi = old_data.get(
        "multi_pack",
        False
    )

    multi_sent = old_data.get(
        "multi_pack_alert_sent",
        False
    )

    if (
        current_multi
        and not old_multi
        and not multi_sent
    ):

        alerts.append(
            "multi_pack"
        )

    # --------------------------------------------------------
    # AMAZON DISCOUNT 25%+ (independent of last-scan price movement)
    # --------------------------------------------------------

    last_discount_alert = old_data.get("last_discount_alert_percent")
    if discount is not None:
        try:
            discount_value = float(discount)
            if discount_value >= DISCOUNT_ALERT_THRESHOLD:
                old_data["discount_below_streak"] = 0
            elif discount_value < (DISCOUNT_ALERT_THRESHOLD - 2.5):
                old_data["discount_below_streak"] = int(old_data.get("discount_below_streak", 0) or 0) + 1
                if old_data["discount_below_streak"] >= 2:
                    old_data["last_discount_alert_percent"] = None
                    last_discount_alert = None
            else:
                old_data["discount_below_streak"] = 0
            if discount_value >= DISCOUNT_ALERT_THRESHOLD and (
                last_discount_alert is None
                or discount_value >= float(last_discount_alert) + 5.0
            ):
                alerts.append("discount_25")
        except (TypeError, ValueError):
            pass

    # --------------------------------------------------------
    # STRONG DISCOUNT
    #
    # Normal Strong logic:
    #
    # <50 -> 50       SEND
    # 50 -> 50        NO
    # 50 -> 55        SEND (at least 5 percentage points better)
    # 55 -> 57        NO (small display fluctuation)
    # 55 -> 60        SEND
    # 60 -> 55        NO
    # 55 -> 50        NO
    # 50 -> 49        RESET
    # 49 -> 50        SEND
    #
    # Back-in-stock is handled separately above.
    # --------------------------------------------------------

    last_strong_discount = old_data.get(
        "last_strong_alert_discount"
    )

    if last_strong_discount is not None:

        try:

            last_strong_discount = float(
                last_strong_discount
            )

        except Exception:

            last_strong_discount = None

    if discount is not None:

        try:

            discount = float(
                discount
            )

            if discount < (STRONG_DISCOUNT - 5):
                old_data["strong_discount_below_streak"] = int(old_data.get("strong_discount_below_streak", 0) or 0) + 1
                if old_data["strong_discount_below_streak"] >= 2:
                    old_data["last_strong_alert_discount"] = None

            elif discount < STRONG_DISCOUNT:
                old_data["strong_discount_below_streak"] = 0

            else:
                old_data["strong_discount_below_streak"] = 0

                # If this is a return-to-availability + strong-discount event,
                # keep it as ONE combined Telegram alert.
                if "back_in_stock_strong" not in alerts:

                    if last_strong_discount is None:

                        alerts.append(
                            "strong_deal"
                        )

                    elif discount >= last_strong_discount + 5.0:

                        alerts.append(
                            "strong_deal"
                        )

        except Exception:

            pass

    # --------------------------------------------------------
    # PRICE DROP
    #
    # Reference = LAST SCAN PRICE
    #
    # <25  -> reset
    # =25  -> SEND
    # >25  -> SEND
    # Same/lower than previous alert -> NO
    # --------------------------------------------------------

    if current_price is not None:

        try:

            current_price = float(
                current_price
            )

            previous_price = old_data.get(
                "price"
            )

            if previous_price is not None:

                previous_price = float(
                    previous_price
                )

                if previous_price > 0:

                    drop_percent = (
                        (
                            previous_price
                            - current_price
                        )
                        / previous_price
                    ) * 100

                    last_drop_alert = old_data.get(
                        "last_price_drop_alert_percent"
                    )

                    if drop_percent < PRICE_DROP_THRESHOLD:

                        old_data[
                            "last_price_drop_alert_percent"
                        ] = None

                    else:

                        if last_drop_alert is None:

                            alerts.append(
                                "price_drop"
                            )

                        else:

                            try:

                                last_drop_alert = float(
                                    last_drop_alert
                                )

                                if drop_percent >= last_drop_alert + 5.0:

                                    alerts.append(
                                        "price_drop"
                                    )

                            except Exception:

                                alerts.append(
                                    "price_drop"
                                )

        except Exception:

            pass

    return alerts


# ============================================================
# BUILD FINAL TRACKING RECORD
# ============================================================

def build_final_tracking_record(
    product,
    old_data,
    alerts,
    normal_scan_id=None
):

    current_discount = product.get(
        "discount_percent"
    )

    current_price = product.get(
        "price"
    )

    current_multi = is_multi_pack(
        product.get("name", "")
    )

    if old_data is not None:

        old_data = normalize_tracking_record(
            old_data
        )

    # --------------------------------------------------------
    # Strong state
    # --------------------------------------------------------

    old_last_strong_discount = None

    if old_data is not None:

        old_last_strong_discount = old_data.get(
            "last_strong_alert_discount"
        )

        if old_last_strong_discount is not None:

            try:

                old_last_strong_discount = float(
                    old_last_strong_discount
                )

            except Exception:

                old_last_strong_discount = None

    if current_discount is not None:

        try:

            current_discount = float(
                current_discount
            )

        except Exception:

            current_discount = None

    old_last_discount_alert = (old_data.get("last_discount_alert_percent") if old_data else None)
    if (
        current_discount is not None
        and current_discount < (DISCOUNT_ALERT_THRESHOLD - 2.5)
        and old_data is not None
        and int(old_data.get("discount_below_streak", 0) or 0) >= 2
    ):
        new_last_discount_alert = None
    elif "discount_25" in alerts:
        new_last_discount_alert = current_discount
    else:
        new_last_discount_alert = old_last_discount_alert

    if (
        current_discount is not None
        and current_discount < (STRONG_DISCOUNT - 5)
        and old_data is not None
        and int(old_data.get("strong_discount_below_streak", 0) or 0) >= 2
    ):

        new_last_strong_discount = None

    elif (
        "back_in_stock_strong" in alerts
        and current_discount is not None
    ):

        new_last_strong_discount = (
            current_discount
        )

    elif "strong_deal" in alerts:

        new_last_strong_discount = (
            current_discount
        )

    else:

        new_last_strong_discount = (
            old_last_strong_discount
        )

    # --------------------------------------------------------
    # Price Drop state
    # --------------------------------------------------------

    old_last_drop_alert = None

    if old_data is not None:

        old_last_drop_alert = old_data.get(
            "last_price_drop_alert_percent"
        )

    new_last_drop_alert = (
        old_last_drop_alert
    )

    if (
        old_data is not None
        and old_data.get("price") is not None
        and current_price is not None
    ):

        try:

            previous_price = float(
                old_data.get("price")
            )

            current_price_float = float(
                current_price
            )

            if previous_price > 0:

                drop_percent = (
                    (
                        previous_price
                        - current_price_float
                    )
                    / previous_price
                ) * 100

                if drop_percent < PRICE_DROP_THRESHOLD:

                    new_last_drop_alert = None

                elif "price_drop" in alerts:

                    new_last_drop_alert = (
                        drop_percent
                    )

        except Exception:

            pass

    # --------------------------------------------------------
    # Availability
    # --------------------------------------------------------

    old_availability = (
        old_data.get(
            "availability_state",
            "available"
        )
        if old_data
        else "available"
    )

    current_availability = (
        "available"
        if product.get(
            "available",
            True
        )
        else "unavailable"
    )

    # --------------------------------------------------------
    # Build record
    # --------------------------------------------------------

    record = {

        "asin": product["asin"],

        "name": product["name"],

        "price": product.get("price"),

        "old_price": product.get(
            "old_price"
        ),

        "discount_percent": current_discount,

        "last_discount_alert_percent": new_last_discount_alert,

        "discount_below_streak": (
            int(old_data.get("discount_below_streak", 0) or 0)
            if old_data
            else 0
        ),

        "multi_pack": current_multi,

        "new_alert_sent": (
            True
            if old_data is not None
            else ("new" in alerts)
        ),

        "multi_pack_alert_sent": (
            old_data.get(
                "multi_pack_alert_sent",
                False
            )
            if old_data
            else False
        ),

        "strong_deal_alert_sent": (
            old_data.get(
                "strong_deal_alert_sent",
                False
            )
            if old_data
            else False
        ),

        "last_strong_alert_discount": (
            new_last_strong_discount
        ),

        "strong_discount_below_streak": (
            int(old_data.get("strong_discount_below_streak", 0) or 0)
            if old_data
            else 0
        ),

        "price_drop_alert_sent": (
            old_data.get(
                "price_drop_alert_sent",
                False
            )
            if old_data
            else False
        ),

        "last_price_drop_alert_percent": (
            new_last_drop_alert
        ),

        # ----------------------------------------------------
        # Availability
        # ----------------------------------------------------

        "availability_state": (
            current_availability
        ),

        "normal_seen": (
            old_data.get(
                "normal_seen",
                False
            )
            if old_data
            else False
        ),

        "last_normal_scan_id": (
            old_data.get(
                "last_normal_scan_id"
            )
            if old_data
            else None
        ),

        "back_in_stock_alert_sent": (
            old_data.get(
                "back_in_stock_alert_sent",
                False
            )
            if old_data
            else False
        ),

        "last_seen": datetime.now().isoformat()
    }

    # --------------------------------------------------------
    # Alert flags
    # --------------------------------------------------------

    if "new" in alerts:

        record[
            "new_alert_sent"
        ] = True

    if "multi_pack" in alerts:

        record[
            "multi_pack_alert_sent"
        ] = True

    if (
        "strong_deal" in alerts
        or "back_in_stock_strong" in alerts
    ):

        record[
            "strong_deal_alert_sent"
        ] = True

    if "price_drop" in alerts:

        record[
            "price_drop_alert_sent"
        ] = True

    if "back_in_stock_strong" in alerts:

        record[
            "back_in_stock_alert_sent"
        ] = True

    # --------------------------------------------------------
    # NORMAL scan marker
    # --------------------------------------------------------

    if normal_scan_id is not None:

        record[
            "normal_seen"
        ] = True

        record[
            "last_normal_scan_id"
        ] = normal_scan_id

    return record


# ============================================================
# SAVE TRACKING IMMEDIATELY
# ============================================================

async def save_tracking_immediately():

    async with TRACKING_SAVE_LOCK:

        await asyncio.to_thread(
            save_yalla_tracking,
            tracking
        )


# ============================================================
# TELEGRAM SENDER
# ============================================================

def send_yalla_telegram_product(product, alerts):
    """Send each route and return alert types that actually reached Telegram."""
    global LAST_TELEGRAM_SEND
    routes = []
    if "new" in alerts:
        routes.append((TELEGRAM_TOPIC_NEW, ["new"]))
    if "multi_pack" in alerts:
        routes.append((TELEGRAM_TOPIC_MULTIPACK, ["multi_pack"]))
    discount_alerts = [a for a in ("discount_25", "strong_deal", "price_drop", "back_in_stock_strong") if a in alerts]
    if discount_alerts:
        routes.append((TELEGRAM_TOPIC_DISCOUNTS, discount_alerts))
    if not routes:
        print(f"❌ No Telegram topic route for alert types: {alerts}")
        return {"sent_alerts": [], "complete": False}

    sent_alerts = []
    with TELEGRAM_SEND_LOCK:
        for topic_id, topic_alerts in routes:
            caption = build_alert_message(product, topic_alerts)
            image_url = product.get("image_url")
            sent = False
            payload = {"chat_id": TELEGRAM_CHAT_ID, "message_thread_id": topic_id}
            if image_url:
                payload.update({"photo": image_url, "caption": caption})
                endpoint = "sendPhoto"
            else:
                payload.update({"text": caption})
                endpoint = "sendMessage"

            response_payload = None
            for attempt in range(2):
                wait = 2.0 - (time.time() - LAST_TELEGRAM_SEND)
                if wait > 0:
                    time.sleep(wait)
                try:
                    response = requests.post(f"{TELEGRAM_API}/{endpoint}", data=payload, timeout=60)
                    response_payload = response.json()
                except Exception as exc:
                    print(f"⚠️ Telegram topic {topic_id} request failed ({type(exc).__name__}); details hidden.")
                    response_payload = None
                    break
                if response_payload.get("ok"):
                    LAST_TELEGRAM_SEND = time.time()
                    sent = True
                    break
                if response_payload.get("error_code") == 429 and attempt == 0:
                    delay = response_payload.get("parameters", {}).get("retry_after", 10)
                    print(f"⏳ Telegram rate limit; waiting {delay}s")
                    time.sleep(delay + 1)
                    continue
                break

            if not sent and endpoint == "sendPhoto" and not (response_payload or {}).get("error_code") == 429:
                fallback = {"chat_id": TELEGRAM_CHAT_ID, "message_thread_id": topic_id, "text": caption}
                wait = 2.0 - (time.time() - LAST_TELEGRAM_SEND)
                if wait > 0:
                    time.sleep(wait)
                try:
                    response_payload = requests.post(f"{TELEGRAM_API}/sendMessage", data=fallback, timeout=60).json()
                    sent = bool(response_payload.get("ok"))
                    if sent:
                        LAST_TELEGRAM_SEND = time.time()
                except Exception as exc:
                    print(f"⚠️ Telegram text fallback failed ({type(exc).__name__}); details hidden.")

            if sent:
                sent_alerts.extend(topic_alerts)
                print(f"✅ Telegram sent to topic {topic_id}")
            else:
                print(f"❌ Telegram send failed for topic {topic_id}: {response_payload}")
    return {"sent_alerts": sent_alerts, "complete": len(sent_alerts) == len(alerts)}

# ============================================================
# PROCESS PRODUCT
# ============================================================

async def process_yalla_product_final(
    product,
    card,
    scan_max_price,
    normal_scan_id=None
):

    asin = product.get(
        "asin"
    )

    if not asin:
        return False

    price = product.get(
        "price"
    )

    if price is None:
        return False

    if price > scan_max_price:
        return False

    async with ASIN_LOCK:

        old_data = tracking[
            "products"
        ].get(
            asin
        )

        if old_data is not None:

            old_data = normalize_tracking_record(
                old_data
            )

        if not tracking["meta"].get("baseline_complete", False):

            tracking["products"][asin] = build_final_tracking_record(
                product, old_data, [], normal_scan_id
            )
            return False

        alerts = get_final_yalla_alerts(
            product,
            old_data
        )

        if old_data is not None and old_data.get("price") is not None and product.get("price") is not None:
            try:
                previous_price = float(old_data["price"])
                current_price = float(product["price"])
                if previous_price > 0 and current_price < previous_price:
                    product["price_drop_change_percent"] = (previous_price-current_price)/previous_price*100
            except (TypeError, ValueError):
                pass

        # ----------------------------------------------------
        # No alert
        # ----------------------------------------------------

        if not alerts:

            tracking[
                "products"
            ][asin] = (
                build_final_tracking_record(
                    product,
                    old_data,
                    [],
                    normal_scan_id
                )
            )

            return False

        # ----------------------------------------------------
        # Get image
        # ----------------------------------------------------

        image_url = (
            await get_yalla_product_image(
                card
            )
        )

        product["image_url"] = (
            image_url
        )

        # ----------------------------------------------------
        # Send Telegram
        # ----------------------------------------------------

        telegram_result = await asyncio.to_thread(
            send_yalla_telegram_product,
            product,
            alerts,
        )
        sent_alerts = list(dict.fromkeys(telegram_result["sent_alerts"]))
        if not sent_alerts:
            print(f"⚠️ Telegram failed for {asin}; tracking not marked as sent")
            return False

        # Store only successfully delivered routes. Failed topics remain eligible
        # for retry without duplicating a message that already reached another topic.
        record = build_final_tracking_record(product, old_data, sent_alerts, normal_scan_id)
        if "back_in_stock_strong" in alerts and "back_in_stock_strong" not in sent_alerts and old_data:
            record["availability_state"] = old_data.get("availability_state", "unavailable")
        tracking["products"][asin] = record
        await save_tracking_immediately()
        if not telegram_result["complete"]:
            print(f"⚠️ Partial Telegram delivery for {asin}; unsent alert types remain eligible for retry.")
        print(f"💾 Saved immediately: {asin}")
        return True

# ============================================================
# MARK PRODUCTS MISSING FROM NORMAL SCAN
# ============================================================

async def mark_missing_normal_products(
    current_normal_scan_id,
    current_normal_seen_asins
):

    async with ASIN_LOCK:
        # Search result pagination/ranking is not a stock-status signal.
        # Reset the seen products' miss counter, but never mark unseen items
        # unavailable; the scanner does not verify availability on product pages.
        for asin in current_normal_seen_asins:
            old_record = tracking["products"].get(asin)
            if old_record is None:
                continue
            old_record = normalize_tracking_record(old_record)
            old_record["normal_missing_streak"] = 0
            tracking["products"][asin] = old_record

    return 0


# ============================================================
# SCAN ONE YALLA PAGE
# ============================================================

async def scan_yalla_page(
    page,
    page_number,
    scan_max_price,
    normal_scan_id=None,
    current_normal_seen_asins=None
):

    url = YALLA_SCAN_URL.format(
        page=page_number
    )

    await page.goto(
        url,
        wait_until="domcontentloaded",
        timeout=60000
    )

    try:

        await page.locator(
            '[data-component-type="s-search-result"]'
        ).first.wait_for(
            timeout=15000
        )

    except Exception:

        pass

    await page.wait_for_timeout(
        250
    )

    cards = page.locator(
        '[data-component-type="s-search-result"]'
    )

    card_count = await cards.count()

    if card_count == 0:

        body_text = (await page.locator("body").inner_text()).lower()
        challenge_markers = ("captcha", "robot check", "automated access", "unusual traffic", "enter the characters")
        if any(marker in body_text for marker in challenge_markers):
            raise RuntimeError("Amazon challenge/block page detected; this page is not a valid empty result.")

        return {
            "cards": 0,
            "parsed": 0,
            "processed": 0,
            "sent": 0,
            "lowest_price": None
        }

    parsed = 0
    processed = 0
    sent = 0
    lowest_price = None

    for i in range(
        card_count
    ):

        try:

            card = cards.nth(i)

            product = (
                await get_yalla_product_data(
                    card
                )
            )

            if product is None:

                continue

            parsed += 1

            price = product.get(
                "price"
            )

            if (
                price is not None
                and (
                    lowest_price is None
                    or price < lowest_price
                )
            ):

                lowest_price = price

            # ------------------------------------------------
            # Availability is based on NORMAL scan presence.
            #
            # A parsed card with a valid ASIN counts as seen.
            # ------------------------------------------------

            if (
                normal_scan_id is not None
                and current_normal_seen_asins
                is not None
            ):

                current_normal_seen_asins.add(
                    product["asin"]
                )

                # IMPORTANT:
                # Save NORMAL presence immediately.
                # Do not depend on price processing,
                # alert generation, or Telegram.

                async with ASIN_LOCK:

                    existing = tracking["products"].get(
                        product["asin"]
                    )

                    if existing is not None:

                        existing = normalize_tracking_record(
                            existing
                        )

                        existing["normal_seen"] = True

                        existing["last_normal_scan_id"] = (
                            normal_scan_id
                        )
                        existing["normal_missing_streak"] = 0

                        tracking["products"][
                            product["asin"]
                        ] = existing

            # ------------------------------------------------
            # No price = cannot process price alerts.
            # ------------------------------------------------

            if (
                price is None
                or price > scan_max_price
            ):

                continue

            processed += 1

            did_send = (
                await process_yalla_product_final(
                    product,
                    card,
                    scan_max_price,
                    normal_scan_id
                )
            )

            if did_send:

                sent += 1

        except Exception as e:

            print(
                f"⚠️ Product error "
                f"Page {page_number} "
                f"Item {i + 1}: {e}"
            )

            error_text = str(e).lower()
            if any(
                marker in error_text
                for marker in (
                    "connection closed while reading from the driver",
                    "pipe closed by peer",
                    "target page, context or browser has been closed",
                )
            ):
                # Do not count this as an ordinary bad product card. The
                # browser driver is gone, so retry the whole page after the
                # batch-level recovery recreates Chromium.
                raise

    return {
        "cards": card_count,
        "parsed": parsed,
        "processed": processed,
        "sent": sent,
        "lowest_price": lowest_price
    }


# ============================================================
# PAGE RETRY
# ============================================================

async def scan_page_with_retry(
    page_number,
    scan_max_price,
    worker_id,
    normal_scan_id=None,
    current_normal_seen_asins=None
):

    last_error = None

    for attempt in range(
        1,
        PAGE_RETRY_COUNT + 1
    ):

        page = None

        try:

            if attempt == 1:

                await asyncio.sleep(
                    (
                        worker_id - 1
                    )
                    * WORKER_START_DELAY
                )

            else:

                await asyncio.sleep(
                    PAGE_RETRY_DELAY
                )

            page = (
                await yalla_context.new_page()
            )

            result = (
                await scan_yalla_page(
                    page,
                    page_number,
                    scan_max_price,
                    normal_scan_id,
                    current_normal_seen_asins
                )
            )

            return {
                "ok": True,
                "page": page_number,
                "worker": worker_id,
                **result
            }

        except Exception as e:

            last_error = e

            error_text = str(e)

            print(
                f"⚠️ Page {page_number} "
                f"attempt "
                f"{attempt}/"
                f"{PAGE_RETRY_COUNT} "
                f"failed:"
            )

            print(
                error_text
            )

            print(
                f"🔄 Retrying Page "
                f"{page_number}..."
            )

        finally:

            if page:

                try:

                    await page.close()

                except Exception:

                    pass

    print()

    print(
        f"❌ Page {page_number} "
        f"FAILED AFTER "
        f"{PAGE_RETRY_COUNT} ATTEMPTS"
    )

    return {
        "ok": False,
        "page": page_number,
        "worker": worker_id,
        "cards": 0,
        "parsed": 0,
        "processed": 0,
        "sent": 0,
        "lowest_price": None,
        "error": str(last_error)
    }


# ============================================================
# WORKER
# ============================================================

async def scan_page_worker(
    page_number,
    scan_max_price,
    worker_id,
    normal_scan_id=None,
    current_normal_seen_asins=None
):

    result = await scan_page_with_retry(
        page_number,
        scan_max_price,
        worker_id,
        normal_scan_id,
        current_normal_seen_asins
    )

    if result.get("ok", False):
        print_page_result(result)

    return result


def print_page_result(result):
    print(
        f"📄 Page {result['page']}: "
        f"{result['cards']} cards | "
        f"{result['parsed']} parsed | "
        f"{result['processed']} processed | "
        f"📨 {result['sent']} sent | "
        f"💾 {len(tracking['products'])} tracked",
        flush=True,
    )


# ============================================================
# BROWSER
# ============================================================

async def start_yalla_browser():

    global yalla_pw
    global yalla_browser
    global yalla_context

    print()
    print(
        "🌐 Starting Amazon browser..."
    )

    print(
        "🇬🇧 FORCE ENGLISH MODE"
    )

    print(
        "🧹 Saved Amazon session"
    )

    yalla_pw = (
        await async_playwright().start()
    )

    yalla_browser = (
        await yalla_pw.chromium.launch(
            headless=True
        )
    )

    yalla_context = (
        await yalla_browser.new_context(
            storage_state=SESSION_FILE,
            locale="en-SA",
            extra_http_headers={
                "Accept-Language":
                    "en-SA,en-US;q=0.9,en;q=0.8"
            }
        )
    )

    try:

        await yalla_context.add_cookies(
            [
                {
                    "name": "lc-acbsa",
                    "value": "en_SA",
                    "domain": ".amazon.sa",
                    "path": "/"
                },
                {
                    "name": "i18n-prefs",
                    "value": "SAR",
                    "domain": ".amazon.sa",
                    "path": "/"
                }
            ]
        )

    except Exception as e:

        print(
            "⚠️ Language cookie warning:",
            e
        )

    page = (
        await yalla_context.new_page()
    )

    await page.goto(
        "https://www.amazon.sa/?language=en_SA",
        wait_until="domcontentloaded",
        timeout=60000
    )

    await page.wait_for_timeout(
        1500
    )

    html_lang = await page.locator(
        "html"
    ).get_attribute(
        "lang"
    )

    print(
        "🌐 HTML lang:",
        html_lang
    )

    try:

        delivery = await page.locator(
            "#glow-ingress-line2"
        ).inner_text(
            timeout=5000
        )

        print(
            "📍 Delivery:",
            delivery
        )

    except Exception:

        print(
            "📍 Delivery: not detected"
        )

    await page.close()

    print(
        "✅ Amazon saved session ready"
    )


async def close_yalla_browser():

    global yalla_pw
    global yalla_browser
    global yalla_context

    try:

        if yalla_context:

            await yalla_context.close()

    except Exception:

        pass

    try:

        if yalla_browser:

            await yalla_browser.close()

    except Exception:

        pass

    try:

        if yalla_pw:

            await yalla_pw.stop()

    except Exception:

        pass

    yalla_pw = None
    yalla_browser = None
    yalla_context = None


async def ensure_yalla_browser():

    global yalla_browser

    if (
        yalla_browser is None
        or not yalla_browser.is_connected()
    ):

        print(
            "🔄 Browser disconnected "
            "— restarting..."
        )

        await close_yalla_browser()

        await start_yalla_browser()


# ============================================================
# RUN ONE SCAN
# ============================================================

async def run_yalla_scan(
    scan_name,
    scan_max_price
):

    async with AMAZON_SCAN_LOCK:

        await ensure_yalla_browser()

        start_time = time.time()

        next_page = 1

        total_cards = 0
        total_parsed = 0
        total_processed = 0
        total_sent = 0

        empty_pages = 0
        pages_completed = 0
        first_page_had_cards = False
        failed_batch_streak = 0

        # ----------------------------------------------------
        # Availability tracking is ONLY performed for NORMAL.
        # ----------------------------------------------------

        is_normal_scan = (
            scan_max_price
            == YALLA_MAX_PRICE
        )

        normal_scan_id = None

        current_normal_seen_asins = set()

        if is_normal_scan:

            normal_scan_id = (
                datetime.now().isoformat()
            )

            print()
            print(
                "📍 NORMAL availability scan:"
            )

            print(
                f"🆔 Scan ID: "
                f"{normal_scan_id}"
            )

        print()
        print("=" * 70)

        print(
            f"🚀 {scan_name} YALLA SCAN"
        )

        print(
            f"💰 Max price: "
            f"{scan_max_price:.2f} SAR"
        )

        print(
            f"🧵 Workers: "
            f"{PAGE_WORKERS} pages at a time"
        )

        print(
            f"🔁 Page retry: "
            f"{PAGE_RETRY_COUNT} attempts"
        )

        print("=" * 70)

        while True:

            page_numbers = list(
                range(
                    next_page,
                    next_page + PAGE_WORKERS
                )
            )

            results = await asyncio.gather(
                *[
                    scan_page_worker(
                        page_number,
                        scan_max_price,
                        worker_id,
                        normal_scan_id,
                        current_normal_seen_asins
                    )
                    for worker_id, page_number
                    in enumerate(
                        page_numbers,
                        start=1
                    )
                ],
                return_exceptions=True
            )

            failed_pages = []

            for result in results:

                if isinstance(
                    result,
                    Exception
                ):

                    print()
                    print(
                        "⚠️ Worker crashed:"
                    )

                    print(result)

                    continue

                if not result.get(
                    "ok",
                    False
                ):

                    failed_pages.append(
                        result["page"]
                    )

            # ------------------------------------------------
            # Retry failed pages.
            # ------------------------------------------------

            if failed_pages:

                # A dead Playwright driver makes every page retry fail on the
                # same stale context. Recreate Chromium once before retrying
                # those pages; run_worker will also restart the scan cycle if
                # the batch still cannot recover.
                fatal_browser_errors = (
                    "Connection closed while reading from the driver",
                    "pipe closed by peer",
                    "Target page, context or browser has been closed",
                )
                failed_messages = [
                    str(result.get("error", ""))
                    for result in results
                    if isinstance(result, dict)
                    and not result.get("ok", False)
                ] + [
                    str(result)
                    for result in results
                    if isinstance(result, Exception)
                ]
                if any(
                    marker.lower() in message.lower()
                    for message in failed_messages
                    for marker in fatal_browser_errors
                ):
                    print("♻️ Playwright driver disconnected; restarting Chromium before retrying the failed pages.")
                    await close_yalla_browser()
                    await start_yalla_browser()

                print()
                print(
                    "🔁 Retrying failed pages "
                    "before continuing:"
                )

                print(
                    failed_pages
                )

                for page_number in failed_pages:

                    retry_result = (
                        await scan_page_with_retry(
                            page_number,
                            scan_max_price,
                            1,
                            normal_scan_id,
                            current_normal_seen_asins
                        )
                    )

                    if retry_result.get(
                        "ok",
                        False
                    ):

                        print_page_result(retry_result)

                        for index, original in enumerate(
                            results
                        ):

                            if (
                                isinstance(
                                    original,
                                    dict
                                )
                                and original.get(
                                    "page"
                                )
                                == page_number
                            ):

                                results[index] = (
                                    retry_result
                                )

                                break

                            elif isinstance(
                                original,
                                Exception
                            ):

                                results[index] = (
                                    retry_result
                                )

                                break

                    else:

                        print()
                        print(
                            f"🛑 Page {page_number} "
                            f"could not be recovered."
                        )

                        print(
                            "⚠️ NORMAL availability "
                            "comparison will NOT "
                            "run for this incomplete "
                            "scan."
                        )

                        print(
                            "🔄 Retrying this batch again..."
                        )

                        await asyncio.sleep(
                            3
                        )

                        continue

            # ------------------------------------------------
            # Process batch
            # ------------------------------------------------

            batch_had_cards = False
            batch_lowest_price = None
            batch_failed = False

            for result in results:

                if isinstance(
                    result,
                    Exception
                ):

                    batch_failed = True

                    print(
                        "⚠️ Unresolved worker error:"
                    )

                    print(result)

                    continue

                if not result.get(
                    "ok",
                    False
                ):

                    batch_failed = True

                    print(
                        f"⚠️ Page "
                        f"{result.get('page')} "
                        f"still failed."
                    )

                    continue

                page_number = result[
                    "page"
                ]

                cards = result[
                    "cards"
                ]

                if page_number == 1 and cards > 0:
                    first_page_had_cards = True

                parsed = result[
                    "parsed"
                ]

                processed = result[
                    "processed"
                ]

                sent = result[
                    "sent"
                ]

                lowest_price = result[
                    "lowest_price"
                ]

                total_cards += cards
                total_parsed += parsed
                total_processed += processed
                total_sent += sent

                pages_completed += 1

                if cards > 0:

                    batch_had_cards = True

                if (
                    lowest_price is not None
                    and (
                        batch_lowest_price is None
                        or lowest_price
                        < batch_lowest_price
                    )
                ):

                    batch_lowest_price = (
                        lowest_price
                    )

            if batch_failed:

                print()
                print(
                    "🛑 Batch had unresolved "
                    "page failure."
                )

                failed_batch_streak += 1
                if failed_batch_streak >= 3:
                    raise RuntimeError("Three consecutive page batches failed after retries; scan aborted safely.")
                print("🔄 Retrying same batch...")
                await asyncio.sleep(3)
                continue

            failed_batch_streak = 0

            if batch_had_cards:

                empty_pages = 0

            else:

                empty_pages += PAGE_WORKERS

            if (
                scan_max_price
                <= FAST_MAX_PRICE
                and batch_lowest_price is not None
                and batch_lowest_price
                > scan_max_price
            ):

                print()
                print(
                    f"🛑 Fast smart stop — "
                    f"lowest price "
                    f"{batch_lowest_price:.2f} "
                    f"> "
                    f"{scan_max_price:.2f}"
                )

                break

            if empty_pages >= 4:

                print()
                print(
                    "🛑 Stopping after "
                    "4 consecutive empty pages"
                )

                break

            next_page += PAGE_WORKERS

            await asyncio.sleep(
                0.2
            )

        # ----------------------------------------------------
        # IMPORTANT:
        # Only after the COMPLETE NORMAL scan succeeds do we
        # compare previous NORMAL products with current results.
        # ----------------------------------------------------

        if is_normal_scan and first_page_had_cards and total_cards > 0 and len(current_normal_seen_asins) > 0:

            print()
            print(
                "🔎 Comparing NORMAL scan "
                "with previous scan..."
            )

            missing_count = (
                await mark_missing_normal_products(
                    normal_scan_id,
                    current_normal_seen_asins
                )
            )

            print(
                f"📍 NORMAL products currently seen: "
                f"{len(current_normal_seen_asins)}"
            )

            print(
                f"📤 Products newly marked unavailable: "
                f"{missing_count}"
            )

        elif is_normal_scan:

            print()
            print("⚠️ NORMAL scan was not validated (no parsed ASINs on page 1); availability comparison skipped.")

        # A baseline is complete only after a valid full NORMAL scan.
        if is_normal_scan and first_page_had_cards and total_cards > 0 and len(current_normal_seen_asins) > 0 and not tracking["meta"].get("baseline_complete", False):
            tracking["meta"]["baseline_complete"] = True
            tracking["meta"]["baseline_completed_at"] = datetime.now().isoformat()
            print("✅ Initial baseline completed; alerting is enabled from now on.")

        # ----------------------------------------------------
        # Final save
        # ----------------------------------------------------

        tracking["meta"]["last_scan"] = datetime.now().isoformat()
        await save_tracking_immediately()

        elapsed = (
            time.time()
            - start_time
        )

        print()
        print("=" * 70)

        print(
            f"✅ {scan_name} SCAN FINISHED"
        )

        print(
            f"📄 Pages scanned: "
            f"{pages_completed}"
        )

        print(
            f"🧺 Cards: "
            f"{total_cards}"
        )

        print(
            f"🔎 Parsed: "
            f"{total_parsed}"
        )

        print(
            f"⚙️ Processed: "
            f"{total_processed}"
        )

        print(
            f"📨 Telegram sent: "
            f"{total_sent}"
        )

        print(
            f"💾 Total tracked: "
            f"{len(tracking['products'])}"
        )

        print(
            f"⏱️ Time: "
            f"{elapsed / 60:.1f} min"
        )

        print("=" * 70)

        return {
            "pages": pages_completed,
            "cards": total_cards,
            "parsed": total_parsed,
            "processed": total_processed,
            "sent": total_sent,
            "tracked": len(
                tracking["products"]
            ),
            "elapsed": elapsed
        }


# ============================================================


# The worker exits before GitHub's six-hour hard limit. A scheduled workflow
# starts a new worker automatically; every completed FAST+NORMAL cycle waits 5m.
WORKER_WINDOW_SECONDS = int(os.environ.get("WORKER_WINDOW_MINUTES", "345")) * 60
CYCLE_DELAY_SECONDS = 5 * 60
SCAN_TIMEOUT_SECONDS = int(os.environ.get("SCAN_TIMEOUT_MINUTES", "35")) * 60

async def run_scan_bounded(label, price_limit):
    try:
        return await asyncio.wait_for(run_yalla_scan(label, price_limit), timeout=SCAN_TIMEOUT_SECONDS)
    except asyncio.TimeoutError as exc:
        await save_tracking_immediately()
        raise TimeoutError(f"{label} exceeded its {SCAN_TIMEOUT_SECONDS // 60}-minute safety limit.") from exc

async def run_worker():
    deadline = time.monotonic() + WORKER_WINDOW_SECONDS
    await close_yalla_browser()
    await start_yalla_browser()
    print("✅ Scanner worker started; Amazon session and tracking are read from Google Drive.")
    cycle = 0
    try:
        while time.monotonic() + (2 * SCAN_TIMEOUT_SECONDS) + CYCLE_DELAY_SECONDS < deadline:
            cycle += 1
            print(f"\n🔁 Cycle {cycle}: FAST, then NORMAL")
            try:
                await run_scan_bounded("⚡ FAST", FAST_MAX_PRICE)
                await run_scan_bounded("🔥 NORMAL", YALLA_MAX_PRICE)
            except Exception as exc:
                print(f"⚠️ Cycle {cycle} stopped: {type(exc).__name__}: {exc}")
                await save_tracking_immediately()
                await close_yalla_browser()
                if time.monotonic() + CYCLE_DELAY_SECONDS >= deadline:
                    break
                await start_yalla_browser()
            if time.monotonic() + CYCLE_DELAY_SECONDS >= deadline:
                break
            print("⏳ Waiting 5 minutes after the completed cycle.")
            await asyncio.sleep(CYCLE_DELAY_SECONDS)
    finally:
        await save_tracking_immediately()
        await close_yalla_browser()
    print("✅ Worker finished; the process supervisor can restart the scanner.")

if __name__ == "__main__":
    asyncio.run(run_worker())
