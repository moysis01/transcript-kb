"""The parts driven by Claude: Discover's choices, knowledge-base building, Ask, and a full pipeline run."""
import io
import json
import re
from contextlib import redirect_stdout

import pytest

from tkb import config
from tkb.core import llm, youtube
from tkb.core.storage import read_json, write_json

from fakes import FakeClaude, FakeYouTube, keep_every_sentence


@pytest.fixture
def claude(monkeypatch):
    fake = FakeClaude()
    monkeypatch.setattr(llm, "call", fake)
    return fake


# ------------------------------------------------------------------ Discover

def _candidates(prefix: str, channel: str, n: int) -> list[dict]:
    return [{"id": f"{prefix}{i:010d}"[-11:], "title": f"{prefix} {i}", "channel": channel} for i in range(n)]


@pytest.fixture
def two_searches(monkeypatch, claude):
    """Search A finds 6 great videos from one channel; search B finds 2 decent ones from another."""
    a, b = _candidates("A", "Big Channel", 6), _candidates("B", "Small Channel", 2)
    yt = FakeYouTube(searches={"topic a": a, "topic b": b})
    monkeypatch.setattr(youtube, "extract", yt)
    scores = {**{c["id"]: 95 - i for i, c in enumerate(a)}, b[0]["id"]: 60, b[1]["id"]: 30}

    def rank(message):
        ids = re.findall(r"^\[(\d+)\] (A|B) (\d+)$", message, re.M)
        return {"scores": [{"n": int(n), "score": scores[f"{p}{int(i):010d}"[-11:]], "reason": "r"} for n, p, i in ids]}

    claude.on("discover_queries", lambda m: {"queries": []}).on("discover_rank", rank)
    return a, b


def test_every_search_is_represented_and_channels_are_capped(two_searches):
    from tkb.services import discover
    a, b = two_searches
    r = discover.discover([{"topic": "topic a"}, {"topic": "topic b"}], picks=4)
    picked = [c["id"] for c in r["picks"]]
    assert b[0]["id"] in picked, "search B's best candidate must be picked even though A's all score higher"
    assert b[1]["id"] not in picked, "a candidate under the score floor is never picked"
    assert sum(c["channel"] == "Big Channel" for c in r["picks"]) == config.get("discover.per_channel")
    assert [c["score"] for c in r["picks"]] == sorted((c["score"] for c in r["picks"]), reverse=True)


def test_videos_offered_before_are_not_offered_again(two_searches):
    from tkb.services import discover
    first = discover.discover([{"topic": "topic a"}], picks=2)
    second = discover.discover([{"topic": "topic a"}], picks=2)
    assert first["picks"] and not second["picks"]
    assert second["dropped"]["offered before for this topic"] == 6


def test_shorts_live_and_library_videos_are_skipped(monkeypatch, claude):
    from tkb.services import discover
    entries = [{"id": "SHORT000001", "title": "s", "channel": "c", "duration": 30},
               {"id": "LIVE0000001", "title": "l", "channel": "c", "duration": None},
               {"id": "HAVE0000001", "title": "h", "channel": "c"},
               {"id": "GOOD0000001", "title": "g", "channel": "c"}]
    monkeypatch.setattr(youtube, "extract", FakeYouTube(searches={"t": entries}))
    write_json(config.DATA / "HAVE0000001" / "kb.json", {"concepts": []})
    claude.on("discover_queries", lambda m: {"queries": []}).on(
        "discover_rank", lambda m: {"scores": [{"n": 0, "score": 80, "reason": "r"}]})
    r = discover.discover([{"topic": "t"}], picks=3)
    assert [c["id"] for c in r["picks"]] == ["GOOD0000001"]
    assert r["dropped"] == {"too short": 1, "live or upcoming": 1, "already in your library": 1}


# ------------------------------------------------------------------ knowledge base

def _cleaned_video(vid: str, sentences: list[str]) -> None:
    d = config.DATA / vid
    write_json(d / "meta.json", {"id": vid, "title": f"Title {vid}", "channel": "C", "duration": 60,
                                 "upload_date": "20260101", "webpage_url": f"https://youtu.be/{vid}"})
    sents = [{"id": i, "start": i * 5.0, "end": i * 5.0 + 4, "para": 0, "src": s, "text": s, "by": "llm"}
             for i, s in enumerate(sentences)]
    write_json(d / "clean" / "000.json", {"v": 2, "index": 0, "start": 0, "end": 60, "noise": False, "warnings": [],
                                          "sentences": sents, "text": "[00:00] " + " ".join(sentences)})
    write_json(d / "clean.json", {"sponsorblock": "none", "sentences": sents, "warnings": []})
    (d / "cleaned.md").write_text("# cleaned\n")


@pytest.fixture
def kb_claude(claude):
    claude.on("extract", lambda m: {"summary": "About LLMs.", "outline": [{"title": "Intro", "at": "00:00"}],
                                    "concepts": [{"name": "LLM", "kind": "concept", "summary": "A language model.",
                                                  "facts": ["Predicts the next word."], "at": "00:05"}]})
    claude.on("synthesize", lambda m: {"tldr": "A video about LLMs.", "takeaways": ["t"], "tags": ["ai"],
                                       "canonical": [{"raw": "LLM", "canonical": "Large Language Model"}]})
    return claude


