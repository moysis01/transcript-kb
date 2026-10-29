"""Search over the knowledge base, and question answering grounded in it.

Search is hybrid: keyword ranking (BM25) finds exact terms and names, embeddings find passages that
say the same thing in other words; the two rankings are merged by weighted reciprocal rank fusion. Without an
embedding model, search is keywords only. Every weight, boost and constant is a setting (Settings → Search).
"""
import math
import re
import threading
from collections import Counter, defaultdict

import numpy as np

from tkb import config
from tkb.config import DATA
from tkb.core import embed, llm
from tkb.core.storage import read_json
from tkb.core.text import fmt_ts, parse_ts
from tkb.pipeline.clean import paragraphs
from tkb.pipeline.diarize import names
from tkb.prompts import prompt
from tkb.services import vectors

# Ignored words, the BM25 constants (k1, b), the concept boost and the fusion constant are Settings → Search.
_cache: dict = {"key": None, "units": None, "df": None, "avg": 1.0, "vecs": None}
# A passage counts as a meaning match only if it stands out from the rest for this query: at least this many
# standard deviations above the mean similarity (an absolute cut-off would depend on the model). Strict (3) was
# measured on 10 paraphrased questions over a 1,500-passage library: keyword MRR@5 0.48, hybrid 0.58 (8/10 found).
OUTLIER_Z = {"strict": 3.0, "normal": 2.5, "loose": 2.0}


_stop: dict = {"words": None, "set": frozenset()}


def _tok(text: str) -> list[str]:
    words = config.get("search.stop_words")
    if _stop["words"] != words:
        _stop.update(words=words, set=frozenset(w.lower() for w in words))
    return [t for t in re.findall(r"\w+", text.lower()) if t not in _stop["set"] and len(t) > 1]


def _passages(clean: dict, names: dict) -> list[dict]:
    """Searchable passages: one per paragraph (and speaker), the speaker named at the start."""
    return [{"t": int(p["start"]), "text": (f"{p['speaker']}: " if p["speaker"] else "") + p["text"]}
            for p in paragraphs(clean.get("sentences", []), names)]


def units() -> list[dict]:
    """Searchable units, rebuilt only when a video's files change."""
    files = sorted(DATA.glob("*/kb.json")) + sorted(DATA.glob("*/clean.json")) + sorted(DATA.glob("*/speakers.json"))
    key = (config.get("search.stop_words"), *((str(f), f.stat().st_mtime) for f in files))  # new words: re-index
    if _cache["key"] == key:
        return _cache["units"]
    out = []
    for kbf in sorted(DATA.glob("*/kb.json")):
        vid = kbf.parent.name
        meta, k = read_json(kbf.parent / "meta.json"), read_json(kbf)
        title = meta["title"]
        out.append({"type": "video", "vid": vid, "t": 0, "title": title,
                    "text": " ".join([k["tldr"], *k["takeaways"], " ".join(k["tags"])])})
        for c in k["concepts"]:
            out.append({"type": "concept", "vid": vid, "t": (c["times"] or [0])[0], "title": c["name"],
                        "slug": c["slug"], "video_title": title,
                        "text": " ".join([c["name"], c["summary"], *c["facts"]])})
        cf = kbf.parent / "clean.json"
        if cf.exists():
            for p in _passages(read_json(cf), names(vid)):
                out.append({"type": "transcript", "vid": vid, "t": p["t"], "title": title, "text": p["text"]})
    df: Counter = Counter()
    for u in out:
        u["tf"] = Counter(_tok(u["title"] + " " + u["text"]))
        u["len"] = sum(u["tf"].values()) or 1
        df.update(u["tf"].keys())
    _cache.update(key=key, units=out, df=df, avg=(sum(u["len"] for u in out) / len(out)) if out else 1.0)
    return out


_building = threading.Lock()


def pending() -> int:
    """How many searchable units aren't embedded yet."""
    return vectors.stored(units())[1] if embed.enabled() else 0


def warm() -> None:
    """Embed whatever is new or changed (blocking). Searches meanwhile use what's already embedded."""
    with _building:
        if vectors.build(us := units()):
            _cache["vecs"] = None  # reload, now complete
            _vectors(us, background=False)


