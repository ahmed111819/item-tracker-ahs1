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
        CREATE TABLE IF NOT EXISTS green_deal_products (
          page_id bigint NOT NULL REFERENCES green_deal_pages(id) ON DELETE CASCADE,
          asin text NOT NULL, title text NOT NULL DEFAULT '', price numeric,
          product_url text NOT NULL, present boolean NOT NULL DEFAULT true,
          last_seen_at timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY(page_id, asin)
        );
        """)
        conn.execute("ALTER TABLE green_deal_pages ADD COLUMN IF NOT EXISTS scan_started_at timestamptz")
        conn.execute("ALTER TABLE green_deal_pages ADD COLUMN IF NOT EXISTS title text NOT NULL DEFAULT ''")


def tg(method, payload):
    response = HTTP.post(f"{API}/{method}", json=payload, timeout=35)
    response.raise_for_status()
    data = response.json()
    if not data.get("ok"):
        raise RuntimeError(data.get("description", "Telegram API error"))
    return data["result"]


def send(text, markup=None):
    payload = {"chat_id": CHAT_ID, "text": text, "disable_web_page_preview": False}
    if THREAD_ID:
        payload["message_thread_id"] = THREAD_ID
    if markup:
        payload["reply_markup"] = markup
    tg("sendMessage", payload)



def send_product_alert(caption, product_url, image_url):
    markup = {"inline_keyboard": [[{"text": "🛒 رابط المنتج", "url": product_url}]]}
    message_text = caption + "\n👇 رابط فتح المنتج بالزر"

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
                "caption": message_text[:1000],
                "reply_markup": json.dumps(markup, ensure_ascii=False),
            }
            if THREAD_ID:
                data["message_thread_id"] = str(THREAD_ID)
            response = HTTP.post(
                f"{API}/sendPhoto",
                data=data,
                files={"photo": ("product.jpg", image_bytes, "image/jpeg")},
                timeout=45,
            )
            result = response.json()
            if not response.ok or not result.get("ok"):
                raise RuntimeError(result.get("description", "Telegram sendPhoto failed"))
            return
        except Exception as exc:
            print(f"Product photo upload failed: {type(exc).__name__}: {exc}")

    send(message_text, markup)


def valid_url(value):
    parsed = urlparse(value)
    return parsed.scheme == "https" and parsed.netloc.endswith("amazon.sa") and "/promotion/psp/" in parsed.path


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
    send(f"تمت إضافة صفحة العرض للمتابعة ✅\n{title or 'سيُقرأ اسم العرض في أول فحص'}\n{url}")


def show_pages():
    with db() as conn:
        rows = conn.execute("SELECT id,url,active,title,expires_on,last_checked_at FROM green_deal_pages ORDER BY id").fetchall()
    if not rows:
        send("ما فيه صفحات عروض مضافة حاليًا.")
        return
    lines = ["صفحات العروض المتابعة:"]
    buttons = []
    for page_id, url, active, title, expiry, checked in rows:
        status = "تعمل" if active else "متوقفة"
        end = expiry.isoformat() if expiry else "غير محدد"
        lines.append(f"{page_id}. {title or 'اسم العرض'} • {status} • النهاية: {end}\n{url}")
        buttons.append([{"text": f"إيقاف #{page_id}" if active else f"استئناف #{page_id}", "callback_data": f"page:{'pause' if active else 'resume'}:{page_id}"}])
        buttons.append([{"text": f"حذف #{page_id}", "callback_data": f"page:delete:{page_id}"}])
    send("\n\n".join(lines), {"inline_keyboard": buttons})


def command(update):
    msg = update.get("message", {})
    if str((msg.get("chat") or {}).get("id")) != CHAT_ID:
        return
    text = (msg.get("text") or "").strip()
    if text.startswith("/add"):
        add_page(text[len("/add"):].strip())
    elif text.startswith("/pages"):
        show_pages()
    elif text.startswith("/status"):
        show_status()
    elif text.startswith("/help") or text.startswith("/start"):
        send("👀 متابع صفحات العروض\n\n/add رابط | اسم العرض — إضافة صفحة\n/pages — الصفحات والأزرار\n/status — حالة الفحص\n\nمثال\n/add https://www.amazon.sa/promotion/psp/… | وفر 15٪ عند شراء 100 ريال")


def show_status():
    with db() as conn:
        rows = conn.execute("SELECT id,url,active,last_checked_at,last_error,scan_started_at FROM green_deal_pages ORDER BY id").fetchall()
        counts = conn.execute("SELECT page_id,count(*) FILTER (WHERE present),count(*) FROM green_deal_products GROUP BY page_id").fetchall()
    count_map = {row[0]: (row[1], row[2]) for row in counts}
    if not rows:
        send("ما فيه صفحات مضافة.")
        return
    lines = ["حالة فحص الصفحات:"]
    for page_id,url,active,checked,error,started in rows:
        present,total = count_map.get(page_id,(0,0))
        state = "يفحص الآن" if started else ("مفعّل" if active else "متوقف")
        last = checked.strftime("%Y-%m-%d %H:%M UTC") if checked else "لم يفحص بعد"
        issue = f"\nآخر خطأ: {error}" if error else ""
        lines.append(f"#{page_id} • {state} • المنتجات الظاهرة: {present}/{total} • آخر فحص: {last}{issue}\n{url}")
    send("\n\n".join(lines))


def callback(update):
    q = update.get("callback_query", {})
    msg = q.get("message", {})
    if str((msg.get("chat") or {}).get("id")) != CHAT_ID:
        return
    try:
        _, action, raw_id = q.get("data", "").split(":")
        page_id = int(raw_id)
    except (ValueError, TypeError):
        tg("answerCallbackQuery", {"callback_query_id": q["id"], "text": "الزر غير صالح"})
        return
    with db() as conn:
        if action in {"pause", "resume"}:
            conn.execute("UPDATE green_deal_pages SET active=%s WHERE id=%s", (action == "resume", page_id))
        elif action == "delete":
            conn.execute("DELETE FROM green_deal_pages WHERE id=%s", (page_id,))
    tg("answerCallbackQuery", {"callback_query_id": q["id"], "text": "تم"})
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
                if offer_title:
                    on_offer_title(offer_title)
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
                        price_node = card.locator(".a-price .a-offscreen, .a-price-whole").first
                        price_text = price_node.inner_text(timeout=800)
                        match = re.search(r"([0-9][0-9,]*(?:\.[0-9]{1,2})?)", price_text)
                        if match:
                            price = float(match.group(1).replace(",", ""))
                    except Exception:
                        pass
                    if price is None:
                        try:
                            card_text = card.inner_text(timeout=800)
                            match = re.search(r"(?:SAR|ر\s*ي\s*ال|ريال)\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)|([0-9][0-9,]*(?:\.[0-9]{1,2})?)\s*(?:SAR|ريال)", card_text, re.I)
                            if match:
                                price = float((match.group(1) or match.group(2)).replace(",", ""))
                        except Exception:
                            pass
                    if price is None or price <= 0:
                        continue
                    product_url = ("https://www.amazon.sa" + link) if link.startswith("/") else (link or f"https://www.amazon.sa/dp/{asin}")
                    image_url = ""
                    try:
                        image = card.locator("img").first
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

    def save_offer_title(value):
        nonlocal promotion_title, promotion
        if not promotion_title and value:
            promotion_title = value[:200]
            promotion = promotion_details(promotion_title)
            with db() as conn:
                conn.execute("UPDATE green_deal_pages SET title=%s WHERE id=%s", (promotion_title, page_id))

    def save_product(asin, title, price, product_url, image_url):
        percent = promotion['percent'] if promotion else None
        buy_quantity = promotion['buy_quantity'] if promotion else None
        discounted_quantity = promotion['discounted_quantity'] if promotion else None
        found[asin] = (title, price, product_url)
        with db() as conn:
            old = conn.execute(
                "SELECT price,present FROM green_deal_products WHERE page_id=%s AND asin=%s FOR UPDATE",
                (page_id, asin),
            ).fetchone()
            event = None
            if old is None:
                event = f"🆕 منتج جديد\n{title}\nالسعر الحالي: {price:.2f} ريال"
            elif not old[1]:
                event = f"↩️ عاد المنتج للظهور\n{title}\nالسعر الحالي: {price:.2f} ريال"
            elif old[0] is not None and float(old[0]) != price:
                event = f"💰 تغير السعر\n{title}\nالسعر السابق: {float(old[0]):.2f} ريال\nالسعر الحالي: {price:.2f} ريال"
            conn.execute("""
              INSERT INTO green_deal_products(page_id,asin,title,price,product_url,present,last_seen_at)
              VALUES (%s,%s,%s,%s,%s,true,now())
              ON CONFLICT(page_id,asin) DO UPDATE SET title=excluded.title,price=excluded.price,
                product_url=excluded.product_url,present=true,last_seen_at=now()
            """, (page_id, asin, title, price, product_url))
        if event:
            if percent is not None:
                if buy_quantity and discounted_quantity:
                    discounted_unit = round(price * (1 - percent / 100), 2)
                    bundle_total = round(price * (buy_quantity - discounted_quantity * percent / 100), 2)
                    average_unit = round(bundle_total / buy_quantity, 2)
                    event += f"\n🎁 سعر القطعة المخفّضة ({percent:g}%): {discounted_unit:.2f} ريال"
                    event += f"\n🛍️ إجمالي {buy_quantity} قطع: {bundle_total:.2f} ريال"
                    event += f"\n📊 متوسط القطعة: {average_unit:.2f} ريال"
                else:
                    discounted_unit = round(price * (1 - percent / 100), 2)
                    event += f"\n🎁 السعر التقديري بعد خصم {percent:g}%: {discounted_unit:.2f} ريال"
                if promotion_title:
                    event += f"\n📌 شرط العرض: {promotion_title}"
            send_product_alert(event, product_url, image_url)

    collect_products(url, save_product, save_offer_title)
    if not found:
        raise RuntimeError("No products could be read. Treating this scan as incomplete.")
    with db() as conn:
        conn.execute("UPDATE green_deal_products SET present=false WHERE page_id=%s AND asin <> ALL(%s)", (page_id, list(found)))
        conn.execute("UPDATE green_deal_pages SET last_checked_at=now(),last_error=NULL,scan_started_at=NULL WHERE id=%s", (page_id,))


def run(continuous):
    init_db()
    offset = 0
    while True:
        offset = handle_updates(offset)
        with db() as conn:
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


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--continuous", action="store_true")
    run(parser.parse_args().continuous)
