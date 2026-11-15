import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from compare import ComparisonError, clean_brand, fetch, main, match, parse_page, public_url, read_source, render, same_named_product, search


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
    def test_brand_store_label_is_not_brand_name(self):
        self.assertEqual(clean_brand("Visit the Apple Store"), "apple")
        self.assertEqual(clean_brand({"name": "Brand: Cello"}), "cello")

    def test_direct_search_link_preferred_over_google_wrapper(self):
        with patch("compare.requests.get") as get:
            get.return_value.status_code = 200
            get.return_value.json.return_value = {"organic_results": [{"link": "https://www.google.com/goto?url=opaque", "redirect_link": "https://shop.example/item"}]}
            self.assertEqual(search(parsed(), "fake"), ["https://shop.example/item"])

    def test_name_match_price_is_pending_not_confirmed(self):
        a = parsed(gtin13="", mpn="", name="Example iPhone Air MagSafe Battery")
        b = parsed(gtin13="", mpn="", name="iPhone Air MagSafe Battery")
        self.assertTrue(same_named_product(a, b))
        self.assertFalse(match(a, b)[0])
        b["title"] += " Case"
        self.assertFalse(same_named_product(a, b))

    def test_known_color_conflict_not_even_pending(self):
        a = parsed(gtin13="", mpn="", name="Example iPhone Air MagSafe Battery", color="Black")
        b = parsed(gtin13="", mpn="", name="Example iPhone Air MagSafe Battery", color="White")
        self.assertFalse(same_named_product(a, b))

    def test_hnak_primary_payload_reader(self):
        p = {"type": "PRODUCT", "relative_url": "example-product", "name": "Example headphones",
             "mpbrand": "Example", "stock_status": "IN_STOCK",
             "price_range": {"minimum_price": {"final_price": {"value": 449, "currency": "SAR"}},
                             "maximum_price": {"final_price": {"value": 449, "currency": "SAR"}}},
             "additional_information": [{"attributes": [{"label": "Model Number", "value": "HP-100-B"},
                                                          {"label": "Bar code", "value": "4006381333931"}]}]}
        payload = "3f:" + json.dumps(["$", "$L40", None, {"productData": p}])
        html = '<script>self.__next_f.push(' + json.dumps([1, payload]) + ')</script>'
        result = parse_page(html, "https://www.hnak.com/en-sa/example-product")
        self.assertEqual(result["offer"]["price"], "449")
        self.assertEqual(result["model"], "hp-100-b")
        self.assertEqual(result["gtins"], ["04006381333931"])
        with self.assertRaises(ValueError):
            parse_page(html, "https://www.hnak.com/en-sa/different-product")

    def test_omitted_product_type_requires_product_metadata(self):
        p = product()
        del p["@type"]
        p["url"] = "https://shop.example/item"
        html = '<meta property="og:type" content="product">' + page(p)
        self.assertEqual(parse_page(html, p["url"])["offer"]["price"], "120.50")
        with self.assertRaises(ValueError):
            parse_page(page(p), p["url"])

    def test_amazon_block_uses_api_and_checks_asin(self):
        with patch("compare.fetch", side_effect=ComparisonError("blocked")), patch("compare.requests.get") as get:
            get.return_value.status_code = 200
            get.return_value.json.return_value = {"product_results": {"asin": "B0C6DRSLCG", "title": "Cello pens", "brand": "Cello"}}
            p = read_source("https://www.amazon.sa/dp/B0C6DRSLCG?ref=tracking", "fake")
            self.assertEqual(p["title"], "Cello pens")
            self.assertEqual(get.call_args.kwargs["params"]["engine"], "amazon_product")
            get.return_value.json.return_value["product_results"]["asin"] = "B000000000"
            with self.assertRaises(ComparisonError):
                read_source("https://www.amazon.sa/dp/B0C6DRSLCG", "fake")

    def test_amazon_without_jsonld_reads_selected_product(self):
        html = '''<span id="productTitle">Cello Box 50 Black</span>
        <a id="bylineInfo">Brand: Cello</a>
        <div id="corePriceDisplay_desktop_feature_div">
          <span class="a-price"><span class="a-offscreen">SAR 15.78</span></span>
          <span class="a-price a-text-price"><span class="a-offscreen">SAR 39.00</span></span>
        </div><div id="availability">In Stock</div>'''
        p = parse_page(html, "https://www.amazon.sa/dp/B0C6DRSLCG")
        self.assertEqual(p["title"], "Cello Box 50 Black")
        self.assertEqual(p["offer"]["price"], "15.78")
        self.assertEqual(p["gtins"], [])
        self.assertEqual(p["offer"]["condition"], "")

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
        self.assertFalse(match(parsed(gtin13="", mpn=""), parsed(gtin13="", mpn=""))[0])

    def test_model_brand_and_variants_match_without_gtin(self):
        self.assertTrue(match(parsed(gtin13=""), parsed(gtin13=""))[0])

    def test_source_price_not_required(self):
        source = parsed(gtin13="")
        source["offer"] = None
        self.assertTrue(match(source, parsed(gtin13=""))[0])

    def test_pack_and_tip_size_conflicts(self):
        source = parsed(gtin13="", name="Example HP-100-B Black 0.7mm Box of 50Pc")
        for title in ("Example HP-100-B Black 0.7mm Box of 10Pc", "Example HP-100-B Black 0.5mm Box of 50Pc",
                      "Example HP-100-B Black 0.7mm"):
            self.assertFalse(match(source, parsed(gtin13="", name=title))[0])

    def test_arabic_english_variant_equivalence(self):
        a = parsed(gtin13="", name="Example Black 0.7mm Box of 50Pc")
        b = parsed(gtin13="", name="Example أسود ٠٫٧ مم علبة ٥٠ قلم", color="أسود")
        # Use a decimal point recognized in both Arabic and English.
        b["title"] = "Example أسود 0.7 مم علبة ٥٠ قلم"
        self.assertTrue(match(a, b)[0])

    def test_bilingual_search_deduplicates(self):
        with patch("compare.requests.get") as get:
            get.return_value.status_code = 200
            get.return_value.json.return_value = {"organic_results": [{"link": "https://shop.example/p"}]}
            self.assertEqual(search(parsed(), "fake"), ["https://shop.example/p"])
            self.assertEqual([c.kwargs["params"]["hl"] for c in get.call_args_list], ["en", "ar"])
            self.assertIn("Saudi Arabia", get.call_args_list[0].kwargs["params"]["q"])

    def test_invalid_barcode_is_not_evidence(self):
        self.assertEqual(parsed(gtin13="4006381333932")["gtins"], [])

    def test_conflicting_variant_rejected(self):
        self.assertFalse(match(parsed(), parsed(color="White"))[0])

    def test_missing_variant_rejected(self):
        self.assertFalse(match(parsed(gtin13=""), parsed(gtin13="", color="", name="Example headphones"))[0])

    def test_refurbished_rejected(self):
        candidate = parsed()
        candidate["offer"]["condition"] = "RefurbishedCondition"
        self.assertFalse(match(parsed(), candidate)[0])

    def test_unknown_condition_rejected(self):
        candidate = parsed()
        candidate["offer"]["condition"] = ""
        self.assertFalse(match(parsed(), candidate)[0])

    def test_foreign_currency_stock_and_aggregate_rejected(self):
        for update in ({"priceCurrency": "USD"},
                       {"@type": "AggregateOffer"}, {"price": "NaN"}, {"price": "-1"},
                       ):
            with self.subTest(update=update):
                offer = product()["offers"] | update
                self.assertIsNone(parsed(offers=offer)["offer"])

    def test_unknown_or_out_of_stock_never_confirmed(self):
        for stock in ("", "https://schema.org/OutOfStock"):
            candidate = parsed(offers=product()["offers"] | {"availability": stock})
            self.assertIsNotNone(candidate["offer"])
            self.assertFalse(match(parsed(), candidate)[0])

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
