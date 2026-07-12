"""Prometheus metric definitions and update logic."""

from __future__ import annotations

import logging
import socket
import statistics
import subprocess
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .iroh import IrohResult  # noqa: F401
    from .probe import ProbeResult
    from .turn import TurnResult  # noqa: F401

from .iroh import IrohStatus

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    disable_created_metrics,
)

from .accounts import is_ip_address
from .probe import _classify_error

# Suppress the _created timestamp lines added by prometheus_client for each
# counter and histogram series -- they double the textfile size and are not
# useful for node_exporter's textfile collector.
# prometheus-client ships py.typed but disable_created_metrics() itself is
# unannotated upstream.  Single inline ignore is cleaner than scattering
# Any-casts; remove if/when prometheus-client tightens its annotations.
disable_created_metrics()  # type: ignore[no-untyped-call]

log = logging.getLogger(__name__)

LABELS = ["source", "destination", "probe_type"]

# Separate registry without default process_* / gc_* collectors.
# Used for textfile output to avoid collisions with node_exporter's
# own process metrics. The default REGISTRY (with process collectors)
# is still used for the HTTP /metrics endpoint.
CMPING_REGISTRY = CollectorRegistry()

rtt_median = Gauge(
    "cmping_rtt_median_seconds",
    "Median round-trip time for the last probe round",
    LABELS,
    registry=CMPING_REGISTRY,
)

rtt_stddev = Gauge(
    "cmping_rtt_stddev_seconds",
    "Standard deviation of round-trip times for the last probe round",
    LABELS,
    registry=CMPING_REGISTRY,
)

rtt_p90 = Gauge(
    "cmping_rtt_p90_seconds",
    "90th-percentile round-trip time for the last probe round",
    LABELS,
    registry=CMPING_REGISTRY,
)

rtt_p10 = Gauge(
    "cmping_rtt_p10_seconds",
    "10th-percentile round-trip time for the last probe round",
    LABELS,
    registry=CMPING_REGISTRY,
)

send_errors_total = Counter(
    "cmping_send_errors_total",
    "Total number of failed probe rounds (timeout, crash, setup failure)",
    LABELS,
    registry=CMPING_REGISTRY,
)

probe_success = Gauge(
    "cmping_probe_success",
    "Whether the last probe round succeeded (1) or failed (0)",
    LABELS,
    registry=CMPING_REGISTRY,
)

probe_loss_ratio = Gauge(
    "cmping_probe_loss_ratio",
    "Fraction of pings lost in the last probe round (0.0 = no loss, 1.0 = all lost)",
    LABELS,
    registry=CMPING_REGISTRY,
)

account_setup_seconds = Gauge(
    "cmping_account_setup_seconds",
    "Time spent on account setup in the last probe round",
    LABELS,
    registry=CMPING_REGISTRY,
)

last_round_timestamp = Gauge(
    "cmping_last_round_completion_timestamp",
    "Unix timestamp of the last completed probe round (for staleness alerting)",
    registry=CMPING_REGISTRY,
)
last_round_timestamp.set(float("nan"))  # NaN until first round completes

round_duration_seconds = Gauge(
    "cmping_round_duration_seconds",
    "Wall-clock duration of the last completed probe round",
    registry=CMPING_REGISTRY,
)
round_duration_seconds.set(float("nan"))  # NaN until first round completes

rounds_total = Counter(
    "cmping_rounds_total",
    "Total number of probe rounds completed since process start",
    registry=CMPING_REGISTRY,
)

account_creations_total = Counter(
    "cmping_account_creations_total",
    "Total chatmail accounts created since process start",
    ["relay"],
    registry=CMPING_REGISTRY,
)

relay_status = Gauge(
    "cmping_relay_status",
    (
        "Relay availability status from alive checks. Integer encodes state: "
        "1=online, 0=unknown, -1=timeout, -2=setup, -3=auth, "
        "-4=tls, -5=connection_refused, -6=dns. Label: relay."
    ),
    ["relay"],
    registry=CMPING_REGISTRY,
)

