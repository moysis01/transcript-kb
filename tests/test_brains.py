"""The system's 'brains' as Settings: changing them never re-cleans or changes a video you already have
(each video is pinned to the settings it was cleaned with); new videos and Redo take the new values."""
import pytest
from test_claude_paths import SEGS, _run_cli

from tkb import config
from tkb.core import llm, youtube
from tkb.services import settings

from fakes import FakeClaude, FakeYouTube

VID = "PPPPPPPPPP1"


def label_intro(message: str) -> dict:
    """Cleaning that marks the greeting as an intro and keeps everything else as said."""
    import re
    out = []
    for i, t in re.findall(r"^\[(\d+)\] \([\d:]+\) (?:[^:\n]+: )?(.*)$", message, re.M):
        out.append({"id": int(i), "category": "intro_outro"} if t.startswith("Welcome") else {"id": int(i), "text": t})
    return {"sentences": out}


@pytest.fixture
def pipeline(monkeypatch):
    from tkb.pipeline import sponsorblock, transcribe
    fake = FakeClaude()
    monkeypatch.setattr(llm, "call", fake)
    monkeypatch.setattr(youtube, "extract", FakeYouTube())
    monkeypatch.setattr(sponsorblock, "fetch", lambda vid, cats=None: {"status": "none", "segments": []})
    monkeypatch.setattr(transcribe, "_run_whisper", lambda *a, **k: {"language": "en", "segments": SEGS})
    monkeypatch.setattr(transcribe, "_release", lambda mlx: None)
    fake.on("clean", label_intro)
    fake.on("extract", lambda m: {"summary": "s", "outline": [], "concepts": [
        {"name": "Jev", "kind": "tool", "summary": "A model.", "facts": ["f"], "at": "00:05"}]})
    fake.on("synthesize", lambda m: {"tldr": "t", "takeaways": [], "tags": [], "canonical": []})
    return fake


def run(*extra: str) -> None:
    _run_cli("run", VID, "--no-speakers", "--jsonl", *extra)


CHANGED = {"noise.remove": ["sponsor"], "sponsorblock.categories": ["sponsor", "intro"],
           "sponsorblock.overlap": 0.9, "chunk.max_words": 600, "chunk.min_words": 100,
           "sentence.max_words": 15, "sentence.target_words": 8, "noise.ad_phrases": "welcome",
           "chunk.chapters": False, "chunk.tail": 0.9, "clean.rewrite_ratio": 1.0, "clean.check_numbers": False,
           "clean.context_chars": 0}


def _clean_json() -> bytes:
    return (config.DATA / VID / "clean.json").read_bytes()


def test_changing_the_brains_never_touches_a_cleaned_video_until_redo(pipeline):
    run()
    before, calls = _clean_json(), len(pipeline.calls)
    assert b'"category": "intro_outro"' in before, "the greeting was cut as an intro"
    settings.save(CHANGED)
    run()
    assert len(pipeline.calls) == calls, "a rerun after changing Settings costs no Claude calls"
    assert _clean_json() == before, "and changes nothing"
    run("--redo")
    after = _clean_json()
    assert after != before and b'"kept": true' in after, "Redo takes the new Settings: intros are now kept"


def test_videos_cleaned_before_pins_existed_are_frozen_too(pipeline):
    from tkb.pipeline import clean
    run()
    before, calls = _clean_json(), len(pipeline.calls)
    (config.DATA / VID / "clean" / clean.PIN).unlink()  # as if cleaned by an older version
    settings.save(CHANGED)
    run()
    assert len(pipeline.calls) == calls and _clean_json() == before
    from tkb.core.storage import read_json
    assert read_json(config.DATA / VID / "clean" / clean.PIN)["chunk"] == clean.LEGACY["chunk"]


def test_a_kind_of_noise_you_keep_comes_back_as_spoken_for_free(pipeline):
    settings.save({"noise.remove": [k for k, _ in config.NOISE if k != "intro_outro"]})
    run()
    from tkb.core.storage import read_json
    greeting = read_json(config.DATA / VID / "clean.json")["sentences"][0]
    assert greeting["text"] == "Welcome to the channel." and greeting["kept"]
    assert pipeline.count("clean") == 1, "no extra Claude call to keep it"
    assert "Welcome to the channel." in (config.DATA / VID / "cleaned.md").read_text()


def test_concept_kinds_reach_claude_and_unknown_ones_fall_back(pipeline, monkeypatch):
    seen = []
    real = pipeline.__call__

    def spy(system, message, *, schema=None, **kw):
        if schema and "concepts" in schema.get("properties", {}):
            seen.append(schema["properties"]["concepts"]["items"]["properties"]["kind"]["enum"])
        return real(system, message, schema=schema, **kw)
    monkeypatch.setattr(llm, "call", spy)
    settings.save({"notes.kinds": "tool\nmethod\npaper"})
    run()
    assert seen and seen[0] == ["tool", "method", "paper", "other"]


