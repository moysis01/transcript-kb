"""Small text helpers: timestamps, slugs, YouTube ids and YAML quoting."""
import json
import re


def fmt_ts(sec: float) -> str:
    s = int(sec)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def parse_ts(ts: str) -> int:
    parts = [int(p) for p in ts.strip("[] ").split(":")]
    sec = 0
    for p in parts:
        sec = sec * 60 + p
    return sec


def yt_link(video_id: str, sec: float) -> str:
    return f"https://youtu.be/{video_id}?t={int(sec)}"


def slugify(text: str, maxlen: int = 60) -> str:
    text = text.lower().replace("++", "-plus-plus").replace("#", "-sharp").replace("+", "-plus")
    s = re.sub(r"[\W_]+", "-", text).strip("-")
    return s[:maxlen].strip("-") or "untitled"


def video_id_from(url_or_id: str) -> str:
    m = re.search(r"(?:v=|youtu\.be/|shorts/|embed/|live/)([A-Za-z0-9_-]{11})", url_or_id)
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", url_or_id):
        return url_or_id
    raise ValueError(f"cannot find a YouTube video id in {url_or_id!r}")


def yq(value) -> str:
    """Quote a value as a YAML scalar (JSON strings are valid YAML)."""
    return json.dumps(value, ensure_ascii=False)


def words(text: str) -> int:
    return len(text.split())

