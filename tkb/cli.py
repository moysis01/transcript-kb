"""The `tkb` command line: process videos, serve the web app, ask, discover, delete, update yt-dlp, serve MCP,
and watch topics."""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from typing import Any

import truststore

from tkb import config
from tkb.app.server import serve
from tkb.config import ROOT
from tkb.core import log as logs
from tkb.core.text import video_id_from
from tkb.pipeline.runner import STAGES, exit_with_parent, run_many, run_video
from tkb.services import search, watch
from tkb.services.delete import delete, preview
from tkb.services.discover import discover
from tkb.services.sources import expand

truststore.inject_into_ssl()

def _settle(a) -> None:
    """Choices not given on the command line come from Settings (models stay unset: each task looks up its own)."""
    for name in ("translate", "sponsorblock", "speakers"):
        if getattr(a, name) is None:
            setattr(a, name, config.get(name))
    a.jobs = a.jobs or config.get("jobs")


def cmd_run(a) -> int:
    _settle(a)
    if a.jsonl:
        exit_with_parent()
        emit = lambda ev, **kw: print(json.dumps({"event": ev, **kw}), flush=True)
    else:
        def emit(ev: str, **kw: Any) -> None:
            if ev == "stage":
                print(f"[{kw['stage']}] {kw['vid']}")
            elif ev == "progress":
                print(f"  {kw['stage']} {kw['done']}/{kw['total']}", end="\r" if kw["done"] < kw["total"] else "\n")
            elif ev in ("info", "warning", "error"):
                print(f"  {'! ' if ev != 'info' else ''}{kw.get('msg') or kw.get('error')}")
            elif ev == "meta":
                print(f"  {kw['title']} ({kw['duration']}s)")
            elif ev == "stage_done":
                waited = f" ({kw['waited']}s of it waiting for a free slot)" if kw.get("waited") else ""
                print(f"  {kw['stage']} done in {kw['seconds']}s{waited}")
    entries, total = expand(" ".join(a.urls), a.limit)
    if total > len(entries):
        emit("warning", vid="",
             msg=f"found {total} videos, processing the first {len(entries)} (use --limit 0 for all)")
    if len(entries) > 1 and a.jobs > 1 and not a.jsonl:
        return run_many([e["id"] for e in entries], a, emit)
    failed = 0
    for e in entries:
        try:
            run_video(e["id"], a, emit)
        except Exception as ex:  # keep going through a batch
            failed += 1
            emit("error", vid=e["id"], error=f"{type(ex).__name__}: {ex}")
    return 1 if failed else 0


def cmd_serve(a) -> int:
    serve(a.port, open_browser=not a.no_open, host=a.host)
    return 0


def cmd_discover(a) -> int:
    searches = [{"topic": t, "duration": a.duration, "date": a.date, "sort": a.sort} for t in a.topics]
    r = discover(searches, a.pick, only_new=not a.all, model=a.llm_model, progress=lambda m: print(f"  {m}..."))
    for s, qs in zip(r["searches"], r["queries"], strict=True):
        print(f"\n{s['topic']!r} searched as: {' | '.join(qs)}")
    if r["dropped"]:
        print("Skipped: " + ", ".join(f"{n} {why}" for why, n in r["dropped"].items()))
    if not r["picks"]:
        print("No suitable videos found.")
        return 1
    for c in r["picks"]:
        print(f"\n  [{c['score']}] {c['title']}  ({c['channel']}, {round((c['duration'] or 0) / 60)} min)\n"
              f"       https://youtu.be/{c['id']}\n       {c['reason']}")
    print(f"\n{len(r['picks'])} videos, about {r['minutes']} min of audio.")
    if a.run:
        a.urls, a.stage, a.jsonl, a.limit, a.redo = [c["id"] for c in r["picks"]], "all", False, 0, False
        a.redo_kb, a.num_speakers, a.speakers, a.translate = False, None, None, None
        a.asr_model, a.force_asr, a.jobs = None, False, None
        return cmd_run(a)
    print("Add --run to process them, or open the app (tkb serve) to review and queue.")
    return 0


