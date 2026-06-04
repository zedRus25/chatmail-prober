"""Tests for config parsing, CLI args, pair generation, and orchestration."""

import argparse
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import pytest

from chatmail_prober import metrics as metrics_mod
from chatmail_prober.__main__ import (
    main,
    parse_args,
)
from chatmail_prober.orchestration import (
    check_relays_alive,
    run_round,
)
from chatmail_prober.output import print_metrics
from chatmail_prober.probe import ProbeResult


class TestParseArgs:
    def test_defaults(self):
        args = parse_args(["relays.txt"])
        assert args.relays == ["relays.txt"]
        assert args.port == 0
        assert args.textfile is None
        assert args.interval == 900
        assert args.count == 5
        assert args.ping_interval == 0.1
        assert args.timeout == 90
        assert args.workers == 5
        assert args.once is False
        assert args.verbose == 0
        assert args.quiet is False

    def test_quiet_flag(self):
        args = parse_args(["r.txt", "-q"])
        assert args.quiet is True
        assert args.verbose == 0

    def test_all_flags(self):
        args = parse_args([
            "r.txt",
            "--port", "0",
            "--textfile", "/tmp/out.prom",
            "--interval", "60",
            "--count", "5",
            "--ping-interval", "0.5",
            "--timeout", "30",
            "--workers", "10",
            "--cache-dir", "/tmp/cache",
            "--once",
            "-vv",
        ])
        assert args.port == 0
        assert args.textfile == "/tmp/out.prom"
        assert args.interval == 60
        assert args.count == 5
        assert args.ping_interval == 0.5
        assert args.timeout == 30
        assert args.workers == 10
        assert args.cache_dir == "/tmp/cache"
        assert args.once is True
        assert args.verbose == 2


# -- Orchestration tests (run_round, check_relays_alive) --


def _make_args(tmp_path, workers=2):
    return argparse.Namespace(
        count=1, ping_interval=0.1, timeout=10, workers=workers,
        cache_dir=str(tmp_path / "cache"), verbose=0,
    )



def _fake_probe(source, dest, count=1, interval=0.1, accounts_dir="", timeout=10, relay_contexts=None):
    return ProbeResult(source, dest, sent=1, received=1, loss=0.0, rtts_ms=[100.0])


class _FakePool:
    """Stand-in for RelayPool that does not spawn RPC servers."""
    def __init__(self, *a, **kw):
        self.reopen_calls = 0

    def open_all(self, relays):
        pass

    def contexts(self):
        return {}

    def close(self):
        pass

    def reopen(self):
        self.reopen_calls += 1


def _make_worker_pools(n):
    """Create n fake worker pools for testing."""
    return [_FakePool() for _ in range(n)]


