"""Embeddings of the searchable units, kept per video in data/<vid>/vectors.npz.

Each vector is keyed by a hash of the model and the text it embeds, so only new or changed text is
embedded again (a speaker rename, a rebuilt knowledge base). Living in the video's folder, the
vectors are deleted with the video.
"""
import hashlib
import os
from collections import defaultdict

import numpy as np

from tkb.config import DATA
from tkb.core import embed
from tkb.core.storage import locked

FILE = "vectors.npz"
LOCK = DATA / "vectors"  # data/.vectors.lock, one for every video: embedding runs a batch at a time anyway

_mem: dict[str, tuple[float, dict[str, np.ndarray]]] = {}  # vid -> (file mtime, {key: vector})


def text(u: dict) -> str:
    """What gets embedded: a passage as spoken; a concept or video with its name for context."""
    return u["text"] if u["type"] == "transcript" else f"{u['title']}: {u['text']}"


def _key(t: str) -> str:
    return hashlib.sha1(f"{embed.name()}\n{t}".encode()).hexdigest()[:16]


def _load(vid: str) -> dict[str, np.ndarray]:
    f = DATA / vid / FILE
    if not f.exists():
        return {}
    mtime = f.stat().st_mtime
    if (hit := _mem.get(vid)) and hit[0] == mtime:
        return hit[1]
    try:
        with np.load(f) as z:
            vecs = dict(zip(z["keys"].tolist(), z["vecs"].astype(np.float32), strict=True))
    except (OSError, ValueError, KeyError):  # damaged: embed again
        vecs = {}
    _mem[vid] = (mtime, vecs)
    return vecs


def _save(vid: str, vecs: dict[str, np.ndarray]) -> None:
    f = DATA / vid / FILE
    tmp = f.with_name(f".{FILE}.{os.getpid()}.tmp")
    try:
        with open(tmp, "wb") as out:
            np.savez(out, keys=np.array(list(vecs)), vecs=np.array(list(vecs.values()), dtype=np.float16))
        os.replace(tmp, f)
    except FileNotFoundError:  # the video was deleted meanwhile
        tmp.unlink(missing_ok=True)
        return
    _mem[vid] = (f.stat().st_mtime, vecs)


def stored(units: list[dict]) -> tuple[np.ndarray, int]:
    """One unit-length row per unit, in order, from what's already embedded (fast: no model needed).
    Rows not embedded yet are zeros, so they match nothing; also returns how many those are."""
    keys = [_key(text(u)) for u in units]
    found: dict[str, np.ndarray] = {}
    for vid in dict.fromkeys(u["vid"] for u in units):
        found.update(_load(vid))
    dim = len(next(iter(found.values()))) if found else 1
    zero = np.zeros(dim, np.float32)
    rows = [found.get(k, zero) for k in keys]
    return np.stack(rows) if rows else np.zeros((0, dim), np.float32), sum(k not in found for k in keys)


def build(units: list[dict]) -> bool:
    """Embed what's new or changed, video by video (slow: minutes for a big new library). False without a model."""
    if not embed.enabled():
        return False
    by_vid: dict[str, dict[str, str]] = defaultdict(dict)
    for u in units:
        t = text(u)
        by_vid[u["vid"]][_key(t)] = t
    with locked(LOCK):
        for vid, todo in by_vid.items():
            have = _load(vid)
            if not (missing := [k for k in todo if k not in have]):
                continue
            new = embed.encode([todo[k] for k in missing])
            if new is None:
                return False
            _save(vid, {k: have[k] for k in todo if k in have} | dict(zip(missing, new, strict=True)))
    return True
