"""Read and estimate non-bank offers from Amazon.sa product pages."""
import re
from decimal import Decimal, ROUND_HALF_UP
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit

from playwright.sync_api import TimeoutError as PlaywrightTimeout

from deal_offer_rules import classify_offers, normalize_text


BANK_TERMS = re.compile(
    r"\b(?:bank\w*|cardholders?|credit cards?|debit cards?|visa|mastercard|mada|amex|"
    r"american express|stc pay|payment method|pay with (?:your|a) card|installments?)\b"
    r"|بنكي|البنك|بنك|مصرف|بطاق[هة]|بطاقات|ماستر\s*كارد|فيزا|مدى|طريقة الدفع|أقساط|تقسيط",
    re.I,
)
CODE = re.compile(r"(?:\b(?:promo\s+)?code\b|رمز\s*الخصم|كود)\s*[:：-]?\s*([A-Z0-9][A-Z0-9_-]{2,24})\b", re.I)
PERCENT = re.compile(r"(\d+(?:\.\d+)?)\s*%")
OFFER_WORDS = re.compile(r"\b(?:coupon|voucher|save|off|discount|free|promotion|buy|get)\b|خصم|وفر|كوبون|قسيمة|مجان|اشتر", re.I)
CONDITIONAL = re.compile(
    r"\b(?:subscribe|subscription|prime|first order|minimum|spend|up to|maximum|only|selected|eligible)\b"
    r"|اشتراك|برايم|أول طلب|حد أدنى|بحد|حتى|عند شراء|مؤهل|مختار", re.I,
)
AMOUNT = r"([0-9]+(?:,[0-9]{3})*(?:\.[0-9]{1,2})?)"
CURRENCY = r"(?:SAR|SR|ريال(?: سعودي)?|ر\.?\s*س\.?|﷼)"
MONEY = rf"(?:{CURRENCY}\s*{AMOUNT}|{AMOUNT}\s*{CURRENCY})"
SPEND_LIMIT = re.compile(rf"(?:when you (?:spend|buy)|minimum (?:order|spend)|عند شراء ب(?:قيمة|مبلغ)|حد أدنى)\s*{MONEY}", re.I)
SAVING_CAP = re.compile(rf"(?:up to|maximum(?: savings of)?|بحد\s*(?:أقصى|اقصى)|حتى)\s*{MONEY}", re.I)
PRODUCT_PAGE = re.compile(r"/(?:dp|gp/product|gp/aw/d|product)/([A-Z0-9]{10})(?=/|$)", re.I)
DETAIL_REGIONS = (
    "#couponFeature, #vpc_feature_div, #vpcButton, [id^='couponFeature'], "
    "#promotions_feature_div, #applicablePromotionList, #sopp_feature_div, "
    "#promoPriceBlockMessage, #promoPriceBlockMessage_feature_div, "
    "#specialOffers, #specialOffers_feature_div, #promoMessaging_feature_div, "
    "#promotionBadge, #dealBadge_feature_div, #discounts_feature_div, "
    "[data-csa-c-content-id*=\"coupon\" i], [data-csa-c-content-id*=\"promotion\" i], "
    "[id*=\"coupon\" i][id$=\"_feature_div\"], [id*=\"promotion\" i][id$=\"_feature_div\"]"
)


def amazon_product_url(value, asin):
    try:
        url = urljoin("https://www.amazon.sa/", value)
        parts = urlsplit(url)
        matched = PRODUCT_PAGE.search(parts.path)
        if (parts.scheme == "https" and parts.hostname in {"amazon.sa", "www.amazon.sa", "m.amazon.sa"}
                and parts.netloc == parts.hostname and matched and matched[1].upper() == asin):
            return url
    except ValueError:
        pass
    return f"https://www.amazon.sa/dp/{asin}"


class ProductPageUnavailable(RuntimeError):
    """A safe, specific failure reason suitable for the workflow log."""


def canonical_product_url(value, asin):
    """Drop referral parameters while preserving an explicitly selected seller."""
    url = amazon_product_url(value, asin)
    seller = [(key, item) for key, item in parse_qsl(urlsplit(url).query)
              if key.lower() in {"m", "smid", "seller", "merchantid"}]
    query = urlencode(seller)
    return f"https://www.amazon.sa/dp/{asin}" + (f"?{query}" if query else "")


