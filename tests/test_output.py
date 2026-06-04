"""Tests for textfile output (atomic write)."""

import pytest

from prometheus_client.parser import text_string_to_metric_families

from chatmail_prober.metrics import (
    account_setup_seconds,
    probe_loss_ratio,
    probe_success,
    rtt_median,
    rtt_p10,
    rtt_p90,
    rtt_stddev,
    send_errors_total,
    update_metrics,
)
from chatmail_prober.output import write_textfile
from chatmail_prober.probe import ProbeResult


@pytest.fixture(autouse=True)
def _clear_output_metrics():
    for m in [rtt_median, rtt_stddev, rtt_p90, rtt_p10,
              probe_success, probe_loss_ratio, account_setup_seconds, send_errors_total]:
        m._metrics.clear()
    yield


class TestWriteTextfile:
    def test_writes_valid_prom_file(self, tmp_path):
        # Feed some data into metrics first
        result = ProbeResult("a.example", "b.example", sent=3, received=3, loss=0.0,
                             rtts_ms=[100.0, 200.0, 300.0])
        update_metrics(result)

        out = tmp_path / "test.prom"
        write_textfile(str(out))

        content = out.read_text()
        assert "cmping_rtt_median_seconds" in content
        assert "cmping_probe_success" in content
        assert content.endswith("\n")

    def test_atomic_write_no_partial_file(self, tmp_path):
        out = tmp_path / "test.prom"
        write_textfile(str(out))

        # No leftover .tmp files
        tmp_files = list(tmp_path.glob("*.tmp"))
        assert tmp_files == []

    def test_overwrites_existing_file(self, tmp_path):
        out = tmp_path / "test.prom"
        out.write_text("old content")

        write_textfile(str(out))
        content = out.read_text()
        assert "old content" not in content
        assert "# HELP" in content

    def test_no_process_metrics_in_textfile(self, tmp_path):
        """Textfile must not include process_* metrics -- they collide
        with node_exporter's own process collectors."""
        out = tmp_path / "test.prom"
        write_textfile(str(out))
        content = out.read_text()
        assert "process_" not in content

    def test_textfile_is_parseable_by_prometheus_client(self, tmp_path):
        """End-to-end: feed several probes and parse the output back through
        prometheus_client's own text parser. This catches malformed labels,
        bad escaping, missing # HELP/TYPE lines, NaN encoding, and any other
        violation of the Prometheus exposition format that would make
        node_exporter silently drop the file.
        """
        results = [
            ProbeResult("a.example", "b.example", sent=3, received=3, loss=0.0,
                        rtts_ms=[100.0, 200.0, 300.0],
                        account_setup_time=1.5, message_time=0.8),
            ProbeResult("a.example", "a.example", sent=3, received=2, loss=33.3,
                        rtts_ms=[110.0, 220.0]),
            ProbeResult("b.example", "a.example", error="Connection timeout"),
        ]
        for r in results:
            update_metrics(r)

        out = tmp_path / "test.prom"
        write_textfile(str(out))
        content = out.read_text()

        families = list(text_string_to_metric_families(content))
        assert families, "parser returned no metric families"

        names = {f.name for f in families}
        # At minimum: gauge for rtt, counter family for probe_success.
        assert any(n.startswith("cmping_rtt") for n in names)
        assert "cmping_probe_success" in names

        # Every label set must round-trip cleanly (catches escaping bugs in
        # source/destination strings; NaN values are intentional for failed
        # probes and are valid in the exposition format).
        for fam in families:
            for sample in fam.samples:
                for k, v in sample.labels.items():
                    assert "\n" not in v and '"' not in v, (
                        f"unescaped char in {sample.name}{{{k}={v!r}}}"
                    )
