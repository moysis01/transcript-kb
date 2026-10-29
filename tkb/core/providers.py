"""The AI providers: one function per kind of API, each taking a system prompt and a request and returning the
answer (text, or a JSON object when a schema is given). tkb.core.llm checks answers and retries; this module
only talks to providers, using the standard library (no SDKs).

  claude-code   the `claude` command (Claude Code headless), with your Claude login
  anthropic     the Anthropic Messages API; structured answers through a forced tool call
  openai, gemini, openrouter, lmstudio, custom
                any OpenAI-compatible chat API; structured answers by JSON schema where the server supports it,
                else JSON mode, else asked for in the prompt (whichever worked is remembered per model)
  ollama        Ollama's own API, which takes a context length (its OpenAI endpoint can silently cut prompts)
  command       any command-line tool that reads the request on stdin and prints the answer

Keys and addresses come from Settings → AI providers. Keys are sent only in request headers, never in URLs,
logs or error messages.
"""
import json
import os
import shlex
import shutil
import subprocess
import urllib.error
import urllib.request
from typing import Any

from tkb import config

OPENAI_STYLE = ("openai", "gemini", "openrouter", "lmstudio", "custom")
CLAUDE_ALIASES = ("sonnet", "opus", "haiku", "fable")
JSON_PLEASE = "\n\nReply with only a JSON object (no other text) that matches this JSON Schema:\n"


class AIError(RuntimeError):
    """A request failed; another attempt may work."""


class SetupError(AIError):
    """A missing or refused key, an unknown model, nothing listening: another attempt won't help."""


class FormatError(AIError):
    """The server rejected the request's form (e.g. a structured-output option it doesn't support)."""


# ------------------------------------------------------------------ connections

def _setting(provider: str, what: str) -> str:
    key = f"provider.{provider}.{what}"
    return config.get(key) if key in config.SETTINGS else ""


def _where(provider: str) -> str:
    return f"{config.PROVIDERS[provider]} (Settings → AI providers)"


def _key(provider: str, required: bool = True) -> str:
    key = _setting(provider, "key")
    if required and not key:
        raise SetupError(f"no API key for {_where(provider)}")
    return key


def _url(provider: str) -> str:
    url = _setting(provider, "url")
    if not url:
        raise SetupError(f"no address set for {_where(provider)}")
    return url


def _headers(provider: str) -> dict[str, str]:
    if provider == "anthropic":
        return {"x-api-key": _key(provider), "anthropic-version": "2023-06-01"}
    key = _key(provider, required=provider in ("openai", "gemini", "openrouter"))
    return {"Authorization": f"Bearer {key}"} if key else {}