def cmd_delete(a) -> int:
    import urllib.error
    import urllib.request
    base = f"http://127.0.0.1:{a.port}/api/"
    server = _app_jobs(a.port) is not None  # if the app is running, delete through it so its queue forgets it too
    failed = 0
    for u in a.videos:
        try:
            vid = video_id_from(u)
            p = preview(vid)
        except (ValueError, FileNotFoundError):
            print(f"{u}: not in the library")
            failed += 1
            continue
        print(f"{p['title']} ({vid}): {p['mb']} MB of files, {p['concepts_deleted']} concept notes deleted, "
              f"{p['concepts_trimmed']} shared concept notes lose this video's section.")
        if not a.yes and input("Delete it permanently? [y/N] ").strip().lower() != "y":
            print("  kept")
            continue
        done = False
        if server:
            req = urllib.request.Request(base + f"video/{vid}/delete", data=b"{}", method="POST",
                                         headers={"Content-Type": "application/json"})
            try:
                urllib.request.urlopen(req, timeout=60)
                done = True
            except urllib.error.HTTPError:  # an app started before this feature existed: delete directly
                pass
        if not done:
            delete(vid)
        print("  deleted")
    return 1 if failed else 0


def cmd_ask(a) -> int:
    if (n := search.pending()) > 50:
        print(f"Indexing {n} passages for meaning-based search (once; the app does this in the background)...",
              file=sys.stderr)
    search.warm()
    res = search.ask(a.question, model=a.llm_model)
    print(res["answer"])
    if res["dropped_citations"]:
        print(f"\n(! removed {len(res['dropped_citations'])} citations not found in the sources)")
    return 0


def _processes(pattern: str) -> list[str]:
    """Command lines of other processes matching `pattern` (a `pgrep -f` regex)."""
    out = subprocess.run(["pgrep", "-fl", pattern], capture_output=True, text=True).stdout
    return [line for line in out.splitlines() if not line.startswith(f"{os.getpid()} ")]


def _ytdlp_version() -> str:
    """Asked of a fresh interpreter: this process still has the old version loaded."""
    return subprocess.run([sys.executable, "-c", "import importlib.metadata as m; print(m.version('yt-dlp'))"],
                          capture_output=True, text=True).stdout.strip() or "not installed"


def cmd_update(a) -> int:
    """YouTube changes break older yt-dlp releases every few weeks; upgrading is usually the whole fix."""
    uv = shutil.which("uv")
    if not uv:
        print("uv not found: install it (https://docs.astral.sh/uv/), then run `tkb update` again.")
        return 1
    busy = _processes(r"tkb(\.cli)? run ")
    if busy and not a.force:
        print(f"{len(busy)} video(s) are being processed right now; updating under them could break them.\n"
              "Wait for them to finish (or cancel them in the app), or use --force.")
        return 1
    old = _ytdlp_version()
    for cmd in ([uv, "lock", "--upgrade-package", "yt-dlp"], [uv, "sync"]):
        if subprocess.run(cmd, cwd=ROOT).returncode:
            print(f"`{' '.join(cmd[1:])}` failed; nothing else was changed.")
            return 1
    new = _ytdlp_version()
    print(f"yt-dlp {old} -> {new}" if new != old else f"yt-dlp {new} is already the latest.")
    if new != old and _processes(r"tkb(\.cli)? serve"):
        print("Restart the app (tkb serve) so it uses the new version too.")
    return 0


def _app_jobs(port: int) -> list[dict] | None:
    """The running app's queue, or None when the app isn't running on that port."""
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/jobs", timeout=3) as r:
            return json.loads(r.read())
    except OSError:
        return None


def cmd_watch(a) -> int:
    if a.run_due:
        return _run_due(a)
    if a.topic:
        t = watch.set_watch(a.topic, None if a.off else a.every, a.pick or config.get("watch.picks"))
        print(f"Stopped watching {t['topic']!r}." if a.off else
              f"Watching {t['topic']!r}: every {t['watch']['every_days']} days, up to {t['watch']['picks']} "
              "new videos each time. The app runs it while open; see `tkb watch --help` for running it from cron.")
        return 0
    ts = watch.watched()
    if not ts:
        print('No watched topics. Watch one with: tkb watch "your topic" --every 7')
    for t in ts:
        w, res = t["watch"], t["watch"].get("result") or {}
        last = (f"last run queued {res['queued']}" if "error" not in res else f"last run failed: {res['error']}") \
            if res else "not run yet"
        when = "due now" if t["next"] <= time.time() else f"next in {(t['next'] - time.time()) / 86400:.1f} days"
        print(f"{t['topic']!r}: every {w['every_days']} days, up to {w['picks']} videos · {when} · {last}")
    return 0


