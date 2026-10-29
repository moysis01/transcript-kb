"""Who spoke when (local, sherpa-onnx), and who each speaker is (LLM, checked against the text).

Speaker ids are assigned to sentences at read time from diarization.json, never stored inside
LLM caches, so re-detecting or renaming speakers never invalidates cleaning or costs credits.
"""
import hashlib
import os
import re
import shutil
import tarfile
import tempfile
import urllib.request
import wave
from collections.abc import Callable
from pathlib import Path

import numpy as np

from tkb import config
from tkb.config import DATA
from tkb.core import llm
from tkb.core.resources import limit, slot
from tkb.core.storage import read_json, write_json
from tkb.core.text import fmt_ts
from tkb.prompts import prompt

MODELS = Path.home() / ".cache" / "tkb" / "models"
REL = "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
SEG = {"url": REL + "speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2",
       "sha256": "24615ee884c897d9d2ba09bb4d30da6bb1b15e685065962db5b02e76e4996488",
       "file": "sherpa-onnx-pyannote-segmentation-3-0/model.onnx"}
EMB = {"url": REL + "speaker-recongition-models/nemo_en_titanet_small.onnx",
       "sha256": "ad4a1802485d8b34c722d2a9d04249662f2ece5d28a7a039063ca22f515a789e",
       "file": "nemo_en_titanet_small.onnx"}
# Tuning (Settings → Speakers, read per detection): speakers.window seconds per voice sample inside each speech
# segment; Laplacian eigenvalues below speakers.sensitivity count as separate speaker groups; with less than
# speakers.min_speech seconds of samples the count isn't reliable, so one speaker is assumed unless told.
# Threads per detection: the cores split between the CPU slots (TKB_CPU), so parallel detections don't oversubscribe.
THREADS = None


def _fetch(m: dict) -> Path:
    """Download once to a temp file, verify sha256, then move into place."""
    target = MODELS / m["file"]
    if target.exists():
        return target
    MODELS.mkdir(parents=True, exist_ok=True)
    with (tempfile.NamedTemporaryFile(dir=MODELS, delete=False) as tmp,
          urllib.request.urlopen(m["url"], timeout=60) as r):
        h = hashlib.sha256()
        while chunk := r.read(1 << 20):
            h.update(chunk)
            tmp.write(chunk)
    tmp_path = Path(tmp.name)
    if h.hexdigest() != m["sha256"]:
        tmp_path.unlink()
        raise RuntimeError(f"checksum mismatch for {m['url']}")
    if m["url"].endswith(".tar.bz2"):  # extract aside, then move into place, so a crash can't leave half a model
        staging = Path(tempfile.mkdtemp(dir=MODELS))
        with tarfile.open(tmp_path) as t:
            t.extractall(staging, filter="data")
        tmp_path.unlink()
        top = m["file"].split("/")[0]
        shutil.rmtree(MODELS / top, ignore_errors=True)
        (staging / top).rename(MODELS / top)
        shutil.rmtree(staging)
    else:
        tmp_path.rename(target)
    return target


def _samples(path: Path) -> np.ndarray:
    with wave.open(str(path)) as w:
        assert w.getframerate() == 16000 and w.getnchannels() == 1 and w.getsampwidth() == 2
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0


def _kmeans(X: np.ndarray, k: int, iters: int = 50, restarts: int | None = None) -> np.ndarray:
    """k-means with k-means++ seeding; the labelling with the lowest inertia over several restarts."""
    restarts = restarts or config.get("speakers.restarts")
    rng = np.random.default_rng(0)
    best_inertia, best_labels = np.inf, np.zeros(len(X), int)
    for _ in range(restarts):
        seeds = [X[rng.integers(len(X))]]
        for _ in range(k - 1):  # k-means++ seeding
            d = np.min([((X - c) ** 2).sum(1) for c in seeds], axis=0)
            seeds.append(X[rng.choice(len(X), p=d / d.sum())] if d.sum() else X[rng.integers(len(X))])
        C = np.array(seeds)
        lab = np.argmin(((X[:, None] - C[None]) ** 2).sum(2), 1)
        for _ in range(iters):
            lab = np.argmin(((X[:, None] - C[None]) ** 2).sum(2), 1)
            C2 = np.array([X[lab == j].mean(0) if (lab == j).any() else C[j] for j in range(k)])
            if np.allclose(C2, C):
                break
            C = C2
        inertia = ((X - C[lab]) ** 2).sum()
        if inertia < best_inertia:
            best_inertia, best_labels = inertia, lab
    return best_labels


