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
from threading import Event, Thread, local
from zoneinfo import ZoneInfo
from datetime import datetime, timezone
from urllib.parse import urlparse

import psycopg
import requests
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

TOKEN = os.environ["GREEN_DEALS_TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["GREEN_DEALS_TELEGRAM_CHAT_ID"]
THREAD_ID = int(os.getenv("GREEN_DEALS_TELEGRAM_MESSAGE_THREAD_ID", "0"))
DB_URL = os.environ["GREEN_DEALS_DATABASE_URL"]
API = f"https://api.telegram.org/bot{TOKEN}"
WAIT_SECONDS = int(os.getenv("GREEN_DEALS_SCAN_INTERVAL_SECONDS", "300"))
HTTP = requests.Session()
SCAN_REQUESTED = Event()
TELEGRAM = local()


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
        CREATE TABLE IF NOT EXISTS green_deal_product_blocks (asin text PRIMARY KEY, blocked_price numeric NOT NULL, resume_price numeric NOT NULL, created_at timestamptz NOT NULL DEFAULT now());
        CREATE TABLE IF NOT EXISTS green_deal_product_preferences (asin text PRIMARY KEY, favorite boolean NOT NULL DEFAULT false, muted boolean NOT NULL DEFAULT false, updated_at timestamptz NOT NULL DEFAULT now());
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
        conn.execute("ALTER TABLE green_deal_products ADD COLUMN IF NOT EXISTS pending_alert text")
        conn.execute("ALTER TABLE green_deal_products ADD COLUMN IF NOT EXISTS pending_url text")
        conn.execute("ALTER TABLE green_deal_products ADD COLUMN IF NOT EXISTS pending_image_url text")


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



def product_keyboard(asin, price, favorite, muted, blocked):
    suffix = f":{price:.2f}" if price is not None else ""
    favorite_action = "unfavorite" if favorite else "favorite"
    mute_action = "unmute" if muted else "mute"
    favorite_label = "⭐ إزالة من المفضلة" if favorite else "☆ إضافة للمفضلة"
    mute_label = "🔔 استئناف التنبيه" if muted else "🔕 إيقاف التنبيه"
    main_row = [menu_button(favorite_label, f"deal:{favorite_action}:{asin}{suffix}")]
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
        SELECT COALESCE(pref.favorite,false), COALESCE(pref.muted,false),
               EXISTS(SELECT 1 FROM green_deal_product_blocks WHERE asin=product.asin)
        FROM (VALUES (%s::text)) AS product(asin)
        LEFT JOIN green_deal_product_preferences AS pref ON pref.asin=product.asin
    """, (asin,)).fetchone()
    return tuple(row)


def send_product_alert(caption, product_url, image_url, asin, price):
    with db() as conn:
        favorite, muted, blocked = get_product_controls(conn, asin)
    if muted or blocked:
        return
    markup = product_keyboard(asin, price, favorite, muted, blocked)
    link = f'<a href="{escape(product_url, quote=True)}">رابط المنتج</a>'
    message_text = f"{escape(caption[:1000].rstrip())}\n\n{link}"

    if image_url:
        try:
            host = (urlparse(image_url).hostname or "").lower()
            allowed_image_host = any(
                host.endswith(domain)
                for domain in ("media-amazon.com", "images-amazon.com", "ssl-images-amazon.com")
            )
            if not allowed_image_host:
                raise RuntimeError("Product image host is not an Amazon image domain.")

            image_response = HTTP.get(image_url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
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
            response = HTTP.post(
                f"{API}/sendPhoto",
                data=data,
                files={"photo": ("product.jpg", BytesIO(image_bytes), "image/jpeg")},
                timeout=45,
            )
            result = response.json()
            if not response.ok or not result.get("ok"):
                raise RuntimeError(result.get("description", "Telegram sendPhoto failed"))
            return
        except Exception as exc:
            print(f"Product photo upload failed: {type(exc).__name__}: {exc}")
            raise RuntimeError("تعذر رفع صورة المنتج إلى تيليجرام") from exc

    send(message_text, markup, parse_mode="HTML", disable_preview=True)


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
            "ON CONFLICT(url) DO UPDATE SET active=true,"
            "title=CASE WHEN excluded.title <> '' THEN excluded.title ELSE green_deal_pages.title END",
            (url, title),
        )
    SCAN_REQUESTED.set()
    send(f"تمت إضافة صفحة العرض للمتابعة ✅\n{title or 'سيُقرأ اسم العرض في أول فحص'}\n{url}")


MENU_SIZE = 5
PRODUCT_MENUS = {
    "favorites": ("⭐ المفضلة", "إزالة من المفضلة", "favorite"),
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
        "/favorites — المفضلة\n"
        "/blocked — المنتجات المحجوبة\n"
        "/muted — التنبيهات الموقوفة",
    ], [
        [menu_button("📋 الصفحات", "menu:pages:0"), menu_button("🔎 حالة الفحص", "menu:status:0")],
        [menu_button("⭐ المفضلة", "menu:favorites:0"), menu_button("🚫 المحجوبات", "menu:blocked:0")],
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
               p.last_error, p.scan_started_at, products.present, products.total
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
        page_id, url, active, title, expiry, checked, error, started, present, total = row
        label = escape(compact_text(title, 120) or "بانتظار اسم العرض")
        state = "⏸ متوقفة" if not active else "🔄 يجري الفحص" if started else "🟢 مفعّلة"
        item = f'<b>{number}. {label}</b>\n{state}'
        if status_view:
            last = checked.astimezone(ZoneInfo("Asia/Riyadh")).strftime("%d-%m-%Y %I:%M %p") if checked else "لم تُفحص بعد"
            item += f"\nآخر فحص: {last}\nالمنتجات الظاهرة: {present}/{total}"
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
        elif view in PRODUCT_MENUS:
            show_product_menu(view, page, message)
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
                    conn.execute("UPDATE green_deal_pages SET active=%s,scan_started_at=NULL WHERE id=%s", (action == "enable", page_id))
            if action == "enable":
                SCAN_REQUESTED.set()
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
    actions = {"block", "unblock", "favorite", "unfavorite", "mute", "unmute"}
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
        favorite, muted, _ = get_product_controls(conn, asin)
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
        else:
            column = "favorite" if action in {"favorite", "unfavorite"} else "muted"
            if version == "product" and action in {"favorite", "mute"}:
                active = not (favorite if column == "favorite" else muted)
            else:
                active = action in {"favorite", "mute"}
            conn.execute(f"""
                INSERT INTO green_deal_product_preferences(asin,{column}) VALUES (%s,%s)
                ON CONFLICT(asin) DO UPDATE SET {column}=excluded.{column},updated_at=now()
            """, (asin, active))
            if column == "favorite":
                confirmation = "أضيف للمفضلة ⭐" if active else "أزيل من المفضلة"
            else:
                confirmation = "تم إيقاف التنبيه 🔕" if active else "تم استئناف التنبيه 🔔"

        favorite, muted, blocked = get_product_controls(conn, asin)
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
            "reply_markup": product_keyboard(asin, price, favorite, muted, blocked),
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
            conn.execute("UPDATE green_deal_pages SET active=%s WHERE id=%s", (action == "resume", page_id))
            if action == "resume":
                SCAN_REQUESTED.set()
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


def collect_products(url, on_product, on_offer_title, browser_channel=None):
    products = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, channel=browser_channel)
        page = browser.new_page(locale="en-US", timezone_id="Asia/Riyadh")
        try:
            response = page.goto(url, wait_until="domcontentloaded", timeout=60000)
            print(f"Opened promotion page: status={response.status if response else 'unknown'} title={page.title()!r}")
            cards = page.locator("[data-asin]")
            try:
                page.locator("[data-asin]:not([data-asin='']) .a-price").first.wait_for(state="attached", timeout=4000)
            except PlaywrightTimeout:
                pass  # Read the available cards or report the page error below.

            offer_title, page_text = page.evaluate(
                "() => [document.querySelector('h1')?.innerText?.trim() || '', document.body?.innerText || '']"
            )
            on_offer_title(offer_title, page_text)
            for batch in range(100):
                batch_start = time.monotonic()
                snapshots = cards.evaluate_all(READ_PRODUCT_CARDS)
                print(f"Promotion batch {batch + 1}: {len(snapshots)} product cards read "
                      f"in {time.monotonic() - batch_start:.2f}s")
                for card in snapshots:
                    asin = card["asin"]
                    price = read_card_price(card)
                    if price is None or price <= 0:
                        continue
                    link = card["link"]
                    product_url = ("https://www.amazon.sa" + link) if link.startswith("/") else (link or f"https://www.amazon.sa/dp/{asin}")
                    product = (card["title"][:300] or asin, price, product_url, read_card_image(card))
                    if products.get(asin) != product:
                        products[asin] = product
                        on_product(asin, *product)
                print(f"Promotion batch {batch + 1} processed in {time.monotonic() - batch_start:.2f}s")

                more = page.get_by_text(re.compile(r"^\s*(إظهار المزيد|Show more|Load more)\s*$", re.I)).last
                if more.count() == 0:
                    print("No visible 'show more' control was found.")
                    break
                try:
                    if not more.is_visible() or not more.is_enabled():
                        print("'Show more' control is hidden or disabled.")
                        break
                    before_count = cards.count()
                    more.scroll_into_view_if_needed(timeout=3000)
                    more.click(timeout=8000)
                    page.wait_for_function(
                        "(previous) => document.querySelectorAll('[data-asin]').length > previous",
                        arg=before_count,
                        timeout=12000,
                    )
                    page.wait_for_timeout(700)
                except Exception as exc:
                    raise RuntimeError(f"Could not load next batch after {len(products)} products: {type(exc).__name__}: {exc}") from exc
            if not products:
                body = (page.locator("body").inner_text(timeout=3000) or "")[:500]
                raise RuntimeError(f"No priced ASIN products were parsed. Page title={page.title()!r}; body preview={body!r}")
        finally:
            browser.close()
    return products



def promotion_details(title):
    digits = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")
    normalized = (title or "").translate(digits).replace("٪", "%").lower()
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

    def save_offer_title(value, page_text):
        nonlocal promotion_title, promotion
        expiry = parse_expiry_date(page_text)
        if not promotion_title and value:
            promotion_title = value[:200]
            promotion = promotion_details(promotion_title)
        with conn.transaction():
            conn.execute(
                "UPDATE green_deal_pages SET title=CASE WHEN title='' THEN %s ELSE title END,"
                "expires_on=COALESCE(%s,expires_on) WHERE id=%s",
                (promotion_title, expiry, page_id),
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
                       saved.pending_image_url, COALESCE(pref.muted,false), block.resume_price
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
            is_muted, resume_price = state[5:]
            event = None
            if resume_price is not None and price > float(resume_price):
                event = None
            elif resume_price is not None:
                conn.execute("DELETE FROM green_deal_product_blocks WHERE asin=%s", (asin,))
                event = f"🔓 انفك حجب المنتج\n\n🏷️ {title}\n\n💰 السعر الحالي: {price:.2f} ريال"
            elif old is None:
                event = f"🆕 منتج جديد\n\n🏷️ {title}\n\n💰 السعر الحالي: {price:.2f} ريال"
            elif not old[1]:
                event = f"↩️ عاد المنتج للظهور\n\n🏷️ {title}\n\n💰 السعر الحالي: {price:.2f} ريال"
            elif old[0] is not None and price < float(old[0]):
                event = f"📉 انخفاض سعر المنتج\n\n🏷️ {title}\n\n📉 السعر السابق: {float(old[0]):.2f} ريال\n💵 السعر الحالي: {price:.2f} ريال"

            pending_alert = old[2] if old else None
            was_pending = bool(pending_alert and not is_muted and resume_price is None)
            alert_url = old[3] if old and old[3] else product_url
            alert_image = old[4] if old and old[4] else image_url
            if was_pending:
                event = pending_alert
            conn.execute("""
              INSERT INTO green_deal_products(page_id,asin,title,price,product_url,present,last_seen_at)
              VALUES (%s,%s,%s,%s,%s,true,now())
              ON CONFLICT(page_id,asin) DO UPDATE SET title=excluded.title,price=excluded.price,
                product_url=excluded.product_url,present=true,last_seen_at=now()
            """, (page_id, asin, title, price, product_url))
            percent = promotion.get("percent") if promotion else None
            buy_quantity = promotion.get("buy_quantity") if promotion else None
            discounted_quantity = promotion.get("discounted_quantity") if promotion else None
            if event and not is_muted:
                if percent is not None and not was_pending:
                    if buy_quantity and discounted_quantity:
                        discounted_unit = round(price * (1 - percent / 100), 2)
                        bundle_total = round(price * (buy_quantity - discounted_quantity * percent / 100), 2)
                        average_unit = round(bundle_total / buy_quantity, 2)
                        event += f"\n🎁 سعر القطعة المخفّضة ({percent:g}%): {discounted_unit:.2f} ريال"
                        event += f"\n🛍️ إجمالي {buy_quantity} قطع: {bundle_total:.2f} ريال"
                        event += f"\n📊 متوسط القطعة: {average_unit:.2f} ريال"
                    else:
                        discounted_unit = round(price * (1 - percent / 100), 2)
                        event += f"\n🎁 السعر بعد خصم {percent:g}%: {discounted_unit:.2f} ريال"
                    if promotion_title:
                        event += f"\n📌 شرط العرض: {promotion_title}"
                conn.execute(
                    "UPDATE green_deal_products SET pending_alert=%s,pending_url=%s,pending_image_url=%s "
                    "WHERE page_id=%s AND asin=%s",
                    (event, product_url, image_url, page_id, asin),
                )
        if event and not is_muted:
            send_product_alert(event, alert_url, alert_image, asin, price)
            with conn.transaction():
                conn.execute(
                    "UPDATE green_deal_products SET pending_alert=NULL,pending_url=NULL,pending_image_url=NULL "
                    "WHERE page_id=%s AND asin=%s",
                    (page_id, asin),
                )

    collect_products(url, save_product, save_offer_title, browser_channel)
    if not found:
        raise RuntimeError("No products could be read. Treating this scan as incomplete.")
    with conn.transaction():
        conn.execute("UPDATE green_deal_products SET present=false WHERE page_id=%s AND asin <> ALL(%s)", (page_id, list(found)))
        conn.execute("UPDATE green_deal_pages SET last_checked_at=now(),last_error=NULL,scan_started_at=NULL WHERE id=%s", (page_id,))


def scan_loop(continuous, browser_channel=None):
    while True:
        SCAN_REQUESTED.clear()
        today = datetime.now(ZoneInfo("Asia/Riyadh")).date()
        with db() as conn:
            conn.execute("UPDATE green_deal_pages SET active=false WHERE active=true AND expires_on < %s", (today,))
            pages = conn.execute("SELECT id,url FROM green_deal_pages WHERE active=true ORDER BY id").fetchall()
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
                    conn.execute("UPDATE green_deal_pages SET last_checked_at=now(),scan_started_at=NULL WHERE id=%s", (page_id,))
        if not continuous:
            break
        SCAN_REQUESTED.wait(WAIT_SECONDS)


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
    init_db()
    stop = Event()
    worker = Thread(target=telegram_worker, args=(stop,), name="telegram-commands", daemon=True)
    worker.start()
    try:
        browser_channel = prepare_browser() if prepare else None
        scan_loop(continuous, browser_channel)
    finally:
        stop.set()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--continuous", action="store_true")
    parser.add_argument("--prepare-browser", action="store_true")
    args = parser.parse_args()
    run(args.continuous, args.prepare_browser)
