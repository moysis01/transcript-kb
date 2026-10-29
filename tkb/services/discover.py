"""Find the best videos for one or more searches, as fast as the YouTube and AI budgets allow.

Several searches (each with its own keywords and filters) run at once:
1. Each search's keywords go to YouTube immediately, while a fast model writes extra phrasings (Settings:
   2 by default), searched as soon as they arrive. YouTube's own filters are applied server-side (sp=).
2. Cheap local filters: Shorts (unless included), too short, live streams, and anything already in the
   library, queue, or offered before.
3. The best candidates of every search are enriched in parallel (chapters, tags, description, language).
4. One AI call scores every candidate; code then picks: never below the lowest score, at most so many per
   channel for keyword searches, each search's best first (unless turned off), in the order chosen in Settings.

A search can instead be a channel (@handle or channel URL): then its candidates are everything the channel
published (videos, Shorts, past live streams, its playlists, videos its posts link to), the AI picks the
most substantial from the whole catalogue at a glance (no extended thinking: 5 s instead of 100+), and those
go through the same reading, scoring and choosing, judged on substance alone. Keyword searches never see
these rules.
"""
import base64
import time
import urllib.parse
from collections.abc import Callable, Collection
from concurrent.futures import ThreadPoolExecutor, as_completed

from tkb import config
from tkb.config import DATA
from tkb.core import llm, youtube
from tkb.core.storage import locked, read_json, write_json
from tkb.prompts import prompt
from tkb.services import channels

# Verified against live results: duration 1 = <4 min, 3 = 4-20 min, 2 = >20 min;
# date 2 = today ... 5 = this year; sort 3 = view count.
DURATION = {"any": None, "short": 1, "medium": 3, "long": 2}
DATE = {"any": None, "today": 2, "week": 3, "month": 4, "year": 5}
SORT = {"relevance": None, "popularity": 3}
TOPICS = DATA / ".topics.json"


def _sp(duration: str, date: str, sort: str) -> str:
    inner = b""
    if (d := DATE[date]) is not None:
        inner += bytes([0x08, d])
    inner += bytes([0x10, 1])  # type = video
    if (n := DURATION[duration]) is not None:
        inner += bytes([0x18, n])
    top = (bytes([0x08, o]) if (o := SORT[sort]) is not None else b"") + bytes([0x12, len(inner)]) + inner
    return base64.b64encode(top).decode()


def search(query: str, n: int = 25, duration="any", date="any", sort="relevance") -> list[dict]:
    url = ("https://www.youtube.com/results?search_query=" + urllib.parse.quote(query)
           + "&sp=" + urllib.parse.quote(_sp(duration, date, sort)))
    entries = youtube.extract(url, {"extract_flat": True, "playlistend": n}).get("entries") or []
    return [{"id": e["id"], "title": e.get("title") or "", "channel": e.get("channel") or e.get("uploader") or "",
             "duration": e.get("duration"), "views": e.get("view_count"), "snippet": e.get("description") or "",
             "url": e.get("url") or "", "query": query, "rank": i}
            for i, e in enumerate(entries) if e and e.get("id") and len(e["id"]) == 11]


VARIANT_SCHEMA = {"type": "object", "properties": {"queries": {"type": "array", "items": {"type": "string"}}},
                  "required": ["queries"]}

RANK_SCHEMA = {"type": "object", "properties": {"scores": {"type": "array", "items": {
    "type": "object",
    "properties": {"n": {"type": "integer"}, "score": {"type": "integer"}, "reason": {"type": "string"}},
    "required": ["n", "score", "reason"]}}}, "required": ["scores"]}

PRESCREEN_SCHEMA = {"type": "object", "properties": {"best": {"type": "array", "items": {"type": "integer"}}},
                    "required": ["best"]}
LENGTHS = {"short": (0, 240), "medium": (240, 1200), "long": (1200, 10**9)}  # seconds, as YouTube's filter
DAYS = {"today": 1, "week": 7, "month": 31, "year": 366}


