import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from deal_parser_v2 import classify_deals
from deal_offer_rules import select_qualifying_offer, record_unrecognized


def read(text, price=20):
    return classify_deals(current_price=price,reference_price=None,card_text=text)


class OfferRulesTests(unittest.TestCase):
    def test_quantity_price_and_free_item_equations(self):
        cases = [
            ('Buy 2 for the price of 1', 2, 20, 50),
            ('اشتر 2 بسعر 1', 2, 20, 50),
            ('اشتري ٢ بسعر ١', 2, 20, 50),
            ('2 for 1', 2, 20, 50),
            ('Buy 1 get 1 free', 2, 20, 50),
            ('Buy one get one free', 2, 20, 50),
            ('اشتر 1 واحصل على 1 مجانا', 2, 20, 50),
            ('اشتر واحدة واحصل على الثانية مجانا', 2, 20, 50),
            ('الثانية مجانا', 2, 20, 50),
            ('Buy 2 get 1 free', 3, 40, 33.33),
            ('Buy 3 get 2 free', 5, 60, 40),
            ('Buy 3 for the price of 2', 3, 40, 33.33),
            ('Buy 2 for SAR 20', 2, 20, 50),
            ('اشتر 2 بسعر 20 ريال', 2, 20, 50),
            ('3 for SAR 30', 3, 30, 50),
        ]
        for text,quantity,pay,percent in cases:
            with self.subTest(text=text):
                o = read(text)['promotion']
                self.assertIsNotNone(o)
                self.assertEqual((o['quantity'],o['pay_price'],o['discount_percent']), (quantity,pay,percent))
                self.assertEqual(o['regular_total'],20*quantity)
                self.assertEqual(o['saving'],20*quantity-pay)

    def test_buy_five_discount_both_languages_and_word_orders(self):
        for text in ['اشتر 5 واحصل على خصم 50%', 'اشتري 5 منتجات واحصل على خصم 50%',
                     'Buy 5 and get 50% off', 'Buy 5 get a discount of 50%',
                     'Save 50% when you buy 5', 'خصم 50% عند شراء 5 قطع',
                     'وفر 50% على أي من 5 أو أكثر', 'اشتر خمسة واحصل على خصم 50%']:
            with self.subTest(text=text):
                o = read(text)['promotion']
                self.assertIsNotNone(o)
                self.assertEqual((o['quantity'],o['regular_total'],o['pay_price'],o['discount_percent']), (5,100,50,50))
                self.assertIsNotNone(select_qualifying_offer(read(text),40,40))

    def test_coupon_is_independent_of_other_percent_and_quantity_offer(self):
        for separator in ['. ', '\n', '; ', ' ']:
            with self.subTest(separator=separator):
                d = read('Save 5% on any 5 or more'+separator+'Save 50% with coupon',100)
                self.assertEqual(d['promotion']['discount_percent'],5)
                self.assertEqual(d['coupon']['discount_percent'],50)
                self.assertEqual(select_qualifying_offer(d,40,40)['type'],'percent_coupon')
        d = read('40% off. Save 50% with coupon',100)
        self.assertEqual(d['coupon']['discount_percent'],50)
        d = read('خصم 70% قسيمة خصم 40%',100)
        self.assertEqual(d['coupon']['discount_percent'],40)

    def test_promotion_not_hidden_by_small_coupon(self):
        d = read('Save 10% with coupon; Buy 2 for the price of 1')
        self.assertEqual(d['coupon']['discount_percent'],10)
        self.assertEqual(select_qualifying_offer(d,40,40)['discount_percent'],50)

    def test_distinct_coupon_and_second_item_offer(self):
        d = read('50% off the second item; Save 60% with coupon',100)
        self.assertEqual(len(d['offers']),2)
        self.assertEqual(d['promotion']['effective_basket_discount_percent'],25)
        self.assertEqual(d['coupon']['discount_percent'],60)

    def test_coupon_math(self):
        for text,rate,pay in [('You pay SAR 60 with coupon',40,60),
                              ('تدفع 60 ريال مع القسيمة',40,60),
                              ('SAR 40 off coupon applied',40,60),
                              ('قسيمة خصم 40 ريال',40,60),
                              ('قسيمة خصم 40%',40,60)]:
            with self.subTest(text=text):
                o = read(text,100)['coupon']
                self.assertIsNotNone(o)
                self.assertEqual((o['discount_percent'],o['pay_price']),(rate,pay))

    def test_multiline_coupon_phrase(self):
        for text in ['Save 50%\nwith coupon','قسيمة خصم\n50%','وفر 50%\nمع القسيمة']:
            with self.subTest(text=text):
                self.assertEqual(read(text)['coupon']['discount_percent'],50)

    def test_arabic_second_item_at_purchase(self):
        o = read('خصم 50% عند شراء المنتج الثاني')['promotion']
        self.assertEqual(o['discount_percent'],50)
        self.assertEqual(o['effective_basket_discount_percent'],25)

    def test_fixed_savings_on_quantity(self):
        for text in ['Save SAR 50 when you buy 5','وفر 50 ريال عند شراء 5 قطع']:
            with self.subTest(text=text):
                o = read(text)['promotion']
                self.assertEqual((o['quantity'],o['pay_price'],o['discount_percent']),(5,50,50))

    def test_checkout_percentage_and_cash_saving(self):
        for text in ['Save 50% at checkout','وفر 50% عند الدفع','وفر 50 ريال']:
            with self.subTest(text=text):
                self.assertEqual(read(text,100)['promotion']['discount_percent'],50)

    def test_recognized_and_unknown_offer_both_retained(self):
        d = read('Save 50% with coupon; Buy 3 get a special discount')
        self.assertEqual(d['coupon']['discount_percent'],50)
        self.assertTrue(d['unrecognized'])

    def test_inclusive_40_percent_threshold(self):
        self.assertIsNone(select_qualifying_offer(read('Buy 2 get 1 free'),40,40))
        self.assertIsNotNone(select_qualifying_offer(read('Buy 3 get 2 free'),40,40))

    def test_missing_price_and_conditions_require_review(self):
        for text,price in [('Buy 1 get 1 free',None),('Up to 50% with coupon',20),
                           ('Save 50% with coupon maximum SAR 5',20),
                           ('Save 50% with bank card',20),
                           ('Buy 2 get the cheapest free',20),
                           ('اشتر 2 واحصل على خصم خاص',20)]:
            with self.subTest(text=text):
                d = read(text,price)
                self.assertFalse(d['offers'])
                self.assertTrue(d['unrecognized'])

    def test_unknown_cases_recorded_without_telegram(self):
        d = read('Buy 2 get a mystery discount')
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ,{'UNKNOWN_OFFERS_DIR':folder}):
            record_unrecognized('B012345678','test_bot',d)
            record_unrecognized('B012345678','test_bot',d)
            lines = (Path(folder)/'test_bot.jsonl').read_text(encoding='utf-8').splitlines()
            self.assertEqual(len(lines),1)
            self.assertEqual(json.loads(lines[0])['asin'],'B012345678')

    def test_no_promotion_from_unrelated_bare_percent(self):
        self.assertFalse(read('50% cotton 2 pieces')['offers'])
        self.assertFalse(read('Buy 5 for SAR 500')['offers'])

    def test_shipping_boilerplate_does_not_fill_unknown_sample_limit(self):
        d = read('Product title\nFREE Delivery tomorrow\nFREE Returns\nتوصيل مجاني')
        self.assertFalse(d['unrecognized'])
        self.assertFalse(d['offers'])


