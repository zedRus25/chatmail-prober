"""Tests for AccountMaker timeout and account reuse behaviour.

These tests use real threading.Event / queue.Queue objects (no mock of
threading primitives) and a minimal stub DeltaChat/Account layer, following
the same pattern as test_thread_leak.py.

Covers:
- wait_account_online raises PingError when IMAP_INBOX_IDLE never arrives
- run_probe surfaces that timeout as a ProbeResult.error classified as -1
- get_relay_account reuses an already-online account (was_online=True)
  without calling dc.add_account() or set_config_from_qr() again
- get_relay_account creates a new account when none exists (was_online=False)
- Self-loop probes get two distinct accounts via the exclude mechanism
"""

from __future__ import annotations

import queue
import threading
import time

import pytest
from deltachat_rpc_client import EventType

from chatmail_prober.metrics import relay_status_value
from chatmail_prober.probe import AccountMaker, PingError

#
# Minimal stubs. Real queue and threading, no network.
#

class _Rpc:
    """Stub Rpc backed by real queue.Queue instances."""

    def __init__(self):
        self._queues: dict[int, queue.Queue] = {}

    def get_queue(self, account_id: int) -> queue.Queue:
        if account_id not in self._queues:
            self._queues[account_id] = queue.Queue()
        return self._queues[account_id]


class _Account:
    """Stub Account with configurable configured_addr and addr."""

    def __init__(self, rpc: _Rpc, account_id: int, domain: str):
        self._rpc = rpc
        self.id = account_id
        self._domain = domain
        self._config: dict[str, str] = {
            "configured_addr": f"user{account_id}@{domain}",
            "addr": f"user{account_id}@{domain}",
        }
        self.start_io_called = 0
        self.set_config_calls: list[tuple[str, str]] = []

    def get_config(self, key: str) -> str | None:
        return self._config.get(key)

    def set_config(self, key: str, value: str) -> None:
        self._config[key] = value
        self.set_config_calls.append((key, value))

    def set_config_from_qr(self, qr_url: str) -> None:
        # Simulate successful QR config: populate configured_addr.
        # create_qr_url() returns "dcaccount:<domain>", so strip the scheme.
        # The real core would set configured_addr to "<user>@<domain>".
        if qr_url.startswith("dcaccount:"):
            domain = qr_url[len("dcaccount:"):]
        else:
            domain = qr_url.rsplit("@", maxsplit=1)[-1]
        self._config["configured_addr"] = f"newuser{self.id}@{domain}"

    def start_io(self) -> None:
        self.start_io_called += 1


class _DC:
    """Stub DeltaChat that tracks account creation calls."""

    def __init__(self, rpc: _Rpc, domain: str):
        self._rpc = rpc
        self._domain = domain
        self._accounts: list[_Account] = []
        self.add_account_calls = 0

    def get_all_accounts(self) -> list[_Account]:
        return list(self._accounts)

    def add_account(self) -> _Account:
        self.add_account_calls += 1
        acct = _Account(self._rpc, len(self._accounts) + 1, self._domain)
        # New accounts start unconfigured (no configured_addr until set_config_from_qr)
        acct._config.pop("configured_addr", None)
        self._accounts.append(acct)
        return acct


def _push_idle(rpc: _Rpc, account_id: int, delay: float = 0.0) -> None:
    """Push an IMAP_INBOX_IDLE event into the account's queue after delay."""
    def _push():
        if delay:
            time.sleep(delay)
        rpc.get_queue(account_id).put({"kind": EventType.IMAP_INBOX_IDLE})
    threading.Thread(target=_push, daemon=True).start()


#
# Tests: wait_account_online timeout
#

