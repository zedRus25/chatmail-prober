"""Account creation, reuse, and per-relay QR/login URL helpers.

Hosts AccountMaker (the per-DC handle that creates and recycles
chatmail accounts on a given relay) plus the small helpers it relies on
to build dcaccount: / dclogin: URLs for domain or IP relays.
"""

from __future__ import annotations

import ipaddress
import queue
import random
import string
import threading
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any

from deltachat_rpc_client import AttrDict, DeltaChat, EventType

from chatmail_prober.errors import _FATAL_CATEGORIES, PingError, _classify_error
from chatmail_prober.log_config import get_logger

log = get_logger(__name__)


#
# QR / login URL helpers
#

def is_ip_address(host: str) -> bool:
    """Check if the given host is an IP address."""
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def generate_credentials() -> tuple[str, str]:
    """Generate random username and password for IP-based login."""
    chars = string.ascii_lowercase + string.digits
    username = "".join(random.choices(chars, k=12))
    password = "".join(random.choices(chars, k=20))
    return username, password


def create_qr_url(domain_or_ip: str) -> str:
    """Create either a dcaccount or dclogin URL based on input type."""
    if is_ip_address(domain_or_ip):
        username, password = generate_credentials()
        encoded_password = urllib.parse.quote(password, safe="")
        return (
            f"dclogin:{username}@{domain_or_ip}/?"
            f"p={encoded_password}&v=1&ip=993&sp=465&ic=3&ss=default"
        )
    return f"dcaccount:{domain_or_ip}"


#
# AccountMaker
#

@dataclass
class _DomainScan:
    """Result of scanning the DC database for accounts on one relay domain."""
    found: Any | None = None             # configured account, not currently online
    found_unconfigured: Any | None = None # ghost (partially-configured) account
    total: int = 0                        # all accounts for this domain (configured + ghost)
    unconfigured: int = 0                 # ghosts within total


