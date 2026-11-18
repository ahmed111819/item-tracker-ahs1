"""Parse each explicit offer independently and calculate its cost basis.

No stacking is inferred. Basket estimates assume equal-priced eligible units;
unverified eligibility, banking and membership terms require review.
"""
from __future__ import annotations

import json
import os
import re
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from deal_parser_v2 import normalize_text, _price_markdown

NUM = r"\d+(?:\.\d{1,2})?"
AMOUNT = r"\d[\d,]*(?:\.\d{1,2})?"
ITEM = r"(?:items?|products?|units?|pieces?|قطعة|قطع|القطع|منتج|منتجات|المنتجات|وحدات)"
BUY = r"(?:buy|purchase|اشتر(?:ي|ِ)?|اشتري|اشتري|شراء)"
GET = r"(?:get|receive|واحصل(?:\s+عل[ىي])?|احصل(?:\s+عل[ىي])?|وخذ)"
COUPON = r"(?:coupon|voucher|(?:ال)?قسيم[ةه]|كوبون)"
CURRENCY = r"(?:SAR|ريال(?:ا)?|ر\.س\.?)"
UNVERIFIED_TERMS = re.compile(r"\b(?:up to|maximum|max\.?|bank|cardholders?|subscribe|subscription|cashback|prime|selected|eligible|different|cheapest|lowest)\b|حتى|بحد\s*(?:اقصى|أقصى)|بنك|بطاقة|اشتراك|استرداد|برايم|مختار|مؤهل|الارخص|الأرخص|الاقل|الأقل", re.I)
_REPORTED = set()
PARTIAL_QUANTITY = (
    rf'^{BUY}\s*(?P<q>\d+)\s*(?:{ITEM}\s*)?[,،]?\s*(?:and\s+)?'
    rf'(?:save|get|وفر|و?احصل(?:\s+عل[ىي])?)\s*(?:خصم\s*)?(?P<rate>{NUM})\s*%'
    rf'\s*(?:off\s*)?(?:on|على)\s*(?:the\s+)?(?:(?:cheapest|lowest-priced)\s+)?'
    rf'(?:(?P<discounted>\d+)\s*(?:{ITEM})?|(?:one|قطعة\s+واحدة|القطعة\s+الأرخص|القطعة\s+الارخص|cheapest\s+item))'
    r'(?:\s+(?P<same>of the same (?:item|product)|من نفس المنتج))?'
    r'(?:\s+(?:cheapest|lowest-priced|الأرخص|الارخص))?\s*\.?$'
)
CAPPED_QUANTITY = (
    rf'^(?:save|وفر|خصم)\s*(?P<rate>{NUM})\s*%\s*(?:on|على)\s*'
    rf'(?P<q>\d+)\s*(?:\+|or\s+more|أو\s*أكثر|او\s*اكثر)\s*(?:{ITEM})?'
    r'(?:\s*(?:from\s+(?:the\s+)?)?(?:UAE|UK|Germany|America|USA|الإمارات|الامارات|ألمانيا|المانيا|أمريكا|امريكا|بريطانيا))?'
    rf'\s*[,،]?\s*(?:up\s+to|maximum(?:\s+savings\s+of)?|بحد\s*أقصى|بحد\s*اقصى|حتى)\s*'
    rf'(?:{CURRENCY}\s*(?P<cap_a>{AMOUNT})|(?P<cap_b>{AMOUNT})\s*{CURRENCY})\s*\.?$'
)
SPEND_TIER = (
    rf'(?:save\s+(?P<en>{NUM})\s*%\s+when\s+you\s+(?:buy|spend)\s+'
    rf'{CURRENCY}\s*(?P<spend_en>{AMOUNT})(?:\s+or\s+more)?\s+(?:of|on)\s+(?:select|selected|eligible)\s+{ITEM}'
    rf'|خصم\s*(?:بنسبة\s*)?(?P<ar>{NUM})\s*%\s*(?:على\s*أي|عند\s*شراء\s*ب(?:قيمة|مبلغ))\s*'
    rf'(?P<spend_ar>{AMOUNT})\s*{CURRENCY})'
)


def number(value):
    try:
        n = Decimal(str(value).replace(',', ''))
        return n if n.is_finite() and n >= 0 else None
    except Exception:
        return None


