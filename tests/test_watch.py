"""Watched topics: due runs happen once, never twice at a time, back off when YouTube pushes back."""
import time

import pytest

from tkb import config
from tkb.core.youtube import RateLimited
from tkb.services import discover, watch

DAY = watch.DAY


@pytest.fixture
def found(monkeypatch):
    """Discover stand-in: each run 'finds' the next two video ids for the topic."""
    runs: list[str] = []

    def fake(searches, picks, only_new=True, exclude=(), **kw):
        topic = searches[0]["topic"]
        runs.append(topic)
        if topic.startswith("limited"):
            raise RateLimited("HTTP Error 429")
        n = len(runs)
        return {"picks": [{"id": f"{topic[:1].upper()}{n:010d}", "title": f"{topic} {n}"}][:picks]}
    monkeypatch.setattr(discover, "discover", fake)
    return runs


def _age(topic: str, days: float) -> None:
    """Pretend the topic last ran `days` ago."""
    watch._update(topic, last=time.time() - days * DAY)


def test_a_due_topic_runs_once_then_waits(found):
    watch.set_watch("jev typesafe", every_days=7, picks=2)
    queued: list[list[dict]] = []
    queue = lambda picks: queued.append(picks) or len(picks)
    assert watch.due() == ["jev typesafe"], "a new watch runs right away"
    r = watch.run_due(queue, active=set)
    assert r[0]["queued"] == 1 and len(queued) == 1
    assert not watch.due() and not watch.run_due(queue, active=set), "not due again for a week"
    _age("jev typesafe", 30)  # the app was closed for a month: one catch-up run, not four
    watch.run_due(queue, active=set)
    assert found == ["jev typesafe"] * 2 and not watch.due()


def test_a_topic_waits_while_its_last_picks_are_still_processing(found):
    watch.set_watch("jev", every_days=1)
    watch.run_due(lambda p: len(p), active=set)
    last = watch.watched()[0]["watch"]["result"]["picked"]
    _age("jev", 2)
    assert watch.run_due(lambda p: len(p), active=lambda: set(last)) == [] and watch.due() == ["jev"]


def test_youtube_rate_limiting_backs_off_and_leaves_the_rest_for_later(found):
    watch.set_watch("limited topic", every_days=7)
    watch.set_watch("zzz later topic", every_days=7)
    _age("zzz later topic", 8)  # both due; the rate-limited one comes first
    _age("limited topic", 9)
    r = watch.run_due(lambda p: len(p), active=set)
    assert found == ["limited topic"] and "RateLimited" in r[0]["error"]
    nxt = {t["topic"]: t["next"] for t in watch.watched()}
    assert time.time() + 3000 < nxt["limited topic"] < time.time() + 3700, "retried in about an hour"


def test_settings_are_clamped_and_watching_can_stop():
    t = watch.set_watch("  jev   model ", every_days=0, picks=99)
    assert t["topic"] == "jev model" and t["watch"]["every_days"] == 1 and t["watch"]["picks"] == 10
    watch.set_watch("jev model", None)
    assert not watch.watched() and discover.load_topics()["jev model"]["topic"] == "jev model"


def test_deleting_a_video_removes_it_from_watch_history(found):
    from tkb.core.storage import write_json
    from tkb.services import delete
    watch.set_watch("jev", every_days=7)
    watch.run_due(lambda p: len(p), active=set)
    vid = watch.watched()[0]["watch"]["result"]["picked"][0]
    write_json(config.DATA / vid / "meta.json", {"id": vid, "title": "t"})
    delete.delete(vid)
    assert vid not in (config.DATA / ".topics.json").read_text()


def test_the_app_scheduler_starts_one_run_at_batch_priority(monkeypatch):
    from tkb.app import scheduler
    started = []

    class Proc:
        def __init__(self, cmd, **kw):
            started.append((cmd, kw["env"]["TKB_PRIORITY"]))

        def poll(self):
            return None  # still running

    monkeypatch.setattr(scheduler.subprocess, "Popen", Proc)
    s = scheduler.Scheduler(port=8765, first=3600)
    try:
        assert not s.tick(), "nothing watched, nothing started"
        watch.set_watch("jev", every_days=7)
        assert s.tick() and not s.tick(), "a second run never starts while one is going"
        cmd, prio = started[0]
        assert cmd[-4:] == ["watch", "--run-due", "--port", "8765"] and float(prio) > 1e9  # not 0: behind the user
    finally:
        s.stop()


def test_cli_run_due_processes_picks_itself_when_the_app_is_closed(found, monkeypatch):
    from tkb import cli
    ran = []
    monkeypatch.setattr(cli, "cmd_run", lambda a: ran.append(a.urls) or 0)
    watch.set_watch("jev", every_days=7)
    with pytest.raises(SystemExit) as done:
        cli.main(["watch", "--run-due", "--port", "9"])  # nothing listens on port 9
    assert done.value.code == 0 and ran == [["J0000000001"]]
