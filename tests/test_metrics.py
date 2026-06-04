"""Tests for Prometheus metric updates from ProbeResults."""

import socket
from unittest.mock import patch

import pytest

from chatmail_prober import metrics as metrics_mod
from chatmail_prober.probe import ProbeResult


@pytest.fixture(autouse=True)
def _auto_fresh_metrics(fresh_metrics):
    """Auto-apply the shared fresh_metrics fixture to every test in this file."""
    return fresh_metrics


def _labels():
    return dict(source="a.example", destination="b.example", probe_type="cross")


class TestUpdateMetricsSuccess:
    def test_rtt_gauges_computed_from_rtts_ms(self):
        result = ProbeResult("a.example", "b.example", sent=3, received=3, loss=0.0,
                             rtts_ms=[500.0, 1500.0, 250.0])
        metrics_mod.update_metrics(result)

        lbl = _labels()
        assert metrics_mod.rtt_median.labels(**lbl)._value.get() == pytest.approx(0.5)
        # statistics.quantiles(method="inclusive") interpolates within data range
        assert metrics_mod.rtt_p90.labels(**lbl)._value.get() == pytest.approx(1.3)
        assert metrics_mod.rtt_p10.labels(**lbl)._value.get() == pytest.approx(0.3)
        assert metrics_mod.rtt_stddev.labels(**lbl)._value.get() == pytest.approx(0.6614, abs=0.001)

    def test_probe_success_set_to_one_on_zero_loss(self):
        result = ProbeResult("a.example", "b.example", sent=3, received=3, loss=0.0)
        metrics_mod.update_metrics(result)
        assert metrics_mod.probe_success.labels(**_labels())._value.get() == 1.0
        assert metrics_mod.probe_loss_ratio.labels(**_labels())._value.get() == pytest.approx(0.0)

    def test_probe_success_set_to_zero_on_partial_loss(self):
        result = ProbeResult("a.example", "b.example", sent=3, received=2, loss=33.3)
        metrics_mod.update_metrics(result)
        assert metrics_mod.probe_success.labels(**_labels())._value.get() == 0.0
        assert metrics_mod.probe_loss_ratio.labels(**_labels())._value.get() == pytest.approx(1/3)

    def test_account_setup_time_stored(self):
        result = ProbeResult("a.example", "b.example", sent=1, received=1, loss=0.0,
                             account_setup_time=1.234)
        metrics_mod.update_metrics(result)
        assert metrics_mod.account_setup_seconds.labels(**_labels())._value.get() == pytest.approx(1.234)

    def test_rtt_gauges_reflect_last_round(self):
        """Gauges should show the last round's values, not accumulate."""
        lbl = _labels()

        result1 = ProbeResult("a.example", "b.example", sent=2, received=2, loss=0.0,
                              rtts_ms=[1000.0, 2000.0])
        metrics_mod.update_metrics(result1)
        assert metrics_mod.rtt_median.labels(**lbl)._value.get() == pytest.approx(1.5)

        result2 = ProbeResult("a.example", "b.example", sent=2, received=2, loss=0.0,
                              rtts_ms=[100.0, 200.0])
        metrics_mod.update_metrics(result2)
        assert metrics_mod.rtt_median.labels(**lbl)._value.get() == pytest.approx(0.15)

    def test_rtt_stddev_zero_for_single_ping(self):
        result = ProbeResult("a.example", "b.example", sent=1, received=1, loss=0.0,
                             rtts_ms=[500.0])
        metrics_mod.update_metrics(result)
        assert metrics_mod.rtt_stddev.labels(**_labels())._value.get() == 0.0

    def test_zero_sent_no_error_records_failure(self):
        result = ProbeResult("a.example", "b.example", sent=0, received=0, loss=0.0)
        metrics_mod.update_metrics(result)
        assert metrics_mod.probe_success.labels(**_labels())._value.get() == 0.0
        assert metrics_mod.probe_loss_ratio.labels(**_labels())._value.get() == 1.0

    def test_empty_rtts_no_error_leaves_rtt_gauges_unchanged(self):
        """RTT gauges are not updated when rtts_ms is empty; old values survive."""
        lbl = _labels()
        first = ProbeResult("a.example", "b.example", sent=3, received=3, loss=0.0,
                            rtts_ms=[100.0, 200.0, 300.0])
        metrics_mod.update_metrics(first)
        prior = metrics_mod.rtt_median.labels(**lbl)._value.get()

        second = ProbeResult("a.example", "b.example", sent=1, received=0, loss=100.0)
        metrics_mod.update_metrics(second)
        assert metrics_mod.rtt_median.labels(**lbl)._value.get() == prior


