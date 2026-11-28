"""Connection failures may retry before SQL, while transactions execute once."""

import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import MagicMock, patch


class ConnectionErrorForTest(Exception):
    sqlstate = None


class PostgresReconnectTests(unittest.TestCase):
    def setUp(self):
        driver = types.ModuleType("psycopg")
        driver.Error = ConnectionErrorForTest
        driver.OperationalError = ConnectionErrorForTest
        driver.connect = MagicMock()
        rows = types.ModuleType("psycopg.rows")
        rows.dict_row = object()
        spec = importlib.util.spec_from_file_location(
            "storage_under_test", Path("postgres_storage.py")
        )
        self.storage = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"psycopg": driver, "psycopg.rows": rows}):
            spec.loader.exec_module(self.storage)
        self.driver = driver
        self.connection = MagicMock()
        self.connection.__enter__.return_value = self.connection

    def test_temporary_connection_failure_recovers_before_sql(self):
        self.driver.connect.side_effect = [ConnectionErrorForTest(), self.connection]
        with patch.object(self.storage, "_database_url", return_value="private-url"), patch.object(
            self.storage.time, "sleep"
        ) as sleep:
            with self.storage._connect() as connection:
                connection.execute("SELECT 1")
        self.assertEqual(self.driver.connect.call_count, 2)
        self.connection.execute.assert_called_once_with("SELECT 1")
        sleep.assert_called_once_with(2)

    def test_exhausted_connection_failure_releases_the_slot(self):
        self.driver.connect.side_effect = ConnectionErrorForTest()
        with patch.object(self.storage, "_database_url", return_value="private-url"), patch.object(
            self.storage.time, "sleep"
        ) as sleep:
            with self.assertRaises(ConnectionErrorForTest):
                with self.storage._connect():
                    self.fail("A transaction cannot start without a connection.")
        self.assertEqual(self.driver.connect.call_count, 4)
        self.assertEqual(sleep.call_count, 3)
        self.assertTrue(self.storage._CONNECTION_SLOTS.acquire(blocking=False))
        self.storage._CONNECTION_SLOTS.release()

    def test_authentication_and_transaction_failures_are_not_replayed(self):
        error = ConnectionErrorForTest()
        error.sqlstate = "28P01"
        self.driver.connect.side_effect = error
        with patch.object(self.storage, "_database_url", return_value="private-url"), patch.object(
            self.storage.time, "sleep"
        ) as sleep:
            with self.assertRaises(ConnectionErrorForTest):
                with self.storage._connect():
                    pass
            self.assertEqual(self.driver.connect.call_count, 1)
            sleep.assert_not_called()

            self.driver.connect.reset_mock(side_effect=True)
            self.driver.connect.return_value = self.connection
            self.connection.__exit__.side_effect = ConnectionErrorForTest()
            with self.assertRaises(ConnectionErrorForTest):
                with self.storage._connect() as connection:
                    connection.execute("UPDATE example SET value = value + 1")
            self.driver.connect.assert_called_once()
            self.connection.execute.assert_called_once()

    def test_readiness_executes_a_query_after_retrying_a_connection(self):
        self.driver.connect.side_effect = [ConnectionErrorForTest(), self.connection]
        with patch.object(self.storage, "_database_url", return_value="private-url"), patch.object(
            self.storage.time, "sleep"
        ):
            self.storage.check_database_ready()
        self.assertEqual(self.driver.connect.call_count, 2)
        self.assertEqual(self.driver.connect.call_args.kwargs["connect_timeout"], 5)
        self.connection.execute.assert_any_call("SET LOCAL statement_timeout = '5s'")
        self.connection.execute.assert_called_with("SELECT 1")

    def test_readiness_failure_does_not_expose_driver_details(self):
        self.driver.connect.side_effect = ConnectionErrorForTest("private-url")
        with patch.object(self.storage, "_database_url", return_value="private-url"), patch.object(
            self.storage.time, "sleep"
        ), self.assertRaises(SystemExit) as failure:
            self.storage.check_database_ready()
        self.assertIn("Oracle PostgreSQL readiness check failed", str(failure.exception))
        self.assertNotIn("private-url", str(failure.exception))
        self.connection.execute.assert_not_called()
