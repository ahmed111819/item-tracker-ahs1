"""Check scanner checkpoints without starting browsers or opening PostgreSQL."""

import ast
from datetime import datetime
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import mock_open, Mock


class TrackingCheckpointTests(unittest.TestCase):
    def checkpoints(self, bot, backend):
        path = Path(f"scanner_amazon_sa_bot{bot}.py")
        source = path.read_text(encoding="utf-8")
        function = next(node for node in ast.parse(source).body
                        if isinstance(node, ast.FunctionDef) and node.name == "save_tracking")
        namespace = dict(
            STORAGE_BACKEND=backend, BOT_ID=f"bot{bot}", datetime=datetime, json=json,
            print=Mock(), open=mock_open(), TRACKING_FILE="tracking.json",
            tracking_data={"products": {}}, time=SimpleNamespace(monotonic=Mock(return_value=60)),
            TRACKING_SYNC_INTERVAL_SECONDS=600, _last_tracking_sync=0, _tracking_dirty=False,
            save_postgres_state=Mock(return_value=100),
            supabase_upsert_bot_state=Mock(),
        )
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(path), "exec"), namespace)
        return namespace

    def test_postgres_skips_full_snapshot_between_due_saves_but_flushes_at_end(self):
        for bot in (1, 2, 3):
            with self.subTest(bot=bot):
                ns = self.checkpoints(bot, "postgres")
                ns["save_tracking"]()
                ns["open"].assert_not_called()
                ns["save_postgres_state"].assert_not_called()
                self.assertTrue(ns["_tracking_dirty"])

                ns["time"].monotonic.return_value = 600
                ns["tracking_data"]["products"]["example"] = {"price": 20}
                ns["save_tracking"]()
                ns["open"].assert_called_once()
                ns["save_postgres_state"].assert_called_once_with(f"bot{bot}", ns["tracking_data"])
                self.assertFalse(ns["_tracking_dirty"])

                ns["time"].monotonic.return_value = 610
                ns["tracking_data"]["products"]["example"]["price"] = 15
                ns["save_tracking"](force=True)
                self.assertEqual(ns["open"].call_count, 2)
                self.assertEqual(ns["save_postgres_state"].call_count, 2)
                self.assertFalse(ns["_tracking_dirty"])

    def test_other_storage_keeps_local_checkpoints_between_backend_syncs(self):
        ns = self.checkpoints(1, "supabase")
        ns["save_tracking"]()
        ns["open"].assert_called_once()
        ns["supabase_upsert_bot_state"].assert_not_called()
