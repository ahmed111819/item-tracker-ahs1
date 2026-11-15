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
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(value or "")).casefold()).strip()


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
                          headers={"User-Agent": "PriceCompareExperiment/0.1", "Accept-Language": "en,ar;q=0.8"}) as r:
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
    return name in (t if isinstance(t, list) else [t])


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


def parse_page(html, url):
    soup = BeautifulSoup(html, "html.parser")
    nodes = []
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            nodes.extend(walk(json.loads(script.string or script.get_text())))
        except (ValueError, TypeError):
            continue
    products = [n for n in nodes if typename(n, "Product")]
    # Product-list pages and ambiguous variant groups must not choose the cheapest sibling.
    unique = {json.dumps(p, sort_keys=True, ensure_ascii=False): p for p in products}
    products = list(unique.values())
    if len(products) > 1:
        exact = [p for p in products if str(p.get("url", "")).rstrip("/") == url.rstrip("/")]
        products = exact if len(exact) == 1 else []
    if not products:
        amazon = amazon_product(soup, url)
        if amazon:
            products = [amazon]
        else:
            raise ValueError("لم أجد بيانات منتج واحد واضحة؛ قد تكون الصفحة محمية أو تحتاج JavaScript")
    p = products[0]
    brand = p.get("brand", "")
    if isinstance(brand, dict):
        brand = brand.get("name", "")
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
        # Unknown stock is not silently presented as purchasable.
        if availability != "InStock":
            continue
        prices.append({"price": str(price), "condition": condition})
    # Multiple different offer conditions/prices may be a variant aggregate.
    offer = prices[0] if prices and all(x == prices[0] for x in prices) else None
    attrs = {k: norm(p[k]) for k in ("color", "size", "material", "pattern") if p.get(k)}
    return {"title": str(p.get("name", "")), "brand": norm(brand),
            "mpn": norm(p.get("mpn", "")), "model": norm(p.get("model", "")),
            "gtins": gtins(p), "attributes": attrs, "offer": offer, "url": url,
            "store": urlparse(url).hostname.removeprefix("www.")}


def match(source, candidate):
    # Family/model matches alone do not prove color, capacity, region or pack quantity.
    if not set(source["gtins"]) & set(candidate["gtins"]):
        return False, "لا يوجد باركود عالمي متطابق؛ الاسم أو الموديل وحده لا يكفي"
    if source["brand"] and candidate["brand"] and source["brand"] != candidate["brand"]:
        return False, "اختلاف الماركة"
    for k in ("mpn", "model"):
        if source[k] and candidate[k] and source[k] != candidate[k]:
            return False, "اختلاف رقم الشركة أو الموديل"
    if source["attributes"] != candidate["attributes"]:
        return False, "المواصفات مختلفة أو ناقصة"
    a, b = source["offer"], candidate["offer"]
    if not a or not b:
        return False, "السعر بالريال أو التوفر أو العرض المحدد غير مؤكد"
    if not a["condition"] or a["condition"] != b["condition"]:
        return False, "حالة المنتج مختلفة أو غير محددة"
    return True, "باركود عالمي متطابق ومواصفات وحالة متوافقة في الصفحة"


def search(source, key):
    identity = " ".join([source["brand"], source["mpn"] or source["model"]])
    query = source["gtins"][0] if source["gtins"] else identity
    if not query.strip():
        query = source["title"][:160]
    try:
        r = requests.get("https://serpapi.com/search.json", timeout=35,
                         params={"engine": "google", "q": query + " شراء السعودية",
                                 "gl": "sa", "hl": "ar", "num": 10, "api_key": key})
        # Never include the prepared URL (which contains the API key) in an error.
        if r.status_code != 200:
            raise ComparisonError(f"خدمة البحث أعادت HTTP {r.status_code}. راجع مفتاح SerpApi والرصيد في حسابك.")
        data = r.json()
    except requests.RequestException:
        raise ComparisonError("تعذر الاتصال بخدمة البحث؛ حاول لاحقًا") from None
    if data.get("error"):
        raise ComparisonError("رفضت خدمة البحث الطلب؛ راجع المفتاح والرصيد")
    return list(dict.fromkeys(x["link"] for x in data.get("organic_results", [])
                             if isinstance(x.get("link"), str)))[:10]


def inspect(url):
    try:
        html, final = fetch(url)
        return {"product": parse_page(html, final)}
    except (requests.RequestException, ValueError, OSError, KeyError):
        return {"url": url, "reason": "تعذرت قراءة صفحة منتج واضحة"}


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
    brand = p.get("brand", "")
    if isinstance(brand, dict):
        brand = brand.get("name", "")
    identity = {k: p[k] for k in ("gtin", "gtin8", "gtin12", "gtin13", "gtin14") if p.get(k)}
    return {"title": str(p["title"]), "brand": norm(brand), "mpn": norm(p.get("mpn")),
            "model": norm(p.get("model")), "gtins": gtins(identity),
            "attributes": {k: norm(p[k]) for k in ("color", "size", "material", "pattern") if p.get(k)},
            "offer": None, "url": canonical, "store": "amazon.sa", "extraction": "serpapi_amazon_product"}


def md(value):
    return re.sub(r"[\r\n|\[\]<>`*]", " ", str(value))[:300]


def render(report):
    lines = ["# مقارنة أسعار المنتج", "", md(report["source"]["title"]), "",
             f"وقت الفحص UTC: {report['checked_at']}", "",
             "الأسعار من صفحات المتاجر، بالريال السعودي، بدون الشحن.", ""]
    if report["matches"]:
        lines.extend(["| المتجر | السعر | رابط المنتج |", "|---|---:|---|"])
        for p in report["matches"]:
            url = p["url"].replace("(", "%28").replace(")", "%29").replace(" ", "%20")
            lines.append(f"| {md(p['store'])} | {p['offer']['price']} | [فتح المنتج]({url}) |")
    else:
        lines.append("لم أجد نتائج قدرت أثبت تطابقها وسعرها وحالتها وتوفرها. هذا لا يعني أنه لا يوجد سعر أرخص.")
        if not report["source"]["gtins"]:
            lines.extend(["", "صفحة المصدر لا تعرض باركودًا عالميًا صالحًا؛ تم البحث عن المنتج، لكن الاسم وحده لا يثبت التطابق في هذه النسخة."])
    lines.extend(["", f"فُحصت {report['checked']} صفحة مرشحة. المستبعدة: {len(report['excluded'])}.",
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
                  "matches": [], "excluded": [], "checked": len(urls)}
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
                    report["matches"].append(candidate)
                else:
                    report["excluded"].append({"url": candidate["url"], "reason": reason})
        report["matches"].sort(key=lambda p: Decimal(p["offer"]["price"]))
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
