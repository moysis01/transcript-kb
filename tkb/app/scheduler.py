"""Runs watched topics while the app is open: checks every few minutes and, when a topic is due, starts
`tkb watch --run-due` as its own process. Being a separate process keeps the run at batch priority
(behind whatever you're doing on screen), and it's the same command cron would run, so the two never
double up. Its output goes to data/.logs/watch.log.
"""
import os
import subprocess
import sys
import threading
import time

from tkb import config
from tkb.config import DATA
from tkb.core import log as logs
from tkb.services import watch

log = logs.get("watch")

FIRST = 60  # seconds: the first check comes soon after starting (catching up); then every watch.check_minutes


class Scheduler:
    def __init__(self, port: int, first: float = FIRST, every: float | None = None):
        self.port, self.every = port, every
        self.proc: subprocess.Popen | None = None
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._loop, args=(first,), name="watch", daemon=True)
        self.thread.start()

    def _loop(self, first: float) -> None:
        wait = first
        while not self.stopped.wait(wait):
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - a bad tick must not end the schedule
                log.exception("watch check failed")
            wait = self.every or config.get("watch.check_minutes") * 60

    def tick(self) -> bool:
        """Start a run if a topic is due and none is running; True if one was started."""
        if self.proc and self.proc.poll() is None:
            return False
        if not (due := watch.due()):
            return False
        log.info("watched topics due: %s", ", ".join(due))
        (DATA / ".logs").mkdir(parents=True, exist_ok=True)
        with open(DATA / ".logs" / "watch.log", "a", encoding="utf-8") as out:
            self.proc = subprocess.Popen(
                [sys.executable, "-m", "tkb.cli", "watch", "--run-due", "--port", str(self.port)],
                stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                env={**os.environ, "TKB_PRIORITY": str(time.time())})  # queued now, like a batch job
        return True

    def stop(self) -> None:
        self.stopped.set()
        self.thread.join()
