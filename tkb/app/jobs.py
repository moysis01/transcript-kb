"""The processing queue: each video runs as a `tkb run --jsonl` subprocess, several at a time.

Scheduling: a video is started when the early stages have room (fewer than "downloading and transcribing
at once" videos downloading, detecting speakers or transcribing, since those funnel into the single GPU) and
fewer than "videos at once" are in progress overall (both are Settings, re-read every second). Videos past
transcription don't hold up new ones, so the GPU always has the next video ready while earlier ones are
cleaned and turned into notes in parallel. Inside each job, stages still wait for their shared resource
slots, oldest video first (TKB_PRIORITY = when it was queued).
"""
import collections
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from tkb import config
from tkb.config import DATA, ROOT
from tkb.core import log as logs
from tkb.core import youtube
from tkb.core.storage import read_json, write_json

log = logs.get("jobs")
JOBS_FILE = DATA / ".jobs.json"
EARLY = (None, "ingest", "speakers", "transcribe")  # None: started, first stage not reported yet


class Jobs:
    def __init__(self, max_running: int | None = None, ahead: int | None = None):
        """Fixed limits (tests); left out, they follow Settings."""
        self.max_running, self.ahead = max_running, ahead
        self.jobs: list[dict] = []
        self.pending: collections.deque = collections.deque()
        self.lock = threading.RLock()  # guards job state transitions together with their process handle
        self.changed = threading.Condition(self.lock)  # a job started, moved stage or finished
        self.lookups = ThreadPoolExecutor(2)  # background title lookups for pasted links
        self.stopped = False
        self.watchers: list[threading.Thread] = []
        # Survive restarts: unfinished jobs resume (every stage is cached, so nothing is redone).
        for j in (read_json(JOBS_FILE) if JOBS_FILE.exists() else []):
            j["proc"] = None
            if j["state"] in ("queued", "running"):
                j.update(state="queued", stage=None, pct=None)
                self.pending.append(j)
            self.jobs.append(j)
        threading.Thread(target=self._dispatch, daemon=True).start()

    def save(self) -> None:
        DATA.mkdir(exist_ok=True)
        with self.lock:
            write_json(JOBS_FILE, self.public()[-config.get("queue.keep"):])

    def add(self, vid: str, title: str | None, sponsorblock: bool, redo: bool = False,
            opts: dict | None = None, translate: bool = False) -> dict:
        job: dict[str, Any] = {
            "id": uuid.uuid4().hex[:8], "vid": vid, "title": title or _known_title(vid) or vid, "state": "queued",
            "stage": None, "done": 0, "total": 0, "pct": None, "warnings": [], "info": [],
            "error": None, "sponsorblock": sponsorblock, "redo": redo, "translate": translate, "opts": opts or {},
            "added": time.time(),
            "started": None, "finished": None, "timings": {}, "waited": {}, "proc": None}
        with self.changed:
            self.jobs.append(job)
            self.pending.append(job)
            self.changed.notify_all()
        if job["title"] == vid:  # a pasted link: fetch its title now rather than when its download starts
            self.lookups.submit(self._lookup_title, job)
        self.save()
        return job

    def _lookup_title(self, job: dict) -> None:
        try:
            title = youtube.extract(job["vid"], {"skip_download": True}).get("title")
        except (youtube.Unavailable, youtube.RateLimited):
            return
        if title and job["title"] == job["vid"]:
            job["title"] = title
            self.save()

    def active_vids(self) -> set[str]:
        return {j["vid"] for j in self.jobs if j["state"] in ("queued", "running")}

    def cancel(self, jid: str) -> bool:
        with self.changed:  # same lock the dispatcher holds from "is it cancelled?" until proc is assigned
            for j in self.jobs:
                if j["id"] == jid and j["state"] in ("queued", "running"):
                    j["state"] = "cancelled"
                    if j["proc"]:
                        j["proc"].terminate()
                    self.changed.notify_all()
                    self.save()
                    return True
        return False

    def forget(self, vid: str) -> None:
        """Stop any job for `vid` (waiting for its process to exit) and drop it from the history."""
        procs = []
        with self.lock:
            for j in self.jobs:
                if j["vid"] == vid and j["state"] in ("queued", "running"):
                    j["state"] = "cancelled"  # the dispatcher skips cancelled jobs still waiting
                    if j["proc"]:
                        j["proc"].terminate()
                        procs.append(j["proc"])
        for p in procs:
            try:
                p.wait(timeout=20)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()
        with self.lock:
            self.jobs = [j for j in self.jobs if j["vid"] != vid]
        self.save()

    def clear_finished(self) -> None:
        with self.lock:
            self.jobs = [j for j in self.jobs if j["state"] in ("queued", "running")]
        self.save()

    def public(self) -> list[dict]:
        return [{k: v for k, v in j.items() if k != "proc"} for j in self.jobs]

    def _room(self) -> bool:
        running = [j for j in self.jobs if j["state"] == "running"]
        early = sum(j["stage"] in EARLY for j in running)
        most = self.max_running or config.get("jobs")
        return len(running) < most and early < min(self.ahead or config.get("ahead"), most)

    def stop(self) -> None:
        """Start nothing more, stop running jobs and wait until they've wound down (used by tests)."""
        with self.changed:
            self.stopped = True
            for j in self.jobs:
                if j["proc"]:
                    j["proc"].terminate()
            self.changed.notify_all()
        self.lookups.shutdown(wait=False, cancel_futures=True)
        for t in list(self.watchers):
            t.join(timeout=10)

    def _dispatch(self) -> None:
        while True:
            with self.changed:
                while not self.stopped and not (self.pending and self._room()):
                    self.changed.wait(timeout=1)
                if self.stopped:
                    return
                job = self.pending.popleft()
                if job["state"] == "cancelled":
                    continue
                # a cancel can't slip in between the check above and the process existing
                job.update(state="running", started=time.time(), stage=None)
                p = subprocess.Popen(_command(job), cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     text=True, env={**os.environ, "TKB_PRIORITY": str(job["added"])})
                job["proc"] = p
                self.save()
            log.info("job %s started: %s (%s)", job["id"], job["vid"], " ".join(_command(job)[4:]))
            w = threading.Thread(target=self._watch, args=(job, p), daemon=True)
            self.watchers = [t for t in self.watchers if t.is_alive()] + [w]
            w.start()

    def _watch(self, job: dict, p: subprocess.Popen) -> None:
        tail: list[str] = []
        threading.Thread(target=self._stderr, args=(job, p, tail), daemon=True).start()
        assert p.stdout is not None  # started with stdout=PIPE
        for line in p.stdout:
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            self._event(job, ev)
        p.wait()
        with self.changed:
            job.update(proc=None, finished=time.time())
            if job["state"] == "running":
                if p.returncode == 0 and not job["error"]:
                    job["state"], job["stage"] = "done", None
                    # a retry that worked makes the video's earlier failed or cancelled attempts noise
                    self.jobs = [j for j in self.jobs if j is job or j["vid"] != job["vid"]
                                 or j["state"] not in ("error", "cancelled")]
                else:
                    job["state"] = "error"
                    job["error"] = job["error"] or ("".join(tail)[-600:] or f"exit code {p.returncode}")
                    log.error("job %s failed: %s: %s", job["id"], job["vid"], job["error"][-300:])
            if job["state"] == "done":
                log.info("job %s done: %s in %.0fs", job["id"], job["vid"], job["finished"] - (job["started"] or 0))
            self.changed.notify_all()
        self.save()

    @staticmethod
    def _stderr(job: dict, p: subprocess.Popen, tail: list[str]) -> None:
        """Whisper's tqdm bar is on stderr, separated by \\r; pick the percentage out of it."""
        buf = ""
        assert p.stderr is not None  # started with stderr=PIPE
        while True:
            ch = p.stderr.read(256)
            if not ch:
                break
            buf = (buf + ch)[-4000:]
            m = re.findall(r"(\d{1,3})%\|", buf)
            if m and job["stage"] == "transcribe":
                job["pct"] = int(m[-1])
            tail[:] = [buf]

    def _event(self, job: dict, ev: dict) -> None:
        e = ev["event"]
        if e == "stage":
            with self.changed:
                job.update(stage=ev["stage"], done=0, total=0, pct=None)
                self.changed.notify_all()  # leaving the early stages may make room for the next video
            self.save()
        elif e == "progress":
            job.update(done=ev["done"], total=ev["total"])
        elif e == "stage_done":
            job.setdefault("timings", {})[ev["stage"]] = ev["seconds"]
            job.setdefault("waited", {})[ev["stage"]] = ev.get("waited", 0)
        elif e == "meta":
            job["title"] = ev["title"]
        elif e == "warning":
            job["warnings"].append(ev["msg"])
        elif e == "info":
            job["info"].append(ev["msg"])
        elif e == "error":
            job["error"] = ev["error"]


def _command(job: dict) -> list[str]:
    cmd = [sys.executable, "-m", "tkb.cli", "run", "--jsonl",  # both choices explicit: the job's own,
           "--sponsorblock" if job["sponsorblock"] else "--no-sponsorblock",  # never the Settings default
           "--translate" if job.get("translate") else "--no-translate"]
    if job.get("redo"):
        cmd.append("--redo")
    o = job.get("opts") or {}
    if o.get("stage"):
        cmd += ["--stage", o["stage"]]
    if o.get("num_speakers"):
        cmd += ["--num-speakers", str(int(o["num_speakers"]))]
    if o.get("redo_kb"):
        cmd.append("--redo-kb")
    return cmd + ["--", job["vid"]]  # after "--": an id can start with "-"


def _known_title(vid: str) -> str | None:
    """The title from an earlier download of this video, if any."""
    f = DATA / vid / "meta.json"
    try:
        return read_json(f).get("title") if f.exists() else None
    except ValueError:
        return None
