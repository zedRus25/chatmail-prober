"""Tests for _run_turn_checks and _run_iroh_checks fan-out in orchestration."""

from __future__ import annotations

import argparse
import shutil
from unittest.mock import MagicMock, patch

import pytest

from chatmail_prober import metrics as metrics_mod
from chatmail_prober.orchestration import _run_iroh_checks, _run_turn_checks
from chatmail_prober.turn import TurnStatus


def _args(check_turn=False, check_iroh=False, workers=2, timeout=10,
          extra_turn_map=None):
    ns = argparse.Namespace(
        check_turn=check_turn,
        check_iroh=check_iroh,
        workers=workers,
        timeout=timeout,
    )
    if extra_turn_map is not None:
        ns.extra_turn_map = extra_turn_map
    return ns


def _pool():
    return MagicMock()


@pytest.fixture(autouse=True)
def _clear_turn_iroh_metrics():
    metrics_mod.relay_turn_status._metrics.clear()
    metrics_mod.relay_iroh_status._metrics.clear()
    metrics_mod.relay_iroh_latency_seconds._metrics.clear()
    yield


class TestRunTurnChecksBinaryMissing:
    def test_binary_missing_sets_status_for_all_relays(self, monkeypatch):
        """When turnutils_uclient is absent, every relay gets BINARY_MISSING."""
        monkeypatch.setattr(shutil, "which", lambda _: None)
        relays = ["a.example", "b.example"]
        _run_turn_checks(_pool(), relays, _args(check_turn=True))

        for relay in relays:
            val = metrics_mod.relay_turn_status.labels(
                relay=relay, turn_endpoint="self")._value.get()
            assert val == TurnStatus.BINARY_MISSING

    def test_binary_missing_no_uclient_call(self, monkeypatch):
        """No uclient invocation when binary is absent."""
        monkeypatch.setattr(shutil, "which", lambda _: None)
        with patch("chatmail_prober.orchestration._check_one_turn") as mock_check:
            _run_turn_checks(_pool(), ["a.example"], _args(check_turn=True))
        mock_check.assert_not_called()


class TestRunTurnChecksNoop:
    def test_no_check_turn_and_no_extra_returns_early(self, monkeypatch):
        """Without check_turn=True or extra_turn_map entries, nothing runs."""
        monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/turnutils_uclient")
        with patch("chatmail_prober.orchestration._check_one_turn") as mock_check:
            _run_turn_checks(_pool(), ["a.example", "b.example"], _args(check_turn=False))
        mock_check.assert_not_called()


class TestRunTurnChecksSuccess:
    def test_all_futures_complete_normally(self, monkeypatch):
        """When check_one_turn completes without error, no metrics are written via on_failure."""
        monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/turnutils_uclient")
        monkeypatch.setattr("chatmail_prober.orchestration._check_one_turn", lambda *a, **k: None)
        # Should not raise and not call update_turn_metrics with None
        with patch("chatmail_prober.orchestration.update_turn_metrics") as mock_utm:
            _run_turn_checks(_pool(), ["a.example"], _args(check_turn=True))
        # on_failure/on_timeout paths not triggered for clean completion
        for c in mock_utm.call_args_list:
            assert c.args[1] is not None, "on_failure called unexpectedly"


class TestRunTurnChecksExtraTurn:
    def test_extra_turn_map_entry_probed_without_check_turn(self, monkeypatch):
        """An extra_turn_map entry is always probed, even without check_turn=True."""
        monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/turnutils_uclient")
        called = []
        monkeypatch.setattr(
            "chatmail_prober.orchestration._check_one_turn",
            lambda pool, relay, timeout, pre=None: called.append(relay),
        )
        extra = {"extra.example": ("h", 3478, "u", "p", "self")}
        _run_turn_checks(_pool(), [], _args(check_turn=False, extra_turn_map=extra))
        assert "extra.example" in called


class TestRunIrohChecksNoop:
    def test_check_iroh_false_returns_early(self, monkeypatch):
        """When check_iroh=False, _check_one_iroh is never called."""
        with patch("chatmail_prober.orchestration._check_one_iroh") as mock_check:
            _run_iroh_checks(_pool(), ["a.example"], _args(check_iroh=False))
        mock_check.assert_not_called()

    def test_empty_relays_returns_early(self, monkeypatch):
        """Empty alive_relays list -> no work submitted."""
        with patch("chatmail_prober.orchestration._check_one_iroh") as mock_check:
            _run_iroh_checks(_pool(), [], _args(check_iroh=True))
        mock_check.assert_not_called()


class TestRunIrohChecksSuccess:
    def test_all_futures_complete_normally(self, monkeypatch):
        """Clean completion -> on_failure/on_timeout not triggered."""
        monkeypatch.setattr("chatmail_prober.orchestration._check_one_iroh",
                            lambda *a, **k: None)
        with patch("chatmail_prober.orchestration.update_iroh_metrics") as mock_uim:
            _run_iroh_checks(_pool(), ["a.example"], _args(check_iroh=True))
        mock_uim.assert_not_called()
