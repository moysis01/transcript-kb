"""Local web app: queue videos, watch progress, browse and query the knowledge base.

Bound to 127.0.0.1 only. Requests must carry a local Host header (blocks DNS rebinding),
a local Origin when one is sent, and POSTs must be application/json (forces a CORS
preflight, which is never answered), so other websites can't drive the pipeline.
"""
import json
import re
import signal
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from tkb import config
from tkb.app.jobs import Jobs
from tkb.app.scheduler import Scheduler
from tkb.app.tasks import Tasks
from tkb.app.views import concept, concept_files, graph, library, video
from tkb.config import DATA
from tkb.core import log as logs
from tkb.core import resources
from tkb.core.storage import read_json, write_json
from tkb.pipeline import kb
from tkb.pipeline.clean import render
from tkb.services import discover as discover_mod
from tkb.services import search, settings
from tkb.services.delete import delete, exists, preview
from tkb.services.sources import expand

log = logs.get("app")
WEB = Path(__file__).parent / "web"
STATIC = {"/": ("index.html", "text/html"), "/app.css": ("app.css", "text/css")}
JS_MODULE = re.compile(r"/js/([a-z]+)\.js")  # the UI's ES modules; the strict pattern rules out "..", slashes etc.



def _untranslated(vid: str) -> bool:
    """A transcript in a language other than English that hasn't been translated."""
    f = DATA / vid / "transcript.json"
    try:
        t = read_json(f) if f.exists() else {}
    except ValueError:
        return False
    return t.get("language") not in (None, "en") and t.get("task") != "translate"


def _task_params(body: dict) -> dict:
    """Validate a task request up front, so a bad request fails now rather than in the background."""
    kind = body.get("kind")
    if kind == "ask":
        q = " ".join(str(body.get("question", "")).split())
        if not q:
            raise ValueError("empty question")
        return {"question": q[:2000]}
    if kind == "discover":
        searches = [discover_mod.normalize(s) for s in body.get("searches") or []][:8]
        if not searches:
            raise ValueError("add at least one search")
        return {"searches": searches, "picks": max(1, min(int(body.get("picks", 5)), config.get("discover.max_picks"))),
                "only_new": bool(body.get("only_new", True)), "auto_queue": bool(body.get("auto_queue")),
                "sponsorblock": bool(body.get("sponsorblock", True)), "translate": bool(body.get("translate"))}
    raise ValueError("unknown task kind")


def _runners(jobs: Jobs) -> dict:
    def run_discover(p: dict, progress) -> dict:
        r = discover_mod.discover(p["searches"], p["picks"], p["only_new"], exclude=jobs.active_vids(),
                                  progress=progress)
        if p["auto_queue"] and r["picks"]:  # done server-side, so it happens even if the tab was closed
            active = jobs.active_vids()
            new = [c for c in r["picks"] if c["id"] not in active and not (DATA / c["id"] / "kb.json").exists()]
            for c in new:
                jobs.add(c["id"], c["title"], p["sponsorblock"], translate=p.get("translate", False))
            r["queued"] = len(new)
            progress(f"Queued {len(new)} video(s) for processing")
        return r

    def run_ask(p: dict, progress) -> dict:
        progress("Reading your knowledge base")
        return search.ask(p["question"])

    return {"discover": run_discover, "ask": run_ask}


Reply = tuple[int, Any]
NOT_FOUND: Reply = (404, {"error": "not found"})
VID = r"([A-Za-z0-9_-]{11})"


