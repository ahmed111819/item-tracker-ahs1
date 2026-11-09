"""Inline controls for temporary product blocks in Telegram alerts."""

from __future__ import annotations

import math

import requests


def _price_text(price: float | None) -> str | None:
    try:
        value = float(price)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    return f"{value:.2f}".rstrip("0").rstrip(".")


def product_keyboard(asin: str, state: str = "open", price: float | None = None) -> dict:
    price_value = _price_text(price)
    suffix = f":{price_value}" if price_value is not None else ""
    if state == "edit":
        rows = [[
            {"text": "✅ فك الحجب", "callback_data": f"pb:unblock:{asin}{suffix}"},
            {"text": "↩️ رجوع", "callback_data": f"pb:back:{asin}{suffix}"},
        ]]
    elif state == "blocked":
        rows = [[
            {"text": "⏸ محجوب", "callback_data": f"pb:noop:{asin}{suffix}"},
            {"text": "⚙️ تعديل", "callback_data": f"pb:edit:{asin}{suffix}"},
        ]]
    else:
        rows = [[
            {"text": "⏳ حجب مؤقت", "style": "danger", "callback_data": f"pb:block:{asin}{suffix}"},
        ]]
    return {"inline_keyboard": rows}


def edit_product_keyboard(
    token: str, chat_id: str, message_id: int, asin: str,
    state: str, price: float | None = None,
) -> bool:
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/editMessageReplyMarkup",
            json={
                "chat_id": chat_id,
                "message_id": int(message_id),
                "reply_markup": product_keyboard(asin, state, price),
            },
            timeout=20,
        )
        result = response.json()
        return bool(response.ok and result.get("ok"))
    except Exception:
        return False
