"""Turn whatever the user pasted (videos, playlists, channels) into a list of video ids."""
import re

from tkb.core import youtube
from tkb.core.text import video_id_from

BARE_ID = re.compile(r"[A-Za-z0-9_-]{11}")


def _is_collection(url: str) -> bool:
    return bool(re.search(r"youtube\.com/(?:playlist\?|@|channel/|c/|user/)", url))


def expand(text: str, limit: int = 50) -> tuple[list[dict], int]:
    """Return (entries, total_found). Entries are {"id", "title"}; capped at `limit` (0 = no cap)."""
    found: list[dict] = []
    # A bare id must be a whole token, and not a plain lowercase word like "programming"
    # (real ids are random mixed-case strings; a full URL always works).
    tokens = [t for t in text.split()
              if t.startswith("http") or (BARE_ID.fullmatch(t) and not (t.isalpha() and t.islower()))]
    for tok in tokens:
        if tok.startswith("http") and _is_collection(tok):
            url = tok
            if re.search(r"youtube\.com/@[^/]+/?$", url):  # bare channel -> its uploads tab
                url = url.rstrip("/") + "/videos"
            info = youtube.extract(url, {"extract_flat": "in_playlist"})
            for e in info.get("entries") or []:
                if e and e.get("id") and len(e["id"]) == 11:
                    found.append({"id": e["id"], "title": e.get("title") or e["id"]})
        else:
            try:
                found.append({"id": video_id_from(tok), "title": None})
            except ValueError:
                continue
    seen, uniq = set(), []
    for e in found:
        if e["id"] not in seen:
            seen.add(e["id"])
            uniq.append(e)
    return (uniq[:limit] if limit else uniq), len(uniq)
