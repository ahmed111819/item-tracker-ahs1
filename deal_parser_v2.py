"""Conservative classifier for Amazon.sa search-result deal cards.

Price markdowns are computed only from a structured struck-through/list price.
Promotion percentages are kept separate from product price markdowns and
quantity-conditioned promotions are never treated as an unconditional coupon.
"""

from __future__ import annotations

import html
import re
import unicodedata
from typing import Any


_DIGIT_TRANSLATION = str.maketrans(
    "٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹٪",
    "01234567890123456789%",
)
_BIDI_AND_ZERO_WIDTH = dict.fromkeys(
    map(ord, "\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069\u200b\ufeff"),
    None,
)


def normalize_text(value: str | None) -> str:
    """Normalize HTML text, Arabic numerals/diacritics, bidi marks, and spaces."""
    if not value:
        return ""
    value = html.unescape(value).translate(_DIGIT_TRANSLATION).translate(_BIDI_AND_ZERO_WIDTH)
    value = unicodedata.normalize("NFC", value)
    value = "".join(char for char in value if not unicodedata.category(char).startswith("M"))
    value = value.replace("٫", ".").replace("٬", ",")
    value = re.sub(r"\s+", " ", value).strip()
    return value


def parse_price(value: str | float | int | None) -> float | None:
    """Parse a SAR amount without accidentally reading a nearby percentage."""
    if value is None:
        return None
    if isinstance(value, (float, int)):
        return float(value) if value > 0 else None
    normalized = normalize_text(value)
    match = re.search(r"(?<!\d)(\d[\d,]*(?:\.\d{1,2})?)(?!\d)", normalized)
    if not match:
        return None
    try:
        result = float(match.group(1).replace(",", ""))
    except ValueError:
        return None
    return result if result > 0 else None


def extract_reference_price(
    card_text: str | None,
    current_price: float | None,
    explicit_reference: str | float | int | None = None,
) -> float | None:
    """Read a struck/list price, never substituting the current price."""
    reference = parse_price(explicit_reference)
    if reference is None:
        text = normalize_text(card_text)
        patterns = (
            r"\bWas\s*:?\s*(?:SAR\s*)?(\d[\d,]*(?:\.\d{1,2})?)",
            r"\bList\s+Price\s*:?\s*(?:SAR\s*)?(\d[\d,]*(?:\.\d{1,2})?)",
            r"(?:سعر\s*القائم(?:ة|ه)|السعر\s*(?:الأصلي|الاصلي))\s*:?\s*(?:SAR|ريال)?\s*(\d[\d,]*(?:\.\d{1,2})?)",
        )
        for pattern in patterns:
            match = re.search(pattern, text, re.I)
            if match:
                reference = parse_price(match.group(1))
                if reference is not None:
                    break
    if reference is not None and current_price is not None and reference > current_price:
        return reference
    return None


_PERCENT = r"(?P<percent>\d{1,3}(?:\.\d+)?)\s*%"
_SECOND_ITEM_PATTERNS = (
    re.compile(_PERCENT + r"\s*(?:off|discount)\s+(?:on|for)\s+(?:the\s+)?(?:second|2nd)\s+(?:item|product|unit)\b", re.I),
    re.compile(_PERCENT + r"\s*(?:off|discount)\s+(?:the\s+)?(?:second|2nd)\s+(?:item|product|unit)\b", re.I),
    re.compile(_PERCENT + r"\s+(?:on|for)\s+(?:the\s+)?(?:second|2nd)\s+(?:item|product|unit)\b", re.I),
    re.compile(r"(?:خصم\s*)?" + _PERCENT + r"\s*(?:خصم\s*)?(?:على|لـ|لل)\s*(?:القطعة|القطعه|المنتج|الوحدة|الوحده)\s*(?:الثانية|الثانيه|الثاني)", re.I),
    re.compile(r"(?:على|لـ|لل)\s*(?:القطعة|القطعه|المنتج|الوحدة|الوحده)\s*(?:الثانية|الثانيه|الثاني)\s*(?:خصم\s*)?" + _PERCENT, re.I),
)

