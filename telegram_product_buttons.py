"""Inline controls for temporary product blocks in Telegram alerts."""

from __future__ import annotations

import requests


def product_keyboard(asin: str, state: str = "open") -> dict:
    if state == "edit":
        rows = [[
            {"text": "✅ فك الحجب", "callback_data": f"pb:unblock:{asin}"},
            {"text": "↩️ رجوع", "callback_data": f"pb:back:{asin}"},
        ]]
    elif state == "blocked":
        rows = [[
            {"text": "⏸ محجوب", "callback_data": f"pb:noop:{asin}"},
            {"text": "⚙️ تعديل", "callback_data": f"pb:edit:{asin}"},
        ]]
    else:
        rows = [[
            {"text": "⏳ حجب مؤقت", "callback_data": f"pb:block:{asin}"},
        ]]
    return {"inline_keyboard": rows}


def edit_product_keyboard(token: str, chat_id: str, message_id: int, asin: str, state: str) -> bool:
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/editMessageReplyMarkup",
            json={
                "chat_id": chat_id,
                "message_id": int(message_id),
                "reply_markup": product_keyboard(asin, state),
            },
            timeout=20,
        )
        result = response.json()
        return bool(response.ok and result.get("ok"))
    except Exception:
        return False
