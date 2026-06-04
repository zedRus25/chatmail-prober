"""Tests for chatmail_prober.turn and chatmail_prober.turn_parse."""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock

import pytest

from chatmail_prober import turn, turn_parse
from chatmail_prober.turn import (
    FALLBACK_TURN,
    _parse_turn_url,
    check_turn,
    resolve_relay_turn,
)
from chatmail_prober.turn_parse import (
    UCLIENT_FLAGS,
    build_uclient_argv,
    parse_uclient_output,
    run_uclient,
)

# Verbatim shape of real turnutils_uclient output: every line is
# prefixed with `N: : ` (uclient log timestamp + thread tag, doubled
# because -y runs two endpoints in the same process).  The regexes
# in turn_parse.py are unanchored so they match against this prefix.
# Numbers tweaked so each parsed field is uniquely identifiable.
SAMPLE_OUTPUT = """\
0: : Total connect time is 1
0: : Total connect time is 1
5: : Total transmit time is 5
5: : Total transmit time is 5
5: : Total lost packets 0 (0.000000%), total send dropped 2 (0.000000%)
5: : Total lost packets 0 (0.000000%), total send dropped 2 (0.000000%)
5: : Average round trip delay 12.500000 ms; min = 8 ms, max = 17 ms
5: : Average round trip delay 12.500000 ms; min = 8 ms, max = 17 ms
5: : Average jitter 1.250000 ms; min = 0 ms, max = 3 ms
5: : Average jitter 1.250000 ms; min = 0 ms, max = 3 ms
"""


def test_build_uclient_argv_has_flags_and_creds():
    argv = build_uclient_argv("turn.example.com", 3478, "user", "pw")
    assert argv[0] == "turnutils_uclient"
    assert argv[1] == "turn.example.com"
    for flag in UCLIENT_FLAGS:
        assert flag in argv
    assert "-u" in argv and "user" in argv
    assert "-w" in argv and "pw" in argv
    # Port goes via -p so uclient hits non-default ports correctly.
    p_idx = argv.index("-p")
    assert argv[p_idx + 1] == "3478"
    # -e <host> means the peer is the same server (loopback test).
    e_idx = argv.index("-e")
    assert argv[e_idx + 1] == "turn.example.com"


def test_parse_turn_url_ipv4():
    assert _parse_turn_url("turn:1.2.3.4:3478") == ("1.2.3.4", 3478)


def test_parse_turn_url_ipv6():
    assert _parse_turn_url("turn:[2a01:4f9:fff1:59::1]:3478") == (
        "2a01:4f9:fff1:59::1", 3478,
    )


def test_parse_turn_url_hostname():
    assert _parse_turn_url("turn:turn.delta.chat:3478") == ("turn.delta.chat", 3478)


def test_parse_turn_url_rejects_stun_and_garbage():
    assert _parse_turn_url("stun:1.2.3.4:3478") is None
    assert _parse_turn_url("turn:no-port") is None
    assert _parse_turn_url("turn:[bad-ipv6:3478") is None


def test_resolve_prefers_ipv4_over_ipv6():
    """When ice_servers() returns both IPv4 and IPv6 URLs we must pick the
    IPv4 entry, because uclient runs with -X (force IPv4 relay)."""
    acct = _mock_account([
        {
            "urls": [
                "turn:[2a01:4f9:fff1:59::1]:3478",  # IPv6 first
                "turn:77.42.49.41:3478",
            ],
            "username": "1778911316",
            "credential": "abc=",
        },
    ])
    result = resolve_relay_turn(acct, "relay.example")
    assert result is not None
    host, port, _, _, kind = result
    assert host == "77.42.49.41"
    assert port == 3478
    assert kind == "self"


def test_parse_uclient_output_normalizes_ms_to_seconds():
    run = parse_uclient_output(SAMPLE_OUTPUT, "", 0)
    assert run.ok is True
    assert run.returncode == 0
    assert run.connect_s == pytest.approx(0.001)
    assert run.transmit_s == pytest.approx(0.005)


def test_parse_uclient_output_handles_missing_fields():
    run = parse_uclient_output("nothing matches", "", 1)
    assert run.ok is False
    assert run.connect_s is None
    assert run.transmit_s is None


def test_run_uclient_binary_missing(monkeypatch):
    def fake(*args, **kwargs):
        raise FileNotFoundError
    monkeypatch.setattr(subprocess, "run", fake)
    run = run_uclient("h", 3478, "u", "p")
    assert run.ok is False
    assert run.error == "binary-missing"


