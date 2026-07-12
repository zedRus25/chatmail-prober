"""Ping protocol: sends timestamped messages between two configured accounts and collects RTTs."""

from __future__ import annotations

import queue
import random
import string
import threading
import time
from collections.abc import Generator
from typing import Any

from deltachat_rpc_client import AttrDict, EventType

from chatmail_prober.log_config import get_logger

log = get_logger(__name__)


# Time receive() will keep waiting for in-flight responses after send_pings
# signals it has stopped (whether by completing self.count sends or erroring).
_POST_STOP_GRACE_S = 60.0


def _parse_pong(text: str, tx: str) -> tuple[int, float] | None:
    """Parse a pong message body into (seq, sent_at_unix). None if malformed.

    Pong format: "<tx_id> <unix_send_time> <seq>" -- see Pinger.send_pings.
    Bad messages are tolerated: any other client could send junk to the chat
    and we don't want a single ValueError to abort the receive loop.
    """
    parts = text.strip().split()
    if len(parts) != 3 or parts[0] != tx:
        return None
    try:
        return int(parts[2]), float(parts[1])
    except ValueError:
        return None


class Pinger:
    """Sends ping messages via 1:1 chat and collects RTTs.

    Lifecycle is explicit: construct, then either call .start() and
    iterate .receive(), or use as a context manager (recommended) which
    starts the send thread on enter and joins it on exit.
    """

    def __init__(
        self,
        sender: Any,
        receiver: Any,
        count: int,
        interval: float,
        timeout: float | None = None,
    ) -> None:
        self.sender = sender
        self.receiver = receiver
        self.count = count
        self.interval = interval
        self.timeout = timeout

        self.addr1: str = sender.get_config("addr")
        self.addr2: str = receiver.get_config("addr")
        self.relay1 = self.addr1.split("@")[1]
        self.relay2 = self.addr2.split("@")[1]

        contact = sender.create_contact(receiver)
        self.chat = contact.create_chat()

        log.debug(
            "PING %s -> %s count=%d interval=%ss",
            self.relay1, self.relay2, count, interval,
        )
        self.tx = "".join(random.choices(string.ascii_lowercase + string.digits, k=30))
        self.sent = 0
        self.received = 0
        self.results: list[tuple[int, float]] = []
        self.account_setup_time = 0.0
        self.message_time = 0.0
        self._stop_event = threading.Event()
        self._send_thread: threading.Thread | None = None
        # Snapshot of monotonic clock at construction. Both threads compute
        # their own deadline from (self._start_monotonic + self.timeout) so
        # there's no shared mutable deadline to race on.
        self._start_monotonic = time.monotonic()
        # Back-compat: kept readable so existing callers/tests can introspect.
        self.deadline: float | None = (
            time.time() + timeout if timeout is not None else None
        )

    @property
    def loss(self) -> float:
        expected = self.sent
        return 0.0 if expected == 0 else (1 - self.received / expected) * 100

    def _monotonic_deadline(self) -> float | None:
        """Per-thread deadline derived once from construction-time inputs."""
        if self.timeout is None:
            return None
        return self._start_monotonic + self.timeout

    def start(self) -> Pinger:
        """Spawn the daemon send thread.  Idempotent."""
        if self._send_thread is None:
            self._send_thread = threading.Thread(target=self.send_pings, daemon=True)
            self._send_thread.start()
        return self

    def join(self, timeout: float = 2.0) -> None:
        """Signal the send thread to exit and wait for it."""
        self._stop_event.set()
        if self._send_thread is not None:
            self._send_thread.join(timeout=timeout)

    def __enter__(self) -> Pinger:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.join()

    def send_pings(self) -> None:
        """Send pings at regular intervals (runs in a daemon thread)."""
        deadline = self._monotonic_deadline()
        try:
            for seq in range(self.count):
                if self._stop_event.is_set():
                    break
                if deadline is not None and time.monotonic() >= deadline:
                    break
                # seq is right-padded to a fixed width so every ping body is
                # the same length; _parse_pong re-splits on whitespace so the
                # padding is cosmetic (keeps message sizes uniform on the wire).
                text = f"{self.tx} {time.time():.4f} {seq:17}"
                self.chat.send_text(text)
                self.sent += 1
                time.sleep(self.interval)
        except Exception as e:
            log.warning("send_pings error on %s -> %s: %s", self.relay1, self.relay2, e)
        finally:
            self._stop_event.set()

    def receive(self) -> Generator[tuple[int, float], None, None]:
        """Receive ping responses, yielding (seq, ms_duration) pairs."""
        num_pending = self.count
        received_seqs: set[int] = set()

        # Local-only deadline: when no overall timeout is set, fall back to a
        # post-stop grace period once send_pings has signalled completion.
        # No cross-thread mutation -- only this thread reads/writes it.
        deadline = self._monotonic_deadline()

        account_queue = self.receiver._rpc.get_queue(self.receiver.id)
        try:
            while num_pending > 0:
                if deadline is None and self._stop_event.is_set():
                    deadline = time.monotonic() + _POST_STOP_GRACE_S
                if deadline is not None and time.monotonic() >= deadline:
                    break
                try:
                    item = account_queue.get(timeout=1.0)
                    event = AttrDict(item)
                except queue.Empty:
                    continue

                if event.kind == EventType.INCOMING_MSG:
                    msg = self.receiver.get_message_by_id(event.msg_id)
                    text = msg.get_snapshot().text
                    parsed = _parse_pong(text, self.tx)
                    if parsed is not None:
                        seq, sent_at = parsed
                        if seq not in received_seqs:
                            ms_duration = (time.time() - sent_at) * 1000
                            self.received += 1
                            num_pending -= 1
                            received_seqs.add(seq)
                            yield seq, ms_duration
                elif event.kind == EventType.ERROR:
                    log.warning("ERROR during receive: %s", event.msg)
        finally:
            # Signal the sender to stop even if the caller breaks early,
            # preventing send_pings from blocking indefinitely on sleep.
            self._stop_event.set()
