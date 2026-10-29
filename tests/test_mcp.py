"""The MCP server: read-only tools over the library, and a stdout that carries nothing but the protocol."""
import asyncio
import json
import os
import subprocess
import sys
import threading

from tkb import config
from tkb.core.storage import write_json

VID = "VVVVVVVVVV1"


def _library() -> None:
    d = config.DATA / VID
    write_json(d / "meta.json", {"id": VID, "title": "Jev explained", "channel": "C", "duration": 600,
                                 "upload_date": "20260901"})
    write_json(d / "kb.json", {"slug": "jev-explained", "tldr": "What Jev is.", "takeaways": ["It classifies."],
                               "tags": ["ai"], "outline": [{"t": 65, "title": "Pricing"}], "speakers": [],
                               "concepts": [{"name": "Confidence Score", "slug": "confidence-score", "kind": "concept",
                                             "summary": "How sure it is.", "facts": ["Calibrated."], "times": [65],
                                             "aliases": ["calibration"]}]})
    sents = [{"id": i, "start": i * 60.0, "end": i * 60.0 + 5, "para": i, "src": t, "text": t, "by": "llm"}
             for i, t in enumerate(["Welcome.", "Pricing is four cents per million tokens.", "Bye."])]
    write_json(d / "clean.json", {"sponsorblock": "none", "sentences": sents, "warnings": []})
    (config.KB / "concepts").mkdir(parents=True, exist_ok=True)
    (config.KB / "concepts" / "confidence-score.md").write_text(
        f'---\nname: "Confidence Score"\nkind: concept\naliases: ["calibration"]\n---\n\n# Confidence Score\n\n'
        f"<!-- src:{VID} -->\nx\n<!-- /src:{VID} -->\n")


def _call(tool: str, args: dict):
    from mcp import Client

    from tkb.app.mcp_server import server

    async def go():
        async with Client(server) as c:
            return await c.call_tool(tool, args)
    r = asyncio.run(go())
    return r.is_error, (json.loads(r.content[0].text) if not r.is_error and len(r.content) == 1 else
                        [json.loads(c.text) for c in r.content] if not r.is_error else r.content[0].text)


def test_tools_read_the_library_with_timestamped_links():
    _library()
    err, hits = _call("search", {"query": "pricing tokens", "kind": "transcript"})
    assert not err and hits["text"].startswith("Pricing") and hits["link"] == f"https://youtu.be/{VID}?t=60"
    err, v = _call("get_video", {"video_id": f"https://www.youtube.com/watch?v={VID}"})
    assert not err and v["outline"][0]["link"].endswith("?t=65") and v["concepts"][0]["concept"] == "confidence-score"
    err, c = _call("get_concept", {"name": "Calibration"})  # by alias
    assert not err and c["videos"][0]["links"] == [f"https://youtu.be/{VID}?t=65"]
    err, t = _call("get_transcript", {"video_id": VID, "start": "1:00", "minutes": 1})
    assert not err and t["text"] == "[01:00] Pricing is four cents per million tokens." and t["next_start"] == "02:00"


def test_mistakes_come_back_as_readable_errors():
    _library()
    err, msg = _call("get_video", {"video_id": "ZZZZZZZZZZ9"})
    assert err and "not in the library" in msg
    err, msg = _call("get_concept", {"name": "nothing like this"})
    assert err and "no concept" in msg


def test_stdout_carries_only_the_protocol():
    """Launch `tkb mcp` as Claude would and talk JSON-RPC to it: every stdout line must be a JSON-RPC message."""
    _library()
    p = subprocess.Popen([sys.executable, "-m", "tkb.cli", "mcp"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True, env={**os.environ, "TKB_EMBED": "off"})
    msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "list_videos", "arguments": {}}}]
    assert p.stdin and p.stdout
    guard = threading.Timer(60, p.kill)  # a server that stops answering fails the test instead of hanging it
    guard.start()
    lines = []
    try:
        for m in msgs:
            p.stdin.write(json.dumps(m) + "\n")
            p.stdin.flush()
            if "id" in m:  # wait for its answer, as a real client does
                while (line := p.stdout.readline()) and json.loads(line).get("id") != m["id"]:
                    lines.append(json.loads(line))  # fails on any stray print
                lines.append(json.loads(line))
    finally:
        guard.cancel()
        p.kill()
        p.wait()
    assert all(m.get("jsonrpc") == "2.0" for m in lines)
    by_id = {m.get("id"): m for m in lines}
    tools = {t["name"]: t["annotations"] for t in by_id[2]["result"]["tools"]}
    reads = {"search", "list_videos", "get_video", "get_concept", "get_transcript", "get_discover_result",
             "queue_status"}
    assert set(tools) == reads | {"discover", "queue_videos"}
    assert all(tools[t]["readOnlyHint"] for t in reads)
    # the two that spend AI credits or add to the queue: the AI app asks before using them, and nothing deletes
    assert all(not tools[t]["readOnlyHint"] and not tools[t]["destructiveHint"] for t in ("discover", "queue_videos"))
    assert VID in by_id[3]["result"]["content"][0]["text"]


