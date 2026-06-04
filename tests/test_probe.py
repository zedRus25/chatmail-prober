"""Tests for the prober (vendored direct-ping logic)."""

import urllib.parse
from unittest.mock import MagicMock, patch

import pytest
from deltachat_rpc_client.rpc import JsonRpcError

from chatmail_prober.metrics import relay_status_value
from chatmail_prober.probe import (
    _FATAL_CATEGORIES,
    AccountMaker,
    PingError,
    ProbeResult,
    _classify_error,
    create_qr_url,
    is_ip_address,
    run_probe,
)


class FakePinger:
    """Minimal stand-in for Pinger returned by _perform_direct_ping()."""
    def __init__(self, sent=3, received=3, loss=0.0, results=None,
                 account_setup_time=0.5, message_time=2.0):
        self.sent = sent
        self.received = received
        self.loss = loss
        self.results = results or [(0, 400.0), (1, 500.0), (2, 600.0)]
        self.account_setup_time = account_setup_time
        self.message_time = message_time


class TestRunProbeSuccess:
    @patch("chatmail_prober.probe._perform_direct_ping")
    def test_returns_probe_result(self, mock_ping):
        mock_ping.return_value = FakePinger()
        contexts = {"a.example": MagicMock(), "b.example": MagicMock()}
        result = run_probe("a.example", "b.example", count=3, relay_contexts=contexts)

        assert isinstance(result, ProbeResult)
        assert result.source == "a.example"
        assert result.destination == "b.example"
        assert result.sent == 3
        assert result.received == 3
        assert result.loss == 0.0
        assert result.error is None

    @patch("chatmail_prober.probe._perform_direct_ping")
    def test_rtts_extracted_from_results(self, mock_ping):
        mock_ping.return_value = FakePinger(
            results=[(0, 123.4), (1, 567.8)]
        )
        contexts = {"a.example": MagicMock(), "b.example": MagicMock()}
        result = run_probe("a.example", "b.example", count=2, relay_contexts=contexts)
        assert result.rtts_ms == [123.4, 567.8]

    @patch("chatmail_prober.probe._perform_direct_ping")
    def test_timing_data_propagated(self, mock_ping):
        mock_ping.return_value = FakePinger(
            account_setup_time=1.1, message_time=3.3,
        )
        contexts = {"a.example": MagicMock(), "b.example": MagicMock()}
        result = run_probe("a.example", "b.example", count=3, relay_contexts=contexts)
        assert result.account_setup_time == pytest.approx(1.1)
        assert result.message_time == pytest.approx(3.3)


class TestRunProbeErrors:
    @patch("chatmail_prober.probe._perform_direct_ping")
    def test_ping_error_returns_error_result(self, mock_ping):
        mock_ping.side_effect = PingError("setup failed")
        contexts = {"a.example": MagicMock(), "b.example": MagicMock()}
        result = run_probe("a.example", "b.example", count=1, relay_contexts=contexts)

        assert result.error == "setup failed"
        assert result.sent == 0
        assert result.received == 0
        assert result.loss == 100.0
        assert result.rtts_ms == []

    @patch("chatmail_prober.probe._perform_direct_ping")
    def test_unexpected_exception_returns_error_result(self, mock_ping):
        mock_ping.side_effect = RuntimeError("something broke")
        contexts = {"a.example": MagicMock(), "b.example": MagicMock()}
        result = run_probe("a.example", "b.example", count=1, relay_contexts=contexts)

        assert result.error == "something broke"
        assert result.sent == 0

    @patch("chatmail_prober.probe._perform_direct_ping")
    def test_error_result_preserves_source_dest(self, mock_ping):
        mock_ping.side_effect = PingError("fail")
        contexts = {"src.example": MagicMock(), "dst.example": MagicMock()}
        result = run_probe("src.example", "dst.example", count=1,
                           relay_contexts=contexts)

        assert result.source == "src.example"
        assert result.destination == "dst.example"


class TestRunProbeWithContexts:
    @patch("chatmail_prober.probe._perform_direct_ping")
    def test_error_with_contexts(self, mock_ping):
        mock_ping.side_effect = PingError("rpc failed")
        contexts = {"a.example": MagicMock()}
        result = run_probe("a.example", "b.example", count=1, relay_contexts=contexts)

        assert result.error == "rpc failed"

    @patch("chatmail_prober.probe.RelayContext")
    @patch("chatmail_prober.probe._perform_direct_ping")
    def test_without_contexts_creates_temporary(self, mock_ping, MockCtx):
        """When relay_contexts is None, creates temporary RelayContexts."""
        mock_ping.return_value = FakePinger()
        result = run_probe("a.example", "b.example", count=3, accounts_dir="/tmp/c")

        mock_ping.assert_called_once()
        assert result.sent == 3


@pytest.mark.parametrize(("error", "category", "is_fatal"), [
    (None, None, False),
    # dns: two distinct keywords
    ("IMAP failed to connect: Could not find DNS resolutions for imap.a.example:993", "dns", True),
    ("Name or service not known", "dns", True),
    # auth
    ("Cannot login as user@a.example: authentication failed", "auth", True),
    # timeout: two distinct keywords
    ("Connection timeout: deadline has elapsed", "timeout", False),
    ("Connection timed out", "timeout", False),
    # tls: two distinct keywords
    ("SSL certificate verify failed", "tls", True),
    ("certificate has expired", "tls", True),
    # connection_refused: two distinct keywords
    ("Connection refused to imap.a.example:993", "connection_refused", True),
    ("ConnectionRefusedError: [Errno 111]", "connection_refused", True),
    # setup
    ("Failed to setup sender profile on relay.example: SomeError: details", "setup", False),
    # unknown -- real DNS string that does NOT match any keyword
    ("Something completely unexpected happened", "unknown", False),
    ("temporary failure in name resolution", "unknown", False),
])
def test_failure_taxonomy(error, category, is_fatal):
    """_classify_error maps errors to categories; fatal categories refuse fast-fail."""
    assert _classify_error(error) == category
    if error is None:
        return
    in_fatal = _classify_error(error) in _FATAL_CATEGORIES
    assert in_fatal is is_fatal