class TestRunRound:
    def test_completes_all_pairs(self, tmp_path, monkeypatch, fresh_metrics):
        monkeypatch.setattr("chatmail_prober.orchestration.run_probe", _fake_probe)
        relays = ["a.example", "b.example", "c.example"]
        args = _make_args(tmp_path, workers=2)
        executors = [ThreadPoolExecutor(max_workers=1) for _ in range(args.workers)]
        try:
            run_round(relays, args, executors, _make_worker_pools(args.workers), shutdown_event=threading.Event())
        finally:
            for ex in executors:
                ex.shutdown(wait=False)

        # All 9 pairs should have probe_success=1
        for s in relays:
            for d in relays:
                pt = "self" if s == d else "cross"
                val = metrics_mod.probe_success.labels(
                    source=s, destination=d, probe_type=pt)._value.get()
                assert val == 1.0, f"{s} -> {d} not recorded"

    def test_shutdown_observable(self, tmp_path, monkeypatch, fresh_metrics):
        """Loose smoke test: shutdown causes some pairs to be skipped.

        Kept alongside the strict version below as a wider safety net --
        it tolerates timing shifts from upstream rpc-server upgrades that
        might invalidate the precise sleep tuning in the strict test, while
        still catching the catastrophic case where shutdown is ignored
        entirely.
        """
        shutdown_event = threading.Event()
        call_count = 0

        def _slow_probe(source, dest, count=1, interval=0.1,
                        accounts_dir="", timeout=10, relay_contexts=None):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                time.sleep(0.05)
                shutdown_event.set()
            return ProbeResult(source, dest, sent=1, received=1,
                               loss=0.0, rtts_ms=[100.0])

        monkeypatch.setattr(
            "chatmail_prober.orchestration.run_probe", _slow_probe,
        )
        relays = ["a.example", "b.example", "c.example"]
        args = _make_args(tmp_path, workers=1)
        executors = [ThreadPoolExecutor(max_workers=1) for _ in range(args.workers)]
        try:
            run_round(relays, args, executors, _make_worker_pools(args.workers),
                      shutdown_event=shutdown_event)
        finally:
            for ex in executors:
                ex.shutdown(wait=False)

        recorded = sum(
            metrics_mod.probe_success.labels(
                source=s, destination=d,
                probe_type="self" if s == d else "cross",
            )._value.get() != 0.0
            for s in relays for d in relays
        )
        assert recorded < len(relays) ** 2

    def test_shutdown_skips_metrics(self, tmp_path, monkeypatch, fresh_metrics):
        """Strict version: shutdown must break out of as_completed early.

        The slow probes (0.5s each) force the as_completed loop to observe
        shutdown before the queue drains. Without the early-exit, all 9
        pairs would eventually record. The recorded >= 1 lower bound
        confirms the first probe still landed (so we are testing
        shutdown-mid-loop, not shutdown-before-start).
        """
        shutdown_event = threading.Event()
        probe_calls = 0
        first_call = threading.Event()

        def _slow_probe(source, dest, count=1, interval=0.1,
                        accounts_dir="", timeout=10, relay_contexts=None):
            nonlocal probe_calls
            probe_calls += 1
            first_call.set()
            # All probes after the first sleep long enough that shutdown
            # will be observed by the as_completed loop before they finish.
            if probe_calls > 1:
                time.sleep(0.5)
            return ProbeResult(source, dest, sent=1, received=1,
                               loss=0.0, rtts_ms=[100.0])

        monkeypatch.setattr(
            "chatmail_prober.orchestration.run_probe", _slow_probe,
        )

        # Trip shutdown shortly after the first probe completes.
        def _fire_shutdown():
            first_call.wait(timeout=2.0)
            time.sleep(0.05)
            shutdown_event.set()

        firer = threading.Thread(target=_fire_shutdown, daemon=True)
        firer.start()

        relays = ["a.example", "b.example", "c.example"]
        args = _make_args(tmp_path, workers=1)
        executors = [ThreadPoolExecutor(max_workers=1) for _ in range(args.workers)]
        try:
            run_round(relays, args, executors, _make_worker_pools(args.workers),
                      shutdown_event=shutdown_event)
        finally:
            for ex in executors:
                ex.shutdown(wait=False)
            firer.join(timeout=2.0)

        recorded = sum(
            metrics_mod.probe_success.labels(
                source=s, destination=d,
                probe_type="self" if s == d else "cross",
            )._value.get() != 0.0
            for s in relays for d in relays
        )
        # Strict invariant: the shutdown must have caused us to leave the
        # as_completed loop with futures still incomplete -- i.e. recorded
        # pairs strictly less than total pairs. Without the early-exit
        # the loop would sit and wait for every slow probe.
        assert recorded < len(relays) ** 2, (
            f"shutdown was ignored: recorded all {recorded} pairs"
        )
        assert recorded >= 1, "expected the first probe to land before shutdown"

    def test_rpc_crash_triggers_pool_reopen(self, tmp_path, monkeypatch, fresh_metrics):
        """A probe error containing an _RPC_CRASH_KEYWORDS substring must
        trigger pool.reopen() exactly once for the affected worker, gated by
        the per-worker reopened_workers set."""
        def _broken_pipe_probe(source, dest, count=1, interval=0.1,
                               accounts_dir="", timeout=10, relay_contexts=None):
            return ProbeResult(
                source, dest,
                error="BrokenPipeError writing to rpc stdin",
            )

        monkeypatch.setattr(
            "chatmail_prober.orchestration.run_probe", _broken_pipe_probe,
        )
        relays = ["a.example", "b.example"]
        args = _make_args(tmp_path, workers=1)  # single worker -> single pool
        pools = _make_worker_pools(args.workers)
        executors = [ThreadPoolExecutor(max_workers=1) for _ in range(args.workers)]
        try:
            run_round(relays, args, executors, pools,
                      shutdown_event=threading.Event())
        finally:
            for ex in executors:
                ex.shutdown(wait=False)

        # Even though all 4 pairs failed with the same crash signature, the
        # per-worker gate means exactly one reopen is performed.
        assert pools[0].reopen_calls == 1, (
            f"Expected 1 reopen via gating, got {pools[0].reopen_calls}"
        )

    def test_app_error_does_not_trigger_reopen(self, tmp_path, monkeypatch, fresh_metrics):
        """An application-level failure (DNS / auth / timeout) must NOT trigger
        pool.reopen() -- only transport-level crashes warrant tearing down the
        shared rpc-server."""
        def _dns_probe(source, dest, count=1, interval=0.1,
                       accounts_dir="", timeout=10, relay_contexts=None):
            return ProbeResult(
                source, dest,
                error="Could not find DNS resolutions for imap.a.example:993",
            )

        monkeypatch.setattr(
            "chatmail_prober.orchestration.run_probe", _dns_probe,
        )
        relays = ["a.example", "b.example"]
        args = _make_args(tmp_path, workers=1)
        pools = _make_worker_pools(args.workers)
        executors = [ThreadPoolExecutor(max_workers=1) for _ in range(args.workers)]
        try:
            run_round(relays, args, executors, pools,
                      shutdown_event=threading.Event())
        finally:
            for ex in executors:
                ex.shutdown(wait=False)

        assert pools[0].reopen_calls == 0, (
            "DNS errors must not tear down the shared rpc-server"
        )

    def test_crashed_probe_records_error(self, tmp_path, monkeypatch, fresh_metrics):
        def _crashing_probe(source, dest, count=1, interval=0.1, accounts_dir="", timeout=10, relay_contexts=None):
            if source == "a.example" and dest == "b.example":
                raise RuntimeError("boom")
            return ProbeResult(source, dest, sent=1, received=1, loss=0.0, rtts_ms=[100.0])

        monkeypatch.setattr("chatmail_prober.orchestration.run_probe", _crashing_probe)
        relays = ["a.example", "b.example"]
        args = _make_args(tmp_path, workers=2)
        executors = [ThreadPoolExecutor(max_workers=1) for _ in range(args.workers)]
        try:
            run_round(relays, args, executors, _make_worker_pools(args.workers), shutdown_event=threading.Event())
        finally:
            for ex in executors:
                ex.shutdown(wait=False)

        # The crashed pair should have an error recorded.
        lbl = dict(source="a.example", destination="b.example", probe_type="cross")
        assert metrics_mod.send_errors_total.labels(**lbl)._value.get() == 1.0
        assert metrics_mod.probe_success.labels(**lbl)._value.get() == 0.0

        # The other pairs should still succeed.
        lbl_ok = dict(source="b.example", destination="a.example", probe_type="cross")
        assert metrics_mod.probe_success.labels(**lbl_ok)._value.get() == 1.0


