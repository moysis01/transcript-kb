"""Stand-ins for Claude, YouTube and Whisper, so the Claude-driven code can be tested for free and offline."""
import json
import re

from tkb.prompts import prompt


class FakeClaude:
    """Replaces tkb.core.llm.call. Answers by which prompt it was given; records every call."""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self.models: list[str | None] = []  # the model each call asked for
        self.handlers = {}

    def on(self, prompt_name: str, handler) -> "FakeClaude":
        """`handler(message) -> result` for calls made with the named system prompt."""
        self.handlers[prompt(prompt_name)] = (prompt_name, handler)
        return self

    def count(self, prompt_name: str) -> int:
        return sum(1 for name, _ in self.calls if name == prompt_name)

    def __call__(self, system: str, message: str, *, schema=None, model=None, retries=3, think=True):
        name, handler = self.handlers[system]
        self.calls.append((name, message))
        self.models.append(model)
        return handler(message)


def keep_every_sentence(message: str) -> dict:
    """The cleaning step's answer that keeps each numbered sentence as it is."""
    return {"sentences": [{"id": int(i), "text": t}
                          for i, t in re.findall(r"^\[(\d+)\] \([\d:]+\) (?:[^:\n]+: )?(.*)$", message, re.M)]}


class FakeYouTube:
    """Replaces tkb.core.youtube.extract: search results pages, channels (their tabs and playlists), and per-video
    metadata. A channel is {"name", "id" (UC + 22 chars), "tabs": {tab: entries}, "playlists": {id: {title,
    entries}}}, keyed by its lowercase @handle; a tab it doesn't have is missing, as on YouTube."""

    def __init__(self, searches: dict[str, list[dict]] | None = None, videos: dict[str, dict] | None = None,
                 channels: dict[str, dict] | None = None):
        self.searches, self.videos, self.channels = searches or {}, videos or {}, channels or {}
        lists = [(None, entries) for entries in self.searches.values()]
        for ch in self.channels.values():
            lists += [(ch["name"], entries) for entries in
                      [*ch.get("tabs", {}).values(), *(p["entries"] for p in ch.get("playlists", {}).values())]]
        for owner, entries in lists:  # a video's page agrees with its listing
            for e in entries:
                page = {k: e[k] for k in ("title", "channel", "duration") if e.get(k)}
                self.videos.setdefault(e["id"], {"channel": owner, **page} if owner else page)
        self.requests: list[str] = []

    def _channel(self, url: str) -> dict:
        from tkb.core import youtube
        m = re.search(r"youtube\.com/(?:channel/(UC[\w-]{22})|(@[\w.-]+))/?(\w*)$", url)
        if m:
            ch = next((c for h, c in self.channels.items() if m.group(1) == c["id"] or h == (m.group(2) or "").lower()),
                      None)
            if ch is None:
                raise youtube.Unavailable(f"{url}: HTTP Error 404: Not Found")
            handle = next(h for h, c in self.channels.items() if c is ch)
            tab = m.group(3)
            if not tab:
                return {"channel": ch["name"], "channel_id": ch["id"], "uploader_id": handle, "entries": []}
            if tab == "playlists" and ch.get("playlists"):
                return {"entries": [{"id": pid, "title": p["title"]} for pid, p in ch["playlists"].items()]}
            if tab not in ch.get("tabs", {}):
                raise youtube.Unavailable(f"{handle}: This channel does not have a {tab} tab")
            return {"entries": [{"url": f"https://www.youtube.com/{'shorts/' if tab == 'shorts' else 'watch?v='}"
                                        f"{e['id']}", **e} for e in ch["tabs"][tab]]}
        pid = re.search(r"list=([\w-]+)", url).group(1)
        return {"entries": next(c["playlists"][pid]["entries"] for c in self.channels.values()
                                if pid in c.get("playlists", {}))}

    def __call__(self, url: str, opts: dict | None = None, download: bool = False) -> dict:
        self.requests.append(url)
        if re.search(r"youtube\.com/(?:channel/|@|playlist\?)", url):
            return self._channel(url)
        if "results?search_query=" in url:
            from urllib.parse import parse_qs, urlparse
            q = parse_qs(urlparse(url).query)["search_query"][0]
            return {"entries": [{"duration": 600, "view_count": 1000, "url": f"https://youtube.com/watch?v={e['id']}",
                                 **e} for e in self.searches.get(q, [])]}
        vid = re.search(r"([A-Za-z0-9_-]{11})$", url).group(1)
        info = {"id": vid, "title": f"Video {vid}", "channel": "Channel", "duration": 600, "description": "",
                "webpage_url": f"https://www.youtube.com/watch?v={vid}", "language": "en", **self.videos.get(vid, {})}
        if download:  # what yt-dlp leaves behind: the 16 kHz wav
            out = opts["outtmpl"].replace("%(ext)s", "wav")
            open(out, "wb").close()
        return info


def write(path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj))