PRODUCT_DETAIL_SNAPSHOT = r"""
({regions, expected}) => {
    if (document.readyState === 'loading') return false;
    const heading = document.querySelector(
        '#productTitle, #title_feature_div h1, #title_feature_div #title, '
        + '#titleSection h1, h1#title, #title_feature_div [role="heading"], '
        + 'h1[itemprop="name"], h1[data-feature-name="title"], #product-title'
    );
    const productLayout = document.querySelector(
        '#dp, #dp-container, #centerCol, #buybox, #buybox_feature_div, '
        + '#buyboxAccordion, #qualifiedBuybox, [itemtype$="/Product"]'
    );
    const metadata = document.querySelector('meta[property="og:type"]')?.content || '';
    const canonical = document.querySelector('link[rel="canonical"]')?.href
        || document.querySelector('meta[property="og:url"]')?.content || '';
    const sku = document.querySelector(
        'input#ASIN, #buybox input[name="ASIN"], #addToCart input[name="ASIN"]'
    )?.value || '';
    const jsonProduct = [...document.querySelectorAll('script[type="application/ld+json"]')].some(script => {
        try {
            const parsed = JSON.parse(script.textContent);
            const roots = Array.isArray(parsed) ? parsed : [parsed];
            return roots.flatMap(value => [value, ...(Array.isArray(value?.['@graph']) ? value['@graph'] : [])])
                .some(value => [value?.['@type']].flat().includes('Product') && value.name);
        } catch { return false; }
    });
    const headerText = (document.querySelector('h1')?.innerText || '').trim();
    if (document.querySelector('form[action*="validateCaptcha"], #captchacharacters')
        || /^(نعتذر|sorry[! .]*|robot check)$/i.test(headerText)
        || /^(نعتذر|robot check)$/i.test(document.title.trim())) {
        return {error: 'صفحة اعتذار أو تحقق من أمازون'};
    }
    const title = (heading?.textContent || headerText
        || document.querySelector('meta[property="og:title"]')?.content || '').trim();
    const canonicalAsin = canonical.match(/\/(?:dp|gp\/product|gp\/aw\/d|product)\/([A-Z0-9]{10})(?=\/|[?#]|$)/i)?.[1];
    const asin = /^[A-Z0-9]{10}$/i.test(sku) ? sku.toUpperCase() : canonicalAsin?.toUpperCase();
    if (asin && asin !== expected) return {error: 'أمازون فتح منتجًا مختلفًا'};
    if (!(heading && title) && !jsonProduct && !(productLayout && (title || asin))
        && !(title && (asin || /^product\b/i.test(metadata)))) return false;

    const blocks = [...document.querySelectorAll(regions)];
    const visible = blocks.filter(node => node.getClientRects().length
        && getComputedStyle(node).visibility !== 'hidden');
    return {asin: asin || null, snippets: visible.filter(node =>
        !visible.some(other => other !== node && other.contains(node))
    ).map(node => node.innerText.trim()).filter(Boolean)};
}
"""


def read_detail_offers(page, product_url, asin, unit_price):
    url = amazon_product_url(product_url, asin)
    try:
        response = page.goto(url, wait_until="commit", timeout=12000)
    except PlaywrightTimeout as exc:
        raise ProductPageUnavailable("انتهت مهلة فتح رابط المنتج") from exc
    if not response or not response.ok:
        code = response.status if response else "unknown"
        raise ProductPageUnavailable(f"أمازون ردّ بحالة HTTP {code}")

    try:
        handle = page.wait_for_function(
            PRODUCT_DETAIL_SNAPSHOT, arg={"regions": DETAIL_REGIONS, "expected": asin},
            polling=200, timeout=7500,
        )
        try:
            snapshot = handle.json_value()
        finally:
            handle.dispose()
    except PlaywrightTimeout as exc:
        raise ProductPageUnavailable("انتهت مهلة ظهور بيانات المنتج بعد فتح الرابط") from exc

    final = urlsplit(page.url)
    asin_match = PRODUCT_PAGE.search(final.path)
    if final.scheme != "https" or final.hostname not in {"amazon.sa", "www.amazon.sa", "m.amazon.sa"}:
        raise ProductPageUnavailable("أمازون حوّل الرابط إلى نطاق آخر")
    if snapshot.get("error"):
        raise ProductPageUnavailable(snapshot["error"])
    if (asin_match and asin_match[1].upper() != asin) or (not asin_match and snapshot["asin"] != asin):
        raise ProductPageUnavailable("تعذّر تأكيد رقم المنتج")
    return parse_detail_offers(snapshot["snippets"], unit_price)