relay_connections = Gauge(
    "cmping_relay_connections",
    "Current network connection count to relay (established TCP connections)",
    ["relay"],
    registry=CMPING_REGISTRY,
)

#
# Per-relay TURN endpoint metrics
#

_TURN_LABELS = ["relay", "turn_endpoint"]

relay_turn_status = Gauge(
    "cmping_relay_turn_status",
    (
        "TURN endpoint health from turnutils_uclient. "
        "1=ok, 0=down, -2=parse-error, -4=binary-missing, -5=timeout. "
        "Labels: relay, turn_endpoint (self|fallback)."
    ),
    _TURN_LABELS,
    registry=CMPING_REGISTRY,
)

relay_turn_connect_seconds = Gauge(
    "cmping_relay_turn_connect_seconds",
    "Time to establish the TURN allocation.",
    _TURN_LABELS,
    registry=CMPING_REGISTRY,
)

relay_turn_transmit_seconds = Gauge(
    "cmping_relay_turn_transmit_seconds",
    "Total TURN loopback test transmit duration.",
    _TURN_LABELS,
    registry=CMPING_REGISTRY,
)

#
# Per-relay iroh-relay metrics
#

_IROH_LABELS = ["relay"]

relay_iroh_status = Gauge(
    "cmping_relay_iroh_status",
    (
        "Iroh-relay HTTP health.  1=ok, 0=down, "
        "-2=no-metadata (server has no /shared/vendor/deltachat/irohrelay), "
        "-3=imap-failed (could not fetch metadata), "
        "-5=timeout (HTTP probe timed out)."
    ),
    _IROH_LABELS,
    registry=CMPING_REGISTRY,
)

relay_iroh_latency_seconds = Gauge(
    "cmping_relay_iroh_latency_seconds",
    "Last successful iroh-relay HTTP GET latency.",
    _IROH_LABELS,
    registry=CMPING_REGISTRY,
)


def _drop_labels(
    metric: Any, keep: Callable[[tuple[str, ...]], bool]
) -> None:
    """Remove label sets where keep(label_tuple) is False."""
    for label_values in list(metric._metrics.keys()):
        if not keep(label_values):
            metric.remove(*label_values)


def clear_stale_labels(active_relays: list[str]) -> None:
    """Remove label sets for relays no longer in the active set.

    Prevents label cardinality from growing unbounded when relays are
    removed from the relay list across process restarts or alive-check
    exclusions.
    """
    all_metrics = [
        rtt_median, rtt_stddev, rtt_p90, rtt_p10,
        probe_success, probe_loss_ratio, account_setup_seconds,
        send_errors_total,
    ]
    active = set(active_relays)
    for metric in all_metrics:
        _drop_labels(metric, lambda lv: lv[0] in active and lv[1] in active)


_CATEGORY_TO_STATUS: dict[str | None, int] = {
    None: 1,
    "timeout": -1,
    "connection_refused": -5,
    "dns": -6,
    "tls": -4,
    "auth": -3,
    "setup": -2,
    "unknown": 0,
}


def relay_status_value(error_str: str | None) -> int:
    """Map alive-check error string to cmping_relay_status integer.

    Delegates to _classify_error() so all pattern matching lives in one place.
    Return values: 1=ok, 0=unknown, -1=timeout, -2=setup, -3=auth,
    -4=tls, -5=connection_refused, -6=dns.
    """
    return _CATEGORY_TO_STATUS.get(_classify_error(error_str), 0)