_QUANTITY_PERCENT_PATTERNS = (
    re.compile(r"\b(?:save|discount)\s+" + _PERCENT + r"\s*(?:off\s*)?(?:on\s+any\s+(?:of\s+)?\d+(?:\s+or\s+more)?|on\s+\d+\s+or\s+more|when\s+you\s+buy\s+\d+|buy\s+\d+\s+or\s+more)", re.I),
    re.compile(_PERCENT + r"\s*(?:off\s*)?(?:on\s+any\s+(?:of\s+)?\d+(?:\s+or\s+more)?|on\s+\d+\s+or\s+more|when\s+you\s+buy\s+\d+|buy\s+\d+\s+or\s+more)", re.I),
    re.compile(r"(?:وفر|خصم|توفير)\s*" + _PERCENT + r"\s*(?:على\s*(?:أي|اي)\s*(?:من\s*)?\d+\s*(?:أو|او)\s*(?:أكثر|اكثر)|عند\s*شراء\s*\d+)", re.I),
    re.compile(_PERCENT + r"\s*(?:على\s*(?:أي|اي)\s*(?:من\s*)?\d+\s*(?:أو|او)\s*(?:أكثر|اكثر)|عند\s*شراء\s*\d+)", re.I),
)
_COUPON_MARKER = re.compile(r"\b(?:coupon|voucher|clip\s+coupon)\b|كوبون|قسيمة|القسيمة|قسيمه|القسيمه", re.I)
_SECOND_MARKER = re.compile(
    r"\b(?:second|2nd)\s+(?:item|product|unit)\b|(?:القطعة|القطعه|المنتج|الوحدة|الوحده)\s*(?:الثانية|الثانيه|الثاني)", re.I
)
_BUNDLE_PATTERNS = (
    re.compile(r"\bbuy\s+(?P<quantity>\d+)\s+for\s+(?:SAR\s*)?(?P<price>\d[\d,]*(?:\.\d{1,2})?)", re.I),
    re.compile(r"\b(?P<quantity>\d+)\s+for\s+SAR\s*(?P<price>\d[\d,]*(?:\.\d{1,2})?)", re.I),
    re.compile(r"اشتر\s*(?P<quantity>\d+)\s*(?:مقابل|بسعر)\s*(?P<price>\d[\d,]*(?:\.\d{1,2})?)\s*(?:ريال)?", re.I),
)


def _percent(match: re.Match[str]) -> float | None:
    try:
        number = float(match.group("percent"))
    except (ValueError, IndexError):
        return None
    return number if 0 < number <= 100 else None


def _price_markdown(current_price: float | None, reference_price: float | None) -> dict[str, Any] | None:
    if current_price is None or reference_price is None or reference_price <= current_price:
        return None
    saving = reference_price - current_price
    return {
        "type": "price_drop",
        "description": "Price reduced from the struck-through/list price",
        "reference_price": round(reference_price, 2),
        "current_price": round(current_price, 2),
        "saving": round(saving, 2),
        "discount_percent": round(saving / reference_price * 100, 2),
        "source": "structured struck-through/list price",
    }