def _run_due(a) -> int:
    """Run the due topics. With the app open, picks go to its queue; otherwise they're processed right here."""
    jobs = _app_jobs(a.port)
    picked: list[dict] = []

    def queue(picks: list[dict]) -> int:
        if jobs is None:
            picked.extend(picks)
            return len(picks)
        import urllib.request
        body = {"text": " ".join(c["id"] for c in picks), "titles": {c["id"]: c["title"] for c in picks},
                "sponsorblock": config.get("sponsorblock"), "translate": config.get("translate")}
        req = urllib.request.Request(f"http://127.0.0.1:{a.port}/api/jobs", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())["added"]

    active = lambda: {j["vid"] for j in _app_jobs(a.port) or [] if j["state"] in ("queued", "running")}
    results = watch.run_due(queue, active, progress=print)
    if picked:
        a.urls, a.stage, a.jsonl, a.limit, a.redo, a.redo_kb = [c["id"] for c in picked], "all", False, 0, False, False
        a.num_speakers, a.speakers, a.translate, a.sponsorblock = None, None, None, None
        a.asr_model, a.force_asr, a.jobs, a.llm_model = None, False, None, None
        return cmd_run(a)
    return 1 if any("error" in r for r in results) else 0


def cmd_mcp(a) -> int:
    from tkb.app.mcp_server import main as serve_mcp  # imported here: video jobs never load the MCP SDK
    serve_mcp()
    return 0


COMMANDS = ("run", "serve", "ask", "discover", "delete", "update", "mcp", "watch")


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    logs.to_stderr()
    if argv and argv[0] not in (*COMMANDS, "-h", "--help"):
        argv = ["run", *argv]  # `tkb URL` keeps working
    a = build_parser().parse_args(argv)
    sys.exit(a.fn(a))


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="tkb", description="YouTube video -> transcript -> knowledge base")
    sub = ap.add_subparsers(dest="cmd", required=True)
    _add_run(sub)
    _add_serve(sub)
    _add_ask(sub)
    _add_discover(sub)
    _add_delete(sub)
    _add_update(sub)
    _add_mcp(sub)
    _add_watch(sub)
    return ap


def _add_run(sub: argparse._SubParsersAction) -> None:
    r = sub.add_parser("run", help="process videos, playlists or channels")
    r.add_argument("urls", nargs="+", help="YouTube URLs (video, playlist, channel) or 11-char video ids")
    r.add_argument("--stage", choices=STAGES + ["all"], default="all",
                   help="run a single stage (earlier stages must already be done)")
    r.add_argument("--asr-model", default=None,
                   help="speech model: turbo, large-v3, or an mlx repo id (default: Settings, turbo)")
    r.add_argument("--llm-model", default=None,
                   help="AI model for every task, as provider/model (default: Settings, per task)")
    r.add_argument("--force-asr", action="store_true", help="re-transcribe even if cached")
    r.add_argument("--sponsorblock", action=argparse.BooleanOptionalAction, default=None,
                   help="cut community-labelled ad segments from sponsor.ajay.app (default: Settings, on)")
    r.add_argument("--redo", action="store_true",
                   help="redo noise removal and knowledge base (keeps audio + transcript)")
    r.add_argument("--redo-kb", action="store_true", help="redo only the knowledge base (e.g. after renaming speakers)")
    r.add_argument("--num-speakers", type=int, default=None,
                   help="number of speakers, if you know it (default: detect)")
    r.add_argument("--speakers", action=argparse.BooleanOptionalAction, default=None,
                   help="identify speakers (default: Settings, on)")
    r.add_argument("--translate", action=argparse.BooleanOptionalAction, default=None,
                   help="translate non-English videos to English (default: Settings, off)")
    r.add_argument("--limit", type=int, default=50, help="max videos taken from a playlist/channel (0 = all)")
    r.add_argument("--jsonl", action="store_true", help="machine-readable progress events on stdout")
    r.add_argument("--jobs", type=int, default=None, help="videos processed at the same time (default: Settings, 8)")
    r.set_defaults(fn=cmd_run)