def _spectral(E: np.ndarray, k: int | None) -> tuple[int, np.ndarray]:
    """Spectral clustering of window embeddings; the speaker count comes from the eigengap
    among near-zero eigenvalues of the normalised graph Laplacian (each ~0 eigenvalue = one voice)."""
    n = len(E)
    min_windows = config.get("speakers.min_speech") / config.get("speakers.window")
    if n < 4 or (n < min_windows and not k):
        return 1, np.zeros(n, int)
    S = E @ E.T
    p = max(2, int(config.get("speakers.neighbours") * n))  # each window's closest (prunes cross-speaker noise)
    A = np.zeros_like(S)
    for i in range(n):
        top = np.argsort(-S[i])[:p]
        A[i, top] = S[i, top]
    A = np.clip((A + A.T) / 2, 0, None)
    dinv = 1 / np.sqrt(np.maximum(A.sum(1), 1e-9))
    w, V = np.linalg.eigh(np.eye(n) - dinv[:, None] * A * dinv[None, :])
    kmax = min(config.get("speakers.max"), n - 1)
    if not k:
        K = max(1, min(int((w[:kmax] < config.get("speakers.sensitivity")).sum()), kmax))
        k = int(np.argmax(w[1:K + 1] - w[:K])) + 1
    k = min(k, kmax)
    if k == 1:
        return 1, np.zeros(n, int)
    U = V[:, :k] / np.linalg.norm(V[:, :k], axis=1, keepdims=True)
    return k, _kmeans(U, k)


def diarize(vid: str, num_speakers: int | None = None,
            progress: Callable[[int, int], None] | None = None) -> dict:
    """Speech segments come from pyannote segmentation (via sherpa-onnx); speakers from our own
    window-level clustering, which counts speakers far more reliably than a fixed threshold."""
    import sherpa_onnx
    seg_model, emb_model = _fetch(SEG), _fetch(EMB)
    n_threads = THREADS or max(2, min(8, (os.cpu_count() or 8) // (limit("cpu") + 1)))  # leave room for Whisper
    samples = _samples(DATA / vid / "audio.wav")
    cfg = sherpa_onnx.OfflineSpeakerDiarizationConfig(
        segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
            pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(model=str(seg_model)),
            num_threads=n_threads),
        embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(emb_model), num_threads=n_threads),
        clustering=sherpa_onnx.FastClusteringConfig(num_clusters=-1, threshold=0.5),
        min_duration_on=config.get("speakers.min_on"), min_duration_off=config.get("speakers.min_off"))

    def cb(done: int, total: int) -> int:
        if progress:
            progress(done, total)
        return 0

    with slot("cpu"):  # uses several threads already; more at once would just thrash
        res = sherpa_onnx.OfflineSpeakerDiarization(cfg).process(samples, callback=cb).sort_by_start_time()
    segs = [{"start": round(r.start, 2), "end": round(r.end, 2), "spk": None} for r in res]
    wins, vecs = _window_embeddings(segs, samples, emb_model, n_threads)
    _, labels = _spectral(vecs, num_speakers)
    _label_segments(segs, wins, labels)
    _remap_overrides(vid, segs)
    talk: dict[str, float] = {}
    for g in segs:
        talk[g["spk"]] = talk.get(g["spk"], 0) + g["end"] - g["start"]
    total = sum(talk.values()) or 1
    out = {"num_speakers_hint": num_speakers, "segments": segs,
           "share": {k: round(v / total, 3) for k, v in talk.items()}, "multi": len(talk) > 1}
    write_json(DATA / vid / "diarization.json", out)
    return out


