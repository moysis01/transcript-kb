"""The knowledge base as an MCP server, so AI apps (Claude, Cursor, any MCP client) can search and read it: `tkb mcp`.

Reading is done here, straight from the library. Finding and adding videos is handed to the running app
(`tkb serve`): it owns the queue and does the processing, and its Discover keeps a history. Only `discover`
(spends AI credits) and `queue_videos` change anything, and they're marked so AI apps ask first; nothing deletes.
Speaks MCP over stdin/stdout, so nothing else may ever print to stdout; logs go to stderr.
Every passage comes with a timestamped YouTube link, so answers can cite the exact moment.
"""
import json
import os
import threading
import time
import urllib.error
import urllib.request
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError  # its message reaches the AI app; other errors don't
from mcp.types import ToolAnnotations

from tkb import config
from tkb.app.views import concept, concept_files, library
from tkb.config import DATA
from tkb.core.storage import read_json
from tkb.core.text import fmt_ts, parse_ts, video_id_from
from tkb.pipeline.clean import paragraphs
from tkb.pipeline.diarize import names
from tkb.services import search as search_mod

# How much each search hit and transcript excerpt carries: Settings → MCP server.
READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
CHANGES = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True)
DISCOVER_WAIT = 45  # seconds `discover` waits for its result before handing back a task id (AI apps time calls out)

server = MCPServer(
    "transcript-kb",
    instructions="A personal knowledge base built from YouTube videos: cleaned transcripts (ads and filler removed, "
                 "speakers named), per-video summaries, and concept notes merged across videos. Start with `search`; "
                 "read more with `get_video`, `get_concept` or `get_transcript`. Cite moments with the `link` "
                 "fields, which open the video at that second. To add videos: `discover` finds and scores "
                 "candidates, show the user the picks, then `queue_videos`; `queue_status` shows when each is "
                 "processed (a few minutes per video) and readable.",
)


def _link(vid: str, t: float = 0) -> str:
    return f"https://youtu.be/{vid}" + (f"?t={int(t)}" if t else "")


def _vid(video_id: str) -> str:
    try:
        vid = video_id_from(video_id.strip())
    except ValueError as e:
        raise ToolError(str(e)) from e
    if not (DATA / vid / "meta.json").exists():
        raise ToolError(f"{video_id!r} is not in the library; `list_videos` shows what is")
    return vid


@server.tool(annotations=READ_ONLY)
def search(query: str, limit: int = 10, kind: str = "all") -> list[dict]:
    """Search the library by keywords and by meaning. `kind`: all, transcript (what was said), concept
    (notes merged across videos) or video (summaries). Returns the best matches, each with a timestamped link."""
    types = ("video", "concept", "transcript") if kind == "all" else (kind,)
    if not set(types) <= {"video", "concept", "transcript"}:
        raise ToolError("kind must be all, transcript, concept or video")
    hits = search_mod.search(query, max(1, min(limit, 30)), types)
    return [{"type": h["type"], "video_id": h["vid"], "title": h["title"], "at": fmt_ts(h["t"]),
             "link": _link(h["vid"], h["t"]), "text": h["text"][:config.get("mcp.hit_chars")],
             **({"concept": h["slug"], "video_title": h["video_title"]} if h["type"] == "concept" else {})}
            for h in hits]


@server.tool(annotations=READ_ONLY)
def list_videos() -> list[dict]:
    """Every video in the library, newest first, with its one-paragraph summary."""
    return [{"video_id": v["id"], "title": v["title"], "channel": v["channel"], "duration_min":
             round((v["duration"] or 0) / 60), "uploaded": v["upload_date"], "summary": v["tldr"],
             "concepts": v["n_concepts"], "link": _link(v["id"])} for v in library() if v["ready"]]