def verify_relay_status(relay: str | None, error_str: str | None) -> int:
    """Get relay status value, cross-checking DNS errors against real resolution.

    When the RPC reports DNS failure but the base domain resolves, the
    error is reclassified as timeout (broken autoconfig, not missing DNS).
    """
    value = relay_status_value(error_str)
    if value != -6 or relay is None:
        return value
    # Strip IPv6 brackets ("[::1]" -> "::1") so getaddrinfo accepts the literal.
    host = relay[1:-1] if relay.startswith("[") and relay.endswith("]") else relay
    # RPC says DNS failure -- verify by resolving the base domain.
    try:
        socket.getaddrinfo(host, 993)
    except socket.gaierror:
        return -6  # genuine DNS failure: base domain does not resolve
    # An IP literal can't have a DNS failure and has no autoconfig subdomains
    # to cross-check; the RPC DNS error was spurious (e.g. a filtered port).
    if is_ip_address(host):
        return -1
    # Base domain resolves -- check autoconfig subdomains for diagnostics.
    missing = []
    for sub in (f"imap.{relay}", f"smtp.{relay}"):
        try:
            socket.getaddrinfo(sub, None)
        except socket.gaierror:
            missing.append(sub)

    if missing:
        log.info(
            "DNS cross-check: %s resolves but subdomain(s) %s missing "
            "(broken autoconfig); reclassifying as timeout",
            relay, ", ".join(missing),
        )
    else:
        log.info(
            "DNS cross-check: %s and subdomains all resolve but RPC "
            "reported DNS error (port filtered?); reclassifying as timeout",
            relay,
        )
    return -1


# Alive-check status values that might resolve on retry: -1 (timeout) and
# 0 (unknown).  Persistent errors (genuine DNS, auth, TLS, connection refused)
# won't change by waiting.  Single source of truth shared with
# orchestration.check_relays_alive so the two retry gates can't diverge.
_TRANSIENT_ALIVE_STATUSES = frozenset({-1, 0})


def is_transient_alive_error(relay: str | None, error_str: str | None) -> bool:
    """Check if an alive-check error is transient and worth retrying.

    Returns True for errors that might resolve on retry (timeouts,
    reclassified DNS).  Returns False for persistent errors (genuine DNS,
    auth, TLS, connection refused) that won't change with a retry.
    """
    return verify_relay_status(relay, error_str) in _TRANSIENT_ALIVE_STATUSES



def clear_stale_relay_labels(configured_relays: list[str]) -> None:
    """Remove per-relay label sets for relays no longer in the configured list."""
    active = set(configured_relays)
    for metric in (relay_status, account_creations_total, relay_connections,
                   relay_turn_status, relay_turn_connect_seconds,
                   relay_turn_transmit_seconds,
                   relay_iroh_status, relay_iroh_latency_seconds):
        _drop_labels(metric, lambda lv: lv[0] in active)


def update_turn_metrics(relay: str, result: "TurnResult | None") -> None:
    """Write a per-relay TURN check outcome to the prometheus gauges.

    Pass result=None when ice_servers() was empty or malformed: records
    status=-2 and leaves timing series untouched so old samples stay
    visible (smokeping-style)."""
    # Lazy import: turn.py -> log_config; metrics.py is imported very early.
    from .turn import TurnStatus  # noqa: PLC0415

    if result is None:
        relay_turn_status.labels(relay=relay, turn_endpoint="self").set(
            TurnStatus.PARSE_ERROR,
        )
        return

    base = {"relay": relay, "turn_endpoint": result.endpoint_kind}
    relay_turn_status.labels(**base).set(result.status_code)
    run = result.run
    for gauge, value in (
        (relay_turn_connect_seconds,  run.connect_s),
        (relay_turn_transmit_seconds, run.transmit_s),
    ):
        if value is not None:
            gauge.labels(**base).set(value)


def update_iroh_metrics(relay: str, result: "IrohResult") -> None:
    """Write a per-relay iroh-relay check outcome to prometheus.

    Always sets the status gauge.  Sets latency only when the probe
    succeeded; on failure the previous latency sample is left in place
    (smokeping convention -- last-good value stays visible)."""
    relay_iroh_status.labels(relay=relay).set(result.status)
    if result.latency_s is not None and result.status == IrohStatus.OK:
        relay_iroh_latency_seconds.labels(relay=relay).set(result.latency_s)


