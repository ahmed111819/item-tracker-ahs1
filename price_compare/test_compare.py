import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from compare import ComparisonError, fetch, main, match, parse_page, public_url, render, search


def product(**updates):
    p = {"@type": "Product", "name": "Example headphones Black", "brand": {"name": "Example"},
         "gtin13": "4006381333931", "mpn": "HP-100-B", "color": "Black",
         "offers": {"@type": "Offer", "price": "120.50", "priceCurrency": "SAR",
                    "availability": "https://schema.org/InStock",
                    "itemCondition": "https://schema.org/NewCondition"}}
    p.update(updates)
    return p


def page(p):
    return '<script type="application/ld+json">' + json.dumps(p) + '</script>'


def parsed(**updates):
    return parse_page(page(product(**updates)), "https://shop.example/item")


class MatchingTests(unittest.TestCase):
    def test_blocked_store_has_specific_safe_error(self):
        with patch("compare.public_url"), patch("compare.requests.get") as get:
            response = get.return_value.__enter__.return_value
            response.is_redirect = False
            response.status_code = 403
            with self.assertRaises(ComparisonError) as error:
                fetch("https://store.example/product?private=do-not-log")
            self.assertIn("403", str(error.exception))
            self.assertNotIn("do-not-log", str(error.exception))

    def test_same_gtin_matches(self):
        self.assertTrue(match(parsed(), parsed())[0])

    def test_similar_name_not_enough(self):
        self.assertFalse(match(parsed(), parsed(gtin13=""))[0])

    def test_invalid_barcode_is_not_evidence(self):
        self.assertEqual(parsed(gtin13="4006381333932")["gtins"], [])

    def test_conflicting_variant_rejected(self):
        self.assertFalse(match(parsed(), parsed(color="White"))[0])

    def test_missing_variant_rejected(self):
        self.assertFalse(match(parsed(), parsed(color=""))[0])

    def test_refurbished_rejected(self):
        candidate = parsed()
        candidate["offer"]["condition"] = "RefurbishedCondition"
        self.assertFalse(match(parsed(), candidate)[0])

    def test_unknown_condition_rejected(self):
        candidate = parsed()
        candidate["offer"]["condition"] = ""
        self.assertFalse(match(parsed(), candidate)[0])

    def test_foreign_currency_stock_and_aggregate_rejected(self):
        for update in ({"priceCurrency": "USD"}, {"availability": "https://schema.org/OutOfStock"},
                       {"@type": "AggregateOffer"}, {"price": "NaN"}, {"price": "-1"},
                       {"availability": ""}):
            with self.subTest(update=update):
                offer = product()["offers"] | update
                self.assertIsNone(parsed(offers=offer)["offer"])

    def test_multiple_variants_not_cheapest(self):
        with self.assertRaises(ValueError):
            parse_page(page([product(), product(color="White")]), "https://shop.example/item")

    def test_multiple_prices_not_cheapest(self):
        offers = [product()["offers"], product()["offers"] | {"price": "50"}]
        self.assertIsNone(parsed(offers=offers)["offer"])

    def test_graph_and_bad_script(self):
        html = '<script type="application/ld+json">broken</script>' + page({"@graph": [product()]})
        self.assertEqual(parse_page(html, "https://shop.example/item")["offer"]["price"], "120.50")

    def test_private_destination_blocked(self):
        with patch("compare.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("127.0.0.1", 443))]):
            with self.assertRaises(ValueError):
                public_url("https://localhost/item")

    def test_search_errors_do_not_leak_key(self):
        with patch("compare.requests.get") as get:
            get.return_value.status_code = 403
            with self.assertRaises(ValueError) as error:
                search(parsed(), "secret-test-token")
            self.assertNotIn("secret-test-token", str(error.exception))

    def test_empty_report_is_explicit(self):
        text = render({"source": parsed(), "checked_at": "test", "matches": [], "excluded": [], "checked": 0})
        self.assertIn("لم أجد", text)

    def test_end_to_end_sorts_and_excludes_different_product(self):
        cheap, expensive, wrong = parsed(), parsed(), parsed(color="White")
        cheap["url"], cheap["offer"]["price"] = "https://cheap.example/item", "80"
        expensive["url"], expensive["offer"]["price"] = "https://expensive.example/item", "150"
        wrong["url"] = "https://wrong.example/item"
        with tempfile.TemporaryDirectory() as folder:
            with patch.dict(os.environ, {"SERPAPI_API_KEY": "fake"}), \
                 patch("sys.argv", ["compare", "--url", "https://source.example/item", "--output", folder]), \
                 patch("compare.fetch", return_value=(page(product()), "https://source.example/item")), \
                 patch("compare.search", return_value=[p["url"] for p in [expensive, cheap, wrong]]), \
                 patch("compare.inspect", side_effect=lambda u: {"product": next(p for p in [expensive, cheap, wrong] if p["url"] == u)}), \
                 patch("builtins.print"):
                self.assertEqual(main(), 0)
            report = json.loads((Path(folder) / "report.json").read_text(encoding="utf-8"))
            self.assertEqual([p["offer"]["price"] for p in report["matches"]], ["80", "150"])
            self.assertEqual(len(report["excluded"]), 1)


if __name__ == "__main__":
    unittest.main()
