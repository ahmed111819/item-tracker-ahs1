"""Execute the real card handler with external effects replaced by local fakes."""
import ast
import asyncio
import html
import unittest
from types import SimpleNamespace
from datetime import datetime
from pathlib import Path

from deal_parser_v2 import classify_deals, extract_reference_price, parse_price
from deal_offer_rules import select_qualifying_offer, reviewed_promotion_details
from scan_link_report import classify_card_for_report, new_page_metrics


def run(coroutine):
    # All external async work is replaced with immediate local fakes. No OS
    # event loop, threads or sockets are needed to verify handler decisions.
    try:
        coroutine.send(None)
    except StopIteration as result:
        return result.value
    raise AssertionError('Unexpected external async work in card-handler test')


async def local_call(function, *args, **kwargs):
    return function(*args, **kwargs)


def handler(bot, suppressed=False):
    src = Path(f'scanner_amazon_sa_bot{bot}.py').read_text(encoding='utf-8')
    tree = ast.parse(src)
    nodes = [n for n in tree.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef))
             and n.name in ('process_card_locked','build_telegram_caption')]
    sent=[]
    ns=dict(asyncio=SimpleNamespace(to_thread=local_call),datetime=datetime,html=html,
            classify_deals=classify_deals,parse_price=parse_price,
            classify_card_for_report=classify_card_for_report,
            extract_reference_price=extract_reference_price,
            select_qualifying_offer=select_qualifying_offer,
            reviewed_promotion_details=reviewed_promotion_details,
            record_unrecognized=lambda *args:None,
            print=lambda *args:None,save_tracking=lambda:None,
            VERBOSE_PRODUCT_LOGS=False,STORAGE_BACKEND='postgres',
            ACTIVE_PRODUCT_BLOCKS={},BOT_ID=f'bot{bot}',
            MIN_DISCOUNT_PERCENT=40,MIN_PROMOTION_DISCOUNT_PERCENT=40,
            MIN_PRICE_DROP_PERCENT=80,TELEGRAM_READY=True,
            RUN_COUPON_SIGNATURES={},RUN_PRICE_DROP_SIGNATURES={},
            tracking_data={'products':{}},
            check_product_block=lambda *args:dict(suppress_alert=suppressed,released=False))
    def send(*args):
        sent.append(args)
        return True
    ns['send_telegram_alert_once']=send
    exec(compile(ast.Module(body=nodes,type_ignores=[]),'scanner_handler','exec'),ns)
    return ns,sent


class ScannerIntegrationTests(unittest.TestCase):
    def test_three_scanners_send_qualifying_offer_once(self):
        cases=['اشتر 2 بسعر 1', 'اشتر 5 واحصل على خصم 50%',
               'Save 10% with coupon; Buy 2 for the price of 1',
               'Save 5% on any 5 or more; Save 50% with coupon']
        for bot in (1,2,3):
            for text in cases:
                with self.subTest(bot=bot,text=text):
                    ns,sent=handler(bot)
                    snapshot=dict(asin='B012345678',title='A valid product title',
                                  text=text,price_text='SAR 20.00',image_url='https://example.com/product.jpg')
                    call=lambda:run(ns['process_card_locked']('search',1,1,1,snapshot))
                    self.assertEqual(call(),(True,True))
                    self.assertEqual(len(sent),1)
                    self.assertEqual(sent[0][4]['discount_percent'],50)
                    self.assertEqual(call(),(True,False))
                    self.assertEqual(len(sent),1)
                    caption=ns['build_telegram_caption'](snapshot['title'],snapshot['asin'],20,sent[0][4])
                    self.assertIn('50.00%',caption)
                    self.assertIn('رابط المنتج',caption)

    def test_block_and_below_threshold_still_suppress(self):
        for bot in (1,2,3):
            for blocked,text in [(True,'Buy 1 get 1 free'),(False,'Buy 2 get 1 free')]:
                with self.subTest(bot=bot,blocked=blocked):
                    ns,sent=handler(bot,blocked)
                    snap=dict(asin='B012345678',title='A valid product title',text=text,price_text='SAR 20')
                    result=run(ns['process_card_locked']('search',1,1,1,snap))
                    self.assertEqual(result,(True,False))
                    self.assertFalse(sent)

    def test_production_thresholds(self):
        for bot in (1,2,3):
            tree=ast.parse(Path(f'scanner_amazon_sa_bot{bot}.py').read_text(encoding='utf-8'))
            values={n.targets[0].id:ast.literal_eval(n.value) for n in tree.body
                    if isinstance(n,ast.Assign) and isinstance(n.targets[0],ast.Name)
                    and n.targets[0].id in ('MIN_DISCOUNT_PERCENT','MIN_PROMOTION_DISCOUNT_PERCENT','MIN_PRICE_DROP_PERCENT')}
            self.assertEqual(values,dict(MIN_DISCOUNT_PERCENT=40,MIN_PROMOTION_DISCOUNT_PERCENT=40,MIN_PRICE_DROP_PERCENT=80))

    def test_report_counts_eligible_types_independently_of_alerts(self):
        for bot in (1, 2, 3):
            with self.subTest(bot=bot):
                ns, sent = handler(bot)
                metrics = new_page_metrics()
                snapshot = dict(asin='B012345678', title='A valid product title',
                                text='Save 45% with coupon; Buy 2 for the price of 1',
                                price_text='SAR 20', reference_price_text='SAR 100')
                run(ns['process_card_locked']('search', 1, 1, 1, snapshot, metrics))
                run(ns['process_card_locked']('search', 2, 1, 1, snapshot, metrics))
                self.assertEqual(metrics['found'], {kind: {'B012345678'} for kind in
                                 ('coupon', 'promotion', 'price_drop')})
                self.assertEqual(metrics['sent'], dict(coupon=0, promotion=1,
                                 price_drop=1, block_release=0))
                caption = ns['build_telegram_caption'](snapshot['title'], snapshot['asin'], 20,
                        dict(type='product_block_release', blocked_price=25))
                self.assertIn('وقت الحجب: 25.00 SAR', caption)
                self.assertIn('الحالي: 20.00 SAR', caption)

    def test_reviewed_promotion_caption_keeps_condition_and_actual_percentage(self):
        cases = (
            ('Buy 2, save 80% on 1 of the same item', '80.00%', 'التوفير على الإجمالي: 40.00%'),
            ('Save 60% on 3+ UK, up to SAR 30.00', 'سقف التوفير: 30.00 SAR', 'بعد السقف: 50.00%'),
            ('Save 45% when you buy SAR 80.00 of select items', '80.00 SAR', 'خصم هذه الشريحة: 45.00%'),
        )
        for bot in (1, 2, 3):
            ns, _ = handler(bot)
            for wording, condition, percentage in cases:
                with self.subTest(bot=bot, wording=wording):
                    deals = classify_deals(current_price=20, reference_price=None, card_text=wording)
                    offer = select_qualifying_offer(deals, 40, 40)
                    caption = ns['build_telegram_caption']('Example product', 'B012345678', 20, offer)
                    self.assertIn(wording, caption)
                    self.assertIn(condition, caption)
                    self.assertIn(percentage, caption)
