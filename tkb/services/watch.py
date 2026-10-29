"""Watched topics: Discover runs by itself every few days for the topics you follow, and queues its picks.

Watch settings live with each topic's Discover memory in data/.topics.json, so a run only offers
videos the topic hasn't offered before, and deleting a video clears it from here too. Runs are
started by the app's scheduler or by cron/launchd (`tkb watch --run-due`); each run claims its topic
under the file lock first, so the two never run the same topic twice.
"""
import time
from collections.abc import Callable, Collection

from tkb import config
from tkb.core.storage import locked, write_json
from tkb.core.youtube import RateLimited
from tkb.services import discover as discover_mod
from tkb.services.discover import TOPICS

DAY = 86_400
EVERY = (1, 90)            # days between runs: at most daily, at least every 3 months
PICKS = (1, 10)            # videos queued per run



def _next(w: dict) -> float:
    return (w.get("last") or 0) + w["every_days"] * DAY


def watched() -> list[dict]:
    """Watched topics with when they run next (seconds since the epoch), soonest first."""
    out = [{**t, "next": _next(t["watch"])} for t in discover_mod.load_topics().values() if t.get("watch")]
    return sorted(out, key=lambda t: t["next"])


def set_watch(topic: str, every_days: int | None, picks: int = 3) -> dict:
    """Watch a topic (a past Discover search, or a new one), change its schedule, or stop (every_days=None)."""
    topic = discover_mod.normalize({"topic": topic})["topic"]
    with locked(TOPICS):
        topics = discover_mod.load_topics()
        t = topics.setdefault(topic.lower(), {"topic": topic, "offered": [], "runs": []})
        if every_days is None:
            t.pop("watch", None)
        else:
            old = t.get("watch") or {}
            t["watch"] = {**old, "every_days": max(EVERY[0], min(int(every_days), EVERY[1])),
                          "picks": max(PICKS[0], min(int(picks), PICKS[1])), "since": old.get("since", time.time())}
        TOPICS.parent.mkdir(parents=True, exist_ok=True)
        write_json(TOPICS, topics)
        return t


def due(now: float | None = None) -> list[str]:
    now = time.time() if now is None else now
    return [t["topic"] for t in watched() if t["next"] <= now]


def _update(topic: str, **changes: object) -> dict | None:
    with locked(TOPICS):
        topics = discover_mod.load_topics()
        t = topics.get(topic.lower())
        if not t or not t.get("watch"):
            return None
        t["watch"].update(changes)
        write_json(TOPICS, topics)
        return t


def _claim(topic: str, now: float) -> dict | None:
    """Stamp the run before starting it, so a second scheduler finds the topic no longer due."""
    with locked(TOPICS):
        topics = discover_mod.load_topics()
        t = topics.get(topic.lower())
        if not t or not t.get("watch") or _next(t["watch"]) > now:
            return None
        t["watch"]["last"] = now
        write_json(TOPICS, topics)
        return t


def run_due(queue: Callable[[list[dict]], int], active: Callable[[], Collection[str]],
            progress: Callable[[str], None] = lambda msg: None) -> list[dict]:
    """Run every due topic once (a long absence still means one run, not one per missed interval).
    `queue(picks) -> how many were queued`; `active()` lists videos in progress."""
    results = []
    for topic in due():
        now = time.time()
        t = discover_mod.load_topics()[topic.lower()]
        busy = set(t["watch"].get("result", {}).get("picked", [])) & set(active())
        if busy:  # its previous picks are still processing: don't pile up more
            progress(f"{topic}: skipped, {len(busy)} of its last picks are still processing")
            continue
        if not (t := _claim(topic, now)):
            continue
        progress(f"{topic}: searching")
        try:
            r = discover_mod.discover([{"topic": t["topic"], **t.get("options", {})}], t["watch"]["picks"],
                                      only_new=True, exclude=active())
            n = queue(r["picks"]) if r["picks"] else 0
            result = {"at": now, "picked": [c["id"] for c in r["picks"]], "queued": n}
            progress(f"{topic}: queued {n} of {len(r['picks'])} picks")
        except Exception as e:  # noqa: BLE001 - recorded on the topic, retried later
            retry = now - t["watch"]["every_days"] * DAY + config.get("watch.retry_minutes") * 60
            result = {"at": now, "picked": [], "queued": 0, "error": f"{type(e).__name__}: {e}"[:300]}
            _update(topic, last=retry, result=result)
            results.append({"topic": topic, **result})
            progress(f"{topic}: failed ({result['error']}); retrying in an hour")
            if isinstance(e, RateLimited):  # YouTube is pushing back: leave the other topics for later
                break
            continue
        _update(topic, result=result)
        results.append({"topic": topic, **result})
    return results
