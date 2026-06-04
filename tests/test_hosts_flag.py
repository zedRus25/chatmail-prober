"""Tests for the --hosts / -H flag, IPv6 auto-bracketing, and short flag aliases."""
from __future__ import annotations

import os
import tempfile

import pytest

from chatmail_prober.__main__ import _bracket_ipv6, parse_args

#
# --hosts / -H
#

@pytest.fixture()
def relay_file(tmp_path):
    f = tmp_path / "r.txt"
    f.write_text("relay.example\n")
    return str(f)


class TestHostsFlag:
    @pytest.mark.parametrize("flag", ["--hosts", "-H"])
    @pytest.mark.parametrize("value", ["a.example", "a.example,b.example",
                                       " a.example , b.example "])
    def test_hosts_flag_stores_raw_value(self, relay_file, flag, value):
        args = parse_args([relay_file, flag, value])
        # Whitespace trimming happens in main(), not parse_args; the raw
        # string is preserved on args.
        assert args.hosts == value

    def test_defaults_to_none(self, relay_file):
        args = parse_args([relay_file])
        assert args.hosts is None


#
# IPv6 auto-bracketing
#

@pytest.mark.parametrize(("host", "expected"), [
    ("::1", "[::1]"),
    ("2001:db8::1", "[2001:db8::1]"),
    ("[::1]", "[::1]"),
    ("192.168.1.1", "192.168.1.1"),
    ("relay.example", "relay.example"),
])
def test_bracket_ipv6(host, expected):
    assert _bracket_ipv6(host) == expected


#
# Short flag aliases. One round-trip test covers all of them.
#

class TestShortAliases:
    """All short flags must wire to the correct long-form attribute."""

    def _args(self, *flags):
        with tempfile.NamedTemporaryFile(suffix=".txt", mode="w", delete=False) as f:
            f.write("relay.example\n")
            name = f.name
        try:
            return parse_args([name] + list(flags))
        finally:
            os.unlink(name)

    def test_all_short_aliases(self):
        args = self._args("-1", "-p", "-m", "-n", "3", "-t", "30", "-w", "8", "-i", "60")
        assert args.once is True
        assert args.print_summary is True
        assert args.print_metrics is True
        assert args.count == 3
        assert args.timeout == 30
        assert args.workers == 8
        assert args.interval == 60
