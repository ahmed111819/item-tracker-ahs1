import unittest

from deal_parser_v2 import classify_deals, extract_reference_price, normalize_text, parse_price


class DealParserV2Tests(unittest.TestCase):
    def test_arabic_digits_and_bidi_marks(self):
        self.assertEqual(parse_price("\u200f١٢٣٫٥٠ ريال"), 123.5)
        self.assertEqual(normalize_text("وفِّر ٥٪"), "وفر 5%")

    def test_second_item_is_promotion_not_price_drop(self):
        deals = classify_deals(
            current_price=100,
            reference_price=120,
            card_text="خصم 50% على القطعة الثانية",
        )
        self.assertEqual(deals["promotion"]["type"], "second_item_promotion")
        self.assertEqual(deals["promotion"]["discount_percent"], 50)
        self.assertEqual(deals["promotion"]["effective_basket_discount_percent"], 25)
        self.assertAlmostEqual(deals["price_drop"]["discount_percent"], 16.67, places=2)

    def test_english_second_item_is_promotion(self):
        deals = classify_deals(current_price=80, reference_price=None, card_text="50% off the second item")
        self.assertEqual(deals["promotion"]["type"], "second_item_promotion")
        self.assertIsNone(deals["price_drop"])

    def test_arabic_quantity_discount_is_not_coupon_or_price_drop(self):
        deals = classify_deals(
            current_price=10,
            reference_price=None,
            card_text="وفِّر 5% على أي من 5 أو أكثر",
        )
        self.assertEqual(deals["promotion"]["type"], "quantity_promotion")
        self.assertIsNone(deals["coupon"])
        self.assertIsNone(deals["price_drop"])

    def test_observed_arabic_search_card_sample(self):
        # Text observed on an Amazon.sa result card during the live page check.
        deals = classify_deals(
            current_price=15.65,
            reference_price=33.95,
            card_text="وفِّر 5% على أي من 5 أو أكثر",
        )
        self.assertEqual(deals["promotion"]["type"], "quantity_promotion")
        self.assertEqual(deals["promotion"]["discount_percent"], 5)
        self.assertIn("5%", deals["promotion"]["description"])
        self.assertIn("على أي من 5 أو أكثر", deals["promotion"]["description"])
        self.assertAlmostEqual(deals["price_drop"]["discount_percent"], 53.9, places=1)

    def test_observed_arabic_list_price_label(self):
        self.assertEqual(
            extract_reference_price("السعر، صفحة المنتج 15.65 ريال سعر القائمه: 33.95 ريال", 15.65),
            33.95,
        )
        self.assertIsNone(extract_reference_price("خصم 50% على القطعة الثانية", 100))

    def test_arabic_coupon_is_classified_separately(self):
        deals = classify_deals(current_price=100, reference_price=None, card_text="قسيمة خصم 50% على هذا المنتج")
        self.assertEqual(deals["coupon"]["type"], "percent_coupon")
        self.assertEqual(deals["coupon"]["discount_percent"], 50)
        self.assertIsNone(deals["price_drop"])

    def test_coupon_pay_price_copy_is_supported(self):
        deals = classify_deals(
            current_price=50,
            reference_price=None,
            card_text="You pay SAR 36.80 with coupon",
        )
        self.assertEqual(deals["coupon"]["type"], "you_pay_coupon")
        self.assertEqual(deals["coupon"]["pay_price"], 36.8)
        self.assertAlmostEqual(deals["coupon"]["discount_percent"], 26.4)

    def test_cash_coupon_copy_is_supported(self):
        deals = classify_deals(
            current_price=100,
            reference_price=None,
            card_text="SAR 12.90 off coupon applied",
        )
        self.assertEqual(deals["coupon"]["type"], "cash_coupon")
        self.assertEqual(deals["coupon"]["saving"], 12.9)

    def test_english_quantity_offer_is_not_coupon(self):
        deals = classify_deals(current_price=10, reference_price=None, card_text="Save 5% on any 5 or more")
        self.assertEqual(deals["promotion"]["type"], "quantity_promotion")
        self.assertIsNone(deals["coupon"])

    def test_bundle_offer_uses_unit_price_and_bundle_total(self):
        deals = classify_deals(current_price=20, reference_price=None, card_text="Buy 3 for SAR 45.00")
        self.assertEqual(deals["promotion"]["type"], "bundle_offer")
        self.assertEqual(deals["promotion"]["quantity"], 3)
        self.assertEqual(deals["promotion"]["pay_price"], 45)
        self.assertEqual(deals["promotion"]["discount_percent"], 25)

    def test_price_drop_uses_only_explicit_reference_price(self):
        deals = classify_deals(current_price=60, reference_price=100, card_text="50% off the second item")
        self.assertEqual(deals["price_drop"]["discount_percent"], 40)
        self.assertEqual(deals["promotion"]["type"], "second_item_promotion")

    def test_bare_percent_does_not_become_coupon_or_price_drop(self):
        deals = classify_deals(current_price=50, reference_price=None, card_text="خصم 50% عند شراء المنتج الثاني")
        self.assertIsNone(deals["coupon"])
        self.assertIsNone(deals["price_drop"])


if __name__ == "__main__":
    unittest.main()