def test_kb_build_merges_names_and_reuses_its_synthesis(kb_claude):
    from tkb.pipeline import kb
    _cleaned_video("VVVVVVVVVV1", ["Large language models predict words.", "That is all."])
    kb.build("VVVVVVVVVV1")
    note = (config.KB / "concepts" / "large-language-model.md").read_text()
    assert 'name: "Large Language Model"' in note and '"LLM"' in note  # renamed, old name kept as an alias
    assert "Predicts the next word." in note and "src:VVVVVVVVVV1" in note
    assert "Large Language Model" in (config.KB / "index.md").read_text()
    assert read_json(config.DATA / "VVVVVVVVVV1" / "kb.json")["concepts"][0]["aliases"] == ["LLM"]
    kb.build("VVVVVVVVVV1")  # notes and synthesis are cached: no new Claude calls
    assert kb_claude.count("extract") == 1 and kb_claude.count("synthesize") == 1


def test_a_second_video_adds_its_own_section_to_a_shared_concept(kb_claude):
    from tkb.pipeline import kb
    for vid in ("VVVVVVVVVV1", "VVVVVVVVVV2"):
        _cleaned_video(vid, ["Large language models predict words."])
        kb.build(vid)
    note = (config.KB / "concepts" / "large-language-model.md").read_text()
    assert note.count("<!-- src:") == 2


# ------------------------------------------------------------------ Ask

def test_ask_drops_citations_that_were_not_retrieved(claude):
    from tkb.services import search
    _cleaned_video("VVVVVVVVVV1", ["Calibrated probabilities make Jev useful for routing."])
    write_json(config.DATA / "VVVVVVVVVV1" / "kb.json", {"tldr": "Jev routing", "takeaways": [], "tags": [],
                                                         "concepts": []})
    claude.on("ask", lambda m: "Jev returns calibrated probabilities [VVVVVVVVVV1@00:00]. Also [ZZZZZZZZZZ9@01:00].")
    r = search.ask("calibrated probabilities routing")
    assert "[VVVVVVVVVV1@00:00]" in r["answer"] and "ZZZZZZZZZZ9" not in r["answer"]
    assert r["dropped_citations"] == ["[ZZZZZZZZZZ9@01:00]"]


def test_ask_with_an_empty_library_makes_no_claude_call(claude):
    from tkb.services import search
    assert "Nothing in the knowledge base" in search.ask("anything")["answer"] and not claude.calls


# ------------------------------------------------------------------ the full pipeline, through the CLI

SEGS = [{"start": 0.0, "end": 4.0, "text": "Welcome to the channel.", "no_speech_prob": 0.0, "avg_logprob": -0.1},
        {"start": 4.0, "end": 9.0, "text": "Jev is a decision model from TypeSafe.", "no_speech_prob": 0.0,
         "avg_logprob": -0.1},
        {"start": 9.0, "end": 14.0, "text": "It returns calibrated probabilities.", "no_speech_prob": 0.0,
         "avg_logprob": -0.1}]


def _run_cli(*argv) -> list[dict]:
    from tkb import cli
    buf = io.StringIO()
    with redirect_stdout(buf), pytest.raises(SystemExit) as done:
        import sys
        old, sys.argv = sys.argv, ["tkb", *argv]
        try:
            cli.main()
        finally:
            sys.argv = old
    assert done.value.code == 0, buf.getvalue()
    return [json.loads(line) for line in buf.getvalue().splitlines() if line.startswith("{")]


def test_full_pipeline_then_a_rerun_that_costs_nothing(monkeypatch, kb_claude):
    from tkb.pipeline import sponsorblock, transcribe
    monkeypatch.setattr(youtube, "extract", FakeYouTube())
    monkeypatch.setattr(sponsorblock, "fetch", lambda vid, cats=None: {"status": "none", "segments": []})
    monkeypatch.setattr(transcribe, "_run_whisper", lambda *a, **k: {"language": "en", "segments": SEGS})
    monkeypatch.setattr(transcribe, "_release", lambda mlx: None)
    kb_claude.on("clean", keep_every_sentence)

    events = _run_cli("run", "PPPPPPPPPP1", "--no-speakers", "--jsonl")
    stages = [e["stage"] for e in events if e["event"] == "stage_done"]
    assert stages == ["ingest", "transcribe", "clean", "kb"] and events[-1]["event"] == "done"
    assert (config.KB / "concepts" / "large-language-model.md").exists()
    timings = read_json(config.DATA / "PPPPPPPPPP1" / "timings.json")
    assert set(timings) >= {"ingest", "transcribe", "clean", "kb", "waited"}
    calls = len(kb_claude.calls)

    _run_cli("run", "PPPPPPPPPP1", "--no-speakers", "--jsonl")
    assert len(kb_claude.calls) == calls, "a rerun with everything cached must not call Claude"


def test_cli_rejects_unknown_videos_for_delete(capsys):
    import sys

    from tkb import cli
    sys.argv = ["tkb", "delete", "QQQQQQQQQQ1", "--yes", "--port", "9"]
    with pytest.raises(SystemExit) as done:
        cli.main()
    assert done.value.code == 1 and "not in the library" in capsys.readouterr().out
