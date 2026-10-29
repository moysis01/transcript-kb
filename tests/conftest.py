"""Every test runs against a throwaway TKB_HOME (and lock dir), never the real library."""
import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path

import pytest

_HOME = Path(tempfile.mkdtemp(prefix="tkb-test-"))
os.environ["TKB_HOME"] = str(_HOME)
os.environ["TKB_LOCKS"] = str(_HOME / "locks")
os.environ["TKB_EMBED"] = "off"  # keyword search unless a test installs a fake embedder (no model download)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(autouse=True)
def clean_home():
    for sub in ("data", "kb"):
        shutil.rmtree(_HOME / sub, ignore_errors=True)
    yield _HOME


@pytest.fixture
def home():
    return _HOME


@pytest.fixture
def app(home, monkeypatch):
    """The web app on a free port, with a queue that never starts a real `tkb run`."""
    from tkb.app import jobs, server, tasks
    monkeypatch.setattr(jobs, "JOBS_FILE", home / "data" / ".jobs.json")
    server.Handler.jobs, server.Handler.port = jobs.Jobs(max_running=1, ahead=1), 0
    server.Handler.jobs.lookups.submit = lambda *a, **k: None  # no network: titles come from the request
    server.Handler.jobs.stopped = True  # queue only: never start a real `tkb run` from a test
    server.Handler.tasks = tasks.Tasks(server._runners(server.Handler.jobs))
    httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    server.Handler.port = httpd.server_port
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd.server_port
    httpd.shutdown()
    server.Handler.jobs.stop()
