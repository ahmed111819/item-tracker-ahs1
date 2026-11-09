"""Poll and apply Telegram inline-button callbacks for the three coupon scanners.

GitHub Actions runs exactly one listener alongside the scanners, so the shared
Telegram bot token never has competing getUpdates consumers.
"""

from __future__ import annotations

import argparse
import os
import re
import signal
import time
from typing import Any

import requests

from postgres_storage import (
    assert_product_blocks_ready,
    clear_released_product_blocks,
    create_or_get_product_block,
    get_latest_known_product_price,
    get_product_block,
    get_telegram_update_offset,
    remove_product_block,
    recover_pending_product_blocks,
    save_telegram_update_offset,
)
from telegram_product_buttons import edit_product_keyboard

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
API = f"https://api.telegram.org/bot{TOKEN}"
ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")
STOP = False


def stop_listener(_signum: int, _frame: Any) -> None:
    global STOP
    STOP = True


def telegram_call(method: str, payload: dict[str, Any], timeout: int = 20) -> dict[str, Any]:
    try:
        response = requests.post(f"{API}/{method}", json=payload, timeout=timeout)
        result = response.json()
    except Exception as exc:
        raise RuntimeError(f"Telegram {method} request failed ({type(exc).__name__}).") from None
    if not response.ok or not result.get("ok"):
        description = str(result.get("description", "Telegram API error"))[:180]
        raise RuntimeError(f"Telegram {method} failed: {description}")
    return result


def answer(callback_id: str, text: str, show_alert: bool = False) -> None:
    try:
        telegram_call(
            "answerCallbackQuery",
            {"callback_query_id": callback_id, "text": text[:180], "show_alert": show_alert},
        )
    except Exception as exc:
        print(exc)


def edit_markup(message: dict[str, Any], asin: str, state: str) -> bool:
    chat = message.get("chat") or {}
    message_id = message.get("message_id")
    if message_id is None or chat.get("id") is None:
        return False
    ok = edit_product_keyboard(TOKEN, str(chat["id"]), int(message_id), asin, state)
    if not ok:
        print("Telegram button markup update failed for", asin)
    return ok


def notify_chat(message: dict[str, Any], text: str) -> None:
    chat = message.get("chat") or {}
    if chat.get("id") is None:
        return
    payload = {
        "chat_id": str(chat["id"]),
        "text": text,
        "disable_notification": True,
    }
    if message.get("message_id") is not None:
        payload["reply_to_message_id"] = int(message["message_id"])
    try:
        telegram_call("sendMessage", payload)
    except Exception as exc:
        print(f"Telegram confirmation message failed ({type(exc).__name__}).")