def _enrich(c: dict) -> dict:
    try:
        info = youtube.extract(c["id"], {"skip_download": True})
    except youtube.Unavailable as e:
        return {**c, "dropped": "unavailable (age-gated, private or removed)", "why": str(e)}
    except youtube.RateLimited:
        return {**c, "dropped": "YouTube rate limit (try again in a few minutes)"}
    if info.get("live_status") in ("is_live", "is_upcoming", "post_live"):
        return {**c, "dropped": "live stream"}
    return {**c, "upload_date": info.get("upload_date"), "language": info.get("language"),
            "description": (info.get("description") or "")[:900], "tags": (info.get("tags") or [])[:12],
            "chapters": [ch["title"] for ch in info.get("chapters") or []][:20],
            "views": info.get("view_count") or c["views"], "likes": info.get("like_count"),
            "duration": info.get("duration") or c["duration"], "channel": info.get("channel") or c["channel"]}


def load_topics() -> dict:
    """Every search's memory (data/.topics.json): what it offered, its runs and filters, and its watch settings."""
    return read_json(TOPICS) if TOPICS.exists() else {}


def normalize(s: dict) -> dict:
    """One search: keywords plus YouTube filters, validated."""
    out = {"topic": " ".join(str(s.get("topic", "")).split())[:200],
           "duration": s.get("duration") or "any", "date": s.get("date") or "any", "sort": s.get("sort") or "relevance"}
    if not out["topic"]:
        raise ValueError("every search needs keywords")
    if out["duration"] not in DURATION or out["date"] not in DATE or out["sort"] not in SORT:
        raise ValueError("unknown filter value")
    out["channel"] = channels.is_channel(out["topic"])
    return out


def discover(searches: list[dict], picks: int = 5, only_new: bool = True, exclude: Collection[str] = (),
             model: str | None = None, progress: Callable[[str], None] | None = None) -> dict:
    """Run every search, filter, enrich and score the candidates, and choose up to `picks` of them."""
    searches = [normalize(s) for s in searches]
    if not searches:
        raise ValueError("add at least one search")
    picks = max(1, min(int(picks), config.get("discover.max_picks")))
    model = model or config.model("discover")
    say = progress or (lambda msg: None)
    _resolve_channels(searches, say)
    offered: set[str] = set()
    if only_new:
        saved = load_topics()
        for s in searches:
            offered |= set(saved.get(s["topic"].lower(), {}).get("offered", []))

    queries, results = _search_all(searches, say)
    dropped: dict[str, int] = {}
    cands = _keep_eligible(_merge(results), searches, offered, set(exclude), dropped)
    budget = _per_search(len(searches), picks)
    extra = {i: _prescreen(cands, i, s, budget, say, model) for i, s in enumerate(searches) if s["channel"]}
    pool = _shortlist(cands, len(searches), budget, extra)
    say(f"Reading the details of {len(pool)} candidates")
    pool = [c for c in _enrich_all(pool, dropped) if _fits_after_details(c, searches, dropped)]
    if not pool:
        return {"searches": searches, "queries": list(queries.values()), "picks": [], "others": [],
                "dropped": dropped, "minutes": 0}
    say(f"Scoring {len(pool)} candidates")
    scored = _score(pool, searches, model)
    chosen = _choose(pool, scored, searches, picks)
    _remember(searches, pool, chosen)

    def public(n: int) -> dict:
        c = {k: v for k, v in pool[n].items() if k != "searches"}
        c["searches"] = sorted(pool[n]["searches"])
        if n in scored:
            c.update(score=scored[n]["score"], reason=scored[n]["reason"])
        return c

    order = sorted(scored, key=lambda n: -scored[n]["score"])
    picked = sorted(chosen, key=lambda n: -scored[n]["score"])
    return {"searches": searches, "queries": list(queries.values()), "picks": [public(n) for n in picked],
            "others": [public(n) for n in order if n not in chosen], "dropped": dropped,
            "minutes": round(sum(pool[n]["duration"] or 0 for n in chosen) / 60)}


def _resolve_channels(searches: list[dict], say: Callable[[str], None]) -> None:
    """Look each channel up once: a wrong handle fails here with a clear message, and every spelling of a channel
    (@Name, @name, its URL) becomes the same @handle, so it shares one topic memory."""
    todo = [s for s in searches if s["channel"]]
    if not todo:
        return
    say(f"Looking up {len(todo)} channel{'s' * (len(todo) > 1)}")
    with ThreadPoolExecutor(len(todo)) as ex:
        for s, ch in zip(todo, ex.map(lambda s: channels.resolve(s["topic"]), todo), strict=True):
            s.update(topic=ch["handle"], name=ch["name"], url=ch["url"])


