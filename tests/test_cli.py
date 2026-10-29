"""Command-line plumbing: `tkb update`, command routing, and keeping heavy optional imports out of jobs."""
import subprocess
import sys

import pytest

from tkb import cli
from tkb.core import youtube


class FakeRun:
    """Stands in for subprocess.run: records commands; reports yt-dlp versions in sequence."""

    def __init__(self, versions=("2026.1.1", "2026.9.9"), busy="", fail=None):
        self.versions, self.busy, self.fail, self.cmds = list(versions), busy, fail, []

    def __call__(self, cmd, **kw):
        self.cmds.append(cmd)
        out = ""
        if cmd[0] == "pgrep":
            out = self.busy if " run " in cmd[-1] else ""
        elif "importlib.metadata" in " ".join(cmd):
            out = self.versions.pop(0)
        return subprocess.CompletedProcess(cmd, 1 if self.fail and self.fail in cmd else 0, out, "")


def _code(argv: list[str]) -> int:
    with pytest.raises(SystemExit) as done:
        cli.main(argv)
    return done.value.code


@pytest.fixture
def run(monkeypatch):
    def install(**kw):
        fake = FakeRun(**kw)
        monkeypatch.setattr(subprocess, "run", fake)
        monkeypatch.setattr(cli.shutil, "which", lambda name: "/bin/uv")
        return fake
    return install


def test_update_upgrades_only_yt_dlp_and_reports_versions(run, capsys):
    fake = run()
    assert _code(["update"]) == 0
    uv = [c[1:] for c in fake.cmds if c[0] == "/bin/uv"]
    assert uv == [["lock", "--upgrade-package", "yt-dlp"], ["sync"]]
    assert "yt-dlp 2026.1.1 -> 2026.9.9" in capsys.readouterr().out


def test_update_refuses_while_videos_are_processing(run, capsys):
    fake = run(busy="4242 python -m tkb.cli run abcdefghijk --jsonl")
    assert _code(["update"]) == 1
    assert not [c for c in fake.cmds if c[0] == "/bin/uv"] and "--force" in capsys.readouterr().out
    fake = run(busy="4242 python -m tkb.cli run abcdefghijk --jsonl")
    assert _code(["update", "--force"]) == 0


def test_update_stops_when_the_lock_fails(run, capsys):
    fake = run(fail="lock")
    assert _code(["update"]) == 1
    assert ["/bin/uv", "sync"] not in fake.cmds


def test_new_commands_are_not_mistaken_for_video_urls():
    for name in cli.COMMANDS:
        assert cli.build_parser().parse_args([name, *(["x"] if name in ("run", "ask", "discover", "delete") else [])])


def test_video_ids_starting_with_a_dash_reach_the_child_as_ids(monkeypatch):
    """YouTube ids can start with "-" (e.g. -czWNiE5Qmw); the child must not read one as an option."""
    from types import SimpleNamespace

    from tkb.app import jobs
    from tkb.pipeline import runner
    vid = "-czWNiE5Qmw"
    cmd = jobs._command({"vid": vid, "sponsorblock": True, "translate": False, "opts": {"stage": "kb"}})
    assert cli.build_parser().parse_args(cmd[3:]).urls == [vid]

    started = []
    monkeypatch.setattr(runner.subprocess, "Popen", lambda cmd, **kw: started.append(cmd) or
                        SimpleNamespace(stdout=iter(()), wait=lambda: 0))
    a = cli.build_parser().parse_args(["run", "--jobs", "2", "--", vid])
    cli._settle(a)
    runner.run_many([vid], a, lambda *_, **__: None)
    assert cli.build_parser().parse_args(started[0][3:]).urls == [vid]


def test_outdated_downloader_failures_suggest_an_update():
    assert "tkb update" in youtube._message(Exception("ERROR: [youtube] abc: Unable to extract nsig function"))
    assert "tkb update" not in youtube._message(Exception("ERROR: [youtube] abc: Private video"))


def test_jobs_do_not_import_optional_heavy_packages():
    """Every video job starts `python -m tkb.cli`; embeddings and MCP must only load where they're used."""
    code = "import sys, tkb.cli; print(sorted(m for m in ('fastembed', 'mcp', 'onnxruntime') if m in sys.modules))"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    assert out.strip() == "[]"
