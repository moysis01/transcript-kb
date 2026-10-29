"""Saved Discover searches and Ask questions: one JSON file each in data/.tasks/.

The store is plain files so deleting a video can scrub them without the web app running.
"""
from tkb import config
from tkb.config import DATA
from tkb.core.storage import read_json, write_json

DIR = DATA / ".tasks"


def read_all() -> list[dict]:
    """Every saved task; a file pruned between listing and reading is just skipped."""
    out = []
    for f in DIR.glob("*.json") if DIR.exists() else []:
        try:
            out.append(read_json(f))
        except (FileNotFoundError, ValueError):
            continue
    return out


def save(t: dict) -> None:
    write_json(DIR / f"{t['id']}.json", t)


def load(tid: str) -> dict | None:
    try:
        return read_json(DIR / f"{tid}.json") if tid.isalnum() else None
    except (FileNotFoundError, ValueError):
        return None


def recent(kind: str | None = None, limit: int = 20) -> list[dict]:
    ts = [t for t in read_all() if kind in (None, t["kind"])]
    return sorted(ts, key=lambda t: -t["created"])[:limit]


def prune(kind: str) -> None:
    done = [t for t in recent(kind, limit=10_000) if t["state"] in ("done", "error")]
    for t in done[config.get("tasks.keep"):]:
        (DIR / f"{t['id']}.json").unlink(missing_ok=True)


def purge_video(vid: str) -> None:
    """Remove every trace of a deleted video from saved tasks: answers that cite it are deleted
    outright; Discover results drop it from their candidates."""
    for t in read_all():
        f = DIR / f"{t['id']}.json"
        r = t.get("result") or {}
        if t["kind"] == "ask" and any(s.get("vid") == vid for s in r.get("sources", [])):
            f.unlink(missing_ok=True)
        elif t["kind"] == "discover" and r:
            hit = any(c["id"] == vid for c in r.get("picks", []) + r.get("others", []))
            if hit:
                r["picks"] = [c for c in r["picks"] if c["id"] != vid]
                r["others"] = [c for c in r["others"] if c["id"] != vid]
                write_json(f, t)

