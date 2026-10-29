"""Deleting a video must leave no trace anywhere, including saved tasks and shared concept notes."""
from tkb import config
from tkb.core.storage import read_json, write_json
from tkb.core.text import yq
from tkb.pipeline.kb import write_index
from tkb.services import delete


def _concept(name, sections, aliases=()):
    body = "".join(f"<!-- src:{vid} -->\n## From [[x-{vid}|{vid}]]\n\nfact\n<!-- /src:{vid} -->\n" for vid in sections)
    return f"---\nname: {yq(name)}\nkind: concept\naliases: {yq(list(aliases))}\n---\n\n# {name}\n\n{body}"


def test_delete_leaves_no_trace(home):
    gone, keep = "AAAAAAAAAA1", "BBBBBBBBBB2"
    D, K = config.DATA, config.KB
    for vid, concepts in ((gone, ["shared", "only"]), (keep, ["shared"])):
        (D / vid).mkdir(parents=True)
        write_json(D / vid / "meta.json", {"id": vid, "title": f"Title {vid}"})
        write_json(D / vid / "kb.json", {"concepts": [{"slug": c, "kind": "concept", "aliases": [f"{c} alias {vid}"]}
                                                           for c in concepts]})
    (K / "concepts").mkdir(parents=True)
    (K / "videos").mkdir()
    (K / "transcripts").mkdir()
    (K / "concepts" / "shared.md").write_text(_concept("Shared", [gone, keep], [f"shared alias {gone}"]))
    (K / "concepts" / "only.md").write_text(_concept("Only", [gone]))
    for vid in (gone, keep):
        (K / "videos" / f"title-{vid}.md").write_text(f"# {vid}")
        (K / "transcripts" / f"{vid}.md").write_text(f"# {vid}")
    reg = {v: {"title": f"Title {v}", "slug": f"title-{v}", "tldr": "", "date": "", "channel": ""}
           for v in (gone, keep)}
    write_json(K / ".videos.json", reg)
    write_index(reg)
    write_json(D / ".jobs.json", [{"vid": gone}, {"vid": keep}])
    write_json(D / ".topics.json", {"t": {"topic": "t", "offered": [gone, keep], "runs": [{"picked": [gone]}]}})
    write_json(D / ".tasks" / "ask1.json", {"id": "ask1", "kind": "ask", "state": "done",
                                                 "result": {"sources": [{"vid": gone}]}})
    write_json(D / ".tasks" / "disc1.json", {"id": "disc1", "kind": "discover", "state": "done",
                                                  "result": {"picks": [{"id": gone}, {"id": keep}], "others": []}})

    delete.delete(gone)

    leftovers = [p for p in home.rglob("*") if p.is_file() and "locks" not in p.parts
                 and gone in p.read_text(errors="ignore")]
    assert leftovers == [], f"traces left in {leftovers}"
    assert not (K / "concepts" / "only.md").exists()
    shared = (K / "concepts" / "shared.md").read_text()
    assert f"src:{keep}" in shared and "shared alias BBBBBBBBBB2" in shared
    assert not (D / ".tasks" / "ask1.json").exists()
    assert read_json(D / ".tasks" / "disc1.json")["result"]["picks"] == [{"id": keep}]