class TestCheckRelaysAlive:
    def test_filters_dead_relays(self, tmp_path, monkeypatch, fresh_metrics):
        def _selective_probe(source, dest, count=1, interval=0.1, accounts_dir="", timeout=10, relay_contexts=None):
            if source == "dead.example":
                return ProbeResult(source, dest, error="connection refused")
            return ProbeResult(source, dest, sent=1, received=1, loss=0.0, rtts_ms=[100.0])

        monkeypatch.setattr("chatmail_prober.orchestration.run_probe", _selective_probe)
        relays = ["a.example", "dead.example", "b.example"]
        args = _make_args(tmp_path, workers=3)
        alive, dead_set = check_relays_alive(relays, args, Path(args.cache_dir))

        assert alive == ["a.example", "b.example"]
        assert "dead.example" not in alive
        assert set(dead_set) == {"dead.example"}

    def test_all_alive(self, tmp_path, monkeypatch, fresh_metrics):
        monkeypatch.setattr("chatmail_prober.orchestration.run_probe", _fake_probe)
        relays = ["a.example", "b.example", "c.example"]
        args = _make_args(tmp_path, workers=3)
        alive, dead_set = check_relays_alive(relays, args, Path(args.cache_dir))

        assert alive == relays
        assert dead_set == {}

    def test_retries_transient_errors(self, tmp_path, monkeypatch, fresh_metrics):
        """Relays with transient errors (timeout) are retried and can recover."""
        call_count = {}

        def _flaky_probe(source, dest, count=1, interval=0.1,
                         accounts_dir="", timeout=10, relay_contexts=None):
            n = call_count.get(source, 0) + 1
            call_count[source] = n
            if source == "flaky.example" and n <= 1:
                return ProbeResult(source, dest, error="timeout")
            return ProbeResult(source, dest, sent=1, received=1,
                               loss=0.0, rtts_ms=[100.0])

        monkeypatch.setattr("chatmail_prober.orchestration.run_probe", _flaky_probe)
        monkeypatch.setattr("chatmail_prober.orchestration.time.sleep", lambda _: None)
        relays = ["a.example", "flaky.example", "b.example"]
        args = _make_args(tmp_path, workers=3)
        alive, dead_set = check_relays_alive(relays, args, Path(args.cache_dir))

        assert "flaky.example" in alive
        assert "flaky.example" not in dead_set
        assert call_count["flaky.example"] == 2  # initial + 1 retry

    def test_no_retry_for_persistent_errors(self, tmp_path, monkeypatch, fresh_metrics):
        """Relays with persistent errors (auth, connection refused) are not retried."""
        call_count = {}

        def _failing_probe(source, dest, count=1, interval=0.1,
                           accounts_dir="", timeout=10, relay_contexts=None):
            call_count[source] = call_count.get(source, 0) + 1
            if source == "auth.example":
                return ProbeResult(source, dest, error="AUTHENTICATIONFAILED")
            if source == "refused.example":
                return ProbeResult(source, dest, error="connection refused")
            return ProbeResult(source, dest, sent=1, received=1,
                               loss=0.0, rtts_ms=[100.0])

        monkeypatch.setattr("chatmail_prober.orchestration.run_probe", _failing_probe)
        monkeypatch.setattr("chatmail_prober.orchestration.time.sleep", lambda _: None)
        relays = ["a.example", "auth.example", "refused.example"]
        args = _make_args(tmp_path, workers=3)
        alive, dead_set = check_relays_alive(relays, args, Path(args.cache_dir))

        assert "auth.example" not in alive
        assert "refused.example" not in alive
        assert set(dead_set) == {"auth.example", "refused.example"}
        assert call_count["auth.example"] == 1
        assert call_count["refused.example"] == 1

    def test_retry_gives_up_after_max_retries(self, tmp_path, monkeypatch, fresh_metrics):
        """Relays that keep timing out are excluded after max retries."""
        call_count = {}

        def _always_timeout(source, dest, count=1, interval=0.1,
                            accounts_dir="", timeout=10, relay_contexts=None):
            call_count[source] = call_count.get(source, 0) + 1
            if source == "slow.example":
                return ProbeResult(source, dest, error="timeout")
            return ProbeResult(source, dest, sent=1, received=1,
                               loss=0.0, rtts_ms=[100.0])

        monkeypatch.setattr("chatmail_prober.orchestration.run_probe", _always_timeout)
        monkeypatch.setattr("chatmail_prober.orchestration.time.sleep", lambda _: None)
        relays = ["a.example", "slow.example"]
        args = _make_args(tmp_path, workers=3)
        alive, dead_set = check_relays_alive(relays, args, Path(args.cache_dir))

        assert "slow.example" not in alive
        assert "slow.example" in dead_set
        assert call_count["slow.example"] == 3  # initial + 2 retries

    def test_no_retry_when_all_alive(self, tmp_path, monkeypatch, fresh_metrics):
        """No retry logic triggered when all relays pass first time."""
        monkeypatch.setattr("chatmail_prober.orchestration.run_probe", _fake_probe)
        relays = ["a.example", "b.example"]
        args = _make_args(tmp_path, workers=3)
        alive, dead_set = check_relays_alive(relays, args, Path(args.cache_dir))

        assert alive == relays
        assert dead_set == {}

    def test_previously_dead_skips_retry(self, tmp_path, monkeypatch, fresh_metrics):
        """Relays in previously_dead are not retried even if transient."""
        call_count = {}

        def _always_timeout(source, dest, count=1, interval=0.1,
                            accounts_dir="", timeout=10, relay_contexts=None):
            call_count[source] = call_count.get(source, 0) + 1
            if source == "known-dead.example":
                return ProbeResult(source, dest, error="timeout")
            return ProbeResult(source, dest, sent=1, received=1,
                               loss=0.0, rtts_ms=[100.0])

        monkeypatch.setattr("chatmail_prober.orchestration.run_probe", _always_timeout)
        monkeypatch.setattr("chatmail_prober.orchestration.time.sleep", lambda _: None)
        relays = ["a.example", "known-dead.example"]
        args = _make_args(tmp_path, workers=3)
        alive, dead_set = check_relays_alive(
            relays, args, Path(args.cache_dir), previously_dead={"known-dead.example": "timeout"})

        assert "known-dead.example" not in alive
        assert "known-dead.example" in dead_set  # dict membership check works on keys
        assert call_count["known-dead.example"] == 1  # initial only, no retries

    def test_previously_dead_recovery_detected(self, tmp_path, monkeypatch, fresh_metrics):
        """A previously-dead relay that now succeeds is included."""
        monkeypatch.setattr("chatmail_prober.orchestration.run_probe", _fake_probe)
        relays = ["a.example", "recovered.example"]
        args = _make_args(tmp_path, workers=3)
        alive, dead_set = check_relays_alive(
            relays, args, Path(args.cache_dir), previously_dead={"recovered.example": "timeout"})

        assert "recovered.example" in alive
        assert dead_set == {}


