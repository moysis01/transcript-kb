"""Everything one YouTube channel has published, for Discover's channel searches: its videos, Shorts,
past live streams, the videos in its playlists and the videos its posts link to.

A channel is looked up once (so a wrong handle is a clear error, and every spelling of it shares one
topic), then its tabs are listed in parallel without downloading anything. Listings are flat: titles,
lengths, views and approximate dates; Shorts have no length or date until their details are read.
"""
import re
from concurrent.futures import ThreadPoolExecutor

from tkb import config
from tkb.core import youtube

CHANNEL = re.compile(r"(@[\w.\-]{3,30}|https?://(?:www\.|m\.)?youtube\.com/(?:@[\w.\-]+|channel/UC[\w-]{22}|c/[\w.\-]+"
                     r"|user/[\w.\-]+))/?(?:videos|shorts|streams|playlists|posts|featured)?/?")
TABS = {"videos": "Videos", "shorts": "Shorts", "streams": "Live", "posts": "Posts"}
FLAT = {"extract_flat": "in_playlist", "extractor_args": {"youtubetab": {"approximate_date": [""]}}}


class NotFound(ValueError):
    """No channel answers to that handle or URL."""


def is_channel(text: str) -> bool:
    return bool(CHANNEL.fullmatch(text.strip()))


def resolve(text: str) -> dict:
    """{"handle", "name", "url"} of the channel a handle or channel URL points to."""
    m = CHANNEL.fullmatch(text.strip())
    if not m:
        raise NotFound(f"{text!r} is not a channel handle or URL")
    base = m.group(1) if m.group(1).startswith("http") else f"https://www.youtube.com/{m.group(1)}"
    try:
        info = youtube.extract(base, {"extract_flat": True, "playlistend": 1})
    except youtube.Unavailable as e:
        raise NotFound(f"No YouTube channel found for {text.strip()} ({e})") from e
    cid = info.get("channel_id") or info.get("id") or ""
    handle = info.get("uploader_id") or ""
    return {"handle": handle if handle.startswith("@") else text.strip(), "name": info.get("channel") or handle,
            "url": f"https://www.youtube.com/channel/{cid}" if cid.startswith("UC") else base}


def _entry(e: dict, ch: dict, tab: str, label: str, i: int) -> dict | None:
    if not e or not e.get("id") or len(e["id"]) != 11:
        return None
    return {"id": e["id"], "title": e.get("title") or "", "channel": e.get("channel") or ch["name"],
            "duration": e.get("duration"), "views": e.get("view_count"), "snippet": e.get("description") or "",
            "url": e.get("url") or "", "timestamp": e.get("timestamp"), "query": label, "rank": i, "tab": tab}


def _list(url: str, n: int) -> list[dict]:
    try:
        return [e for e in youtube.extract(url, {**FLAT, "playlistend": n}).get("entries") or [] if e]
    except youtube.Unavailable:  # e.g. "This channel does not have a posts tab"
        return []


def listing(ch: dict) -> tuple[list[dict], dict[str, int]]:
    """Every item from the channel's tabs and playlists (a video can appear more than once), and how many each
    source gave. Each item says where it was found: `tab` (videos, shorts, streams, playlists, posts), `query`."""
    with ThreadPoolExecutor(8) as ex:
        per_tab, n_lists = config.get("channel.per_tab"), config.get("channel.playlists")
        tabs = dict(zip(TABS, ex.map(lambda t: _list(f"{ch['url']}/{t}", per_tab), TABS), strict=True))
        playlists = [p for p in _list(f"{ch['url']}/playlists", n_lists) if p.get("id")][:n_lists] if n_lists else []
        per_playlist = config.get("channel.per_playlist")
        expanded = list(ex.map(lambda p: _list(f"https://www.youtube.com/playlist?list={p['id']}", per_playlist),
                               playlists))
    items: list[dict] = []
    counts: dict[str, int] = {}
    for tab, entries in tabs.items():
        got = [c for i, e in enumerate(entries) if (c := _entry(e, ch, tab, TABS[tab], i))]
        items += got
        counts[TABS[tab]] = len(got)
    for p, entries in zip(playlists, expanded, strict=True):
        label = f"playlist “{p.get('title') or p['id']}”"
        items += [c for i, e in enumerate(entries) if (c := _entry(e, ch, "playlists", label, i))]
    counts["Playlists"] = len(playlists)
    return items, counts