def _search_all(searches: list[dict], say: Callable[[str], None]) -> tuple[dict[int, list[str]], list]:
    """Every search's own keywords at once; its variant queries as soon as the fast model writes them.
    A channel search lists the channel's tabs and playlists instead."""
    say(f"Searching YouTube for {len(searches)} search{'es' * (len(searches) > 1)}")
    queries = {i: [s["topic"]] for i, s in enumerate(searches)}
    results: list[tuple[int, list[dict]]] = []
    with ThreadPoolExecutor(16) as ex:
        def run_search(i: int, q: str) -> tuple[int, list]:
            s = searches[i]
            try:
                return i, search(q, config.get("discover.results"), s["duration"], s["date"], s["sort"])
            except (youtube.Unavailable, youtube.RateLimited):
                return i, []

        n_variants = config.get("discover.phrasings")

        def variants(i: int) -> tuple[int, list]:
            try:
                ask = f"Topic: {searches[i]['topic']}\nWrite {n_variants} queries."
                qs = llm.call(prompt("discover_queries"), ask, schema=VARIANT_SCHEMA, model=config.model("phrasing"),
                              think=False)["queries"][:n_variants]
            except llm.LLMError:
                qs = []
            return i, [q for q in qs if q.strip().lower() != searches[i]["topic"].lower()]

        def run_channel(i: int) -> tuple[int, list]:
            items, counts = channels.listing({"name": searches[i]["name"], "url": searches[i]["url"]})
            queries[i] = [f"{n} {what}" for what, n in counts.items() if n]  # shown as what was searched
            return i, items

        words = [i for i, s in enumerate(searches) if not s["channel"]]
        pending = {ex.submit(run_search, i, searches[i]["topic"]) for i in words}
        pending |= {ex.submit(variants, i) for i in words if n_variants}
        pending |= {ex.submit(run_channel, i) for i, s in enumerate(searches) if s["channel"]}
        while pending:
            for f in as_completed(pending):
                pending.discard(f)
                i, val = f.result()
                if val and isinstance(val[0], str):  # variant queries arrived: search them too
                    queries[i] += val
                    pending |= {ex.submit(run_search, i, q) for q in val}
                    say(f"Searching {sum(len(q) for q in queries.values())} queries")
                else:
                    results.append((i, val))
                break
    return queries, results


def _merge(results: list[tuple[int, list[dict]]]) -> dict[str, dict]:
    """One entry per video, remembering every query and search that found it and its best rank."""
    merged: dict[str, dict] = {}
    for i, res in results:
        for c in res:
            m = merged.setdefault(c["id"], {**c, "found_by": [], "searches": set()})
            m["rank"] = min(m["rank"], c["rank"])
            if c["query"] not in m["found_by"]:
                m["found_by"].append(c["query"])
            m["searches"].add(i)
    return merged


def _keep_eligible(merged: dict[str, dict], searches: list[dict], offered: set[str], exclude: set[str],
                   dropped: dict[str, int]) -> list[dict]:
    """Cheap local filters; every rejection is counted by reason in `dropped`."""
    in_library = {p.parent.name for p in DATA.glob("*/kb.json")}
    min_len = config.get("discover.min_length")
    if any(s["duration"] == "short" and not s["channel"] for s in searches):
        min_len = min(min_len, 60)
    shorts = config.get("discover.shorts")

    def reason(c: dict) -> str | None:
        if c.get("tab"):  # from a channel search: Shorts are welcome, its own filters apply here
            if c["tab"] != "shorts" and not c["duration"]:
                return "live or upcoming"
            if not any(_fits(c, searches[i]) for i in c["searches"]):
                return "outside the length or date filter"
        elif "/shorts/" in c["url"]:
            if not shorts:
                return "Shorts"
        elif not c["duration"]:
            return "live or upcoming"
        elif c["duration"] < min_len and "/shorts/" not in c["url"]:
            return "too short"
        if c["id"] in in_library:
            return "already in your library"
        if c["id"] in exclude:
            return "already queued"
        if c["id"] in offered:
            return "offered before for this topic"
        return None

    out = []
    for c in merged.values():
        if why := reason(c):
            dropped[why] = dropped.get(why, 0) + 1
        else:
            out.append(c)
    return out