class TestRunRoundExclude:
    def test_excludes_pairs(self, tmp_path, monkeypatch, fresh_metrics):
        probed_pairs = []

        def _tracking_probe(source, dest, count=1, interval=0.1, accounts_dir="", timeout=10, relay_contexts=None):
            probed_pairs.append((source, dest))
            return ProbeResult(source, dest, sent=1, received=1, loss=0.0, rtts_ms=[100.0])

        monkeypatch.setattr("chatmail_prober.orchestration.run_probe", _tracking_probe)
        relays = ["a.example", "b.example"]
        args = _make_args(tmp_path, workers=2)
        executors = [ThreadPoolExecutor(max_workers=1) for _ in range(args.workers)]
        exclude = {("a.example", "b.example")}
        try:
            run_round(relays, args, executors, _make_worker_pools(args.workers), shutdown_event=threading.Event(),
                      exclude=exclude)
        finally:
            for ex in executors:
                ex.shutdown(wait=False)

        # 4 pairs minus 1 excluded = 3
        assert len(probed_pairs) == 3
        assert ("a.example", "b.example") not in probed_pairs



# -- Tests merged from test_print_flag.py, test_print_metrics.py,
#    test_optional_relay_file.py --


def _run_main_once(tmp_path, extra_flags=()):
    relay_file = tmp_path / "relays.txt"
    relay_file.write_text("a.example\n")
    argv = [str(relay_file), "--once"] + list(extra_flags)
    with patch("chatmail_prober.__main__.check_relays_alive",
               return_value=(["a.example"], set())), \
         patch("chatmail_prober.__main__.run_round",
               return_value=(0.1, [])), \
         patch("chatmail_prober.__main__.render_summary") as mock_render, \
         patch("chatmail_prober.__main__.print_metrics") as mock_pm, \
         patch("chatmail_prober.__main__.write_textfile"):
        main(argv)
    return mock_render, mock_pm