class TestUpdateMetricsError:
    def test_error_increments_send_errors(self):
        result = ProbeResult("a.example", "b.example", error="connection refused")
        metrics_mod.update_metrics(result)

        lbl = _labels()
        assert metrics_mod.send_errors_total.labels(**lbl)._value.get() == 1

    def test_error_sets_probe_success_to_zero(self):
        result = ProbeResult("a.example", "b.example", error="timeout")
        metrics_mod.update_metrics(result)
        assert metrics_mod.probe_success.labels(**_labels())._value.get() == 0.0
        assert metrics_mod.probe_loss_ratio.labels(**_labels())._value.get() == 1.0

    def test_error_clears_rtt_gauges_to_nan(self):
        """RTT gauges must be NaN on error so dashboards don't show stale values."""
        import math
        result = ProbeResult("a.example", "b.example", error="boom")
        metrics_mod.update_metrics(result)

        lbl = _labels()
        assert math.isnan(metrics_mod.rtt_median.labels(**lbl)._value.get())
        assert math.isnan(metrics_mod.rtt_p90.labels(**lbl)._value.get())
        assert math.isnan(metrics_mod.rtt_p10.labels(**lbl)._value.get())
        assert math.isnan(metrics_mod.rtt_stddev.labels(**lbl)._value.get())
        assert math.isnan(metrics_mod.account_setup_seconds.labels(**lbl)._value.get())


@pytest.mark.parametrize(("src", "dst", "expected_type"), [
    ("a.example", "a.example", "self"),
    ("a.example", "b.example", "cross"),
])
def test_probe_type_label(src, dst, expected_type):
    result = ProbeResult(src, dst, sent=1, received=1, loss=0.0, rtts_ms=[100.0])
    metrics_mod.update_metrics(result)
    lbl = dict(source=src, destination=dst, probe_type=expected_type)
    assert metrics_mod.probe_success.labels(**lbl)._value.get() == 1.0


class TestUpdateMetricsMultiplePairs:
    def test_different_pairs_are_independent(self):
        r1 = ProbeResult("a.example", "b.example", sent=5, received=5, loss=0.0)
        r2 = ProbeResult("b.example", "a.example", sent=5, received=3, loss=40.0)
        metrics_mod.update_metrics(r1)
        metrics_mod.update_metrics(r2)

        assert metrics_mod.probe_success.labels(
            source="a.example", destination="b.example", probe_type="cross")._value.get() == 1.0
        assert metrics_mod.probe_success.labels(
            source="b.example", destination="a.example", probe_type="cross")._value.get() == 0.0


class TestClearStaleLabels:
    def test_removes_stale_and_keeps_active_across_all_metrics(self):
        """clear_stale_labels removes gone-relay labels from every probe metric
        and leaves active-relay labels intact."""
        # Success probes populate rtt/probe_success/probe_loss_ratio/account_setup_seconds
        metrics_mod.update_metrics(
            ProbeResult("a.example", "a.example", sent=1, received=1, loss=0.0, rtts_ms=[50.0])
        )
        # Error probes populate send_errors_total too; use a self-loop to keep it active
        metrics_mod.update_metrics(ProbeResult("a.example", "a.example", error="self-fail"))
        # Stale pair: gone.example is not in the active list
        metrics_mod.update_metrics(ProbeResult("a.example", "gone.example", error="dead"))

        metrics_mod.clear_stale_labels(["a.example"])

        stale = ("a.example", "gone.example", "cross")
        active = ("a.example", "a.example", "self")

        all_probe_metrics = [
            metrics_mod.rtt_median, metrics_mod.rtt_stddev,
            metrics_mod.rtt_p90, metrics_mod.rtt_p10,
            metrics_mod.probe_success, metrics_mod.probe_loss_ratio,
            metrics_mod.account_setup_seconds, metrics_mod.send_errors_total,
        ]
        for m in all_probe_metrics:
            assert stale not in m._metrics, f"{m._name} still has stale label {stale}"
            assert active in m._metrics, (
                f"{m._name} lost active label set after clear_stale_labels"
            )


class TestClearStaleRelayLabels:
    def test_removes_labels_for_unconfigured_relay(self):
        metrics_mod.relay_status.labels(relay="a.example").set(1)
        metrics_mod.relay_status.labels(relay="b.example").set(1)

        metrics_mod.clear_stale_relay_labels(["a.example"])

        assert ("a.example",) in metrics_mod.relay_status._metrics
        assert ("b.example",) not in metrics_mod.relay_status._metrics

    def test_keeps_all_configured_relays(self):
        relays = ["a.example", "b.example", "c.example"]
        for r in relays:
            metrics_mod.relay_status.labels(relay=r).set(1)

        metrics_mod.clear_stale_relay_labels(relays)

        for r in relays:
            assert (r,) in metrics_mod.relay_status._metrics

    def test_noop_when_no_labels_exist(self):
        # Should not raise when metric has no label sets yet
        metrics_mod.clear_stale_relay_labels(["a.example"])