def handle_callback(callback: dict[str, Any]) -> None:
    callback_id = callback.get("id")
    message = callback.get("message") or {}
    chat = message.get("chat") or {}
    user = callback.get("from") or {}
    raw = str(callback.get("data") or "")
    parts = raw.split(":")
    if not callback_id:
        return
    if str(chat.get("id", "")) != CHAT_ID:
        answer(callback_id, "هذا الزر غير مخصص لهذه المحادثة.", True)
        return
    if len(parts) != 3 or parts[0] != "pb" or not ASIN_RE.fullmatch(parts[2]):
        answer(callback_id, "بيانات الزر غير صالحة.", True)
        return

    action, asin = parts[1], parts[2]
    if action not in {"block", "edit", "unblock", "back", "noop"}:
        answer(callback_id, "الإجراء غير صالح.", True)
        return

    if action == "noop":
        block = get_product_block(asin)
        if block and block["status"] == "active":
            answer(
                callback_id,
                f"محجوب حتى يصل السعر إلى {float(block['resume_price']):.2f} ريال أو أقل.",
            )
        else:
            answer(callback_id, "الحجب لم يعد نشطًا.", True)
        return

    # Acknowledge the tap before any database access. Loading the compressed
    # bot snapshots can take long enough for Telegram's callback spinner to time out.
    answer(callback_id, "جارٍ تنفيذ طلبك…")

    if action == "block":
        price = get_latest_known_product_price(asin)
        if price is None:
            notify_chat(message, "لم أجد سعرًا محفوظًا لهذا المنتج. جرّب بعد دورة فحص.")
            return
        block = create_or_get_product_block(
            asin=asin,
            blocked_price=price,
            updated_by=str(user.get("id", "unknown")),
            message_chat_id=str(chat["id"]),
            message_id=int(message["message_id"]),
        )
        if block["status"] == "active":
            updated = edit_markup(message, asin, "blocked")
            notify_chat(
                message,
                f"تم الحجب عند {float(block['blocked_price']):.2f} ريال. "
                f"ينفك تلقائيًا عند {float(block['resume_price']):.2f} ريال أو أقل."
                + ("" if updated else " تعذر تحديث أزرار الرسالة؛ حاول الضغط على «حجب مؤقت» مرة أخرى."),
            )
            print(f"Temporary product block created/confirmed for {asin}.")
        else:
            edit_markup(message, asin, "open" if block["status"] == "released" else "blocked")
            notify_chat(message, "يجري فك الحجب تلقائيًا بعد بلوغ حد السعر.")
        return

    block = get_product_block(asin)
    if action == "edit":
        if block is None:
            edit_markup(message, asin, "open")
            notify_chat(message, "هذا المنتج غير محجوب.")
        elif block["status"] == "active":
            edit_markup(message, asin, "edit")
            notify_chat(message, "اختر «فك الحجب» لإلغاء الحجب يدويًا.")
        else:
            edit_markup(message, asin, "blocked")
            notify_chat(message, "يجري فك الحجب تلقائيًا بعد بلوغ حد السعر.")
        return

    if action == "unblock":
        removed = remove_product_block(asin)
        edit_markup(message, asin, "open")
        notify_chat(
            message,
            "تم فك الحجب، ولن أرسل تنبيهًا قديمًا بأثر رجعي."
            if removed else "هذا المنتج غير محجوب.",
        )
        if removed:
            print(f"Temporary product block manually removed for {asin}.")
        return

    if action == "back":
        edit_markup(message, asin, "blocked" if block else "open")
        return


def main() -> None:
    global STOP
    parser = argparse.ArgumentParser()
    parser.add_argument("--cleanup-released", action="store_true")
    args = parser.parse_args()

    if not TOKEN or not CHAT_ID:
        raise RuntimeError("Telegram token/chat ID are missing.")
    assert_product_blocks_ready()

    if args.cleanup_released:
        removed = clear_released_product_blocks()
        print(f"Cleared {removed} auto-released product block(s) after scanner completion.")
        return

    recovered = recover_pending_product_blocks()
    if recovered:
        print(f"Recovered {recovered} unfinished automatic release(s).")
    signal.signal(signal.SIGTERM, stop_listener)
    signal.signal(signal.SIGINT, stop_listener)
    offset = get_telegram_update_offset()
    print("Telegram product-block callback listener started.")

    while not STOP:
        try:
            result = telegram_call(
                "getUpdates",
                {
                    "offset": offset,
                    "timeout": 20,
                    "allowed_updates": ["callback_query"],
                },
                timeout=30,
            )
            for update in result.get("result", []):
                if STOP:
                    break
                try:
                    callback = update.get("callback_query")
                    if isinstance(callback, dict):
                        handle_callback(callback)
                except Exception as exc:
                    print(f"Could not apply Telegram callback ({type(exc).__name__}).")
                offset = max(offset, int(update.get("update_id", offset)) + 1)
                save_telegram_update_offset(offset)
        except Exception as exc:
            if not STOP:
                message = str(exc).lower()
                if "webhook" in message or "conflict" in message:
                    raise
                print(f"Telegram callback polling failed ({type(exc).__name__}); retrying shortly.")
                time.sleep(5)

    print("Telegram product-block callback listener stopped.")


if __name__ == "__main__":
    main()
