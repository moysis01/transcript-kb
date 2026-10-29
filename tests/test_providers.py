"""Any AI provider: the default stays exactly as before; other providers work, fall back gracefully, fail fast on
set-up problems, and never leak keys."""
import json
import os
import socket
import stat
import subprocess
import sys
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from tkb import config
from tkb.core import llm, providers
from tkb.services import settings

SCHEMA = {"type": "object", "properties": {"scores": {"type": "array", "items": {
    "type": "object", "properties": {"n": {"type": "integer"}, "kind": {"type": "string", "enum": ["a", "b"]}},
    "required": ["n", "kind"]}}}, "required": ["scores"]}
GOOD = {"scores": [{"n": 1, "kind": "a"}]}
KEY = "sk-test-0123456789"


class Fake(BaseHTTPRequestHandler):
    """Speaks the Anthropic, OpenAI-compatible and Ollama APIs, as configured per test in `Fake.cfg`."""
    cfg: dict = {}
    seen: list = []

    def log_message(self, *a):
        pass

    def _reply(self, code: int, obj) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bad_key(self) -> bool:
        return (self.headers.get("Authorization", f"Bearer {KEY}") != f"Bearer {KEY}"
                or self.headers.get("x-api-key", KEY) != KEY)

    def do_GET(self):
        self.seen.append(("GET", self.path, None, {k.lower(): v for k, v in self.headers.items()}))
        if self._bad_key():
            return self._reply(401, {"error": "bad key"})
        if self.path.endswith("/models"):
            return self._reply(200, {"data": [{"id": "model-b"}, {"id": "model-a"}]})
        if self.path == "/api/tags":
            return self._reply(200, {"models": [{"name": "llama3.1:8b"}]})
        self._reply(404, {"error": "no"})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.seen.append(("POST", self.path, body, {k.lower(): v for k, v in self.headers.items()}))
        if self._bad_key():
            return self._reply(401, {"error": "bad key"})
        if self.path == "/v1/messages":
            return self._reply(200, {"content": [{"type": "tool_use", "name": "answer", "input": GOOD}]})
        if self.path == "/v1/chat/completions":
            fmt = (body.get("response_format") or {}).get("type")
            if fmt in self.cfg.get("reject", ()):
                return self._reply(400, {"error": f"response_format {fmt} unsupported"})
            return self._reply(200, {"choices": [{"message": {"content": self.cfg.get("answer", json.dumps(GOOD))}}]})
        if self.path == "/v1/embeddings":
            return self._reply(200, {"data": [{"index": i, "embedding": [float(len(t)), 1.0]}
                                              for i, t in enumerate(body["input"])]})
        if self.path == "/api/chat":
            return self._reply(200, {"message": {"content": json.dumps(GOOD)}})
        self._reply(404, {"error": "no such path"})


@pytest.fixture
def server():
    Fake.cfg, Fake.seen = {}, []
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{httpd.server_port}"
    settings.save({"provider.custom.url": f"{url}/v1", "provider.custom.key": KEY,
                   "provider.anthropic.url": url, "provider.anthropic.key": KEY, "provider.ollama.url": url})
    yield url
    httpd.shutdown()
    providers._modes.clear()


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)


def test_the_default_runs_claude_code_exactly_as_before(monkeypatch):
    calls = []
    monkeypatch.setattr(providers.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or subprocess.CompletedProcess(
        cmd, 0, json.dumps({"structured_output": GOOD}), ""))
    llm.call("system", "prompt", schema=SCHEMA)
    llm.call("system", "prompt", schema=SCHEMA, model="haiku")
    before = ["claude", "-p", "--output-format", "json", "--tools", "", "--no-session-persistence",
              "--strict-mcp-config", "--setting-sources", "", "--model", "sonnet", "--system-prompt", "system",
              "--json-schema", json.dumps(SCHEMA)]
    assert calls[0] == before
    assert calls[1] == [*before[:11], "haiku", *before[12:]]


def test_anthropic_answers_through_a_forced_tool(server):
    assert llm.call("sys", "hi", schema=SCHEMA, model="anthropic/claude-x") == GOOD
    method, path, body, headers = Fake.seen[-1]
    assert body["tool_choice"] == {"type": "tool", "name": "answer"} and body["max_tokens"] == 16000
    assert headers["x-api-key"] == KEY and KEY not in path


def test_openai_style_falls_back_and_remembers_what_worked(server):
    Fake.cfg["reject"] = ("json_schema",)
    assert llm.call("sys", "hi", schema=SCHEMA, model="custom/m1") == GOOD
    assert [s[2].get("response_format", {}).get("type") for s in Fake.seen] == ["json_schema", "json_object"]
    assert "matches this JSON Schema" in Fake.seen[-1][2]["messages"][1]["content"], "asked in the request itself"
    Fake.seen.clear()
    llm.call("sys", "again", schema=SCHEMA, model="custom/m1")
    assert len(Fake.seen) == 1, "the mode that worked is used straight away next time"


def test_a_model_without_structured_output_is_asked_in_the_prompt(server):
    Fake.cfg.update(reject=("json_schema", "json_object"), answer="Sure!\n```json\n" + json.dumps(GOOD) + "\n```")
    assert llm.call("sys", "hi", schema=SCHEMA, model="custom/small") == GOOD
    assert "response_format" not in Fake.seen[-1][2]