def sample_relay_connections(relays: list[str]) -> None:
    """Sample established TCP connection counts to each relay.

    Counts connections from the whole host (not just this process) to *any*
    of the relay's resolved IPs, so treat it as a coarse gauge.  A relay
    behind several A/AAAA records is summed across all of them rather than
    counting only the first.
    """
    for relay in relays:
        try:
            infos = socket.getaddrinfo(relay, 993, proto=socket.IPPROTO_TCP)
            ips = {info[4][0] for info in infos}
            conn_count = 0
            for ip in ips:
                result = subprocess.run(
                    ["ss", "-tn", f"dst {ip}"],
                    capture_output=True, text=True, timeout=5,
                    check=False,
                )
                rows = sum(1 for l in result.stdout.splitlines() if l.strip())
                conn_count += max(0, rows - 1)  # subtract per-invocation header
            relay_connections.labels(relay=relay).set(conn_count)
        except Exception as e:
            log.debug("sample_connections failed for %s: %s", relay, type(e).__name__)
            relay_connections.labels(relay=relay).set(0)


def _set_rtt_metrics(labels: dict[str, str], rtt_s: list[float]) -> None:
    """Set median/p10/p90/stddev gauges from a non-empty RTT sample (seconds)."""
    if len(rtt_s) >= 2:
        # quantiles(n=10, method="inclusive") returns 9 cut points;
        # index 0 = p10, index 8 = p90.  "inclusive" interpolates within
        # the data range (exclusive can extrapolate beyond min/max).  This is
        # the same "inclusive" method ProbeResult._stats uses (n=100), so the
        # p90 here and the CLI table's p90 agree on identical samples.
        deciles = statistics.quantiles(rtt_s, n=10, method="inclusive")
        p10, p90, stddev = deciles[0], deciles[-1], statistics.stdev(rtt_s)
    else:
        p10 = p90 = rtt_s[0]
        stddev = 0.0
    rtt_median.labels(**labels).set(statistics.median(rtt_s))
    rtt_p10.labels(**labels).set(p10)
    rtt_p90.labels(**labels).set(p90)
    rtt_stddev.labels(**labels).set(stddev)


def update_metrics(result: ProbeResult) -> None:
    """Update Prometheus metrics from a ProbeResult."""
    probe_type = "self" if result.source == result.destination else "cross"
    labels: dict[str, str] = dict(source=result.source, destination=result.destination,
                                  probe_type=probe_type)

    if result.error:
        send_errors_total.labels(**labels).inc()
        probe_success.labels(**labels).set(0)
        probe_loss_ratio.labels(**labels).set(1.0)
        # Clear RTT gauges so dashboards don't show stale values from the
        # last successful round while probe_success=0.
        rtt_median.labels(**labels).set(float("nan"))
        rtt_p90.labels(**labels).set(float("nan"))
        rtt_p10.labels(**labels).set(float("nan"))
        rtt_stddev.labels(**labels).set(float("nan"))
        account_setup_seconds.labels(**labels).set(float("nan"))
        return

    # Derive both success and loss from sent/received (single source of truth)
    # rather than mixing result.loss with our own computation.
    if result.sent > 0:
        loss_ratio = 1.0 - result.received / result.sent
        probe_success.labels(**labels).set(1 if loss_ratio == 0.0 else 0)
        probe_loss_ratio.labels(**labels).set(loss_ratio)
    else:
        probe_success.labels(**labels).set(0)
        probe_loss_ratio.labels(**labels).set(1.0)
    account_setup_seconds.labels(**labels).set(result.account_setup_time)

    if result.rtts_ms:
        _set_rtt_metrics(labels, [r / 1000.0 for r in result.rtts_ms])
