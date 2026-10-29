"""Text embeddings for meaning-based search: a local model (fastembed, ONNX, CPU only; ~90 MB by default) or
a provider's embeddings endpoint ("openai/text-embedding-3-small", "ollama/nomic-embed-text": Settings → Search).

A local model is loaded on first use, and only by processes that search (the app, `tkb ask`, the MCP server),
never by video jobs. If the model can't be used (offline on first use, no key, TKB_EMBED=off), callers fall back
to keyword search; `encode` then returns None.
"""
import threading
from collections.abc import Sequence
from pathlib import Path

import numpy as np

from tkb import config
from tkb.core import log as logs
from tkb.core import providers

log = logs.get("embed")

CACHE = Path.home() / ".cache" / "tkb" / "models" / "fastembed"  # not the default temp dir, which macOS clears
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "  # arctic-embed / BGE query instruction
# (only for local models: other models' query conventions differ)

BATCH = 16      # local model: texts per batch (a query waits for at most one)
API_BATCH = 64  # provider: texts per request
_lock = threading.Lock()
_state: dict = {"name": None, "model": None, "failed": False}


def name() -> str:
    """The model the Settings page asks for, or "off"."""
    return config.get("search.model")


def _model():  # -> fastembed.TextEmbedding | None
    with _lock:
        if _state["name"] != (want := name()):  # changed in Settings: load the new one (or retry a failed one)
            _state.update(name=want, model=None, failed=False)
        if _state["model"] is None and not _state["failed"]:
            try:
                from fastembed import TextEmbedding
                _state["model"] = TextEmbedding(want, cache_dir=str(CACHE))
                log.info("embedding model %s loaded", want)
            except Exception as e:  # noqa: BLE001 - any failure means keyword search only, not a broken app
                _state["failed"] = True
                log.warning("embedding model %s unavailable, so search uses keywords only: %s: %s",
                            want, type(e).__name__, e)
        return _state["model"]


def _api(model: str) -> tuple[str, str] | None:
    """(provider, model) when the search model is a provider's, else None (a local model)."""
    provider, _, rest = model.partition("/")
    return (provider, rest) if provider in config.EMBED_PROVIDERS else None


def enabled() -> bool:
    return name() != "off" and not (_state["failed"] and _state["name"] == name())


def _encode_api(provider: str, model: str, texts: Sequence[str]) -> np.ndarray | None:
    try:
        rows: list[list[float]] = []
        for i in range(0, len(texts), API_BATCH):
            rows += providers.embed(provider, model, list(texts[i:i + API_BATCH]))
        return np.array(rows, dtype=np.float32)
    except providers.AIError as e:
        with _lock:
            _state.update(name=f"{provider}/{model}", model=None, failed=True)
        log.warning("embeddings from %s/%s unavailable, so search uses keywords only: %s", provider, model, e)
        return None


def encode(texts: Sequence[str], query: bool = False) -> np.ndarray | None:
    """Unit-length vectors, one row per text; None when embeddings are unavailable."""
    if not enabled():
        return None
    if api := _api(name()):
        got = _encode_api(*api, texts)  # provider models take queries as they are
        if got is None:
            return None
        vecs = got.reshape(len(texts), -1)
    else:
        if (m := _model()) is None:
            return None
        if query:
            texts = [QUERY_PREFIX + t for t in texts]
        parts = []
        for i in range(0, len(texts), BATCH):  # one ONNX session for the app's threads, held a batch at a time
            with _lock:  # so a query waits for at most one batch of background indexing
                parts += list(m.embed(list(texts[i:i + BATCH]), batch_size=BATCH))
        vecs = np.array(parts, dtype=np.float32).reshape(len(texts), -1)
    return vecs / np.maximum(np.linalg.norm(vecs, axis=1, keepdims=True), 1e-9)
