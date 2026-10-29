"""Translation decisions, with Whisper replaced by stand-ins (no GPU or audio needed)."""
import pytest

from tkb import config
from tkb.core.storage import read_json, write_json
from tkb.pipeline import transcribe as tr

SEG = {"start": 0.0, "end": 2.0, "text": "Hello there.", "no_speech_prob": 0.0, "avg_logprob": -0.1}


@pytest.fixture
def video(monkeypatch):
    d = config.DATA / "vid00000001"
    d.mkdir(parents=True)
    write_json(d / "meta.json", {"id": "vid00000001", "title": "T", "channel": "C", "duration": 2,
                                 "webpage_url": "https://youtu.be/vid00000001"})
    (d / "audio.wav").write_bytes(b"")
    calls = []

    def run(mlx, audio, model, meta, words, task="transcribe", language=None):
        calls.append({"model": model, "words": words, "task": task, "language": language})
        return {"language": language or "en", "segments": [SEG]}

    monkeypatch.setattr(tr, "_run_whisper", run)
    monkeypatch.setattr(tr, "_release", lambda mlx: None)
    return d, calls


def test_english_is_never_translated(video, monkeypatch):
    d, calls = video
    monkeypatch.setattr(tr, "_detect_language", lambda *a: "en")
    doc = tr.transcribe("vid00000001", "turbo", words=True, translate=True)
    assert doc["task"] == "transcribe" and calls[0]["model"] == tr.MODELS[tr.engine()]["turbo"] and calls[0]["words"]


def test_non_english_is_translated_with_large_v3_and_no_word_timestamps(video, monkeypatch):
    d, calls = video
    monkeypatch.setattr(tr, "_detect_language", lambda *a: "hi")
    doc = tr.transcribe("vid00000001", "turbo", words=True, translate=True)
    large = tr.MODELS[tr.engine()]["large-v3"]
    assert calls == [{"model": large, "words": False, "task": "translate", "language": "hi"}]
    assert doc["task"] == "translate" and doc["language"] == "hi"
    assert "translated_from: hi" in (d / "transcript.md").read_text()


def test_existing_untranslated_transcript_is_redone_only_when_translation_is_asked(video, monkeypatch):
    d, calls = video
    write_json(d / "transcript.json", {"language": "hi", "task": "transcribe", "model": "m", "segments": [SEG],
                                       "paragraphs": [{"start": 0, "end": 2, "text": "x"}]})
    monkeypatch.setattr(tr, "_detect_language", lambda *a: "hi")
    assert tr.transcribe("vid00000001", translate=False)["task"] == "transcribe" and not calls  # cached
    assert tr.transcribe("vid00000001", translate=True)["task"] == "translate" and len(calls) == 1
    assert read_json(d / "transcript.json")["task"] == "translate"


def test_clean_cache_is_not_reused_when_sentence_text_changed(monkeypatch):
    """Same sentence numbering, different words (e.g. after translation): the chunk must be cleaned again."""
    from tkb.pipeline import clean
    d = config.DATA / "vid00000002"
    (d / "clean").mkdir(parents=True)
    write_json(d / "meta.json", {"id": "vid00000002", "title": "T", "duration": 4, "webpage_url": "u"})
    write_json(d / "transcript.json", {"language": "en", "model": "m",
                                       "segments": [{"start": 0, "end": 2, "text": "New words here."}],
                                       "paragraphs": [{"start": 0, "end": 2, "text": "New words here."}]})
    stale = {"v": clean.CACHE_VERSION, "index": 0, "start": 0, "end": 2, "noise": False, "warnings": [],
             "text": "[00:00] Old words.", "sentences": [{"id": 0, "start": 0, "end": 2, "para": 0,
                                                          "src": "Old words.", "text": "Old words.", "by": "llm"}]}
    write_json(d / "clean" / "000.json", stale)
    cleaned = []
    monkeypatch.setattr(clean, "_clean_chunk", lambda meta, sents, model, names=None, ads=None, rules=None: (
        cleaned.append(sents) or ([{**s, "text": s["src"], "by": "llm"} for s in sents], [])))
    clean.clean("vid00000002", use_sponsorblock=False)
    assert cleaned and cleaned[0][0]["src"] == "New words here."