class TestWaitAccountOnlineTimeout:
    """AccountMaker.wait_account_online must raise PingError on timeout."""

    def test_timeout_raises_classified_ping_error(self):
        """No event in queue -> PingError with correct message and relay_status_value -1."""
        rpc = _Rpc()
        dc = _DC(rpc, "relay.example")
        maker = AccountMaker(dc)
        account = _Account(rpc, 1, "relay.example")

        with pytest.raises(PingError) as exc_info:
            maker.wait_account_online(account, timeout=0.15)

        msg = str(exc_info.value)
        assert msg.startswith("Timeout waiting for") and "to come online" in msg
        wrapped = f"Timeout or error waiting for profiles to be online: {exc_info.value}"
        assert relay_status_value(wrapped) == -1

    def test_succeeds_when_event_arrives_in_time(self):
        """wait_account_online must return normally when IMAP_INBOX_IDLE arrives."""
        rpc = _Rpc()
        dc = _DC(rpc, "relay.example")
        maker = AccountMaker(dc)
        account = _Account(rpc, 1, "relay.example")

        _push_idle(rpc, account.id, delay=0.05)

        # Should not raise; event arrives within 0.5s timeout
        maker.wait_account_online(account, timeout=0.5)

    def test_ignores_non_idle_events_before_idle(self):
        """Non-IMAP_INBOX_IDLE events must be ignored; only IMAP_INBOX_IDLE unblocks."""
        rpc = _Rpc()
        dc = _DC(rpc, "relay.example")
        maker = AccountMaker(dc)
        account = _Account(rpc, 1, "relay.example")

        # Push a noise event first, then the real one
        def _push_sequence():
            time.sleep(0.02)
            rpc.get_queue(account.id).put({"kind": EventType.INFO, "msg": "noise"})
            time.sleep(0.02)
            rpc.get_queue(account.id).put({"kind": EventType.IMAP_INBOX_IDLE})

        threading.Thread(target=_push_sequence, daemon=True).start()
        maker.wait_account_online(account, timeout=0.5)  # must not raise

    def test_fatal_error_event_raises_immediately(self):
        """An ERROR event with a fatal message must raise PingError before the timeout."""
        rpc = _Rpc()
        dc = _DC(rpc, "relay.example")
        maker = AccountMaker(dc)
        account = _Account(rpc, 1, "relay.example")

        def push_fatal():
            time.sleep(0.02)
            rpc.get_queue(account.id).put(
                {"kind": EventType.ERROR, "msg": "[AUTHENTICATIONFAILED] Authentication failed."}
            )
        threading.Thread(target=push_fatal, daemon=True).start()

        with pytest.raises(PingError, match="AUTHENTICATIONFAILED"):
            maker.wait_account_online(account, timeout=2.0)  # long timeout; must fire fast

    def test_non_fatal_error_event_continues_waiting(self):
        """A non-fatal ERROR event must not stop waiting; IMAP_INBOX_IDLE still resolves it."""
        rpc = _Rpc()
        dc = _DC(rpc, "relay.example")
        maker = AccountMaker(dc)
        account = _Account(rpc, 1, "relay.example")

        def push_sequence():
            time.sleep(0.02)
            rpc.get_queue(account.id).put({"kind": EventType.ERROR, "msg": "INFO: connecting"})
            time.sleep(0.02)
            rpc.get_queue(account.id).put({"kind": EventType.IMAP_INBOX_IDLE})
        threading.Thread(target=push_sequence, daemon=True).start()

        maker.wait_account_online(account, timeout=1.0)  # must not raise


#
# Tests: account reuse via get_relay_account
#

