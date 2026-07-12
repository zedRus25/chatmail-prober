"""Probe orchestration: alive checks, probe rounds, and relay scanning."""

import argparse
import gc
import os
import shutil
import signal
import statistics
import threading
import time
from concurrent.futures import (
    Future,
    ThreadPoolExecutor,
    as_completed,
)
from concurrent.futures import (
    TimeoutError as FuturesTimeoutError,
)
from pathlib import Path
from collections.abc import Callable
from typing import Any

import structlog

from .log_config import get_logger
from .iroh import IrohResult, IrohStatus, check_relay_iroh
from .metrics import (
    clear_stale_labels,
    clear_stale_relay_labels,
    last_round_timestamp,
    relay_status,
    relay_turn_status,
    round_duration_seconds,
    rounds_total,
    sample_relay_connections,
    update_iroh_metrics,
    update_metrics,
    update_turn_metrics,
    verify_relay_status,
)
from .output import write_textfile
from .probe import ProbeResult, RelayPool, run_probe
from .proc_utils import rpc_server_pids
from .turn import TurnResolved, TurnStatus, check_turn, resolve_relay_turn

log = get_logger(__name__)

# Pre-lowercased keywords indicating an RPC transport failure (not an
# application-level error).  Used to decide when to reopen a RelayContext.
_RPC_CRASH_KEYWORDS = (
    "brokenpipe", "connectionreset", "eoferror", "process",
    "rpc server closed", "rpc process",
)


def _is_rpc_crash(error_str: str) -> bool:
    """True if error_str looks like an RPC transport failure worth a reopen."""
    s = error_str.lower()
    return any(kw in s for kw in _RPC_CRASH_KEYWORDS)


def _try_reopen_pool(
    pool: RelayPool,
    relay_contexts: dict[str, RelayPool],
    *,
    log_event: str,
    **log_ctx: Any,
) -> bool:
    """Reopen pool and refresh relay_contexts; log+return False on failure.

    The caller decides *whether* to reopen (per-pool gate, retry budget,
    etc.); this helper just performs the reopen + context refresh + error
    logging that's identical between alive-check and run_round.
    """
    try:
        pool.reopen()
        relay_contexts.update(pool.contexts())
        return True
    except Exception as e:
        log.warning(log_event, error=str(e), **log_ctx)
        return False


def kill_stale_rpc_servers(cache_dir: str | Path, graceful: bool = True) -> None:
    """Kill orphaned deltachat-rpc-server processes rooted at cache_dir.

    graceful=True sends SIGTERM first (lets sqlite close WAL cleanly);
    graceful=False goes straight to SIGKILL (used during signal-handler shutdown).

    Processes are matched by their DC_ACCOUNTS_PATH environment variable (the
    rpc client passes the accounts dir there, not on argv), so this is
    Linux-only; elsewhere rpc_server_pids returns an empty list.
    """
    pids = rpc_server_pids(cache_dir)
    if not pids:
        return
    if graceful:
        for pid in pids:
            log.warning("Sending SIGTERM to stale deltachat-rpc-server (PID %d)", pid)
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                continue
        time.sleep(2)
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
            log.warning("Sent SIGKILL to deltachat-rpc-server (PID %d)", pid)
        except ProcessLookupError:
            pass  # already exited from SIGTERM


def _get_relay_account(
    pool: RelayPool, relay: str, log_event: str,
) -> Any | None:
    """Shared maker+get_relay_account preamble for aux per-relay checks.

    Returns the configured account, or None if the pool has no maker yet
    or get_relay_account raises.  Callers handle the None case by writing
    the appropriate "unavailable" metric.
    """
    if pool.maker is None:
        return None
    try:
        account, _ = pool.maker.get_relay_account(relay)
        return account
    except Exception as e:
        log.debug(log_event, relay=relay, error=str(e))
        return None