class ReviewedBasketOffersTests(unittest.TestCase):
    def test_partial_quantity_uses_basket_rate_and_requires_prices(self):
        mixed = read('Buy 2, save 20% on 1.')
        self.assertFalse(mixed['offers'])
        self.assertEqual(mixed['unrecognized'][0]['maximum_basket_discount_percent'], 10)
        high_mixed = read('Buy 2, save 100% on 1.')
        self.assertIsNone(select_qualifying_offer(high_mixed, 40, 40))
        self.assertEqual(high_mixed['unrecognized'][0]['maximum_basket_discount_percent'], 50)
        high_same = read('Buy 2, save 80% on 1 of the same item')
        offer = select_qualifying_offer(high_same, 40, 40)
        self.assertEqual((offer['discount_percent'], offer['stated_discount_percent']), (40, 80))

    def test_capped_quantity_uses_cap_before_threshold(self):
        actual = 'Save 10% on 3+ from the UAE, up to SAR 15.00'
        offer = read(actual, 100)['promotion']
        self.assertEqual((offer['saving'], offer['discount_percent']), (15, 5))
        high = 'Save 60% on 3+ UK, up to SAR 30.00'
        self.assertIsNone(select_qualifying_offer(read(high, 30), 40, 40))
        self.assertEqual(select_qualifying_offer(read(high, 20), 40, 40)['discount_percent'], 50)

    def test_spend_tiers_keep_conditions_and_test_threshold(self):
        d = read('خصم بنسبة 8% على أي 40.00 ريال، خصم بنسبة 20% على أي 200.00 ريال')
        self.assertEqual([(o['discount_percent'], o['minimum_spend']) for o in d['offers']],
                         [(8, 40), (20, 200)])
        self.assertIsNone(select_qualifying_offer(d, 40, 40))
        offer = select_qualifying_offer(read('Save 45% when you buy SAR 80.00 of select items'), 40, 40)
        self.assertEqual((offer['discount_percent'], offer['minimum_spend']), (45, 80))
        self.assertFalse(read('Save 45% when you buy SAR 80.00 of select items, maximum SAR 10')['offers'])

    def test_prime_member_price_is_a_promotion_with_its_own_threshold(self):
        for text in ('Join Prime to buy this item at SAR 29.99',
                     'انضم إلى برايم لشراء هذا المنتج بسعر 29.99 ريال'):
            with self.subTest(text=text):
                offer = select_qualifying_offer(read(text, 49.99), 90, 40)
                self.assertEqual(offer['type'], 'prime_price_promotion')
                self.assertEqual(offer['pay_price'], 29.99)
                self.assertIsNone(select_qualifying_offer(read(text, 49.98), 40, 40))

    def test_prime_without_a_verified_regular_price_is_reviewable(self):
        text = 'Join Prime to buy this item at SAR 29.99'
        for price, verified in ((None, True), (29.99, True), (50, False)):
            with self.subTest(price=price, verified=verified):
                deals = classify_deals(current_price=price, reference_price=80,
                                       card_text=text, regular_price_verified=verified)
                self.assertFalse(deals['offers'])
                self.assertEqual(deals['unrecognized'][0]['reason'], 'prime_regular_price_unverified')

    def test_only_corroborated_bare_markdown_badges_are_removed_from_review(self):
        for current, reference, text in ((11.95, 43.56, 'Save 73%'),
                                         (15.48, 40, 'Save 61.30%'),
                                         (39, 79, 'خصم 51%')):
            with self.subTest(text=text):
                deals = classify_deals(current_price=current, reference_price=reference,
                                       card_text=text + '; Buy 3 get a special discount')
                self.assertIsNotNone(deals['price_drop'])
                self.assertFalse(deals['offers'])
                self.assertEqual([case['text'] for case in deals['unrecognized']],
                                 ['Buy 3 get a special discount'])
        self.assertTrue(read('Save 73%')['unrecognized'])
        self.assertTrue(classify_deals(current_price=11.95, reference_price=43.56,
                                      card_text='Save 80%')['unrecognized'])

    def test_unhandled_later_item_is_never_an_all_items_offer(self):
        d = read('Buy 4 and get 50% off the fifth item')
        self.assertFalse(d['offers'])
        self.assertTrue(d['unrecognized'])