@pytest.mark.parametrize(("error", "is_crash"), [
    ("Failed to setup sender profile on dns.example: JsonRpcError: "
     "{'code': -1, 'message': 'Could not find DNS resolutions'}", False),
    ("AUTHENTICATIONFAILED: login failed", False),
    ("Connection timeout: deadline has elapsed", False),
    ("RPC server closed", True),
    ("rpc process crashed", True),
    ("BrokenPipeError writing to rpc stdin", True),
    ("ConnectionResetError: [Errno 104] Connection reset by peer", True),
    ("EOFError reading from rpc server", True),
])
def test_rpc_crash_classification(error, is_crash):
    """App-level errors must not match RPC crash keywords; transport errors must."""
    from chatmail_prober.orchestration import _RPC_CRASH_KEYWORDS
    matched = any(kw in error.lower() for kw in _RPC_CRASH_KEYWORDS)
    assert matched is is_crash


# -- Tests merged from test_ip_relay.py --


@pytest.mark.parametrize(("host", "expected"), [
    ("192.168.1.1", True),
    ("::1", True),
    ("2001:db8::1", True),
    ("relay.example", False),
    ("", False),
])
def test_is_ip_address(host, expected):
    assert is_ip_address(host) is expected


class TestCreateQrUrl:
    def test_domain_produces_dcaccount_url(self):
        assert create_qr_url("relay.example") == "dcaccount:relay.example"

    def test_ip_produces_dclogin_url(self):
        url = create_qr_url("192.168.1.1")
        assert url.startswith("dclogin:")
        assert "192.168.1.1" in url

    def test_dclogin_url_has_required_params(self):
        url = create_qr_url("192.168.1.1")
        qs = urllib.parse.parse_qs(url.split("?")[1]) if "?" in url else {}
        assert "p" in qs
        assert "ip" in qs
        assert "sp" in qs


#
# RPC-level setup error handling: inject real JsonRpcError instances using
# the exact message strings produced by chatmail/core's Rust network layer
# and verify run_probe surfaces them with the right relay_status_value code.
#
# Error string origins (chatmail/core):
#   DNS:     src/net/dns.rs   "Could not find DNS resolutions for {host}:{port}..."
#   Auth:    src/stock_str.rs + IMAP server "[AUTHENTICATIONFAILED]"
#   Timeout: src/net.rs       tokio::time::timeout -> "Connection timeout: deadline has elapsed"
#


def _dns_error(host: str = "imap.dns.example", port: int = 993) -> JsonRpcError:
    return JsonRpcError({
        "code": -1,
        "message": (
            f'Error:\n\n"IMAP failed to connect to {host}:{port}:tls: '
            f"Could not find DNS resolutions for {host}:{port}. "
            'Check server hostname and your network"'
        ),
    })


def _auth_error(addr: str = "user@auth.example") -> JsonRpcError:
    return JsonRpcError({
        "code": -1,
        "message": (
            f'Error:\n\n"Cannot login as "{addr}". '
            "Please check if the email address and the password are correct. "
            '(no response: code: None, info: Some("[AUTHENTICATIONFAILED] Authentication failed."))"'
        ),
    })


def _timeout_error(host: str = "timeout.example") -> JsonRpcError:
    return JsonRpcError({
        "code": -1,
        "message": (
            f'Error:\n\n"IMAP failed to connect to {host}:993:tls: '
            'Connection timeout: deadline has elapsed"'
        ),
    })


@pytest.fixture()
def mock_relay_contexts():
    """Factory: returns a contexts dict whose AccountMaker.dc.add_account()
    raises the supplied exception, simulating a failure during account setup
    (set_config_from_qr -> start_io -> configure)."""
    def _make(exc: Exception):
        class _FakeDC:
            def get_all_accounts(self):
                return []

            def add_account(self):
                raise exc

        maker = AccountMaker(_FakeDC())

        class _Ctx:
            def __init__(self):
                self.maker = maker

        return {"src.example": _Ctx(), "dst.example": _Ctx()}

    return _make


@pytest.mark.parametrize(("error_factory", "expected_status"), [
    (_dns_error, -6),
    (_auth_error, -3),
    (_timeout_error, -1),
])
def test_run_probe_classifies_rpc_setup_error(
    mock_relay_contexts, error_factory, expected_status,
):
    contexts = mock_relay_contexts(error_factory())
    result = run_probe(
        "src.example", "dst.example",
        count=1, relay_contexts=contexts,
    )
    assert relay_status_value(result.error) == expected_status, (
        f"Expected status {expected_status}, got "
        f"{relay_status_value(result.error)!r} for error: {result.error!r}"
    )


def test_self_loop_surfaces_rpc_error(mock_relay_contexts):
    """Self-loop probes (src==dst) must also surface errors correctly."""
    contexts = mock_relay_contexts(_dns_error("imap.self.example"))
    contexts["self.example"] = contexts.pop("src.example")
    contexts.pop("dst.example", None)
    result = run_probe(
        "self.example", "self.example",
        count=1, relay_contexts=contexts,
    )
    assert result.error is not None
    assert relay_status_value(result.error) == -6