class TestPrintFlag:
    """--print / --print-metrics dispatch in main(). Mocks everything else."""

    @pytest.mark.parametrize("flags,render_called,pm_called", [
        ([],                   False, False),
        (["--print"],          True,  False),
        (["--print-metrics"],  False, True),
    ])
    def test_dispatch(self, tmp_path, flags, render_called, pm_called):
        mock_render, mock_pm = _run_main_once(tmp_path, extra_flags=flags)
        assert mock_render.called is render_called
        assert mock_pm.called is pm_called


class TestPrintMetrics:
    def test_writes_to_stdout(self, capsys):
        print_metrics()
        assert len(capsys.readouterr().out) > 0


class TestOptionalRelayFile:
    def test_hosts_flag_needs_no_relay_file(self):
        args = parse_args(["-H", "a.example"])
        assert args.hosts == "a.example"
        assert args.relays == []

    def test_reset_needs_no_relay_file(self):
        args = parse_args(["--reset", "all"])
        assert args.reset == ["all"]

    def test_no_relay_source_errors(self, tmp_path):
        with pytest.raises(SystemExit):
            main(["--cache-dir", str(tmp_path / "cache")])

    def test_reset_without_relay_file_succeeds(self, tmp_path):
        cache = tmp_path / "cache"
        cache.mkdir()
        with patch("chatmail_prober.__main__.reset_accounts"):
            with pytest.raises(SystemExit) as exc_info:
                main(["--reset", "all", "--cache-dir", str(cache)])
        assert exc_info.value.code in (0, None)

    def test_hosts_without_relay_file_proceeds(self, tmp_path):
        cache = tmp_path / "cache"
        cache.mkdir()
        with patch("chatmail_prober.__main__.check_relays_alive",
                   return_value=(["a.example"], set())), \
             patch("chatmail_prober.__main__.run_round", return_value=(0.1, [])), \
             patch("chatmail_prober.__main__.render_summary"), \
             patch("chatmail_prober.__main__.write_textfile"), \
             patch("chatmail_prober.__main__.print_metrics"):
            main(["-H", "a.example", "--once",
                  "--cache-dir", str(cache)])


