"""Tests for chatmail_prober.imap_metadata."""

from __future__ import annotations

import imaplib
from unittest.mock import MagicMock

import pytest

from chatmail_prober import imap_metadata
from chatmail_prober.imap_metadata import (
    ImapCreds,
    ImapMetadataError,
    creds_from_account,
    fetch_metadata_entry,
)

ENTRY = "/shared/vendor/deltachat/irohrelay"


#
# creds_from_account
#

def _account_with(config: dict[str, str | None]) -> MagicMock:
    acct = MagicMock()
    acct.get_config.side_effect = lambda k: config.get(k)
    return acct


def test_creds_from_account_complete():
    acct = _account_with({
        "configured_addr": "u@relay.example",
        "mail_pw": "secret",
    })
    creds = creds_from_account(acct)
    assert creds == ImapCreds("relay.example", 993, "u@relay.example", "secret")


def test_creds_from_account_falls_back_to_addr_when_configured_addr_missing():
    acct = _account_with({
        "addr": "u@relay.example",
        "mail_pw": "secret",
    })
    assert creds_from_account(acct) == ImapCreds(
        "relay.example", 993, "u@relay.example", "secret",
    )


def test_creds_from_account_missing_password():
    acct = _account_with({"configured_addr": "u@relay.example"})
    assert creds_from_account(acct) is None


def test_creds_from_account_missing_addr():
    acct = _account_with({"mail_pw": "secret"})
    assert creds_from_account(acct) is None


def test_creds_from_account_malformed_addr():
    acct = _account_with({
        "configured_addr": "no-at-sign-here",
        "mail_pw": "secret",
    })
    assert creds_from_account(acct) is None


#
# fetch_metadata_entry -- IMAP transport faked at the imaplib.IMAP4_SSL class
#

class _FakeImap:
    """Minimal stand-in for imaplib.IMAP4_SSL.

    Records calls (`login`, `_simple_command`, `_untagged_response`,
    `logout`) and returns canned values configured via class attrs.
    """
    # Set by tests before instantiation.
    login_raises: Exception | None = None
    connect_raises: Exception | None = None
    command_status: str = "OK"
    untagged_lines: list = []

    def __init__(self, host, port, timeout=None):  # noqa: D401 - test stub
        if _FakeImap.connect_raises is not None:
            raise _FakeImap.connect_raises
        self.host = host
        self.port = port
        self.timeout = timeout
        self.calls: list[tuple] = []
        self.logged_out = False

    def login(self, user, pw):
        self.calls.append(("login", user, pw))
        if _FakeImap.login_raises is not None:
            raise _FakeImap.login_raises

    def _simple_command(self, *args):
        self.calls.append(("_simple_command", *args))
        return _FakeImap.command_status, []

    def _untagged_response(self, typ, dat, name):
        self.calls.append(("_untagged_response", typ, name))
        return typ, list(_FakeImap.untagged_lines)

    def logout(self):
        self.logged_out = True


@pytest.fixture(autouse=True)
def _reset_fake_imap(monkeypatch):
    _FakeImap.login_raises = None
    _FakeImap.connect_raises = None
    _FakeImap.command_status = "OK"
    _FakeImap.untagged_lines = []
    monkeypatch.setattr(imap_metadata.imaplib, "IMAP4_SSL", _FakeImap)


CREDS = ImapCreds("imap.example.com", 993, "u", "p")


def test_fetch_returns_quoted_url():
    _FakeImap.untagged_lines = [
        b'"" (/shared/vendor/deltachat/irohrelay "https://iroh.example/")',
    ]
    assert fetch_metadata_entry(CREDS, ENTRY) == "https://iroh.example/"


def test_fetch_returns_literal_value():
    # Verbatim shape captured from a real Dovecot GETMETADATA reply:
    #   [(b'"" (/path {N}', b'<N bytes>'), b')']
    _FakeImap.untagged_lines = [
        (
            b'"" (/shared/vendor/deltachat/irohrelay {19}',
            b"https://iroh.example/",
        ),
        b")",
    ]
    assert fetch_metadata_entry(CREDS, ENTRY) == "https://iroh.example/"


def test_fetch_returns_none_on_nil():
    _FakeImap.untagged_lines = [
        b'"" (/shared/vendor/deltachat/irohrelay NIL)',
    ]
    assert fetch_metadata_entry(CREDS, ENTRY) is None


def test_fetch_returns_none_when_entry_absent():
    _FakeImap.untagged_lines = []
    assert fetch_metadata_entry(CREDS, ENTRY) is None


def test_fetch_returns_none_when_only_other_entries_present():
    _FakeImap.untagged_lines = [
        b'"" (/shared/vendor/deltachat/turn "host:port:1:pw")',
    ]
    assert fetch_metadata_entry(CREDS, ENTRY) is None


def test_fetch_raises_on_connect_failure():
    _FakeImap.connect_raises = OSError("network unreachable")
    with pytest.raises(ImapMetadataError, match="connect"):
        fetch_metadata_entry(CREDS, ENTRY)


def test_fetch_raises_on_login_failure():
    _FakeImap.login_raises = imaplib.IMAP4.error("AUTHENTICATIONFAILED")
    with pytest.raises(ImapMetadataError, match="session"):
        fetch_metadata_entry(CREDS, ENTRY)


def test_fetch_raises_on_bad_command_status():
    _FakeImap.command_status = "NO"
    with pytest.raises(ImapMetadataError, match="NO"):
        fetch_metadata_entry(CREDS, ENTRY)


def test_fetch_calls_logout_on_success():
    sessions: list[_FakeImap] = []
    real_init = _FakeImap.__init__

    def capturing_init(self, *a, **kw):
        real_init(self, *a, **kw)
        sessions.append(self)

    _FakeImap.__init__ = capturing_init  # type: ignore[method-assign]
    try:
        _FakeImap.untagged_lines = [
            b'"" (/shared/vendor/deltachat/irohrelay "https://x/")',
        ]
        fetch_metadata_entry(CREDS, ENTRY)
        assert sessions and sessions[0].logged_out
    finally:
        _FakeImap.__init__ = real_init  # type: ignore[method-assign]
