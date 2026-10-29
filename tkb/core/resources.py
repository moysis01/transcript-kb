"""Capacity limits shared by every tkb process on this Mac (the web app's jobs and any CLI runs).

Each resource has N slots, each slot a lock file; holding a slot = holding an exclusive flock on
it. The OS releases locks when a process dies, so a crash can never leak capacity.

Waiting is first-come, first-served across processes: a waiter takes a ticket named after its
priority (TKB_PRIORITY, the time its video was queued; interactive work uses 0), and only the
oldest tickets may grab a free slot. Each process also measures how long it was completely
blocked waiting for capacity, so stage timings can separate waiting from working.

How many slots each resource has is a setting (Settings → Speed), or TKB_<NAME>=n (e.g. TKB_LLM=8).
"""
import contextlib
import fcntl
import os
import random
import threading
import time
import uuid
from collections.abc import Iterator
from typing import IO

from tkb import config
from tkb.config import LOCKS

PRIORITY = float(os.environ.get("TKB_PRIORITY", time.time()))  # lower goes first


def limit(name: str) -> int:
    return config.get(f"limit.{name}")


class _Blocked:
    """Time this process spent with at least one thread waiting for a slot and none holding one."""

    def __init__(self):
        self.lock = threading.Lock()
        self.held = self.waiting = 0
        self.total = 0.0
        self.since = time.monotonic()

    def _tick(self) -> None:  # call with the lock held, before the counts change
        now = time.monotonic()
        if self.waiting and not self.held:
            self.total += now - self.since
        self.since = now

    def change(self, held: int = 0, waiting: int = 0) -> None:
        with self.lock:
            self._tick()
            self.held += held
            self.waiting += waiting

    def seconds(self) -> float:
        with self.lock:
            self._tick()
            return self.total


blocked = _Blocked()


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _queue_position(wait_dir, ticket: str) -> int:
    """How many live tickets are ahead of ours (stale tickets of dead processes are removed)."""
    ahead = 0
    for t in sorted(p.name for p in wait_dir.iterdir()):
        if t == ticket:
            return ahead
        try:
            pid = int(t.split("-")[1])
        except (IndexError, ValueError):
            continue
        if _alive(pid):
            ahead += 1
        else:
            (wait_dir / t).unlink(missing_ok=True)
    return ahead


def _try_acquire(name: str, n: int) -> IO[str] | None:
    for i in random.sample(range(n), n):
        f = open(LOCKS / f"{name}.{i}", "w")  # noqa: SIM115 - returned to the caller, which closes it
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return f
        except BlockingIOError:
            f.close()
    return None


@contextlib.contextmanager
def slot(name: str, priority: float | None = None) -> Iterator[None]:
    """Block until one of the resource's slots is free (oldest waiters first), hold it for the block."""
    n = limit(name)
    wait_dir = LOCKS / f"{name}.queue"
    wait_dir.mkdir(parents=True, exist_ok=True)
    prio = PRIORITY if priority is None else priority
    ticket = f"{prio:020.6f}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    (wait_dir / ticket).touch()
    blocked.change(waiting=1)
    f = None
    try:
        while True:
            if _queue_position(wait_dir, ticket) < n and (f := _try_acquire(name, n)):
                break
            time.sleep(0.05 + random.random() * 0.1)
    finally:
        (wait_dir / ticket).unlink(missing_ok=True)
        blocked.change(waiting=-1, held=1 if f else 0)
    try:
        yield
    finally:
        fcntl.flock(f, fcntl.LOCK_UN)
        f.close()
        blocked.change(held=-1)
