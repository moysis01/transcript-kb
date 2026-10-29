"""Hybrid search: meaning-based matches on top of keywords, an index that's reused and deleted with its video."""
import re
import zlib

import numpy as np
import pytest

from tkb import config
from tkb.core import embed
from tkb.core.storage import write_json

SYNONYMS = [{"cost", "price", "pricing", "cents", "expensive"}, {"founder", "ceo", "started", "created"}]


class FakeEmbedder:
    """Words in the same synonym group share a dimension; every other word gets its own (hashed)."""

    def __init__(self):
        self.texts: list[str] = []

    def __call__(self, texts, query=False):
        self.texts += [] if query else list(texts)
        out = np.zeros((len(texts), 64), np.float32)
        for row, t in enumerate(texts):
            for w in re.findall(r"[a-z]+", t.lower()):
                group = next((i for i, g in enumerate(SYNONYMS) if w in g), None)
                out[row, group if group is not None else 2 + zlib.crc32(w.encode()) % 62] += 1
        return out / np.maximum(np.linalg.norm(out, axis=1, keepdims=True), 1e-9)


@pytest.fixture
def embedder(monkeypatch):
    fake = FakeEmbedder()
    monkeypatch.setattr(embed, "enabled", lambda: True)
    monkeypatch.setattr(embed, "encode", fake)
    return fake


FILLER = ["Weather in the mountains turns quickly", "A recipe for bread with seeds", "Football scores from Sunday",
          "Painting a fence before winter", "Trains running late again", "Birds migrating south in autumn",
          "A guide to tuning guitars", "Chess openings for beginners", "Planting tomatoes in spring",
          "Repairing a bicycle puncture", "Knitting a scarf with wool", "Stars visible in July",
          "Cleaning a cast iron pan", "Swimming technique drills", "Learning to juggle three balls",
          "Making coffee with a press", "The history of lighthouses", "Folding paper cranes"]


def _video(vid: str, passages: list[str]) -> None:
    d = config.DATA / vid
    write_json(d / "meta.json", {"id": vid, "title": "A video", "channel": "C", "duration": 60})
    write_json(d / "kb.json", {"tldr": "", "takeaways": [], "tags": [], "concepts": []})
    sents = [{"id": i, "start": i * 30.0, "end": i * 30.0 + 5, "para": i, "src": p, "text": p, "by": "llm"}
             for i, p in enumerate(passages)]
    write_json(d / "clean.json", {"sponsorblock": "none", "sentences": sents, "warnings": []})


def test_meaning_finds_a_paraphrase_that_keywords_miss(embedder):
    from tkb.services import search
    _video("VVVVVVVVVV1", ["Pricing: four cents.", *FILLER])
    assert not search.search("cost", types=("transcript",)), "no shared keyword"
    search.warm()
    hits = search.search("cost", types=("transcript",))
    assert hits and hits[0]["text"] == "Pricing: four cents."
    assert not search.search("quantum chromodynamics lattice", types=("transcript",)), "unrelated: no matches"


def test_the_index_is_reused_and_deleted_with_its_video(embedder):
    from tkb.services import delete, search
    _video("VVVVVVVVVV1", ["The founder used to work at a big lab.", *FILLER])
    search.warm()
    first = len(embedder.texts)
    search.warm()
    assert len(embedder.texts) == first, "unchanged text is never embedded again"
    _video("VVVVVVVVVV2", ["Another video entirely.", *FILLER[:3]])
    search.warm()
    assert len(embedder.texts) == first + 5, "only the new video's text (4 passages + its video entry)"
    assert (config.DATA / "VVVVVVVVVV1" / "vectors.npz").exists()
    delete.delete("VVVVVVVVVV1")
    assert not (config.DATA / "VVVVVVVVVV1").exists()
    assert all(h["vid"] != "VVVVVVVVVV1" for h in search.search("who created it"))


def test_search_falls_back_to_keywords_when_the_model_cannot_load(monkeypatch):
    from tkb.services import search
    monkeypatch.setattr(embed, "name", lambda: "not-a-real/model")
    monkeypatch.setattr(embed, "_state", {"name": None, "model": None, "failed": False})
    import builtins
    real_import = builtins.__import__

    def no_fastembed(name, *a, **k):
        if name == "fastembed":
            raise ImportError("no fastembed here")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", no_fastembed)
    _video("VVVVVVVVVV1", ["Calibrated probabilities make routing easy.", *FILLER])
    search.warm()
    hits = search.search("calibrated probabilities", types=("transcript",))
    assert hits and "Calibrated" in hits[0]["text"] and embed._state["failed"]
