"""Concept notes edited by hand (e.g. in Obsidian) must neither crash the knowledge base nor lose the edits."""
import pytest

from tkb import config
from tkb.core import llm
from tkb.core.notes import Note, new_note
from tkb.core.storage import write_json

from fakes import FakeClaude

SECTION = "<!-- src:{vid} -->\n## From [[x-{vid}|{vid}]]\n\nfact\n<!-- /src:{vid} -->\n"


def test_untouched_notes_round_trip_byte_for_byte():
    note = new_note("Attention", "technique")
    note.put_section("AAAAAAAAAA1", SECTION.format(vid="AAAAAAAAAA1"))
    note.put_section("BBBBBBBBBB2", SECTION.format(vid="BBBBBBBBBB2"))
    text = note.render()
    assert Note.parse(text).render() == text
    assert text.startswith('---\nname: "Attention"\nkind: technique\naliases: []\n---\n\n# Attention\n\n<!-- src:A')
    assert "<!-- /src:AAAAAAAAAA1 -->\n\n<!-- src:BBBBBBBBBB2 -->" in text  # sections separated as before


def test_damaged_headers_fall_back_instead_of_crashing():
    assert Note.parse("---\nname: Hand Typed\n---\nbody").name("x") == "Hand Typed"  # unquoted
    assert Note.parse("---\nkind: tool\n---\nbody").name("multi-head-attention") == "Multi Head Attention"
    assert Note.parse("no front matter at all").kind() == "concept"
    assert Note.parse("---\naliases: [broken\n---\n").aliases() == []


@pytest.fixture
def claude(monkeypatch):
    fake = FakeClaude()
    fake.on("extract", lambda m: {"summary": "s", "outline": [], "concepts": [
        {"name": "Attention", "kind": "technique", "summary": "Weights tokens.", "facts": ["f"], "at": "00:01"}]})
    fake.on("synthesize", lambda m: {"tldr": "t", "takeaways": [], "tags": [], "canonical": []})
    monkeypatch.setattr(llm, "call", fake)
    return fake


def _video(vid):
    d = config.DATA / vid
    write_json(d / "meta.json", {"id": vid, "title": vid, "channel": "C", "duration": 9, "upload_date": "",
                                 "webpage_url": f"https://youtu.be/{vid}"})
    s = [{"id": 0, "start": 0, "end": 2, "para": 0, "src": "Attention.", "text": "Attention.", "by": "llm"}]
    write_json(d / "clean" / "000.json", {"v": 2, "index": 0, "start": 0, "end": 2, "noise": False, "warnings": [],
                                          "sentences": s, "text": "[00:00] Attention."})
    write_json(d / "clean.json", {"sponsorblock": "none", "sentences": s, "warnings": []})
    (d / "cleaned.md").write_text("x")


def test_hand_edits_survive_a_rebuild_and_a_delete(claude):
    from tkb.app.views import concept_files
    from tkb.pipeline import kb
    from tkb.services import delete
    _video("AAAAAAAAAA1")
    kb.build("AAAAAAAAAA1")
    f = config.KB / "concepts" / "attention.md"
    edited = (f.read_text()
              .replace("kind: technique\n", "")  # property deleted by hand
              .replace("---\n\n# Attention", "tags:\n  - ml\n  - nlp\n---\n\n# Attention\n\nMy own summary.")
              + "\nMy closing thoughts.\n")
    f.write_text(edited)

    _video("BBBBBBBBBB2")
    kb.build("BBBBBBBBBB2")  # used to crash on the missing kind; also used to wipe the user's text
    text = f.read_text()
    kept_parts = ("tags:\n  - ml\n  - nlp\n", "My own summary.", "My closing thoughts.",
                  "src:AAAAAAAAAA1", "src:BBBBBBBBBB2")
    for kept in kept_parts:
        assert kept in text, kept
    assert "Attention" in (config.KB / "index.md").read_text()
    assert concept_files()["attention"]["videos"] == 2

    for vid in ("AAAAAAAAAA1", "BBBBBBBBBB2"):  # deleting every source video keeps a note someone wrote in
        delete.delete(vid)
    text = f.read_text()
    assert "src:" not in text and "My own summary." in text and "My closing thoughts." in text


def test_a_note_nobody_wrote_in_still_goes_with_its_only_video(claude):
    from tkb.pipeline import kb
    from tkb.services import delete
    _video("AAAAAAAAAA1")
    kb.build("AAAAAAAAAA1")
    delete.delete("AAAAAAAAAA1")
    assert not (config.KB / "concepts" / "attention.md").exists()