def _fits(c: dict, s: dict) -> bool:
    """A channel item against its search's length and date filters (lengths and dates may still be unknown)."""
    if not s["channel"]:
        return True
    lo, hi = LENGTHS.get(s["duration"], (0, 10**9))
    length = c["duration"] if c["duration"] is not None else (59 if c.get("tab") == "shorts" else None)
    if length is not None and not lo <= length < hi:
        return False
    when = c.get("timestamp")
    return not (s["date"] in DAYS and when and time.time() - when > DAYS[s["date"]] * 86400)


def _fits_after_details(c: dict, searches: list[dict], dropped: dict[str, int]) -> bool:
    """Shorts get their length and date only with their details; dates in listings are approximate."""
    if not c.get("tab"):
        return True
    when = c.get("upload_date")
    exact = {**c, "timestamp": time.mktime(time.strptime(when, "%Y%m%d")) if when else c.get("timestamp")}
    if any(_fits(exact, searches[i]) for i in c["searches"]):
        return True
    dropped["outside the length or date filter"] = dropped.get("outside the length or date filter", 0) + 1
    return False


def _age(ts: float | None) -> str:
    if not ts:
        return "date unknown"
    days = (time.time() - ts) / 86400
    return f"{round(days)} days ago" if days < 60 else f"{round(days / 30)} months ago" if days < 730 else \
        f"{round(days / 365)} years ago"


def _prescreen(cands: list[dict], i: int, s: dict, budget: int, say: Callable[[str], None], model: str) -> int:
    """Rank a channel's whole catalogue so the best `budget` items get read in detail: the fast model's choice
    (or the most viewed, when sorting by popularity or if the model fails), plus the best few of every
    source it didn't pick from. Writes the order into `rank`; returns how many extra slots the sources need."""
    mine = [c for c in cands if i in c["searches"] and c.get("tab")]  # found first by keywords: keeps that rank
    if not mine:
        return 0
    by_views = sorted(range(len(mine)), key=lambda n: -(mine[n]["views"] or 0))
    order: list[int] = []
    if s["sort"] != "popularity" and len(mine) > budget:
        say(f"Choosing from {len(mine)} items on {s['topic']}")
        lines = [f"[{n}] {c['title']} | {c['query']} | "
                 + ("Short" if c["tab"] == "shorts" else f"{round((c['duration'] or 0) / 60)} min")
                 + f" | {c['views'] or 0:,} views | {_age(c.get('timestamp'))}" for n, c in enumerate(mine)]
        try:
            ask = f"Channel: {s['topic']} ({s['name']})\nReturn the best {budget}.\n\n" + "\n".join(lines)
            best = llm.call(prompt("discover_channel"), ask, schema=PRESCREEN_SCHEMA, model=model, think=False)["best"]
            order = list(dict.fromkeys(n for n in best if isinstance(n, int) and 0 <= n < len(mine)))
        except llm.LLMError:
            order = []
    order = (order or by_views)[:budget]
    extra = 0
    for tab in dict.fromkeys(c["tab"] for c in mine):  # every source gets a look, e.g. Shorts and Live
        have = sum(mine[n]["tab"] == tab for n in order)
        want = max(0, config.get("channel.per_source") - have)
        more = [n for n in by_views if mine[n]["tab"] == tab and n not in order][:want]
        order += more
        extra += len(more)
    for n, c in enumerate(mine):
        c["rank"] = order.index(n) if n in order else 10_000 + c["rank"]
    return extra