def parse_detail_offers(snippets, price):
    offers = {}
    for snippet in snippets:
        # Keep the eligibility text together so a bank's amount cannot become a public offer.
        if BANK_TERMS.search(snippet):
            continue
        text = normalize_text(snippet)
        if not text or len(text) > 3000:
            continue
        coupon_text = re.sub(r"((?:\bcoupon\b|\bvoucher\b|كوبون|قسيمة))\s*[:：]\s*", r"\1 ", snippet, flags=re.I)
        parsed = classify_offers(float(price), None, coupon_text)
        code = CODE.search(text)
        single_code = code[1].upper() if code and len(list(CODE.finditer(text))) == 1 else ""
        candidates = parsed["offers"]
        for item in candidates:
            description = item["description"]
            kind = item["type"]
            category = "coupon" if "coupon" in kind else "checkout"
            rate = item.get("stated_discount_percent") if PERCENT.search(description) else None
            amount = None
            if kind in {"cash_coupon", "cash_promotion"}:
                amount = item["saving"]
            eligible = (
                kind in {"percent_coupon", "cash_coupon", "checkout_promotion", "cash_promotion"}
                and item["quantity"] == 1 and not CONDITIONAL.search(description)
            )
            usable_code = single_code if len(candidates) == 1 and category == "checkout" else ""
            if usable_code and not CODE.search(description):
                description = f"{description} | Code: {usable_code}"
            offer = {
                "label": description[:180], "category": category, "rate": rate,
                "amount": amount, "code": usable_code, "stackable_estimate": eligible,
            }
            if eligible or usable_code:
                key = f"{category}:{rate}:{amount}:{usable_code}"
            else:
                key = f"terms:{normalize_text(description).lower()}"
            offers[key] = offer

        if not candidates and single_code and OFFER_WORDS.search(text):
            rate = PERCENT.search(text)
            if rate:
                percent = float(rate[1])
                if 0 < percent <= 100:
                    offers[f"checkout:{percent}:None:{single_code}"] = {
                        "label": text[:180], "category": "checkout", "rate": percent,
                        "amount": None, "code": single_code, "stackable_estimate": not CONDITIONAL.search(text),
                    }
                    continue

        for item in parsed["unrecognized"]:
            label = item["text"]
            if not OFFER_WORDS.search(label) or not (re.search(r"\d|free\b|مجان", label, re.I)):
                continue
            normalized = normalize_text(label).lower()
            if any(normalized in normalize_text(o["label"]).lower() or normalize_text(o["label"]).lower() in normalized for o in offers.values()):
                continue
            rate = PERCENT.search(normalized)
            offers[f"terms:{normalized}"] = {
                "label": label[:180], "category": "", "rate": float(rate[1]) if rate else None,
                "amount": None, "code": "", "stackable_estimate": False,
            }
    return offers


def without_green_duplicate(offers, green_title):
    title = normalize_text(green_title or "").casefold().strip(" .")
    green_code = CODE.search(title)
    return {
        key: offer for key, offer in offers.items()
        if not BANK_TERMS.search(offer["label"])
        and not (title and normalize_text(offer["label"]).casefold().strip(" .") == title)
        and not (green_code and offer["code"] == green_code[1].upper())
    }


def is_quiet_offer(title):
    rates = PERCENT.findall(normalize_text(title))
    return bool(rates) and all(Decimal(rate) == 5 for rate in rates)


def find_amount(pattern, text):
    match = pattern.search(text)
    return Decimal((match[1] or match[2]).replace(",", "")) if match else None


def green_offer_key(title):
    text = normalize_text(title or "").casefold().strip(" .")
    if text and not BANK_TERMS.search(text) and OFFER_WORDS.search(text) and re.search(r"\d|free\b|مجان", text, re.I):
        return text
    return ""


def new_notifiable_offers(previous, current):
    if previous is None:
        return set()
    return {key for key in current.keys() - previous.keys() if not is_quiet_offer(current[key]["label"])}


