import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from promotion_digest import (
    Promotion, build_messages, load_discoveries, overlap_blocks, promotion_link,
    record_promotion_links, saving_for,
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
