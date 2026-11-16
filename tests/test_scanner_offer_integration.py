"""Execute the real card handler with external effects replaced by local fakes."""
import ast
import asyncio
import html
import unittest
from types import SimpleNamespace
from datetime import datetime
from pathlib import Path

from deal_parser_v2 import classify_deals, extract_reference_price, parse_price
from deal_offer_rules import select_qualifying_offer


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
            extract_reference_price=extract_reference_price,
            select_qualifying_offer=select_qualifying_offer,
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