def _request(provider: str, url: str, body: dict | None = None, timeout: float | None = None) -> Any:
    """GET (no body) or POST JSON; errors sorted into SetupError, FormatError or AIError."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET",
                                 headers={"Content-Type": "application/json", **_headers(provider)})
    try:
        with urllib.request.urlopen(req, timeout=timeout or config.get("ai.timeout")) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        detail = e.read()[:400].decode("utf-8", "replace").strip()
        if e.code in (401, 403):
            raise SetupError(f"{_where(provider)} refused the key (HTTP {e.code}): {detail}") from e
        if e.code == 404:
            raise SetupError(f"{_where(provider)}: not found (HTTP 404), check the model name and address: "
                             f"{detail}") from e
        if e.code in (400, 422):
            raise FormatError(f"{config.PROVIDERS[provider]} rejected the request (HTTP {e.code}): {detail}") from e
        raise AIError(f"{config.PROVIDERS[provider]}: HTTP {e.code}: {detail}") from e
    except TimeoutError as e:
        raise AIError(f"{config.PROVIDERS[provider]} didn't answer in time") from e
    except urllib.error.URLError as e:
        if isinstance(e.reason, ConnectionRefusedError):
            raise SetupError(f"nothing answers at {url.split('/api')[0].split('/v1')[0]}: is "
                             f"{config.PROVIDERS[provider]} running?") from e
        raise AIError(f"can't reach {config.PROVIDERS[provider]}: {e.reason}") from e
    except ValueError as e:
        raise AIError(f"{config.PROVIDERS[provider]} sent something that isn't JSON") from e


def parse_json(text: str) -> Any:
    """The JSON object in an answer, also when wrapped in a code fence or a sentence."""
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0]
    try:
        return json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except ValueError:
                pass
    raise AIError("the answer wasn't JSON")


# ------------------------------------------------------------------ asking

def ask(provider: str, model: str, system: str, prompt: str, schema: dict | None, think: bool = True) -> Any:
    if provider not in config.PROVIDERS:
        raise SetupError(f"unknown AI provider {provider!r}")
    return {"claude-code": _claude_code, "anthropic": _anthropic, "ollama": _ollama, "command": _command}.get(
        provider, _openai_style)(provider, model, system, prompt, schema, think)


def _claude_code(provider: str, model: str, system: str, prompt: str, schema: dict | None, think: bool) -> Any:
    cmd = ["claude", "-p", "--output-format", "json", "--tools", "",
           "--no-session-persistence", "--strict-mcp-config",
           "--setting-sources", "", "--model", model, "--system-prompt", system]
    if schema:
        cmd += ["--json-schema", json.dumps(schema)]
    try:
        proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=config.get("ai.timeout"),
                              env=None if think else {**os.environ, "MAX_THINKING_TOKENS": "0"})
    except FileNotFoundError as e:
        raise SetupError("the claude command isn't installed: install Claude Code, or pick another provider") from e
    except subprocess.TimeoutExpired as e:
        raise AIError("Claude Code didn't answer in time") from e
    try:
        out = json.loads(proc.stdout)
    except ValueError as e:
        raise AIError((proc.stderr or proc.stdout)[:300]) from e
    if out.get("is_error"):
        message = str(out.get("result") or out)[:300]
        if "Not logged in" in message or "/login" in message or "Invalid API key" in message:
            # Not a SetupError: under load one call can get this while the login is fine (parallel calls work),
            # so it's retried; a login that's really missing fails every attempt with this advice.
            raise AIError("Claude Code isn't logged in: run `claude` once and log in (in Docker: set "
                          "CLAUDE_CODE_OAUTH_TOKEN from `claude setup-token`), or pick another provider. "
                          f"Claude Code said: {message[:100]}")
        raise AIError(message)
    if schema:
        if out.get("structured_output") is None:
            raise AIError("no structured answer")
        return out["structured_output"]
    return out["result"]


def _anthropic(provider: str, model: str, system: str, prompt: str, schema: dict | None, think: bool) -> Any:
    body: dict = {"model": model, "max_tokens": config.get("ai.max_tokens"), "system": system,
                  "messages": [{"role": "user", "content": prompt}]}
    if schema:
        body["tools"] = [{"name": "answer", "description": "Give the answer.", "input_schema": schema}]
        body["tool_choice"] = {"type": "tool", "name": "answer"}
    out = _request(provider, f"{_url(provider)}/v1/messages", body)
    blocks = out.get("content") or []
    if schema:
        use = next((b for b in blocks if b.get("type") == "tool_use"), None)
        if use is None:
            raise AIError("no structured answer")
        return use.get("input")
    return "".join(b.get("text", "") for b in blocks if b.get("type") == "text")


_modes: dict[tuple[str, str], str] = {}  # (provider, model) -> the structured-output mode that worked
MODES = ("schema", "json", "prompt")


def _openai_style(provider: str, model: str, system: str, prompt: str, schema: dict | None, think: bool) -> Any:
    url = f"{_url(provider)}/chat/completions"
    start = MODES.index(_modes.get((provider, model), "schema")) if schema else len(MODES) - 1
    for mode in MODES[start:]:
        user = prompt + (JSON_PLEASE + json.dumps(schema) if schema and mode != "schema" else "")
        body: dict = {"model": model, "messages": [{"role": "system", "content": system},
                                                   {"role": "user", "content": user}]}
        if schema and mode == "schema":
            body["response_format"] = {"type": "json_schema", "json_schema": {"name": "answer", "schema": schema}}
        elif schema and mode == "json":
            body["response_format"] = {"type": "json_object"}
        try:
            out = _request(provider, url, body)
        except FormatError:
            if mode == MODES[-1] or not schema:
                raise
            continue  # this server doesn't take that form: try the next
        if schema:
            _modes[(provider, model)] = mode
        try:
            text = out["choices"][0]["message"].get("content") or ""
        except (KeyError, IndexError, TypeError) as e:
            raise AIError("an answer in an unexpected form") from e
        return parse_json(text) if schema else text
    raise AIError("unreachable")


def _ollama(provider: str, model: str, system: str, prompt: str, schema: dict | None, think: bool) -> Any:
    body: dict = {"model": model, "stream": False, "options": {"num_ctx": config.get("provider.ollama.context")},
                  "messages": [{"role": "system", "content": system},
                               {"role": "user", "content": prompt + (JSON_PLEASE + json.dumps(schema) if schema
                                                                     else "")}]}
    if schema:
        body["format"] = schema
    out = _request(provider, f"{_url(provider)}/api/chat", body)
    text = (out.get("message") or {}).get("content") or ""
    return parse_json(text) if schema else text


def _command(provider: str, model: str, system: str, prompt: str, schema: dict | None, think: bool) -> Any:
    line = config.get("provider.command.line")
    if not line:
        raise SetupError(f"no command set for {_where(provider)}")
    cmd = [w.replace("{model}", model) for w in shlex.split(line)]  # never through a shell
    stdin = f"{system}\n\n{prompt}" + (JSON_PLEASE + json.dumps(schema) if schema else "")
    try:
        proc = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=config.get("ai.timeout"))
    except FileNotFoundError as e:
        raise SetupError(f"{cmd[0]} isn't installed") from e
    except subprocess.TimeoutExpired as e:
        raise AIError(f"{cmd[0]} didn't answer in time") from e
    if proc.returncode:
        raise AIError(f"{cmd[0]} failed: {(proc.stderr or proc.stdout)[:300]}")
    return parse_json(proc.stdout) if schema else proc.stdout.strip()


# ------------------------------------------------------------------ embeddings

def embed(provider: str, model: str, texts: list[str]) -> list[list[float]]:
    """Vectors from a provider's embeddings endpoint, one per text, in order."""
    if provider == "ollama":
        out = _request(provider, f"{_url(provider)}/api/embed", {"model": model, "input": texts})
        vecs = out.get("embeddings")
    elif provider in OPENAI_STYLE:
        out = _request(provider, f"{_url(provider)}/embeddings", {"model": model, "input": texts})
        vecs = [d["embedding"] for d in sorted(out.get("data") or [], key=lambda d: d.get("index", 0))]
    else:
        raise SetupError(f"{config.PROVIDERS.get(provider, provider)} has no embeddings")
    if not vecs or len(vecs) != len(texts):
        raise AIError("embeddings in an unexpected form")
    return vecs


