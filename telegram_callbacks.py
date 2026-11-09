"""Poll and apply Telegram inline-button callbacks for the three coupon scanners.

GitHub Actions runs exactly one listener alongside the scanners, so the shared
Telegram bot token never has competing getUpdates consumers.
"""

from __future__ import annotations

import argparse
import math
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
ACKED_CALLBACKS: set[str] = set()
NOTIFIED_FAILED_CALLBACKS: set[str] = set()


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
            timeout=5,
        )
    except Exception as exc:
        print(exc)


def edit_markup(message: dict[str, Any], asin: str, state: str, price: float | None = None) -> bool:
    chat = message.get("chat") or {}
    message_id = message.get("message_id")
    if message_id is None or chat.get("id") is None:
        return False
    ok = edit_product_keyboard(TOKEN, str(chat["id"]), int(message_id), asin, state, price)
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
    if len(parts) not in {3, 4} or parts[0] != "pb" or not ASIN_RE.fullmatch(parts[2]):
        answer(callback_id, "بيانات الزر غير صالحة.", True)
        return

    action, asin = parts[1], parts[2]
    payload_price: float | None = None
    if len(parts) == 4:
        try:
            payload_price = float(parts[3])
        except ValueError:
            answer(callback_id, "السعر المرفق بالزر غير صالح.", True)
            return
        if not math.isfinite(payload_price) or payload_price <= 0:
            answer(callback_id, "السعر المرفق بالزر غير صالح.", True)
            return

    if action not in {"block", "edit", "unblock", "back", "noop"}:
        answer(callback_id, "الإجراء غير صالح.", True)
        return

    if action == "noop":
        answer(callback_id, "المنتج محجوب بالفعل.")
        return

    # Close Telegram's spinner once, before database or network work. A failed
    # DB attempt is retried with the same update ID, so don't re-answer it.
    if callback_id not in ACKED_CALLBACKS:
        answer(callback_id, "جارٍ تنفيذ طلبك…")
        ACKED_CALLBACKS.add(callback_id)

    if action == "block":
        # New alerts carry their observed price in the callback data, avoiding
        # decompression of all three large state snapshots on every tap.
        price = payload_price
        if price is None:
            # Backward compatibility for buttons on older Telegram messages.
            price = get_latest_known_product_price(asin)
        if price is None:
            notify_chat(message, "ما عندي سعر محفوظ لهذا التنبيه القديم. التنبيهات الجديدة تسجل السعر داخل الزر.")
            return
        block = create_or_get_product_block(
            asin=asin,
            blocked_price=price,
            updated_by=str(user.get("id", "unknown")),
            message_chat_id=str(chat["id"]),
            message_id=int(message["message_id"]),
        )
        active_price = float(block["blocked_price"])
        if block["status"] == "active":
            updated = edit_markup(message, asin, "blocked", active_price)
            notify_chat(
                message,
                f"تم الحجب عند {active_price:.2f} ريال. "
                f"ينفك تلقائيًا عند {float(block['resume_price']):.2f} ريال أو أقل."
                + ("" if updated else " تعذر تحديث أزرار الرسالة؛ الحجب محفوظ ويمكن المحاولة مجددًا."),
            )
            print(f"Temporary product block created/confirmed for {asin}.")
        else:
            edit_markup(message, asin, "open", active_price)
            notify_chat(message, "الحجب السابق انتهى تلقائيًا.")
        return

    block = get_product_block(asin)
    remembered_price = (
        float(block["blocked_price"])
        if block and block.get("blocked_price") is not None
        else payload_price
    )
    if action == "edit":
        if block is None:
            edit_markup(message, asin, "open", remembered_price)
            notify_chat(message, "هذا المنتج غير محجوب.")
        elif block["status"] == "active":
            edit_markup(message, asin, "edit", remembered_price)
            notify_chat(message, "اختر «فك الحجب» لإلغاء الحجب يدويًا.")
        else:
            edit_markup(message, asin, "blocked", remembered_price)
            notify_chat(message, "يجري فك الحجب تلقائيًا بعد بلوغ حد السعر.")
        return

    if action == "unblock":
        removed = remove_product_block(asin)
        edit_markup(message, asin, "open", remembered_price)
        notify_chat(
            message,
            "تم فك الحجب، ولن أرسل تنبيهًا قديمًا بأثر رجعي."
            if removed else "هذا المنتج غير محجوب.",
        )
        if removed:
            print(f"Temporary product block manually removed for {asin}.")
        return

    if action == "back":
        edit_markup(message, asin, "blocked" if block else "open", remembered_price)
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
                    next_offset = max(offset, int(update.get("update_id", offset)) + 1)
                    # Commit the offset only after the callback action succeeds.
                    # If PostgreSQL is temporarily unavailable, Telegram will
                    # redeliver this update and the idempotent DB action retries.
                    save_telegram_update_offset(next_offset)
                    offset = next_offset
                    if isinstance(callback, dict):
                        callback_id = str(callback.get("id") or "")
                        ACKED_CALLBACKS.discard(callback_id)
                        NOTIFIED_FAILED_CALLBACKS.discard(callback_id)
                except Exception as exc:
                    print(f"Could not apply Telegram callback/update ({type(exc).__name__}); keeping it queued for retry.")
                    callback = update.get("callback_query")
                    if isinstance(callback, dict):
                        callback_id = str(callback.get("id") or "")
                        if callback_id and callback_id not in NOTIFIED_FAILED_CALLBACKS:
                            notify_chat(
                                callback.get("message") or {},
                                "تعذر الاتصال بقاعدة البيانات مؤقتًا؛ سأعيد محاولة الحجب تلقائيًا.",
                            )
                            NOTIFIED_FAILED_CALLBACKS.add(callback_id)
                    time.sleep(3)
                    break
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