def test_ignored_words_apply_without_any_video_changing():
    from tkb.core.storage import write_json
    from tkb.services import search
    d = config.DATA / "VVVVVVVVVV1"
    write_json(d / "meta.json", {"id": "VVVVVVVVVV1", "title": "A video", "channel": "C", "duration": 60})
    write_json(d / "kb.json", {"tldr": "", "takeaways": [], "tags": [], "concepts": []})
    said = "Routing with Jev."
    write_json(d / "clean.json", {"sponsorblock": "none", "warnings": [], "sentences": [
        {"id": 0, "start": 0, "end": 2, "para": 0, "src": said, "text": said, "by": "llm"}]})
    assert search.search("routing", types=("transcript",))
    settings.save({"search.stop_words": "routing"})
    assert not search.search("routing", types=("transcript",)), "now an ignored word"


def test_extra_searches_follow_the_setting_and_zero_skips_the_call(monkeypatch):
    from tkb.services import discover
    fake = FakeClaude()
    monkeypatch.setattr(llm, "call", fake)
    monkeypatch.setattr(youtube, "extract", FakeYouTube(searches={"jev": []}))
    fake.on("discover_queries", lambda m: {"queries": ["a", "b", "c", "d"]})
    settings.save({"discover.phrasings": 3})
    r = discover.discover([{"topic": "jev"}])
    assert r["queries"][0] == ["jev", "a", "b", "c"] and "Write 3 queries." in fake.calls[0][1]
    settings.save({"discover.phrasings": 0})
    discover.discover([{"topic": "jev"}], only_new=False)
    assert fake.count("discover_queries") == 1, "no call at all for zero extra searches"


def _pool_and_scores():
    pool = [{"id": f"V{i:010d}", "channel": f"C{i}", "searches": {0 if i < 3 else 1}, "views": v, "upload_date": d}
            for i, (v, d) in enumerate([(10, "20200101"), (900, "20260101"), (50, "20250101"), (5, "20240101")])]
    scored = {0: {"score": 90}, 1: {"score": 60}, 2: {"score": 80}, 3: {"score": 50}}
    return pool, scored


def test_discover_picks_follow_the_ranking_settings():
    from tkb.services import discover
    pool, scored = _pool_and_scores()
    searches = [{"channel": False}, {"channel": False}]
    assert discover._choose(pool, scored, searches, 2) == [0, 3], "best of each search first: 3 is search 2's"
    settings.save({"discover.cover_searches": False})
    assert discover._choose(pool, scored, searches, 2) == [0, 2], "now simply the two best scores"
    settings.save({"discover.order": "views"})
    assert discover._choose(pool, scored, searches, 2) == [1, 2], "the most viewed above the lowest score"
    settings.save({"discover.min_score": 70})
    assert discover._choose(pool, scored, searches, 2) == [2, 0], "...never below the lowest score picked"


def test_search_ranking_weights_and_citation_checking(monkeypatch):
    from tkb.core.storage import write_json
    from tkb.services import search
    d = config.DATA / "VVVVVVVVVV1"
    write_json(d / "meta.json", {"id": "VVVVVVVVVV1", "title": "Routing", "channel": "C", "duration": 60})
    write_json(d / "kb.json", {"tldr": "routing routing", "takeaways": [], "tags": [], "concepts": []})
    said = "Routing decisions with Jev."
    write_json(d / "clean.json", {"sponsorblock": "none", "warnings": [], "sentences": [
        {"id": 0, "start": 0, "end": 2, "para": 0, "src": said, "text": said, "by": "llm"}]})
    assert search.search("routing")[0]["type"] == "video"
    settings.save({"search.transcript_boost": 3.0, "search.video_boost": 0.1})
    assert search.search("routing")[0]["type"] == "transcript"
    settings.save({"search.keyword_weight": 0})
    assert not search.search("routing"), "keywords switched off (and meaning is off in tests)"
    settings.save({"search.keyword_weight": 1})
    fake = FakeClaude()
    monkeypatch.setattr(llm, "call", fake)
    fake.on("ask", lambda m: "Yes [VVVVVVVVVV1@00:00] and [ZZZZZZZZZZ9@01:00].")
    assert "ZZZZZZZZZZ9" not in search.ask("routing")["answer"]
    settings.save({"ask.check_citations": False})
    assert "[ZZZZZZZZZZ9@01:00]" in search.ask("routing")["answer"]


def test_notes_can_keep_synonyms_apart_and_ignore_the_library(pipeline):
    pipeline.on("synthesize", lambda m: {"tldr": "t", "takeaways": [], "tags": [],
                                         "canonical": [{"raw": "Jev", "canonical": "Jev Model"}]})
    settings.save({"notes.merge_synonyms": False, "notes.link_existing": False})
    run()
    assert (config.KB / "concepts" / "jev.md").exists(), "not renamed into Jev Model"
    synth_message = next(m for name, m in pipeline.calls if name == "synthesize")
    assert "Existing wiki concept names:\n(none)" in synth_message
