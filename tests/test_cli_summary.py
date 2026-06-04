"""Tests for the cli_summary table renderer.

cli_summary.render(results, alive_relays, dead_relays, elapsed_s) must
produce a compact table where each probe pair occupies one row:

  Route                                    Sent  Recv  Loss    p50    p90    p99   mdev      Setup       Msg
  nine.testrun.org -> nine.testrun.org        3     3   0.0%  2271   2316   2326    431  4850.00ms  3240.00ms
  ...

Failed rows show the failure category in the p50 column and dashes for
RTT/timing columns.  A grouped failure block and a one-line summary
footer follow the table.
"""
from __future__ import annotations

import io

from chatmail_prober.probe import ProbeResult

#
# Helpers
#

def _ok(src: str = "a.example", dst: str = "b.example",
        rtts: list[float] | None = None,
        setup: float = 3.5, msg: float = 2.1) -> ProbeResult:
    return ProbeResult(
        source=src, destination=dst,
        sent=3, received=3, loss=0.0,
        rtts_ms=rtts or [1000.0, 1200.0, 900.0],
        account_setup_time=setup,
        message_time=msg,
    )


def _fail(src: str = "a.example", dst: str = "b.example",
          error: str = "Connection timeout: deadline has elapsed") -> ProbeResult:
    return ProbeResult(source=src, destination=dst, error=error)


def _render(*args, **kwargs) -> str:
    from chatmail_prober.cli_summary import render
    buf = io.StringIO()
    render(*args, out=buf, **kwargs)
    return buf.getvalue()


#
# Table header
#

import pytest


@pytest.mark.parametrize("header", [
    "Route", "Sent", "Recv", "Loss",
    "p50", "p90", "p99", "mdev",
    "Setup", "Msg",
])
def test_table_header_present(header):
    out = _render([_ok()], ["a.example", "b.example"], [], elapsed_s=5.0)
    assert header in out


#
# Table rows for successful probes.
#

class TestSuccessRows:
    def test_route_present(self):
        out = _render([_ok("nine.testrun.org", "mailchat.pl")],
                      ["nine.testrun.org", "mailchat.pl"], [], elapsed_s=5.0)
        assert "nine.testrun.org" in out
        assert "mailchat.pl" in out

    def test_zero_loss(self):
        out = _render([_ok()], ["a.example", "b.example"], [], elapsed_s=5.0)
        assert "0.0%" in out

    def test_partial_loss(self):
        r = ProbeResult(source="a.example", destination="b.example",
                        sent=5, received=3, loss=40.0,
                        rtts_ms=[1000.0, 1100.0, 1200.0])
        out = _render([r], ["a.example", "b.example"], [], elapsed_s=5.0)
        assert "40.0%" in out

    def test_rtt_p50_value(self):
        # p50 of [500, 600, 700] = 600
        out = _render([_ok(rtts=[500.0, 600.0, 700.0])],
                      ["a.example", "b.example"], [], elapsed_s=5.0)
        assert "600ms" in out

    def test_setup_time_in_ms(self):
        # 6.66 s -> 6660.00ms
        out = _render([_ok(setup=6.66)], ["a.example", "b.example"], [], elapsed_s=5.0)
        assert "6660.00ms" in out

    def test_msg_time_in_ms(self):
        # 9.47 s -> 9470.00ms
        out = _render([_ok(msg=9.47)], ["a.example", "b.example"], [], elapsed_s=5.0)
        assert "9470.00ms" in out

    def test_multiple_rows_all_present(self):
        results = [_ok("a.example", "b.example"), _ok("b.example", "a.example")]
        out = _render(results, ["a.example", "b.example"], [], elapsed_s=5.0)
        assert out.count("->") >= 2


#
# Table rows for failed probes.
#

class TestFailedRows:
    def test_failed_row_shows_category(self):
        out = _render([_fail()], [], {"a.example": "timeout"}, elapsed_s=5.0)
        assert "timeout" in out.lower()

    def test_failed_row_shows_route(self):
        out = _render([_fail("x.example", "y.example")], [], {}, elapsed_s=5.0)
        assert "x.example" in out
        assert "y.example" in out

    def test_failed_row_shows_dashes_for_rtt(self):
        out = _render([_fail()], [], {}, elapsed_s=5.0)
        assert "-" in out

    def test_dns_failure_category(self):
        out = _render(
            [_fail(error="Name or service not known: imap.a.example")],
            [], {"a.example": "Name or service not known"}, elapsed_s=5.0,
        )
        assert "dns" in out.lower()

    def test_mixed_ok_and_failed(self):
        out = _render(
            [_ok("a.example", "b.example"), _fail("b.example", "a.example")],
            ["a.example", "b.example"], {}, elapsed_s=5.0,
        )
        assert "0.0%" in out          # ok row
        assert "timeout" in out.lower()  # failed row


#
# Failure block
#

class TestFailureBlock:
    def test_failure_block_present_when_failures_exist(self):
        out = _render([_fail()], [], {"a.example": "timeout"}, elapsed_s=5.0)
        assert "failure" in out.lower()


#
# Dead-relay failures table
#

class TestDeadRelayTable:
    """When dead_relays is a dict[str, str|None], a Host/Error/Message table
    is rendered below the probe table."""

    def test_dead_relay_table(self):
        dead = {"owo.void.my": "AUTHENTICATIONFAILED: login failed"}
        out = _render([_ok()], ["a.example"], dead, elapsed_s=5.0)
        assert "Host" in out and "Error" in out
        assert "owo.void.my" in out
        assert "AUTHENTICATIONFAILED" in out

    def test_no_dead_table_when_all_alive(self):
        out = _render([_ok()], ["a.example", "b.example"], {}, elapsed_s=5.0)
        assert "Host" not in out

    def test_multiple_dead_relays_shown(self):
        dead = {
            "dead1.example": "timeout",
            "dead2.example": "AUTHENTICATIONFAILED",
        }
        out = _render([_ok()], ["a.example"], dead, elapsed_s=5.0)
        assert "dead1.example" in out
        assert "dead2.example" in out

    def test_dead_relay_with_none_error(self):
        """A dead relay with no error string should render gracefully."""
        dead = {"silent.example": None}
        out = _render([_ok()], ["a.example"], dead, elapsed_s=5.0)
        assert "silent.example" in out


#
# Summary footer
#

class TestSummaryFooter:
    def test_elapsed_shown(self):
        out = _render([_ok()], ["a.example", "b.example"], {}, elapsed_s=42.3)
        assert "42.3s" in out

    def test_probes_ok_fraction(self):
        results = [_ok(), _ok("b.example", "a.example")]
        out = _render(results, ["a.example", "b.example"], {}, elapsed_s=5.0)
        assert "2/2" in out

    def test_alive_fraction_format(self):
        """Footer shows Alive: 1/2 when one of two relays is dead."""
        dead = {"c.example": "timeout"}
        out = _render(
            [_ok(), _fail("c.example", "c.example")],
            ["a.example", "b.example"], dead,
            elapsed_s=5.0,
        )
        # total = alive + dead = 2 + 1 = 3
        assert "2/3" in out

    def test_footer_contains_elapsed_on_last_line(self):
        out = _render([_ok()], ["a.example", "b.example"], {}, elapsed_s=5.0)
        last_line = out.rstrip("\n").split("\n")[-1]
        assert "5.0s" in last_line
