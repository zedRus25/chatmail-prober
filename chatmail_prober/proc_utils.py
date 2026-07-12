"""Locate deltachat-rpc-server processes by their accounts directory.

The rpc client (``deltachat_rpc_client.Rpc``) launches the server as a bare
``deltachat-rpc-server`` process and passes the accounts directory via the
``DC_ACCOUNTS_PATH`` environment variable, *not* on the command line.  So
``pgrep -f deltachat-rpc-server.*<cache_dir>`` can never match our servers --
the cache dir simply isn't in argv.  We have to read each candidate's
environment from ``/proc`` instead.  Linux-only, which matches the rest of
the process tooling in this project (pgrep, ss, systemd); on other platforms
``rpc_server_pids`` returns an empty list.
"""

from __future__ import annotations

import os
from pathlib import Path

# deltachat_rpc_client.Rpc passes the accounts dir through this env var.
_ACCOUNTS_ENV = b"DC_ACCOUNTS_PATH="
# /proc/<pid>/comm is truncated to 15 bytes; "deltachat-rpc-server" (20 chars)
# lands here as "deltachat-rpc-s".
_COMM_PREFIX = "deltachat-rpc-s"


def _accounts_path_under(environ: bytes, cache_str: str) -> bool:
    """True if the process' DC_ACCOUNTS_PATH is cache_str or a subdir of it.

    Each pool passes a ``worker-N`` / ``alive-check`` / ``scan`` subdir of the
    cache dir, so a prefix match (not equality) is what we want.  Both the raw
    string and its realpath are compared so a symlinked home/cache dir still
    matches.
    """
    for entry in environ.split(b"\0"):
        if not entry.startswith(_ACCOUNTS_ENV):
            continue
        path = entry[len(_ACCOUNTS_ENV):].decode("utf-8", "replace")
        for p, c in (
            (path, cache_str),
            (os.path.realpath(path), os.path.realpath(cache_str)),
        ):
            if p == c or p.startswith(c + os.sep):
                return True
        return False  # env var present but not under our cache dir
    return False  # no DC_ACCOUNTS_PATH -> not one of ours


def rpc_server_pids(cache_dir: str | Path) -> list[int]:
    """Return PIDs of deltachat-rpc-server processes rooted at *cache_dir*.

    Matches processes whose ``DC_ACCOUNTS_PATH`` env var equals *cache_dir* or
    points inside it.  Returns an empty list on non-Linux systems, when
    ``/proc`` is unreadable, or when no matching process is found.
    """
    cache_str = str(cache_dir)
    pids: list[int] = []
    try:
        proc_entries = list(Path("/proc").iterdir())
    except OSError:
        return pids  # no /proc (non-Linux) -> nothing we can find
    for proc in proc_entries:
        if not proc.name.isdigit():
            continue
        try:
            comm = (proc / "comm").read_text().strip()
        except OSError:
            continue  # process exited between listdir and read
        if not comm.startswith(_COMM_PREFIX):
            continue
        try:
            environ = (proc / "environ").read_bytes()
        except OSError:
            continue  # process gone, or not ours to read (different user)
        if _accounts_path_under(environ, cache_str):
            pids.append(int(proc.name))
    return pids
