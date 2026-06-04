"""Tests for RelayPool: shared rpc-server lifecycle, relay set, duck-typing.

RelayPool owns one deltachat-rpc-server process per worker thread and
serves multiple relay domains from it. Tests here mock chatmail_prober.pool.Rpc
so no real subprocess is spawned.
"""
from __future__ import annotations

from unittest.mock import patch

from chatmail_prober.probe import RelayPool


class TestRelayPool:
    """Only invariants worth asserting; trivial constructor-mock checks dropped."""

    @patch("chatmail_prober.pool.Rpc")
    def test_open_all_deduplicates_rpc_and_unions_relays(self, MockRpc, tmp_path):
        pool = RelayPool(tmp_path)
        pool.open_all(["a.example", "b.example"])
        pool.open_all(["a.example", "c.example"])
        MockRpc.assert_called_once()
        assert set(pool.contexts().keys()) == {"a.example", "b.example", "c.example"}

    @patch("chatmail_prober.pool.Rpc")
    def test_reopen_restarts_rpc_and_keeps_relays(self, MockRpc, tmp_path):
        pool = RelayPool(tmp_path)
        pool.open_all(["a.example"])
        pool.reopen()
        assert MockRpc.call_count == 2
        assert "a.example" in pool.contexts()

    @patch("chatmail_prober.pool.Rpc")
    def test_prune_forgets_relays(self, MockRpc, tmp_path):
        pool = RelayPool(tmp_path)
        pool.open_all(["a.example", "b.example", "c.example"])
        pool.prune(["a.example", "c.example"])
        assert set(pool.contexts().keys()) == {"a.example", "c.example"}

    @patch("chatmail_prober.pool.Rpc")
    def test_contexts_duck_type_for_perform_direct_ping(self, MockRpc, tmp_path):
        """Each context yielded by the pool must expose .maker (consumed by _perform_direct_ping)."""
        pool = RelayPool(tmp_path)
        pool.open_all(["a.example"])
        ctx = pool.contexts()["a.example"]
        assert ctx.maker is pool.maker is not None
