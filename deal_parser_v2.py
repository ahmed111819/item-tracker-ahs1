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


def classify_deals(*, current_price, reference_price, card_text, regular_price_verified=True):
    """Classify independent offers; unknown conditions remain reviewable."""
    from deal_offer_rules import classify_offers
    return classify_offers(current_price, reference_price, card_text,
                           regular_price_verified=regular_price_verified)
