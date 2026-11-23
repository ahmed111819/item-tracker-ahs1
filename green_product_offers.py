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
    r"|\b(?:cannot|can't|may not|not to be)\s+(?:be\s+)?combined\b"
    r"|اشتراك|برايم|أول طلب|حد أدنى|بحد|حتى|عند\s+(?:الشراء|شراء)|مؤهل|مختار|(?:غير قابل|لا يمكن)\s+للدمج", re.I,
)
AMOUNT = r"([0-9]+(?:,[0-9]{3})*(?:\.[0-9]{1,2})?)"
CURRENCY = r"(?:SAR|SR|ريال(?: سعودي)?|ر\.?\s*س\.?|﷼)"
MONEY = rf"(?:{CURRENCY}\s*{AMOUNT}|{AMOUNT}\s*{CURRENCY})"
SPEND_LIMIT = re.compile(
    rf"(?:when you (?:spend|buy)|when spending|minimum (?:order|spend)(?: of)?|(?:off\s+)?(?:on\s+)?orders?(?:\s+of)?"
    rf"|(?:عند|مع)\s+(?:شراء|الشراء|إنفاق|انفاق)\s*(?:ب(?:ـ|قيمة|مبلغ)?)?"
    rf"|(?:على\s+)?(?:الطلبات|طلب)\s+بقيمة|(?:ب)?حد\s+أدنى(?:\s+للشراء)?)"
    rf"\s*[:：]?\s*{MONEY}(?:\s*(?:\+|or more|and above|أو أكثر|او اكثر|فأكثر))?"
    rf"(?:\s+(?:of|on)\s+(?:select|selected|eligible)\s+(?:items|products))?",
    re.I,
)
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


