"""The tunnel restarts a lost SSH process and shuts down its child cleanly."""

from pathlib import Path
import threading
import unittest
from unittest.mock import MagicMock, patch

import ssh_tunnel


class TunnelSupervisorTests(unittest.TestCase):
    def test_disconnect_restarts_and_stop_terminates_the_live_child(self):
        exited = MagicMock()
        exited.poll.return_value = 1
        live = MagicMock()
        live.poll.return_value = None
        stop = threading.Event()
        delays = []

        def wait(delay):
            delays.append(delay)
            if delay == 1:
                stop.set()

        with patch.object(ssh_tunnel.subprocess, "Popen", side_effect=[exited, live]) as spawn, patch.object(
            stop, "wait", side_effect=wait
        ), patch.object(Path, "unlink"):
            ssh_tunnel.supervise(["ssh", "-N"], Path("test.sock"), stop)
        self.assertEqual(spawn.call_count, 2)
        self.assertEqual(delays, [2, 1])
        live.terminate.assert_called_once()
        live.wait.assert_called_once_with(timeout=10)

    def test_scan_and_rest_jobs_both_start_and_stop_the_supervisor(self):
        source = Path(".github/workflows/amazon-sa-bot3.yml").read_text(encoding="utf-8")
        self.assertEqual(source.count("python -u ssh_tunnel.py start"), 2)
        self.assertEqual(source.count("python ssh_tunnel.py stop"), 2)