def test_answers_are_checked_and_malformed_ones_retried(server):
    Fake.cfg["answer"] = json.dumps({"scores": [{"n": "2", "kind": "a"}]})
    assert llm.call("sys", "hi", schema=SCHEMA, model="custom/m") == {"scores": [{"n": 2, "kind": "a"}]}
    Fake.cfg["answer"] = json.dumps({"scores": [{"n": 1, "kind": "zzz"}]})
    Fake.seen.clear()
    with pytest.raises(llm.LLMError, match="isn't one of the allowed values"):
        llm.call("sys", "hi", schema=SCHEMA, model="custom/m")
    assert len(Fake.seen) == config.get("claude.retries"), "each malformed answer was retried"


def test_a_refused_key_fails_at_once_with_a_clear_message(server):
    settings.save({"provider.custom.key": "sk-wrong-key"})
    with pytest.raises(llm.LLMError) as err:
        llm.call("sys", "hi", schema=SCHEMA, model="custom/m")
    assert len(Fake.seen) == 1 and "Settings → AI providers" in str(err.value) and "sk-wrong" not in str(err.value)


def test_nothing_running_fails_at_once():
    with socket.socket() as s:  # a port nothing listens on
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    settings.save({"provider.ollama.url": f"http://127.0.0.1:{port}"})
    with pytest.raises(llm.LLMError, match="is Ollama .local. running"):
        llm.call("sys", "hi", model="ollama/llama3.1")


def test_timeouts_are_retried_then_reported(server, monkeypatch):
    tries = []

    def slow(*a, **k):
        tries.append(1)
        raise TimeoutError
    monkeypatch.setattr(urllib.request, "urlopen", slow)
    with pytest.raises(llm.LLMError, match="didn't answer in time"):
        llm.call("sys", "hi", model="custom/m")
    assert len(tries) == config.get("claude.retries")


def test_ollama_gets_a_context_length_and_the_schema(server):
    assert llm.call("sys", "hi", schema=SCHEMA, model="ollama/llama3.1:8b") == GOOD
    body = Fake.seen[-1][2]
    assert body["options"]["num_ctx"] == 32768 and body["format"] == SCHEMA and body["model"] == "llama3.1:8b"


def test_any_command_line_tool_can_answer():
    script = "import sys, json; sys.stdin.read(); print('ok: ' + json.dumps({'scores': [{'n': 3, 'kind': 'b'}]}))"
    settings.save({"provider.command.line": f"{sys.executable} -c \"{script}\" {{model}}"})
    assert llm.call("sys", "hi", schema=SCHEMA, model="command/whatever") == {"scores": [{"n": 3, "kind": "b"}]}


def test_keys_never_reach_the_page_and_are_kept_private(server):
    page = json.dumps(settings.page())
    assert KEY not in page
    field = next(f for s in settings.page()["sections"] for f in s["fields"] if f["key"] == "provider.custom.key")
    assert field["has_value"] and field["value"] == ""
    assert stat.S_IMODE(os.stat(config.SECRETS_FILE).st_mode) == 0o600
    assert KEY not in (config.SETTINGS_FILE.read_text() if config.SETTINGS_FILE.exists() else "")
    settings.save({"provider.custom.key": ""})
    assert config.get("provider.custom.key") == KEY, "an empty box keeps the saved key"
    settings.save({"provider.custom.key": None})
    assert config.get("provider.custom.key") == "", "clearing is explicit"


def test_testing_a_provider_lists_its_models(server):
    assert settings.test("custom") == {"ok": True, "message": "Connected: 2 models", "models": ["model-a", "model-b"]}
    assert settings.test("ollama")["models"] == ["llama3.1:8b"]
    settings.save({"provider.custom.key": "sk-wrong-key"})
    assert not settings.test("custom")["ok"]


def test_search_can_use_a_providers_embeddings(server, monkeypatch):
    from tkb.core import embed
    monkeypatch.delenv("TKB_EMBED")
    settings.save({"search.model": "custom/embedder"})
    vecs = embed.encode(["ab", "abcd"], query=True)
    assert vecs.shape == (2, 2) and Fake.seen[-1][2]["input"] == ["ab", "abcd"], "no local-model query prefix"


def test_quick_jobs_follow_the_chosen_provider():
    assert config.model("phrasing") == "claude-code/haiku", "Claude Code's fast model, as before"
    settings.save({"model.default": "openai/gpt-x"})
    assert config.model("phrasing") == "openai/gpt-x" and config.model("clean") == "openai/gpt-x"


def test_claude_code_login_errors_are_retried_and_keep_what_it_said(monkeypatch):
    """Under load Claude Code sometimes answers one call with a login error while the login is fine (calls at the
    same moment work): retried like any failure. A login that's really missing still says how to fix it."""
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    calls = []

    def answer(*replies):
        def run(cmd, **kw):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, json.dumps(replies[min(len(calls), len(replies)) - 1]), "")
        return run
    monkeypatch.setattr(providers.subprocess, "run", answer(
        {"is_error": True, "result": "OAuth token has expired · Please run /login"}, {"result": "ok"}))
    assert llm.call("system", "prompt") == "ok" and len(calls) == 2

    calls.clear()
    missing = {"is_error": True, "result": "Not logged in · Please run /login"}
    monkeypatch.setattr(providers.subprocess, "run", answer(missing))
    with pytest.raises(llm.LLMError, match="CLAUDE_CODE_OAUTH_TOKEN") as err:
        llm.call("system", "prompt")
    assert len(calls) == config.get("claude.retries") and "Not logged in · Please run /login" in str(err.value)
