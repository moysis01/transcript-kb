"""Discover with a channel instead of keywords: everything the channel published, judged on substance."""
import re
import time

import pytest

from tkb import config
from tkb.core import llm, youtube

from fakes import FakeClaude, FakeYouTube

DAY = 86_400
CID = "UC" + "m" * 22


def _vids(prefix: str, n: int, **extra) -> list[dict]:
    return [{"id": f"{prefix}{i:010d}"[-11:], "title": f"{prefix} {i}", **extra} for i in range(n)]


def _channel(**tabs) -> dict:
    now = time.time()
    defaults = {
        "videos": _vids("V", 30, duration=900, view_count=1000, timestamp=now - 40 * DAY),
        "shorts": _vids("S", 10, view_count=5000),
        "streams": _vids("L", 2, duration=3600, view_count=800, timestamp=now - 5 * DAY)
        + [{"id": "LUPCOMING01", "title": "Live next week", "duration": None}],
        "posts": [{"id": "POTHERCH001", "title": "Guest on a podcast", "duration": 3000, "view_count": 50,
                   "channel": "Some Podcast", "timestamp": now - 300 * DAY}],
    }
    return {"name": "Matt Pocock", "id": CID, "tabs": {**defaults, **tabs},
            "playlists": {"PLtips": {"title": "Tips", "entries": defaults["videos"][:2]}}}


@pytest.fixture
def claude(monkeypatch):
    fake = FakeClaude()
    monkeypatch.setattr(llm, "call", fake)
    # The prescreen likes long videos only; Claude's final scoring likes everything.
    fake.on("discover_channel", lambda m: {"best": [int(n) for n in re.findall(r"^\[(\d+)\] V ", m, re.M)]})
    fake.on("discover_rank", lambda m: {"scores": [{"n": int(n), "score": 80, "reason": "r"}
                                                   for n in re.findall(r"^\[(\d+)\]", m, re.M)]})
    fake.on("discover_queries", lambda m: {"queries": []})
    return fake


@pytest.fixture
def yt(monkeypatch):
    def install(**kw):
        fake = FakeYouTube(**kw)
        monkeypatch.setattr(youtube, "extract", fake)
        return fake
    return install


def _seen(r: dict) -> list[dict]:
    return r["picks"] + r["others"]


def test_every_kind_of_content_gets_a_look_and_shorts_are_kept(claude, yt):
    from tkb.services import discover
    yt(channels={"@mattpocockuk": _channel()})
    r = discover.discover([{"topic": "@mattpocockuk"}], picks=5)
    tabs = {c["tab"] for c in _seen(r)}
    assert tabs >= {"videos", "shorts", "streams", "posts"}, "the prescreen only liked videos; every source is read"
    assert r["dropped"].get("live or upcoming") == 1
    assert len(r["picks"]) == 5, "one channel, five picks: the per-channel cap doesn't apply"
    assert r["searches"][0]["topic"] == "@mattpocockuk" and r["searches"][0]["name"] == "Matt Pocock"
    assert r["queries"][0][:2] == ["30 Videos", "10 Shorts"]
    assert next(c for c in _seen(r) if c["tab"] == "posts")["channel"] == "Some Podcast"
    assert not any("results?search_query" in u for u in youtube.extract.requests), "no keyword search"


def test_keyword_searches_keep_their_channel_cap_in_a_mixed_run(claude, yt):
    from tkb.services import discover
    keyword_hits = [{"id": f"K{i:010d}", "title": f"K {i}", "channel": "Busy Channel"} for i in range(5)]
    yt(channels={"@mattpocockuk": _channel()}, searches={"agents": keyword_hits})
    r = discover.discover([{"topic": "agents"}, {"topic": "@MattPocockUK"}], picks=10)
    by = {}
    for c in r["picks"]:
        by[c["channel"]] = by.get(c["channel"], 0) + 1
    cap = config.get("discover.per_channel")
    assert by["Busy Channel"] == cap and by["Matt Pocock"] > cap


def test_a_channel_without_live_or_posts_tabs_still_works(claude, yt):
    from tkb.services import discover
    ch = _channel()
    del ch["tabs"]["streams"], ch["tabs"]["posts"], ch["playlists"]
    yt(channels={"@small": ch})
    r = discover.discover([{"topic": "@small"}], picks=3)
    assert len(r["picks"]) == 3 and {c["tab"] for c in _seen(r)} == {"videos", "shorts"}


def test_an_unknown_channel_is_a_clear_error(claude, yt):
    from tkb.services import discover
    yt(channels={})
    with pytest.raises(ValueError, match="No YouTube channel found for @nobody"):
        discover.discover([{"topic": "@nobody"}])


def test_every_spelling_of_a_channel_shares_one_topic(claude, yt):
    from tkb.services import discover
    yt(channels={"@mattpocockuk": _channel()})
    discover.discover([{"topic": "@MattPocockUK"}], picks=3)
    again = discover.discover([{"topic": "https://www.youtube.com/@mattpocockuk/videos"}], picks=3)
    assert again["dropped"]["offered before for this topic"] >= 3
    assert [t["topic"] for t in discover.load_topics().values()] == ["@mattpocockuk"]


def test_length_and_date_filters_apply_to_channel_content(claude, yt):
    from tkb.services import discover
    shorts = _vids("S", 3, view_count=5000)
    old_short, new_short = shorts[0]["id"], shorts[1]["id"]
    fake = yt(channels={"@mattpocockuk": _channel(shorts=shorts)})
    fake.videos[old_short]["upload_date"] = "20200101"  # Shorts' dates only come with their details
    fake.videos[new_short]["upload_date"] = time.strftime("%Y%m%d")
    r = discover.discover([{"topic": "@mattpocockuk", "date": "week"}], picks=10)
    assert {c["id"] for c in _seen(r)} >= {new_short, "L0000000000", "L0000000001"}
    assert not any(c["tab"] == "videos" or c["id"] == old_short for c in _seen(r)), "40-day-old videos are out"
    r = discover.discover([{"topic": "@mattpocockuk", "duration": "long"}], picks=10, only_new=False)
    assert all((c["duration"] or 0) >= 1200 for c in _seen(r))


def test_without_the_fast_model_the_most_viewed_are_read(claude, yt):
    from tkb.services import discover
    vids = _vids("V", 30, duration=900, view_count=10)
    vids[17]["view_count"] = 10**6
    yt(channels={"@mattpocockuk": _channel(videos=vids)})

    def down(m):
        raise llm.LLMError("claude is down")
    claude.on("discover_channel", down)
    r = discover.discover([{"topic": "@mattpocockuk"}], picks=3)
    assert vids[17]["id"] in {c["id"] for c in _seen(r)}
