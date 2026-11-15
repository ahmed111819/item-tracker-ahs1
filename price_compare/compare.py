"""Conservative, link-only Saudi price comparison experiment (Python 3.12)."""
import argparse
import concurrent.futures
import ipaddress
import json
import os
import re
import socket
import sys
import unicodedata
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlparse, urljoin

import requests
from bs4 import BeautifulSoup

MAX_BYTES = 4_000_000


class ComparisonError(ValueError):
    """A deliberately safe, user-facing diagnostic with no request credentials."""


def norm(value):
    value = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"\s+", " ", "".join(c for c in value if unicodedata.category(c) != "Cf")).strip()


def clean_brand(value):
    if isinstance(value, dict):
        value = value.get("name", "")
    value = norm(value)
    for pattern in (r"^visit the (.+) store$", r"^brand:\s*(.+)$", r"^العلامة التجارية\s*:\s*(.+)$"):
        found = re.match(pattern, value)
        if found:
            return found.group(1).strip()
    return value


def public_url(url):
    p = urlparse(url)
    if p.scheme not in {"http", "https"} or not p.hostname or p.username or p.password:
        raise ValueError("رابط غير صالح؛ استخدم رابط صفحة منتج http أو https")
    if p.port not in {None, 80, 443}:
        raise ValueError("منفذ الرابط غير مدعوم")
    addresses = socket.getaddrinfo(p.hostname, p.port or 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise ValueError("الرابط يجب أن يكون لموقع عام")
    return url


def fetch(url):
    # No API keys/cookies are attached to retailer requests. Validate every redirect.
    for _ in range(6):
        public_url(url)
        with requests.get(url, timeout=(10, 25), allow_redirects=False, stream=True,
                          headers={"User-Agent": "Mozilla/5.0 (compatible; PriceCompare/0.2)", "Accept-Language": "en,ar;q=0.8"}) as r:
            if r.is_redirect:
                url = urljoin(url, r.headers["Location"])
                continue
            if r.status_code in {403, 429}:
                raise ComparisonError(f"المتجر رفض القراءة الآلية (HTTP {r.status_code}). لم يتم الوصول إلى مرحلة مقارنة الأسعار. جرّب رابط نفس المنتج من متجر آخر؛ إعادة التشغيل وحدها قد لا تحل الحظر.")
            if r.status_code >= 400:
                raise ComparisonError(f"تعذر فتح صفحة المنتج (HTTP {r.status_code}). تحقق أن الرابط يعمل وأن المنتج لم يُحذف.")
            if "html" not in r.headers.get("Content-Type", "").lower():
                raise ValueError("الصفحة ليست HTML")
            data = bytearray()
            for chunk in r.iter_content(65536):
                data.extend(chunk)
                if len(data) > MAX_BYTES:
                    raise ValueError("حجم الصفحة أكبر من الحد")
            return data.decode(r.encoding or "utf-8", errors="replace"), url
    raise ValueError("تحويلات كثيرة في الرابط")


def walk(value):
    if isinstance(value, dict):
        yield value
        for v in value.values():
            yield from walk(v)
    elif isinstance(value, list):
        for v in value:
            yield from walk(v)


def typename(obj, name):
    t = obj.get("@type", [])
    return any(str(v).rstrip("/").split("/")[-1] == name for v in (t if isinstance(t, list) else [t]))


def gtins(obj):
    result = set()
    for key in ("gtin", "gtin8", "gtin12", "gtin13", "gtin14"):
        raw = re.sub(r"\D", "", str(obj.get(key, "")))
        if len(raw) not in {8, 12, 13, 14}:
            continue
        digits = [int(x) for x in raw]
        check = (10 - sum(n * (3 if i % 2 == 0 else 1)
                          for i, n in enumerate(reversed(digits[:-1]))) % 10) % 10
        if check == digits[-1]:
            result.add(raw.zfill(14))
    return sorted(result)


def amazon_product(soup, url):
    """Read the selected Amazon item, never recommendation cards or list prices."""
    host = (urlparse(url).hostname or "").lower()
    if host not in {"amazon.sa", "www.amazon.sa"}:
        return None
    title = soup.select_one("#productTitle")
    if title is None or not title.get_text(strip=True):
        raise ComparisonError("أمازون لم يعرض تفاصيل المنتج؛ قد تكون الصفحة صفحة تحقق أو حظر مؤقت.")
    result = {"@type": "Product", "name": title.get_text(" ", strip=True)}
    brand = soup.select_one("#bylineInfo")
    if brand:
        raw = brand.get_text(" ", strip=True)
        for pattern in (r"^Brand:\s*(.+)$", r"^Visit the (.+) Store$", r"^العلامة التجارية\s*:\s*(.+)$"):
            found = re.match(pattern, raw, re.I)
            if found:
                result["brand"] = found.group(1)
                break
    labels = {"item model number": "model", "manufacturer part number": "mpn",
              "model number": "model", "رقم الموديل": "model",
              "رقم موديل السلعة": "model", "color": "color", "اللون": "color",
              "size": "size", "الحجم": "size", "upc": "gtin12", "ean": "gtin13"}
    containers = soup.select('[id^="productDetails_"], #detailBullets_feature_div, #productOverview_feature_div')
    for container in containers:
        for row in container.select("tr"):
            cells = row.find_all(["th", "td"], recursive=False)
            if len(cells) == 2:
                label = norm(cells[0].get_text(" ", strip=True)).strip(" :\u200e\u200f")
                if label in labels:
                    result[labels[label]] = cells[1].get_text(" ", strip=True).strip("\u200e\u200f")
    price = soup.select_one('#corePriceDisplay_desktop_feature_div .a-price:not(.a-text-price) .a-offscreen')
    if price is None:
        price = soup.select_one('#corePrice_feature_div .a-price:not(.a-text-price) .a-offscreen')
    availability = soup.select_one("#availability")
    if price and availability and re.search(r"in stock|متوفر", availability.get_text(), re.I):
        raw = norm(price.get_text(" ", strip=True))
        amount = re.search(r"\d[\d,]*(?:\.\d+)?", raw)
        if amount and ("sar" in raw or "ر.س" in raw or "ريال" in raw):
            # Condition remains unknown unless the page explicitly states it.
            condition_node = soup.select_one("#condition, #newAccordionRow .a-text-bold")
            condition = norm(condition_node.get_text(" ", strip=True)) if condition_node else ""
            result["offers"] = {"@type": "Offer", "price": amount.group().replace(",", ""),
                                "priceCurrency": "SAR", "availability": "https://schema.org/InStock",
                                "itemCondition": "NewCondition" if condition in {"new", "جديد"} else ""}
    return result


def hnak_product(soup, url):
    """Read HNAK's server-rendered primary product payload without executing JS."""
    if (urlparse(url).hostname or "").lower() not in {"hnak.com", "www.hnak.com"}:
        return None
    for script in soup.find_all("script"):
        text = script.get_text()
        if "productData" not in text or not text.strip().startswith("self.__next_f.push("):
            continue
        try:
            payload = json.loads(text[text.index("(") + 1:text.rindex(")")])
            records = payload[1].splitlines()
        except (ValueError, TypeError, IndexError):
            continue
        for record in records:
            try:
                decoded = json.loads(record.split(":", 1)[1])
            except (ValueError, IndexError):
                continue
            for node in walk(decoded):
                p = node.get("productData")
                if not isinstance(p, dict) or p.get("type") != "PRODUCT":
                    continue
                if not p.get("relative_url") or urlparse(url).path.rstrip("/").split("/")[-1] != p["relative_url"]:
                    continue
                result = {"@type": "Product", "name": p.get("name", ""), "brand": p.get("mpbrand", "")}
                for attr in walk(p.get("additional_information", [])):
                    value = attr.get("value")
                    if not isinstance(value, str):
                        continue
                    label = norm(attr.get("label"))
                    key = {"model number": "model", "bar code": "gtin", "color": "color"}.get(label)
                    if key:
                        result[key] = value
                prices = p.get("price_range", {})
                low = prices.get("minimum_price", {}).get("final_price", {})
                high = prices.get("maximum_price", {}).get("final_price", {})
                if low and low == high:
                    result["offers"] = {"@type": "Offer", "price": low.get("value"),
                                        "priceCurrency": low.get("currency"),
                                        "availability": "InStock" if p.get("stock_status") == "IN_STOCK" else "OutOfStock"}
                return result
    return None


def parse_page(html, url):
    soup = BeautifulSoup(html, "html.parser")
    nodes = []
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            nodes.extend(walk(json.loads(script.string or script.get_text())))
        except (ValueError, TypeError):
            continue
    products = [n for n in nodes if typename(n, "Product")]
    # Some stores omit @type on the primary Product while marking og:type=product.
    og_type = soup.find("meta", property="og:type")
    if not products and og_type and og_type.get("content") == "product":
        products = [n for n in nodes if not n.get("@type") and n.get("name") and n.get("offers") and n.get("url") == url]
    # Product-list pages and ambiguous variant groups must not choose the cheapest sibling.
    unique = {json.dumps(p, sort_keys=True, ensure_ascii=False): p for p in products}
    products = list(unique.values())
    if len(products) > 1:
        exact = [p for p in products if str(p.get("url", "")).rstrip("/") == url.rstrip("/")]
        products = exact if len(exact) == 1 else []
    if not products:
        custom = amazon_product(soup, url) or hnak_product(soup, url)
        if custom:
            products = [custom]
        else:
            raise ValueError("لم أجد بيانات منتج واحد واضحة؛ قد تكون الصفحة محمية أو تحتاج JavaScript")
    p = products[0]
    brand = clean_brand(p.get("brand", ""))
    offers = p.get("offers", [])
    if isinstance(offers, dict):
        offers = [offers]
    offers = [o for o in offers if isinstance(o, dict) and not typename(o, "AggregateOffer")]
    prices = []
    for o in offers:
        try:
            price = Decimal(str(o.get("price", "")))
        except InvalidOperation:
            continue
        if not price.is_finite() or price <= 0 or o.get("priceCurrency") != "SAR":
            continue
        availability = str(o.get("availability", "")).split("/")[-1]
        condition = str(o.get("itemCondition", p.get("itemCondition", ""))).split("/")[-1]
        prices.append({"price": str(price), "condition": condition, "availability": availability})
    # Multiple different offer conditions/prices may be a variant aggregate.
    offer = prices[0] if prices and all(x == prices[0] for x in prices) else None
    attrs = {k: norm(p[k]) for k in ("color", "size", "material", "pattern") if p.get(k)}
    return {"title": str(p.get("name", "")), "brand": norm(brand),
            "mpn": norm(p.get("mpn", "")), "model": norm(p.get("model", "")),
            "gtins": gtins(p), "attributes": attrs, "offer": offer, "url": url,
            "store": urlparse(url).hostname.removeprefix("www.")}


def match(source, candidate):
    shared_gtin = bool(set(source["gtins"]) & set(candidate["gtins"]))
    if source["gtins"] and candidate["gtins"] and not shared_gtin:
        return False, "اختلاف الباركود العالمي"
    if source["brand"] and candidate["brand"] and source["brand"] != candidate["brand"]:
        return False, "اختلاف الماركة"
    for k in ("mpn", "model"):
        if source[k] and candidate[k] and source[k] != candidate[k]:
            return False, "اختلاف رقم الشركة أو الموديل"
    if any(k in candidate["attributes"] and norm(v) != norm(candidate["attributes"][k])
           for k, v in source["attributes"].items() if k not in {"color", "size"}):
        return False, "المواصفات مختلفة أو ناقصة"
    sa, ca = variant_specs(source), variant_specs(candidate)
    for key in sa.keys() | ca.keys():
        if key in sa and key in ca and sa[key] != ca[key]:
            return False, f"اختلاف مواصفات النسخة: {key}"
        if not shared_gtin and sa.get(key) != ca.get(key):
            return False, f"مواصفات ناقصة لتأكيد نفس النسخة: {key}"
    if not shared_gtin:
        brand = source["brand"]
        candidate_brand = candidate["brand"] or (brand if contains(candidate["title"], brand) else "")
        ids = [source[k] for k in ("mpn", "model") if valid_model(source[k])]
        if not brand or candidate_brand != brand or not ids:
            return False, "لا يوجد باركود متطابق أو ماركة ورقم موديل كافيان"
        if not any(any(model_key(i) == model_key(candidate[k]) for k in ("mpn", "model") if candidate[k])
                   or contains(candidate["title"], i) for i in ids):
            return False, "رقم الموديل الكامل غير موجود في النتيجة"
        if source["attributes"].get("material") != candidate["attributes"].get("material"):
            return False, "الخامة مختلفة أو ناقصة"
        if source["attributes"].get("pattern") != candidate["attributes"].get("pattern"):
            return False, "النقشة مختلفة أو ناقصة"
    a, b = source["offer"], candidate["offer"]
    if not b:
        return False, "السعر بالريال أو التوفر أو العرض المحدد غير مؤكد"
    if b.get("availability", "InStock") != "InStock":
        return False, "التوفر في المتجر غير مؤكد أو المنتج نافد"
    if a and a["condition"] and a["condition"] != b["condition"]:
        return False, "حالة المنتج مختلفة أو غير محددة"
    if not b["condition"]:
        return False, "حالة المنتج في المتجر غير محددة"
    if not a or not a["condition"]:
        if b["condition"] != "NewCondition" or re.search(r"\b(used|renewed|refurbished)\b|مستعمل|مجدد", norm(source["title"])):
            return False, "لا يمكن تأكيد توافق حالة المنتج"
    return True, "باركود متطابق ومواصفات متوافقة" if shared_gtin else "ماركة ورقم موديل كامل ومواصفات نسخة متطابقة"


def model_key(value):
    return re.sub(r"[^a-z0-9]", "", norm(value))


def valid_model(value):
    key = model_key(value)
    return len(key) >= 5 and bool(re.search(r"[a-z]", key)) and bool(re.search(r"\d", key))


def contains(text, value):
    return bool(value and re.search(r"(?<!\w)" + re.escape(norm(value)) + r"(?!\w)", norm(text)))


def variant_specs(product):
    text = norm(product["title"] + " " + " ".join(str(v) for v in product["attributes"].values()))
    result = {}
    colors = {"black": ["black", "أسود", "اسود"], "white": ["white", "أبيض", "ابيض"],
              "blue": ["blue", "أزرق", "ازرق"], "red": ["red", "أحمر", "احمر"],
              "green": ["green", "أخضر", "اخضر"], "silver": ["silver", "فضي"],
              "gold": ["gold", "ذهبي"], "grey": ["grey", "gray", "رمادي"]}
    found = {key for key, words in colors.items() if any(contains(text, word) for word in words)}
    raw_color = norm(product["attributes"].get("color"))
    if found:
        result["color"] = tuple(sorted(found))
    elif raw_color:
        result["color"] = (raw_color,)
    packs = set()
    for pattern in (r"\b(?:pack|box|set)\s*(?:of\s*)?(\d+)\b",
                    r"\b(\d+)\s*(?:pcs?|pieces?|count|ct|pack)\b",
                    r"(?:عبوة|علبة|طقم)\s*(?:من\s*)?(\d+)", r"(\d+)\s*(?:قطعة|قطع|حبة|حبات|قلم)"):
        packs.update(str(int(n)) for n in re.findall(pattern, text))
    if packs:
        result["pack"] = tuple(sorted(packs))
    multipacks = re.findall(r"\b(\d+)\s*[x×]\s*(\d+)\s*(?:pcs?|pieces?|count|ct)\b", text)
    if multipacks:
        result["multipack"] = tuple(sorted(f"{int(a)}x{int(b)}" for a, b in multipacks))
    # Preserve capacities and dimensions; normalize equivalent volume/storage units.
    units = {"gb": ("storage", 1), "جيجابايت": ("storage", 1), "tb": ("storage", 1000),
             "ml": ("volume", 1), "مل": ("volume", 1), "l": ("volume", 1000), "لتر": ("volume", 1000),
             "mm": ("length", 1), "مم": ("length", 1), "cm": ("length", 10),
             "g": ("weight", 1), "kg": ("weight", 1000), "inch": ("inch", 1), "inches": ("inch", 1),
             "v": ("voltage", 1), "volt": ("voltage", 1), "w": ("power", 1)}
    for amount, unit in re.findall(r"(?<![\w.])(\d+(?:\.\d+)?)\s*(gb|tb|ml|mm|cm|kg|inches|inch|volt|v|w|g|l|جيجابايت|مل|مم|لتر)(?!\w)", text):
        category, factor = units[unit]
        result.setdefault(category, set()).add(str((Decimal(amount) * factor).normalize()))
    for k, value in list(result.items()):
        if isinstance(value, set):
            result[k] = tuple(sorted(value))
    if product["attributes"].get("size") and not any(k in result for k in ("length", "volume", "weight", "storage", "inch")):
        result["size"] = norm(product["attributes"]["size"])
    return result


def search(source, key):
    identity = '"' + norm(source["title"])[:140] + '"'
    if source["mpn"] or source["model"]:
        identity += ' "' + (source["mpn"] or source["model"]) + '"'
    query = source["gtins"][0] if source["gtins"] else identity
    if not query.strip():
        query = source["title"][:160]
    links = []
    for language, suffix in (("en", " Saudi Arabia buy"), ("ar", " شراء السعودية")):
        links.extend(search_language(query + suffix, language, key))
    direct = [u for u in links if not search_wrapper(u)]
    if not direct or sum(search_wrapper(u) for u in links) > len(links) / 2:
        # Do not keep retrying Google's opaque redirect links from the same network.
        for language in ("en", "ar"):
            try:
                direct.extend(search_language(query + " Saudi Arabia", language, key, engine="bing"))
            except ComparisonError:
                continue
        # Search known storefronts; never synthesize a product URL from a title.
        shops = "(site:apple.com/sa-en/shop/product OR site:hnak.com OR site:alephksa.com OR site:extra.com OR site:jarir.com OR site:noon.com OR site:almanea.sa)"
        try:
            direct.extend(search_language(query + " " + shops, "en", key, engine="bing"))
        except ComparisonError:
            pass
    clean = [u for u in direct if not search_wrapper(u) and not social_link(u)]
    return list(dict.fromkeys(clean + links))[:30]


def search_wrapper(url):
    host = (urlparse(url).hostname or "").lower()
    return host in {"google.com", "www.google.com", "bing.com", "www.bing.com"}


def social_link(url):
    host = (urlparse(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in ("facebook.com", "instagram.com", "youtube.com", "tiktok.com", "x.com"))


def search_language(query, language, key, engine="google"):
    params = {"engine": engine, "q": query + " -site:facebook.com -site:instagram.com -site:youtube.com", "api_key": key}
    params.update({"gl": "sa", "hl": language, "num": 10} if engine == "google" else {"mkt": language + "-SA"})
    try:
        r = requests.get("https://serpapi.com/search.json", timeout=35,
                         params=params)
        # Never include the prepared URL (which contains the API key) in an error.
        if r.status_code != 200:
            raise ComparisonError(f"خدمة البحث أعادت HTTP {r.status_code}. راجع مفتاح SerpApi والرصيد في حسابك.")
        data = r.json()
    except requests.RequestException:
        raise ComparisonError("تعذر الاتصال بخدمة البحث؛ حاول لاحقًا") from None
    if data.get("error"):
        raise ComparisonError("رفضت خدمة البحث الطلب؛ راجع المفتاح والرصيد")
    links = []
    for item in data.get("organic_results", []):
        options = [item.get("link"), item.get("redirect_link")]
        options = [urljoin("https://www.google.com", u) for u in options if isinstance(u, str)]
        direct = [u for u in options if not (urlparse(u).hostname or "").endswith("google.com")]
        if direct or options:
            links.append((direct or options)[0])
    return list(dict.fromkeys(links))[:10]


def inspect(url):
    final = url
    try:
        html, final = fetch(url)
        return {"product": parse_page(html, final)}
    except (requests.RequestException, ValueError, OSError, KeyError) as error:
        reason = str(error) if isinstance(error, ComparisonError) else "تعذرت قراءة بيانات منتج واحد من الصفحة"
        return {"url": url, "final_url": final, "reason": reason}


def read_source(url, key):
    """Try direct extraction, then the supported Amazon API for a blocked ASIN."""
    host = (urlparse(url).hostname or "").lower()
    asin = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})(?:[/?]|$)", url, re.I)
    is_amazon = host in {"amazon.sa", "www.amazon.sa"} and asin is not None
    canonical = f"https://www.amazon.sa/dp/{asin.group(1).upper()}" if is_amazon else url
    try:
        html, final = fetch(canonical)
        return parse_page(html, final)
    except (ValueError, requests.RequestException, OSError, KeyError):
        if not is_amazon:
            raise
    try:
        r = requests.get("https://serpapi.com/search.json", timeout=75,
                         params={"engine": "amazon_product", "amazon_domain": "amazon.sa",
                                 "asin": asin.group(1).upper(), "api_key": key})
        if r.status_code != 200:
            raise ComparisonError(f"تعذرت القراءة المباشرة من أمازون، والمسار البديل SerpApi أعاد HTTP {r.status_code}. تحقق من دعم amazon_product في خطتك والرصيد.")
        data = r.json()
    except requests.RequestException:
        raise ComparisonError("تعذرت القراءة المباشرة من أمازون ثم تعذر الاتصال بمسار SerpApi البديل.") from None
    if data.get("error"):
        raise ComparisonError("تعذرت القراءة المباشرة من أمازون، وخدمة SerpApi رفضت طلب amazon_product؛ تحقق من الخطة والرصيد.")
    p = data.get("product_results", {})
    if not isinstance(p, dict) or p.get("asin", "").upper() != asin.group(1).upper():
        raise ComparisonError("مسار أمازون البديل لم يُرجع نفس ASIN المطلوب؛ أوقفت المقارنة لتجنب مقارنة نسخة مختلفة.")
    if not p.get("title"):
        raise ComparisonError("مسار أمازون البديل لم يُرجع اسم المنتج؛ لم تبدأ المقارنة.")
    # Only map explicit identity fields. Do not treat ASIN or retailer SKU as a GTIN.
    brand = clean_brand(p.get("brand", ""))
    identity = {k: p[k] for k in ("gtin", "gtin8", "gtin12", "gtin13", "gtin14") if p.get(k)}
    fields = {"model": p.get("model", ""), "mpn": p.get("mpn", "")}
    # Product details only; recommendations can contain unrelated identifiers.
    aliases = {"item_model_number": "model", "model_number": "model", "model": "model",
               "manufacturer_part_number": "mpn", "mpn": "mpn", "upc": "gtin12", "ean": "gtin13"}
    for section in ("product_details", "technical_details", "specifications"):
        for node in walk(p.get(section, {})):
            for label, value in node.items():
                name = aliases.get(norm(label).replace(" ", "_"))
                if name and isinstance(value, (str, int)):
                    if name.startswith("gtin"):
                        identity[name] = value
                    elif not fields[name]:
                        fields[name] = value
    return {"title": str(p["title"]), "brand": norm(brand), "mpn": norm(fields["mpn"]),
            "model": norm(fields["model"]), "gtins": gtins(identity),
            "attributes": {k: norm(p[k]) for k in ("color", "size", "material", "pattern") if p.get(k)},
            "offer": None, "url": canonical, "store": "amazon.sa", "extraction": "serpapi_amazon_product"}


