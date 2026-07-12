#!/usr/bin/env python3
"""migrate_accounts.py -- migrate from per-relay to per-worker account layout.

Old layout: worker-N/relay.domain/accounts.toml  (one rpc-server per relay)
New layout: worker-N/accounts.toml                (one rpc-server per worker)

Merges all per-relay accounts.toml files into a single per-worker file,
moves UUID data directories up one level, and removes empty relay subdirs.

Usage:
    # Dry-run (default): show what would change
    uv run python scripts/migrate_accounts.py /var/lib/chatmail-prober

    # Apply the migration (service must be stopped)
    uv run python scripts/migrate_accounts.py /var/lib/chatmail-prober --apply

Exit codes:
    0  nothing to migrate (or migration applied successfully)
    1  migration needed (dry-run) or errors during migration
    2  refused to run (RPC servers still running)
"""

from __future__ import annotations

import argparse
import shutil
import sys

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore[no-redef]
from pathlib import Path

from chatmail_prober.proc_utils import rpc_server_pids


def check_rpc_servers(cache_dir: Path) -> list[int]:
    """Return PIDs of deltachat-rpc-server processes using this cache dir.

    Matches on the DC_ACCOUNTS_PATH env var (the rpc client does not put the
    accounts dir on argv), so this only works on Linux.
    """
    return rpc_server_pids(cache_dir)


def parse_accounts_toml(path: Path) -> list[dict]:
    """Parse accounts.toml, return list of account dicts."""
    with open(path, "rb") as f:
        data = tomllib.load(f)
    return data.get("accounts", [])


def write_merged_accounts_toml(path: Path, accounts: list[dict]) -> None:
    """Write accounts.toml with renumbered sequential IDs."""
    if not accounts:
        path.write_text("")
        return
    lines = []
    ids = []
    for i, acct in enumerate(accounts, start=1):
        ids.append(i)
        lines.extend([
            "[[accounts]]",
            f"id = {i}",
            f'dir = "{acct["dir"]}"',
            f'uuid = "{acct["uuid"]}"',
            "",
        ])
    header = [
        f"selected_account = {ids[0]}",
        f"next_id = {ids[-1] + 1}",
        f"accounts_order = [{', '.join(str(i) for i in ids)}]",
        "",
    ]
    path.write_text("\n".join(header + lines))


def find_pools_to_migrate(cache_dir: Path) -> list[Path]:
    """Find pool dirs (worker-N, alive-check) that use the old per-relay layout."""
    pools = []
    for pool_dir in sorted(cache_dir.iterdir()):
        if not pool_dir.is_dir():
            continue
        # Already migrated: accounts.toml at pool level
        if (pool_dir / "accounts.toml").exists():
            continue
        # Old layout: relay subdirs containing accounts.toml
        has_relay_subdirs = any(
            (sub / "accounts.toml").exists()
            for sub in pool_dir.iterdir()
            if sub.is_dir()
        )
        if has_relay_subdirs:
            pools.append(pool_dir)
    return pools


def migrate_pool(pool_dir: Path, apply: bool) -> tuple[int, int]:
    """Migrate one pool dir from per-relay to flat layout.

    Returns (accounts_migrated, errors).
    """
    all_accounts = []
    relay_dirs = []
    errors = 0

    for relay_dir in sorted(pool_dir.iterdir()):
        if not relay_dir.is_dir():
            continue
        toml_path = relay_dir / "accounts.toml"
        if not toml_path.exists():
            continue
        relay_dirs.append(relay_dir)
        try:
            accounts = parse_accounts_toml(toml_path)
        except Exception as e:
            print(f"  error parsing {toml_path}: {e}", file=sys.stderr)
            errors += 1
            continue

        for acct in accounts:
            acct_data_dir = relay_dir / acct["dir"]
            all_accounts.append({
                "dir": acct["dir"],
                "uuid": acct["uuid"],
                "source_dir": acct_data_dir,
                "source_relay": relay_dir.name,
            })

    if not all_accounts:
        return 0, errors

    print(f"  {pool_dir.name}: {len(all_accounts)} account(s) from "
          f"{len(relay_dirs)} relay dir(s)")
    for acct in all_accounts:
        exists = acct["source_dir"].is_dir()
        print(f"    {acct['source_relay']}/{acct['dir'][:8]}... "
              f"({'exists' if exists else 'MISSING'})")

    if not apply:
        return len(all_accounts), errors

    # Move UUID data dirs up to pool level
    moved = 0
    for acct in all_accounts:
        src = acct["source_dir"]
        dst = pool_dir / acct["dir"]
        if not src.is_dir():
            print(f"    skip {src} (not found)")
            continue
        if dst.exists():
            print(f"    skip {dst} (already exists)")
            continue
        try:
            shutil.move(str(src), str(dst))
            moved += 1
        except Exception as e:
            print(f"    error moving {src} -> {dst}: {e}", file=sys.stderr)
            errors += 1

    # Write merged accounts.toml
    merged_path = pool_dir / "accounts.toml"
    clean_accounts = [{"dir": a["dir"], "uuid": a["uuid"]} for a in all_accounts]
    try:
        write_merged_accounts_toml(merged_path, clean_accounts)
        print(f"    wrote {merged_path} ({len(clean_accounts)} accounts)")
    except Exception as e:
        print(f"    error writing {merged_path}: {e}", file=sys.stderr)
        errors += 1
        return moved, errors

    # Remove old relay subdirs (should be empty now except for accounts.toml)
    for relay_dir in relay_dirs:
        try:
            shutil.rmtree(relay_dir)
            print(f"    removed {relay_dir.name}/")
        except Exception as e:
            print(f"    error removing {relay_dir}: {e}", file=sys.stderr)
            errors += 1

    return moved, errors


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Migrate chatmail-prober cache from per-relay to per-worker layout")
    parser.add_argument("cache_dir", type=Path,
                        help="path to chatmail-prober cache directory")
    parser.add_argument("--apply", action="store_true",
                        help="actually perform the migration (default: dry-run)")
    args = parser.parse_args(argv)

    cache_dir = args.cache_dir.resolve()
    if not cache_dir.is_dir():
        print(f"error: {cache_dir} is not a directory", file=sys.stderr)
        return 1

    if args.apply:
        pids = check_rpc_servers(cache_dir)
        if pids:
            print(f"error: {len(pids)} deltachat-rpc-server process(es) still running "
                  f"(PIDs: {', '.join(map(str, pids))})", file=sys.stderr)
            print("stop the service first: systemctl stop chatmail-prober",
                  file=sys.stderr)
            return 2

    pools = find_pools_to_migrate(cache_dir)
    if not pools:
        print("Nothing to migrate -- all pool dirs already use flat layout "
              "(or no pool dirs found).")
        return 0

    print(f"Found {len(pools)} pool dir(s) to migrate:")
    total_accounts = 0
    total_errors = 0
    for pool_dir in pools:
        migrated, errs = migrate_pool(pool_dir, apply=args.apply)
        total_accounts += migrated
        total_errors += errs

    if not args.apply:
        print(f"\nDry run: {total_accounts} account(s) in {len(pools)} pool(s) "
              f"would be migrated.")
        print("Re-run with --apply to execute.")
        return 1 if total_accounts > 0 else 0

    print(f"\nMigrated {total_accounts} account(s), {total_errors} error(s).")
    return 1 if total_errors > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
