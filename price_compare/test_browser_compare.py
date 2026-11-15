import unittest
from unittest.mock import Mock, patch

from browser_compare import blocked, discover_store_links, google_links, native_browser_search, rendered_product


class BrowserTests(unittest.TestCase):
    def test_native_search_uses_store_results_without_api(self):
        from pathlib import Path
        import tempfile
        page = Mock()
        page.url = 'https://www.apple.com/sa-en/search/test'
        page.content.return_value = '<a href="https://www.apple.com/sa-en/shop/product/actual/item">Item</a>'
        with tempfile.TemporaryDirectory() as folder, patch('browser_compare.visit'), patch('browser_compare.search', side_effect=AssertionError('Paid API used')):
            links, diagnostics = native_browser_search(page, {'title': 'Apple test product', 'brand': 'apple', 'model': '', 'mpn': ''}, Path(folder))
            self.assertEqual(links, ['https://www.apple.com/sa-en/shop/product/actual/item'])
            self.assertEqual(len(diagnostics), 3)
    def test_service_discovery_never_returns_google_wrappers_or_social(self):
        with patch('browser_compare.search', return_value=['https://www.google.com/goto?url=opaque',
                   'https://facebook.com/post', 'https://shop.example/product', 'https://shop.example/product']):
            self.assertEqual(discover_store_links({}, 'fake'), ['https://shop.example/product'])

    def test_google_result_links_only(self):
        html = '''<a href="https://shop.example/p"><h3>Product</h3></a>
        <a href="/url?q=https%3A%2F%2Fother.example%2Fp"><h3>Other</h3></a>
        <a href="https://facebook.com/post"><h3>Social</h3></a>
        <a href="https://unrelated.example">Footer</a>'''
        self.assertEqual(google_links(html, 'https://www.google.com/search?q=test'),
                         ['https://shop.example/p', 'https://other.example/p'])

    def test_challenge_is_detected(self):
        page = Mock()
        page.url = 'https://www.google.com/sorry/index'
        page.locator.return_value.inner_text.return_value = 'Unusual traffic'
        self.assertTrue(blocked(page))

    def test_no_arbitrary_visible_number_as_price(self):
        page = Mock()
        page.url = 'https://shop.example/item'
        page.content.return_value = '<h1>Headphones</h1><p>Pay 4 installments of 25</p>'
        with self.assertRaises(ValueError):
            rendered_product(page)