def md(value):
    return re.sub(r"[\r\n|\[\]<>`*]", " ", str(value))[:300]


def same_named_product(source, candidate):
    """Expose readable prices separately; never promote a name match to exact SKU."""
    if not source["brand"] or source["brand"] != candidate["brand"] or not candidate["offer"]:
        return False
    sa, ca = variant_specs(source), variant_specs(candidate)
    if any(sa[k] != ca[k] for k in sa.keys() & ca.keys()):
        return False
    for field in ("model", "mpn"):
        if source[field] and candidate[field] and model_key(source[field]) != model_key(candidate[field]):
            return False
    def name(p):
        title = norm(p["title"])
        title = re.sub(r"(?<!\w)" + re.escape(p["brand"]) + r"(?!\w)", " ", title)
        for identifier in (p["model"], p["mpn"]):
            if valid_model(identifier):
                title = re.sub(r"(?<!\w)" + re.escape(norm(identifier)) + r"(?!\w)", " ", title)
        color = norm(p["attributes"].get("color"))
        if color:
            title = re.sub(r"(?<!\w)" + re.escape(color) + r"(?!\w)", " ", title)
        return re.findall(r"\w+", title)
    a, b = name(source), name(candidate)
    return len(a) >= 4 and a == b


def render(report):
    lines = ["# مقارنة أسعار المنتج", "", md(report["source"]["title"]), "",
             f"وقت الفحص UTC: {report['checked_at']}", "",
             "الأسعار من صفحات المتاجر، بالريال السعودي، بدون الشحن.", ""]
    if report["matches"]:
        lines.extend(["| المتجر | السعر | أساس المطابقة | رابط المنتج |", "|---|---:|---|---|"])
        for p in report["matches"]:
            url = p["url"].replace("(", "%28").replace(")", "%29").replace(" ", "%20")
            lines.append(f"| {md(p['store'])} | {p['offer']['price']} | {md(p.get('match_reason', ''))} | [فتح المنتج]({url}) |")
    else:
        lines.append("لم أجد نتائج قدرت أثبت تطابقها وسعرها وحالتها وتوفرها. هذا لا يعني أنه لا يوجد سعر أرخص.")
        if not report["source"]["gtins"]:
            lines.extend(["", "لا يوجد باركود صالح بالمصدر؛ جرت محاولة المطابقة بالماركة ورقم الموديل الكامل ومواصفات النسخة. الاسم وحده لا يكفي."])
    pending = report.get("pending", [])
    if pending:
        lines.extend(["", "## أسعار قُرئت وتحتاج تأكيد النسخة", "",
                      "تطابق الاسم الكامل والماركة، لكن بيانات الموديل أو الحالة أو التوفر غير كافية؛ ليست نتائج مطابقة مؤكدة.", "",
                      "| المتجر | السعر بالريال | سبب عدم التأكيد | الرابط |", "|---|---:|---|---|"])
        for p in pending:
            url = p["url"].replace("(", "%28").replace(")", "%29").replace(" ", "%20")
            lines.append(f"| {md(p['store'])} | {p['offer']['price']} | {md(p['match_reason'])} | [فتح المنتج]({url}) |")
    lines.extend(["", f"فُحصت {report['checked']} صفحة مرشحة. المستبعدة: {len(report['excluded'])}. تحتاج تأكيدًا: {len(pending)}.",
                  "", "المطابقة تعتمد على بيانات المتجر وقد تكون بياناته خاطئة؛ تحقق من الصفحة قبل الشراء."])
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=os.environ.get("PRODUCT_URL"))
    ap.add_argument("--output", default="reports")
    args = ap.parse_args()
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    stage = "الإعداد"
    try:
        if not args.url:
            raise ComparisonError("أدخل رابط المنتج في خانة product_url")
        key = os.environ.get("SERPAPI_API_KEY", "").strip()
        if not key:
            raise ComparisonError("مفتاح البحث غير موجود. أضف SERPAPI_API_KEY في GitHub Secrets أولًا")
        stage = "قراءة بيانات المنتج (مباشرة أو عبر المسار البديل)"
        source = read_source(args.url, key)
        final = source["url"]
        stage = "البحث عن المتاجر"
        urls = [u for u in search(source, key) if u.rstrip("/") != final.rstrip("/")]
        report = {"source": source, "checked_at": datetime.now(timezone.utc).isoformat(),
                  "matches": [], "pending": [], "excluded": [], "checked": len(urls)}
        seen = {final}
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            for result in pool.map(inspect, urls):
                candidate = result.get("product")
                if not candidate:
                    report["excluded"].append(result)
                    continue
                if candidate["url"] in seen:
                    continue
                seen.add(candidate["url"])
                ok, reason = match(source, candidate)
                if ok:
                    candidate["match_reason"] = reason
                    report["matches"].append(candidate)
                elif same_named_product(source, candidate):
                    stock = candidate["offer"].get("availability", "")
                    stock_note = "؛ نافد" if stock == "OutOfStock" else ("؛ التوفر غير محدد" if stock != "InStock" else "")
                    candidate["match_reason"] = reason + stock_note
                    report["pending"].append(candidate)
                else:
                    report["excluded"].append({"url": candidate["url"], "reason": reason})
        report["matches"].sort(key=lambda p: Decimal(p["offer"]["price"]))
        report["pending"].sort(key=lambda p: Decimal(p["offer"]["price"]))
        text = render(report)
        (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        (out / "report.md").write_text(text, encoding="utf-8")
    except (ValueError, requests.RequestException, OSError, KeyError) as error:
        # HTTP errors can contain private URL parameters. Keep logs and reports generic.
        if isinstance(error, ComparisonError):
            reason = str(error)
        elif isinstance(error, requests.Timeout):
            reason = "انتهت مهلة الاتصال بصفحة المنتج. المتجر لم يستجب في الوقت المحدد؛ قد يرفض الاتصالات الآلية."
        elif stage == "استخراج بيانات المنتج":
            reason = "الصفحة لا تحتوي بيانات منتج واحد واضحة بصيغة JSON-LD المدعومة حاليًا. قد تحتاج JavaScript أو قارئًا مخصصًا لهذا المتجر."
        else:
            reason = "تعذر إتمام هذه المرحلة. تحقق من الرابط والاتصال؛ لا تغيّر المفتاح دون ظهور خطأ من خدمة البحث."
        text = f"# تعذرت المقارنة\n\nالمرحلة: {stage}\n\n{reason}\n"
        (out / "error.json").write_text(json.dumps({"stage": stage, "reason": reason}, ensure_ascii=False, indent=2), encoding="utf-8")
        (out / "report.md").write_text(text, encoding="utf-8")
        print(text)
        return 1
    finally:
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary and (out / "report.md").exists():
            with open(summary, "a", encoding="utf-8") as f:
                f.write((out / "report.md").read_text(encoding="utf-8"))
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
