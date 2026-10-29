"""Community-labelled ad segments from SponsorBlock (sponsor.ajay.app).

Uses the hash-prefix endpoint so the exact video id is never sent: we request
every video whose sha256 starts with the same 4 hex chars and filter locally.
"""
import hashlib
import json
import urllib.error
import urllib.parse
import urllib.request

from tkb.config import DATA
from tkb.core.storage import read_json, write_json

# The categories cut by default (Settings → Noise removal picks others); a cache without a "categories"
# field was fetched with these.
CATEGORIES = ["sponsor", "selfpromo", "interaction"]
API = "https://sponsor.ajay.app/api/skipSegments/"


def fetch(vid: str, categories: list[str] = CATEGORIES) -> dict:
    prefix = hashlib.sha256(vid.encode()).hexdigest()[:4]
    url = API + prefix + "?categories=" + urllib.parse.quote(json.dumps(list(categories)))
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            rows = json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 404:  # no labelled video shares this prefix
            return {"status": "none", "segments": []}
        return {"status": "error", "error": str(e), "segments": []}
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        return {"status": "error", "error": str(e), "segments": []}
    segs = [{"category": s["category"], "start": s["segment"][0], "end": s["segment"][1]}
            for row in rows if row.get("videoID") == vid for s in row.get("segments", [])]
    return {"status": "ok" if segs else "none", "segments": segs}


def segments(vid: str, enabled: bool = True, categories: list[str] = CATEGORIES) -> dict:
    """Cached per video and set of categories; failures are recorded but never fatal (and retried next run)."""
    f = DATA / vid / "sponsorblock.json"
    if not enabled or not categories:
        return {"status": "disabled", "segments": []}
    if f.exists():
        cached = read_json(f)  # a cache from before categories were recorded holds the default ones
        if cached["status"] != "error" and cached.get("categories", CATEGORIES) == list(categories):
            return {k: v for k, v in cached.items() if k != "categories"}
    res = fetch(vid, categories)
    write_json(f, res if list(categories) == CATEGORIES else {**res, "categories": list(categories)})
    return res
