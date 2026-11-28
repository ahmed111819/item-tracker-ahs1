import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from promotion_digest import (
    CACHE_SECONDS, Promotion, build_messages, load_discoveries, load_observed_asins,
    PROMOTION_RESULTS, overlap_blocks, promotion_hint, promotion_link, record_promotion_links,
    refresh_discoveries, saving_for, watch_discoveries,
)
from scan_link_report import summarize


class PromotionDigestTests(unittest.TestCase):
    def test_observations_deduplicate_across_bots_and_keep_report_compatible(self):
        url = "https://www.amazon.sa/promotion/psp/A1WSIDJI0UEOX0"
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"SCAN_REPORT_DIR": temp}):
            for bot, link in (
                ("bot1", f"{url}?redirectAsin=B0DWT7PWHN"),
                ("bot2", url.replace("/promotion", "/-/en/promotion")),
                ("bot3", "https://www.amazon.sa/dp/B0DWT7PWHN"),
            ):
                record_promotion_links(bot, [{
                    "asin": "B0DWT7PWHN", "title": "Comfort", "price_text": "SAR 50",
                    "promotion_links": [link],
                }])
            self.assertEqual(list(summarize(Path(temp))), [])
            found = load_discoveries(Path(temp))
            self.assertEqual(load_observed_asins(Path(temp)), {"B0DWT7PWHN"})
        self.assertEqual(list(found), ["A1WSIDJI0UEOX0"])
        self.assertEqual(found["A1WSIDJI0UEOX0"].products, {"B0DWT7PWHN": ("Comfort", 50)})
        self.assertIsNone(promotion_link(url.replace("amazon.sa", "example.test")))

    def test_overlap_requires_two_eligible_offers_and_safe_estimate(self):
        product = {"B0DWT7PWHN": ("Comfort & home", 50)}
        first = Promotion("https://www.amazon.sa/promotion/psp/A1WSIDJI0UEOX0", "Get Extra 20% off", product)
        second = Promotion("https://www.amazon.sa/promotion/psp/AJ7WCPGD2U9WP", "Save 10%", product)
        third = Promotion("https://www.amazon.sa/promotion/psp/A31IJHE1QWX79", "Save 9%", product)
        offers = {str(i): offer for i, offer in enumerate((first, second, third))}
        [block] = overlap_blocks(offers)
        self.assertIn("28.00٪", block)
        self.assertIn("Comfort &amp; home", block)
        self.assertNotIn("Save 9%", block)
        second.title = "Buy 2 Get 1 Free"
        self.assertNotIn("تقديري", overlap_blocks(offers)[0])
        second.title = "Save 9%"
        self.assertEqual(overlap_blocks(offers), [])
        self.assertEqual(saving_for("Save 15% up to SAR 50.00", 1000), (5, False))

    def test_long_digest_splits_with_complete_links(self):
        offers = {str(i): Promotion(f"https://www.amazon.sa/promotion/psp/TEST{i:08}",
                                     "A" * 140, complete=True) for i in range(80)}
        messages = build_messages(offers, "123")
        self.assertGreater(len(messages), 1)
        self.assertTrue(all(len(message) < 4000 for message in messages))
        self.assertEqual(sum(message.count("</a>") for message in messages), len(offers))

    def test_amazon_titles_with_paid_quantity_can_qualify_for_overlap(self):
        product = {"B0DWT7PWHN": ("Comfort", None)}
        offers = {
            "A1WSIDJI0UEOX0": Promotion(
                "https://www.amazon.sa/promotion/psp/A1WSIDJI0UEOX0",
                "Amazon.sa: Get Extra 20% off العرض", product,
            ),
            "A31IJHE1QWX79": Promotion(
                "https://www.amazon.sa/promotion/psp/A31IJHE1QWX79",
                "Get 2 for the price of 1.", product,
            ),
        }
        [block] = overlap_blocks(offers)
        self.assertIn("20.00٪", block)
        self.assertIn("50.00٪", block)
        self.assertNotIn("تقديري", block)
        arabic_rate, _ = saving_for("احصل على 3 بسعر 2 العرض", None)
        self.assertEqual(arabic_rate.quantize(Decimal("0.01")), Decimal("33.33"))

    def test_no_eligible_overlap_sends_only_the_promotion_list(self):
        url = "https://www.amazon.sa/promotion/psp/A1WSIDJI0UEOX0"
        messages = build_messages({"A1WSIDJI0UEOX0": Promotion(url, "Get Extra 20% off")}, "123")
        self.assertEqual(len(messages), 1)
        self.assertIn("تعذر استكمال قراءة منتجات 1", messages[0])

    def test_overlap_diagnostics_distinguish_low_discounts_and_unknown_terms(self):
        a = "B000000001"
        b = "B000000002"
        products = {a: ("First", 50), b: ("Second", 50)}
        offers = {
            "first": Promotion("https://www.amazon.sa/promotion/psp/TESTFIRST", "Save 20%", products),
            "second": Promotion("https://www.amazon.sa/promotion/psp/TESTSECOND", "Save 5%", {a: products[a]}),
            "third": Promotion("https://www.amazon.sa/promotion/psp/TESTTHIRD", "Promotion details", {b: products[b]}),
        }
        counts = {}
        self.assertEqual(overlap_blocks(offers, counts), [])
        self.assertEqual(counts, {"observed": 2, "below_threshold": 1, "unreadable": 1})

    def test_only_discovered_links_are_listed_and_confirmed_five_percent_is_excluded(self):
        offers = {
            "auto": Promotion("https://www.amazon.sa/promotion/psp/TESTAUTO", "Save 20%"),
            "manual": Promotion("https://www.amazon.sa/promotion/psp/TESTMANUAL", "Save 15%"),
            "five": Promotion("https://www.amazon.sa/promotion/psp/TESTFIVE", "Buy 2 or more and save 5%"),
            "arabic": Promotion("https://www.amazon.sa/promotion/psp/TESTARABIC", "اشتر 2 أو أكثر ووفّر %5"),
            "tiered": Promotion("https://www.amazon.sa/promotion/psp/TESTTIERED", "Save 5% or 20%"),
        }
        [message] = build_messages(offers, "123", discovered_codes={"auto", "five", "arabic", "tiered"})
        self.assertIn("TESTAUTO", message)
        self.assertIn("TESTTIERED", message)
        self.assertNotIn("TESTMANUAL", message)
        self.assertNotIn("TESTFIVE", message)
        self.assertNotIn("TESTARABIC", message)
        self.assertEqual(build_messages(offers, "123", discovered_codes={"five"}), [])

    def test_cached_campaign_reuses_asins_only_until_expiry(self):
        url = "https://www.amazon.sa/promotion/psp/TESTCACHE"
        original = {"B000000001": ("First", 20)}
        cache = {"TESTCACHE": {"checked_at": 10, "url": url, "title": "Save 20%", "products": original}}
        offers = {"TESTCACHE": Promotion(url, products={"B000000002": ("Second", 25)})}
        with patch("promotion_digest.complete_discoveries") as complete:
            updated = refresh_discoveries(offers, cache, 11)
            self.assertEqual(updated["TESTCACHE"]["checked_at"], 10)
            self.assertEqual(set(updated["TESTCACHE"]["products"]), {"B000000001", "B000000002"})
            self.assertTrue(offers["TESTCACHE"].complete)
            self.assertEqual(set(offers["TESTCACHE"].products), {"B000000001", "B000000002"})
            complete.assert_called_once_with(offers, None)
        with patch("promotion_digest.complete_discoveries"):
            next_run = {"TESTCACHE": Promotion(url)}
            self.assertEqual(refresh_discoveries(next_run, cache, 10 + CACHE_SECONDS), {})
            self.assertFalse(next_run["TESTCACHE"].complete)

    def test_manual_pages_still_participate_in_overlap_for_observed_asins(self):
        product = {"B000000001": ("First", 20)}
        offers = {
            "auto": Promotion("https://www.amazon.sa/promotion/psp/TESTAUTO", "Save 20%", product),
            "manual": Promotion("https://www.amazon.sa/promotion/psp/TESTMANUAL", "Save 15%", product),
        }
        self.assertEqual(build_messages(offers, "123", discovered_codes=set(), observed_asins=set()), [])
        [message] = build_messages(offers, "123", discovered_codes=set(), observed_asins=set(product))
        self.assertIn("TESTMANUAL", message)
        self.assertIn("TESTAUTO", message)
        self.assertIn("B000000001", message)

    def test_exact_offer_link_label_can_exclude_five_percent_before_a_page_request(self):
        url = "https://www.amazon.sa/promotion/psp/TESTFIVE"
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"SCAN_REPORT_DIR": temp}):
            record_promotion_links("bot1", [{
                "asin": "B000000001",
                "promotion_links": [{"url": url, "label": "Buy 2 or more and save 5%"}],
            }, {"asin": "B000000002"}])
            offers = load_discoveries(Path(temp))
            self.assertEqual(load_observed_asins(Path(temp)), {"B000000001", "B000000002"})
        with patch("promotion_digest.subprocess.run") as fetch:
            refresh_discoveries(offers, {}, 10)
            fetch.assert_not_called()
        self.assertEqual(build_messages(offers, "123", discovered_codes=set(offers)), [])

    def test_background_reader_reuses_results_without_reopening_the_offer(self):
        url = "https://www.amazon.sa/promotion/psp/TESTWATCH"
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"SCAN_REPORT_DIR": temp}):
            folder = Path(temp)
            snapshot = {"asin": "B000000001", "promotion_links": [url]}
            record_promotion_links("bot1", [snapshot])
            polls = 0

            def next_poll(_seconds):
                nonlocal polls
                polls += 1
                if polls == 1:
                    record_promotion_links("bot2", [snapshot])
                else:
                    (folder / PROMOTION_RESULTS / "stop").touch()

            def read(_code, _url, output, _timeout):
                output.write_text('{"title":"Save 20%"}\n{"complete":true}\n', encoding="utf-8")

            with (
                patch("promotion_digest.cached_promotions", return_value={}),
                patch("promotion_digest.time.sleep", side_effect=next_poll),
                patch("promotion_digest.read_promotion_page", side_effect=read) as fetch,
            ):
                watch_discoveries(folder)
                fetch.assert_called_once()
                offers = load_discoveries(folder)
                cache = refresh_discoveries(offers, {}, 10, folder / PROMOTION_RESULTS)
                fetch.assert_called_once()
                self.assertTrue(offers["TESTWATCH"].complete)
                self.assertEqual(cache["TESTWATCH"]["title"], "Save 20%")

    def test_background_worker_finds_product_offer_from_text_only_search_result(self):
        url = "https://www.amazon.sa/promotion/psp/A31IJHE1QWX79"
        asin = "B0BT22JP9X"
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"SCAN_REPORT_DIR": temp}):
            folder = Path(temp)
            card = {"asin": asin, "text": "Product title\nوفر 15% عند شراء 250 ريال", "price_text": "SAR 90"}
            for bot in ("bot1", "bot2"):
                record_promotion_links(bot, [card])
            self.assertEqual(load_discoveries(folder), {})
            with (
                patch("promotion_digest.cached_promotions", return_value={}),
                patch("promotion_digest.read_product_links", return_value=[
                    {"url": url + "?redirectAsin=B0BT22JP9X", "label": "تسوق السلع المستوفية للشروط"},
                    {"url": "https://www.amazon.sa/b/?node=224615726031", "label": "تسوق السلع المستوفية للشروط"},
                ]) as lookup,
                patch("promotion_digest.read_promotion_page") as read,
                patch("promotion_digest.time.sleep", side_effect=lambda _: (folder / PROMOTION_RESULTS / "stop").touch()),
            ):
                watch_discoveries(folder)
            lookup.assert_called_once_with(asin)
            self.assertEqual(read.call_args.args[0], "A31IJHE1QWX79")
            offers = load_discoveries(folder)
            self.assertEqual(list(offers), ["A31IJHE1QWX79"])
            self.assertIn(asin, offers["A31IJHE1QWX79"].products)

    def test_candidate_hint_skips_known_five_percent_and_product_titles(self):
        self.assertEqual(promotion_hint("Cable with 15% recycled material"), "")
        self.assertEqual(promotion_hint("Buy 2 or more and save 5%"), "")
        self.assertEqual(promotion_hint("اشتر 2 أو أكثر ووفّر %5"), "")
        self.assertEqual(promotion_hint("Product title\nSave 15%, Max.100SAR Min"), "save 15%, max.100sar min")