CONTINUE_SHOPPING = "صفحة تابع التسوق من أمازون"
AMAZON_VERIFICATION_REASONS = frozenset({"صفحة تحقق من أمازون", CONTINUE_SHOPPING})


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
({regions, expected, waitForOffers}) => {
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
    const verificationForm = document.querySelector('form[action*="validateCaptcha"]');
    const challengeInput = document.querySelector('#captchacharacters')
        || verificationForm?.querySelector('input[name="field-keywords"]:not([type="hidden"])');
    const controls = [...(verificationForm?.querySelectorAll('button, input[type="submit"]') || [])];
    const continueShopping = controls.some(control => {
        const label = (control.innerText || control.value || '').trim().replace(/\s+/g, ' ');
        return /^(continue shopping|تابع التسوق|متابعة التسوق)[!. ]*$/i.test(label);
    });
    if (!challengeInput && continueShopping) {
        return {error: 'صفحة تابع التسوق من أمازون'};
    }
    if (verificationForm || challengeInput || /^robot check[! .]*$/i.test(headerText)
        || /^robot check[! .]*$/i.test(document.title.trim())) {
        return {error: 'صفحة تحقق من أمازون'};
    }
    if (/^(نعتذر|sorry)[! .]*$/i.test(headerText) || /^(نعتذر|sorry)[! .]*$/i.test(document.title.trim())) {
        return {error: 'صفحة اعتذار من أمازون'};
    }
    if (location.pathname === '/' || /^\/ref=/.test(location.pathname)
        || document.querySelector('#gw-layout, #gw-content-grid, #gw-card-layout')) {
        return {home: true};
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
    const snippets = visible.filter(node =>
        !visible.some(other => other !== node && other.contains(node))
    ).map(node => node.innerText.trim()).filter(Boolean);
    const loading = '[aria-busy="true"], [data-loading="true"], .a-spinner';
    const offersLoading = visible.some(node => node.matches(loading)
        || [...node.querySelectorAll(loading)].some(indicator => indicator.getClientRects().length));
    if (waitForOffers && (!snippets.length || offersLoading)) return false;
    return {asin: asin || null, snippets, offersLoading, ready: document.readyState === 'complete'};
}
"""


def wait_for_detail_snapshot(page, asin, *, wait_for_offers=False):
    try:
        handle = page.wait_for_function(
            PRODUCT_DETAIL_SNAPSHOT,
            arg={"regions": DETAIL_REGIONS, "expected": asin, "waitForOffers": wait_for_offers},
            polling=200, timeout=1400 if wait_for_offers else 7500,
        )
        try:
            return handle.json_value()
        finally:
            handle.dispose()
    except PlaywrightTimeout as exc:
        if wait_for_offers:
            return page.evaluate(PRODUCT_DETAIL_SNAPSHOT, {"regions": DETAIL_REGIONS, "expected": asin}) or {}
        raise ProductPageUnavailable("انتهت مهلة ظهور بيانات المنتج بعد فتح الرابط") from exc


def click_continue_shopping(page, asin, capture):
    form = page.locator("form[action*='validateCaptcha']")
    if form.count() != 1:
        return None
    button = form.get_by_role(
        "button", name=re.compile(r"^(continue shopping|تابع التسوق|متابعة التسوق)[!. ]*$", re.I)
    )
    if button.count() != 1:
        return None
    safe_form = button.evaluate("""control => {
        const form = control.form;
        if (!form || new URL(control.formAction && control.hasAttribute('formaction')
            ? control.formAction : form.action).origin !== location.origin) return false;
        if (document.querySelector('#captchacharacters')) return false;
        return !form.querySelector(
            'input:not([type="hidden"], [type="submit"], [type="button"]), textarea, iframe'
        );
    }""")
    if not safe_form:
        return None
    capture(page, "before")
    clicked = False
    try:
        with page.expect_navigation(wait_until="domcontentloaded", timeout=8000) as navigation:
            button.click(timeout=2500)
            clicked = True
        response = navigation.value
        if not is_amazon_detail_origin(urlsplit(page.url)):
            raise ProductPageUnavailable("أمازون حوّل الرابط إلى نطاق آخر")
        if response is not None and not response.ok:
            raise ProductPageUnavailable(f"أمازون ردّ بحالة HTTP {response.status}")
        return wait_for_detail_snapshot(page, asin)
    finally:
        if clicked:
            capture(page, "after")


def open_detail_snapshot(page, url, asin):
    try:
        response = page.goto(url, wait_until="commit", timeout=12000)
    except PlaywrightTimeout as exc:
        raise ProductPageUnavailable("انتهت مهلة فتح رابط المنتج") from exc
    if not response or not response.ok:
        code = response.status if response else "unknown"
        raise ProductPageUnavailable(f"أمازون ردّ بحالة HTTP {code}")

    snapshot = wait_for_detail_snapshot(page, asin)
    if not is_amazon_detail_origin(urlsplit(page.url)):
        raise ProductPageUnavailable("أمازون حوّل الرابط إلى نطاق آخر")
    return snapshot


def read_detail_snippets(page, product_url, asin, *, capture_continue=None):
    url = amazon_product_url(product_url, asin)
    snapshot = open_detail_snapshot(page, url, asin)
    continued = snapshot.get("error") == CONTINUE_SHOPPING and capture_continue is not None
    reopened = False
    if continued:
        print(f"Amazon showed Continue Shopping for {asin}; checking its result.")
        try:
            snapshot = click_continue_shopping(page, asin, capture_continue)
            if snapshot is None:
                raise ProductPageUnavailable(CONTINUE_SHOPPING)
            if snapshot.get("home"):
                reopened = True
                print(f"Continue Shopping opened Amazon home for {asin}; reopening the direct product link once.")
                snapshot = open_detail_snapshot(page, canonical_product_url(url, asin), asin)
        except PlaywrightTimeout as exc:
            print(f"Continue Shopping timed out for {asin}.")
            raise ProductPageUnavailable(CONTINUE_SHOPPING) from exc
        except ProductPageUnavailable as exc:
            print(f"Continue Shopping did not open {asin}: {exc}.")
            raise
    if "snippets" in snapshot and (not snapshot["snippets"] or snapshot["offersLoading"]):
        snapshot = wait_for_detail_snapshot(page, asin, wait_for_offers=True)
        incomplete = "snippets" in snapshot and (
            snapshot["offersLoading"] or not snapshot["snippets"] and not snapshot["ready"]
        )
        if not snapshot or incomplete:
            raise ProductPageUnavailable("قسم عروض المنتج ما اكتمل تحميله")

    final = urlsplit(page.url)
    if not is_amazon_detail_origin(final):
        raise ProductPageUnavailable("أمازون حوّل الرابط إلى نطاق آخر")
    asin_match = PRODUCT_PAGE.search(final.path)
    reason = snapshot.get("error")
    if snapshot.get("home"):
        reason = "أمازون فتح الصفحة الرئيسية بدل المنتج"
    if not reason and ((asin_match and asin_match[1].upper() != asin)
                       or (not asin_match and snapshot["asin"] != asin)):
        reason = "تعذّر تأكيد رقم المنتج"
    if reason:
        if continued:
            print(f"Continue Shopping did not open {asin}: {reason}.")
        raise ProductPageUnavailable(reason)
    if reopened:
        print(f"Reopened the requested product {asin} after Continue Shopping.")
    elif continued:
        print(f"Continue Shopping opened the requested product {asin}.")
    return snapshot["snippets"]


def read_detail_offers(page, product_url, asin, unit_price, *, capture_continue=None):
    snippets = read_detail_snippets(page, product_url, asin, capture_continue=capture_continue)
    return parse_detail_offers(snippets, unit_price)


def is_amazon_detail_origin(parts):
    return (parts.scheme == "https" and parts.hostname in {"amazon.sa", "www.amazon.sa", "m.amazon.sa"}
            and parts.netloc == parts.hostname)


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


def free_bundle_signature(title):
    text = normalize_text(title or "").casefold().strip(" .")
    same_cost = re.fullmatch(r"(?:get\s+)?(\d+)\s+for\s+the\s+price\s+of\s+(\d+)", text)
    if same_cost:
        total, paid = map(int, same_cost.groups())
        return (total, paid) if 0 < paid < total else None
    free_units = re.fullmatch(r"buy\s+(\d+)\s*(?:,\s*)?(?:and\s+)?get\s+(\d+)\s+free", text)
    if free_units:
        paid, free = map(int, free_units.groups())
        return (paid + free, paid) if paid and free else None
    return None


def without_green_duplicate(offers, green_title):
    title = normalize_text(green_title or "").casefold().strip(" .")
    green_code = CODE.search(title)
    green_bundle = free_bundle_signature(title)
    return {
        key: offer for key, offer in offers.items()
        if not BANK_TERMS.search(offer["label"])
        and not (title and normalize_text(offer["label"]).casefold().strip(" .") == title)
        and not (green_code and offer["code"] == green_code[1].upper())
        and not (green_bundle and not offer["code"] and free_bundle_signature(offer["label"]) == green_bundle)
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


def pricing_terms(text):
    """Separate a stated basket minimum and saving cap from other eligibility terms."""
    text = normalize_text(text).replace("٪", "%")
    minimum = find_amount(SPEND_LIMIT, text)
    cap = find_amount(SAVING_CAP, text)
    unresolved = bool(CONDITIONAL.search(SAVING_CAP.sub("", SPEND_LIMIT.sub("", text))))
    return minimum, cap, unresolved


def duplicates_green_spend(offer, green_title, green):
    if not green or "percent" not in green or offer["category"] != "checkout" or offer["rate"] is None:
        return False
    minimum, cap, _ = pricing_terms(green_title)
    if minimum is None or re.search(r"\bbuy\s*\d+\b", SPEND_LIMIT.sub("", normalize_text(green_title)), re.I):
        return False
    other_minimum, other_cap, _ = pricing_terms(offer["label"])
    return (other_minimum == minimum and other_cap == cap
            and Decimal(str(offer["rate"])) == Decimal(str(green["percent"])))


def has_notifiable_combination(offers, green_key, green_title, green):
    distinct = [offer for offer in offers.values() if not duplicates_green_spend(offer, green_title, green)]
    return (len(distinct) + bool(green_key) >= 2
            and any(not is_quiet_offer(offer["label"]) for offer in distinct))


ADDITIONAL_QUANTITY = re.compile(
    r"\b(?:when you )?buy\s+\d+(?:\s+or more)?(?:\s+(?:eligible|selected))?(?:\s+(?:items?|units?|pieces?))?"
    r"|\b(?:on\s+any|when buying)\s+\d+(?:\s+(?:or more|and above)|\+)?"
    r"(?:\s+(?:selected|eligible))?(?:\s+(?:items?|units?|pieces?))?"
    r"|\bon\s+\d+(?:\s+(?:or more|and above)|\+)"
    r"(?:\s+(?:selected|eligible))?(?:\s+(?:items?|units?|pieces?))?"
    r"|(?:عند شراء|اشتر(?:ي)?|على)\s*(?:أي\s*)?\d+"
    r"(?:\s*(?:قطع|قطعة|منتجات))?(?:\s*(?:أو أكثر|او اكثر|فأكثر))?",
    re.I,
)
PARTIAL_ITEM_DISCOUNT = re.compile(
    r"\b(?:cheapest|lowest(?:-priced)?|second|2nd|third|3rd|each eligible unit)\b"
    r"|(?:الأرخص|الارخص|الثانية|الثالثة)|\bon\s+(?:one|1)(?:\s|$)", re.I,
)


def additional_percentage(offer):
    """Model clear percentage offers as if the eligible quantity/basket is completed."""
    text = normalize_text(offer["label"]).replace("٪", "%")
    if BANK_TERMS.search(text) or PARTIAL_ITEM_DISCOUNT.search(text):
        return None
    matches = PERCENT.findall(text)
    if len(matches) != 1:
        return None
    code = CODE.search(text)
    quantity = ADDITIONAL_QUANTITY.search(text)
    minimum, cap, _ = pricing_terms(text)
    _, _, unsupported = pricing_terms(ADDITIONAL_QUANTITY.sub("", text))
    if unsupported or not (offer["stackable_estimate"] or code or quantity or minimum is not None):
        return None
    rate = Decimal(matches[0])
    if not 0 < rate <= 100:
        return None
    offer_code = (offer["code"] or (code[1] if code else "")).upper()
    key = ("code", offer_code) if offer_code else ("terms", text.casefold().strip(" ."))
    return key, rate, cap


def offer_totals(price, green_title, green, additional):
    """Only combine offers whose conditions can be modelled on one basket."""
    unit = Decimal(str(price))
    green_title = normalize_text(green_title)
    if BANK_TERMS.search(green_title):
        green_title, green = "", None
    count = (green.get("buy_quantity") or 1) if green else 1
    minimum, cap, unsupported_terms = pricing_terms(green_title)
    spend_only = (
        green and "percent" in green and minimum is not None
        and not re.search(r"\bbuy\s*\d+\b", SPEND_LIMIT.sub("", green_title), re.I)
    )
    if spend_only:
        count = 1
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
            # A known minimum can be met by the rest of the eligible basket.
            if unsupported_terms:
                complete = False
            else:
                discount_count = 1 if spend_only else (green.get("discounted_quantity") or count)
                discount = unit * discount_count * Decimal(str(green["percent"])) / 100
                subtotal -= min(cap, discount) if cap is not None else discount
                attempted = True

    estimated_rates = set()
    cash_by_category = {}
    for offer in additional.values():
        if BANK_TERMS.search(offer["label"]) or duplicates_green_spend(offer, green_title, green):
            # A mirrored checkout code remains visible and its basket tier is counted once.
            continue
        percentage = additional_percentage(offer)
        if percentage:
            key, rate, cap = percentage
            if key not in estimated_rates:
                discount = original * rate / 100
                subtotal -= min(cap, discount) if cap is not None else discount
                estimated_rates.add(key)
                attempted = True
            continue

        category = offer["category"]
        if (not offer["stackable_estimate"] or offer["rate"] is not None or offer["amount"] is None
                or category not in {"checkout", "coupon"} or (count > 1 and category == "coupon")):
            complete = False
        else:
            cash_by_category.setdefault(category, []).append(offer)
    for category, cash_offers in cash_by_category.items():
        if len(cash_offers) != 1 or (category == "checkout" and green_title and not cash_offers[0]["code"]):
            complete = False
            continue
        subtotal -= Decimal(str(cash_offers[0]["amount"]))
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
    count, original, green_subtotal, mission, _, green_calculated = offer_totals(price, green_title, green, {})
    five_offers = {}
    for key, item in additional.items():
        percentage = additional_percentage(item)
        if percentage and percentage[1] == 5 and not duplicates_green_spend(item, green_title, green):
            five_offers[key] = percentage
    # A quantity-based or automatic 5% offer takes precedence over a competing 5% code.
    baseline_five_key = max(
        five_offers,
        key=lambda key: (five_offers[key][0][0] != "code",
                         five_offers[key][2] if five_offers[key][2] is not None else Decimal("Infinity")),
        default=None,
    )
    baseline = {baseline_five_key: additional[baseline_five_key]} if baseline_five_key is not None else {}
    merged = {
        key: item for key, item in additional.items()
        if key not in five_offers or key == baseline_five_key
    }
    _, _, baseline_subtotal, _, _, _ = offer_totals(price, green_title, green, baseline)
    _, _, merged_subtotal, _, _, _ = offer_totals(price, green_title, green, merged)
    baseline_price = max(Decimal(0), baseline_subtotal - mission)
    merged_price = max(Decimal(0), merged_subtotal - mission)

    calculation = []
    if count > 1 and green_calculated:
        saving = max(Decimal(0), original - green_subtotal)
        calculation.append(
            f"🛒 {count} قطع: قبل العرض {original:.2f}، بعد الأساسي {green_subtotal:.2f}، التوفير {saving:.2f} ريال"
        )
    components = []
    if green_calculated:
        components.append("الأساسي")
    if baseline:
        components.append("5%")
    components.append("المهام")
    calculation.append(f"🎯 بعد {' + '.join(components)}: {baseline_price:.2f} ريال")
    if merged_price < baseline_price:
        calculation.append(f"🤝 إذا اندمجت باقي العروض: {merged_price:.2f} ريال تقديريًا")
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
    return f"{base}\n\n{footer}"