def _per_search(n_searches: int, picks: int) -> int:
    per_pick = config.get("discover.read_per_pick")
    return max(config.get("discover.read_per_search"), -(-per_pick * picks // n_searches))


def _shortlist(cands: list[dict], n_searches: int, per_search: int, extra: dict[int, int] | None = None) -> list[dict]:
    """The best few of EACH search go forward, so one broad search can't crowd out the others."""
    pool: list[dict] = []
    seen: set[str] = set()
    for i in range(n_searches):
        for c in sorted((c for c in cands if i in c["searches"]), key=lambda c: (c["rank"], -len(c["found_by"]))):
            if len([p for p in pool if i in p["searches"]]) >= per_search + (extra or {}).get(i, 0):
                break
            if c["id"] not in seen:
                seen.add(c["id"])
                pool.append(c)
    return pool[:config.get("discover.read_max")]


def _enrich_all(pool: list[dict], dropped: dict[str, int]) -> list[dict]:
    with ThreadPoolExecutor(12) as ex:
        pool = list(ex.map(_enrich, pool))
    for c in pool:
        if c.get("dropped"):
            dropped[c["dropped"]] = dropped.get(c["dropped"], 0) + 1
    return [c for c in pool if not c.get("dropped")]


def _score(pool: list[dict], searches: list[dict], model: str) -> dict[int, dict]:
    """One AI call scores every candidate; returns {candidate number: {score, reason}}."""
    context = config.get("discover.context_chars")
    lines = []
    for n, c in enumerate(pool):
        lines.append(
            f"[{n}] {c['title']}\n    found by search {', '.join(str(i + 1) for i in sorted(c['searches']))} | "
            f"channel: {c['channel']} | {round((c['duration'] or 0) / 60)} min | {c['views'] or 0:,} views | "
            f"uploaded {c.get('upload_date') or '?'} | language: {c.get('language') or '?'}"
            + (f" | from the channel's {c['query']}" if c.get("tab") else "") + "\n"
            + (f"    chapters: {'; '.join(c['chapters'])}\n" if c.get("chapters") else "")
            + (f"    tags: {', '.join(c['tags'])}\n" if c.get("tags") else "")
            + f"    description: {' '.join((c.get('description') or c['snippet']).split())[:context]}")
    head = "Searches:\n" + "\n".join(
        f"{i + 1}. " + (f"CHANNEL {s['topic']} ({s['name']}): everything it published" if s["channel"] else s["topic"])
        for i, s in enumerate(searches))
    # No extended thinking: on 40 real candidates, 19 of 20 picks were the same with it, scores within ~3 points.
    res = llm.call(prompt("discover_rank"), head + "\n\nCandidates:\n\n" + "\n\n".join(lines), schema=RANK_SCHEMA,
                   model=model, think=False)
    scored: dict[int, dict] = {}
    for r in res["scores"]:
        if 0 <= r["n"] < len(pool) and r["n"] not in scored:
            scored[r["n"]] = r
    return scored


def _choose(pool: list[dict], scored: dict[int, dict], searches: list[dict], picks: int) -> list[int]:
    """The best of every search first (if good enough), then the best of the rest; never below the lowest score
    picked, and at most so many per channel among keyword results (a channel search's picks are exempt)."""
    by = config.get("discover.order")
    rank = {"score": lambda n: (-scored[n]["score"],), "views": lambda n: (-(pool[n].get("views") or 0),),
            "recent": lambda n: (-int(d) if (d := str(pool[n].get("upload_date") or "")).isdigit() else 0,)}[by]
    order = sorted(scored, key=lambda n: (*rank(n), -scored[n]["score"]))
    chosen: list[int] = []
    per_channel: dict[str, int] = {}
    floor, cap = config.get("discover.min_score"), config.get("discover.per_channel")

    def take(n: int) -> bool:
        ch = pool[n]["channel"]
        exempt = any(searches[i]["channel"] for i in pool[n]["searches"])
        full = len(chosen) >= picks or (not exempt and per_channel.get(ch, 0) >= cap)
        if n in chosen or scored[n]["score"] < floor or full:
            return False
        chosen.append(n)
        if not exempt:
            per_channel[ch] = per_channel.get(ch, 0) + 1
        return True

    if config.get("discover.cover_searches"):
        for i in range(len(searches)):
            next((n for n in order if i in pool[n]["searches"] and take(n)), None)
    for n in order:
        take(n)
    return chosen


def _remember(searches: list[dict], pool: list[dict], chosen: list[int]) -> None:
    """Topic memory: what each search has offered, so "only new" can skip it next time."""
    with locked(TOPICS):  # parallel discovers must not lose each other's topic memory
        topics_ = load_topics()
        for i, s in enumerate(searches):
            t = topics_.setdefault(s["topic"].lower(), {"topic": s["topic"], "offered": [], "runs": []})
            t.update(topic=s["topic"], last_run=time.time(), options={k: s[k] for k in ("duration", "date", "sort")})
            t["offered"] = sorted(set(t["offered"]) | {c["id"] for c in pool if i in c["searches"]})
            t["runs"] = (t["runs"] + [{"at": time.time(), "picked": [pool[n]["id"] for n in chosen
                                                                    if i in pool[n]["searches"]]}])[-20:]
        DATA.mkdir(exist_ok=True)
        write_json(TOPICS, topics_)
