#!/usr/bin/env python3
"""cleanup_accounts.py -- analyze and trim stale accounts from the cache dir.

Supports both directory layouts:
  - New (flat):  worker-N/accounts.toml  (one DB per worker, multiple domains)
  - Old (split): worker-N/relay.domain/accounts.toml  (one DB per relay)

Identifies accounts exceeding the per-domain limit and removes excess entries.
Keeps the newest accounts (highest IDs) per domain, removes the oldest.

Usage:
    # Dry-run (default): report what would be cleaned
    uv run python scripts/cleanup_accounts.py /var/lib/chatmail-prober

    # Actually clean up (service must be stopped first)
    uv run python scripts/cleanup_accounts.py /var/lib/chatmail-prober --apply

Exit codes:
    0  no excess accounts found (or cleanup applied successfully)
    1  excess accounts found (dry-run) or errors during cleanup
    2  refused to run (RPC servers still running)
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import subprocess
import sys
try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore[no-redef]
from collections import defaultdict
from pathlib import Path

from chatmail_prober.accounts import AccountMaker
from chatmail_prober.proc_utils import rpc_server_pids

# Single source of truth lives on AccountMaker; the script-side name is just
# an alias so the existing call sites stay readable.
_MAX_ACCOUNTS_PER_DOMAIN = AccountMaker.DEFAULT_MAX_ACCOUNTS_PER_DOMAIN


def _count_blobs(acct_dir: Path) -> int:
    """Count files in the dc.db-blobs directory of an account."""
    blobs_dir = acct_dir / "dc.db-blobs"
    if not blobs_dir.is_dir():
        return 0
    return sum(1 for f in blobs_dir.iterdir() if f.is_file())


def _clean_blobs(acct_dir: Path) -> int:
    """Delete all files in dc.db-blobs/. Returns number of files deleted."""
    blobs_dir = acct_dir / "dc.db-blobs"
    if not blobs_dir.is_dir():
        return 0
    deleted = 0
    for f in blobs_dir.iterdir():
        if f.is_file():
            try:
                f.unlink()
                deleted += 1
            except Exception:
                pass
    return deleted


def _all_account_dirs(cache_dir: Path) -> list[Path]:
    """Return all account data dirs (UUID dirs containing dc.db) across all pools."""
    dirs = []
    for pool_dir in cache_dir.iterdir():
        if not pool_dir.is_dir():
            continue
        for entry in pool_dir.iterdir():
            if entry.is_dir() and (entry / "dc.db").exists():
                dirs.append(entry)
    return dirs


def _clear_messages(db: Path) -> int:
    """Delete all rows from message-related tables. Returns rows deleted."""
    tables = ("msgs", "mime_headers", "imap", "smtp", "smtp_mdns", "smtp_status_updates",
              "imap_markseen", "imap_send", "imap_sync", "sending_blocklist")
    deleted = 0
    try:
        con = sqlite3.connect(str(db))
        for table in tables:
            try:
                cur = con.execute(f"DELETE FROM {table}")  # noqa: S608
                deleted += cur.rowcount
            except sqlite3.OperationalError:
                pass  # table may not exist in older schema versions
        con.commit()
        con.close()
    except Exception:
        pass
    return deleted


def _du_sh(path: Path) -> str:
    """Return human-readable disk usage for path (like du -sh)."""
    try:
        result = subprocess.run(["du", "-sh", str(path)], capture_output=True, text=True)
        return result.stdout.split()[0] if result.stdout else "?"
    except Exception:
        return "?"


def check_rpc_servers(cache_dir: Path) -> list[int]:
    """Return PIDs of deltachat-rpc-server processes using this cache dir.

    Matches on the DC_ACCOUNTS_PATH env var (the rpc client does not put the
    accounts dir on argv), so this only works on Linux.
    """
    return rpc_server_pids(cache_dir)


def read_account_domain(pool_dir: Path, acct_dir_name: str) -> str | None:
    """Read domain from account sqlite DB. Returns domain string or None."""
    db = pool_dir / acct_dir_name / "dc.db"
    if not db.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        # Prefer configured_addr (fully configured) over addr (partially configured),
        # matching the priority in RelayPool._account_domain().
        cur = con.execute("SELECT value FROM config WHERE keyname='configured_addr'")
        row = cur.fetchone()
        if not row:
            cur = con.execute("SELECT value FROM config WHERE keyname='addr'")
            row = cur.fetchone()
        con.close()
        if row and "@" in row[0]:
            return row[0].split("@")[1]
    except Exception:
        pass
    return None


def parse_accounts_toml(path: Path) -> tuple[dict, list[dict]]:
    """Parse accounts.toml, return (top-level dict, list of account dicts).

    Format:
        selected_account = 2
        next_id = 3
        accounts_order = [1, 2]
        [[accounts]]
        id = 1
        dir = "uuid-string"
        uuid = "uuid-string"
    """
    with open(path, "rb") as f:
        data = tomllib.load(f)
    accounts = data.get("accounts", [])
    return data, accounts


def write_accounts_toml(path: Path, accounts: list[dict]) -> None:
    """Write accounts.toml keeping the given accounts, renumbered from 1."""
    if not accounts:
        path.write_text("")
        return
    ids = list(range(1, len(accounts) + 1))
    lines = [
        f"selected_account = {ids[0]}",
        f"next_id = {ids[-1] + 1}",
        f"accounts_order = [{', '.join(str(i) for i in ids)}]",
        "",
    ]
    for i, acct in zip(ids, accounts):
        lines += [
            "[[accounts]]",
            f"id = {i}",
            f'dir = "{acct["dir"]}"',
            f'uuid = "{acct["uuid"]}"',
            "",
        ]
    path.write_text("\n".join(lines))


def get_excess_accounts(accounts: list[dict]) -> list[dict]:
    """Return accounts exceeding _MAX_ACCOUNTS_PER_DOMAIN per domain.

    Keeps the newest accounts (highest IDs) per domain, marks oldest as excess.
    Accounts without a domain (unconfigured ghosts) are all excess -- keep 0.
    """
    by_domain: dict[str, list[dict]] = defaultdict(list)
    for a in accounts:
        key = a.get("domain") or "(unknown)"
        by_domain[key].append(a)

    excess = []
    for domain, accts in by_domain.items():
        keep = 0 if domain == "(unknown)" else _MAX_ACCOUNTS_PER_DOMAIN
        sorted_accts = sorted(accts, key=lambda a: a["id"], reverse=True)  # newest first
        excess.extend(sorted_accts[keep:])
    return excess


def analyze_dir(cache_dir: Path) -> list[dict]:
    """Walk the cache dir and find all accounts.toml files.

    Handles both layouts:
    - New: worker-N/accounts.toml (flat, one DB per worker, domain read from sqlite)
    - Old: worker-N/relay.domain/accounts.toml (split, one DB per relay)
    """
    results = []
    for pool_dir in sorted(cache_dir.iterdir()):
        if not pool_dir.is_dir():
            continue
        # New flat layout: accounts.toml directly in pool dir
        flat_toml = pool_dir / "accounts.toml"
        if flat_toml.exists():
            try:
                _data, accounts = parse_accounts_toml(flat_toml)
            except Exception as e:
                results.append({
                    "pool": pool_dir.name,
                    "relay": "(shared)",
                    "path": flat_toml,
                    "error": str(e),
                })
                continue
            # Enrich with domain info from each account's sqlite DB
            for acct in accounts:
                acct["domain"] = read_account_domain(pool_dir, acct["dir"])
            results.append({
                "pool": pool_dir.name,
                "relay": "(shared)",
                "path": flat_toml,
                "accounts": accounts,
                "count": len(accounts),
                "flat": True,
            })
            continue
        # Old per-relay layout: accounts.toml in relay subdirs
        for relay_dir in sorted(pool_dir.iterdir()):
            if not relay_dir.is_dir():
                continue
            toml_path = relay_dir / "accounts.toml"
            if not toml_path.exists():
                continue
            try:
                _data, accounts = parse_accounts_toml(toml_path)
            except Exception as e:
                results.append({
                    "pool": pool_dir.name,
                    "relay": relay_dir.name,
                    "path": toml_path,
                    "error": str(e),
                })
                continue
            results.append({
                "pool": pool_dir.name,
                "relay": relay_dir.name,
                "path": toml_path,
                "accounts": accounts,
                "count": len(accounts),
                "flat": False,
            })
    return results


def count_excess(r: dict) -> int:
    """Return the number of excess accounts in a result entry."""
    if "accounts" not in r:
        return 0
    if r.get("flat"):
        return len(get_excess_accounts(r["accounts"]))
    else:
        # Old per-relay layout: each file is single-domain, limit is _MAX_ACCOUNTS_PER_DOMAIN
        return max(0, r["count"] - _MAX_ACCOUNTS_PER_DOMAIN)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Analyze and clean up excess accounts in chatmail-prober cache")
    parser.add_argument("cache_dir", type=Path,
                        help="path to chatmail-prober cache directory")
    parser.add_argument("--apply", action="store_true",
                        help="actually remove excess accounts (default: dry-run)")
    parser.add_argument("--clear-messages", action="store_true",
                        help="clear message tables from all account DBs (one-shot legacy cleanup; "
                             "requires --apply; accounts have delete_device_after=3600 so this "
                             "is not needed after the first run)")
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
            print("stop the service first: systemctl stop chatmail-prober", file=sys.stderr)
            return 2

    results = analyze_dir(cache_dir)
    if not results:
        print(f"no accounts.toml files found in {cache_dir}")
        return 0

    # Summary
    total_files = len([r for r in results if "error" not in r])
    total_accounts = sum(r.get("count", 0) for r in results)
    total_excess = sum(count_excess(r) for r in results)
    excess_entries = [r for r in results if count_excess(r) > 0]
    error_entries = [r for r in results if "error" in r]

    print(f"Cache dir: {cache_dir}")
    print(f"  accounts.toml files: {total_files}")
    print(f"  total accounts:      {total_accounts}")
    print(f"  files with excess:   {len(excess_entries)}")
    print(f"  excess accounts:     {total_excess}")
    if error_entries:
        print(f"  parse errors:        {len(error_entries)}")
    print()

    # Per-pool breakdown
    pools: dict[str, dict] = {}
    for r in results:
        pool = r["pool"]
        if pool not in pools:
            pools[pool] = {"files": 0, "accounts": 0, "excess": 0}
        if "error" not in r:
            pools[pool]["files"] += 1
            pools[pool]["accounts"] += r["count"]
            pools[pool]["excess"] += count_excess(r)

    print(f"  {'Pool':<20} {'Files':>6} {'Accounts':>9} {'Excess':>7}")
    print(f"  {'-'*20} {'-'*6} {'-'*9} {'-'*7}")
    for pool, stats in sorted(pools.items()):
        print(f"  {pool:<20} {stats['files']:>6} {stats['accounts']:>9} {stats['excess']:>7}")
    print()

    if error_entries:
        print("Parse errors:")
        for r in error_entries:
            print(f"  {r['pool']}/{r['relay']}: {r['error']}")
        print()

    total_blobs = sum(_count_blobs(d) for d in _all_account_dirs(cache_dir))
    du_before = _du_sh(cache_dir)
    print(f"Blob files:  {total_blobs}")
    print(f"Disk usage:  {du_before}")

    if not excess_entries and not total_blobs and not args.clear_messages:
        print("No cleanup needed.")
        return 0

    if excess_entries:
        print("\nExcess accounts:")
        for r in excess_entries:
            accounts = r["accounts"]
            if r.get("flat"):
                by_domain: dict[str, list[dict]] = defaultdict(list)
                for a in accounts:
                    by_domain[a.get("domain") or "(unknown)"].append(a)
                for domain, accts in sorted(by_domain.items()):
                    keep = 0 if domain == "(unknown)" else _MAX_ACCOUNTS_PER_DOMAIN
                    sorted_accts = sorted(accts, key=lambda a: a["id"], reverse=True)
                    n_excess = max(0, len(sorted_accts) - keep)
                    if n_excess > 0:
                        keep_ids = [a["id"] for a in sorted_accts[:keep]]
                        rm_ids = [a["id"] for a in sorted_accts[keep:]]
                        print(f"  {r['pool']}/{domain}: {len(accts)} accounts, "
                              f"keep={keep_ids}, remove={rm_ids}")
            else:
                sorted_accts = sorted(accounts, key=lambda a: a["id"], reverse=True)
                keep = sorted_accts[:_MAX_ACCOUNTS_PER_DOMAIN]
                remove = sorted_accts[_MAX_ACCOUNTS_PER_DOMAIN:]
                keep_ids = [a["id"] for a in keep]
                remove_ids = [a["id"] for a in remove]
                print(f"  {r['pool']}/{r['relay']}: {r['count']} accounts, "
                      f"keep={keep_ids}, remove={remove_ids}")

    if not args.apply:
        parts = []
        if total_excess:
            parts.append(f"{total_excess} excess account(s)")
        if total_blobs:
            parts.append(f"{total_blobs} blob file(s)")
        if args.clear_messages:
            parts.append("message tables (--clear-messages)")
        print(f"\nDry run: would remove {', '.join(parts)}.")
        print("Re-run with --apply to execute.")
        return 1 if (total_excess or total_blobs or args.clear_messages) > 0 else 0

    print("Cleaning...")

    # Apply cleanup
    cleaned = 0
    errors = 0
    for r in excess_entries:
        accounts = r["accounts"]
        pool_dir = r["path"].parent

        if r.get("flat"):
            excess = get_excess_accounts(accounts)
            excess_set = {a["id"] for a in excess}
            keep_accounts = [a for a in accounts if a["id"] not in excess_set]
        else:
            sorted_accts = sorted(accounts, key=lambda a: a["id"], reverse=True)
            keep_accounts = sorted_accts[:_MAX_ACCOUNTS_PER_DOMAIN]
            excess = sorted_accts[_MAX_ACCOUNTS_PER_DOMAIN:]

        # Back up accounts.toml
        backup = r["path"].with_suffix(".toml.bak")
        shutil.copy2(r["path"], backup)

        for acct in excess:
            acct_dir = pool_dir / acct["dir"]
            if acct_dir.is_dir():
                try:
                    shutil.rmtree(acct_dir)
                    print(f"  removed {acct_dir}")
                    cleaned += 1
                except Exception as e:
                    print(f"  error removing {acct_dir}: {e}", file=sys.stderr)
                    errors += 1
            else:
                print(f"  skipped {acct_dir} (not found)")
                cleaned += 1

        try:
            write_accounts_toml(r["path"], keep_accounts)
            print(f"  rewrote {r['path']} (kept {len(keep_accounts)} account(s))")
        except Exception as e:
            print(f"  error rewriting {r['path']}: {e}", file=sys.stderr)
            errors += 1

    # Clean blobs and VACUUM all surviving account DBs across the whole cache
    blobs_deleted = 0
    vacuumed = 0
    vacuum_errors = 0
    for acct_dir in _all_account_dirs(cache_dir):
        blobs_deleted += _clean_blobs(acct_dir)
        db = acct_dir / "dc.db"
        try:
            con = sqlite3.connect(str(db))
            con.execute("VACUUM")
            con.close()
            vacuumed += 1
        except Exception as e:
            print(f"  vacuum error {db}: {e}", file=sys.stderr)
            vacuum_errors += 1

    msgs_deleted = 0
    if args.clear_messages:
        for acct_dir in _all_account_dirs(cache_dir):
            msgs_deleted += _clear_messages(acct_dir / "dc.db")
        # Re-VACUUM after clearing message tables to actually reclaim space
        for acct_dir in _all_account_dirs(cache_dir):
            try:
                con = sqlite3.connect(str(acct_dir / "dc.db"))
                con.execute("VACUUM")
                con.close()
            except Exception:
                pass

    du_after = _du_sh(cache_dir)
    print(f"\nCleaned {cleaned} excess account(s), {errors} error(s).")
    print(f"Deleted {blobs_deleted} blob file(s), vacuumed {vacuumed} DB(s)"
          f"{f', {vacuum_errors} vacuum error(s)' if vacuum_errors else ''}.")
    if args.clear_messages:
        print(f"Cleared {msgs_deleted} message row(s) from all DBs (re-vacuumed).")
    print(f"Disk usage after:  {du_after} (was {du_before})")
    return 1 if (errors or vacuum_errors) > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
