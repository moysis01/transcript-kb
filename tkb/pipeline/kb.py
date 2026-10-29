"""Turn cleaned transcripts into a Markdown wiki: video notes + merged concept notes + index."""
import fcntl
import shutil
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from tkb import config
from tkb.config import DATA, KB
from tkb.core import llm
from tkb.core.notes import Note, new_note
from tkb.core.storage import read_json, write_json, write_text
from tkb.core.text import fmt_ts, parse_ts, slugify, yq, yt_link
from tkb.pipeline import diarize
from tkb.pipeline.clean import paragraphs
from tkb.prompts import prompt


def kinds() -> list[str]:
    """The kinds of concept (Settings → Notes); "other" is always there, as the fallback."""
    ks = list(config.get("notes.kinds"))
    return ks if "other" in ks else [*ks, "other"]


def extract_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "outline": {"type": "array", "items": {
                "type": "object",
                "properties": {"title": {"type": "string"}, "at": {"type": "string"}},
                "required": ["title", "at"]}},
            "concepts": {"type": "array", "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"}, "kind": {"type": "string", "enum": kinds()},
                    "summary": {"type": "string"},
                    "facts": {"type": "array", "items": {"type": "string"}},
                    "at": {"type": "string"}},
                "required": ["name", "kind", "summary", "facts", "at"]}}},
        "required": ["summary", "outline", "concepts"]}


SYNTH_SCHEMA = {
    "type": "object",
    "properties": {
        "tldr": {"type": "string"},
        "takeaways": {"type": "array", "items": {"type": "string"}},
        "tags": {"type": "array", "items": {"type": "string"}},
        "canonical": {"type": "array", "items": {
            "type": "object",
            "properties": {"raw": {"type": "string"}, "canonical": {"type": "string"}},
            "required": ["raw", "canonical"]}}},
    "required": ["tldr", "takeaways", "tags", "canonical"]}


def extract_chunk(vid: str, c: dict, model: str | None = None) -> dict:
    """Notes for one cleaned chunk (cached). Called as soon as each chunk is cleaned, so extraction
    overlaps cleaning instead of waiting for the whole video."""
    d = DATA / vid
    f = d / "notes" / f"{c['index']:03d}.json"
    if f.exists() and f.stat().st_mtime >= (d / "clean" / f"{c['index']:03d}.json").stat().st_mtime:
        return read_json(f)
    (d / "synth.json").unlink(missing_ok=True)  # notes changed -> video synthesis is stale
    if c["noise"]:
        r = {"summary": "", "outline": [], "concepts": []}
    else:
        names = diarize.names(vid) if diarize.assign(c["sentences"], vid) else {}  # joined at read time
        text = "\n\n".join(f"[{fmt_ts(p['start'])}] " + (f"{p['speaker']}: " if p["speaker"] else "") + p["text"]
                           for p in paragraphs(c["sentences"], names)) if names else c["text"]
        meta = read_json(d / "meta.json")
        r = llm.call(prompt("extract"), f"Video: {meta['title']}\n\n{text}", schema=extract_schema(),
                     model=model or config.model("notes"))
    r.update(index=c["index"], start=c["start"])
    f.parent.mkdir(exist_ok=True)
    write_json(f, r)
    return r


def _extract(vid: str, model: str, workers: int | None = None, progress=None) -> list[dict]:
    d = DATA / vid
    (d / "notes").mkdir(exist_ok=True)
    chunks = [read_json(f) for f in sorted((d / "clean").glob("*.json"))]
    for f in (d / "notes").glob("*.json"):  # notes for chunks that no longer exist
        if int(f.stem) >= len(chunks):
            f.unlink()
    done = [0]

    def run(c: dict) -> dict:
        r = extract_chunk(vid, c, model)
        done[0] += 1
        progress and progress(done[0], len(chunks))
        return r

    with ThreadPoolExecutor(workers or config.get("notes.parallel")) as ex:  # the AI budget caps real concurrency
        return list(ex.map(run, chunks))


def _read_note(f) -> Note:
    return Note.parse(f.read_text(encoding="utf-8"))


def _existing_concepts() -> dict[str, str]:
    return {f.stem: _read_note(f).name(f.stem) for f in (KB / "concepts").glob("*.md")}


def _safe_ts(ts: str, fallback: float) -> int:
    try:
        return parse_ts(ts)
    except ValueError:
        return int(fallback)


def _concept_block(vid: str, video_slug: str, title: str, c: dict) -> str:
    links = ", ".join(f"[{fmt_ts(t)}]({yt_link(vid, t)})" for t in sorted(set(c["times"])))
    facts = "\n".join(f"- {f}" for f in dict.fromkeys(c["facts"]))
    return (f"<!-- src:{vid} -->\n## From [[{video_slug}|{title}]]\n\n{c['summary']}\n\n"
            f"{facts}\n\nDiscussed at: {links}\n<!-- /src:{vid} -->\n")


