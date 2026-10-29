"""Durable background tasks (Discover searches, Ask questions) that outlive the browser tab.

Each task is saved by tkb.services.taskstore: its inputs, live progress and, when finished, its result.
Pages poll by id, so refreshing, closing the tab or losing the connection never loses work.
If the server restarts mid-task, the task is re-run from its saved inputs.
"""
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from tkb.core import log as logs
from tkb.services import taskstore

log = logs.get("tasks")


class Tasks:
    def __init__(self, runners: dict, workers: int = 4):
        """`runners[kind](params, progress) -> result`, where `progress(msg)` reports a step."""
        self.runners = runners
        self.pool = ThreadPoolExecutor(workers)
        self.live: dict[str, dict] = {}  # running/queued tasks; finished ones live only on disk
        self.lock = threading.RLock()
        taskstore.DIR.mkdir(parents=True, exist_ok=True)
        for t in taskstore.read_all():  # resume whatever a restart interrupted
            if t["state"] in ("queued", "running"):
                t.update(state="queued", progress=t.get("progress", []) + ["Restarted after the app restarted"])
                self._start(t)

    def _start(self, t: dict) -> None:
        with self.lock:
            self.live[t["id"]] = t
        taskstore.save(t)
        self.pool.submit(self._run, t)

    def submit(self, kind: str, params: dict) -> dict:
        if kind not in self.runners:
            raise ValueError(f"unknown task kind {kind!r}")
        t = {"id": uuid.uuid4().hex[:12], "kind": kind, "params": params, "state": "queued",
             "created": time.time(), "started": None, "finished": None, "progress": [], "result": None, "error": None}
        self._start(t)
        return t

    def _run(self, t: dict) -> None:
        t.update(state="running", started=time.time())
        taskstore.save(t)

        def progress(msg: str) -> None:
            t["progress"].append(msg)
            taskstore.save(t)

        try:
            t["result"] = self.runners[t["kind"]](t["params"], progress)
            t["state"] = "done"
        except Exception as e:  # noqa: BLE001 - any failure is reported to the page
            t.update(state="error", error=f"{type(e).__name__}: {e}")
            log.exception("%s task %s failed", t["kind"], t["id"])
        t["finished"] = time.time()
        taskstore.save(t)
        with self.lock:
            self.live.pop(t["id"], None)
        taskstore.prune(t["kind"])

    def get(self, tid: str) -> dict | None:
        with self.lock:
            if tid in self.live:
                return self.live[tid]
        return taskstore.load(tid)

    def list(self, kind: str | None = None, limit: int = 20) -> list[dict]:
        return taskstore.recent(kind, limit)
