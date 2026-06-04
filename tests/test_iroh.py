"""Tests for chatmail_prober.iroh."""

from __future__ import annotations

import socket
import urllib.error
from unittest.mock import MagicMock

import pytest

from chatmail_prober import iroh
from chatmail_prober.imap_metadata import ImapMetadataError
from chatmail_prober.iroh import (
    IROH_METADATA_KEY,
    IrohResult,
    IrohStatus,
    check_iroh,
    check_relay_iroh,
    resolve_iroh_url,
)
from chatmail_prober.metrics import relay_iroh_latency_seconds, relay_iroh_status


@pytest.fixture(autouse=True)
def _clear_iroh_metrics():
    relay_iroh_status._metrics.clear()
    relay_iroh_latency_seconds._metrics.clear()
    yield


#
# resolve_iroh_url
#

def _account(addr: str = "u@example.com", pw: str = "secret") -> MagicMock:
    acct = MagicMock()
    acct.get_config.side_effect = lambda k: {
        "configured_addr": addr,
        "addr": addr,
        "mail_pw": pw,
    }.get(k)
    return acct


def test_resolve_returns_url(monkeypatch):
    seen: dict[str, str] = {}

    def fake_fetch(creds, entry, timeout=15.0):
        seen["entry"] = entry
        seen["host"] = creds.host
        return "https://iroh.example/"

    monkeypatch.setattr(iroh, "fetch_metadata_entry", fake_fetch)
    assert resolve_iroh_url(_account()) == "https://iroh.example/"
    assert seen["entry"] == IROH_METADATA_KEY
    assert seen["host"] == "example.com"


def test_resolve_returns_none_when_unconfigured(monkeypatch):
    acct = MagicMock()
    acct.get_config.return_value = None  # no addr, no pw
    # Should never even call fetch.
    monkeypatch.setattr(iroh, "fetch_metadata_entry",
                        lambda *a, **k: pytest.fail("should not be called"))
    assert resolve_iroh_url(acct) is None


def test_resolve_returns_none_when_metadata_absent(monkeypatch):
    monkeypatch.setattr(iroh, "fetch_metadata_entry", lambda *a, **k: None)
    assert resolve_iroh_url(_account()) is None


def test_resolve_propagates_imap_failed(monkeypatch):
    def boom(*a, **k):
        raise ImapMetadataError("connect refused")
    monkeypatch.setattr(iroh, "fetch_metadata_entry", boom)
    with pytest.raises(ImapMetadataError):
        resolve_iroh_url(_account())


#
# check_iroh -- HTTP outcomes
#

class _FakeResp:
    def __init__(self, status):
        self.status = status
    def __enter__(self):
        return self
    def __exit__(self, *exc):
        pass


def test_check_iroh_ok(monkeypatch):
    monkeypatch.setattr(iroh.urllib.request, "urlopen",
                        lambda url, timeout: _FakeResp(200))
    r = check_iroh("https://x/", timeout=1.0)
    assert r.status == IrohStatus.OK
    assert r.http_status == 200
    assert r.latency_s is not None and r.latency_s >= 0


def test_check_iroh_non_2xx(monkeypatch):
    monkeypatch.setattr(iroh.urllib.request, "urlopen",
                        lambda url, timeout: _FakeResp(503))
    r = check_iroh("https://x/", timeout=1.0)
    assert r.status == IrohStatus.DOWN
    assert r.http_status == 503


def test_check_iroh_http_error(monkeypatch):
    def boom(url, timeout):
        raise urllib.error.HTTPError(url, 502, "bad gateway", {}, None)
    monkeypatch.setattr(iroh.urllib.request, "urlopen", boom)
    r = check_iroh("https://x/", timeout=1.0)
    assert r.status == IrohStatus.DOWN
    assert r.http_status == 502


def test_check_iroh_url_error_connection_refused(monkeypatch):
    def boom(url, timeout):
        raise urllib.error.URLError("connection refused")
    monkeypatch.setattr(iroh.urllib.request, "urlopen", boom)
    r = check_iroh("https://x/", timeout=1.0)
    assert r.status == IrohStatus.DOWN
    assert "refused" in r.error


def test_check_iroh_socket_timeout(monkeypatch):
    def boom(url, timeout):
        raise socket.timeout("timed out")
    monkeypatch.setattr(iroh.urllib.request, "urlopen", boom)
    r = check_iroh("https://x/", timeout=1.0)
    assert r.status == IrohStatus.TIMEOUT


def test_check_iroh_url_error_wrapping_timeout(monkeypatch):
    def boom(url, timeout):
        raise urllib.error.URLError(socket.timeout("timed out"))
    monkeypatch.setattr(iroh.urllib.request, "urlopen", boom)
    r = check_iroh("https://x/", timeout=1.0)
    assert r.status == IrohStatus.TIMEOUT


#
# check_relay_iroh -- end-to-end sentinel mapping
#

def test_check_relay_iroh_no_metadata(monkeypatch):
    monkeypatch.setattr(iroh, "fetch_metadata_entry", lambda *a, **k: None)
    r = check_relay_iroh(_account())
    assert r.status == IrohStatus.NO_METADATA
    assert r.url is None


def test_check_relay_iroh_imap_failed(monkeypatch):
    def boom(*a, **k):
        raise ImapMetadataError("login failed")
    monkeypatch.setattr(iroh, "fetch_metadata_entry", boom)
    r = check_relay_iroh(_account())
    assert r.status == IrohStatus.IMAP_FAILED
    assert "login" in r.error


def test_check_relay_iroh_unconfigured_account(monkeypatch):
    acct = MagicMock()
    acct.get_config.return_value = None
    monkeypatch.setattr(iroh, "fetch_metadata_entry",
                        lambda *a, **k: pytest.fail("should not be called"))
    r = check_relay_iroh(acct)
    assert r.status == IrohStatus.NO_METADATA


def test_check_relay_iroh_ok(monkeypatch):
    monkeypatch.setattr(iroh, "fetch_metadata_entry",
                        lambda *a, **k: "https://relay.example/")
    monkeypatch.setattr(iroh.urllib.request, "urlopen",
                        lambda url, timeout: _FakeResp(200))
    r = check_relay_iroh(_account())
    assert r.status == IrohStatus.OK
    assert r.url == "https://relay.example/"


#
# update_iroh_metrics -- gauge wiring
#

def test_update_iroh_metrics_ok_sets_latency():
    from chatmail_prober.metrics import (
        relay_iroh_latency_seconds,
        relay_iroh_status,
        update_iroh_metrics,
    )
    update_iroh_metrics("relay-ok.test", IrohResult(
        status=IrohStatus.OK, url="https://x/", latency_s=0.123,
        http_status=200,
    ))
    assert relay_iroh_status.labels(relay="relay-ok.test")._value.get() == 1
    assert (
        relay_iroh_latency_seconds.labels(relay="relay-ok.test")._value.get()
        == 0.123
    )


def test_update_iroh_metrics_no_metadata_skips_latency():
    from chatmail_prober.metrics import (
        relay_iroh_latency_seconds,
        relay_iroh_status,
        update_iroh_metrics,
    )
    relay = "relay-nometa.test"
    # Pre-set latency to a sentinel, confirm it is NOT overwritten.
    relay_iroh_latency_seconds.labels(relay=relay).set(99.0)
    update_iroh_metrics(relay, IrohResult(status=IrohStatus.NO_METADATA))
    assert relay_iroh_status.labels(relay=relay)._value.get() == -2
    assert relay_iroh_latency_seconds.labels(relay=relay)._value.get() == 99.0