def _window_embeddings(segs: list[dict], samples: np.ndarray, emb_model: Path,
                       n_threads: int) -> tuple[list[tuple[int, float]], np.ndarray]:
    """A voice embedding for every few seconds of each speech segment: [(segment, seconds)], vectors."""
    import sherpa_onnx
    ext = sherpa_onnx.SpeakerEmbeddingExtractor(
        sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(emb_model), num_threads=n_threads))
    wins: list[tuple[int, float]] = []
    vecs: list[np.ndarray] = []
    window = config.get("speakers.window")
    for i, g in enumerate(segs):
        t = g["start"]
        while g["end"] - t >= 1.0:
            e = min(t + window, g["end"])
            if g["end"] - e < 1.0:
                e = g["end"]  # absorb a short tail
            st = ext.create_stream()
            st.accept_waveform(16000, samples[int(t * 16000):int(e * 16000)])
            st.input_finished()
            v = np.array(ext.compute(st))
            vecs.append(v / (np.linalg.norm(v) or 1))
            wins.append((i, e - t))
            t = e
    return wins, np.array(vecs)


def _label_segments(segs: list[dict], wins: list[tuple[int, float]], labels: np.ndarray) -> None:
    """Each segment gets the voice most of its windows had; S1, S2 ... in order of first appearance."""
    votes: dict[int, dict[int, float]] = {}
    for (i, dur), lab in zip(wins, labels, strict=True):
        votes.setdefault(i, {}).setdefault(int(lab), 0.0)
        votes[i][int(lab)] += dur
    for i, g in enumerate(segs):
        if i in votes:
            g["spk"] = max(votes[i].items(), key=lambda kv: kv[1])[0]
    labelled = [g for g in segs if g["spk"] is not None]
    for g in segs:  # segments too short to embed: nearest labelled neighbour in time
        if g["spk"] is None:
            start = g["start"]
            g["spk"] = min(labelled, key=lambda o: abs(o["start"] - start))["spk"] if labelled else 0
    order: dict[int, str] = {}
    for g in segs:
        g["spk"] = order.setdefault(g["spk"], f"S{len(order) + 1}")


def _remap_overrides(vid: str, new_segs: list[dict]) -> None:
    """Re-detection renumbers voices; carry each user rename to the new speaker that overlaps the old one
    most (at least half of its talk time), and drop renames with no clear match."""
    df, sf = DATA / vid / "diarization.json", DATA / vid / "speakers.json"
    if not (df.exists() and sf.exists()):
        return
    sp = read_json(sf)
    if not sp.get("overrides"):
        return
    old = read_json(df)["segments"]
    moved = {}
    for old_id, name in sp["overrides"].items():
        mine = [g for g in old if g["spk"] == old_id]
        talk = sum(g["end"] - g["start"] for g in mine) or 1
        ov: dict[str, float] = {}
        for g in mine:
            for h in new_segs:
                o = min(g["end"], h["end"]) - max(g["start"], h["start"])
                if o > 0:
                    ov[h["spk"]] = ov.get(h["spk"], 0) + o
        best = max(ov.items(), key=lambda kv: kv[1])[0] if ov else None
        if best and ov[best] >= 0.5 * talk and best not in moved:
            moved[best] = name
    sp["overrides"] = moved
    write_json(sf, sp)


def assign(sents: list[dict], vid: str) -> bool:
    """Set s["spk"] by largest time overlap (nearest segment if none). Returns whether the video is multi-speaker."""
    f = DATA / vid / "diarization.json"
    d = read_json(f) if f.exists() else None
    for s in sents:
        s.pop("spk", None)
    if not d or not d["multi"] or not d["segments"]:
        return False
    segs = d["segments"]
    for s in sents:
        best, best_ov = None, 0.0
        for g in segs:
            if g["end"] < s["start"] or g["start"] > s["end"]:
                continue
            ov = min(s["end"], g["end"]) - max(s["start"], g["start"])
            if ov > best_ov:
                best, best_ov = g["spk"], ov
        if best is None:
            mid = (s["start"] + s["end"]) / 2
            best = min(segs, key=lambda g: min(abs(g["start"] - mid), abs(g["end"] - mid)))["spk"]
        s["spk"] = best
    return True



NAME_SCHEMA = {"type": "object", "properties": {"speakers": {"type": "array", "items": {
    "type": "object", "properties": {
        "id": {"type": "string"}, "name": {"type": "string"}, "role": {"type": "string"},
        "evidence": {"type": "string"}},
    "required": ["id", "name", "role", "evidence"]}}}, "required": ["speakers"]}