class QuantityAndSpendTierTests(unittest.TestCase):
    def test_observed_toy_tiers_apply_to_the_whole_basket(self):
        for text, expected in (
            ('Buy 1 Get 10%, Buy 2 Get 15% Off on Toys', [(1, 10, 18), (2, 15, 34)]),
            ('Buy 2 Get 5%, Buy 3 Get 10% Off on Toys', [(2, 5, 38), (3, 10, 54)]),
        ):
            with self.subTest(text=text):
                deals = read(text, 20)
                self.assertEqual([(o['quantity'], o['discount_percent'], o['pay_price'])
                                  for o in deals['offers']], expected)
                self.assertFalse(deals['unrecognized'])
                self.assertIsNone(select_qualifying_offer(deals, 40, 40))

    def test_qualifying_tier_keeps_the_required_quantity(self):
        deals = read('Buy 1 Get 10%, Buy 3 Get 45% Off on Toys', 20)
        offer = select_qualifying_offer(deals, 40, 40)
        self.assertEqual((offer['quantity'], offer['pay_price']), (3, 33))
        self.assertEqual(offer['type'], 'quantity_tier_promotion')

    def test_partial_and_capped_tiers_require_review(self):
        for text in ('Buy 1 Get 10%, Buy 3 Get 80% Off on 1',
                     'Buy 1 Get 10%, Buy 3 Get 80% Off on Toys, maximum SAR 5'):
            with self.subTest(text=text):
                deals = read(text)
                self.assertFalse(deals['offers'])
                self.assertTrue(deals['unrecognized'])

    def test_spend_threshold_and_extra_discount_variants(self):
        for text, rate in (('Save 10% when you spend SAR 50.00', 10),
                           ('Save 45% when you spend SAR 100.00', 45)):
            offer = read(text)['promotion']
            self.assertEqual(offer['discount_percent'], rate)
            self.assertEqual(offer['minimum_spend'], 50 if rate == 10 else 100)
        for text, rate in (('Buy & Get extra 15% Off', 15),
                           ('Buy & Get extra 45% Off', 45)):
            deals = read(text, 100)
            self.assertEqual(deals['promotion']['pay_price'], 100 - rate)
            self.assertFalse(deals['unrecognized'])
        self.assertFalse(read('Save 50% when you spend SAR 50, with bank card')['offers'])

if __name__ == '__main__':
    unittest.main()