def _add_serve(sub: argparse._SubParsersAction) -> None:
    s = sub.add_parser("serve", help="open the web app")
    s.add_argument("--port", type=int, default=int(os.environ.get("TKB_PORT") or 8765),
                   help="default 8765, or TKB_PORT")
    s.add_argument("--no-open", action="store_true", help="don't open a browser tab")
    s.add_argument("--host", default="127.0.0.1",
                   help="address to listen on: 0.0.0.0 only inside a container published on 127.0.0.1")
    s.set_defaults(fn=cmd_serve)


def _add_ask(sub: argparse._SubParsersAction) -> None:
    q = sub.add_parser("ask", help="ask a question answered from the knowledge base")
    q.add_argument("question")
    q.add_argument("--llm-model", default=None, help="default: Settings")
    q.set_defaults(fn=cmd_ask)


def _add_discover(sub: argparse._SubParsersAction) -> None:
    d = sub.add_parser("discover", help="find the best videos for one or more searches and (optionally) process them")
    d.add_argument("topics", nargs="+", help="one or more searches, each as you'd type it into YouTube (quote each)")
    d.add_argument("--pick", type=int, default=5,
                   help="how many videos to pick (at most Settings → Discover → Most picks)")
    d.add_argument("--duration", choices=["any", "short", "medium", "long"], default="any",
                   help="short <4 min, medium 4-20 min, long >20 min")
    d.add_argument("--date", choices=["any", "today", "week", "month", "year"], default="any")
    d.add_argument("--sort", choices=["relevance", "popularity"], default="relevance")
    d.add_argument("--all", action="store_true", help="also offer videos this topic offered before")
    d.add_argument("--run", action="store_true", help="process the picks right away")
    d.add_argument("--sponsorblock", action=argparse.BooleanOptionalAction, default=None)
    d.add_argument("--llm-model", default=None, help="default: Settings")
    d.set_defaults(fn=cmd_discover)


def _add_delete(sub: argparse._SubParsersAction) -> None:
    x = sub.add_parser("delete", help="delete videos and every trace of them from the knowledge base")
    x.add_argument("videos", nargs="+", help="YouTube URLs or video ids")
    x.add_argument("--yes", action="store_true", help="don't ask for confirmation")
    x.add_argument("--port", type=int, default=8765, help="port of a running app, if any")
    x.set_defaults(fn=cmd_delete)


def _add_update(sub: argparse._SubParsersAction) -> None:
    u = sub.add_parser("update", help="upgrade yt-dlp, the YouTube downloader (fixes most download failures)")
    u.add_argument("--force", action="store_true", help="update even while videos are being processed")
    u.set_defaults(fn=cmd_update)


def _add_mcp(sub: argparse._SubParsersAction) -> None:
    m = sub.add_parser("mcp", help="serve the knowledge base to AI apps over MCP (Claude, Cursor...), read-only")
    m.set_defaults(fn=cmd_mcp)


WATCH_HELP = """Without arguments, lists watched topics. The app runs due topics while it's open. To run them when it
isn't, schedule `tkb watch --run-due` (e.g. hourly with cron or launchd); runs never double up."""


def _add_watch(sub: argparse._SubParsersAction) -> None:
    w = sub.add_parser("watch", help="follow a topic: Discover runs every few days and queues new videos",
                       description=WATCH_HELP)
    w.add_argument("topic", nargs="?", help="the search to follow, as you'd type it into YouTube")
    w.add_argument("--every", type=int, default=7, help="days between runs (1-90, default 7)")
    w.add_argument("--pick", type=int, default=None, help="new videos queued per run (1-10, default: Settings, 3)")
    w.add_argument("--off", action="store_true", help="stop watching the topic")
    w.add_argument("--run-due", action="store_true", help="run the topics that are due now")
    w.add_argument("--port", type=int, default=8765, help="port of the app, if it's running")
    w.set_defaults(fn=cmd_watch)



if __name__ == "__main__":
    main()
