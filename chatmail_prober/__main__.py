"""chatmail-prober: Smokeping-style Prometheus exporter for chatmail relay interop.

Periodically probes all pairs of configured chatmail relays and exposes
round-trip time histograms, counters, and success gauges as Prometheus metrics.
"""

import argparse
import logging
import os
import re
import resource
import shutil
import signal
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import FrameType

from .cli_summary import render as render_summary
from .log_config import configure_logging, get_logger
from .orchestration import (
    check_relays_alive,
    kill_stale_rpc_servers,
    run_round,
    scan_relays,
)
from .output import print_metrics, start_exporter_server, write_textfile
from .probe import RelayPool
from .turn import TurnResolved

log = get_logger(__name__)

AUTO_FETCH_URL = "https://chatmail.at/relays"


class _SupprRpcClosedFilter(logging.Filter):
    """Suppress expected 'RPC server closed' errors during shutdown."""
    def __init__(self, shutdown_event: threading.Event) -> None:
        super().__init__()
        self._shutdown_event = shutdown_event

    def filter(self, record: logging.LogRecord) -> bool:
        if (self._shutdown_event.is_set()
                and "RPC server closed" in str(record.getMessage())):
            return False  # suppress during shutdown
        return True


def _parse_extra_turns(specs: list[str]) -> dict[str, TurnResolved]:
    """Parse HOST:PORT:USER:CRED specs into a host -> TurnResolved map."""
    result: dict[str, TurnResolved] = {}
    for spec in specs:
        try:
            host, port_s, user, cred = spec.split(":", 3)
            result[host] = (host, int(port_s), user, cred, "self")
        except ValueError:
            raise SystemExit(
                f"CHATMAIL_EXTRA_TURN: bad spec {spec!r} (expected HOST:PORT:USER:CRED)"
            )
    return result


def read_relay_list(paths: list[str]) -> list[str]:
    """Read relay domains from one or more files (one per line, # comments).

    Deduplicates across files while preserving first-seen order.
    """
    raw_lines: list[str] = []
    for path in paths:
        with open(path) as f:
            for raw in f:
                line = raw.strip()
                if line and not line.startswith("#"):
                    raw_lines.append(line)
    relays = list(dict.fromkeys(raw_lines))
    if not relays:
        raise SystemExit(f"No relays found in {paths}")
    return relays


def read_exclude_list(path: str) -> set[tuple[str, str]]:
    """Read pair exclusions from a file.

    Format: one "source->destination" per line.  # comments and blank lines
    are ignored.  Returns a set of (source, destination) tuples.
    """
    excludes: set[tuple[str, str]] = set()
    with open(path) as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "->" not in line:
                log.warning("Ignoring malformed exclude line: %s", line)
                continue
            src, dst = line.split("->", 1)
            excludes.add((src.strip(), dst.strip()))
    log.info("Loaded %d pair exclusion(s) from %s", len(excludes), path)
    return excludes


def _bracket_ipv6(host: str) -> str:
    """Wrap a bare IPv6 address in square brackets; leave everything else unchanged."""
    if ":" in host and not host.startswith("["):
        return f"[{host}]"
    return host