def _free_slug(name: str) -> str:
    """Slug for `name`; suffixed if another concept already owns that filename."""
    base = slug = slugify(name)
    n = 1
    while True:
        f = KB / "concepts" / f"{slug}.md"
        if not f.exists():
            return slug
        if _read_note(f).name(slug).casefold() == name.casefold():
            return slug
        n += 1
        slug = f"{base}-{n}"


def _write_concept(slug: str, name: str, kind: str, aliases: set[str], vid: str, block: str) -> None:
    """Add or replace this video's section. Everything else in the note (the user's own text, extra
    properties such as Obsidian tags) is kept; a kind already set wins, since kinds drift between videos."""
    f = KB / "concepts" / f"{slug}.md"
    note = _read_note(f) if f.exists() else new_note(name, kind)
    aliases = (aliases | set(note.aliases())) - {name}
    note.set("name", yq(name))
    note.set("kind", note.raw("kind") or kind)
    note.set("aliases", yq(sorted(aliases)))
    note.put_section(vid, block)
    write_text(f, note.render())


def _drop_video(vid: str) -> None:
    """Remove this video's sections so a re-run can't leave stale ones behind."""
    for f in (KB / "concepts").glob("*.md"):
        note = _read_note(f)
        if not note.drop_section(vid):
            continue
        if note.sources() or note.has_user_text():  # never throw away what a person wrote
            write_text(f, note.render())
        else:
            f.unlink()