def money(value):
    return float(value.quantize(Decimal('.01'), rounding=ROUND_HALF_UP))


def calculated(kind, description, unit, quantity=1, *, paid_quantity=None,
               percent=None, amount=None, total=None, stated=None):
    """All verified percentages derive from regular_total and payable_total."""
    price = number(unit)
    qty = number(quantity)
    if not price or not qty or qty != qty.to_integral_value() or qty > 1000:
        return None
    regular = price * qty
    if paid_quantity is not None:
        pay = price * Decimal(paid_quantity)
    elif percent is not None:
        p = number(percent)
        if p is None or not 0 < p <= 100:
            return None
        pay = regular * (1 - p / 100)
    elif amount is not None:
        a = number(amount)
        if a is None:
            return None
        pay = regular - a
    else:
        pay = number(total)
    if pay is None or not 0 <= pay < regular:
        return None
    saving = regular - pay
    rate = saving / regular * 100
    return dict(type=kind, description=description, quantity=int(qty),
                regular_total=money(regular), pay_price=money(pay), saving=money(saving),
                discount_percent=money(rate), effective_basket_discount_percent=money(rate),
                stated_discount_percent=float(stated) if stated is not None else money(rate),
                calculation=f'({money(regular)} - {money(pay)}) / {money(regular)} * 100',
                quantity_condition=int(qty) > 1, equal_price_assumption=int(qty) > 1)