# -- Tests merged from test_main_orchestration.py --
# These test cross-module integration (orchestration + metrics) and need
# to clear metrics in-place rather than replacing them, because
# orchestration.py holds direct import bindings to the real gauge objects.


def _orch_args(tmp_path, *, workers=3, timeout=90):
    return argparse.Namespace(
        cache_dir=str(tmp_path), workers=workers, timeout=timeout,
        count=1, ping_interval=0.1, interval=900, once=True, verbose=0, exclude=[],
    )


def _ok(src, dst):
    return ProbeResult(src, dst, sent=1, received=1, loss=0.0, rtts_ms=[50.0])


def _err(src, dst, error):
    return ProbeResult(src, dst, error=error)


@pytest.fixture()
def clear_metrics():
    """Clear metric label sets in-place for integration tests."""
    for metric in [
        metrics_mod.rtt_median, metrics_mod.rtt_stddev,
        metrics_mod.rtt_p90, metrics_mod.rtt_p10,
        metrics_mod.probe_success, metrics_mod.probe_loss_ratio,
        metrics_mod.account_setup_seconds, metrics_mod.send_errors_total,
        metrics_mod.relay_status,
    ]:
        metric._metrics.clear()
    yield


_ALIVE_CHECK_CASES = [
    (
        "dns.example",
        "Failed to setup sender profile on dns.example: JsonRpcError: "
        "{'code': -1, 'message': 'Error: IMAP failed to connect to "
        "imap.dns.example:993:tls: Could not find DNS resolutions for "
        "imap.dns.example:993. Check server hostname and your network'}",
        -6.0,
    ),
    (
        "auth.example",
        "Failed to setup sender profile on auth.example: JsonRpcError: "
        "{'code': -1, 'message': 'Error: Cannot login as "
        '"user@auth.example". [AUTHENTICATIONFAILED] Authentication failed.\'}',
        -3.0,
    ),
    (
        "timeout.example",
        "Failed to setup sender profile on timeout.example: JsonRpcError: "
        "{'code': -1, 'message': 'Error: IMAP failed to connect to "
        "timeout.example:993:tls: Connection timeout: deadline has elapsed'}",
        -1.0,
    ),
]