def fetch_relay_list(url: str, dest: str) -> None:
    """Fetch relay domains from url, write to dest (one domain per line).

    Parses chatmail.at/relays HTML: extracts text from <a class="hilite"> tags.
    """
    with urllib.request.urlopen(url, timeout=30) as resp:
        html = resp.read().decode()
    domains = re.findall(r'class="hilite">([^<]+)', html)
    if not domains:
        raise SystemExit(f"No relay domains found at {url}")
    Path(dest).write_text("\n".join(domains) + "\n")
    log.info("Fetched %d relays from %s -> %s", len(domains), url, dest)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="chatmail-prober",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "relays",
        nargs="*",
        help="relay list file(s) (one domain per line, # comments); at least one of this or --auto-fetch required",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=0,
        help="HTTP listen port for /metrics (0 = disabled, e.g. --port 9740)",
    )
    parser.add_argument(
        "--textfile",
        default=None,
        help="path to write .prom file for node_exporter textfile collector",
    )
    parser.add_argument(
        "-i", "--interval",
        type=int,
        default=900,
        help="seconds between probe rounds (default: 900 = 15min)",
    )
    parser.add_argument(
        "--alive-check-interval",
        type=int,
        default=86400,
        help="seconds between relay alive re-checks (default: 86400 = 24h, 0 = every round)",
    )
    parser.add_argument(
        "-n", "--count",
        type=int,
        default=5,
        help="number of pings per pair per round (default: 5)",
    )
    parser.add_argument(
        "--ping-interval",
        type=float,
        default=0.1,
        help="seconds between individual pings within a probe (default: 0.1)",
    )
    parser.add_argument(
        "-t", "--timeout",
        type=int,
        default=90,
        help="per-pair receive timeout in seconds (default: 90)",
    )
    parser.add_argument(
        "-w", "--workers",
        type=int,
        default=5,
        help="max concurrent probe threads (default: 5)",
    )
    parser.add_argument(
        "--cache-dir",
        default="~/.cache/chatmail-prober",
        help="base dir for per-pair accounts (default: ~/.cache/chatmail-prober)",
    )
    parser.add_argument(
        "-1", "--once",
        action="store_true",
        help="run one probe round then exit (useful for testing)",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="count",
        default=0,
        help="increase verbosity (-v debug, -vv rpc/deltachat events)",
    )
    parser.add_argument(
        "-q", "--quiet",
        action="store_true",
        help="suppress progress output (only show warnings/errors)",
    )
    parser.add_argument(
        "--scan",
        action="store_true",
        help="run self-probes on all relays, print ranked by RTT, then exit",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=10,
        help="number of fastest relays to highlight in --scan output (default: 10)",
    )
    parser.add_argument(
        "--auto-fetch",
        default=None,
        metavar="PATH",
        help=f"fetch relay list from {AUTO_FETCH_URL} and write to PATH before starting",
    )
    parser.add_argument(
        "--exclude",
        default=None,
        metavar="PATH",
        help='file of pairs to skip, one per line: "src->dst" (# comments allowed)',
    )
    parser.add_argument(
        "-u", "--unreachable",
        default=None,
        metavar="FILE",
        help=(
            "file of known-unreachable relays (one per line); these are alive-checked "
            "each round but excluded from the probe matrix. If one recovers it is "
            "automatically promoted to the active set for that round."
        ),
    )
    parser.add_argument(
        "--reset",
        nargs="*",
        metavar="DOMAIN",
        default=None,
        help="reset cached accounts; with no args resets all, with DOMAIN args resets only those domains",
    )
    parser.add_argument(
        "-m", "--print-metrics",
        action="store_true",
        default=False,
        help="print Prometheus metrics to stdout after --once exits (requires --once)",
    )
    parser.add_argument(
        "-p", "--print",
        dest="print_summary",
        action="store_true",
        default=False,
        help="print tabular summary to stdout after --once exits (requires --once)",
    )
    parser.add_argument(
        "-T", "--check-turn",
        action="store_true",
        default=False,
        help="also probe each relay's TURN endpoint via turnutils_uclient (requires coturn-utils)",
    )
    parser.add_argument(
        "-I", "--check-iroh",
        action="store_true",
        default=False,
        help="also probe each relay's iroh-relay URL (via IMAP METADATA + HTTP GET)",
    )
    parser.add_argument(
        "-H", "--hosts",
        default=None,
        metavar="HOST[,HOST...]",
        help="comma-separated relay list overriding relay file(s); bare IPv6 addresses are auto-bracketed",
    )
    args = parser.parse_args(argv)
    if args.reset is not None and not args.reset:
        parser.error(
            "--reset requires at least one DOMAIN or 'all'\n"
            "  examples: --reset all\n"
            "            --reset nine.testrun.org mailchat.pl"
        )
    env_specs = os.environ.get("CHATMAIL_EXTRA_TURN", "").split()
    args.extra_turn_map = _parse_extra_turns(env_specs)
    return args