def build(vid: str, model: str | None = None, progress=None) -> str:
    """LLM work (extraction, synthesis) runs unlocked so several videos overlap; only the wiki writes
    are serialised with an flock, so concurrent jobs and CLI runs never interleave them."""
    KB.mkdir(parents=True, exist_ok=True)
    model = model or config.model("notes")
    notes = _extract(vid, model, progress=progress)
    _synthesize(vid, notes, model)
    with open(KB / ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _build(vid, model, None)


def _raw_concepts(notes: list[dict]) -> dict[str, dict]:
    raw: dict[str, dict] = {}  # raw name -> merged entry
    for n in notes:
        for c in n["concepts"]:
            e = raw.setdefault(c["name"], {"kind": c["kind"], "summary": c["summary"], "facts": [], "times": []})
            e["facts"] += c["facts"]
            e["times"].append(_safe_ts(c["at"], n["start"]))
    return raw


def _synthesize(vid: str, notes: list[dict], model: str) -> dict:
    """TL;DR, takeaways and concept de-duplication against the wiki (cached in synth.json)."""
    synth_f = DATA / vid / "synth.json"  # cached so re-runs are stable; dropped whenever notes change
    if synth_f.exists():
        return read_json(synth_f)
    meta, raw = read_json(DATA / vid / "meta.json"), _raw_concepts(notes)
    existing = _existing_concepts() if config.get("notes.link_existing") else {}
    synth = llm.call(prompt("synthesize"), (
        f"Video: {meta['title']} ({meta.get('channel')})\n\nExcerpt summaries:\n"
        + "\n".join(f"- [{fmt_ts(n['start'])}] {n['summary']}" for n in notes if n["summary"])
        + "\n\nRaw concept names:\n" + "\n".join(f"- {k}" for k in raw)
        + "\n\nExisting wiki concept names:\n" + ("\n".join(f"- {v}" for v in existing.values()) or "(none)")),
        schema=SYNTH_SCHEMA, model=model)
    write_json(synth_f, synth)
    return synth


def _build(vid: str, model: str, progress: Callable[[int, int], None] | None) -> str:
    d = DATA / vid
    meta = read_json(d / "meta.json")
    for sub in ("videos", "concepts", "transcripts"):
        (KB / sub).mkdir(parents=True, exist_ok=True)
    notes = _extract(vid, model, progress=progress)  # all cached by now: no LLM calls under the lock
    raw = _raw_concepts(notes)
    _drop_video(vid)
    synth = _synthesize(vid, notes, model)
    merged = _merge_names(raw, synth)
    speakers = _speakers(vid)
    video_slug = f"{slugify(meta['title'], 50)}-{vid}"
    slugs = {}
    for name, c in merged.items():
        slugs[name] = _free_slug(name)
        _write_concept(slugs[name], name, c["kind"], c["aliases"], vid,
                       _concept_block(vid, video_slug, meta["title"], c))
    outline = [{"t": _safe_ts(o["at"], n["start"]), "title": o["title"]} for n in notes for o in n["outline"]]
    date = _iso_date(meta.get("upload_date") or "")
    write_text(KB / "videos" / f"{video_slug}.md",
               _video_note(vid, meta, date, synth, speakers, outline, {n: slugs[n] for n in sorted(merged)}))
    shutil.copy(d / "cleaned.md", KB / "transcripts" / f"{vid}.md")
    write_json(d / "kb.json", {  # structured copy for the web app
        "slug": video_slug, "speakers": speakers, "tldr": synth["tldr"],
        "takeaways": synth["takeaways"], "tags": synth["tags"],
        "outline": outline,
        "concepts": [{"name": n, "slug": slugs[n], "kind": c["kind"], "summary": c["summary"],
                      "facts": list(dict.fromkeys(c["facts"])), "times": sorted(set(c["times"])),
                      "aliases": sorted(c["aliases"])} for n, c in sorted(merged.items())]})
    registry = KB / ".videos.json"
    reg = read_json(registry) if registry.exists() else {}
    reg[vid] = {"title": meta["title"], "slug": video_slug, "tldr": synth["tldr"], "date": date,
                "channel": meta.get("channel")}
    write_json(registry, reg)
    write_index(reg)
    return video_slug


def _merge_names(raw: dict[str, dict], synth: dict) -> dict[str, dict]:
    """Apply the synthesis's renames: synonyms collapse into one canonical concept, keeping old names as aliases."""
    rename = {m["raw"]: m["canonical"] for m in synth["canonical"]
              if m["raw"] in raw} if config.get("notes.merge_synonyms") else {}
    merged: dict[str, dict] = {}
    for name, e in raw.items():
        canon = rename.get(name, name)
        m = merged.setdefault(canon, {"kind": e["kind"], "summary": e["summary"], "facts": [],
                                      "times": [], "aliases": set()})
        if canon != name:
            m["aliases"].add(name)
        if e["summary"] != m["summary"] and e["summary"] not in m["summary"]:
            m["summary"] += " " + e["summary"]
        m["facts"] += e["facts"]
        m["times"] += e["times"]
    return merged


def _speakers(vid: str) -> list[dict]:
    f = DATA / vid / "speakers.json"
    sp = read_json(f) if f.exists() else {}
    shown = diarize.names(vid)
    return [{"id": k, "name": shown[k], "role": v.get("role", ""), "share": v.get("share", 0)}
            for k, v in sp.get("speakers", {}).items() if k in shown]


def _iso_date(d: str) -> str:
    return f"{d[:4]}-{d[4:6]}-{d[6:]}" if len(d) == 8 else d


def _video_note(vid: str, meta: dict, date: str, synth: dict, speakers: list[dict], outline: list[dict],
                slugs: dict[str, str]) -> str:
    """The video's Markdown note: summary, takeaways, speakers, timestamped outline, concept links."""
    lines = "\n".join(f"- [{fmt_ts(o['t'])}]({yt_link(vid, o['t'])}) {o['title']}" for o in outline)
    concept_links = "\n".join(f"- [[{slug}|{name}]]" for name, slug in slugs.items())
    title = meta["title"]
    return (f"---\nvideo_id: {vid}\ntitle: {yq(title)}\nchannel: {yq(meta.get('channel'))}\n"
            f"url: {meta['webpage_url']}\nuploaded: {date}\nduration: {yq(fmt_ts(meta['duration']))}\n"
            f"tags: {yq(synth['tags'])}\n"
            + (f"speakers: {yq([s['name'] for s in speakers])}\n" if speakers else "")
            + f"---\n\n# {title}\n\n"
            f"{synth['tldr']}\n\n## Key takeaways\n\n"
            + "\n".join(f"- {t}" for t in synth["takeaways"])
            + ("\n\n## Speakers\n\n" + "\n".join(f"- {s['name']}" + (f" ({s['role']})" if s["role"] else "")
                                                   + f", {round(100 * s['share'])}% of talk time" for s in speakers)
               if speakers else "")
            + f"\n\n## Outline\n\n{lines}\n\n## Concepts\n\n{concept_links}\n\n"
            f"## Source\n\n[Watch]({meta['webpage_url']}) · [[{vid}|Cleaned transcript]]\n")


def write_index(reg: dict) -> None:
    vids = "\n".join(f"- [[{v['slug']}|{v['title']}]] — {v['channel']}, {v['date']}\n  {v['tldr']}"
                     for v in sorted(reg.values(), key=lambda v: v["date"], reverse=True))
    concepts = []
    for f in sorted((KB / "concepts").glob("*.md")):
        note = _read_note(f)
        name, kind, n = note.name(f.stem), note.kind(), len(note.sources())
        concepts.append(f"- [[{f.stem}|{name}]] ({kind}; {n} video{'s' * (n != 1)})")
    write_text((KB / "index.md"), f"# Knowledge Base\n\n## Videos ({len(reg)})\n\n{vids}\n\n"
        f"## Concepts ({len(concepts)})\n\n" + "\n".join(concepts) + "\n")