def cents(value):
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def offer_totals(price, green_title, green, additional):
    """Only combine offers whose conditions can be modelled on one basket."""
    unit = Decimal(str(price))
    green_title = normalize_text(green_title)
    if BANK_TERMS.search(green_title):
        green_title, green = "", None
    count = (green.get("buy_quantity") or 1) if green else 1
    original = cents(unit * count)
    subtotal = original
    complete = True
    attempted = False
    if green_title:
        if not green:
            complete = False
        elif "bundle_total" in green:
            subtotal = green["bundle_total"]
            attempted = True
        elif "paid_quantity" in green:
            subtotal = unit * green["paid_quantity"]
            attempted = True
        else:
            minimum = find_amount(SPEND_LIMIT, green_title)
            cap = find_amount(SAVING_CAP, green_title)
            unsupported_terms = CONDITIONAL.search(SAVING_CAP.sub("", SPEND_LIMIT.sub("", green_title)))
            if unsupported_terms or (minimum is not None and original < minimum):
                complete = False
            else:
                discount_count = green.get("discounted_quantity") or count
                discount = unit * discount_count * Decimal(str(green["percent"])) / 100
                subtotal -= min(cap, discount) if cap is not None else discount
                attempted = True

    categories = {}
    for offer in additional.values():
        category = offer["category"]
        if not offer["stackable_estimate"] or (count > 1 and category == "coupon"):
            complete = False
        else:
            categories.setdefault(category, []).append(offer)
    for category in ("checkout", "coupon"):
        offers = categories.get(category, [])
        if len(offers) > 1 or (offers and category == "checkout" and green_title and not offers[0]["code"]):
            complete = False
            continue
        if not offers:
            continue
        offer = offers[0]
        if offer["rate"] is not None:
            subtotal *= 1 - Decimal(str(offer["rate"])) / 100
        elif offer["amount"] is not None:
            subtotal -= Decimal(str(offer["amount"]))
        else:
            complete = False
            continue
        attempted = True

    subtotal = cents(max(Decimal(0), subtotal))
    mission = min(Decimal(40), cents(original * Decimal("0.40")))
    return count, original, subtotal, mission, complete, attempted


def format_offer_alert(base, price, green_title, green, offers, new_keys=(), *, cached=False):
    # Pending notifications written by the older scanner may already contain offer math.
    base = re.split(r"\n(?:🎁 |🛍️ |📊 |📌 شرط العرض:)", base, maxsplit=1)[0]
    green_title = " ".join((green_title or "").split())
    if BANK_TERMS.search(green_title):
        green_title, green = "", None

    # Avoid repeating the same green offer when it is also visible on the product page.
    additional = without_green_duplicate(offers, green_title)
    count, original, subtotal, mission, complete, attempted = offer_totals(price, green_title, green, additional)
    basket = f" لـ{count} قطع" if count > 1 else ""
    final = max(Decimal(0), subtotal - mission)
    saving = max(Decimal(0), original - final)
    percent = cents(saving * 100 / original) if original else Decimal(0)
    saving_label = "التوفير التقديري" if complete else "التوفير التقديري المحسوب"
    calculation = []
    if count > 1:
        calculation.append(f"🛒 إجمالي {count} قبل العروض: {original:.2f} ريال")
    if attempted:
        label = "السعر التقديري بعد العروض" if complete else "السعر التقديري بعد العروض القابلة للحساب"
        calculation.append(f"🎁 {label}{basket}: {subtotal:.2f} ريال")
    elif not complete:
        calculation.append("تعذّر حساب بعض شروط العروض. تقدير المهام مبني على السعر الحالي.")
    calculation.extend([
        f"🎯 السعر التقديري بعد خصم المهام{basket}: {final:.2f} ريال",
        f"خصم المهام: {mission:.2f} ريال (40% من السعر المعروض قبل العروض، بحد 40 ريال للسلة)",
        f"💸 {saving_label}: {saving:.2f} ريال ({percent:.2f}%)",
        "التقدير يفترض قبول الدمج وإكمال سلة المهام المؤهلة إلى 100 ريال.",
    ])
    footer = "\n".join(calculation)
    rows = []
    if green_title:
        suffix = " — جديد" if "__green__" in new_keys else ""
        rows.append(f"• {green_title}{suffix}")
    for key, item in sorted(additional.items(), key=lambda pair: (pair[0] not in new_keys, pair[0])):
        suffix = " — جديد" if key in new_keys else ""
        rows.append(f"• {item['label']}{suffix}")
    title = "آخر العروض المرصودة" if cached else "العروض الحالية"
    if rows:
        text = f"{base}\n\n{title}\n"
        remaining = 3800 - len((text + footer).encode("utf-16-le")) // 2
        visible = []
        for row in rows:
            row_size = len(row.encode("utf-16-le")) // 2 + 1
            if row_size > remaining - 60:
                break
            visible.append(row)
            remaining -= row_size
        if len(visible) < len(rows):
            visible.append(f"• {len(rows) - len(visible)} عروض إضافية لم تتسع لها الرسالة")
        return text + "\n".join(visible) + "\n\n" + footer
    if cached:
        return f"{base}\n\nتعذّر التحقق من العروض الإضافية في آخر محاولة.\n\n{footer}"
    return f"{base}\n\n{footer}"
