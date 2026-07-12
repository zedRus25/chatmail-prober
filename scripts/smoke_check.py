#!/usr/bin/env python3
"""smoke_check.py — validate chatmail-prober .prom file against relay list.

Usage:
    python3 scripts/smoke_check.py \\
        --prom /var/lib/prometheus/node-exporter/chatmail-prober.prom \\
        --relays /var/lib/chatmail-prober/relays.txt \\
                 /var/lib/chatmail-prober/relays.local

Exit codes:
    0  all checks passed
    1  one or more checks failed
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

#
# Parsing helpers
#

_LABEL_RE = re.compile(r'(\w+)="([^"]*)"')


def _parse_labels(label_str: str) -> dict[str, str]:
    return dict(_LABEL_RE.findall(label_str))


def _parse_prom(path: Path) -> dict[str, list[dict[str, str]]]:
    """Return {metric_name: [label_dict, ...]} for every sample in the file.

    Handles both labelled samples (``metric{k="v"} 1``) and bare samples
    (``metric 1``) such as counters and label-less gauges.
    """
    result: dict[str, list[dict[str, str]]] = {}
    labelled_re = re.compile(r'^(\w+)\{([^}]*)\}\s+(.+)$')
    bare_re = re.compile(r'^(\w+)\s+([\d.eE+\-NnAaIi]+)$')  # NaN, Inf, numbers
    for line in path.read_text().splitlines():
        if line.startswith('#') or not line.strip():
            continue
        m = labelled_re.match(line)
        if m:
            name, label_str = m.group(1), m.group(2)
            result.setdefault(name, []).append(_parse_labels(label_str))
            continue
        m = bare_re.match(line)
        if m:
            result.setdefault(m.group(1), []).append({})
    return result


def _read_relays(*files: Path) -> list[str]:
    """Read relay hostnames from one or more files (one per line, skip blanks/comments)."""
    seen: set[str] = set()
    relays: list[str] = []
    for f in files:
        for line in f.read_text().splitlines():
            host = line.strip()
            if host and not host.startswith('#') and host not in seen:
                seen.add(host)
                relays.append(host)
    return relays


#
# Checks
#

def check_relay_status(
    metrics: dict[str, list[dict[str, str]]],
    relays: list[str],
) -> tuple[bool, str]:
    """Check cmping_relay_status: every relay should have a status entry."""
    entries = metrics.get("cmping_relay_status", [])
    known = {e["relay"] for e in entries}
    missing = [r for r in relays if r not in known]
    extra = [r for r in known if r not in relays]
    lines = [
        f"  relay_status entries : {len(entries)} / {len(relays)} expected",
    ]
    ok = True
    if missing:
        lines.append(f"  MISSING from .prom   : {', '.join(sorted(missing))}")
        ok = False
    if extra:
        lines.append(f"  EXTRA in .prom       : {', '.join(sorted(extra))}")
    return ok, "\n".join(lines)


def check_matrix(
    metrics: dict[str, list[dict[str, str]]],
    relays: list[str],
    metric: str,
    probe_type: str = "cross",
    threshold: float = 0.80,
) -> tuple[bool, str]:
    """Check that a matrix metric has at least threshold*N² pairs."""
    entries = [
        e for e in metrics.get(metric, [])
        if e.get("probe_type") == probe_type
    ]
    pairs = {(e["source"], e["destination"]) for e in entries}
    n = len(relays)
    expected = n * n
    actual = len(pairs)
    pct = 100.0 * actual / expected if expected else 0.0
    ok = actual >= threshold * expected

    # Find which relays are completely absent as source
    sources_seen = {p[0] for p in pairs}
    absent_sources = sorted(r for r in relays if r not in sources_seen)

    lines = [
        f"  {metric}[{probe_type}]",
        f"  pairs in .prom : {actual} / {expected} expected ({pct:.1f}%)",
    ]
    if absent_sources:
        lines.append(f"  absent sources : {', '.join(absent_sources)}")
    if not ok:
        lines.append(f"  FAIL: below {threshold*100:.0f}% threshold")
    return ok, "\n".join(lines)


def check_heartbeat(
    metrics: dict[str, list[dict[str, str]]],
) -> tuple[bool, str]:
    """Check that heartbeat gauges are present and non-NaN."""
    # We just check the metric names exist; value parsing is not needed for
    # a smoke check (NaN is valid during the first round).
    present = {
        "cmping_last_round_completion_timestamp": "cmping_last_round_completion_timestamp" in metrics,
        "cmping_round_duration_seconds": "cmping_round_duration_seconds" in metrics,
        "cmping_rounds_total": "cmping_rounds_total" in metrics,
    }
    missing = [k for k, v in present.items() if not v]
    ok = not missing
    lines = ["  heartbeat metrics present: " + (", ".join(present) if ok else "MISSING: " + ", ".join(missing))]
    return ok, "\n".join(lines)


def check_setup_vs_rtt(
    metrics: dict[str, list[dict[str, str]]],
    relays: list[str],
) -> tuple[bool, str]:
    """Warn if setup entries significantly outnumber RTT entries (indicates mass setup failures)."""
    rtt_pairs = {
        (e["source"], e["destination"])
        for e in metrics.get("cmping_rtt_median_seconds", [])
        if e.get("probe_type") == "cross"
    }
    setup_pairs = {
        (e["source"], e["destination"])
        for e in metrics.get("cmping_account_setup_seconds", [])
        if e.get("probe_type") == "cross"
    }
    setup_only = setup_pairs - rtt_pairs  # had setup but no RTT → probe failed
    pct_failed = 100.0 * len(setup_only) / len(setup_pairs) if setup_pairs else 0.0
    ok = pct_failed < 20.0  # warn if >20% of pairs have setup but no RTT
    lines = [
        f"  setup pairs : {len(setup_pairs)}  rtt pairs : {len(rtt_pairs)}",
        f"  setup-only (no RTT, i.e. failed after setup) : {len(setup_only)} ({pct_failed:.1f}%)",
    ]
    if not ok:
        lines.append(f"  WARN: {pct_failed:.1f}% of pairs failed after account setup")
    return ok, "\n".join(lines)


#
# Main
#

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Smoke-check chatmail-prober .prom file against relay list"
    )
    parser.add_argument(
        "--prom", required=True, type=Path,
        metavar="FILE",
        help="path to chatmail-prober.prom textfile",
    )
    parser.add_argument(
        "--relays", required=True, nargs="+", type=Path,
        metavar="FILE",
        help="one or more relay list files (one hostname per line)",
    )
    parser.add_argument(
        "--threshold", type=float, default=0.80,
        metavar="FLOAT",
        help="minimum fraction of expected matrix pairs (default: 0.80)",
    )
    parser.add_argument(
        "--exclude-unreachable", type=Path, default=None,
        metavar="FILE",
        help=(
            "file of known-unreachable relays (one per line); these are excluded "
            "from the expected matrix N so known-dead relays don't cause FAIL"
        ),
    )
    args = parser.parse_args(argv)

    all_relays = _read_relays(*args.relays)
    unreachable: set[str] = set()
    if args.exclude_unreachable:
        unreachable = set(_read_relays(args.exclude_unreachable))
    relays = [r for r in all_relays if r not in unreachable]
    metrics = _parse_prom(args.prom)

    if unreachable:
        print(f"Relay list  : {len(relays)} active relays "
              f"({len(unreachable)} unreachable excluded) "
              f"from {[str(f) for f in args.relays]}")
    else:
        print(f"Relay list  : {len(relays)} unique relays from {[str(f) for f in args.relays]}")
    print(f"Prom file   : {args.prom}  ({sum(len(v) for v in metrics.values())} samples)\n")

    checks = [
        ("relay_status",   check_relay_status(metrics, relays)),
        ("matrix:rtt",     check_matrix(metrics, relays, "cmping_rtt_median_seconds", threshold=args.threshold)),
        ("matrix:setup",   check_matrix(metrics, relays, "cmping_account_setup_seconds", threshold=args.threshold)),
        ("setup_vs_rtt",   check_setup_vs_rtt(metrics, relays)),
        ("heartbeat",      check_heartbeat(metrics)),
    ]

    all_ok = True
    for name, (ok, detail) in checks:
        status = "PASS" if ok else "FAIL"
        print(f"[{status}] {name}")
        print(detail)
        print()
        if not ok:
            all_ok = False

    if all_ok:
        print("All checks passed.")
    else:
        print("One or more checks FAILED.")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