def reset_accounts(cache_dir: Path, domains: list[str]) -> None:
    """Remove cached account directories.

    domains=["all"] wipes all worker-* dirs but preserves alive-check
    (its long-lived persistent accounts are valuable to keep).
    Selective per-domain reset is not supported with the flat layout
    (worker-N/accounts.toml holds many domains in one DB); use
    scripts/cleanup_accounts.py for per-account pruning instead.
    """
    if domains != ["all"]:
        raise SystemExit(
            "Selective per-domain reset is not supported with the flat "
            "per-worker layout. Use one of:\n"
            "  --reset all                          (wipe all worker dirs)\n"
            "  scripts/cleanup_accounts.py --apply  (trim excess accounts)"
        )
    for child in cache_dir.iterdir():
        if child.is_dir() and child.name != "alive-check":
            shutil.rmtree(child)
            log.info("Reset: removed %s", child)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)

    if args.quiet:
        app_level = logging.WARNING
    elif args.verbose >= 1:
        app_level = logging.DEBUG
    else:
        app_level = logging.INFO

    configure_logging(level=app_level)

    start_time = time.time()
    try:
        from ._version import __version__ as _pkg_version
    except ImportError:
        _pkg_version = "unknown"
    log.debug("chatmail-prober %s starting at %s",
              _pkg_version,
              time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(start_time)))

    shutdown_event = threading.Event()
    # Mutable single-slot holder so SIGUSR2 can report whatever phase the
    # main loop is in.  Updated in-place as the loop transitions.
    phase: list[str] = ["starting"]

    # Suppress harmless "RPC server closed" errors from event loop during shutdown.
    logging.getLogger().addFilter(_SupprRpcClosedFilter(shutdown_event))

    if args.verbose >= 2:
        logging.getLogger().setLevel(logging.DEBUG)

    # Raise the fd soft limit to the hard limit so large relay matrices
    # don't hit the default 1024 cap when deltachat-rpc-server opens many DBs.
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        if soft < hard:
            resource.setrlimit(resource.RLIMIT_NOFILE, (hard, hard))
            log.debug("Raised fd limit %d -> %d", soft, hard)
    except (ValueError, OSError) as e:
        log.debug("fd limit raise failed: %s", e)

    cache_dir = Path(args.cache_dir).expanduser()
    cache_dir.mkdir(parents=True, exist_ok=True)

    if args.reset is not None:
        reset_accounts(cache_dir, domains=args.reset)
        raise SystemExit(0)

    # Load relays.
    if args.hosts is not None:
        relays = [_bracket_ipv6(h.strip()) for h in args.hosts.split(",") if h.strip()]
        if not relays:
            raise SystemExit("error: --hosts list is empty")
    else:
        relay_files = list(args.relays)
        if args.auto_fetch:
            fetch_relay_list(AUTO_FETCH_URL, args.auto_fetch)
            relay_files.append(args.auto_fetch)
        if not relay_files:
            raise SystemExit("error: at least one relay list file, --hosts, or --auto-fetch is required")
        relays = read_relay_list(relay_files)
    log.info("Loaded %d relays: %s", len(relays), ", ".join(relays))

    unreachable_relays: list[str] = []
    if args.unreachable:
        unreachable_relays = read_relay_list([args.unreachable])
        log.info("Loaded %d unreachable relays: %s",
                 len(unreachable_relays), ", ".join(unreachable_relays))
    exclude = set()
    if args.exclude:
        exclude = read_exclude_list(args.exclude)

    if args.scan:
        scan_relays(relays, args, cache_dir)
        return

    total_pairs = len(relays) ** 2 - len(exclude)
    log.info(
        "Pairs: %d, count: %d, interval: %ds, workers: %d",
        total_pairs, args.count, args.interval, args.workers,
    )

    kill_stale_rpc_servers(cache_dir)
    for lock in cache_dir.rglob("accounts.lock"):
        lock.unlink(missing_ok=True)
        log.debug("Removed stale lock: %s", lock)

    executors: list[ThreadPoolExecutor] = []

    stop_after_round = threading.Event()

    def _handle_signal(signum: int, frame: FrameType | None) -> None:
        if shutdown_event.is_set():
            return  # second signal: let systemd SIGKILL us
        log.info("Shutting down, killing running probes...")
        phase[0] = "shutting down"
        shutdown_event.set()
        for ex in executors:
            ex.shutdown(wait=False, cancel_futures=True)

    def _handle_usr1(signum: int, frame: FrameType | None) -> None:
        stop_after_round.set()
        log.warning("SIGUSR1 received -- will exit after current round completes")

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGUSR1, _handle_usr1)

    # Verbosity cycle: quiet -> normal -> debug -> debug+rpc -> quiet ...
    _verbosity_levels = [
        (logging.WARNING, logging.WARNING, "quiet"),
        (logging.INFO,    logging.WARNING, "normal"),
        (logging.DEBUG,   logging.WARNING, "debug"),
        (logging.DEBUG,   logging.DEBUG,   "debug+rpc"),
    ]
    if args.quiet:
        _verbosity_idx = 0
    elif args.verbose >= 2:
        _verbosity_idx = 3
    elif args.verbose >= 1:
        _verbosity_idx = 2
    else:
        _verbosity_idx = 1

    def _handle_usr2(signum: int, frame: FrameType | None) -> None:
        nonlocal _verbosity_idx
        _verbosity_idx = (_verbosity_idx + 1) % len(_verbosity_levels)
        level, root_level, label = _verbosity_levels[_verbosity_idx]
        logging.getLogger("chatmail_prober").setLevel(level)
        logging.getLogger().setLevel(root_level)
        uptime_s = time.time() - start_time
        log.warning(
            "SIGUSR2: verbosity -> %s | phase: %s | uptime: %.0fs | version: %s",
            label, phase[0], uptime_s, _pkg_version,
        )

    signal.signal(signal.SIGUSR2, _handle_usr2)

    all_relays = relays
    alive_pool = RelayPool(cache_dir / "alive-check")
    worker_pools: list[RelayPool] = []
    previously_dead: dict[str, str | None] = {}
    try:
        phase[0] = "initial alive check"
        relays, previously_dead = check_relays_alive(
            all_relays, args, cache_dir, unreachable_relays=unreachable_relays,
            alive_pool=alive_pool)
        if not relays:
            raise SystemExit("No reachable relays -- aborting")
        log.info("continuing with %d/%d relays online, starting matrix probe", len(relays), len(all_relays))
        last_alive_check = time.monotonic()
        log.info("next alive check in %ds", args.alive_check_interval)

        if args.port:
            start_exporter_server(args.port)

        executors.extend(ThreadPoolExecutor(max_workers=1) for _ in range(args.workers))
        worker_pools = [RelayPool(cache_dir / f"worker-{i}") for i in range(args.workers)]

        while not shutdown_event.is_set():
            interval = args.alive_check_interval
            if interval == 0 or time.monotonic() - last_alive_check >= interval:
                if args.auto_fetch:
                    try:
                        fetch_relay_list(AUTO_FETCH_URL, args.auto_fetch)
                        refreshed = read_relay_list(relay_files)
                        if refreshed != all_relays:
                            added = set(refreshed) - set(all_relays)
                            removed = set(all_relays) - set(refreshed)
                            if added:
                                log.warning("Relay list: %d new: %s", len(added), ", ".join(added))
                            if removed:
                                log.warning("Relay list: %d removed: %s", len(removed), ", ".join(removed))
                            all_relays = refreshed
                    except Exception as e:
                        log.warning("Failed to refresh relay list: %s", e)
                phase[0] = "alive check"
                relays, previously_dead = check_relays_alive(
                    all_relays, args, cache_dir, previously_dead=previously_dead,
                    unreachable_relays=unreachable_relays,
                    alive_pool=alive_pool)
                last_alive_check = time.monotonic()
                alive_pool.prune(all_relays)
                for pool in worker_pools:
                    pool.prune(all_relays)
                log.info("continuing with %d/%d relays online, next check in %ds", len(relays), len(all_relays), interval)

            phase[0] = f"probe round ({len(relays)} relays)"
            elapsed, round_results = run_round(
                relays, args, executors, worker_pools,
                shutdown_event,
                textfile=args.textfile, exclude=exclude,
            )

            if args.textfile:
                write_textfile(args.textfile)

            if args.once or stop_after_round.is_set():
                if args.print_summary:
                    render_summary(
                        round_results, relays, previously_dead, elapsed_s=elapsed
                    )
                if args.print_metrics:
                    print_metrics()
                break

            remaining = max(0, args.interval - elapsed)
            if remaining == 0:
                log.warning(
                    "Probe round took %.0fs, exceeds interval %ds -- starting next immediately",
                    elapsed, args.interval,
                )
            else:
                log.info("Sleeping %.0fs until next round", remaining)
                phase[0] = f"sleeping {remaining:.0f}s until next round"
                shutdown_event.wait(timeout=remaining)
    finally:
        if args.textfile:
            log.info("Writing final metrics")
            write_textfile(args.textfile)
        alive_pool.close()
        for pool in worker_pools:
            pool.close()
        for ex in executors:
            ex.shutdown(wait=not shutdown_event.is_set(), cancel_futures=True)
        if shutdown_event.is_set():
            os._exit(0)


if __name__ == "__main__":
    main()