class TestAliveCheckMetrics:
    @pytest.mark.parametrize(("relay", "error_fragment", "status"), _ALIVE_CHECK_CASES)
    def test_failure_sets_relay_status(self, relay, error_fragment, status,
                                      tmp_path, monkeypatch, clear_metrics):
        def _probe(src, dst, count=1, interval=0.1, accounts_dir="",
                   timeout=10, relay_contexts=None):
            if src == relay:
                return _err(src, dst, error_fragment)
            return _ok(src, dst)

        monkeypatch.setattr("chatmail_prober.orchestration.run_probe", _probe)
        args = _orch_args(tmp_path, workers=2)
        alive, _ = check_relays_alive([relay, "host.good"], args, Path(args.cache_dir))

        assert relay not in alive
        assert metrics_mod.relay_status.labels(relay=relay)._value.get() == status

    def test_online_relay_sets_status_one(self, tmp_path, monkeypatch, clear_metrics):
        monkeypatch.setattr("chatmail_prober.orchestration.run_probe",
                            lambda *a, **kw: _ok(a[0], a[1]))
        args = _orch_args(tmp_path, workers=1)
        alive, dead = check_relays_alive(["host.good"], args, Path(args.cache_dir))

        assert alive == ["host.good"]
        assert metrics_mod.relay_status.labels(relay="host.good")._value.get() == 1.0


class TestReopenGuard:
    """Application-level errors must not trigger pool.reopen() in run_round."""

    def _run_with_tracking_pool(self, tmp_path, monkeypatch, error_relay, error_msg):
        reopen_calls = []

        class _TrackingPool:
            def open_all(self, relays): pass
            def contexts(self): return {}
            def reopen(self, relay): reopen_calls.append(relay)
            def close(self): pass

        def _probe(src, dst, count=1, interval=0.1, accounts_dir="",
                   timeout=10, relay_contexts=None):
            if src == error_relay:
                return _err(src, dst, error_msg)
            return _ok(src, dst)

        monkeypatch.setattr("chatmail_prober.orchestration.run_probe", _probe)
        executor = ThreadPoolExecutor(max_workers=1)
        try:
            run_round(
                [error_relay, "host.good"],
                _orch_args(tmp_path, workers=1),
                executors=[executor],
                worker_pools=[_TrackingPool()],
                shutdown_event=threading.Event(),
            )
        finally:
            executor.shutdown(wait=False)
        return reopen_calls

    def test_dns_error_does_not_reopen(self, tmp_path, monkeypatch, clear_metrics):
        calls = self._run_with_tracking_pool(
            tmp_path, monkeypatch, "dns.example",
            "Failed to setup: Could not find DNS resolutions for imap.dns.example:993",
        )
        assert calls == []

    def test_timeout_does_not_reopen(self, tmp_path, monkeypatch, clear_metrics):
        calls = self._run_with_tracking_pool(
            tmp_path, monkeypatch, "relay.example",
            "Timeout waiting for user@relay.example to come online",
        )
        assert calls == []


class TestRunRoundMetrics:
    def test_mixed_round_updates_metrics(self, tmp_path, monkeypatch, clear_metrics):
        def _probe(src, dst, count=1, interval=0.1, accounts_dir="",
                   timeout=10, relay_contexts=None):
            if src == "bad.example":
                return _err(src, dst, "connection refused")
            return _ok(src, dst)

        monkeypatch.setattr("chatmail_prober.orchestration.run_probe", _probe)

        class _FakePool:
            def open_all(self, relays): pass
            def contexts(self): return {}
            def reopen(self, relay): pass
            def close(self): pass

        executors = [ThreadPoolExecutor(max_workers=1), ThreadPoolExecutor(max_workers=1)]
        try:
            run_round(
                ["good.example", "bad.example"],
                _orch_args(tmp_path, workers=2),
                executors=executors,
                worker_pools=[_FakePool(), _FakePool()],
                shutdown_event=threading.Event(),
            )
        finally:
            for ex in executors:
                ex.shutdown(wait=False)

        good_lbl = dict(source="good.example", destination="good.example", probe_type="self")
        assert metrics_mod.probe_success.labels(**good_lbl)._value.get() == 1.0

        bad_lbl = dict(source="bad.example", destination="bad.example", probe_type="self")
        assert metrics_mod.probe_success.labels(**bad_lbl)._value.get() == 0.0