class AccountMaker:
    """Creates and manages deltachat accounts on a relay."""

    DEFAULT_MAX_ACCOUNTS_PER_DOMAIN = 3

    def __init__(
        self,
        dc: DeltaChat,
        max_accounts_per_domain: int = DEFAULT_MAX_ACCOUNTS_PER_DOMAIN,
    ) -> None:
        self.dc = dc
        self.online: list[Any] = []
        self.max_accounts_per_domain = max_accounts_per_domain
        # A single maker is shared across threads by the alive-check pool
        # (one thread per relay, all pointing at this maker).  The
        # find-or-create body of get_relay_account is check-then-act, so two
        # threads racing on the *same* domain could both miss the reuse check
        # and create duplicate accounts / exceed the per-domain limit.  A
        # per-domain lock serializes that section while leaving different
        # domains fully parallel.  self.online is only mutated while holding a
        # domain lock; cross-domain iteration of it in _find_reusable_online
        # is a read that CPython lists tolerate alongside another domain's
        # append.
        self._locks_guard = threading.Lock()
        self._domain_locks: dict[str, threading.Lock] = {}

    def _domain_lock(self, domain: str) -> threading.Lock:
        """Return the (lazily created) lock guarding setup for *domain*."""
        with self._locks_guard:
            lock = self._domain_locks.get(domain)
            if lock is None:
                lock = self._domain_locks[domain] = threading.Lock()
            return lock

    def wait_account_online(self, account: Any, timeout: float | None = None) -> None:
        """Wait for a single account to reach IMAP_INBOX_IDLE."""
        addr = account.get_config("addr") or account.get_config("configured_addr") or "unknown"
        log.debug("join_start", addr=addr)
        join_start = time.time()
        deadline = time.time() + timeout if timeout is not None else None
        eq = account._rpc.get_queue(account.id)
        while True:
            if deadline is not None and time.time() >= deadline:
                raise PingError(f"Timeout waiting for {addr} to come online")
            try:
                event = AttrDict(eq.get(timeout=1.0))
            except queue.Empty:
                continue
            if event.kind == EventType.IMAP_INBOX_IDLE:
                log.debug("join_done", addr=addr,
                          elapsed_s=round(time.time() - join_start, 3))
                return
            elif event.kind == EventType.ERROR:
                log.warning("ERROR during profile setup: %s", event.msg)
                if _classify_error(event.msg) in _FATAL_CATEGORIES:
                    raise PingError(event.msg)

    def _add_online(self, account: Any) -> None:
        account.set_config("bot", "1")
        account.set_config("delete_device_after", "3600")
        account.set_config("delete_server_after", "3600")
        account.start_io()
        self.online.append(account)

    @staticmethod
    def _account_domain(account: Any) -> tuple[str | None, bool]:
        """Extract the relay domain from an account, preferring configured_addr
        (fully configured) over addr (partially configured).
        Returns (domain, is_configured) or (None, False).
        """
        for value, is_configured in ((account.get_config("configured_addr"), True),
                                      (account.get_config("addr"), False)):
            if value and "@" in value:
                return value.partition("@")[2], is_configured
        return None, False

    def _find_reusable_online(
        self, domain: str, exclude: tuple[Any, ...]
    ) -> tuple[Any | None, str | None]:
        """Return an already-online account for domain, or None.

        Skips anything in *exclude* (used by self-loops to force a second account).
        """
        for account in self.online:
            if account in exclude:
                continue
            addr = account.get_config("configured_addr")
            if addr and addr.split("@")[1] == domain:
                return account, addr
        return None, None

    def _scan_db_for_domain(
        self, domain: str, exclude: tuple[Any, ...]
    ) -> _DomainScan:
        """Scan all DB accounts for *domain*.

        Ghosts (partially-configured accounts) count toward the per-domain
        limit so we don't infinitely retry broken setups.
        """
        scan = _DomainScan()
        for account in self.dc.get_all_accounts():
            if account in exclude:
                continue
            acct_domain, is_configured = self._account_domain(account)
            if acct_domain != domain:
                continue
            scan.total += 1
            if is_configured:
                if scan.found is None and account not in self.online:
                    scan.found = account
            else:
                scan.unconfigured += 1
                if scan.found_unconfigured is None and account not in self.online:
                    scan.found_unconfigured = account
        return scan

    def get_relay_account(
        self,
        domain: str,
        exclude: tuple[Any, ...] | None = None,
        worker_id: int | None = None,
    ) -> tuple[Any, bool]:
        """Get or create an account for domain.

        Returns (account, was_online) -- was_online=True means the account
        was already running and does not need wait_account_online().
        """
        _exclude = exclude or ()
        _wid = f"w{worker_id}" if worker_id is not None else "w?"

        # Serialize the find-or-create section per domain so concurrent
        # callers for the same relay can't race into duplicate accounts.
        with self._domain_lock(domain):
            reusable, addr = self._find_reusable_online(domain, _exclude)
            if reusable is not None:
                log.info("account_reused", relay=domain, addr=addr, worker=_wid)
                return reusable, True

            log.debug("setup_start", relay=domain, worker=_wid)
            setup_start = time.time()

            scan = self._scan_db_for_domain(domain, _exclude)

            if scan.found is not None:
                addr = scan.found.get_config("configured_addr") or "unknown"
                log.info("account_resumed", relay=domain, addr=addr,
                         worker=_wid, total=scan.total)
            elif scan.found_unconfigured is not None:
                scan.found = scan.found_unconfigured
                log.info("account_resuming_unconfigured", relay=domain,
                         worker=_wid, total=scan.total,
                         unconfigured=scan.unconfigured)
            else:
                if scan.total >= self.max_accounts_per_domain:
                    raise PingError(
                        f"Too many accounts for {domain} ({scan.total}, "
                        f"{scan.unconfigured} unconfigured), "
                        f"refusing to create more (limit {self.max_accounts_per_domain})"
                    )
                scan.found = self.dc.add_account()
                qr_url = create_qr_url(domain)
                scan.found.set_config_from_qr(qr_url)
                # Lazy import: metrics.py -> probe.py -> accounts.py would cycle.
                from chatmail_prober.metrics import account_creations_total  # noqa: PLC0415
                account_creations_total.labels(relay=domain).inc()
                reason = "no_accounts" if scan.total == 0 else "all_online"
                log.warning("account_created", relay=domain,
                            total=scan.total + 1,
                            unconfigured=scan.unconfigured,
                            reason=reason, worker=_wid)

            self._add_online(scan.found)
            log.debug("setup_done", relay=domain, worker=_wid,
                      elapsed_s=round(time.time() - setup_start, 3))
            return scan.found, False