def _check_one_turn(
    pool: RelayPool, relay: str, timeout: float,
    pre_resolved: TurnResolved | None = None,
) -> None:
    """Resolve TURN coords for `relay` and run turnutils_uclient.

    When pre_resolved is provided (CHATMAIL_EXTRA_TURN entry), the
    account/ice_servers lookup is skipped entirely.  Writes metrics directly.
    """
    if pre_resolved is None:
        account = _get_relay_account(pool, relay, "turn_account_unavailable")
        if account is None:
            update_turn_metrics(relay, None)
            return
        pre_resolved = resolve_relay_turn(account, relay)
        if pre_resolved is None:
            update_turn_metrics(relay, None)
            return
    result = check_turn(pre_resolved, timeout=timeout)
    update_turn_metrics(relay, result)
    log.info("turn_check_done", relay=relay, endpoint=pre_resolved[4],
             status=int(result.status_code),
             connect_s=result.run.connect_s,
             transmit_s=result.run.transmit_s)


def _check_one_iroh(
    pool: RelayPool, relay: str, imap_timeout: float, http_timeout: float,
) -> None:
    """Resolve iroh-relay URL via IMAP METADATA and HTTP-probe it."""
    account = _get_relay_account(pool, relay, "iroh_account_unavailable")
    if account is None:
        update_iroh_metrics(relay, IrohResult(
            status=IrohStatus.IMAP_FAILED, error="account unavailable",
        ))
        return
    result = check_relay_iroh(
        account, imap_timeout=imap_timeout, http_timeout=http_timeout,
    )
    update_iroh_metrics(relay, result)
    log.info("iroh_check_done", relay=relay, status=int(result.status),
             url=result.url, latency_s=result.latency_s,
             http_status=result.http_status, error=result.error)


def _run_aux_checks(
    name: str,
    alive_pool: RelayPool,
    alive_relays: list[str],
    workers: int,
    deadline_s: float,
    submit: Callable[[ThreadPoolExecutor, RelayPool, str], Future],
    on_failure: Callable[[str, BaseException], None],
    on_timeout: Callable[[str], None],
) -> None:
    """Generic per-relay fan-out used by both TURN and iroh checks.

    Each task is submitted via `submit(executor, pool, relay)`; per-task
    exceptions go to `on_failure(relay, exc)`, and futures still running
    when the outer deadline trips go to `on_timeout(relay)`.
    """
    log.warning(f"{name}_check_start", count=len(alive_relays), workers=workers)
    start = time.monotonic()
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {submit(executor, alive_pool, r): r for r in alive_relays}
        try:
            for future in as_completed(futures, timeout=deadline_s):
                relay = futures[future]
                try:
                    future.result()
                except Exception as e:
                    log.warning(f"{name}_check_failed", relay=relay, error=str(e))
                    on_failure(relay, e)
        except FuturesTimeoutError:
            for future, relay in futures.items():
                if not future.done():
                    log.warning(f"{name}_check_timeout", relay=relay)
                    future.cancel()
                    on_timeout(relay)
    log.warning(f"{name}_check_complete",
                elapsed_s=round(time.monotonic() - start, 1))


