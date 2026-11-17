import csv
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scan_link_report import ALERT_KINDS, OFFER_KINDS, record_event, render_report


class ScanLinkReportTests(unittest.TestCase):
    def test_retries_deduplicate_pages_and_products_but_keep_actual_sends(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events = root / 'events'
            summary = root / 'actions-summary.md'
            url = 'https://www.amazon.sa/s?k=test'
            sent = dict.fromkeys(ALERT_KINDS, 0)
            found = dict.fromkeys(OFFER_KINDS, ['B012345678'])
            with patch.dict(os.environ, SCAN_REPORT_DIR=str(events),
                            GITHUB_STEP_SUMMARY=str(summary)):
                record_event('bot1', 1, url, kind='start')
                record_event('bot1', 1, url, kind='page', page=1, success=True,
                             products=['B012345678'], found=found, sent={**sent, 'coupon': 1},
                             unknown=['B012345678'])
                record_event('bot1', 1, url, kind='finish', status='failed')
                record_event('bot1', 1, url, kind='start')
                record_event('bot1', 1, url, kind='page', page=1, success=True,
                             products=['B012345678'], found=found, sent=sent, unknown=[])
                record_event('bot1', 1, url, kind='page', page=2, success=False,
                             products=[], found=dict.fromkeys(OFFER_KINDS, []), sent=sent, unknown=[])
                record_event('bot2', 1, url, kind='registered')
                render_report(events, root / 'report')
            data = json.loads((root / 'report' / 'scan-links.json').read_text(encoding='utf-8'))
            row, pending = data
            self.assertEqual((row['pages_opened'], row['pages_failed'], row['unique_products']), (1, 1, 1))
            self.assertEqual((row['found_coupon'], row['sent_coupon'], row['unrecognized_products']), (1, 1, 1))
            self.assertEqual(pending['status'], 'not_started')
            with (root / 'report' / 'scan-links.csv').open(encoding='utf-8-sig', newline='') as stream:
                self.assertEqual(next(csv.DictReader(stream))['url'], url)
            self.assertIn(f'[رابط 1](<{url}>)', summary.read_text(encoding='utf-8'))