def classify_offers(current_price, reference_price, card_text):
    # Preserve sentence boundaries before whitespace normalization. A percent
    # elsewhere in a card must never be attributed to a nearby coupon marker.
    raw = str(card_text or '')
    # Amazon may wrap an attached coupon phrase onto two adjacent lines.
    raw = re.sub(rf'(%|{CURRENCY})\s*\n\s*((?:with|مع|باستخدام)\s*(?:a\s+|the\s+)?{COUPON})', r'\1 \2', raw, flags=re.I)
    raw = re.sub(rf'({COUPON}\s*(?:خصم|بقيمة|بخصم|of|for)?)[ \t]*\n\s*(?=\d)', r'\1 ', raw, flags=re.I)
    clauses = [normalize_text(t) for t in re.split(r'[\n;؛]|(?<=[%!?])\s*\.\s*|(?<!\d)\.(?!\d)', raw)]
    clauses = [t for t in clauses if t]
    offers, unknown = [], []

    def add(candidate):
        if candidate and not any((x['type'],x['quantity'],x['pay_price'],x['stated_discount_percent']) ==
                                 (candidate['type'],candidate['quantity'],candidate['pay_price'],candidate['stated_discount_percent']) and
                                 x.get('minimum_spend') == candidate.get('minimum_spend') and
                                 x.get('maximum_saving') == candidate.get('maximum_saving') for x in offers):
            offers.append(candidate)

    for text in clauses:
        text = re.sub(rf'({BUY})\s*(?:قطعتين|منتجين|اثنين|اثنتين)\b', r'\1 2 قطع', text, flags=re.I)
        text = re.sub(rf'({BUY})\s*(?:خمسة|خمس)\b', r'\1 5', text, flags=re.I)
        occupied = []
        review_start = len(unknown)

        def scan(pattern, factory, *, reviewed_terms=False):
            for match in re.finditer(pattern, text, re.I):
                if any(match.start() < b and match.end() > a for a,b in occupied):
                    continue
                occupied.append(match.span())
                # Restrictions alter the equation or eligibility: never infer
                # them from an unrelated displayed unit price.
                if not reviewed_terms and UNVERIFIED_TERMS.search(text):
                    unknown.append(dict(text=text[:500], reason='conditional_or_capped_offer'))
                    continue
                candidate = factory(match)
                if candidate and 'review_reason' in candidate:
                    unknown.append(dict(text=text[:500], reason=candidate['review_reason'],
                                        maximum_basket_discount_percent=candidate['maximum_basket_discount_percent']))
                elif candidate:
                    add(candidate)
                else:
                    unknown.append(dict(text=match.group(0)[:500], reason='missing_price_or_invalid_equation'))

        def partial_quantity(match):
            quantity = int(match['q'])
            discounted = int(match['discounted'] or 1)
            rate = number(match['rate'])
            if not rate or rate > 100 or not 0 < discounted < quantity:
                return None
            upper_bound = rate * discounted / quantity
            if not match['same']:
                return dict(review_reason='cheapest_items_require_basket_prices',
                            maximum_basket_discount_percent=money(upper_bound))
            return calculated('partial_quantity_promotion', match[0], current_price, quantity,
                              percent=upper_bound, stated=rate)

        def capped_quantity(match):
            rate = number(match['rate'])
            cap = number(match['cap_a'] or match['cap_b'])
            price = number(current_price)
            quantity = int(match['q'])
            if not price or not rate or rate > 100 or not cap or not 2 <= quantity <= 1000:
                return None
            saving = min(price * quantity * rate / 100, cap)
            result = calculated('capped_quantity_promotion', match[0], price, quantity,
                                amount=saving, stated=rate)
            if result:
                result['maximum_saving'] = money(cap)
            return result

        def spend_tier(match):
            # A clean clause may contain several tiers separated by commas.
            # Extra terms could change eligibility or cap the saving.
            remainder = re.sub(SPEND_TIER, '', text, flags=re.I).strip(' .,،')
            if remainder:
                return None
            rate = number(match['en'] or match['ar'])
            spend = number(match['spend_en'] or match['spend_ar'])
            result = calculated('spend_tier_promotion', match[0], spend, percent=rate)
            if result:
                result['minimum_spend'] = money(spend)
            return result

        scan(PARTIAL_QUANTITY, partial_quantity, reviewed_terms=True)
        scan(CAPPED_QUANTITY, capped_quantity, reviewed_terms=True)
        scan(SPEND_TIER, spend_tier, reviewed_terms=True)

        # X items for the price of Y items (the Y is not a currency amount).
        scan(rf'(?:{BUY}\s*)?(?P<q>\d+)\s*(?:{ITEM}\s*)?(?:for\s+(?:the\s+)?price\s+of|بسعر|بثمن)\s*(?P<p>\d+)(?![\d.])(?:\s*{ITEM})?(?!\s*{CURRENCY})',
             lambda m: calculated('quantity_price_offer',m[0],current_price,int(m['q']),paid_quantity=int(m['p'])) if int(m['p']) < int(m['q']) else None)
        scan(r'(?P<q>\d+)\s+for\s+(?P<p>\d+)\s*$',
             lambda m: calculated('quantity_price_offer',m[0],current_price,int(m['q']),paid_quantity=int(m['p'])) if int(m['p']) < int(m['q']) else None)

        # Buy X, get Y free; Arabic written-one/two variants are normalized
        # below before this clause reaches the matcher.
        scan(rf'{BUY}\s*(?P<paid>\d+)\s*(?:{ITEM}\s*)?[,،+]?\s*{GET}\s*(?P<free>\d+)\s*(?:{ITEM}\s*)?(?:free|مجانا|مجانية|مجاناً)',
             lambda m: calculated('free_items_promotion',m[0],current_price,int(m['paid'])+int(m['free']),paid_quantity=int(m['paid'])))
        scan(r'(?:buy\s+one\s*[,،]?\s*get\s+one\s+free|(?:اشتر|اشتري)\s*(?:واحدة|واحد|قطعة)\s*(?:واحصل\s+على|وخذ)\s*(?:الثانية|واحدة|واحد|قطعة)\s*(?:مجانا|مجانية)|(?:القطعة\s+)?الثانية\s*(?:مجانا|مجانية)|\bBOGO\s+free\b)',
             lambda m: calculated('free_items_promotion',m[0],current_price,2,paid_quantity=1))

        # A fixed total for a bundle must contain currency to distinguish
        # '2 for the price of 1' from '2 for SAR 1'.
        scan(rf'(?:{BUY}\s*)?(?P<q>\d+)\s*(?:{ITEM}\s*)?(?:for|مقابل|بسعر|بـ?)\s*(?:{CURRENCY}\s*(?P<a>{AMOUNT})|(?P<b>{AMOUNT})\s*{CURRENCY})',
             lambda m: calculated('bundle_offer',m[0],current_price,int(m['q']),total=m['a'] or m['b']))

        # Explicit percentage on the second item: retain its stated rate for
        # the established 40% rule, and label the smaller basket rate clearly.
        second = rf'(?:the\s+)?(?:second|2nd)\s+{ITEM}|(?:القطعة|القطعه|المنتج|الوحدة|الوحده)\s*(?:الثانية|الثانيه|الثاني)'
        def second_offer(m):
            p = number(m['rate'])
            if p is None or not 0 < p <= 100:
                return None
            result = calculated('second_item_promotion',m[0],current_price,2,percent=p/2,stated=p)
            if result:
                result['discount_percent'] = float(p)
                result['discount_basis'] = 'second_item'
            return result
        scan(rf'(?P<rate>{NUM})\s*%\s*(?:off|discount|خصم)?\s*(?:on|for|على|عند\s*شراء)?\s*(?:{second})', second_offer)
        scan(rf'(?:{second})\s*(?:بخصم|خصم|off|discount)?\s*(?P<rate>{NUM})\s*%', second_offer)

        # Coupon values must be syntactically attached to their own marker.
        # Extract these first so a quantity rate cannot borrow a coupon rate.
        scan(rf'(?:you\s+pay|pay|تدفع|ادفع)\s*(?:{CURRENCY}\s*)?(?P<a>{AMOUNT})\s*(?:{CURRENCY}\s*)?(?:with|مع|باستخدام)\s*(?:a\s+|the\s+)?{COUPON}',
             lambda m: calculated('you_pay_coupon',m[0],current_price,total=m['a']))
        scan(rf'{COUPON}\s*(?:خصم|بقيمة|بخصم|of|for|save|off|applied)?\s*[:،-]?\s*(?P<rate>{NUM})\s*%',
             lambda m: calculated('percent_coupon',m[0],current_price,percent=m['rate']))
        scan(rf'(?P<rate>{NUM})\s*%\s*(?:off\s*)?(?:extra\s*|additional\s*|إضافي(?:ة)?\s*)?(?:with\s*(?:a\s+|the\s+)?|مع\s*|باستخدام\s*|خصم\s*)?{COUPON}(?:\s*applied)?',
             lambda m: calculated('percent_coupon',m[0],current_price,percent=m['rate']))
        scan(rf'(?:{CURRENCY}\s*(?P<a>{AMOUNT})|(?P<b>{AMOUNT})\s*{CURRENCY})\s*(?:off\s*)?{COUPON}(?:\s*applied)?',
             lambda m: calculated('cash_coupon',m[0],current_price,amount=m['a'] or m['b']))
        scan(rf'{COUPON}\s*(?:خصم|بقيمة|بخصم|of|for)?\s*(?:{CURRENCY}\s*(?P<a>{AMOUNT})|(?P<b>{AMOUNT})\s*{CURRENCY})',
             lambda m: calculated('cash_coupon',m[0],current_price,amount=m['a'] or m['b']))

        # Percent applies to all N items, in either word order.
        qty = rf'(?:on\s+(?:any\s+(?:of\s+)?)?|when\s+you\s+buy\s+|عند\s*شراء\s*|على\s*(?:أي\s*(?:من\s*)?)?)(?P<q>\d+)(?![\d,.]|\s*{CURRENCY})\s*(?:{ITEM})?\s*(?:or\s+more|أو\s*أكثر|او\s*اكثر)?'
        scan(rf'(?P<rate>{NUM})\s*%\s*(?:off\s*)?{qty}',
             lambda m: calculated('quantity_promotion',m[0],current_price,int(m['q']),percent=m['rate'])
             if int(m['q']) >= 2 else None)
        scan(rf'{BUY}\s*(?P<q>\d+)\s*(?:{ITEM}\s*)?(?:[,،]\s*)?(?:and\s*)?(?:get|save|واحصل(?:\s+عل[ىي])?|تحصل(?:\s+عل[ىي])?)\s*(?:a\s*)?(?:discount\s*(?:of\s*)?|خصم\s*)?(?P<rate>{NUM})\s*%'
             r'(?!\s*(?:off\s*)?(?:on|for|على|the\b|\d))',
             lambda m: calculated('quantity_promotion',m[0],current_price,int(m['q']),percent=m['rate']))

        scan(rf'(?:save|خصم|وفر)\s*(?P<rate>{NUM})\s*%\s*(?:off\s*)?(?:at\s+checkout|عند\s*(?:الدفع|إتمام\s*الشراء|اتمام\s*الشراء))\s*$',
             lambda m: calculated('checkout_promotion',m[0],current_price,percent=m['rate']))

        # Fixed saving on an explicitly specified group of N items.
        scan(rf'(?:save|وفر|خصم)\s*(?:{CURRENCY}\s*(?P<a>{AMOUNT})|(?P<b>{AMOUNT})\s*{CURRENCY})\s*(?:when\s+you\s+buy|on|عند\s*شراء|على)\s*(?P<q>\d+)\s*(?:{ITEM})?',
             lambda m: calculated('quantity_cash_promotion',m[0],current_price,int(m['q']),amount=m['a'] or m['b']))

        # A flat explicitly advertised product saving; coupon/quantity ranges
        # above own their text and cannot be consumed again by this rule.
        scan(rf'(?:save|وفر)\s*(?:{CURRENCY}\s*(?P<a>{AMOUNT})|(?P<b>{AMOUNT})\s*{CURRENCY})(?:\s*(?:on\s+this\s+(?:item|product)|على\s*هذا\s*المنتج))?\s*$',
             lambda m: calculated('cash_promotion',m[0],current_price,amount=m['a'] or m['b']))

        # Even with one recognized offer, retain a second unexplained one.
        residual = ''.join(' ' if any(a <= i < b for a,b in occupied) else c for i,c in enumerate(text))
        residual = re.sub(r'\bfree\s+(?:delivery|shipping|returns?)\b|(?:توصيل|شحن|ارجاع|إرجاع)\s*مجاني', '', residual, flags=re.I)
        if len(unknown) == review_start and re.search(rf'{COUPON}|\b(?:buy|free|offer)\b|(?:save|discount|خصم|وفر)\s*\d|مجانا|اشتر|احصل',residual,re.I):
            unknown.append(dict(text=text[:500],reason='unrecognized_offer_terms'))

    coupons = [o for o in offers if 'coupon' in o['type']]
    promos = [o for o in offers if 'coupon' not in o['type']]
    return dict(price_drop=_price_markdown(current_price,reference_price),
                coupon=max(coupons,key=lambda o:o['discount_percent'],default=None),
                promotion=max(promos,key=lambda o:o['discount_percent'],default=None),
                offers=offers, unrecognized=unknown)


