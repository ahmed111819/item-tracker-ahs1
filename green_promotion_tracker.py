"""Green Amazon promotion page tracker.

The tracker intentionally uses dedicated GREEN_DEALS_* environment variables and
an independent Telegram bot token. It stores observed items in PostgreSQL.
"""
from __future__ import annotations

import argparse
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
          active boolean NOT NULL DEFAULT true, expires_on date,
          added_at timestamptz NOT NULL DEFAULT now(),
          last_checked_at timestamptz, last_error text
        );
        CREATE TABLE IF NOT EXISTS green_deal_products (
          page_id bigint NOT NULL REFERENCES green_deal_pages(id) ON DELETE CASCADE,
          asin text NOT NULL, title text NOT NULL DEFAULT '', price numeric,
          product_url text NOT NULL, present boolean NOT NULL DEFAULT true,
          last_seen_at timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY(page_id, asin)
        );
        """)


def tg(method, payload):
    response = HTTP.post(f"{API}/{method}", json=payload, timeout=35)
    response.raise_for_status()
    data = response.json()
    if not data.get("ok"):
        raise RuntimeError(data.get("description", "Telegram API error"))
    return data["result"]


def send(text, markup=None):
    payload = {"chat_id": CHAT_ID, "text": text, "disable_web_page_preview": False}
    if markup:
        payload["reply_markup"] = markup
    tg("sendMessage", payload)


def valid_url(value):
    parsed = urlparse(value)
    return parsed.scheme == "https" and parsed.netloc.endswith("amazon.sa") and "/promotion/psp/" in parsed.path


def add_page(url):
    if not valid_url(url):
        send("أرسل رابط صفحة عرض صالحة من Amazon.sa.")
        return
    with db() as conn:
        conn.execute("INSERT INTO green_deal_pages(url) VALUES (%s) ON CONFLICT(url) DO UPDATE SET active=true", (url,))
    send("تمت إضافة صفحة العرض للمتابعة ✅\n" + url)


def show_pages():
    with db() as conn:
        rows = conn.execute("SELECT id,url,active,expires_on,last_checked_at FROM green_deal_pages ORDER BY id").fetchall()
    if not rows:
        send("ما فيه صفحات عروض مضافة حاليًا.")
        return
    lines = ["صفحات العروض المتابعة:"]
    buttons = []
    for page_id, url, active, expiry, checked in rows:
        status = "تعمل" if active else "متوقفة"
        end = expiry.isoformat() if expiry else "غير محدد"
        lines.append(f"#{page_id} • {status} • النهاية: {end}\n{url}")
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
    elif text.startswith("/help") or text.startswith("/start"):
        send("أوامر متابع صفحات العروض\n/add رابط الصفحة — إضافة صفحة\n/pages — عرض الصفحات وإدارتها")


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


def collect_products(url):
    products = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(locale="en-US", timezone_id="Asia/Riyadh")
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2500)
        for _ in range(100):
            cards = page.locator("div[data-asin]").all()
            for card in cards:
                asin = (card.get_attribute("data-asin") or "").strip()
                if not re.fullmatch(r"[A-Z0-9]{10}", asin):
                    continue
                title = ""
                link = ""
                price = None
                try:
                    title = card.locator("h2").first.inner_text(timeout=500)
                    link = card.locator("h2 a").first.get_attribute("href") or ""
                    price_text = card.locator(".a-price .a-offscreen").first.inner_text(timeout=500)
                    match = re.search(r"([\d,]+(?:\.\d{1,2})?)", price_text)
                    if match:
                        price = float(match.group(1).replace(",", ""))
                except Exception:
                    pass
                if price is not None:
                    products[asin] = (title[:300], price, "https://www.amazon.sa" + link if link.startswith("/") else link)
            more = page.get_by_role("button", name=re.compile("إظهار المزيد|show more", re.I))
            if not more.count() or not more.first.is_enabled():
                break
            before = len(products)
            more.first.click(timeout=4000)
            page.wait_for_timeout(1800)
            if len(products) == before and not more.first.is_visible():
                break
        browser.close()
    return products


def scan_page(page_id, url):
    found = collect_products(url)
    if not found:
        raise RuntimeError("No products could be read. Treating this scan as incomplete.")
    with db() as conn:
        for asin, (title, price, product_url) in found.items():
            old = conn.execute(
                "SELECT price,present FROM green_deal_products WHERE page_id=%s AND asin=%s FOR UPDATE",
                (page_id, asin),
            ).fetchone()
            event = None
            if old is None:
                event = f"🆕 منتج جديد\n{title}\nالسعر: {price:.2f} ريال"
            elif not old[1]:
                event = f"↩️ عاد المنتج للظهور\n{title}\nالسعر الحالي: {price:.2f} ريال"
            elif old[0] is not None and float(old[0]) != price:
                event = f"💰 تغير السعر\n{title}\nالسابق: {float(old[0]):.2f} ريال\nالحالي: {price:.2f} ريال"
            conn.execute("""
              INSERT INTO green_deal_products(page_id,asin,title,price,product_url,present,last_seen_at)
              VALUES (%s,%s,%s,%s,%s,true,now())
              ON CONFLICT(page_id,asin) DO UPDATE SET title=excluded.title,price=excluded.price,
                product_url=excluded.product_url,present=true,last_seen_at=now()
            """, (page_id, asin, title, price, product_url))
            if event:
                send(f"{event}\n{product_url}")
        conn.execute("UPDATE green_deal_products SET present=false WHERE page_id=%s AND asin <> ALL(%s)", (page_id, list(found)))
        conn.execute("UPDATE green_deal_pages SET last_checked_at=now(),last_error=NULL WHERE id=%s", (page_id,))


def run(continuous):
    init_db()
    offset = 0
    while True:
        offset = handle_updates(offset)
        with db() as conn:
            pages = conn.execute("SELECT id,url FROM green_deal_pages WHERE active=true ORDER BY id").fetchall()
        for page_id, url in pages:
            try:
                scan_page(page_id, url)
            except Exception as exc:
                with db() as conn:
                    conn.execute("UPDATE green_deal_pages SET last_checked_at=now(),last_error=%s WHERE id=%s", (type(exc).__name__, page_id))
                print(f"Page {page_id} scan failed: {type(exc).__name__}")
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