def _vectors(us: list[dict], background: bool = True) -> np.ndarray | None:
    """Vectors for these units, never waiting for embedding: missing ones are embedded in the background."""
    if not embed.enabled():
        return None
    cached = _cache["vecs"]
    if cached is not None and cached[0] is us and cached[1] == embed.name():  # for these units, by this model
        return cached[2]
    vecs, missing = vectors.stored(us)
    if not missing:
        _cache["vecs"] = (us, embed.name(), vecs)
    elif background and not _building.locked():
        threading.Thread(target=warm, name="embed", daemon=True).start()
    return vecs if missing < len(us) else None


def _keyword_ranking(query: str, us: list[dict], pool: list[int]) -> list[int]:
    q = _tok(query)
    n, df, avg = len(us), _cache["df"], _cache["avg"]
    scored = []
    k1, b = config.get("search.k1"), config.get("search.b")
    boost = {"concept": config.get("search.concept_boost"), "video": config.get("search.video_boost"),
             "transcript": config.get("search.transcript_boost")}
    for i in pool:
        u, s = us[i], 0.0
        for t in q:
            f = u["tf"].get(t, 0)
            if f:
                idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
                s += idf * f * (k1 + 1) / (f + k1 * (1 - b + b * u["len"] / avg))
        if s > 0:
            scored.append((s * boost.get(u["type"], 1.0), i))
    scored.sort(key=lambda x: -x[0])
    return [i for _, i in scored[:config.get("search.depth")]]


def _meaning_ranking(query: str, us: list[dict], pool: list[int]) -> list[int]:
    vecs = _vectors(us)
    qv = embed.encode([query], query=True) if vecs is not None else None
    if vecs is None or qv is None or not pool:
        return []
    sims = vecs[pool] @ qv[0]
    floor = sims.mean() + OUTLIER_Z[config.get("search.strictness")] * sims.std()
    top = np.argsort(-sims)[:config.get("search.depth")]
    return [pool[j] for j in top if sims[j] > floor]  # strict: equal scores match nothing


def search(query: str, k: int = 30, types: tuple = ("video", "concept", "transcript")) -> list[dict]:
    us = units()
    if not query.strip() or not us:
        return []
    pool = [i for i, u in enumerate(us) if u["type"] in types]
    fused: dict[int, float] = defaultdict(float)
    k = config.get("search.fusion")
    for weight, ranking in ((config.get("search.keyword_weight"), _keyword_ranking(query, us, pool)),
                            (config.get("search.meaning_weight"), _meaning_ranking(query, us, pool))):
        if not weight:
            continue
        for rank, i in enumerate(ranking):
            fused[i] += weight / (k + rank + 1)
    best = sorted(fused, key=lambda i: -fused[i])[:k]
    return [{**{k2: v for k2, v in us[i].items() if k2 not in ("tf", "len")}, "score": round(fused[i] * 1000, 1)}
            for i in best]


CITE_RE = re.compile(r"\[([A-Za-z0-9_-]{11})@(\d+(?::\d+)+)\]")


def ask(question: str, model: str | None = None) -> dict:
    g = config.get
    hits = [h for kind, n in (("transcript", g("ask.transcript")), ("concept", g("ask.concepts")),
                              ("video", g("ask.videos"))) if n for h in search(question, n, (kind,))]
    if not hits:
        return {"answer": "Nothing in the knowledge base matches that question yet.", "sources": [],
                "dropped_citations": []}
    sources, seen = [], set()
    for h in hits:
        label = f"{h['vid']}@{fmt_ts(h['t'])}"
        if (label, h["type"]) in seen:
            continue
        seen.add((label, h["type"]))
        head = h["title"] if h["type"] != "concept" else f"{h['title']} (concept, from {h['video_title']})"
        sources.append({"label": label, "vid": h["vid"], "t": h["t"], "type": h["type"],
                        "title": head, "text": h["text"][:config.get("ask.chars")]})
    ctx = "\n\n".join(f"SOURCE [{s['label']}] {s['title']}\n{s['text']}" for s in sources)
    answer = llm.call(prompt("ask"), f"{ctx}\n\n---\nQuestion: {question}", model=model or config.model("ask"))
    allowed = {s["label"] for s in sources}
    dropped = []

    strict = config.get("ask.check_citations")

    def check(m: re.Match) -> str:
        label = f"{m.group(1)}@{fmt_ts(parse_ts(m.group(2)))}"
        if label in allowed or not strict:
            return f"[{label}]"
        dropped.append(m.group(0))
        return ""

    answer = CITE_RE.sub(check, answer)
    return {"answer": answer, "sources": sources, "dropped_citations": dropped}