# ------------------------------------------------------------------ finding and adding videos (through the app)

PICK = {"id": "AAAAAAAAAA1", "title": "Mods explained", "channel": "C", "duration": 900, "views": 1200,
        "upload_date": "20261002", "score": 88, "reason": "Deep and recent.", "searches": ["claude mods"]}


def _discover_result(searches, picks, *a, **k):
    return {"searches": searches, "queries": [["claude mods"]], "picks": [PICK], "others": [], "dropped": {},
            "minutes": 15}


def test_discover_runs_in_the_app_and_returns_its_picks(app, monkeypatch):
    from tkb.app import server
    monkeypatch.setenv("TKB_PORT", str(app))
    monkeypatch.setattr(server.discover_mod, "discover", _discover_result)
    err, r = _call("discover", {"searches": ["claude mods"], "picks": 3, "date": "month"})
    assert not err and r["status"] == "done"
    assert r["picks"] == [{"video_id": "AAAAAAAAAA1", "title": "Mods explained", "channel": "C", "duration_min": 15,
                           "views": 1200, "uploaded": "20261002", "score": 88, "reason": "Deep and recent.",
                           "link": "https://youtu.be/AAAAAAAAAA1"}]
    saved = server.Handler.tasks.get(r["task_id"])  # it's the app's own Discover, kept in its history
    assert saved["params"]["searches"][0]["date"] == "month" and saved["params"]["picks"] == 3


def test_a_slow_discover_hands_back_a_task_to_check_later(app, monkeypatch):
    from tkb.app import mcp_server, server
    monkeypatch.setenv("TKB_PORT", str(app))
    monkeypatch.setattr(mcp_server, "DISCOVER_WAIT", 0.2)
    go = threading.Event()
    monkeypatch.setattr(server.discover_mod, "discover", lambda *a, **k: go.wait(10) and _discover_result(*a, **k))
    err, r = _call("discover", {"searches": ["claude mods"]})
    assert not err and r["status"] == "running" and r["task_id"]
    err, later = _call("get_discover_result", {"task_id": r["task_id"]})
    assert not err and later["status"] == "running"
    go.set()
    for _ in range(50):
        err, later = _call("get_discover_result", {"task_id": r["task_id"]})
        if later["status"] != "running":
            break
        threading.Event().wait(0.1)
    assert later["status"] == "done" and later["picks"][0]["video_id"] == "AAAAAAAAAA1"
    err, msg = _call("get_discover_result", {"task_id": "nope"})
    assert err and "no Discover task" in msg


def test_queue_videos_takes_single_videos_and_reports_what_it_skipped(app, monkeypatch):
    monkeypatch.setenv("TKB_PORT", str(app))
    err, r = _call("queue_videos", {"video_ids": ["AAAAAAAAAA1", "https://youtu.be/BBBBBBBBBB2"]})
    assert not err and r == {"added": 2, "skipped": 0}
    err, r = _call("queue_videos", {"video_ids": ["AAAAAAAAAA1"]})
    assert not err and r == {"added": 0, "skipped": 1}  # already queued
    for whole in ("@somechannel", "https://www.youtube.com/playlist?list=PL1234567890"):
        err, msg = _call("queue_videos", {"video_ids": [whole]})
        assert err and "one video" in msg
    err, q = _call("queue_status", {})
    assert not err and [(j["video_id"], j["state"]) for j in q] == [("AAAAAAAAAA1", "queued"),
                                                                    ("BBBBBBBBBB2", "queued")]


def test_tools_that_need_the_app_say_so_when_it_is_closed(monkeypatch):
    import socket
    with socket.socket() as s:  # a port nothing listens on
        s.bind(("127.0.0.1", 0))
        monkeypatch.setenv("TKB_PORT", str(s.getsockname()[1]))
    for tool, args in (("discover", {"searches": ["x"]}), ("queue_videos", {"video_ids": ["AAAAAAAAAA1"]}),
                       ("queue_status", {})):
        err, msg = _call(tool, args)
        assert err and "isn't running" in msg, tool
