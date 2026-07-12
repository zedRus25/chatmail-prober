"""Tests for kill_stale_rpc_servers in chatmail_prober.orchestration."""

from __future__ import annotations

import signal
from unittest.mock import call, patch

from chatmail_prober.orchestration import kill_stale_rpc_servers


class TestKillStaleRpcServers:
    def test_no_pids_found_is_noop(self):
        """No matching processes -> no signal sent."""
        with patch("chatmail_prober.orchestration.rpc_server_pids", return_value=[]), \
             patch("os.kill") as mock_kill:
            kill_stale_rpc_servers("/tmp/cache")
            mock_kill.assert_not_called()

    def test_graceful_sends_sigterm_then_sigkill(self):
        """graceful=True sends SIGTERM to each PID, sleeps, then SIGKILL."""
        pids = [1001, 1002]
        with patch("chatmail_prober.orchestration.rpc_server_pids", return_value=pids), \
             patch("os.kill") as mock_kill, \
             patch("chatmail_prober.orchestration.time.sleep") as mock_sleep:
            kill_stale_rpc_servers("/tmp/cache", graceful=True)

        term_calls = [call(p, signal.SIGTERM) for p in pids]
        kill_calls = [call(p, signal.SIGKILL) for p in pids]
        mock_kill.assert_has_calls(term_calls + kill_calls, any_order=False)
        mock_sleep.assert_called_once()

    def test_not_graceful_only_sigkill(self):
        """graceful=False goes straight to SIGKILL with no SIGTERM and no sleep."""
        pids = [2001]
        with patch("chatmail_prober.orchestration.rpc_server_pids", return_value=pids), \
             patch("os.kill") as mock_kill, \
             patch("chatmail_prober.orchestration.time.sleep") as mock_sleep:
            kill_stale_rpc_servers("/tmp/cache", graceful=False)

        mock_kill.assert_called_once_with(2001, signal.SIGKILL)
        mock_sleep.assert_not_called()

    def test_process_lookup_error_on_sigterm_continues(self):
        """ProcessLookupError from SIGTERM (process already gone) must not abort."""
        pids = [3001, 3002]

        def _raise_on_term(pid, sig):
            if sig == signal.SIGTERM:
                raise ProcessLookupError(f"no such process {pid}")

        with patch("chatmail_prober.orchestration.rpc_server_pids", return_value=pids), \
             patch("os.kill", side_effect=_raise_on_term), \
             patch("chatmail_prober.orchestration.time.sleep"):
            kill_stale_rpc_servers("/tmp/cache", graceful=True)  # must not raise
