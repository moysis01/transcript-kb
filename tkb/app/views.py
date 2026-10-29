"""Read models for the web app: what each page shows, assembled from data/ and kb/."""
import re
from pathlib import Path
from typing import Any

from tkb.config import DATA, KB
from tkb.core.notes import Note
from tkb.core.storage import read_json
from tkb.pipeline import diarize


def _maybe(path: Path, default: Any = None) -> Any:
    return read_json(path) if path.exists() else default


def library() -> list[dict]:
    out = []
    for mf in DATA.glob("*/meta.json"):
        d = mf.parent
        meta, k, c = read_json(mf), _maybe(d / "kb.json"), _maybe(d / "clean.json")
        removed: dict[str, int] = {}
        for s in (c or {}).get("sentences", []):
            if not s.get("text"):
                removed[s["category"]] = removed.get(s["category"], 0) + 1
        out.append({"id": meta["id"], "title": meta["title"], "channel": meta.get("channel"),
                    "duration": meta.get("duration"), "upload_date": meta.get("upload_date"),
                    "ready": k is not None, "tldr": k["tldr"] if k else None,
                    "n_concepts": len(k["concepts"]) if k else 0, "removed": removed,
                    "warnings": len((c or {}).get("warnings", [])),
                    "updated": (d / "kb.json").stat().st_mtime if k else mf.stat().st_mtime})
    reg_f = KB / ".videos.json"  # notes whose working files are gone: listed so they can still be deleted
    for vid, r in (read_json(reg_f) if reg_f.exists() else {}).items():
        if not (DATA / vid).exists():
            out.append({"id": vid, "title": r.get("title", vid), "channel": r.get("channel"), "duration": None,
                        "upload_date": (r.get("date") or "").replace("-", ""), "ready": False, "orphan": True,
                        "tldr": None, "n_concepts": 0, "removed": {}, "warnings": 0, "updated": 0})
    return sorted(out, key=lambda v: -v["updated"])


def video(vid: str) -> dict | None:
    d = DATA / vid
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", vid) or not (d / "meta.json").exists():
        return None
    meta = read_json(d / "meta.json")
    c = _maybe(d / "clean.json", {})
    keep = ("id", "title", "channel", "duration", "upload_date", "chapters", "webpage_url")
    sp, shown = _maybe(d / "speakers.json", {}), diarize.names(vid)
    speakers = [{"id": k, "name": shown[k], "auto_name": v.get("name", ""), "role": v.get("role", ""),
                 "share": v.get("share", 0), "evidence": v.get("evidence", ""), "renamed": k in sp.get("overrides", {})}
                for k, v in sp.get("speakers", {}).items() if k in shown]
    t = _maybe(d / "transcript.json", {}) or {}
    transcript = {"language": t.get("language"), "translated": t.get("task") == "translate", "model": t.get("model")}
    return {"meta": {k: meta.get(k) for k in keep}, "kb": _maybe(d / "kb.json"), "speakers": speakers,
            "transcript": transcript,
            "speakers_checked": (d / "diarization.json").exists(),
            "sentences": c.get("sentences", []), "warnings": c.get("warnings", []),
            "sponsorblock": c.get("sponsorblock")}



def concept_files() -> dict[str, dict]:
    out = {}
    for f in (KB / "concepts").glob("*.md"):
        note = Note.parse(f.read_text(encoding="utf-8"))
        out[f.stem] = {"slug": f.stem, "name": note.name(f.stem), "kind": note.kind(), "aliases": note.aliases(),
                       "videos": len(note.sources())}
    return out


def concept(slug: str) -> dict | None:
    info = concept_files().get(slug)
    if not info:
        return None
    sections = []
    for kf in DATA.glob("*/kb.json"):
        for c in read_json(kf)["concepts"]:
            if c["slug"] == slug:
                meta = read_json(kf.parent / "meta.json")
                sections.append({"vid": meta["id"], "title": meta["title"], **c})
    return {**info, "sections": sections}


def graph() -> dict:
    nodes, edges, names = [], [], concept_files()
    for kf in DATA.glob("*/kb.json"):
        meta = read_json(kf.parent / "meta.json")
        nodes.append({"id": "v:" + meta["id"], "label": meta["title"], "type": "video"})
        for c in read_json(kf)["concepts"]:
            if c["slug"] in names:
                edges.append({"from": "v:" + meta["id"], "to": "c:" + c["slug"]})
    used = {e["to"] for e in edges}
    nodes += [{"id": "c:" + s, "label": n["name"], "type": "concept", "kind": n["kind"], "videos": n["videos"]}
              for s, n in names.items() if "c:" + s in used]
    return {"nodes": nodes, "edges": edges}