@server.tool(annotations=READ_ONLY)
def get_video(video_id: str) -> dict:
    """One video's summary, key takeaways, outline (timestamped), speakers and concepts. Accepts an id or URL."""
    vid = _vid(video_id)
    d = DATA / vid
    meta, k = read_json(d / "meta.json"), read_json(d / "kb.json") if (d / "kb.json").exists() else {}
    return {"video_id": vid, "title": meta["title"], "channel": meta.get("channel"), "link": _link(vid),
            "duration_min": round((meta.get("duration") or 0) / 60), "summary": k.get("tldr"),
            "takeaways": k.get("takeaways", []), "tags": k.get("tags", []),
            "speakers": sorted(set(names(vid).values())),
            "outline": [{"at": fmt_ts(o["t"]), "title": o["title"], "link": _link(vid, o["t"])}
                        for o in k.get("outline", [])],
            "concepts": [{"name": c["name"], "concept": c["slug"], "summary": c["summary"]}
                         for c in k.get("concepts", [])]}


@server.tool(annotations=READ_ONLY)
def get_concept(name: str) -> dict:
    """A concept note: what each video says about it (summary, facts, timestamped links). Accepts the concept's
    name, an alias or its id from search results."""
    want = name.strip().lower()
    slug = next((s for s, c in concept_files().items()
                 if want in (s, c["name"].lower(), *(a.lower() for a in c["aliases"]))), None)
    c = concept(slug) if slug else None
    if not c:
        raise ToolError(f"no concept called {name!r}; try `search` with kind='concept'")
    return {"name": c["name"], "kind": c["kind"], "aliases": c["aliases"],
            "videos": [{"video_id": s["vid"], "title": s["title"], "summary": s["summary"], "facts": s["facts"],
                        "links": [_link(s["vid"], t) for t in s["times"]]} for s in c["sections"]]}


@server.tool(annotations=READ_ONLY)
def get_transcript(video_id: str, start: str = "0:00", minutes: float = 10) -> dict:
    """The cleaned transcript (ads and filler removed, speakers named) from `start` (m:ss or h:mm:ss) for up to
    `minutes`, at most about 10 minutes per call. `next_start` continues from where it stopped."""
    vid = _vid(video_id)
    f = DATA / vid / "clean.json"
    if not f.exists():
        raise ToolError(f"{vid} has no cleaned transcript yet")
    t0 = parse_ts(start) if ":" in start else float(start or 0)
    t1 = t0 + max(0.5, min(minutes, 30)) * 60
    lines, size, stop = [], 0, None
    for p in paragraphs(read_json(f)["sentences"], names(vid)):
        if p["start"] < t0:
            continue
        if p["start"] >= t1 or size > config.get("mcp.excerpt_chars"):
            stop = p["start"]
            break
        line = f"[{fmt_ts(p['start'])}] " + (f"{p['speaker']}: " if p["speaker"] else "") + p["text"]
        lines.append(line)
        size += len(line)
    return {"video_id": vid, "title": read_json(DATA / vid / "meta.json")["title"], "from": fmt_ts(t0),
            "text": "\n\n".join(lines), "next_start": fmt_ts(stop) if stop is not None else None}


# ------------------------------------------------------------------ finding and adding videos (through the app)

def _app(path: str, body: dict | None = None, missing: str = "not found") -> Any:
    """A request to the running app; a readable error when it isn't running or refuses."""
    port = int(os.environ.get("TKB_PORT") or 8765)
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method="GET" if body is None else "POST",
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise ToolError(missing) from e
        try:
            msg = json.loads(e.read()).get("error")
        except ValueError:
            msg = None
        raise ToolError(msg or f"the app refused: HTTP {e.code}") from e
    except OSError as e:
        raise ToolError(f"Transcript KB isn't running on port {port}: open it (Transcript KB.command, or "
                        "`tkb serve`) and try again") from e