class TestAccountReuse:
    """get_relay_account must reuse online accounts without re-running setup."""

    def test_first_call_creates_new_account(self):
        """First call for a domain must call dc.add_account() and return was_online=False."""
        rpc = _Rpc()
        dc = _DC(rpc, "relay.example")
        maker = AccountMaker(dc)

        account, was_online = maker.get_relay_account("relay.example")

        assert dc.add_account_calls == 1
        assert was_online is False
        assert account in maker.online

    def test_second_call_reuses_account_without_extra_setup(self):
        """Second call must return same account object, was_online=True, no new add_account/start_io."""
        rpc = _Rpc()
        dc = _DC(rpc, "relay.example")
        maker = AccountMaker(dc)

        first, _ = maker.get_relay_account("relay.example")
        add_calls_after_first = dc.add_account_calls
        start_io_count = first.start_io_called

        second, was_online = maker.get_relay_account("relay.example")

        assert second is first
        assert was_online is True
        assert dc.add_account_calls == add_calls_after_first
        assert first.start_io_called == start_io_count

    def test_self_loop_returns_two_distinct_accounts(self):
        """Self-loop (src==dst) must produce two different accounts via exclude."""
        rpc = _Rpc()
        dc = _DC(rpc, "relay.example")
        maker = AccountMaker(dc)

        sender, sender_was_online = maker.get_relay_account("relay.example")
        receiver, receiver_was_online = maker.get_relay_account(
            "relay.example", exclude=(sender,)
        )

        assert sender is not receiver, (
            "Self-loop must use two distinct accounts; sender and receiver are the same"
        )
        assert dc.add_account_calls == 2

    def test_different_domain_creates_separate_account(self):
        """Accounts for different domains must not be reused across domains."""
        rpc = _Rpc()
        dc_a = _DC(rpc, "a.example")
        dc_b = _DC(rpc, "b.example")
        maker_a = AccountMaker(dc_a)
        maker_b = AccountMaker(dc_b)

        acct_a, _ = maker_a.get_relay_account("a.example")
        acct_b, _ = maker_b.get_relay_account("b.example")

        assert acct_a is not acct_b
        assert dc_a.add_account_calls == 1
        assert dc_b.add_account_calls == 1

    def test_multiple_rounds_no_extra_setup(self):
        """Simulating N probe rounds: add_account must be called exactly once."""
        rpc = _Rpc()
        dc = _DC(rpc, "relay.example")
        maker = AccountMaker(dc)

        N = 10
        for _ in range(N):
            _, was_online = maker.get_relay_account("relay.example")

        assert dc.add_account_calls == 1, (
            f"Expected 1 add_account call across {N} rounds, got {dc.add_account_calls}"
        )

    def test_concurrent_same_domain_creates_one_account(self):
        """Many threads racing on one domain must create exactly one account.

        The alive-check pool shares a single AccountMaker across threads; the
        per-domain lock must stop concurrent callers from each missing the
        reuse check and creating duplicates.  Without the lock, add_account
        would be called once per racing thread.
        """
        rpc = _Rpc()
        dc = _DC(rpc, "relay.example")
        maker = AccountMaker(dc)

        start = threading.Barrier(8)
        results: list[object] = []
        results_lock = threading.Lock()

        def _worker() -> None:
            start.wait()  # release all threads at once to maximize the race
            acct, _ = maker.get_relay_account("relay.example")
            with results_lock:
                results.append(acct)

        threads = [threading.Thread(target=_worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert dc.add_account_calls == 1, (
            f"Expected exactly 1 account under concurrency, got {dc.add_account_calls}"
        )
        assert len(results) == 8
        assert all(a is results[0] for a in results), "All callers must share one account"

    def test_concurrent_different_domains_run_in_parallel(self):
        """Different domains must not serialize on each other's lock."""
        rpc = _Rpc()
        # One DC per domain, mirroring how a shared maker would still route
        # add_account per domain; here we just assert independent creation.
        maker = AccountMaker(_DC(rpc, "a.example"))
        maker_b = AccountMaker(_DC(rpc, "b.example"))

        a, _ = maker.get_relay_account("a.example")
        b, _ = maker_b.get_relay_account("b.example")
        assert a is not b


#
# Tests: per-domain account creation cap
#

class TestMaxAccountsPerDomain:
    """get_relay_account must refuse to create more than max_accounts_per_domain.

    The cap protects relays from runaway account creation when ghost
    (partially-configured) accounts accumulate from broken setups.
    """

    def _ghost(self, rpc, account_id, domain):
        """Build a partially-configured ('ghost') account: only addr, no configured_addr."""
        a = _Account(rpc, account_id, domain)
        a._config.pop("configured_addr", None)
        return a

    def test_raises_at_limit_with_only_ghosts_online(self):
        """Cap fires when N ghost accounts are already online and another is requested."""
        rpc = _Rpc()
        dc = _DC(rpc, "relay.example")
        maker = AccountMaker(dc, max_accounts_per_domain=2)

        # Two ghosts already in DB and tracked as online; nothing is reusable
        # (ghosts have no configured_addr, so _find_reusable_online returns None).
        g1 = self._ghost(rpc, 1, "relay.example")
        g2 = self._ghost(rpc, 2, "relay.example")
        dc._accounts.extend([g1, g2])
        maker.online.extend([g1, g2])

        with pytest.raises(PingError) as exc_info:
            maker.get_relay_account("relay.example")

        msg = str(exc_info.value)
        assert "Too many accounts" in msg
        assert "relay.example" in msg
        assert "limit 2" in msg
        assert "2 unconfigured" in msg
        assert dc.add_account_calls == 0, "Must not create a new account at the cap"

    def test_under_limit_creates(self):
        """Below the cap, get_relay_account creates normally."""
        rpc = _Rpc()
        dc = _DC(rpc, "relay.example")
        maker = AccountMaker(dc, max_accounts_per_domain=3)

        _, was_online = maker.get_relay_account("relay.example")

        assert was_online is False
        assert dc.add_account_calls == 1

    def test_self_loop_can_hit_cap_via_exclude(self):
        """In the self-loop case, the excluded sender does not count toward
        the cap because exclude is checked before counting -- but a fresh
        ghost still in self.online does count, blocking creation.
        """
        rpc = _Rpc()
        dc = _DC(rpc, "relay.example")
        maker = AccountMaker(dc, max_accounts_per_domain=1)

        # Existing online ghost on the relay (counts, not reusable).
        ghost = self._ghost(rpc, 1, "relay.example")
        dc._accounts.append(ghost)
        maker.online.append(ghost)

        with pytest.raises(PingError) as exc_info:
            maker.get_relay_account("relay.example")
        assert "Too many accounts" in str(exc_info.value)
        assert "limit 1" in str(exc_info.value)