def _run_turn_checks(
    alive_pool: RelayPool, alive_relays: list[str],
    args: argparse.Namespace,
) -> None:
    """Fan out TURN health checks across alive relays and any CHATMAIL_EXTRA_TURN hosts."""
    extra: dict[str, TurnResolved] = getattr(args, "extra_turn_map", {})
    if not getattr(args, "check_turn", False) and not extra:
        return
    # Relay-discovered targets only when --check-turn is set; extra hosts always.
    relay_targets = list(alive_relays) if getattr(args, "check_turn", False) else []
    for host in extra:
        if host not in relay_targets:
            relay_targets.append(host)
    if not relay_targets:
        return
    if shutil.which("turnutils_uclient") is None:
        log.warning("turn_check_skipped",
                    reason="turnutils_uclient not installed (apt install coturn-utils)")
        for relay in relay_targets:
            relay_turn_status.labels(relay=relay, turn_endpoint="self").set(
                TurnStatus.BINARY_MISSING,
            )
        return
    timeout = float(args.timeout // 2) if args.timeout else 30.0
    _run_aux_checks(
        "turn", alive_pool, relay_targets,
        workers=min(len(relay_targets), args.workers),
        deadline_s=timeout * 2,
        submit=lambda ex, p, r: ex.submit(_check_one_turn, p, r, timeout, extra.get(r)),
        on_failure=lambda relay, _e: update_turn_metrics(relay, None),
        on_timeout=lambda relay: update_turn_metrics(relay, None),
    )


def _run_iroh_checks(
    alive_pool: RelayPool, alive_relays: list[str],
    args: argparse.Namespace,
) -> None:
    """Fan out iroh-relay health checks across alive relays."""
    if not getattr(args, "check_iroh", False) or not alive_relays:
        return
    # IMAP login + GETMETADATA is fast (~1s); cap the HTTP probe at the same
    # short window so a misbehaving iroh-relay does not stall the round.
    imap_timeout = min(15.0, float(args.timeout)) if args.timeout else 15.0
    http_timeout = min(15.0, float(args.timeout) / 2) if args.timeout else 15.0
    _run_aux_checks(
        "iroh", alive_pool, alive_relays,
        workers=min(len(alive_relays), args.workers),
        deadline_s=(imap_timeout + http_timeout) * 2,
        submit=lambda ex, p, r: ex.submit(
            _check_one_iroh, p, r, imap_timeout, http_timeout,
        ),
        on_failure=lambda relay, e: update_iroh_metrics(relay, IrohResult(
            status=IrohStatus.IMAP_FAILED, error=str(e),
        )),
        on_timeout=lambda relay: update_iroh_metrics(relay, IrohResult(
            status=IrohStatus.TIMEOUT, error="round timeout",
        )),
    )


def check_relays_alive(
    relays: list[str],
    args: argparse.Namespace,
    cache_dir: Path,
    previously_dead: dict[str, str | None] | None = None,
    unreachable_relays: list[str] | None = None,
    alive_pool: RelayPool | None = None,
) -> tuple[list[str], dict[str, str | None]]:
    """Self-probe each relay in parallel; return (alive_list, dead_dict).

    Transient failures (timeout, unknown) are retried up to 2 times unless
    the relay was already dead last round.  Relays from unreachable_relays
    are checked but excluded from the alive list unless they recover.

    When alive_pool (a RelayPool) is provided, its shared contexts are reused
    across rounds instead of creating throwaway accounts each time.
    """
    if previously_dead is None:
        previously_dead = {}
    unreachable_set: set[str] = set(unreachable_relays or [])
    # Include unreachable relays in the alive check so we can detect recovery.
    all_check_relays = list(relays) + [r for r in unreachable_set if r not in relays]

    # Pre-open shared contexts so accounts are reused across rounds.
    relay_contexts: dict[str, RelayPool] | None
    if alive_pool is not None:
        alive_pool.open_all(all_check_relays)
        relay_contexts = alive_pool.contexts()
    else:
        relay_contexts = None

    def _submit_probe(executor: ThreadPoolExecutor, relay: str) -> Future[ProbeResult]:
        if relay_contexts is not None:
            return executor.submit(
                run_probe, relay, relay, 1, args.ping_interval,
                timeout=args.timeout // 2, relay_contexts=relay_contexts)
        return executor.submit(
            run_probe, relay, relay, 1, args.ping_interval,
            str(cache_dir / "alive-check" / relay), args.timeout // 2)

    with ThreadPoolExecutor(max_workers=min(len(all_check_relays), args.workers)) as pool:
        futures = {
            _submit_probe(pool, r): r
            for r in all_check_relays
        }
        dead: dict[str, str | None] = {}  # relay -> error string
        completed: set[str] = set()
        alive_pool_reopened = False
        actual_workers = min(len(all_check_relays), args.workers)
        batches = -(-len(all_check_relays) // actual_workers)  # ceil division
        deadline = args.timeout * (batches + 1)
        check_start = time.monotonic()
        timeout_at = time.time() + deadline
        log.warning("alive_check_start",
                     count=len(all_check_relays), workers=actual_workers,
                     timeout_at=time.strftime('%H:%M:%S', time.localtime(timeout_at)))
        try:
            for future in as_completed(futures, timeout=deadline):
                relay = futures[future]
                completed.add(relay)
                result = future.result()
                with structlog.contextvars.bound_contextvars(relay=relay):
                    if result.error:
                        log.warning("relay_dead", error=result.error)
                        dead[relay] = result.error
                        if (alive_pool is not None and not alive_pool_reopened
                                and _is_rpc_crash(result.error)):
                            assert relay_contexts is not None  # alive_pool implies contexts
                            if _try_reopen_pool(alive_pool, relay_contexts,
                                                log_event="alive_reopen_failed"):
                                alive_pool_reopened = True
                    else:
                        log.info("relay_ok",
                                 rtt_ms=round(result.rtts_ms[0]) if result.rtts_ms else 0)
                if len(completed) % 10 == 0 or len(completed) == len(all_check_relays):
                    remaining = [r for r in all_check_relays if r not in completed and r not in dead]
                    if remaining:
                        log.info("alive_check_progress",
                                 completed=len(completed), total=len(all_check_relays),
                                 pending=remaining[:5])
        except FuturesTimeoutError:
            elapsed = time.monotonic() - check_start
            for future, relay in futures.items():
                if relay not in completed and relay not in dead:
                    with structlog.contextvars.bound_contextvars(relay=relay):
                        log.warning("relay_timeout",
                                    deadline_s=round(elapsed))
                    dead[relay] = "timeout"
                    future.cancel()

    # Retry relays that failed with transient errors (timeout, unknown).
    # Persistent errors (genuine DNS, auth, TLS, connection refused) are
    # not retried since they won't resolve by waiting.  Relays that were
    # already dead last round are also skipped -- if they didn't recover
    # between rounds, retrying within the same window won't help.
    max_retries = 2
    retry_delay = 5
    # Cache DNS-based verify_relay_status results to avoid redundant lookups.
    _status_cache: dict[tuple[str, str | None], int] = {}

    def _cached_status(relay: str, error_str: str | None) -> int:
        key = (relay, error_str)
        if key not in _status_cache:
            _status_cache[key] = verify_relay_status(relay, error_str)
        return _status_cache[key]

    def _is_transient(relay: str, error_str: str | None) -> bool:
        return _cached_status(relay, error_str) in (-1, 0)

    retryable = {r: err for r, err in dead.items()
                 if r not in previously_dead
                 and _is_transient(r, err)}
    skipped = {r for r in dead if r in previously_dead
               and _is_transient(r, dead[r])}
    if skipped:
        log.info("Skipping retries for %d previously-dead relay(s): %s",
                 len(skipped), ", ".join(skipped))
    if retryable:
        log.warning("alive_check_retrying",
                    count=len(retryable), relays=list(retryable))
    for attempt in range(1, max_retries + 1):
        if not retryable:
            break
        time.sleep(retry_delay)
        log.info("alive_check_retry_attempt",
                 attempt=attempt, max_retries=max_retries, count=len(retryable))
        with ThreadPoolExecutor(max_workers=min(len(retryable), args.workers)) as pool:
            retry_futures = {
                _submit_probe(pool, r): r
                for r in retryable
            }
            try:
                for future in as_completed(retry_futures, timeout=args.timeout * 2):
                    relay = retry_futures[future]
                    result = future.result()
                    with structlog.contextvars.bound_contextvars(
                        relay=relay, attempt=attempt, max_retries=max_retries
                    ):
                        if result.error:
                            log.warning("relay_retry_dead", error=result.error)
                            dead[relay] = result.error
                        else:
                            log.info("relay_retry_ok",
                                     rtt_ms=round(result.rtts_ms[0]) if result.rtts_ms else 0)
                            del dead[relay]
                            retryable.pop(relay, None)
            except FuturesTimeoutError:
                for future, relay in retry_futures.items():
                    if relay in retryable and relay in dead:
                        with structlog.contextvars.bound_contextvars(relay=relay):
                            log.warning("relay_retry_timeout",
                                        attempt=attempt, max_retries=max_retries)
        # Re-evaluate retryable with updated errors (new error string -> new cache entry)
        retryable = {r: dead[r] for r in retryable
                     if r in dead and _is_transient(r, dead[r])}

    # TURN check: run across just-confirmed-alive relays before the metric
    # update below so cmping_relay_turn_status is fresh on the same tick as
    # cmping_relay_status.  Needs a configured account, which only the
    # shared alive_pool provides; skipped in throwaway-context mode.
    if alive_pool is not None:
        _alive_now = [r for r in relays if r not in dead]
        _alive_now += [r for r in (unreachable_set or ()) if r not in dead]
        _run_turn_checks(alive_pool, _alive_now, args)
        _run_iroh_checks(alive_pool, _alive_now, args)

    # Build alive list: normal relays that passed + unreachable relays that recovered.
    alive = [r for r in relays if r not in dead]
    recovered_unreachable = [r for r in unreachable_set if r not in dead]
    if recovered_unreachable:
        log.warning("relay_recovered_from_unreachable",
                    count=len(recovered_unreachable),
                    relays=recovered_unreachable)
        alive = alive + recovered_unreachable
    recovered = set(previously_dead) - set(dead)
    if recovered:
        log.warning("relays_recovered", count=len(recovered), relays=list(recovered))
    if dead:
        log.warning("relays_unreachable", count=len(dead), relays=list(dead))
    # Update per-relay status metric; remove labels for relays dropped from config.
    # Include unreachable relays in the status metric so their recovery is visible.
    extra_turn_hosts = list(getattr(args, "extra_turn_map", {}).keys())
    relay_known = list(relays) + list(unreachable_set)
    clear_stale_relay_labels(relay_known + extra_turn_hosts)
    for r in relay_known:
        relay_status.labels(relay=r).set(_cached_status(r, dead.get(r)))
    elapsed = time.monotonic() - check_start
    log.warning("alive_check_complete",
                elapsed_s=round(elapsed, 1),
                online=len(alive), total=len(relay_known))
    return alive, dead


def run_round(
    relays: list[str],
    args: argparse.Namespace,
    executors: list[ThreadPoolExecutor],
    worker_pools: list[RelayPool],
    shutdown_event: threading.Event | None,
    textfile: str | None = None,
    exclude: set[tuple[str, str]] | None = None,
) -> tuple[float, list[ProbeResult]]:
    """Run one complete probe round across all relay pairs."""
    clear_stale_labels(relays)
    pairs = [(s, d) for s in relays for d in relays
             if not exclude or (s, d) not in exclude]
    log.info("probe_round_start", pairs=len(pairs), workers=args.workers)
    round_start = time.time()

    # Ensure all worker pools have contexts for all relays.
    for pool in worker_pools:
        pool.open_all(relays)

    worker_pairs: list[list[tuple[str, str]]] = [[] for _ in range(args.workers)]
    for i, pair in enumerate(pairs):
        worker_pairs[i % args.workers].append(pair)

    # Capture per-worker relay contexts so we can update them if a relay is reopened
    worker_relay_contexts: dict[int, dict[str, RelayPool]] = {}
    all_futures: dict[Future[ProbeResult], tuple[str, str, int]] = {}
    for worker_id, executor in enumerate(executors):
        relay_contexts = worker_pools[worker_id].contexts()
        worker_relay_contexts[worker_id] = relay_contexts
        for src, dst in worker_pairs[worker_id]:
            try:
                future = executor.submit(
                    run_probe, src, dst, args.count, args.ping_interval,
                    timeout=args.timeout,
                    relay_contexts=relay_contexts,
                )
            except RuntimeError:
                # Executor was shut down (e.g. by signal handler).
                break
            all_futures[future] = (src, dst, worker_id)

    completed = 0
    failed = 0
    round_results: list[ProbeResult] = []
    reopened_workers: set[int] = set()  # worker_ids whose shared rpc-server was restarted
    _reopen_limit = 3
    for future in as_completed(all_futures):
        if shutdown_event and shutdown_event.is_set():
            break
        completed += 1
        src, dst, worker_id = all_futures[future]
        with structlog.contextvars.bound_contextvars(
            src=src, dst=dst, worker_id=worker_id
        ):
            try:
                result = future.result()
            except Exception as exc:
                log.exception("worker_crash")
                result = ProbeResult(src, dst, error=str(exc))
            round_results.append(result)
            update_metrics(result)
            if completed % 50 == 0:
                gc.collect()
                if textfile:
                    write_textfile(textfile)
            if result.error:
                failed += 1
                log.warning(
                    "probe_failed",
                    n=completed, total=len(pairs), error=result.error,
                )
                if (_is_rpc_crash(result.error)
                        and worker_id not in reopened_workers
                        and len(reopened_workers) < _reopen_limit):
                    if _try_reopen_pool(
                        worker_pools[worker_id],
                        worker_relay_contexts[worker_id],
                        log_event="reopen_failed",
                        worker_id=worker_id,
                    ):
                        reopened_workers.add(worker_id)
            else:
                log.info(
                    "probe_ok",
                    n=completed, total=len(pairs),
                    sent=result.sent, recv=result.received,
                    avg_ms=round(statistics.fmean(result.rtts_ms)) if result.rtts_ms else 0,
                    loss_pct=result.loss,
                )
            if completed % 5 == 0:
                log.info("probe_round_progress",
                         completed=completed, total=len(pairs),
                         failed=failed)

    elapsed = time.time() - round_start
    last_round_timestamp.set(time.time())
    round_duration_seconds.set(elapsed)
    rounds_total.inc()
    success_count = completed - failed
    success_rate = 100.0 * success_count / completed if completed > 0 else 0.0
    avg_ms_per_pair = int(elapsed * 1000 / completed) if completed > 0 else 0
    sample_relay_connections(relays)
    log.warning("round_complete",
                success_count=success_count, total=completed,
                success_rate_pct=round(success_rate, 1),
                avg_ms_per_pair=avg_ms_per_pair,
                elapsed_s=round(elapsed, 1))
    return elapsed, round_results


def scan_relays(relays: list[str], args: argparse.Namespace, cache_dir: Path) -> None:
    """Self-probe all relays in parallel, print ranked by avg RTT, then exit."""
    log.info("Scanning %d relays...", len(relays))

    scan_pool = RelayPool(cache_dir / "scan")
    try:
        scan_pool.open_all(relays)
        relay_contexts = scan_pool.contexts()
        results: dict[str, ProbeResult] = {}
        with ThreadPoolExecutor(max_workers=min(len(relays), args.workers)) as pool:
            futures = {
                pool.submit(run_probe, r, r, args.count, args.ping_interval,
                            timeout=args.timeout,
                            relay_contexts=relay_contexts): r
                for r in relays
            }
            for future in as_completed(futures):
                relay = futures[future]
                results[relay] = future.result()
                if results[relay].error:
                    log.info("DEAD %s: %s", relay, results[relay].error)
                else:
                    log.info("OK   %s (%.0fms)", relay, statistics.fmean(results[relay].rtts_ms))
    finally:
        scan_pool.close()

    ranked = sorted(
        relays,
        key=lambda r: statistics.fmean(results[r].rtts_ms) if results[r].rtts_ms else float("inf"),
    )

    print("\nScan results (fastest first):\n")
    print(f"  {'Rank':<5} {'Relay':<40} {'Avg RTT':>10} {'Loss':>8} {'Samples':>8}")
    print(f"  {'-'*5} {'-'*40} {'-'*10} {'-'*8} {'-'*8}")
    for i, relay in enumerate(ranked, 1):
        r = results[relay]
        if r.error:
            print(f"  {i:<5} {relay:<40} {'DEAD':>10} {'':>8} {'':>8}")
        else:
            marker = " <--" if i <= args.top else ""
            print(f"  {i:<5} {relay:<40} {statistics.fmean(r.rtts_ms):>9.0f}ms {r.loss:>7.1f}% {len(r.rtts_ms):>8}{marker}")
    print()