def _discover_view(t: dict) -> dict:
    if t["state"] == "error":
        raise ToolError(f"Discover failed: {t['error']}")
    if t["state"] != "done":
        return {"status": "running", "task_id": t["id"], "progress": (t["progress"] or [None])[-1],
                "next": "call `get_discover_result` with this task_id in about a minute"}
    r = t["result"]
    return {"status": "done", "task_id": t["id"], "skipped": r.get("dropped", {}),
            "picks": [{"video_id": c["id"], "title": c["title"], "channel": c.get("channel"),
                       "duration_min": round((c.get("duration") or 0) / 60), "views": c.get("views"),
                       "uploaded": c.get("upload_date"), "score": c.get("score"), "reason": c.get("reason"),
                       "link": _link(c["id"])} for c in r["picks"]]}


@server.tool(annotations=CHANGES)
def discover(searches: list[str], picks: int = 10, only_new: bool = True, date: str = "any",
             duration: str = "any", sort: str = "relevance") -> dict:
    """Find the best YouTube videos to add, with the app's Discover: each search (keywords, or a channel as @handle
    or URL) also runs as AI-written phrasings; Shorts, live streams and videos already in the library are skipped;
    each candidate is read and scored by the AI, and the best `picks` come back with a reason each. Filters for
    every search: `date` any/today/week/month/year, `duration` any/short (under 4 min)/medium (4-20)/long (over
    20), `sort` relevance/popularity. `only_new` skips videos these searches offered before. Nothing is queued:
    show the picks, then `queue_videos`. Spends AI credits and takes about a minute; if it isn't done by then,
    it returns a task_id for `get_discover_result`."""
    if not searches:
        raise ToolError("add at least one search")
    task = _app("/api/tasks", {"kind": "discover", "picks": picks, "only_new": only_new, "searches": [
        {"topic": s, "date": date, "duration": duration, "sort": sort} for s in searches]})
    end = time.monotonic() + DISCOVER_WAIT
    while task["state"] in ("queued", "running") and time.monotonic() < end:
        time.sleep(min(1.0, max(0.0, end - time.monotonic())))
        task = _app(f"/api/tasks/{task['id']}")
    return _discover_view(task)


@server.tool(annotations=READ_ONLY)
def get_discover_result(task_id: str) -> dict:
    """The result of a `discover` that was still running: its picks when done, or its latest progress."""
    missing = f"no Discover task {task_id!r}"
    t = _app(f"/api/tasks/{task_id}", missing=missing) if task_id.isalnum() else None
    if not t or t["kind"] != "discover":
        raise ToolError(missing)
    return _discover_view(t)


@server.tool(annotations=CHANGES)
def queue_videos(video_ids: list[str]) -> dict:
    """Add videos to the app's queue to be downloaded, transcribed, cleaned and turned into notes (a few minutes
    each; `queue_status` follows them). Takes video ids or video links, one video each: not channels or playlists.
    Videos already in the library or queue are skipped."""
    vids = []
    for v in video_ids:
        try:
            vids.append(video_id_from(v.strip()))
        except ValueError as e:
            raise ToolError(f"{v!r} isn't one video: queue_videos takes video ids or links, not channels or "
                            "playlists (use `discover` to choose from those)") from e
    if not vids:
        raise ToolError("no videos given")
    r = _app("/api/jobs", {"text": " ".join(dict.fromkeys(vids)), "sponsorblock": config.get("sponsorblock"),
                           "translate": config.get("translate")})
    if r.get("needs_confirm"):
        raise ToolError(f"{r['total']} videos is more than the app queues at once without asking; send fewer")
    return {"added": r["added"], "skipped": r["skipped"]}


@server.tool(annotations=READ_ONLY)
def queue_status() -> list[dict]:
    """The app's processing queue: each video's state (queued, running, done, error), current stage and error.
    A done video can be read with `get_video` and `get_transcript`."""
    return [{"video_id": j["vid"], "title": j["title"], "state": j["state"], "stage": j["stage"],
             "error": j["error"]} for j in _app("/api/jobs")]


def main() -> None:
    threading.Thread(target=search_mod.warm, name="embed", daemon=True).start()  # index new text in the background
    server.run("stdio")
