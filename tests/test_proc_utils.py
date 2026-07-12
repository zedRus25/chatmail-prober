"""Tests for chatmail_prober.proc_utils.rpc_server_pids.

The rpc client passes the accounts dir via the DC_ACCOUNTS_PATH env var, so
process discovery reads /proc/<pid>/environ rather than argv.  These tests
build a fake /proc tree so they run on any platform.
"""

from __future__ import annotations

import pytest

from chatmail_prober import proc_utils
from chatmail_prober.proc_utils import _accounts_path_under, rpc_server_pids


def _environ(**vars: str) -> bytes:
    """Encode an env mapping the way /proc/<pid>/environ stores it (NUL-separated)."""
    return b"".join(f"{k}={v}".encode() + b"\0" for k, v in vars.items())


class TestAccountsPathUnder:
    def test_exact_match(self):
        env = _environ(DC_ACCOUNTS_PATH="/cache/worker-0")
        assert _accounts_path_under(env, "/cache/worker-0") is True

    def test_subdir_match(self):
        # Pool dirs are subdirs of the cache dir passed to kill_stale_rpc_servers.
        env = _environ(DC_ACCOUNTS_PATH="/cache/worker-0")
        assert _accounts_path_under(env, "/cache") is True

    def test_prefix_is_not_a_subdir(self):
        # /cache-other must not match cache dir /cache (no false sibling match).
        env = _environ(DC_ACCOUNTS_PATH="/cache-other/worker-0")
        assert _accounts_path_under(env, "/cache") is False

    def test_env_present_but_elsewhere(self):
        env = _environ(DC_ACCOUNTS_PATH="/somewhere/else")
        assert _accounts_path_under(env, "/cache") is False

    def test_no_accounts_env(self):
        env = _environ(PATH="/usr/bin", HOME="/root")
        assert _accounts_path_under(env, "/cache") is False


class TestRpcServerPids:
    def _fake_proc(self, tmp_path, pid: int, comm: str, environ: bytes) -> None:
        d = tmp_path / str(pid)
        d.mkdir()
        (d / "comm").write_text(comm + "\n")
        (d / "environ").write_bytes(environ)

    @pytest.fixture()
    def fake_proc_root(self, tmp_path, monkeypatch):
        # Redirect the /proc scan at the Path("/proc") call site.
        real_path = proc_utils.Path

        def fake_path(arg):
            if arg == "/proc":
                return real_path(tmp_path)
            return real_path(arg)

        monkeypatch.setattr(proc_utils, "Path", fake_path)
        return tmp_path

    def test_matches_only_our_server(self, fake_proc_root):
        self._fake_proc(fake_proc_root, 100, "deltachat-rpc-s",
                        _environ(DC_ACCOUNTS_PATH="/cache/worker-0"))
        # Right binary, different cache dir -> excluded.
        self._fake_proc(fake_proc_root, 101, "deltachat-rpc-s",
                        _environ(DC_ACCOUNTS_PATH="/other/worker-0"))
        # Our cache dir but a different (non-rpc) process -> excluded by comm.
        self._fake_proc(fake_proc_root, 102, "python3",
                        _environ(DC_ACCOUNTS_PATH="/cache/worker-1"))
        # A non-numeric /proc entry (e.g. "self") must be skipped, not crash.
        (fake_proc_root / "self").mkdir()

        assert rpc_server_pids("/cache") == [100]

    def test_no_proc_returns_empty(self, monkeypatch):
        def boom(_arg):
            raise FileNotFoundError("no /proc here")

        monkeypatch.setattr(proc_utils, "Path", boom)
        assert rpc_server_pids("/cache") == []

    def test_unreadable_environ_is_skipped(self, fake_proc_root):
        d = fake_proc_root / "200"
        d.mkdir()
        (d / "comm").write_text("deltachat-rpc-s\n")
        # environ is a directory, so read_bytes() raises OSError -> skipped.
        (d / "environ").mkdir()
        assert rpc_server_pids("/cache") == []