class TestRelayStatusMetric:
    def test_relay_status_value_encoding(self):
        # Test integer encoding for different failure modes
        assert metrics_mod.relay_status_value(None) == 1  # ok
        assert metrics_mod.relay_status_value("timeout") == -1
        assert metrics_mod.relay_status_value("connection refused") == -5
        assert metrics_mod.relay_status_value("name or service not known") == -6  # DNS fail
        assert metrics_mod.relay_status_value("unknown error") == 0
        assert metrics_mod.relay_status_value("Failed to setup sender profile: SomeError") == -2


class TestVerifyRelayStatus:
    @patch("chatmail_prober.metrics.socket.getaddrinfo")
    def test_dns_error_reclassified_when_base_resolves(self, mock_gai):
        mock_gai.return_value = [(2, 1, 6, "", ("1.2.3.4", 993))]
        result = metrics_mod.verify_relay_status(
            "chat.example",
            "Could not find DNS resolutions for imap.chat.example:993",
        )
        assert result == -1

    @patch("chatmail_prober.metrics.socket.getaddrinfo")
    def test_dns_error_stays_when_base_fails(self, mock_gai):
        mock_gai.side_effect = socket.gaierror("Name or service not known")
        result = metrics_mod.verify_relay_status(
            "dead.example", "Name or service not known"
        )
        assert result == -6

    def test_non_dns_error_unchanged(self):
        assert metrics_mod.verify_relay_status("a.example", "connection refused") == -5
        assert metrics_mod.verify_relay_status("a.example", "timeout") == -1

    def test_none_error_returns_ok(self):
        assert metrics_mod.verify_relay_status("a.example", None) == 1

    def test_none_relay_with_dns_error(self):
        assert metrics_mod.verify_relay_status(
            None, "Name or service not known"
        ) == -6

    @patch("chatmail_prober.metrics.socket.getaddrinfo")
    def test_logs_missing_subdomains(self, mock_gai):
        def side_effect(host, port):
            if host == "chat.example":
                return [(2, 1, 6, "", ("1.2.3.4", 993))]
            if host == "imap.chat.example":
                raise socket.gaierror("NXDOMAIN")
            if host == "smtp.chat.example":
                return [(2, 1, 6, "", ("1.2.3.5", 0))]
            raise socket.gaierror("unexpected")
        mock_gai.side_effect = side_effect
        assert metrics_mod.verify_relay_status(
            "chat.example", "Could not find DNS resolutions"
        ) == -1

    @patch("chatmail_prober.metrics.socket.getaddrinfo")
    def test_both_subdomains_missing(self, mock_gai):
        def side_effect(host, port):
            if host == "chat.example":
                return [(2, 1, 6, "", ("1.2.3.4", 993))]
            raise socket.gaierror("NXDOMAIN")
        mock_gai.side_effect = side_effect
        assert metrics_mod.verify_relay_status(
            "chat.example", "Could not find DNS resolutions"
        ) == -1

    @patch("chatmail_prober.metrics.socket.getaddrinfo")
    def test_all_subdomains_resolve(self, mock_gai):
        mock_gai.return_value = [(2, 1, 6, "", ("1.2.3.4", 993))]
        assert metrics_mod.verify_relay_status(
            "chat.example", "Could not find DNS resolutions"
        ) == -1


class TestIsTransientAliveError:
    def test_timeout_is_transient(self):
        assert metrics_mod.is_transient_alive_error("a.example", "timeout") is True

    def test_unknown_is_transient(self):
        assert metrics_mod.is_transient_alive_error("a.example", "weird error") is True

    def test_connection_refused_not_transient(self):
        assert metrics_mod.is_transient_alive_error("a.example", "connection refused") is False

    def test_auth_not_transient(self):
        assert metrics_mod.is_transient_alive_error("a.example", "AUTHENTICATIONFAILED") is False

    @patch("chatmail_prober.metrics.socket.getaddrinfo")
    def test_genuine_dns_not_transient(self, mock_gai):
        mock_gai.side_effect = socket.gaierror("NXDOMAIN")
        assert metrics_mod.is_transient_alive_error(
            "dead.example", "Name or service not known"
        ) is False

    @patch("chatmail_prober.metrics.socket.getaddrinfo")
    def test_reclassified_dns_is_transient(self, mock_gai):
        mock_gai.return_value = [(2, 1, 6, "", ("1.2.3.4", 993))]
        assert metrics_mod.is_transient_alive_error(
            "alive.example", "Could not find DNS resolutions"
        ) is True

    def test_none_error_not_transient(self):
        assert metrics_mod.is_transient_alive_error("a.example", None) is False

    def test_tls_not_transient(self):
        assert metrics_mod.is_transient_alive_error(
            "a.example", "certificate has expired"
        ) is False

    def test_setup_error_not_transient(self):
        assert metrics_mod.is_transient_alive_error(
            "a.example", "Failed to setup profile: SomeError"
        ) is False


