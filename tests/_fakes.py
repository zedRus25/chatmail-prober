"""Shared in-memory fakes for the deltachat layer.

These replace a real deltachat-rpc-server for the logic tests in
test_accounts.py and test_pinger.py.  They live in one place so the fake's
behaviour stays consistent and can be kept honest against the real client
(see test_core_contract.py, which pins the same API surface against the
actual server).

All threading/queue primitives are real; only the deltachat objects are faked.
"""

from __future__ import annotations

import queue
from typing import Any


class FakeRpc:
    """Rpc stub backed by real queue.Queue instances (one per account id)."""

    def __init__(self) -> None:
        self._queues: dict[int, queue.Queue[Any]] = {}

    def get_queue(self, account_id: int) -> queue.Queue[Any]:
        if account_id not in self._queues:
            self._queues[account_id] = queue.Queue()
        return self._queues[account_id]


class FakeChat:
    def send_text(self, text: str) -> None:
        pass  # send_pings calls this; a no-op is enough


class FakeContact:
    def create_chat(self) -> FakeChat:
        return FakeChat()


class FakeAccount:
    """Account stub with local config plus call counters for reuse assertions."""

    def __init__(self, rpc: FakeRpc, account_id: int, domain: str) -> None:
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
        # create_qr_url() returns "dcaccount:<domain>"; strip the scheme.
        # The real core would set configured_addr to "<user>@<domain>".
        if qr_url.startswith("dcaccount:"):
            domain = qr_url[len("dcaccount:"):]
        else:
            domain = qr_url.rsplit("@", maxsplit=1)[-1]
        self._config["configured_addr"] = f"newuser{self.id}@{domain}"

    def start_io(self) -> None:
        self.start_io_called += 1

    def create_contact(self, other: Any) -> FakeContact:
        return FakeContact()


class FakeDeltaChat:
    """DeltaChat stub that tracks account creation on one domain."""

    def __init__(self, rpc: FakeRpc, domain: str) -> None:
        self._rpc = rpc
        self._domain = domain
        self._accounts: list[FakeAccount] = []
        self.add_account_calls = 0

    def get_all_accounts(self) -> list[FakeAccount]:
        return list(self._accounts)

    def add_account(self) -> FakeAccount:
        self.add_account_calls += 1
        acct = FakeAccount(self._rpc, len(self._accounts) + 1, self._domain)
        # New accounts start unconfigured (no configured_addr until QR setup).
        acct._config.pop("configured_addr", None)
        self._accounts.append(acct)
        return acct