def test_run_uclient_timeout(monkeypatch):
    def fake(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="x", timeout=1)
    monkeypatch.setattr(subprocess, "run", fake)
    run = run_uclient("h", 3478, "u", "p", timeout=0.01)
    assert run.ok is False
    assert run.error == "timeout"


def test_run_uclient_dispatches_parser(monkeypatch):
    proc = MagicMock(stdout=SAMPLE_OUTPUT, stderr="", returncode=0)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: proc)
    run = run_uclient("h", 3478, "u", "p")
    assert run.ok is True
    assert run.connect_s == pytest.approx(0.001)


#
# resolve_relay_turn
#

def _mock_account(servers):
    acct = MagicMock()
    acct.ice_servers.return_value = servers
    return acct


def test_resolve_self_published():
    acct = _mock_account([
        {"urls": ["turn:10.0.0.1:3478"], "username": "1758650868", "credential": "abc"},
    ])
    result = resolve_relay_turn(acct, "relay.example.com")
    assert result is not None
    host, port, user, cred, kind = result
    assert host == "10.0.0.1"
    assert user == "1758650868"
    assert cred == "abc"
    assert kind == "self"


def test_resolve_fallback_only():
    acct = _mock_account([
        {"urls": ["stun:9.9.9.9"], "username": None, "credential": None},
        {"urls": ["turn:1.2.3.4:3478"], "username": "public",
         "credential": "o4tR7yG4rG2slhXqRUf9zgmHz"},
    ])
    result = resolve_relay_turn(acct, "no-turn-relay.example.com")
    assert result is not None
    host, port, user, cred, kind = result
    assert host == "1.2.3.4"
    assert user == "public"
    assert kind == "fallback"


def test_resolve_self_preferred_over_fallback():
    acct = _mock_account([
        {"urls": ["turn:1.2.3.4:3478"], "username": "public", "credential": "x"},
        {"urls": ["turn:5.6.7.8:3478"], "username": "1700000000", "credential": "y"},
    ])
    result = resolve_relay_turn(acct, "relay.example.com")
    assert result is not None
    assert result[4] == "self"
    assert result[0] == "5.6.7.8"


def test_resolve_empty_returns_none():
    acct = _mock_account([])
    assert resolve_relay_turn(acct, "x") is None


def test_resolve_malformed_returns_none():
    acct = _mock_account("not a list")
    assert resolve_relay_turn(acct, "x") is None


def test_resolve_raises_returns_none():
    acct = MagicMock()
    acct.ice_servers.side_effect = RuntimeError("boom")
    assert resolve_relay_turn(acct, "x") is None


def test_resolve_skips_entries_without_credentials():
    acct = _mock_account([
        {"urls": ["turn:1.2.3.4:3478"], "username": "1", "credential": None},
    ])
    assert resolve_relay_turn(acct, "x") is None


#
# check_turn -- status codes
#

def _resolved(kind="self"):
    return ("h", 3478, "u", "p", kind)


def test_check_turn_endpoint_kind(monkeypatch):
    """check_turn propagates endpoint_kind from the resolved tuple."""
    monkeypatch.setattr(turn, "run_uclient",
                        lambda *a, **k: turn_parse.TurnRun(ok=True, returncode=0))
    assert check_turn(_resolved("self")).endpoint_kind == "self"


@pytest.mark.parametrize(("run_kwargs", "expected"), [
    ({"ok": True,  "returncode": 0},                             turn.TurnStatus.OK),
    ({"ok": False, "returncode": 1},                             turn.TurnStatus.DOWN),
    ({"ok": False, "returncode": -1, "error": "binary-missing"}, turn.TurnStatus.BINARY_MISSING),
    ({"ok": False, "returncode": -1, "error": "timeout"},        turn.TurnStatus.TIMEOUT),
])
def test_check_turn_status(monkeypatch, run_kwargs, expected):
    monkeypatch.setattr(turn, "run_uclient",
                        lambda *a, **k: turn_parse.TurnRun(**run_kwargs))
    assert check_turn(_resolved()).status_code == expected


def test_fallback_constant_matches_core():
    # Verbatim from mnt/core/src/calls.rs:754-758.
    assert FALLBACK_TURN == (
        "turn.delta.chat", 3478, "public", "o4tR7yG4rG2slhXqRUf9zgmHz",
    )
