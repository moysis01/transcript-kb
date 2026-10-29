import subprocess
import sys
import threading
import time

from tkb.app import jobs as jobs_mod
from tkb.app.tasks import Tasks
from tkb.core import resources, storage


def test_slot_limits_threads(monkeypatch):
    monkeypatch.setenv("TKB_LLM", "2")
    active, peak, lock = [0], [0], threading.Lock()

    def work():
        with resources.slot("llm"):
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            time.sleep(0.1)
            with lock:
                active[0] -= 1

    ts = [threading.Thread(target=work) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert peak[0] == 2


def test_slot_limits_processes():
    code = ("import time; from tkb.core.resources import slot\n"
            "with slot('gpu'): time.sleep(0.6)")
    t = time.monotonic()
    ps = [subprocess.Popen([sys.executable, "-c", code]) for _ in range(2)]
    assert all(p.wait() == 0 for p in ps)
    assert time.monotonic() - t >= 1.1  # the two holds ran one after the other


def test_locked_read_modify_write_loses_nothing(home):
    f = home / "data" / "state.json"

    def bump(i):
        with storage.locked(f):
            d = storage.read_json(f) if f.exists() else {}
            time.sleep(0.005)
            d[str(i)] = i
            storage.write_json(f, d)

    ts = [threading.Thread(target=bump, args=(i,)) for i in range(20)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(storage.read_json(f)) == 20
    assert not list(f.parent.glob(".*.tmp"))  # atomic writes leave no temp files behind


def test_cancel_cannot_slip_past_a_starting_job(monkeypatch, home):
    """Cancelling while the worker is starting the process must still stop it."""
    real = subprocess.Popen
    started = []

    def slow_popen(cmd, **kw):
        time.sleep(0.3)  # widen the window between "mark running" and "process exists"
        p = real([sys.executable, "-c", "import time; time.sleep(30)"], **kw)
        started.append(p)
        return p

    monkeypatch.setattr(jobs_mod, "JOBS_FILE", home / "data" / ".jobs.json")
    monkeypatch.setattr(jobs_mod.subprocess, "Popen", slow_popen)
    jobs = jobs_mod.Jobs(max_running=4, ahead=4)
    jobs.lookups.submit = lambda *a, **k: None
    ids = [jobs.add(f"vid{i:08d}", None, True)["id"] for i in range(4)]
    time.sleep(0.1)  # every worker is now inside slow_popen
    for jid in ids:
        jobs.cancel(jid)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and any(p.poll() is None for p in started):
        time.sleep(0.05)
    jobs.stop()
    assert started and all(p.poll() is not None for p in started), "a cancelled job kept running"
    assert all(j["state"] == "cancelled" for j in jobs.jobs)


def test_tasks_resume_after_restart(home, monkeypatch):
    from tkb.services import taskstore
    monkeypatch.setattr(taskstore, "DIR", home / "data" / ".tasks")
    gate = threading.Event()
    calls = []

    def slow(params, progress):
        calls.append(params["n"])
        progress("working")
        gate.wait(5)
        return {"n": params["n"]}

    first = Tasks({"x": slow}, workers=1)
    t = first.submit("x", {"n": 1})
    time.sleep(0.2)
    assert storage.read_json(taskstore.DIR / f"{t['id']}.json")["state"] == "running"
    # simulate the app restarting while the task was mid-flight
    second = Tasks({"x": lambda p, progress: {"n": p["n"], "resumed": True}}, workers=1)
    for _ in range(50):
        r = second.get(t["id"])
        if r["state"] == "done":
            break
        time.sleep(0.05)
    gate.set()
    assert r["state"] == "done" and r["result"]["resumed"]


def test_waiters_are_served_oldest_first():
    order = []

    def hold():
        with resources.slot("gpu", priority=0):
            time.sleep(0.5)

    def wait(p):
        with resources.slot("gpu", priority=p):
            order.append(p)

    h = threading.Thread(target=hold)
    h.start()
    time.sleep(0.1)
    ws = [threading.Thread(target=wait, args=(p,)) for p in (30, 10, 20)]  # queued in a different order
    for w in ws:
        w.start()
        time.sleep(0.05)
    h.join()
    [w.join() for w in ws]
    assert order == [10, 20, 30]


def test_blocked_time_counts_only_waiting_for_capacity():
    hold = "import time; from tkb.core.resources import slot\nwith slot('gpu', priority=0): time.sleep(0.8)"
    other = subprocess.Popen([sys.executable, "-c", hold])
    time.sleep(0.3)  # the other process now holds the only GPU slot
    b0 = resources.blocked.seconds()
    with resources.slot("gpu", priority=1):
        time.sleep(0.3)  # working, not waiting
    waited = resources.blocked.seconds() - b0
    other.wait()
    assert 0.2 <= waited <= 1.2  # ~0.5 s by design; generous bounds so a busy machine doesn't fail it


def test_scheduler_keeps_the_early_stages_bounded_but_lets_later_videos_through(monkeypatch, home):
    """Videos past transcription don't count toward the early-stage limit, so new ones can start."""
    real = subprocess.Popen
    script = ("import json, sys, time\n"
              "print(json.dumps({'event': 'stage', 'stage': sys.argv[1]}), flush=True)\n"
              "time.sleep(4.0)")

    def fake(cmd, **kw):
        stage = "clean" if int(cmd[-1][-1]) < 2 else "speakers"  # videos 0-1 are past transcription
        return real([sys.executable, "-c", script, stage], **kw)

    monkeypatch.setattr(jobs_mod, "JOBS_FILE", home / "data" / ".jobs.json")
    monkeypatch.setattr(jobs_mod.subprocess, "Popen", fake)
    jobs = jobs_mod.Jobs(max_running=5, ahead=2)
    jobs.lookups.submit = lambda *a, **k: None
    for i in range(6):
        jobs.add(f"vid{i:08d}", f"v{i}", True)
    most_early, running = 0, []
    deadline = time.monotonic() + 3.5
    while time.monotonic() < deadline:  # poll: process start-up is slower on a busy machine
        with jobs.lock:
            running = [j for j in jobs.jobs if j["state"] == "running"]
            most_early = max(most_early, sum(j["stage"] in jobs_mod.EARLY and j["vid"][-1] >= "2" for j in running))
        if len(running) == 4 and all(j["stage"] for j in running):
            break
        time.sleep(0.05)
    jobs.stop()  # before the monkeypatches are undone, so nothing real is ever started
    assert most_early <= 2, "more videos in the early stages than allowed"
    assert len(running) == 4, f"expected 2 later-stage + 2 early videos running, got {len(running)}"


def test_a_successful_retry_clears_the_videos_earlier_failures(monkeypatch, home):
    real = subprocess.Popen
    monkeypatch.setattr(jobs_mod, "JOBS_FILE", home / "data" / ".jobs.json")
    monkeypatch.setattr(jobs_mod.subprocess, "Popen", lambda cmd, **kw: real([sys.executable, "-c", "pass"], **kw))
    jobs = jobs_mod.Jobs(max_running=2, ahead=2)
    jobs.lookups.submit = lambda *a, **k: None
    with jobs.lock:  # an earlier attempt that failed, and an unrelated failure that must stay
        jobs.jobs += [{"id": "old", "vid": "vid00000001", "state": "error", "proc": None},
                      {"id": "other", "vid": "vid00000002", "state": "error", "proc": None}]
    jobs.add("vid00000001", "v", True)
    for _ in range(50):
        if any(j["vid"] == "vid00000001" and j["state"] == "done" for j in jobs.jobs):
            break
        time.sleep(0.1)
    jobs.stop()
    assert [(j["vid"], j["state"]) for j in jobs.jobs] == [("vid00000002", "error"), ("vid00000001", "done")]