def classify_deals(
    *,
    current_price: float | None,
    reference_price: float | None,
    card_text: str | None,
) -> dict[str, dict[str, Any] | None]:
    """Return separately classified price drop, coupon, and quantity promotion.

    A percentage in promotional copy is never used to infer a product price
    reduction. When the text is ambiguous, no coupon/promotion is emitted.
    """
    text = normalize_text(card_text)
    output: dict[str, dict[str, Any] | None] = {
        "price_drop": _price_markdown(current_price, reference_price),
        "coupon": None,
        "promotion": None,
    }
    if not text:
        return output

    # A second-item offer is a basket promotion. Its stated rate and its
    # approximate average basket rate are both retained to avoid confusion.
    for pattern in _SECOND_ITEM_PATTERNS:
        match = pattern.search(text)
        if match:
            percent = _percent(match)
            if percent is None:
                continue
            promotion = {
                "type": "second_item_promotion",
                "description": match.group(0).strip(),
                "discount_percent": round(percent, 2),
                "stated_discount_percent": round(percent, 2),
                "quantity": 2,
                "effective_basket_discount_percent": round(percent / 2, 2),
                "pay_price": round(current_price * (2 - percent / 100), 2) if current_price else None,
                "saving": round(current_price * percent / 100, 2) if current_price else None,
            }
            output["promotion"] = promotion
            return output

    # E.g. "Save 5% on any 5 or more" / Arabic equivalent. Require the
    # percentage and the quantity condition in the same phrase.
    for pattern in _QUANTITY_PERCENT_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        percent = _percent(match)
        if percent is None:
            continue
        output["promotion"] = {
            "type": "quantity_promotion",
            "description": match.group(0).strip(),
            "discount_percent": round(percent, 2),
            "stated_discount_percent": round(percent, 2),
            "quantity_condition": True,
            "quantity": 1,
            "pay_price": None,
            "saving": None,
        }
        return output

    # Fixed-price multi-item bundles are calculated from the displayed unit
    # price, and never from the Amazon percentage text.
    if current_price is not None:
        for pattern in _BUNDLE_PATTERNS:
            match = pattern.search(text)
            if not match:
                continue
            quantity = int(match.group("quantity"))
            bundle_price = parse_price(match.group("price"))
            regular_total = current_price * quantity
            if quantity < 2 or bundle_price is None or bundle_price >= regular_total:
                continue
            saving = regular_total - bundle_price
            percent = saving / regular_total * 100
            output["promotion"] = {
                "type": "bundle_offer",
                "description": match.group(0).strip(),
                "discount_percent": round(percent, 2),
                "stated_discount_percent": round(percent, 2),
                "quantity": quantity,
                "pay_price": round(bundle_price, 2),
                "saving": round(saving, 2),
            }
            return output

    # Explicit coupon copy only. This intentionally does not interpret any
    # generic percentage elsewhere on the product card as a coupon.
    coupon = _COUPON_MARKER.search(text)
    if coupon and not _SECOND_MARKER.search(text):
        around = text[max(0, coupon.start() - 40): min(len(text), coupon.end() + 40)]
        pay_match = re.search(
            r"(?:you\s+pay|pay)\s+(?:SAR|ريال)\s*(\d[\d,]*(?:\.\d{1,2})?)\s+with\s+coupon",
            around,
            re.I,
        )
        if pay_match and current_price:
            pay_price = parse_price(pay_match.group(1))
            saving = current_price - pay_price if pay_price is not None else None
            if saving is not None and saving > 0:
                output["coupon"] = {
                    "type": "you_pay_coupon",
                    "description": pay_match.group(0).strip(),
                    "pay_price": round(pay_price, 2),
                    "saving": round(saving, 2),
                    "discount_percent": round(saving / current_price * 100, 2),
                    "quantity": 1,
                }
                return output
        percent_match = re.search(_PERCENT, around, re.I)
        if percent_match:
            percent = _percent(percent_match)
            if percent is not None:
                output["coupon"] = {
                    "type": "percent_coupon",
                    "description": around.strip(),
                    "discount_percent": round(percent, 2),
                    "pay_price": round(current_price * (1 - percent / 100), 2) if current_price else None,
                    "saving": round(current_price * percent / 100, 2) if current_price else None,
                    "quantity": 1,
                }
        else:
            cash_match = re.search(
                r"(?:SAR|ريال)\s*(\d[\d,]*(?:\.\d{1,2})?)\s*(?:off\s+coupon\s+applied|off|خصم)",
                around,
                re.I,
            )
            if cash_match and current_price:
                cash_saving = parse_price(cash_match.group(1))
                if cash_saving and cash_saving < current_price:
                    output["coupon"] = {
                        "type": "cash_coupon",
                        "description": around.strip(),
                        "discount_percent": round(cash_saving / current_price * 100, 2),
                        "pay_price": round(current_price - cash_saving, 2),
                        "saving": round(cash_saving, 2),
                        "quantity": 1,
                    }
    return output

