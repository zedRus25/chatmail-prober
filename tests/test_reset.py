"""Tests for --reset behaviour.

Cache layout under test (legacy per-relay shape, used to populate the dir;
the new code only cares about top-level pool dirs):
  cache_dir/
    worker-0/...
    worker-1/...
    alive-check/...

--reset all       : wipe all worker-* dirs, leave alive-check untouched
--reset DOMAIN... : not supported under the flat layout, raises SystemExit
"""
from __future__ import annotations

from pathlib import Path

import pytest

from chatmail_prober.__main__ import parse_args, reset_accounts

#
# Helpers
#

def _make_cache(tmp_path: Path, workers: int = 2,
                domains: tuple[str, ...] = ("a.example", "b.example")) -> Path:
    """Create a fake cache directory tree."""
    cache = tmp_path / "cache"
    for i in range(workers):
        for d in domains:
            (cache / f"worker-{i}" / d).mkdir(parents=True)
    for d in domains:
        (cache / "alive-check" / d).mkdir(parents=True)
    return cache


#
# parse_args: --reset flag shape
#

class TestResetArgParsing:
    def test_reset_all_keyword(self, tmp_path):
        relay_file = tmp_path / "r.txt"
        relay_file.write_text("a.example\n")
        args = parse_args([str(relay_file), "--reset", "all"])
        assert args.reset == ["all"]

    def test_reset_with_domains(self, tmp_path):
        relay_file = tmp_path / "r.txt"
        relay_file.write_text("a.example\n")
        args = parse_args([str(relay_file), "--reset", "a.example", "b.example"])
        assert args.reset == ["a.example", "b.example"]

    def test_reset_default_is_none(self, tmp_path):
        relay_file = tmp_path / "r.txt"
        relay_file.write_text("a.example\n")
        args = parse_args([str(relay_file)])
        assert args.reset is None

    def test_reset_bare_raises_system_exit(self, tmp_path):
        """--reset with no args must exit with an error message."""
        relay_file = tmp_path / "r.txt"
        relay_file.write_text("a.example\n")
        with pytest.raises(SystemExit) as exc_info:
            parse_args([str(relay_file), "--reset"])
        assert exc_info.value.code != 0


#
# reset_accounts() function
#

class TestResetAccountsFunction:
    def test_full_reset_removes_worker_dirs_preserves_alive_check(self, tmp_path):
        cache = _make_cache(tmp_path)
        reset_accounts(cache, domains=["all"])
        assert not (cache / "worker-0").exists()
        assert not (cache / "worker-1").exists()
        assert (cache / "alive-check").exists()

    def test_selective_reset_raises_systemexit(self, tmp_path):
        """Selective per-domain reset is not supported under the flat layout."""
        cache = _make_cache(tmp_path)
        with pytest.raises(SystemExit) as exc_info:
            reset_accounts(cache, domains=["a.example"])
        # Message should point users at cleanup_accounts.py
        assert "cleanup_accounts" in str(exc_info.value)

    def test_full_reset_empty_cache_is_noop(self, tmp_path):
        cache = tmp_path / "empty-cache"
        cache.mkdir()
        reset_accounts(cache, domains=["all"])  # must not raise
