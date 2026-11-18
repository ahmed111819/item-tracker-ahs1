"""Green Amazon promotion page tracker.

The tracker intentionally uses dedicated GREEN_DEALS_* environment variables and
an independent Telegram bot token. It stores observed items in PostgreSQL.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import time
from html import escape
from io import BytesIO
from zoneinfo import ZoneInfo
from datetime import datetime, timezone
from urllib.parse import urlparse

import psycopg
import requests
from playwright.sync_api import sync_playwright

TOKEN = os.environ["GREEN_DEALS_TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["GREEN_DEALS_TELEGRAM_CHAT_ID"]
THREAD_ID = int(os.getenv("GREEN_DEALS_TELEGRAM_MESSAGE_THREAD_ID", "0"))
DB_URL = os.environ["GREEN_DEALS_DATABASE_URL"]
API = f"https://api.telegram.org/bot{TOKEN}"
WAIT_SECONDS = int(os.getenv("GREEN_DEALS_SCAN_INTERVAL_SECONDS", "300"))
HTTP = requests.Session()
SCAN_REQUESTED = False


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
    response = HTTP.post(f"{API}/{method}", json=payload, timeout=35)
    response.raise_for_status()
    data = response.json()
    if not data.get("ok"):
        raise RuntimeError(data.get("description", "Telegram API error"))
    return data["result"]


def send(text, markup=None, *, parse_mode=None, disable_preview=False):
    payload = {"chat_id": CHAT_ID, "text": text, "disable_web_page_preview": disable_preview}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if THREAD_ID:
        payload["message_thread_id"] = THREAD_ID
    if markup:
        payload["reply_markup"] = markup
    tg("sendMessage", payload)



def send_product_alert(caption, product_url, image_url, asin, price):
    markup = {"inline_keyboard": [
        [
            {"text": "⭐ المفضلة", "callback_data": f"product:favorite:{asin}"},
            {"text": "🚫 حجب مؤقت", "callback_data": f"product:block:{asin}:{price:.2f}"},
        ],
        [{"text": "🔕 إيقاف تنبيه المنتج", "callback_data": f"product:mute:{asin}"}],
    ]}
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
    global SCAN_REQUESTED
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
    SCAN_REQUESTED = True
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
    global SCAN_REQUESTED
    selection = re.fullmatch(r"menu:([a-z]+):([0-9]{1,6})", data)
    manage = re.fullmatch(r"manage:([a-z]+):([0-9]{1,6}):([A-Z0-9]{10})", data)
    page_action = re.fullmatch(r"page:(details|confirm|stop|enable|remove):([0-9]{1,18}):([0-9]{1,6})", data)
    answer = {"callback_query_id": query["id"]}
    if selection:
        view, raw_page = selection.groups()
        page = int(raw_page)
        tg("answerCallbackQuery", answer)
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
        tg("answerCallbackQuery", {**answer, "text": "تم التحديث"})
        show_product_menu(view, int(raw_page), message)
    elif page_action:
        action, raw_page_id, raw_page = page_action.groups()
        page_id, page = int(raw_page_id), int(raw_page)
        if action in {"stop", "enable", "remove"}:
            with db() as conn:
                if action == "remove":
                    conn.execute("DELETE FROM green_deal_pages WHERE id=%s", (page_id,))
                else:
                    conn.execute("UPDATE green_deal_pages SET active=%s WHERE id=%s", (action == "enable", page_id))
            if action == "enable":
                SCAN_REQUESTED = True
        tg("answerCallbackQuery", answer)
        if action == "remove":
            show_pages("pages", page, message)
        else:
            show_page_details(page_id, page, message, confirm=action == "confirm")
    else:
        return False
    return True


def callback(update):
    global SCAN_REQUESTED
    query = update.get("callback_query", {})
    message = query.get("message", {})
    if str((message.get("chat") or {}).get("id")) != CHAT_ID:
        return
    data = str(query.get("data") or "")
    if menu_callback(query, message, data):
        return
    parts = data.split(":")
    if len(parts) >= 3 and parts[0] == "product":
        asin = parts[2]
        if not re.fullmatch(r"[A-Z0-9]{10}", asin):
            tg("answerCallbackQuery", {"callback_query_id": query["id"], "text": "بيانات المنتج غير صالحة", "show_alert": True})
            return
        action = parts[1]
        if action == "block" and len(parts) == 4:
            try:
                blocked_price = float(parts[3])
                if not 0 < blocked_price < 10000000:
                    raise ValueError
            except ValueError:
                tg("answerCallbackQuery", {"callback_query_id": query["id"], "text": "السعر غير صالح", "show_alert": True})
                return
            resume_price = round(blocked_price * 0.95, 2)
            with db() as conn:
                conn.execute(
                    "INSERT INTO green_deal_product_blocks(asin,blocked_price,resume_price) VALUES (%s,%s,%s) "
                    "ON CONFLICT(asin) DO UPDATE SET blocked_price=excluded.blocked_price,resume_price=excluded.resume_price,created_at=now()",
                    (asin, blocked_price, resume_price),
                )
            answer_text = f"تم الحجب حتى يصل السعر إلى {resume_price:.2f} ريال"
        elif action in {"favorite", "mute"}:
            column = "favorite" if action == "favorite" else "muted"
            with db() as conn:
                conn.execute("INSERT INTO green_deal_product_preferences(asin) VALUES (%s) ON CONFLICT DO NOTHING", (asin,))
                row = conn.execute(f"UPDATE green_deal_product_preferences SET {column}=NOT {column},updated_at=now() WHERE asin=%s RETURNING {column}", (asin,)).fetchone()
            state = bool(row[0]) if row else False
            answer_text = "أضيف للمفضلة ⭐" if action == "favorite" and state else "أزيل من المفضلة" if action == "favorite" else "تم إيقاف تنبيه المنتج 🔕" if state else "تمت إعادة تنبيهات المنتج 🔔"
        else:
            tg("answerCallbackQuery", {"callback_query_id": query["id"], "text": "الإجراء غير معروف", "show_alert": True})
            return
        tg("answerCallbackQuery", {"callback_query_id": query["id"], "text": answer_text})
        return

    try:
        _, action, raw_id = data.split(":")
        page_id = int(raw_id)
    except (ValueError, TypeError):
        tg("answerCallbackQuery", {"callback_query_id": query["id"], "text": "الزر غير صالح"})
        return
    if action == "show":
        with db() as conn:
            row = conn.execute("SELECT title FROM green_deal_pages WHERE id=%s", (page_id,)).fetchone()
        tg("answerCallbackQuery", {"callback_query_id": query["id"], "text": row[0] if row else "الصفحة غير موجودة"})
        return
    with db() as conn:
        if action in {"pause", "resume"}:
            conn.execute("UPDATE green_deal_pages SET active=%s WHERE id=%s", (action == "resume", page_id))
            SCAN_REQUESTED = action == "resume"
        elif action == "delete":
            conn.execute("DELETE FROM green_deal_pages WHERE id=%s", (page_id,))
    tg("answerCallbackQuery", {"callback_query_id": query["id"], "text": "تم"})
    show_pages()


def handle_updates(offset):
    result = tg("getUpdates", {"offset": offset, "timeout": 25, "allowed_updates": ["message", "callback_query"]})
    for item in result:
        if "message" in item:
            command(item)
        elif "callback_query" in item:
            callback(item)
        offset = item["update_id"] + 1
    return offset



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


def collect_products(url, on_product, on_offer_title):
    products = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(locale="en-US", timezone_id="Asia/Riyadh")
        try:
            response = page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(3500)
            print(f"Opened promotion page: status={response.status if response else 'unknown'} title={page.title()!r}")
            try:
                offer_title = page.locator("h1").first.inner_text(timeout=2000).strip()
                page_text = page.locator("body").inner_text(timeout=3000)
                on_offer_title(offer_title, page_text)
            except Exception:
                pass
            for batch in range(100):
                cards = page.locator("[data-asin]").all()
                print(f"Promotion batch {batch + 1}: {len(cards)} ASIN elements")
                for card in cards:
                    asin = (card.get_attribute("data-asin") or "").strip()
                    if not re.fullmatch(r"[A-Z0-9]{10}", asin):
                        continue
                    title = ""
                    link = ""
                    price = None
                    try:
                        title_node = card.locator("h2, [data-cy='title-recipe-title'], .a-size-base-plus").first
                        title = title_node.inner_text(timeout=800).strip()
                    except Exception:
                        pass
                    try:
                        link = card.locator("a[href*='/dp/'], a[href*='/gp/product/']").first.get_attribute("href") or ""
                    except Exception:
                        pass
                    try:
                        price_node = card.locator(".a-price").first
                        offscreen = price_node.locator(".a-offscreen").first
                        if offscreen.count():
                            price = parse_amazon_price(offscreen.inner_text(timeout=800))
                        if price is None:
                            whole_node = price_node.locator(".a-price-whole").first
                            whole_text = whole_node.inner_text(timeout=800) if whole_node.count() else ""
                            fraction_node = price_node.locator(".a-price-fraction").first
                            fraction_text = fraction_node.inner_text(timeout=800) if fraction_node.count() else ""
                            whole = re.sub(r"\D", "", whole_text.translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")))
                            fraction = re.sub(r"\D", "", fraction_text.translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")))
                            if whole:
                                price = parse_amazon_price(f"{whole}.{fraction[:2]}" if fraction else whole)
                    except Exception:
                        pass
                    if price is None:
                        try:
                            card_text = card.inner_text(timeout=800).translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789"))
                            match = re.search(r"(?:SAR|ر\s*ي\s*ا\s*ل|ريال)\s*([0-9][0-9,]*(?:[.٫][0-9]{1,2})?)|([0-9][0-9,]*(?:[.٫][0-9]{1,2})?)\s*(?:SAR|ريال)", card_text, re.I)
                            if match:
                                price = parse_amazon_price(match.group(1) or match.group(2))
                        except Exception:
                            pass
                    if price is None or price <= 0:
                        continue
                    product_url = ("https://www.amazon.sa" + link) if link.startswith("/") else (link or f"https://www.amazon.sa/dp/{asin}")
                    image_url = ""
                    try:
                        image_locator = card.locator("img[data-a-hires], img[data-a-dynamic-image], img[data-src]")
                        image = image_locator.first if image_locator.count() else card.locator("img").first
                        image_candidates = []
                        for attribute in ("data-a-hires", "data-src", "src"):
                            candidate = image.get_attribute(attribute) or ""
                            if candidate.startswith("https://"):
                                candidate_host = (urlparse(candidate).hostname or "").lower()
                                if candidate_host.endswith(("media-amazon.com", "images-amazon.com", "ssl-images-amazon.com")):
                                    image_candidates.append(candidate)
                        dynamic_image = image.get_attribute("data-a-dynamic-image") or ""
                        if dynamic_image:
                            dimensions = json.loads(dynamic_image)
                            image_candidates.extend(
                                candidate for candidate in dimensions
                                if candidate.startswith("https://")
                                and (urlparse(candidate).hostname or "").lower().endswith(
                                    ("media-amazon.com", "images-amazon.com", "ssl-images-amazon.com")
                                )
                            )
                        image_url = image_candidates[0] if image_candidates else ""
                    except Exception:
                        pass
                    product = (title[:300] or asin, price, product_url, image_url)
                    if products.get(asin) != product:
                        products[asin] = product
                        on_product(asin, *product)

                more = page.get_by_text(re.compile(r"^\s*(إظهار المزيد|Show more|Load more)\s*$", re.I)).last
                if more.count() == 0:
                    print("No visible 'show more' control was found.")
                    break
                try:
                    if not more.is_visible() or not more.is_enabled():
                        print("'Show more' control is hidden or disabled.")
                        break
                    before_count = len(page.locator("[data-asin]").all())
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


def scan_page(page_id, url):
    found = {}
    with db() as conn:
        page_row = conn.execute("SELECT title FROM green_deal_pages WHERE id=%s", (page_id,)).fetchone()
    promotion_title = page_row[0] if page_row else ""
    promotion = promotion_details(promotion_title)

    def save_offer_title(value, page_text):
        nonlocal promotion_title, promotion
        expiry = parse_expiry_date(page_text)
        if not promotion_title and value:
            promotion_title = value[:200]
            promotion = promotion_details(promotion_title)
        with db() as conn:
            conn.execute(
                "UPDATE green_deal_pages SET title=CASE WHEN title='' THEN %s ELSE title END,"
                "expires_on=COALESCE(%s,expires_on) WHERE id=%s",
                (promotion_title, expiry, page_id),
            )

    def save_product(asin, title, price, product_url, image_url):
        found[asin] = (title, price, product_url)
        with db() as conn:
            old = conn.execute(
                "SELECT price,present,pending_alert,pending_url,pending_image_url "
                "FROM green_deal_products WHERE page_id=%s AND asin=%s FOR UPDATE",
                (page_id, asin),
            ).fetchone()
            preference = conn.execute("SELECT muted FROM green_deal_product_preferences WHERE asin=%s", (asin,)).fetchone()
            is_muted = bool(preference[0]) if preference else False
            event = None
            block = conn.execute("SELECT resume_price FROM green_deal_product_blocks WHERE asin=%s", (asin,)).fetchone()
            if block and price > float(block[0]):
                event = None
            elif block:
                conn.execute("DELETE FROM green_deal_product_blocks WHERE asin=%s", (asin,))
                event = f"🔓 انفك حجب المنتج\n\n🏷️ {title}\n\n💰 السعر الحالي: {price:.2f} ريال"
            elif old is None:
                event = f"🆕 منتج جديد\n\n🏷️ {title}\n\n💰 السعر الحالي: {price:.2f} ريال"
            elif not old[1]:
                event = f"↩️ عاد المنتج للظهور\n\n🏷️ {title}\n\n💰 السعر الحالي: {price:.2f} ريال"
            elif old[0] is not None and price < float(old[0]):
                event = f"📉 انخفاض سعر المنتج\n\n🏷️ {title}\n\n📉 السعر السابق: {float(old[0]):.2f} ريال\n💵 السعر الحالي: {price:.2f} ريال"

            pending_alert = old[2] if old else None
            was_pending = bool(pending_alert and not is_muted)
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
            with db() as conn:
                conn.execute(
                    "UPDATE green_deal_products SET pending_alert=NULL,pending_url=NULL,pending_image_url=NULL "
                    "WHERE page_id=%s AND asin=%s",
                    (page_id, asin),
                )

    collect_products(url, save_product, save_offer_title)
    if not found:
        raise RuntimeError("No products could be read. Treating this scan as incomplete.")
    with db() as conn:
        conn.execute("UPDATE green_deal_products SET present=false WHERE page_id=%s AND asin <> ALL(%s)", (page_id, list(found)))
        conn.execute("UPDATE green_deal_pages SET last_checked_at=now(),last_error=NULL,scan_started_at=NULL WHERE id=%s", (page_id,))


def run(continuous):
    global SCAN_REQUESTED
    init_db()
    offset = 0
    while True:
        offset = handle_updates(offset)
        SCAN_REQUESTED = False
        today = datetime.now(ZoneInfo("Asia/Riyadh")).date()
        with db() as conn:
            conn.execute("UPDATE green_deal_pages SET active=false WHERE active=true AND expires_on < %s", (today,))
            pages = conn.execute("SELECT id,url FROM green_deal_pages WHERE active=true ORDER BY id").fetchall()
        for page_id, url in pages:
            completed = False
            for attempt in range(1, 5):
                try:
                    with db() as conn:
                        conn.execute("UPDATE green_deal_pages SET scan_started_at=now(),last_error=NULL WHERE id=%s", (page_id,))
                    print(f"Starting page {page_id}, attempt {attempt}/4")
                    scan_page(page_id, url)
                    completed = True
                    break
                except Exception as exc:
                    detail = f"{type(exc).__name__}: {str(exc)[:300]}"
                    print(f"Page {page_id}, attempt {attempt}/4 failed: {detail}")
                    with db() as conn:
                        conn.execute("UPDATE green_deal_pages SET last_error=%s WHERE id=%s", (detail, page_id))
                    if attempt < 4:
                        time.sleep((10, 20, 40)[attempt - 1])
            if not completed:
                with db() as conn:
                    conn.execute("UPDATE green_deal_pages SET last_checked_at=now(),scan_started_at=NULL WHERE id=%s", (page_id,))
            offset = handle_updates(offset)
        if not continuous:
            break
        deadline = time.monotonic() + WAIT_SECONDS
        while time.monotonic() < deadline:
            offset = handle_updates(offset)
            if SCAN_REQUESTED:
                break


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--continuous", action="store_true")
    run(parser.parse_args().continuous)
