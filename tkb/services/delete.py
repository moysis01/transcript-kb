"""Remove a video completely: its files, its sections in shared concept notes, and every reference to it."""
import fcntl
import re
import shutil

from tkb.config import DATA, KB
from tkb.core.notes import Note
from tkb.core.storage import locked, read_json, write_json, write_text
from tkb.core.text import yq
from tkb.pipeline.kb import write_index
from tkb.services.taskstore import purge_video

VID_RE = re.compile(r"[A-Za-z0-9_-]{11}")


def _concepts_with(vid: str) -> tuple[list[str], list[str]]:
    """(concept notes that would lose a section, notes that would disappear entirely)."""
    shared: list[str] = []
    only: list[str] = []
    for f in sorted((KB / "concepts").glob("*.md")):
        note = Note.parse(f.read_text(encoding="utf-8"))
        if vid in note.sources():  # a note someone wrote in is kept, even if this was its only video
            (only if note.sources() == [vid] and not note.has_user_text() else shared).append(f.stem)
    return shared, only


def exists(vid: str) -> bool:
    """Anything at all left for this video, including notes whose working files are already gone."""
    reg = KB / ".videos.json"
    return bool(VID_RE.fullmatch(vid)) and (
        (DATA / vid).exists() or (reg.exists() and vid in read_json(reg))
        or any((KB / "videos").glob(f"*-{vid}.md")) or (KB / "transcripts" / f"{vid}.md").exists())


def preview(vid: str) -> dict:
    """What delete() would remove, for the confirmation prompt."""
    if not exists(vid):
        raise FileNotFoundError(vid)
    meta = read_json(DATA / vid / "meta.json") if (DATA / vid / "meta.json").exists() else {}
    reg = read_json(KB / ".videos.json") if (KB / ".videos.json").exists() else {}
    shared, only = _concepts_with(vid)
    size = sum(f.stat().st_size for f in (DATA / vid).rglob("*") if f.is_file()) if (DATA / vid).exists() else 0
    return {"id": vid, "title": meta.get("title") or reg.get(vid, {}).get("title", vid), "concepts_deleted": len(only),
            "concepts_trimmed": len(shared), "mb": round(size / 1e6, 1)}


def _drop_from_note(f, vid: str, remaining: list[dict]) -> None:
    """Remove the video's section; rebuild the aliases and kind it may have contributed from what's left."""
    note = Note.parse(f.read_text(encoding="utf-8"))
    note.drop_section(vid)
    name = note.name(f.stem)
    note.set("aliases", yq([a for a in sorted({a for c in remaining for a in c.get("aliases", [])}) if a != name]))
    if remaining:
        note.set("kind", remaining[0]["kind"])
    write_text(f, note.render())


def delete(vid: str) -> dict:
    """Delete everything about `vid`. Callers must make sure no job is processing it."""
    info = preview(vid)
    KB.mkdir(parents=True, exist_ok=True)
    with open(KB / ".lock", "w") as lock:  # same lock as kb.build: never interleave with a wiki write
        fcntl.flock(lock, fcntl.LOCK_EX)
        # 1. its sections in concept notes; notes that only it supported are removed
        shared, only = _concepts_with(vid)
        for slug in only:
            (KB / "concepts" / f"{slug}.md").unlink()
        others = [read_json(k) for k in DATA.glob("*/kb.json") if k.parent.name != vid]
        for slug in shared:
            _drop_from_note(KB / "concepts" / f"{slug}.md", vid,
                            [c for k in others for c in k["concepts"] if c["slug"] == slug])
        # 2. its own notes, the registry entry, and the index
        for f in list((KB / "videos").glob(f"*-{vid}.md")) + [KB / "transcripts" / f"{vid}.md"]:
            f.unlink(missing_ok=True)
        reg_f = KB / ".videos.json"
        reg = read_json(reg_f) if reg_f.exists() else {}
        reg.pop(vid, None)
        write_json(reg_f, reg)
        write_index(reg)
    # 3. all of its working files (audio, transcripts, speakers, caches)
    shutil.rmtree(DATA / vid, ignore_errors=True)
    # 4. saved Ask answers and Discover results, queue history, and Discover's memory of having offered it
    purge_video(vid)
    jobs_f = DATA / ".jobs.json"
    if jobs_f.exists():
        write_json(jobs_f, [j for j in read_json(jobs_f) if j.get("vid") != vid])
    topics_f = DATA / ".topics.json"
    with locked(topics_f):
        topics = read_json(topics_f) if topics_f.exists() else {}
        for t in topics.values():
            t["offered"] = [x for x in t.get("offered", []) if x != vid]
            for r in [*t.get("runs", []), (t.get("watch") or {}).get("result") or {}]:
                r["picked"] = [x for x in r.get("picked", []) if x != vid]
        if topics:
            write_json(topics_f, topics)
    return info