class Handler(BaseHTTPRequestHandler):
    """Routes requests by method and path (the tables below the class) to small `get_*`/`post_*` methods.
    Each returns (status, body); an unexpected error becomes a logged 500 instead of a dropped connection."""
    jobs: Jobs
    tasks: Tasks
    port: int

    def log_message(self, *a: Any) -> None:  # requests aren't logged; failures are (see _dispatch)
        pass

    def _allowed(self) -> bool:
        hosts = {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}
        origin = self.headers.get("Origin")
        return self.headers.get("Host") in hosts and (origin is None or origin in {f"http://{h}" for h in hosts})

    def _send(self, code: int, body: Any, ctype: str = "application/json") -> None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _dispatch(self, routes: list[tuple[re.Pattern, str]], path: str, *args: Any) -> None:
        for pattern, method in routes:
            if m := pattern.fullmatch(path):
                try:
                    code, body = getattr(self, method)(*args, *m.groups())
                except Exception:  # noqa: BLE001 - one bad request must not take the page down silently
                    log.exception("%s %s failed", self.command, path)
                    code, body = 500, {"error": "internal error (details in data/.logs/tkb.log)"}
                return self._send(code, body)
        self._send(*NOT_FOUND)

    def do_GET(self) -> None:
        if not self._allowed():
            return self._send(403, {"error": "forbidden"})
        u = urlparse(self.path)
        if u.path in STATIC:
            f, ct = STATIC[u.path]
            return self._send(200, (WEB / f).read_bytes(), ct)
        if (m := JS_MODULE.fullmatch(u.path)) and (WEB / "js" / f"{m.group(1)}.js").is_file():
            return self._send(200, (WEB / "js" / f"{m.group(1)}.js").read_bytes(), "text/javascript")
        self._dispatch(GET_ROUTES, u.path, parse_qs(u.query))

    def do_POST(self) -> None:
        if not self._allowed() or not self.headers.get("Content-Type", "").startswith("application/json"):
            return self._send(403, {"error": "forbidden"})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        except json.JSONDecodeError:
            return self._send(400, {"error": "bad json"})
        self._dispatch(POST_ROUTES, urlparse(self.path).path, body)

    # ------------------------------------------------------------------ GET
    def get_library(self, qs: dict) -> Reply:
        return 200, library()

    def get_jobs(self, qs: dict) -> Reply:
        return 200, self.jobs.public()

    def get_concepts(self, qs: dict) -> Reply:
        return 200, sorted(concept_files().values(), key=lambda c: c["name"].lower())

    def get_tasks(self, qs: dict) -> Reply:
        return 200, self.tasks.list(qs.get("kind", [None])[0], int(qs.get("limit", ["20"])[0]))

    def get_task(self, qs: dict, tid: str) -> Reply:
        t = self.tasks.get(tid)
        return (200, t) if t else NOT_FOUND

    def get_graph(self, qs: dict) -> Reply:
        return 200, graph()

    def get_search(self, qs: dict) -> Reply:
        return 200, search.search(qs.get("q", [""])[0], config.get("search.results"))

    def get_delete_preview(self, qs: dict, vid: str) -> Reply:
        try:
            return 200, preview(vid)
        except FileNotFoundError:
            return NOT_FOUND

    def get_video(self, qs: dict, vid: str) -> Reply:
        v = video(vid)
        return (200, v) if v else NOT_FOUND

    def get_concept(self, qs: dict, slug: str) -> Reply:
        c = concept(unquote(slug))
        return (200, c) if c else NOT_FOUND

    # ------------------------------------------------------------------ POST
    def post_jobs(self, body: dict) -> Reply:
        try:
            entries, _ = expand(body.get("text", ""), limit=0)
        except Exception as err:  # noqa: BLE001 - shown to the user as "could not read that link"
            return 400, {"error": f"could not read that link: {err}"}
        if not entries:
            return 400, {"error": "no YouTube links or video ids found"}
        if len(entries) > config.get("confirm_above") and not body.get("confirm"):
            return 200, {"needs_confirm": True, "total": len(entries)}
        opts: dict[str, Any] = {}
        if body.get("stage") in ("speakers", "kb"):
            opts["stage"] = body["stage"]
        if body.get("num_speakers"):
            opts["num_speakers"] = max(1, min(int(body["num_speakers"]), 8))
        if body.get("redo_kb"):
            opts["redo_kb"] = True
        added, skipped = [], []
        active = self.jobs.active_vids()
        for entry in entries:
            vid = entry["id"]
            done = (DATA / vid / "kb.json").exists()
            # a finished non-English video still gets a job when translation is now asked for
            retranslate = bool(body.get("translate")) and _untranslated(vid)
            if vid in active or (done and not body.get("force") and not retranslate):
                skipped.append(vid)
                continue
            title = str((body.get("titles") or {}).get(vid) or entry["title"] or "")[:300] or None
            added.append(self.jobs.add(vid, title, body.get("sponsorblock", True),
                                       redo=done and bool(body.get("force")) and not opts, opts=opts,
                                       translate=bool(body.get("translate")))["id"])
        return 200, {"added": len(added), "skipped": len(skipped)}

    def post_delete(self, body: dict, vid: str) -> Reply:
        if not exists(vid):
            return NOT_FOUND
        self.jobs.forget(vid)
        log.info("deleted video %s", vid)
        return 200, delete(vid)

    def post_speakers(self, body: dict, vid: str) -> Reply:
        f = DATA / vid / "speakers.json"
        if not f.exists():
            return 404, {"error": "no speakers for this video"}
        sp = read_json(f)
        for k, name in (body.get("names") or {}).items():
            if k in sp["speakers"]:
                name = " ".join(str(name).split())[:80]
                if name:
                    sp["overrides"][k] = name
                else:
                    sp["overrides"].pop(k, None)
        write_json(f, sp)
        if (DATA / vid / "clean.json").exists():
            render(vid)
        if (DATA / vid / "kb.json").exists():
            kb.build(vid)  # cached notes and synthesis: no LLM calls, just rewrites the notes
        return 200, video(vid)

    def post_cancel(self, body: dict, jid: str) -> Reply:
        return 200, {"ok": self.jobs.cancel(jid)}

    def post_clear(self, body: dict) -> Reply:
        self.jobs.clear_finished()
        return 200, {"ok": True}

    def get_settings(self, qs: dict) -> Reply:
        return 200, settings.page()

    def get_defaults(self, qs: dict) -> Reply:
        return 200, settings.defaults()

    def post_settings(self, body: dict) -> Reply:
        try:
            page = settings.save(body.get("values") or {})
        except (TypeError, ValueError) as err:
            return 400, {"error": str(err)}
        log.info("settings changed: %s", ", ".join(sorted(body["values"])))
        return 200, page

    def post_provider_test(self, body: dict) -> Reply:
        try:
            return 200, settings.test(str(body.get("provider", "")))
        except ValueError as err:
            return 400, {"error": str(err)}

    def post_tasks(self, body: dict) -> Reply:
        """Long-running work (Discover, Ask) runs server-side and survives refreshes."""
        try:
            params = _task_params(body)  # validates "kind" too
        except ValueError as err:
            return 400, {"error": str(err)}
        return 200, self.tasks.submit(str(body["kind"]), params)