def select_qualifying_offer(deals, coupon_threshold, promotion_threshold):
    eligible = [o for o in deals.get('offers',[]) if o['discount_percent'] >=
                (coupon_threshold if 'coupon' in o['type'] else promotion_threshold)]
    return max(eligible,key=lambda o:o['discount_percent'],default=None)


def reviewed_promotion_details(offer):
    """Return separate eligibility and discount lines for reviewed basket offers."""
    kind = offer['type']
    if kind == 'spend_tier_promotion':
        return (f"🛒 الحد الأدنى للمنتجات المؤهلة: {offer['minimum_spend']:.2f} SAR\n"
                f"🏷️ خصم هذه الشريحة: {offer['discount_percent']:.2f}%\n")
    if kind == 'capped_quantity_promotion':
        return (f"🛒 الكمية المؤهلة: {offer['quantity']} قطع أو أكثر\n"
                f"💵 سقف التوفير: {offer['maximum_saving']:.2f} SAR\n"
                f"🏷️ خصم {offer['quantity']} من هذا المنتج بعد السقف: {offer['discount_percent']:.2f}%\n")
    if kind == 'partial_quantity_promotion':
        return (f"🏷️ نسبة الخصم على القطع المشمولة: {offer['stated_discount_percent']:.2f}%\n"
                f"🛒 التوفير على الإجمالي: {offer['discount_percent']:.2f}%\n")
    return ""


def record_unrecognized(asin, bot_id, deals):
    """Bounded diagnostic artifact only; no Telegram messages or credentials."""
    folder = os.environ.get('UNKNOWN_OFFERS_DIR','').strip()
    if not folder:
        return
    for case in deals.get('unrecognized',[]):
        key = (str(bot_id),case['text'],case['reason'])
        if key in _REPORTED or len(_REPORTED) >= 100:
            continue
        _REPORTED.add(key)
        try:
            dest = Path(folder)
            dest.mkdir(parents=True,exist_ok=True)
            with (dest / f'{bot_id}.jsonl').open('a',encoding='utf-8') as out:
                out.write(json.dumps(dict(asin=asin,**case),ensure_ascii=False)+'\n')
        except OSError as exc:
            print(f'Unrecognized-offer diagnostic save failed ({type(exc).__name__}).')
