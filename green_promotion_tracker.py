"""Green Amazon promotion page tracker.

The tracker intentionally uses dedicated GREEN_DEALS_* environment variables and
an independent Telegram bot token. It stores observed items in PostgreSQL.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from html import escape
from io import BytesIO
from threading import Event, Lock, Thread, local
from zoneinfo import ZoneInfo
from datetime import datetime, timezone
from decimal import Decimal
from urllib.parse import urlparse

import psycopg
import requests
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright
from psycopg.types.json import Jsonb

from green_product_offers import (
    BANK_TERMS, ProductPageUnavailable, amazon_product_url, canonical_product_url,
    duplicates_green_spend, format_offer_alert, green_offer_key, has_notifiable_combination, is_quiet_offer,
    new_notifiable_offers, read_detail_offers, without_green_duplicate,
)

TOKEN = os.getenv("GREEN_DEALS_TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("GREEN_DEALS_TELEGRAM_CHAT_ID", "")
THREAD_ID = int(os.getenv("GREEN_DEALS_TELEGRAM_MESSAGE_THREAD_ID", "0"))
DB_URL = os.getenv("GREEN_DEALS_DATABASE_URL", "")
API = f"https://api.telegram.org/bot{TOKEN}"
WAIT_SECONDS = int(os.getenv("GREEN_DEALS_SCAN_INTERVAL_SECONDS", "300"))
AMAZON_PAGE_GAP = 2
AMAZON_REJECTION_GAP = 8
DETAIL_WORKERS = 2
HTTP = requests.Session()
AMAZON_REQUEST_LOCK = Lock()
NEXT_AMAZON_REQUEST = 0.0
RETURN_ALERT = "↩️ عاد المنتج للظهور"
TELEGRAM = local()


class DetailRound:
    def __init__(self):
        self.ready = Event()
        self.listings_done = Event()
        self.started_at = None
        self.detail_at = None
        self.paused = Event()
        self.outcome_lock = Lock()
        self.rejections = 0

    def start(self, round_at, detail_at, listings_done=False):
        with self.outcome_lock:
            self.started_at = round_at
            self.detail_at = detail_at
            self.rejections = 0
            self.paused.clear()
        if listings_done:
            self.listings_done.set()
        else:
            self.listings_done.clear()
        self.ready.set()

    def note_outcome(self, listings_round, rejected):
        with self.outcome_lock:
            if listings_round != self.started_at or self.paused.is_set():
                return
            self.rejections = self.rejections + 1 if rejected else 0
            if self.rejections >= 3:
                self.paused.set()
                print("Amazon returned 3 consecutive rejection pages. Product details paused; "
                      "remaining products and retry counts are kept.")


def wait_for_amazon_slot():
    global NEXT_AMAZON_REQUEST
    while True:
        with AMAZON_REQUEST_LOCK:
            now = time.monotonic()
            delay = NEXT_AMAZON_REQUEST - now
            if delay <= 0:
                NEXT_AMAZON_REQUEST = now + AMAZON_PAGE_GAP
                return
        time.sleep(delay)


def defer_amazon_requests():
    global NEXT_AMAZON_REQUEST
    with AMAZON_REQUEST_LOCK:
        NEXT_AMAZON_REQUEST = max(NEXT_AMAZON_REQUEST, time.monotonic() + AMAZON_REJECTION_GAP)


def db():
    return psycopg.connect(DB_URL, connect_timeout=15)


def init_db():
    with db() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS green_deal_pages (
          id bigserial PRIMARY KEY, url text UNIQUE NOT NULL,
          active boolean NOT NULL DEFAULT true, title text NOT NULL DEFAULT '', expires_on date,
          added_at timestamptz NOT NULL DEFAULT now(),
          last_checked_at timestamptz, last_error text, scan_started_at timestamptz
        );
        CREATE TABLE IF NOT EXISTS green_deal_scan_cycle (
          singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
          round_at timestamptz NOT NULL, listings_done boolean NOT NULL DEFAULT false,
          rest_until timestamptz
        );
        CREATE TABLE IF NOT EXISTS green_deal_product_blocks (asin text PRIMARY KEY, blocked_price numeric NOT NULL, resume_price numeric NOT NULL, created_at timestamptz NOT NULL DEFAULT now());
        CREATE TABLE IF NOT EXISTS green_deal_product_preferences (asin text PRIMARY KEY, favorite boolean NOT NULL DEFAULT false, following boolean NOT NULL DEFAULT false, muted boolean NOT NULL DEFAULT false, updated_at timestamptz NOT NULL DEFAULT now());
        CREATE TABLE IF NOT EXISTS green_deal_products (
          page_id bigint NOT NULL REFERENCES green_deal_pages(id) ON DELETE CASCADE,
          asin text NOT NULL, title text NOT NULL DEFAULT '', price numeric,
          product_url text NOT NULL, present boolean NOT NULL DEFAULT true,
          pending_alert text, pending_url text, pending_image_url text,
          last_seen_at timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY(page_id, asin)
        );
        """)
        conn.execute("ALTER TABLE green_deal_pages ADD COLUMN IF NOT EXISTS scan_started_at timestamptz")
        conn.execute("ALTER TABLE green_deal_pages ADD COLUMN IF NOT EXISTS title text NOT NULL DEFAULT ''")
        conn.execute("ALTER TABLE green_deal_product_preferences ADD COLUMN IF NOT EXISTS following boolean NOT NULL DEFAULT false")
        conn.execute("ALTER TABLE green_deal_products ADD COLUMN IF NOT EXISTS pending_alert text")
        conn.execute("ALTER TABLE green_deal_products ADD COLUMN IF NOT EXISTS pending_url text")
        conn.execute("ALTER TABLE green_deal_products ADD COLUMN IF NOT EXISTS pending_image_url text")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS green_deal_combination_alerts (
                asin text PRIMARY KEY,
                page_id bigint NOT NULL,
                pending_revision bigint NOT NULL,
                sent_at timestamptz
            )
        """)
        conn.execute("ALTER TABLE green_deal_scan_cycle ADD COLUMN IF NOT EXISTS detail_cycle_at timestamptz")
        conn.execute("ALTER TABLE green_deal_pages ADD COLUMN IF NOT EXISTS offer_terms text NOT NULL DEFAULT ''")
        conn.execute("""
            ALTER TABLE green_deal_pages
                ADD COLUMN IF NOT EXISTS scan_failures integer NOT NULL DEFAULT 0,
                ADD COLUMN IF NOT EXISTS scan_retry_after timestamptz
        """)
        conn.execute("""
            ALTER TABLE green_deal_products
                ADD COLUMN IF NOT EXISTS image_url text NOT NULL DEFAULT '',
                ADD COLUMN IF NOT EXISTS detail_offers jsonb,
                ADD COLUMN IF NOT EXISTS detail_due_at timestamptz NOT NULL DEFAULT now(),
                ADD COLUMN IF NOT EXISTS detail_lease_until timestamptz,
                ADD COLUMN IF NOT EXISTS pending_revision bigint NOT NULL DEFAULT 0,
                ADD COLUMN IF NOT EXISTS pending_new_offers jsonb,
                ADD COLUMN IF NOT EXISTS detail_green_terms text,
                ADD COLUMN IF NOT EXISTS pending_price numeric,
                ADD COLUMN IF NOT EXISTS detail_failure_count integer NOT NULL DEFAULT 0,
                ADD COLUMN IF NOT EXISTS detail_round_at timestamptz,
                ADD COLUMN IF NOT EXISTS detail_attempt_round timestamptz,
                ADD COLUMN IF NOT EXISTS detail_attempts integer NOT NULL DEFAULT 0
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS green_deal_detail_due_idx
            ON green_deal_products (detail_due_at, page_id, asin) WHERE present
        """)


def tg(method, payload):
    polling = method == "getUpdates"
    session = getattr(TELEGRAM, "session", HTTP)
    response = session.post(f"{API}/{method}", json=payload, timeout=(5, 35 if polling else 10))
    response.raise_for_status()
    data = response.json()
    if not data.get("ok"):
        raise RuntimeError(data.get("description", "Telegram API error"))
    return data["result"]



def answer_callback(payload):
    try:
        tg("answerCallbackQuery", payload)
    except requests.HTTPError as exc:
        response = exc.response
        if response is None or response.status_code != 400:
            raise
        description = response.json().get("description", "").lower()
        if "query is too old" not in description and "query id is invalid" not in description:
            raise
        print("Telegram callback expired before it could be acknowledged.")


def send(text, markup=None, *, parse_mode=None, disable_preview=False):
    payload = {"chat_id": CHAT_ID, "text": text, "disable_web_page_preview": disable_preview}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if THREAD_ID:
        payload["message_thread_id"] = THREAD_ID
    if markup:
        payload["reply_markup"] = markup
    tg("sendMessage", payload)



def product_keyboard(asin, price, following, muted, blocked):
    suffix = f":{price:.2f}" if price is not None else ""
    follow_action = "unfollow" if following else "follow"
    mute_action = "unmute" if muted else "mute"
    follow_label = "إيقاف متابعة المنتج" if following else "👀 متابعة المنتج"
    mute_label = "🔔 استئناف التنبيه" if muted else "🔕 إيقاف التنبيه"
    main_row = [menu_button(follow_label, f"deal:{follow_action}:{asin}{suffix}")]
    if blocked:
        main_row.append(menu_button("✅ فك الحجب", f"deal:unblock:{asin}{suffix}"))
    elif price is not None:
        main_row.append(menu_button("🚫 حجب مؤقت", f"deal:block:{asin}{suffix}"))
    return {"inline_keyboard": [
        main_row,
        [menu_button(mute_label, f"deal:{mute_action}:{asin}{suffix}")],
    ]}


def get_product_controls(conn, asin):
    row = conn.execute("""
        SELECT COALESCE(pref.following,false), COALESCE(pref.muted,false),
               EXISTS(SELECT 1 FROM green_deal_product_blocks WHERE asin=product.asin)
        FROM (VALUES (%s::text)) AS product(asin)
        LEFT JOIN green_deal_product_preferences AS pref ON pref.asin=product.asin
    """, (asin,)).fetchone()
    return tuple(row)


def send_product_alert(caption, product_url, image_url, asin, price):
    with db() as conn:
        following, muted, blocked = get_product_controls(conn, asin)
    if muted or blocked or (caption.startswith(RETURN_ALERT) and not following):
        return False
    markup = product_keyboard(asin, price, following, muted, blocked)
    link = f'<a href="{escape(product_url, quote=True)}">رابط المنتج</a>'
    message_text = f"{escape(caption.rstrip())}\n\n{link}"

    # Keep long offer lists and their calculation together in a single text message.
    if image_url and len(caption.encode("utf-16-le")) // 2 <= 1000:
        session = getattr(TELEGRAM, "session", HTTP)
        try:
            host = (urlparse(image_url).hostname or "").lower()
            allowed_image_host = any(
                host.endswith(domain)
                for domain in ("media-amazon.com", "images-amazon.com", "ssl-images-amazon.com")
            )
            if not allowed_image_host:
                raise RuntimeError("Product image host is not an Amazon image domain.")

            image_response = session.get(image_url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
            image_response.raise_for_status()
            image_bytes = image_response.content
            if not image_bytes or len(image_bytes) > 8 * 1024 * 1024:
                raise RuntimeError("Product image is empty or exceeds 8 MiB.")

            data = {
                "chat_id": CHAT_ID,
                "caption": message_text,
                "parse_mode": "HTML",
                "reply_markup": json.dumps(markup, ensure_ascii=False),
            }
            if THREAD_ID:
                data["message_thread_id"] = str(THREAD_ID)
            response = session.post(
                f"{API}/sendPhoto",
                data=data,
                files={"photo": ("product.jpg", BytesIO(image_bytes), "image/jpeg")},
                timeout=45,
            )
            result = response.json()
            if not response.ok or not result.get("ok"):
                raise RuntimeError(result.get("description", "Telegram sendPhoto failed"))
            return True
        except Exception as exc:
            print(f"Product photo upload failed: {type(exc).__name__}")
            raise RuntimeError("تعذر رفع صورة المنتج إلى تيليجرام") from exc

    send(message_text, markup, parse_mode="HTML", disable_preview=True)
    return True


def valid_url(value):
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and (host == "amazon.sa" or host.endswith(".amazon.sa")) and "/promotion/psp/" in parsed.path


def add_page(value):
    parts = [part.strip() for part in value.split("|", 1)]
    url = parts[0]
    title = parts[1] if len(parts) > 1 else ""
    if not valid_url(url):
        send("أرسل رابط صفحة عرض صالحة من Amazon.sa.")
        return
    with db() as conn:
        conn.execute(
            "INSERT INTO green_deal_pages(url,title) VALUES (%s,%s) "
            "ON CONFLICT(url) DO UPDATE SET active=true,scan_failures=0,scan_retry_after=NULL,"
            "title=CASE WHEN excluded.title <> '' THEN excluded.title ELSE green_deal_pages.title END",
            (url, title),
        )
    send(f"تمت إضافة صفحة العرض للمتابعة ✅\n{title or 'سيُقرأ اسم العرض في أول فحص'}\n{url}")


MENU_SIZE = 5
PRODUCT_MENUS = {
    "following": ("👀 المنتجات المتابَعة", "إيقاف متابعة المنتج", "following"),
    "blocked": ("🚫 المنتجات المحجوبة", "فك الحجب", None),
    "muted": ("🔕 التنبيهات الموقوفة", "استئناف التنبيه", "muted"),
}


def menu_button(text, data):
    return {"text": text, "callback_data": data}


def menu_message(lines, buttons, message=None):
    text = "\n\n".join(lines)
    markup = {"inline_keyboard": buttons}
    if message is None:
        send(text, markup, parse_mode="HTML", disable_preview=True)
        return

    try:
        tg("editMessageText", {
            "chat_id": CHAT_ID,
            "message_id": message["message_id"],
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "reply_markup": markup,
        })
    except requests.HTTPError as exc:
        response = exc.response
        if response is None or response.status_code != 400:
            raise
        if "message is not modified" not in response.json().get("description", ""):
            raise


def menu_navigation(view, page, has_next):
    arrows = []
    if page:
        arrows.append(menu_button("⬅️ السابق", f"menu:{view}:{page - 1}"))
    if has_next:
        arrows.append(menu_button("التالي ➡️", f"menu:{view}:{page + 1}"))
    back = [menu_button("🏠 القائمة الرئيسية", "menu:help:0")]
    return [arrows, back] if arrows else [back]


def compact_text(value, limit):
    value = " ".join((value or "").split())
    return value[:limit - 1] + "…" if len(value) > limit else value


def show_help(message=None):
    menu_message([
        "<b>👀 مراقبة صفحات العروض</b>",
        "<b>إدارة الصفحات</b>\n"
        "/add رابط | اسم العرض — إضافة صفحة\n"
        "/pages — الصفحات المتابعة\n"
        "/status — حالة الفحص",
        "<b>إدارة المنتجات</b>\n"
        "/following — المنتجات المتابَعة\n"
        "/blocked — المنتجات المحجوبة\n"
        "/muted — التنبيهات الموقوفة",
    ], [
        [menu_button("📋 الصفحات", "menu:pages:0"), menu_button("🔎 حالة الفحص", "menu:status:0")],
        [menu_button("👀 المنتجات المتابَعة", "menu:following:0"), menu_button("🚫 المحجوبات", "menu:blocked:0")],
        [menu_button("🔕 التنبيهات الموقوفة", "menu:muted:0")],
    ], message)


def show_product_menu(view, page=0, message=None):
    heading, action, preference = PRODUCT_MENUS[view]
    if preference is None:
        saved = "SELECT asin, resume_price FROM green_deal_product_blocks"
    else:
        saved = f"SELECT asin, NULL AS resume_price FROM green_deal_product_preferences WHERE {preference}"
    query = f"""
        SELECT saved.asin, product.title, product.product_url, saved.resume_price
        FROM ({saved}) AS saved
        LEFT JOIN LATERAL (
            SELECT title, product_url FROM green_deal_products
            WHERE asin=saved.asin ORDER BY last_seen_at DESC, page_id DESC LIMIT 1
        ) AS product ON true
        ORDER BY saved.asin LIMIT %s OFFSET %s
    """
    with db() as conn:
        rows = conn.execute(query, (MENU_SIZE + 1, page * MENU_SIZE)).fetchall()
        if not rows and page:
            page = 0
            rows = conn.execute(query, (MENU_SIZE + 1, 0)).fetchall()

    lines = [f"<b>{heading}</b>"]
    buttons = []
    if not rows:
        lines.append("القائمة فارغة.")
    for number, (asin, title, url, resume_price) in enumerate(rows[:MENU_SIZE], start=page * MENU_SIZE + 1):
        label = escape(compact_text(title, 140) or asin)
        link = escape(url or f"https://www.amazon.sa/dp/{asin}", quote=True)
        item = f'<b>{number}.</b> <a href="{link}">{label}</a>'
        if resume_price is not None:
            item += f"\nيفك الحجب تلقائيًا عند {resume_price:.2f} ريال أو أقل."
        lines.append(item)
        buttons.append([menu_button(f"{number}. {action}", f"manage:{view}:{page}:{asin}")])
    menu_message(lines, buttons + menu_navigation(view, page, len(rows) > MENU_SIZE), message)


def show_pages(view="pages", page=0, message=None):
    query = """
        SELECT p.id, p.url, p.active, p.title, p.expires_on, p.last_checked_at,
               p.last_error, p.scan_started_at, products.present, products.total, p.scan_retry_after
        FROM green_deal_pages AS p
        LEFT JOIN LATERAL (
            SELECT count(*) FILTER (WHERE present) AS present, count(*) AS total
            FROM green_deal_products WHERE page_id=p.id
        ) AS products ON true
        ORDER BY p.id LIMIT %s OFFSET %s
    """
    with db() as conn:
        rows = conn.execute(query, (MENU_SIZE + 1, page * MENU_SIZE)).fetchall()
        if not rows and page:
            page = 0
            rows = conn.execute(query, (MENU_SIZE + 1, 0)).fetchall()

    status_view = view == "status"
    lines = ["<b>🔎 حالة الفحص</b>" if status_view else "<b>📋 الصفحات المتابعة</b>"]
    buttons = []
    if not rows:
        lines.append("ما فيه صفحات عروض مضافة حاليًا.\nأضف صفحة بأمر /add.")
    else:
        lines.append("الأوقات بتوقيت الرياض." if status_view else "اضغط رقم الصفحة لإدارتها.")
    for number, row in enumerate(rows[:MENU_SIZE], start=page * MENU_SIZE + 1):
        page_id, url, active, title, expiry, checked, error, started, present, total, retry_at = row
        label = escape(compact_text(title, 120) or "بانتظار اسم العرض")
        state = "⏸ متوقفة" if not active else "🔄 يجري الفحص" if started else "🟢 مفعّلة"
        if active and not started and retry_at and retry_at > datetime.now(timezone.utc):
            state = "⏳ ينتظر إعادة المحاولة"
        item = f'<b>{number}. {label}</b>\n{state}'
        if status_view:
            last = checked.astimezone(ZoneInfo("Asia/Riyadh")).strftime("%d-%m-%Y %I:%M %p") if checked else "لم تُفحص بعد"
            item += f"\nآخر فحص: {last}\nالمنتجات الظاهرة: {present}/{total}"
            if retry_at and active and not started and retry_at > datetime.now(timezone.utc):
                item += f"\nالمحاولة القادمة بعد: {retry_at.astimezone(ZoneInfo('Asia/Riyadh')).strftime('%I:%M %p')}"
            if error:
                item += f"\n⚠️ {escape(compact_text(error, 160))}"
        else:
            end = expiry.strftime("%d-%m-%Y") if expiry else "غير محدد"
            item += f'\nنهاية العرض: {end}\n<a href="{escape(url, quote=True)}">رابط العرض</a>'
            buttons.append([menu_button(str(number), f"page:details:{page_id}:{page}")])
        lines.append(item)
    menu_message(lines, buttons + menu_navigation(view, page, len(rows) > MENU_SIZE), message)


def show_page_details(page_id, page, message, confirm=False):
    with db() as conn:
        row = conn.execute("SELECT title,url,active FROM green_deal_pages WHERE id=%s", (page_id,)).fetchone()
    back = [menu_button("⬅️ الصفحات", f"menu:pages:{page}")]
    if not row:
        menu_message(["الصفحة غير موجودة."], [back], message)
        return
    title, url, active = row
    label = escape(compact_text(title, 120) or "صفحة عرض")
    if confirm:
        buttons = [[menu_button("🗑 تأكيد الحذف", f"page:remove:{page_id}:{page}"), menu_button("إلغاء", f"page:details:{page_id}:{page}")]]
        lines = [f"<b>{label}</b>", "حذف الصفحة من المتابعة؟"]
    else:
        action = "stop" if active else "enable"
        toggle = "⏸ إيقاف المتابعة" if active else "▶️ استئناف المتابعة"
        buttons = [[menu_button(toggle, f"page:{action}:{page_id}:{page}"), menu_button("🗑 حذف", f"page:confirm:{page_id}:{page}")]]
        lines = [f"<b>{label}</b>", "🟢 مفعّلة" if active else "⏸ متوقفة", f'<a href="{escape(url, quote=True)}">رابط العرض</a>']
    menu_message(lines, buttons + [back], message)


def command(update):
    message = update.get("message", {})
    if str((message.get("chat") or {}).get("id", "")) != CHAT_ID:
        return
    if THREAD_ID and message.get("message_thread_id") not in (None, THREAD_ID):
        return
    words = (message.get("text") or "").split(maxsplit=1)
    if not words:
        return
    name = words[0].split("@", 1)[0]
    if name == "/favorites":
        name = "/following"
    argument = words[1].strip() if len(words) > 1 else ""
    if name == "/add":
        if argument:
            add_page(argument)
        else:
            send("أرسل /add متبوعًا برابط صفحة العرض.\nتقدر تضيف الاسم بعد علامة |.")
    elif name in {"/pages", "/status"}:
        show_pages(name[1:])
    elif name.startswith("/") and name[1:] in PRODUCT_MENUS:
        show_product_menu(name[1:])
    elif name in {"/help", "/start"}:
        show_help()


def menu_callback(query, message, data):
    selection = re.fullmatch(r"menu:([a-z]+):([0-9]{1,6})", data)
    manage = re.fullmatch(r"manage:([a-z]+):([0-9]{1,6}):([A-Z0-9]{10})", data)
    page_action = re.fullmatch(r"page:(details|confirm|stop|enable|remove):([0-9]{1,18}):([0-9]{1,6})", data)
    answer = {"callback_query_id": query["id"]}
    if selection:
        view, raw_page = selection.groups()
        page = int(raw_page)
        answer_callback(answer)
        if view == "help":
            show_help(message)
        elif view in {"pages", "status"}:
            show_pages(view, page, message)
        elif view == "favorites":
            show_product_menu("following", page, message)
        elif view in PRODUCT_MENUS:
            show_product_menu(view, page, message)
    elif manage and manage[1] == "favorites":
        answer_callback(answer)
        show_product_menu("following", int(manage[2]), message)
    elif manage and manage[1] in PRODUCT_MENUS:
        view, raw_page, asin = manage.groups()
        preference = PRODUCT_MENUS[view][2]
        with db() as conn:
            if preference is None:
                conn.execute("DELETE FROM green_deal_product_blocks WHERE asin=%s", (asin,))
            else:
                conn.execute(
                    f"UPDATE green_deal_product_preferences SET {preference}=false,updated_at=now() WHERE asin=%s",
                    (asin,),
                )
        answer_callback({**answer, "text": "تم التحديث"})
        show_product_menu(view, int(raw_page), message)
    elif page_action:
        action, raw_page_id, raw_page = page_action.groups()
        page_id, page = int(raw_page_id), int(raw_page)
        if action in {"stop", "enable", "remove"}:
            with db() as conn:
                if action == "remove":
                    conn.execute("DELETE FROM green_deal_pages WHERE id=%s", (page_id,))
                else:
                    conn.execute("UPDATE green_deal_pages SET active=%s,scan_started_at=NULL,scan_failures=0,scan_retry_after=NULL WHERE id=%s", (action == "enable", page_id))
        answer_callback(answer)
        if action == "remove":
            show_pages("pages", page, message)
        else:
            show_page_details(page_id, page, message, confirm=action == "confirm")
    else:
        return False
    return True


def product_callback(query, message, parts):
    answer = {"callback_query_id": query["id"]}
    actions = {"block", "unblock", "follow", "unfollow", "favorite", "unfavorite", "mute", "unmute"}
    if len(parts) not in (3, 4) or parts[1] not in actions or not re.fullmatch(r"[A-Z0-9]{10}", parts[2]):
        answer_callback({**answer, "text": "بيانات الزر غير صالحة", "show_alert": True})
        return

    version, action, asin = parts[:3]
    price = None
    if len(parts) == 4:
        try:
            price = float(parts[3])
        except ValueError:
            pass
        if price is None or not 0 < price < 10000000:
            answer_callback({**answer, "text": "السعر غير صالح", "show_alert": True})
            return
    if action == "block" and price is None:
        answer_callback({**answer, "text": "السعر غير موجود في التنبيه", "show_alert": True})
        return

    with db() as conn:
        _, muted, _ = get_product_controls(conn, asin)
        if action == "block":
            resume_price = round(price * 0.95, 2)
            row = conn.execute("""
                INSERT INTO green_deal_product_blocks(asin,blocked_price,resume_price)
                VALUES (%s,%s,%s) ON CONFLICT(asin) DO NOTHING RETURNING resume_price
            """, (asin, price, resume_price)).fetchone()
            confirmation = f"تم الحجب حتى {row[0]:.2f} ريال" if row else "المنتج محجوب بالفعل"
        elif action == "unblock":
            conn.execute("DELETE FROM green_deal_product_blocks WHERE asin=%s", (asin,))
            confirmation = "تم فك الحجب ✅"
        elif action in {"favorite", "unfavorite"}:
            confirmation = "اضغط «متابعة المنتج» لتفعيل تنبيه الرجوع"
        else:
            column = "following" if action in {"follow", "unfollow"} else "muted"
            if version == "product" and action == "mute":
                active = not muted
            else:
                active = action in {"follow", "mute"}
            conn.execute(f"""
                INSERT INTO green_deal_product_preferences(asin,{column}) VALUES (%s,%s)
                ON CONFLICT(asin) DO UPDATE SET {column}=excluded.{column},updated_at=now()
            """, (asin, active))
            if column == "following":
                confirmation = "بتوصلك رسالة إذا رجع المنتج للصفحة" if active else "تم إيقاف متابعة المنتج"
            else:
                confirmation = "تم إيقاف التنبيه 🔕" if active else "تم استئناف التنبيه 🔔"

        following, muted, blocked = get_product_controls(conn, asin)
        if price is None:
            row = conn.execute("""
                SELECT price FROM green_deal_products
                WHERE asin=%s AND price > 0 ORDER BY last_seen_at DESC, page_id DESC LIMIT 1
            """, (asin,)).fetchone()
            if row:
                price = float(row[0])

    answer_callback({**answer, "text": confirmation})
    try:
        tg("editMessageReplyMarkup", {
            "chat_id": CHAT_ID,
            "message_id": message["message_id"],
            "reply_markup": product_keyboard(asin, price, following, muted, blocked),
        })
    except requests.HTTPError as exc:
        response = exc.response
        if response is None or response.status_code != 400:
            raise
        description = response.json().get("description", "").lower()
        if "message is not modified" not in description:
            raise


def callback(update):
    query = update.get("callback_query", {})
    message = query.get("message", {})
    if str((message.get("chat") or {}).get("id")) != CHAT_ID:
        return
    data = str(query.get("data") or "")
    if menu_callback(query, message, data):
        return
    parts = data.split(":")
    if parts[0] in {"deal", "product"}:
        product_callback(query, message, parts)
        return

    try:
        _, action, raw_id = data.split(":")
        page_id = int(raw_id)
    except (ValueError, TypeError):
        answer_callback({"callback_query_id": query["id"], "text": "الزر غير صالح"})
        return
    if action == "show":
        with db() as conn:
            row = conn.execute("SELECT title FROM green_deal_pages WHERE id=%s", (page_id,)).fetchone()
        answer_callback({"callback_query_id": query["id"], "text": row[0] if row else "الصفحة غير موجودة"})
        return
    with db() as conn:
        if action in {"pause", "resume"}:
            conn.execute("UPDATE green_deal_pages SET active=%s,scan_started_at=NULL,scan_failures=0,scan_retry_after=NULL WHERE id=%s", (action == "resume", page_id))
        elif action == "delete":
            conn.execute("DELETE FROM green_deal_pages WHERE id=%s", (page_id,))
    answer_callback({"callback_query_id": query["id"], "text": "تم"})
    show_pages()


def telegram_worker(stop):
    offset = 0
    print("Telegram command worker started.")
    with requests.Session() as session:
        TELEGRAM.session = session
        while not stop.is_set():
            try:
                updates = tg("getUpdates", {
                    "offset": offset, "timeout": 25,
                    "allowed_updates": ["message", "callback_query"],
                })
            except Exception as exc:
                response = getattr(exc, "response", None)
                status = response.status_code if response is not None else None
                print(f"Telegram polling failed: {type(exc).__name__} (HTTP {status}); retrying.")
                # Back off on polling errors, including overlapping pollers and rate limits.
                stop.wait(30 if status == 429 else 3)
                continue
            for item in updates:
                if stop.is_set():
                    return
                try:
                    if "message" in item:
                        command(item)
                    elif "callback_query" in item:
                        callback(item)
                except Exception as exc:
                    response = getattr(exc, "response", None)
                    status = response.status_code if response is not None else None
                    # Avoid logging exception URLs, which may contain the bot token.
                    print(f"Telegram update {item['update_id']} failed: {type(exc).__name__} (HTTP {status}).")
                offset = item["update_id"] + 1


def parse_expiry_date(text):
    value = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")
    normalized = (text or "").translate(value).lower()
    months = {
        "يناير": 1, "فبراير": 2, "مارس": 3, "ابريل": 4, "أبريل": 4,
        "مايو": 5, "يونيو": 6, "يوليو": 7, "أغسطس": 8, "اغسطس": 8,
        "سبتمبر": 9, "أكتوبر": 10, "اكتوبر": 10, "نوفمبر": 11, "ديسمبر": 12,
        "january": 1, "february": 2, "march": 3, "april": 4, "may": 5,
        "june": 6, "july": 7, "august": 8, "september": 9,
        "october": 10, "november": 11, "december": 12,
    }
    found = []
    month_names = "|".join(re.escape(name) for name in months)
    for match in re.finditer(rf"\b(\d{{1,2}})\s+({month_names})\s+(\d{{4}})\b", normalized, re.I):
        day, month_name, year = match.groups()
        try:
            found.append(datetime(int(year), months[month_name.lower()], int(day)).date())
        except ValueError:
            pass
    for match in re.finditer(rf"\b({month_names})\s+(\d{{1,2}}),?\s+(\d{{4}})\b", normalized, re.I):
        month_name, day, year = match.groups()
        try:
            found.append(datetime(int(year), months[month_name.lower()], int(day)).date())
        except ValueError:
            pass
    return max(found) if found else None


def parse_amazon_price(value):
    normalized = str(value or "").translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789"))
    normalized = normalized.replace("٫", ".").replace("٬", ",")
    match = re.search(r"(?<!\d)(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{1,2}))?", normalized)
    if not match:
        return None
    whole = match.group(1).replace(",", "")
    fraction = match.group(2) or ""
    return float(f"{whole}.{fraction}") if fraction else float(whole)


READ_PRODUCT_CARDS = r"""
cards => cards.flatMap(card => {
    const asin = (card.getAttribute("data-asin") || "").trim();
    if (!/^[A-Z0-9]{10}$/.test(asin)) return [];
    const title = card.querySelector("h2, [data-cy='title-recipe-title'], .a-size-base-plus");
    const link = card.querySelector("a[href*='/dp/'], a[href*='/gp/product/']");
    const price = card.querySelector(".a-price");
    const image = card.querySelector("img[data-a-hires], img[data-a-dynamic-image], img[data-src]")
        || card.querySelector("img");
    return [{
        asin,
        title: title?.innerText?.trim() || title?.textContent?.trim() || "",
        link: link?.getAttribute("href") || "",
        fullPrice: price?.querySelector(".a-offscreen")?.textContent || "",
        whole: price?.querySelector(".a-price-whole")?.textContent || "",
        fraction: price?.querySelector(".a-price-fraction")?.textContent || "",
        fallbackPrice: card.innerText || "",
        images: ["data-a-hires", "data-src", "src"].map(attr => image?.getAttribute(attr)),
        dynamicImage: image?.getAttribute("data-a-dynamic-image") || "",
    }];
})
"""


def read_card_price(card):
    price = parse_amazon_price(card["fullPrice"])
    if price is not None:
        return price

    arabic_digits = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")
    whole = re.sub(r"\D", "", card["whole"].translate(arabic_digits))
    fraction = re.sub(r"\D", "", card["fraction"].translate(arabic_digits))
    if whole:
        return parse_amazon_price(f"{whole}.{fraction[:2]}" if fraction else whole)

    text = card["fallbackPrice"].translate(arabic_digits)
    amount = r"([0-9][0-9,٬]*(?:[.٫][0-9]{1,2})?)"
    currency = r"(?:SAR|ر\s*ي\s*ا\s*ل)"
    match = re.search(rf"{currency}\s*{amount}|{amount}\s*{currency}", text, re.I)
    return parse_amazon_price(match.group(1) or match.group(2)) if match else None


def read_card_image(card):
    candidates = card["images"]
    if card["dynamicImage"]:
        try:
            images = json.loads(card["dynamicImage"])
            if isinstance(images, dict):
                candidates = [*candidates, *images]
        except ValueError:
            pass
    for candidate in candidates:
        if candidate and candidate.startswith("https://"):
            host = (urlparse(candidate).hostname or "").lower()
            if host.endswith(("media-amazon.com", "images-amazon.com", "ssl-images-amazon.com")):
                return candidate
    return ""


SHOW_MORE_NAME = re.compile(r"^\s*(إظهار المزيد|Show more|Load more)\s*$", re.I)
SHOW_MORE_WRAPPER = (
    "xpath=ancestor::*[contains(concat(' ', normalize-space(@class), ' '), ' a-button ')][1]"
)
CLICKABLE = "input.a-button-input, button, a[href], [role='button'], input[type='submit'], input[type='button']"


def show_more_button(page):
    labels = page.get_by_text(SHOW_MORE_NAME).filter(visible=True)
    for label in reversed(labels.all()):
        # Amazon's button text can be a span sitting on top of the actual input.
        wrapper = label.locator(SHOW_MORE_WRAPPER)
        controls = wrapper.locator(CLICKABLE).filter(visible=True)
        if controls.count():
            return controls.first
        control = label.locator("xpath=ancestor-or-self::*[self::button or self::a or @role='button'][1]")
        if control.count():
            return control
    control = page.get_by_role("button", name=SHOW_MORE_NAME).filter(visible=True).last
    if control.count():
        return control
    if labels.count():
        raise RuntimeError("Show more label was visible but no clickable control was found.")
    return None


class PromotionPageUnavailable(RuntimeError):
    """Amazon did not provide a usable promotion listing."""


EMPTY_PROMOTION_SNAPSHOT = r"""
expected => {
    const promotion = document.querySelector('#queryModel_promotionId');
    const list = document.querySelector('#productInfoList');
    const empty = document.querySelector('#errorMessageWithoutSelection');
    const more = document.querySelector('#showMoreBtnContainer');
    const visible = node => node && node.getClientRects().length && getComputedStyle(node).visibility !== 'hidden';
    return promotion?.value === expected && list && visible(empty) && !visible(more)
        && ![...list.querySelectorAll('[data-asin]')].some(card => /^[A-Z0-9]{10}$/.test(card.getAttribute('data-asin') || ''));
}
"""


def confirmed_empty_promotion(page, url):
    promotion = re.search(r"/promotion/psp/([A-Z0-9]+)(?:/|$)", urlparse(url).path, re.I)
    if not promotion:
        return False
    try:
        handle = page.wait_for_function(
            EMPTY_PROMOTION_SNAPSHOT, arg=promotion[1].upper(), polling=200, timeout=4000,
        )
        handle.dispose()
        return True
    except PlaywrightTimeout:
        return False


def collect_products(url, on_batch, on_offer_title, browser_channel=None):
    products = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, channel=browser_channel)
        page = browser.new_page(locale="en-US", timezone_id="Asia/Riyadh")
        try:
            wait_for_amazon_slot()
            response = page.goto(url, wait_until="domcontentloaded", timeout=60000)
            title = page.title()
            status = response.status if response else None
            print(f"Opened promotion page: status={status or 'unknown'} title={title!r}")
            if not response or not response.ok:
                if status in {429, 503}:
                    defer_amazon_requests()
                raise PromotionPageUnavailable(f"أمازون ردّ بحالة HTTP {status or 'unknown'}")
            if re.fullmatch(r"(?:نعتذر|sorry|robot check)[!. ]*", title.strip(), re.I):
                defer_amazon_requests()
                raise PromotionPageUnavailable("أمازون عرض صفحة اعتذار أو تحقق")
            cards = page.locator("[data-asin]")
            try:
                page.locator("[data-asin]:not([data-asin='']) .a-price").first.wait_for(state="attached", timeout=4000)
            except PlaywrightTimeout:
                pass  # Read the available cards or report the page error below.

            offer_title, page_text = page.evaluate(
                "() => [document.querySelector('h1')?.innerText?.trim() || '', document.body?.innerText || '']"
            )
            on_offer_title(offer_title, page_text, page.title())
            for batch in range(100):
                batch_start = time.monotonic()
                snapshots = cards.evaluate_all(READ_PRODUCT_CARDS)
                print(f"Promotion batch {batch + 1}: {len(snapshots)} product cards read "
                      f"in {time.monotonic() - batch_start:.2f}s")
                changed = {}
                for card in snapshots:
                    asin = card["asin"]
                    price = read_card_price(card)
                    if price is None or price <= 0:
                        continue
                    link = card["link"]
                    product_url = ("https://www.amazon.sa" + link) if link.startswith("/") else (link or f"https://www.amazon.sa/dp/{asin}")
                    product = (card["title"][:300] or asin, price, product_url, read_card_image(card))
                    if products.get(asin) != product:
                        changed[asin] = product
                if changed:
                    on_batch(changed)
                    products.update(changed)
                print(f"Promotion batch {batch + 1} processed in {time.monotonic() - batch_start:.2f}s")

                try:
                    more = show_more_button(page)
                    if more is None or not more.is_enabled():
                        print("No enabled 'show more' control; reached the end of the offer.")
                        break
                    previous = list({card["asin"] for card in snapshots})
                    wait_for_amazon_slot()
                    more.click(timeout=5000)
                    page.wait_for_function(
                        """previous => {
                            const seen = new Set(previous);
                            return [...document.querySelectorAll('[data-asin]')].some(card => {
                                const asin = (card.getAttribute('data-asin') || '').trim();
                                return /^[A-Z0-9]{10}$/.test(asin) && !seen.has(asin);
                            });
                        }""",
                        arg=previous,
                        timeout=12000,
                    )
                    page.wait_for_timeout(700)
                except PlaywrightError as exc:
                    raise RuntimeError(
                        f"Could not load next batch after {len(products)} products: {str(exc).splitlines()[0]}"
                    ) from exc
            else:
                raise RuntimeError("Pagination limit reached; scan is incomplete.")
            if not products:
                if snapshots:
                    raise PromotionPageUnavailable(f"أمازون عرض {len(snapshots)} بطاقة بدون أسعار قابلة للقراءة")
                if confirmed_empty_promotion(page, url):
                    print("Amazon confirmed an empty promotion. Keeping this page active for the next scan.")
                else:
                    raise PromotionPageUnavailable("صفحة العرض فتحت بدون بطاقات منتجات ولم تؤكد أمازون أنها فارغة")
        finally:
            browser.close()
    return products



def promotion_details(title):
    digits = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹٫٬٪", "01234567890123456789.,%")
    normalized = re.sub(r"[\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]", "", (title or "").translate(digits).lower())
    free = re.search(
        r"(?:\bbuy\b|(?:اشتر(?:ي)?|إشتر(?:ي)?))\s*(?P<paid>[0-9]+)\s*"
        r"(?:(?:items?|units?|pieces?|قطع|قطعة|منتجات)\s*)?[,،]?\s*"
        r"(?:(?:and\s+)?get\b|و?\s*احصل\s+على|وخذ)\s*(?P<free>[0-9]+)\s*"
        r"(?:(?:items?|units?|pieces?|قطع|قطعة|منتجات)\s*)?(?:free\b|مجان[ًاا]?|مجاني(?:ة)?)",
        normalized,
    )
    if free:
        paid, extra = int(free["paid"]), int(free["free"])
        if paid > 0 and extra > 0:
            return {"buy_quantity": paid + extra, "paid_quantity": paid}

    currency = r"(?:sar|sr|ريال(?:\s+سعودي)?|ر\.?\s*س\.?|﷼)"
    amount = r"(?P<total>[0-9]+(?:,[0-9]{3})*(?:\.[0-9]{1,2})?)(?!(?:[0-9]|[.,][0-9]|\s*%))"
    fixed = re.search(
        rf"(?:\bbuy\s+(?:any\s+)?|(?:اشتر(?:ي)?|إشتر(?:ي)?)\s*)?"
        rf"(?<![\d.,])(?P<quantity>[0-9]+)\s*(?:(?:items?|units?|pieces?|قطع|قطعة|منتجات)\s*)?"
        rf"(?:\bfor\b|بسعر|مقابل|بـ?)\s*(?:{currency}\s*)?{amount}"
        rf"(?!\s*(?:{currency}\s*)?(?:%|each\b|per\b|لل?(?:قطعة|وحدة)))",
        normalized,
    )
    if fixed:
        quantity = int(fixed["quantity"])
        total = Decimal(fixed["total"].replace(",", ""))
        if quantity >= 2 and total > 0:
            return {"buy_quantity": quantity, "bundle_total": total}

    percent_match = re.search(r"(\d+(?:\.\d+)?)\s*%", normalized)
    if not percent_match:
        return None
    percent = float(percent_match.group(1))
    if not 0 < percent < 100:
        return None
    buy_match = re.search(r"buy\s*(\d+)", normalized)
    buy_quantity = int(buy_match.group(1)) if buy_match else None
    on_match = re.search(r"\bon\s*(\d+)\b", normalized)
    discounted_quantity = int(on_match.group(1)) if on_match and buy_quantity else buy_quantity
    if buy_quantity and (discounted_quantity is None or discounted_quantity > buy_quantity):
        discounted_quantity = buy_quantity
    return {
        "percent": percent,
        "buy_quantity": buy_quantity,
        "discounted_quantity": discounted_quantity,
    }


class ScanStopped(Exception):
    """The page was paused or removed during a scan."""


def scan_page(page_id, url, browser_channel=None):
    with db() as conn:
        scan_page_with_connection(conn, page_id, url, browser_channel)


def scan_page_with_connection(conn, page_id, url, browser_channel):
    found = {}
    with conn.transaction():
        page_row = conn.execute("SELECT title FROM green_deal_pages WHERE id=%s", (page_id,)).fetchone()
    promotion_title = page_row[0] if page_row else ""
    promotion = promotion_details(promotion_title)

    def save_offer_title(value, page_text, document_title):
        nonlocal promotion_title, promotion
        expiry = parse_expiry_date(page_text)
        document_offer = re.sub(r"^Amazon\.sa\s*[:|–-]\s*|\s+promotion\s*$", "", document_title, flags=re.I)
        document_offer = " ".join(document_offer.split())
        value = " ".join(value.split())
        discovered_title = value
        discovered = promotion_details(value)
        if not discovered and document_offer:
            discovered = promotion_details(document_offer)
            if discovered:
                discovered_title = document_offer
        if discovered:
            promotion_title, promotion = discovered_title[:200], discovered
        elif not promotion:
            raw_title = value
            if document_offer and document_offer.lower() not in {"amazon.sa", "amazon", "نعتذر"}:
                if not raw_title or re.search(r"\bpromotion\s*$", document_title, re.I):
                    raw_title = document_offer
            if raw_title:
                promotion_title = raw_title[:200]
        with conn.transaction():
            conn.execute(
                "UPDATE green_deal_pages SET title=CASE WHEN title='' THEN %s ELSE title END,"
                "offer_terms=%s,expires_on=COALESCE(%s,expires_on) WHERE id=%s",
                (promotion_title, promotion_title, expiry, page_id),
            )

    def save_product(asin, title, price, product_url, image_url):
        found[asin] = (title, price, product_url)
        with conn.transaction():
            page_active = conn.execute(
                "SELECT active FROM green_deal_pages WHERE id=%s FOR SHARE", (page_id,)
            ).fetchone()
            if not page_active or not page_active[0]:
                raise ScanStopped()
            state = conn.execute("""
                SELECT saved.price, saved.present, saved.pending_alert, saved.pending_url,
                       saved.pending_image_url, COALESCE(pref.muted,false),
                       COALESCE(pref.following,false), block.resume_price
                FROM (VALUES (%s::text)) AS product(asin)
                LEFT JOIN LATERAL (
                    SELECT price,present,pending_alert,pending_url,pending_image_url
                    FROM green_deal_products WHERE page_id=%s AND asin=product.asin
                    FOR UPDATE
                ) AS saved ON true
                LEFT JOIN green_deal_product_preferences AS pref ON pref.asin=product.asin
                LEFT JOIN green_deal_product_blocks AS block ON block.asin=product.asin
            """, (asin, page_id)).fetchone()
            old = state[:5] if state[1] is not None else None
            is_muted, following, resume_price = state[5:]
            event = None
            if resume_price is not None and price > float(resume_price):
                event = None
            elif resume_price is not None:
                conn.execute("DELETE FROM green_deal_product_blocks WHERE asin=%s", (asin,))
                event = f"🔓 انفك حجب المنتج\n\n🏷️ {title}\n\n💰 السعر الحالي: {price:.2f} ريال"
            elif old is None:
                event = f"🆕 منتج جديد\n\n🏷️ {title}\n\n💰 السعر الحالي: {price:.2f} ريال"
            elif not old[1]:
                if following:
                    event = f"{RETURN_ALERT}\n\n🏷️ {title}\n\n💰 السعر الحالي: {price:.2f} ريال"
            elif old[0] is not None and price < float(old[0]):
                event = f"📉 انخفاض سعر المنتج\n\n🏷️ {title}\n\n📉 السعر السابق: {float(old[0]):.2f} ريال\n💵 السعر الحالي: {price:.2f} ريال"

            pending_alert = old[2] if old else None
            if pending_alert and pending_alert.startswith(RETURN_ALERT) and not following:
                conn.execute(
                    "UPDATE green_deal_products SET pending_alert=NULL,pending_url=NULL,pending_image_url=NULL, "
                    "pending_new_offers=NULL,pending_price=NULL,pending_revision=pending_revision+1 "
                    "WHERE page_id=%s AND asin=%s", (page_id, asin),
                )
                pending_alert = None
            conn.execute("""
              INSERT INTO green_deal_products(page_id,asin,title,price,product_url,image_url,present,last_seen_at)
              VALUES (%s,%s,%s,%s,%s,%s,true,now())
              ON CONFLICT(page_id,asin) DO UPDATE SET title=excluded.title,price=excluded.price,
                product_url=excluded.product_url, image_url=COALESCE(NULLIF(excluded.image_url,''),green_deal_products.image_url),
                detail_due_at=CASE WHEN NOT green_deal_products.present
                                  THEN now() ELSE green_deal_products.detail_due_at END,
                present=true,last_seen_at=now()
            """, (page_id, asin, title, price, product_url, image_url))
            if event and not is_muted:
                conn.execute("""
                    UPDATE green_deal_products
                    SET pending_alert=%s,pending_url=%s,pending_image_url=%s,
                        pending_revision=pending_revision + 1,pending_new_offers=NULL,pending_price=%s,
                        detail_round_at=NULL,detail_due_at=now()
                    WHERE page_id=%s AND asin=%s
                """, (event, product_url, image_url, price, page_id, asin))

    def save_batch(products):
        incoming = [
            {"asin": asin, "title": title, "price": price, "product_url": url, "image_url": image}
            for asin, (title, price, url, image) in products.items()
        ]
        with conn.transaction():
            page_active = conn.execute(
                "SELECT active FROM green_deal_pages WHERE id=%s FOR SHARE", (page_id,)
            ).fetchone()
            if not page_active or not page_active[0]:
                raise ScanStopped()
            rows = conn.execute("""
                UPDATE green_deal_products AS saved
                SET title=incoming.title, product_url=incoming.product_url, last_seen_at=now(),
                    image_url=COALESCE(NULLIF(incoming.image_url,''),saved.image_url)
                FROM jsonb_to_recordset(%s::jsonb)
                    AS incoming(asin text, title text, price numeric, product_url text, image_url text)
                WHERE saved.page_id=%s AND saved.asin=incoming.asin
                  AND saved.present AND saved.price=incoming.price
                  AND NOT EXISTS (
                      SELECT 1 FROM green_deal_product_blocks AS block
                      WHERE block.asin=saved.asin AND block.resume_price >= incoming.price
                  )
                RETURNING saved.asin
            """, (json.dumps(incoming), page_id)).fetchall()
        unchanged = {row[0] for row in rows}
        print(f"Updated {len(unchanged)} unchanged products in one query; "
              f"{len(products) - len(unchanged)} require individual processing.")
        for asin, product in products.items():
            if asin in unchanged:
                found[asin] = product[:3]
            else:
                save_product(asin, *product)

    collect_products(url, save_batch, save_offer_title, browser_channel)
    # An empty result only reaches here after Amazon rendered its empty grid state.
    with conn.transaction():
        conn.execute("UPDATE green_deal_products SET present=false WHERE page_id=%s AND asin <> ALL(%s::text[])", (page_id, list(found)))
        conn.execute("UPDATE green_deal_pages SET last_checked_at=now(),last_error=NULL,scan_started_at=NULL,scan_failures=0,scan_retry_after=NULL WHERE id=%s", (page_id,))


def claim_product_detail(conn, detail_at, listings_round, listings_done, pending_only, prioritize_alerts):
    with conn.transaction():
        return conn.execute("""
            WITH next_product AS (
                SELECT d.page_id,d.asin
                FROM green_deal_products d JOIN green_deal_pages p ON p.id=d.page_id
                WHERE p.active AND d.present AND d.price>0
                  AND d.detail_round_at IS DISTINCT FROM %s
                  AND (d.detail_attempt_round IS DISTINCT FROM %s OR d.detail_due_at<=now())
                  AND (%s OR EXISTS (
                      SELECT 1 FROM green_deal_scan_cycle
                      WHERE singleton AND round_at=%s AND rest_until IS NULL
                  ))
                  AND (%s OR d.last_seen_at>=%s)
                  AND (d.detail_lease_until IS NULL OR d.detail_lease_until<=now())
                  AND (NOT %s OR d.pending_alert IS NOT NULL)
                ORDER BY (%s AND d.pending_alert IS NOT NULL) DESC,
                         d.detail_failure_count,d.detail_due_at,d.page_id,d.asin
                LIMIT 1 FOR UPDATE OF d SKIP LOCKED
            )
            UPDATE green_deal_products d
            SET detail_lease_until=now()+interval '5 minutes'
            FROM next_product candidate
            WHERE d.page_id=candidate.page_id AND d.asin=candidate.asin
            RETURNING d.page_id,d.asin,d.product_url,d.price,d.detail_lease_until,
                      CASE WHEN d.detail_attempt_round=%s THEN d.detail_attempts ELSE 0 END
        """, (detail_at, detail_at, pending_only, listings_round, listings_done, listings_round,
              pending_only, prioritize_alerts, detail_at)).fetchone()


def reserve_combination_alert(conn, asin, page_id, revision):
    return conn.execute("""
        INSERT INTO green_deal_combination_alerts AS saved (asin,page_id,pending_revision)
        VALUES (%s,%s,%s)
        ON CONFLICT (asin) DO UPDATE
        SET page_id=excluded.page_id, pending_revision=excluded.pending_revision
        WHERE saved.sent_at IS NULL AND (
            saved.page_id=excluded.page_id OR NOT EXISTS (
                SELECT 1 FROM green_deal_products d JOIN green_deal_pages p ON p.id=d.page_id
                WHERE d.asin=saved.asin AND d.page_id=saved.page_id AND d.pending_alert IS NOT NULL
                  AND d.pending_revision=saved.pending_revision AND d.present AND p.active
            )
        )
        RETURNING asin
    """, (asin, page_id, revision)).fetchone() is not None


def record_product_detail(conn, claim, snapshot):
    page_id, asin, product_url, price, lease, _ = claim
    with conn.transaction():
        row = conn.execute("""
            SELECT d.title,d.price,d.product_url,d.image_url,d.pending_alert,d.pending_url,
                   d.pending_image_url,d.pending_revision,d.pending_new_offers,d.detail_offers,
                   COALESCE(NULLIF(p.offer_terms,''),p.title),d.detail_green_terms,d.pending_price,
                   (p.active AND d.present),
                   NOT EXISTS (SELECT 1 FROM green_deal_product_preferences pref WHERE pref.asin=d.asin AND pref.muted)
                   AND NOT EXISTS (SELECT 1 FROM green_deal_product_blocks block WHERE block.asin=d.asin)
            FROM green_deal_products d JOIN green_deal_pages p ON p.id=d.page_id
            WHERE d.page_id=%s AND d.asin=%s AND d.detail_lease_until=%s
            FOR UPDATE OF d
        """, (page_id, asin, lease)).fetchone()
        if not row:
            return None
        (title, current_price, current_url, image, pending, pending_url, pending_image, revision,
         pending_keys, previous, green_title, previous_green, pending_price, active, can_announce) = row
        if not active or (current_url, current_price) != (product_url, price):
            conn.execute("""
                UPDATE green_deal_products SET detail_lease_until=NULL,detail_due_at=now()
                WHERE page_id=%s AND asin=%s AND detail_lease_until=%s
            """, (page_id, asin, lease))
            return None

        green = promotion_details(green_title) if not BANK_TERMS.search(green_title) else None
        green_key = green_offer_key(green_title) if green_title else ""
        if snapshot is not None:
            snapshot = without_green_duplicate(snapshot, green_title)
            new = {
                key for key in new_notifiable_offers(previous, snapshot)
                if not duplicates_green_spend(snapshot[key], green_title, green)
            }
            if previous_green is not None and green_key and green_key != previous_green:
                if not is_quiet_offer(green_title):
                    new.add("__green__")
            pending_keys = sorted(set(pending_keys or ()) | new)
            first_combination = False
            if can_announce and has_notifiable_combination(snapshot, green_key, green_title, green):
                first_combination = reserve_combination_alert(conn, asin, page_id, revision if pending else revision + 1)
            if (new or first_combination) and not pending:
                title_line = "🆕 خصم جديد" if new else "🎁 أكثر من عرض على المنتج"
                pending = f"{title_line}\n\n🏷️ {title}\n\n💰 السعر الحالي: {price:.2f} ريال"
                pending_url, pending_image, pending_price = product_url, image, price
                revision += 1
            conn.execute("""
                UPDATE green_deal_products
                SET detail_offers=%s,detail_green_terms=%s,pending_alert=%s,pending_url=%s,
                    pending_image_url=%s,pending_revision=%s,pending_new_offers=%s,pending_price=%s,
                    detail_failure_count=0
                WHERE page_id=%s AND asin=%s
            """, (Jsonb(snapshot), green_key, pending, pending_url, pending_image, revision,
                  Jsonb(pending_keys) if pending_keys else None, pending_price, page_id, asin))
        else:
            conn.execute("""
                UPDATE green_deal_products SET detail_failure_count=detail_failure_count+1
                WHERE page_id=%s AND asin=%s
            """, (page_id, asin))
    if not pending:
        return revision, None
    alert_price = pending_price if pending_price is not None else price
    body = format_offer_alert(
        pending, alert_price, green_title, green, snapshot if snapshot is not None else previous or {},
        pending_keys or (), cached=snapshot is None,
    )
    url = amazon_product_url(pending_url or product_url, asin)
    return revision, (body, url, pending_image or image, asin, alert_price)


def finish_product_detail(conn, claim, revision, round_at, handled, sent):
    page_id, asin, _, _, lease, _ = claim
    with conn.transaction():
        updated = conn.execute("""
            UPDATE green_deal_products
            SET detail_lease_until=NULL,detail_attempt_round=NULL,detail_attempts=0,
                detail_round_at=CASE WHEN pending_revision<>%s AND pending_alert IS NOT NULL THEN NULL ELSE %s END,
                detail_due_at=now(),
                pending_alert=CASE WHEN pending_revision=%s AND %s THEN NULL ELSE pending_alert END,
                pending_url=CASE WHEN pending_revision=%s AND %s THEN NULL ELSE pending_url END,
                pending_image_url=CASE WHEN pending_revision=%s AND %s THEN NULL ELSE pending_image_url END,
                pending_new_offers=CASE WHEN pending_revision=%s AND %s THEN NULL ELSE pending_new_offers END,
                pending_price=CASE WHEN pending_revision=%s AND %s THEN NULL ELSE pending_price END
            WHERE page_id=%s AND asin=%s AND detail_lease_until=%s
        """, (revision, round_at, *((revision, handled) * 5), page_id, asin, lease))
        if updated.rowcount and sent:
            conn.execute("""
                UPDATE green_deal_combination_alerts SET sent_at=now()
                WHERE asin=%s AND page_id=%s AND pending_revision=%s AND sent_at IS NULL
            """, (asin, page_id, revision))


def defer_product_retry(conn, claim, round_at, attempt, delay):
    page_id, asin, _, _, lease, _ = claim
    with conn.transaction():
        conn.execute("""
            UPDATE green_deal_products
            SET detail_lease_until=NULL,detail_attempt_round=%s,detail_attempts=%s,
                detail_due_at=now()+make_interval(secs=>%s)
            WHERE page_id=%s AND asin=%s AND detail_lease_until=%s
        """, (round_at, attempt, delay, page_id, asin, lease))


def read_product_attempt(page, url, asin, price, attempt):
    target = url if attempt == 1 else canonical_product_url(url, asin)
    wait_for_amazon_slot()
    try:
        return read_detail_offers(page, target, asin, price), None, 0
    except (PlaywrightError, RuntimeError, ValueError) as exc:
        reason = str(exc) if isinstance(exc, ProductPageUnavailable) else type(exc).__name__
        site_rejection = any(label in reason for label in ("HTTP 429", "HTTP 503", "صفحة تحقق من أمازون"))
        if site_rejection:
            defer_amazon_requests()
        # Keep product-level apology pages from pausing requests for every other product.
        apology = reason == "صفحة اعتذار من أمازون"
        delay = (15 if site_rejection or apology else 5) * 2 ** (attempt - 1)
        return None, reason, delay


def should_restart_detail_browser(reason):
    return bool(reason) and (
        reason.startswith("انتهت مهلة")
        or reason in {"TimeoutError", "TargetClosedError", "BrowserClosedError"}
    )


def detail_worker_loop(stop, page, detail_round, pending_only):
    sequence = 0
    finished = 0
    succeeded = 0
    with db() as conn:
        while not stop.is_set():
            if not detail_round.ready.wait(1):
                continue
            if detail_round.paused.is_set():
                stop.wait(2)
                continue
            listing_round = detail_round.started_at
            round_at = detail_round.detail_at
            listing_finished = detail_round.listings_done.is_set()
            claim = claim_product_detail(conn, round_at, listing_round, listing_finished, pending_only, sequence % 4 != 3)
            if not claim:
                stop.wait(2)
                continue
            sequence += 1
            _, asin, url, price, _, previous_attempts = claim
            if stop.is_set() or detail_round.paused.is_set():
                defer_product_retry(conn, claim, round_at, previous_attempts, 0)
                continue
            attempt = previous_attempts + 1
            print(f"Checking product {asin}, attempt {attempt}/4.")
            snapshot, reason, delay = read_product_attempt(page, url, asin, price, attempt)
            rejected = bool(reason and any(label in reason for label in (
                "HTTP 429", "HTTP 503", "صفحة تحقق من أمازون", "صفحة اعتذار من أمازون"
            )))
            detail_round.note_outcome(listing_round, rejected)
            if snapshot is None and attempt < 4:
                print(f"Product {asin} deferred after attempt {attempt}/4: {reason}.")
            restart_browser = page.is_closed() or should_restart_detail_browser(reason)
            if snapshot is None and attempt < 4:
                defer_product_retry(conn, claim, round_at, attempt, delay)
                if restart_browser:
                    print("Reopening product browser after a load or browser failure.")
                    return
                continue

            finished += 1
            if snapshot is None:
                print(f"Product offers unavailable for {asin} after 4/4 attempts: {reason}")
            else:
                succeeded += 1
                print(f"Product {asin} read successfully: {len(snapshot)} offer(s).")
            if finished % 25 == 0:
                print(f"Product detail progress: {succeeded}/{finished} pages read successfully.")
            result = record_product_detail(conn, claim, snapshot)
            if result is None:
                if page.is_closed():
                    return
                continue
            revision, alert = result
            handled = sent = False
            if alert and not stop.is_set():
                try:
                    sent = send_product_alert(*alert)
                    handled = True
                except Exception as exc:
                    print(f"Product alert remains queued for {asin}: {type(exc).__name__}")
            finish_product_detail(conn, claim, revision, round_at, handled, sent)
            if restart_browser or page.is_closed():
                print("Reopening product browser after a load or browser failure.")
                return


def product_detail_worker(stop, browser_channel, detail_round, pending_only):
    with requests.Session() as session:
        TELEGRAM.session = session
        while not stop.is_set():
            try:
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True, channel=browser_channel)
                    context = browser.new_context(locale="en-US", timezone_id="Asia/Riyadh", service_workers="block")
                    context.route("**/*", lambda route: (
                        route.abort() if route.request.resource_type in {"image", "font", "media"} else route.continue_()
                    ))
                    try:
                        page = context.new_page()
                        print("Amazon product offer worker started.")
                        detail_worker_loop(stop, page, detail_round, pending_only)
                    finally:
                        browser.close()
            except Exception as exc:
                # Do not log request URLs, as exceptions can contain credentials.
                print(f"Amazon product offer worker restarting: {type(exc).__name__}")
                stop.wait(5)


def wait_for_product_round(stop, detail_round, round_at, pending_only):
    deadline = time.monotonic() + 60 if pending_only else None
    next_report = time.monotonic() + 30
    with db() as conn:
        while not stop.is_set() and (deadline is None or time.monotonic() < deadline):
            if detail_round.paused.is_set():
                return False
            with conn.transaction():
                unfinished, waiting = conn.execute("""
                    SELECT count(*),count(*) FILTER (
                        WHERE d.detail_attempt_round=%s AND d.detail_due_at>now()
                    )
                    FROM green_deal_products d JOIN green_deal_pages p ON p.id=d.page_id
                    WHERE p.active AND d.present AND d.price>0
                      AND d.detail_round_at IS DISTINCT FROM %s
                      AND (NOT %s OR d.pending_alert IS NOT NULL)
                """, (round_at, round_at, pending_only)).fetchone()
            if not unfinished:
                return True
            if time.monotonic() >= next_report:
                print(f"Product round remaining: {unfinished}, including {waiting} waiting for a retry.")
                next_report = time.monotonic() + 30
            stop.wait(5)
    return False


def scan_promotion_pages(browser_channel):
    today = datetime.now(ZoneInfo("Asia/Riyadh")).date()
    with db() as conn:
        conn.execute("UPDATE green_deal_pages SET active=false WHERE active=true AND expires_on < %s", (today,))
        pages = conn.execute("""
            SELECT id,url FROM green_deal_pages WHERE active=true
              AND (scan_retry_after IS NULL OR scan_retry_after<=now())
            ORDER BY scan_failures,id
        """).fetchall()
    for page_id, url in pages:
        for attempt in range(1, 5):
            try:
                with db() as conn:
                    updated = conn.execute(
                        "UPDATE green_deal_pages SET scan_started_at=now(),last_error=NULL WHERE id=%s AND active",
                        (page_id,),
                    )
                    if not updated.rowcount:
                        break
                print(f"Starting page {page_id}, attempt {attempt}/4")
                scan_page(page_id, url, browser_channel)
                break
            except ScanStopped:
                with db() as conn:
                    conn.execute("UPDATE green_deal_pages SET scan_started_at=NULL WHERE id=%s", (page_id,))
                break
            except Exception as exc:
                detail = f"{type(exc).__name__}: {str(exc).replace(TOKEN, '[redacted]')[:300]}"
                print(f"Page {page_id}, attempt {attempt}/4 failed: {detail}")
                with db() as conn:
                    conn.execute("UPDATE green_deal_pages SET last_error=%s WHERE id=%s", (detail, page_id))
                if attempt < 4:
                    time.sleep((10, 20, 40)[attempt - 1])
        else:
            with db() as conn:
                row = conn.execute("""
                    UPDATE green_deal_pages
                    SET last_checked_at=now(),scan_started_at=NULL,
                        scan_retry_after=now()+make_interval(mins=>LEAST(30, 5*(scan_failures+1))),
                        scan_failures=scan_failures+1
                    WHERE id=%s AND active
                    RETURNING LEAST(30,5*scan_failures)
                """, (page_id,)).fetchone()
                if row:
                    print(f"Page {page_id} will retry in {row[0]} minutes; saved availability was preserved.")


def begin_continuous_round(stop):
    while not stop.is_set():
        with db() as conn:
            conn.execute("""
                INSERT INTO green_deal_scan_cycle(singleton,round_at)
                VALUES (true,now())
                ON CONFLICT (singleton) DO UPDATE
                SET round_at=now(),listings_done=false,rest_until=NULL
                WHERE green_deal_scan_cycle.rest_until<=now()
            """)
            round_at, detail_at, listings_done, rest_seconds = conn.execute("""
                SELECT round_at,COALESCE(detail_cycle_at, round_at),listings_done,EXTRACT(EPOCH FROM rest_until-now())
                FROM green_deal_scan_cycle WHERE singleton
            """).fetchone()
        if rest_seconds is None:
            return round_at, detail_at, listings_done
        if rest_seconds > 0:
            stop.wait(float(rest_seconds))
    return None


def scan_loop(stop, detail_round, continuous, browser_channel=None):
    while not stop.is_set():
        if continuous:
            cycle = begin_continuous_round(stop)
        else:
            now = datetime.now(timezone.utc)
            cycle = (now, now, False)
        if cycle is None:
            return
        round_at, detail_at, listings_done = cycle
        detail_round.start(round_at, detail_at, listings_done)
        if not listings_done:
            scan_promotion_pages(browser_channel)
            if continuous:
                with db() as conn:
                    conn.execute("""
                        UPDATE green_deal_scan_cycle SET listings_done=true
                        WHERE singleton AND round_at=%s
                    """, (round_at,))
            detail_round.listings_done.set()
        print("Promotion listings ready. Waiting for product scans or Amazon cooldown.")
        completed = wait_for_product_round(stop, detail_round, detail_at, not continuous)
        detail_round.ready.clear()
        if not continuous or stop.is_set():
            return
        with db() as conn:
            conn.execute("""
                UPDATE green_deal_scan_cycle
                SET rest_until=now()+make_interval(secs=>%s), detail_cycle_at=%s
                WHERE singleton AND round_at=%s
            """, (WAIT_SECONDS, None if completed else detail_at, round_at))
        summary = "Product scan completed." if completed else "Product scan paused; the next round will resume remaining products."
        print(f"{summary} Resting for {WAIT_SECONDS}s.")


def prepare_browser():
    if shutil.which("google-chrome") or shutil.which("google-chrome-stable"):
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(channel="chrome", headless=True, timeout=10000)
                browser.close()
        except PlaywrightError:
            print("Runner Chrome could not start; preparing Playwright Chromium.")
        else:
            print("Using the runner's preinstalled Chrome.")
            return "chrome"

    print("Preparing Chromium headless shell while Telegram handles commands.")
    subprocess.run(
        [sys.executable, "-m", "playwright", "install", "--with-deps", "--only-shell", "chromium"],
        check=True,
        timeout=300,
    )
    return None


def run(continuous, prepare=False):
    if not all((TOKEN, CHAT_ID, DB_URL)):
        raise RuntimeError("Missing required green tracker database or Telegram settings.")
    init_db()
    stop = Event()
    detail_threads = []
    detail_round = DetailRound()
    worker = Thread(target=telegram_worker, args=(stop,), name="telegram-commands", daemon=True)
    worker.start()
    try:
        browser_channel = prepare_browser() if prepare else None
        for index in range(DETAIL_WORKERS):
            detail = Thread(
                target=product_detail_worker, args=(stop, browser_channel, detail_round, not continuous),
                name=f"product-offers-{index + 1}", daemon=True,
            )
            detail.start()
            detail_threads.append(detail)
        scan_loop(stop, detail_round, continuous, browser_channel)
    finally:
        stop.set()
        for detail in detail_threads:
            detail.join(timeout=1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--continuous", action="store_true")
    parser.add_argument("--prepare-browser", action="store_true")
    args = parser.parse_args()
    run(args.continuous, args.prepare_browser)