def _routes(table: list[tuple[str, str]]) -> list[tuple[re.Pattern, str]]:
    return [(re.compile(path), method) for path, method in table]


GET_ROUTES = _routes([
    ("/api/settings", "get_settings"),
    ("/api/defaults", "get_defaults"),
    ("/api/library", "get_library"),
    ("/api/jobs", "get_jobs"),
    ("/api/concepts", "get_concepts"),
    ("/api/tasks", "get_tasks"),
    (r"/api/tasks/(\w+)", "get_task"),
    ("/api/graph", "get_graph"),
    ("/api/search", "get_search"),
    (f"/api/video/{VID}/delete-preview", "get_delete_preview"),
    (f"/api/video/{VID}", "get_video"),
    ("/api/concept/([^/]+)", "get_concept"),
])
POST_ROUTES = _routes([
    ("/api/settings", "post_settings"),
    ("/api/providers/test", "post_provider_test"),
    ("/api/jobs", "post_jobs"),
    (f"/api/video/{VID}/delete", "post_delete"),
    (f"/api/video/{VID}/speakers", "post_speakers"),
    (r"/api/jobs/(\w+)/cancel", "post_cancel"),
    ("/api/jobs/clear", "post_clear"),
    ("/api/tasks", "post_tasks"),
])


def serve(port: int = 8765, open_browser: bool = True, host: str = "127.0.0.1") -> None:
    """`host` other than 127.0.0.1 is for a container (0.0.0.0), whose port must then be published on the host's
    127.0.0.1 only, at the same port: Settings can run programs, so the app must never be reachable from a network."""
    resources.PRIORITY = 0.0  # Discover and Ask (someone is waiting at the screen) go ahead of batch jobs
    url = f"http://127.0.0.1:{port}/"
    try:
        httpd = ThreadingHTTPServer((host, port), Handler)
    except OSError:  # port taken: if it's already us, just open it
        import urllib.request
        try:
            urllib.request.urlopen(url + "api/jobs", timeout=3)
        except OSError:
            sys.exit(f"Port {port} is in use by another program. Try: tkb serve --port {port + 1}")
        print(f"tkb is already running at {url}")
        if open_browser:
            webbrowser.open(url)
        return
    logs.to_file()
    log.info("app started on %s", url)
    Handler.jobs, Handler.port = Jobs(), port
    Handler.tasks = Tasks(_runners(Handler.jobs))
    threading.Thread(target=search.warm, name="embed", daemon=True).start()  # index new text for search
    Scheduler(port)  # runs watched topics when they're due
    print(f"tkb running at {url}  (Ctrl+C to stop)", flush=True)
    if host not in ("127.0.0.1", "localhost", "::1"):
        log.warning("listening on %s:%s: publish this port on 127.0.0.1 only", host, port)
    if open_browser:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()

    def stop(*_: Any) -> None:  # `docker stop` sends SIGTERM: shut down as on Ctrl+C (jobs exit with the app)
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log.info("app stopped")

