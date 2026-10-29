"""The Settings page: every setting with its value, default and where the value comes from; saving and resetting.

API keys are stored apart, in data/.secrets.json (owner-only), and never sent back: the page only learns whether
a key is set. An empty key box changes nothing; clearing a key is an explicit reset.

Values are checked here before anything is written, and saved under the file lock, so two tabs saving at once
can't lose each other's changes. A value equal to its default is removed from the file rather than stored, so
a later change of default (in a new version) still reaches it.
"""
import importlib.metadata
import os
import platform
from typing import Any

from tkb import config
from tkb.config import SECRETS_FILE, SETTINGS, SETTINGS_FILE
from tkb.core import providers
from tkb.core.storage import locked, read_json, write_json
from tkb.pipeline import transcribe
from tkb.prompts import default as prompt_default

# Settings that must not exceed another: (smaller, bigger).
RULES = [("ahead", "jobs"), ("chunk.min_words", "chunk.max_words"), ("sentence.target_words", "sentence.max_words"),
         ("sentence.target_secs", "sentence.max_secs"), ("discover.max_picks", "discover.read_max")]


def _default(s: config.Setting) -> Any:
    return prompt_default(s.key.removeprefix("prompt.")) if s.kind == "text" else s.default


def _field(s: config.Setting) -> dict:
    env, value = config.from_env(s.key), config.get(s.key)
    out = {"key": s.key, "label": s.label, "help": s.help, "kind": s.kind, "choices": [list(c) for c in s.choices],
           "lo": s.lo, "hi": s.hi, "default": _default(s), "value": _default(s) if value is None else value,
           "env": s.env if env is not None else None, "applies": s.applies, "advanced": s.advanced, "step": s.step,
           "test": s.test}
    if s.kind == "secret":  # never the key itself
        out.update(value="", default="", has_value=bool(value))
    return out


def _providers() -> dict:
    out = {}
    for pid, label in config.PROVIDERS.items():
        ready, why = providers.status(pid)
        out[pid] = {"label": label, "ready": ready, "why": why}
    return out


def _versions() -> dict[str, str]:
    out = {"Python": platform.python_version()}
    for pkg in ("yt-dlp", "mlx-whisper", "sherpa-onnx", "fastembed"):
        try:
            out[pkg] = importlib.metadata.version(pkg)
        except importlib.metadata.PackageNotFoundError:
            out[pkg] = "not installed"
    return out


def page() -> dict:
    """Everything the page shows, section by section, plus a read-only About section."""
    sections = [{"id": sid, "title": title, "description": desc,
                 "fields": [_field(s) for s in SETTINGS.values() if s.section == sid]}
                for sid, title, desc in config.SECTIONS]
    about = {"Library folder": str(config.HOME), "Videos and app data": str(config.DATA),
             "Knowledge base notes": str(config.KB), "Settings file": str(SETTINGS_FILE),
             "Lock folder": str(config.LOCKS), **_versions()}
    return {"sections": sections, "about": about, "providers": _providers()}


def save(changes: dict[str, Any]) -> dict:
    """Apply {key: value} (None resets to the default); all-or-nothing. ValueError says what's wrong."""
    if not isinstance(changes, dict) or not changes:
        raise ValueError("nothing to save")
    clean: dict[str, Any] = {}
    keys: dict[str, str | None] = {}
    for key, raw in changes.items():
        s = SETTINGS.get(key)
        if s is None:
            raise ValueError(f"unknown setting {key!r}")
        if config.from_env(key) is not None:
            raise ValueError(f"{s.label} is set by the environment variable {s.env}")
        if s.kind == "secret":
            if raw is None:
                keys[key] = None  # cleared
            elif str(raw).strip():
                keys[key] = config.parse(s, raw)
            continue  # an empty box keeps the saved key
        if raw is None:
            clean[key] = None
            continue
        value = config.parse(s, raw)
        if key == "model.default" and not value:
            raise ValueError(f"{s.label}: choose a model")
        if s.kind == "embed":
            _check_local_model(s, value)
        clean[key] = None if value == _default(s) else value
    if keys:
        _save_keys(keys)
    if not clean:
        return page()
    with locked(SETTINGS_FILE):
        current = read_json(SETTINGS_FILE) if SETTINGS_FILE.exists() else {}
        merged = {**current, **clean}

        def effective(k: str) -> Any:
            if (env := config.from_env(k)) is not None:
                return env
            return config.parse(SETTINGS[k], merged[k]) if merged.get(k) is not None else _default(SETTINGS[k])

        for small, big in RULES:
            if effective(small) > effective(big):
                raise ValueError(f"{SETTINGS[small].label} can't be more than {SETTINGS[big].label.lower()}")
        if "asr.model" in clean or "asr.engine" in clean:  # a model must suit the engine
            engine = effective("asr.engine")
            transcribe.resolve_model(effective("asr.model"), None if engine == "auto" else engine)
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        write_json(SETTINGS_FILE, {k: v for k, v in merged.items() if v is not None and k in SETTINGS})
    return page()


def _check_local_model(s: config.Setting, value: str) -> None:
    """A local search model must be one fastembed knows, so a typo can't download an arbitrary repository."""
    if value == "off" or value.partition("/")[0] in config.EMBED_PROVIDERS:
        return
    try:
        from fastembed import TextEmbedding
        known = {m["model"] for m in TextEmbedding.list_supported_models()}
    except ImportError:
        return
    if value not in known:
        raise ValueError(f"{s.label}: {value} isn't a model fastembed knows; for a provider's, write provider/model")


def _save_keys(keys: dict[str, str | None]) -> None:
    with locked(SECRETS_FILE):
        current = read_json(SECRETS_FILE) if SECRETS_FILE.exists() else {}
        merged = {k: v for k, v in {**current, **keys}.items() if v and k in SETTINGS}
        SECRETS_FILE.parent.mkdir(parents=True, exist_ok=True)
        write_json(SECRETS_FILE, merged)  # written through a temp file created owner-only (0600)
        os.chmod(SECRETS_FILE, 0o600)


def test(provider: str) -> dict:
    """Try a provider with the saved settings: does it answer, and which models does it have?"""
    if provider not in config.PROVIDERS:
        raise ValueError("unknown provider")
    return providers.test(provider)


def defaults() -> dict:
    """The defaults the Add videos and Discover checkboxes start from."""
    return {"translate": config.get("translate"), "sponsorblock": config.get("sponsorblock"),
            "max_picks": config.get("discover.max_picks")}
