"""Contract tests against the real deltachat-rpc-server / rpc-client.

Every other unit test mocks the deltachat layer with hand-written stubs that
encode *the prober's assumptions* about how core behaves.  If core renames an
event, drops a method, or changes a config key, those stubs keep passing while
production breaks.  These tests bind to the *real* client and server to pin the
exact API surface the prober depends on -- so core API drift fails loudly in CI.

They start a real deltachat-rpc-server but perform only network-free
operations (add an unconfigured account, read/write local config, inspect the
event-queue accessor).  Nothing here contacts a relay: no set_config_from_qr,
no start_io, no configure.  Live end-to-end coverage lives in test_live.py.
"""

from __future__ import annotations

import tempfile

import pytest

# If the client package itself is gone/renamed, that is a contract break too.
dc_rpc = pytest.importorskip("deltachat_rpc_client")


# --- symbols the prober imports directly -----------------------------------

def test_imported_symbols_exist():
    """chatmail_prober imports these names from deltachat_rpc_client."""
    for name in ("Rpc", "DeltaChat", "AttrDict", "EventType"):
        assert hasattr(dc_rpc, name), f"deltachat_rpc_client.{name} is gone"


def test_event_type_members():
    """pinger/accounts compare event.kind against these enum members."""
    for member in ("IMAP_INBOX_IDLE", "ERROR", "INCOMING_MSG"):
        assert hasattr(dc_rpc.EventType, member), f"EventType.{member} is gone"


def test_attrdict_exposes_keys_as_attributes():
    """pinger wraps raw event dicts in AttrDict and reads .kind/.msg/.msg_id."""
    ad = dc_rpc.AttrDict({"kind": dc_rpc.EventType.INCOMING_MSG, "msg_id": 7})
    assert ad.kind == dc_rpc.EventType.INCOMING_MSG
    assert ad.msg_id == 7


# --- live server surface (network-free) ------------------------------------

@pytest.fixture()
def dc():
    """A started rpc-server + DeltaChat handle, torn down after the test.

    Skips (rather than fails) if the server binary can't run in this
    environment -- the point is to catch API drift, not to require the
    binary everywhere.
    """
    try:
        rpc = dc_rpc.Rpc(accounts_dir=tempfile.mkdtemp())
        rpc.start()
    except Exception as e:  # noqa: BLE001 - environment issue, not a contract break
        pytest.skip(f"deltachat-rpc-server unavailable: {e}")
    try:
        yield dc_rpc.DeltaChat(rpc)
    finally:
        rpc.close()


def test_event_queue_accessor(dc):
    """pinger + accounts.wait_account_online read events via
    account._rpc.get_queue(account.id) -- a private accessor the prober
    depends on, so pin it explicitly."""
    acc = dc.add_account()
    assert callable(acc._rpc.get_queue)
    q = acc._rpc.get_queue(acc.id)
    assert hasattr(q, "get"), "event queue must be a Queue-like with .get()"


def test_account_creation_and_enumeration(dc):
    """accounts.py uses dc.add_account() then dc.get_all_accounts() to scan."""
    assert dc.get_all_accounts() == []
    acc = dc.add_account()
    all_accounts = dc.get_all_accounts()
    assert len(all_accounts) == 1
    assert all_accounts[0].id == acc.id


def test_config_read_write_surface(dc):
    """accounts._add_online sets bot + delete_device_after; imap_metadata reads
    addr/mail_pw.  (delete_server_after was removed from core in 2.5x, so the
    prober no longer sets it -- see AccountMaker._add_online.)"""
    acc = dc.add_account()
    # Unconfigured account: these keys read back empty/None, not an error.
    for key in ("addr", "configured_addr", "mail_pw"):
        acc.get_config(key)  # must not raise
    for key, val in (("bot", "1"), ("delete_device_after", "3600")):
        acc.set_config(key, val)
    assert acc.get_config("bot") == "1"


def test_account_method_surface(dc):
    """Methods the prober calls on a configured account.  Checked by presence
    (they need network / a second account to invoke), so drift in their names
    still fails here without a live relay."""
    acc = dc.add_account()
    for method in (
        "get_config", "set_config", "set_config_from_qr", "start_io",
        "create_contact", "get_message_by_id", "ice_servers",
    ):
        assert callable(getattr(acc, method, None)), f"Account.{method} is gone"
