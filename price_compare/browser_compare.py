"""Browser-first experiment: Google search, rendered store pages and evidence."""
import argparse
import json
import os
import re
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as BrowserTimeout

from compare import (ComparisonError, clean_brand, match, norm, parse_page, public_url,
                     read_source, render, same_named_product, social_link)


def blocked(page):
    text = norm(page.locator('body').inner_text(timeout=10000)[:6000])
    return any(s in text for s in ('unusual traffic', 'verify you are human', 'not a robot',
                                   'enter the characters you see', 'أثبت أنك', 'تحقق من أنك')) or '/sorry/' in page.url


def visit(page, url):
    public_url(url)
    page.goto(url, wait_until='domcontentloaded', timeout=35000)
    try:
        page.wait_for_load_state('networkidle', timeout=6000)
    except BrowserTimeout:
        pass
    if blocked(page):
        raise ComparisonError('صفحة تحقق أو CAPTCHA؛ لم يتم تجاوزها')


def google_links(html, base):
    soup = BeautifulSoup(html, 'html.parser')
    links = []
    for heading in soup.find_all('h3'):
        a = heading.find_parent('a', href=True)
        if not a:
            continue
        link = urljoin(base, a['href'])
        p = urlparse(link)
        if p.hostname in {'google.com', 'www.google.com'} and p.path == '/url':
            link = parse_qs(p.query).get('q', parse_qs(p.query).get('url', ['']))[0]
        p = urlparse(link)
        if p.scheme in {'https', 'http'} and p.hostname and not social_link(link):
            if p.hostname not in {'google.com', 'www.google.com'} or p.path == '/goto':
                links.append(link)
    return list(dict.fromkeys(links))


def rendered_product(page):
    html, url = page.content(), page.url
    try:
        return parse_page(html, url)
    except ValueError:
        pass
    # Fallback limited to an explicitly marked product page and a unique price.
    soup = BeautifulSoup(html, 'html.parser')
    title = soup.select_one('h1')
    kind = soup.find('meta', property='og:type')
    if not title or not kind or kind.get('content') != 'product':
        raise ComparisonError('الصفحة المحمّلة لا تعرض منتجًا واحدًا ببيانات قابلة للتأكيد')
    price = soup.find('meta', property='product:price:amount') or soup.find('meta', itemprop='price')
    currency = soup.find('meta', property='product:price:currency') or soup.find('meta', itemprop='priceCurrency')
    if not price or not currency or currency.get('content') != 'SAR':
        raise ComparisonError('لم أجد سعر المنتج بعملة SAR؛ لم أستخدم أسعار التوصيات أو الأقساط')
    p = {'@type': 'Product', 'name': title.get_text(' ', strip=True),
         'offers': {'@type': 'Offer', 'price': price.get('content'), 'priceCurrency': 'SAR'}}
    brand = soup.find('meta', property='product:brand')
    p['brand'] = clean_brand(brand.get('content')) if brand else ''
    return parse_page('<script type="application/ld+json">' + json.dumps(p) + '</script>', url)


def run(url, output, headed=False):
    output.mkdir(parents=True, exist_ok=True)
    report = {'checked_at': datetime.now(timezone.utc).isoformat(), 'source': {},
              'matches': [], 'pending': [], 'excluded': [], 'checked': 0,
              'mode': 'chromium', 'searches': [], 'browser_search_links': 0}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not headed)
        context = browser.new_context(locale='en-US', viewport={'width': 1440, 'height': 1000})
        # Validate redirected and embedded network destinations too.
        def guard(route):
            try:
                if urlparse(route.request.url).scheme in {'http', 'https'}:
                    public_url(route.request.url)
                if route.request.resource_type in {'media', 'font'}:
                    route.abort()
                else:
                    route.continue_()
            except (ValueError, OSError):
                route.abort()
        context.route('**/*', guard)
        page = context.new_page()
        try:
            visit(page, url)
            source = rendered_product(page)
            report['source_method'] = 'browser'
        except Exception as error:
            report['source_browser_error'] = type(error).__name__
            key = os.environ.get('SERPAPI_API_KEY', '')
            if not key:
                raise ComparisonError('تعذرت قراءة المصدر بالمتصفح؛ راجع صورة source.png. المسار البديل اختياري ويتطلب مفتاح البحث') from None
            source = read_source(url, key)
            report['source_method'] = 'explicit_api_fallback'
        finally:
            page.screenshot(path=str(output / 'source.png'), full_page=False)
        report['source'] = source
        links = []
        for language in ('en', 'ar'):
            query = norm(source['title']) + (' Saudi Arabia price' if language == 'en' else ' سعر السعودية')
            entry = {'language': language, 'query': query}
            try:
                visit(page, 'https://www.google.com/search?' + urlencode({'q': query, 'hl': language, 'gl': 'sa', 'udm': '14'}))
                found = google_links(page.content(), page.url)
                entry.update(status='read', links=len(found))
                links.extend(found)
            except Exception as error:
                entry.update(status='blocked_or_unreadable', reason=str(error) if isinstance(error, ComparisonError) else type(error).__name__)
            page.screenshot(path=str(output / f'google-{language}.png'), full_page=False)
            report['searches'].append(entry)
        links = list(dict.fromkeys(links))[:20]
        report['browser_search_links'] = len(links)
        seen = {source['url'].rstrip('/')}
        for index, link in enumerate(links):
            report['checked'] += 1
            try:
                visit(page, link)
                if social_link(page.url):
                    continue
                canonical = page.url.rstrip('/')
                if canonical in seen:
                    continue
                seen.add(canonical)
                candidate = rendered_product(page)
                candidate['evidence'] = f'store-{index}.png'
                page.screenshot(path=str(output / candidate['evidence']), full_page=False)
                ok, reason = match(source, candidate)
                candidate['match_reason'] = reason
                if ok:
                    report['matches'].append(candidate)
                elif same_named_product(source, candidate):
                    report['pending'].append(candidate)
                else:
                    report['excluded'].append({'url': page.url, 'reason': reason})
            except Exception as error:
                report['excluded'].append({'url': link, 'reason': str(error) if isinstance(error, ComparisonError) else type(error).__name__})
        browser.close()
    for key in ('matches', 'pending'):
        report[key].sort(key=lambda p: Decimal(p['offer']['price']))
    text = render(report)
    if not report['browser_search_links']:
        text += '\nلم يتمكن المتصفح من قراءة روابط نتائج Google. راجع صور البحث؛ هذا ليس إثباتًا لعدم توفر المنتج.\n'
    (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    (output / 'report.md').write_text(text, encoding='utf-8')
    print(text)
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as f:
            f.write(text)
    return 0 if report['browser_search_links'] and (report['matches'] or report['pending']) else 2


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--url', default=os.environ.get('PRODUCT_URL'), required=not bool(os.environ.get('PRODUCT_URL')))
    parser.add_argument('--output', type=Path, default=Path('reports'))
    parser.add_argument('--headed', action='store_true')
    args = parser.parse_args()
    raise SystemExit(run(args.url, args.output, args.headed))