def _norm(t: str) -> str:
    """Words only, so a quote with slightly different punctuation still counts as verbatim."""
    return " ".join(re.findall(r"\w+", t.lower()))


def _close(a: str, b: str) -> bool:
    """Equal, or one edit apart (speech recognition writes "Carl" for "Karl")."""
    if a == b:
        return True
    if abs(len(a) - len(b)) > 1 or min(len(a), len(b)) < 4:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b, strict=True)) == 1
    short, long_ = sorted((a, b), key=len)
    return any(long_[:i] + long_[i + 1:] == short for i in range(len(long_)))


def name_speakers(vid: str, sents: list[dict], model: str | None = None) -> dict:
    d = read_json(DATA / vid / "diarization.json")
    f = DATA / vid / "speakers.json"
    old = read_json(f) if f.exists() else {}
    if not d["multi"]:
        out = {"multi": False, "speakers": {}, "overrides": old.get("overrides", {})}
        write_json(f, out)
        return out
    meta = read_json(DATA / vid / "meta.json")
    by: dict[str, list[dict]] = {}
    for s in sents:
        by.setdefault(s["spk"], []).append(s)
    # Each speaker's first lines, plus every line around a speaker change (where introductions happen).
    blocks = []
    for k in sorted(by, key=lambda k: int(k[1:])):
        lines = by[k][:config.get("speakers.name_lines")]
        blocks.append(f"## {k} ({round(d['share'].get(k, 0) * 100)}% of talk time)\n"
                      + "\n".join(f"({fmt_ts(s['start'])}) {s['src']}" for s in lines))
    # The opening, plus the lead-up to every speaker change: introductions and "So Jane, ..." happen right before.
    keep = set(range(min(config.get("speakers.name_opening"), len(sents))))
    for i in range(1, len(sents)):
        if sents[i]["spk"] != sents[i - 1]["spk"]:
            keep.update(range(max(0, i - 3), min(len(sents), i + 2)))
    turns = [f"({fmt_ts(sents[i]['start'])}) {sents[i]['spk']}: {sents[i]['src']}" for i in sorted(keep)][:400]
    description = (meta.get("description") or "")[:1500]
    message = (f"Title: {meta['title']}\nChannel: {meta.get('channel')}\nDescription:\n{description}\n\n"
              + "\n\n".join(blocks) + "\n\n## Conversation (start, then every speaker change)\n" + "\n".join(turns))
    res = llm.call(prompt("name_speakers"), message, schema=NAME_SCHEMA,
                   model=model or config.model("speakers"))
    meta_text = _norm(meta["title"] + " " + (meta.get("description") or ""))
    haystack = meta_text + " " + _norm(" ".join(s["src"] for s in sents))
    speakers = {}
    for k in by:
        r: dict = next((x for x in res["speakers"] if x["id"] == k), {})
        name, ev = r.get("name", "").strip(), r.get("evidence", "").strip()
        grounded = bool(name and ev and _norm(ev) in haystack and (
            _norm(name) in meta_text or any(_close(p, w) for p in _norm(name).split() if len(p) > 2
                                            for w in _norm(ev).split())))
        speakers[k] = {"name": name if grounded else "", "role": r.get("role", ""),
                       "evidence": ev if grounded else "", "share": d["share"].get(k, 0),
                       "rejected": name if name and not grounded else ""}
    out = {"multi": True, "speakers": speakers, "overrides": old.get("overrides", {})}
    write_json(f, out)
    return out


def names(vid: str) -> dict[str, str]:
    """Display name per speaker id: user override > grounded LLM name > "Speaker N"."""
    f = DATA / vid / "speakers.json"
    if not f.exists():
        return {}
    sp = read_json(f)
    if not sp.get("multi"):
        return {}
    roles = [v.get("role", "") for v in sp["speakers"].values() if not v["name"]]

    def fallback(k: str, v: dict) -> str:  # an unnamed but unambiguous role reads better than a number
        r = v.get("role", "")
        return r.capitalize() if r and r != "other" and roles.count(r) == 1 else f"Speaker {k[1:]}"
    return {k: sp["overrides"].get(k) or v["name"] or fallback(k, v) for k, v in sp["speakers"].items()}