# ------------------------------------------------------------------ set-up status and testing

def status(provider: str) -> tuple[bool | None, str]:
    """Is the provider set up, as far as can be told without asking it? (True, False, or None for "test it")."""
    if provider == "claude-code":
        return (True, "") if shutil.which("claude") else (False, "the claude command isn't installed")
    if provider in ("anthropic", "openai", "gemini", "openrouter"):
        return (True, "") if _setting(provider, "key") else (False, "no API key")
    if provider == "custom":
        return (None, "not tested") if _setting(provider, "url") else (False, "no address")
    if provider == "command":
        return (None, "not tested") if config.get("provider.command.line") else (False, "no command")
    return None, "not tested"


def test(provider: str) -> dict:
    """Ask the provider which models it has (a cheap request that proves the address and key work)."""
    try:
        if provider == "claude-code":
            if not shutil.which("claude"):
                raise SetupError("the claude command isn't installed")
            return {"ok": True, "message": "Claude Code is installed", "models": list(CLAUDE_ALIASES)}
        if provider == "command":
            line = config.get("provider.command.line")
            if not line:
                raise SetupError("no command set")
            program = shlex.split(line)[0]
            if not shutil.which(program):
                raise SetupError(f"{program} isn't installed")
            return {"ok": True, "message": f"{program} is installed", "models": []}
        if provider == "anthropic":
            names = [m["id"] for m in _request(provider, f"{_url(provider)}/v1/models", timeout=20).get("data", [])]
        elif provider == "ollama":
            names = [m["name"] for m in _request(provider, f"{_url(provider)}/api/tags", timeout=20).get("models", [])]
        else:
            names = [m["id"] for m in _request(provider, f"{_url(provider)}/models", timeout=20).get("data", [])]
    except AIError as e:
        return {"ok": False, "message": str(e), "models": []}
    except (KeyError, TypeError, ValueError):
        return {"ok": False, "message": "it answered, but not with a list of models", "models": []}
    return {"ok": True, "message": f"Connected: {len(names)} models", "models": sorted(names)}
