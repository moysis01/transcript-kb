"""Strip noise from a transcript sentence by sentence, so every removal is explicit and auditable.

Each chunk is sent as numbered sentences. The AI returns, per id, either the lightly fixed
sentence or the category it was dropped for. The transcript is then reassembled in source
order, so timestamps stay exact and a sentence the AI forgets is kept rather than lost.
SponsorBlock-labelled segments are dropped before the AI sees them. Kinds of noise you keep
(Settings → Noise removal) are put back as spoken, without asking the AI again.
"""
import bisect
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from tkb import config
from tkb.config import DATA, KB
from tkb.core import llm
from tkb.core.storage import read_json, write_json, write_text
from tkb.core.text import fmt_ts, words
from tkb.pipeline import diarize, sponsorblock
from tkb.pipeline.transcribe import render_markdown
from tkb.prompts import prompt

CACHE_VERSION = 2
# The settings a video is cleaned with are pinned in clean/settings.pin (not *.json, so the chunk globs never see
# it): reruns use the pin, so changing Settings never re-cleans a video or changes its text; Redo deletes the
# cache and the pin together, and takes the current Settings. Videos cleaned before pins existed get LEGACY,
# today's values frozen (and, if they predate the sentence cap, no cap: see _pinned).
PIN = "settings.pin"
LEGACY = {"chunk": [600, 2200], "sentence": [40, 20, 25, 15], "sponsorblock": ["sponsor", "selfpromo", "interaction"],
          "overlap": 0.5, "remove": ["sponsor", "self_promotion", "engagement", "intro_outro", "filler", "off_topic"],
          "ad_phrases": list(config.AD_PHRASES), "chapters": True, "tail": 1 / 3, "rewrite": 2.0, "numbers": True,
          "context": 1200}  # (a pin from before a value existed takes it from here: the value it was cleaned with)



SCHEMA = {
    "type": "object",
    "properties": {"sentences": {"type": "array", "items": {
        "type": "object",
        "properties": {"id": {"type": "integer"}, "text": {"type": "string"},
                       "category": {"type": "string", "enum": [k for k, _ in config.NOISE]}},
        "required": ["id"]}}},
    "required": ["sentences"]}



def settings_now() -> dict:
    """The cleaning settings as they are in Settings now (what a new video, or a Redo, is cleaned with)."""
    g = config.get
    return {"chunk": [g("chunk.min_words"), g("chunk.max_words")],
            "sentence": [g("sentence.max_words"), g("sentence.max_secs"), g("sentence.target_words"),
                         g("sentence.target_secs")],
            "sponsorblock": list(g("sponsorblock.categories")), "overlap": g("sponsorblock.overlap"),
            "remove": list(g("noise.remove")), "ad_phrases": list(g("noise.ad_phrases")),
            "chapters": g("chunk.chapters"), "tail": g("chunk.tail"), "rewrite": g("clean.rewrite_ratio"),
            "numbers": g("clean.check_numbers"), "context": g("clean.context_chars")}


def _pinned(d: Path, doc: dict) -> tuple[dict, list[dict]]:
    """This video's cleaning settings, pinned on first use, and its sentences split with them."""
    f = d / "clean" / PIN
    if f.exists():
        pin = {**LEGACY, **read_json(f)}
        return pin, split_sentences(doc["segments"], doc["paragraphs"], pin["sentence"])
    cached = any((d / "clean").glob("[0-9]*.json"))
    pin = dict(LEGACY) if cached else settings_now()
    sents = split_sentences(doc["segments"], doc["paragraphs"], pin["sentence"])
    if cached and (d / "clean.json").exists():  # cleaned before the sentence cap: keep its uncapped split
        old = [s["src"] for s in read_json(d / "clean.json")["sentences"]]
        uncapped = split_sentences(doc["segments"], doc["paragraphs"], None)
        if [s["src"] for s in sents] != old and [s["src"] for s in uncapped] == old:
            pin, sents = {**pin, "sentence": None}, uncapped
    write_json(f, pin)
    return pin, sents


def split_sentences(segs: list[dict], paras: list[dict], cap: list[int] | None | bool = True) -> list[dict]:
    """Sentences with start/end (from word timestamps when present, else interpolated inside segments).
    `cap`: [max words, max seconds, target words, target seconds] for splitting over-long sentences; True means
    as in Settings now, None (or False) leaves them whole, as videos cleaned before the cap were split."""
    limits = settings_now()["sentence"] if cap is True else cap or None
    max_words, max_secs, target_words, target_secs = limits or (0, 0, 0, 0)
    text, offs, starts, ends = "", [], [], []
    units = ([w for s in segs for w in s["words"]] if segs and all(s.get("words") for s in segs)
             else [{"start": s["start"], "end": s["end"], "word": s["text"]} for s in segs])
    for u in units:
        piece = u["word"].strip()
        if not piece:
            continue
        if text:
            text += " "
        offs.append(len(text))
        starts.append(u["start"])
        ends.append(u["end"])
        text += piece

    def time_at(off: int) -> float:
        i = max(bisect.bisect_right(offs, off) - 1, 0)
        seg_len = (offs[i + 1] - 1 if i + 1 < len(offs) else len(text)) - offs[i]
        frac = min(max((off - offs[i]) / max(seg_len, 1), 0.0), 1.0)
        return starts[i] + (ends[i] - starts[i]) * frac

    def unit_end(j: int) -> int:
        return offs[j + 1] - 1 if j + 1 < len(offs) else len(text)

    def capped(a: int, b: int) -> list[tuple[int, int]]:
        """Split an over-long span at word/segment boundaries (preferring a comma), never mid-word."""
        if not limits or (len(text[a:b].split()) <= max_words and time_at(b) - time_at(a) <= max_secs):
            return [(a, b)]
        pieces, cur, n = [], a, 0
        for j in range(max(bisect.bisect_right(offs, a) - 1, 0), len(offs)):
            if offs[j] >= b:
                break
            e = min(unit_end(j), b)
            piece = text[max(offs[j], a):e]
            n += len(piece.split())
            soft = n >= 12 and piece.rstrip().endswith((",", ";", ":"))
            if e < b and (soft or n >= target_words or time_at(e) - time_at(cur) >= target_secs):
                pieces.append((cur, e))
                cur, n = e + 1, 0
        if cur < b:
            pieces.append((cur, b))
        return pieces

    pstarts = [p["start"] for p in paras]
    out: list[dict] = []
    # A sentence ends at . ? ! (not an ellipsis, which usually means the thought continues), CJK and Devanagari marks.
    for m in re.finditer(r"\S.*?(?:(?:[?!。？！।॥]+|(?<!\.)\.(?!\.))[\"'”’)\]]*(?=\s|$)|$)", text, re.S):
        for a, b in capped(m.start(), m.end()):
            src = text[a:b].strip()
            if not src:
                continue
            t0, t1 = time_at(a), time_at(b)
            para = max(bisect.bisect_right(pstarts, t0 + 0.01) - 1, 0)
            out.append({"id": len(out), "start": round(t0, 2), "end": round(t1, 2), "para": para, "src": src})
    return out


def make_chunks(paras: list[dict], chapters: list[dict], sizes: list[int] | None = None, at_chapters: bool = True,
                tail: float = 1 / 3) -> list[list[int]]:
    """Paragraph indices per chunk: greedy fill up to the most words, preferring chapter boundaries once a chunk
    has the fewest (if `at_chapters`); a last chunk under `tail` of the fewest joins the one before.
    `sizes` = [fewest, most] words (default: Settings now)."""
    min_words, max_words = sizes or settings_now()["chunk"]
    if not at_chapters:
        chapters = []
    starts = sorted(c["start_time"] for c in chapters or [])
    chunks: list[list[int]] = []
    cur: list[int] = []
    n, prev = 0, -1.0
    for i, p in enumerate(paras):
        w = words(p["text"])
        at_chapter = any(prev < s <= p["start"] + 1 for s in starts)
        prev = p["start"]
        if cur and (n + w > max_words or (at_chapter and n >= min_words)):
            chunks.append(cur)
            cur, n = [], 0
        cur.append(i)
        n += w
    if cur:
        if chunks and n < min_words * tail:
            chunks[-1] += cur
        else:
            chunks.append(cur)
    return chunks


def _sb_category(s: dict, segs: list[dict], share: float) -> str | None:
    """The SponsorBlock category of a segment covering at least `share` of the sentence's time, if any."""
    dur = max(s["end"] - s["start"], 0.01)
    for g in segs:
        overlap = min(s["end"], g["end"]) - max(s["start"], g["start"])
        if overlap / dur >= share:
            return g["category"]
    return None


def _numbers(text: str) -> set[str]:
    return set(re.findall(r"\d+(?:[.,]\d+)?", text))


def _clean_chunk(meta: dict, sents: list[dict], model: str, names: dict | None = None,
                 ads: re.Pattern | None = None, rules: dict | None = None) -> tuple[list[dict], list[str]]:
    """Fill in text/category/by on each sentence. Returns (sentences, warnings); `ads` flags likely ads left.
    `rules` (from the pin): rewrite (longest accepted rewrite, × the original), numbers (flag added numbers),
    context (characters of the video description shown)."""
    rules = {**LEGACY, **(rules or {})}
    warnings: list[str] = []
    todo = [s for s in sents if s.get("by") != "sponsorblock"]
    if todo:
        who = lambda s: f"{names[s['spk']]}: " if names and s.get("spk") in names else ""
        lines = "\n".join(f"[{s['id']}] ({fmt_ts(s['start'])}) {who(s)}{s['src']}" for s in todo)
        message = (f"Video title: {meta['title']}\nChannel: {meta.get('channel')}\n"
                  f"Description (for spelling context only):\n{(meta.get('description') or '')[:rules['context']]}\n\n"
                  f"--- SENTENCES ---\n{lines}")
        try:
            res = {r["id"]: r for r in llm.call(prompt("clean"), message, schema=SCHEMA, model=model)["sentences"]}
        except llm.LLMError as e:
            res = {}
            warnings.append(f"LLM failed, chunk kept raw: {e}")
        valid = {s["id"] for s in todo}
        if set(res) - valid:
            warnings.append(f"ignored {len(set(res) - valid)} unknown sentence ids")
        missing = 0
        for s in todo:
            r = res.get(s["id"])
            if r and r.get("category"):
                s.update(text=None, category=r["category"], by="llm")
            elif r and r.get("text", "").strip():
                t = r["text"].strip()
                if words(t) > rules["rewrite"] * words(s["src"]) + 5:  # rewrote instead of cleaning
                    s.update(text=s["src"], category=None, by="fallback")
                    warnings.append(f"[{fmt_ts(s['start'])}] rewrite rejected, kept raw sentence")
                    continue
                s.update(text=t, category=None, by="llm")
                if rules["numbers"] and _numbers(t) - _numbers(s["src"]):
                    warnings.append(f"[{fmt_ts(s['start'])}] numbers not in source: "
                                    f"{sorted(_numbers(t) - _numbers(s['src']))}")
            else:
                missing += 1
                s.update(text=s["src"], category=None, by="fallback")
        if missing:
            warnings.append(f"{missing} sentences not returned by the LLM; kept raw")
    for s in sents:
        if ads and s.get("text") and ads.search(s["text"]):
            warnings.append(f"[{fmt_ts(s['start'])}] possible ad left: {s['text'][:100]}")
    return sents, warnings


def paragraphs(sents: list[dict], names: dict | None = None) -> list[dict]:
    """Kept sentences regrouped by source paragraph and speaker; timestamp = first kept sentence."""
    out, cur = [], None
    for s in sents:
        if not s.get("text"):
            continue
        key = (s["para"], s.get("spk"))
        if key != cur:
            out.append({"start": s["start"], "text": s["text"],
                        "speaker": (names or {}).get(s.get("spk"))})
            cur = key
        else:
            out[-1]["text"] += " " + s["text"]
    return out


def render(vid: str) -> None:
    """Rewrite clean.json speakers, removed.md and cleaned.md from caches (no LLM). Safe after renames."""
    d = DATA / vid
    c, meta, doc = read_json(d / "clean.json"), read_json(d / "meta.json"), read_json(d / "transcript.json")
    sents = c["sentences"]
    multi = diarize.assign(sents, vid)
    names = diarize.names(vid) if multi else {}
    write_json(d / "clean.json", {**c, "sentences": sents})
    removed = [s for s in sents if not s.get("text")]
    counts: dict[str, int] = {}
    for s in removed:
        counts[s["category"]] = counts.get(s["category"], 0) + 1
    lines = [f"# Removed by cleaning: {len(removed)} sentences", "",
             f"SponsorBlock: {c['sponsorblock']}. " + ", ".join(f"{k}: {v}" for k, v in sorted(counts.items())), ""]
    lines += [f"- [{fmt_ts(s['start'])}] **{s['category']}** ({s['by']}) "
              + (f"{names[s['spk']]}: " if s.get("spk") in names else "") + s["src"] for s in removed]
    write_text((d / "removed.md"), "\n".join(lines) + "\n")
    md = render_markdown(meta, paragraphs(sents, names), doc["language"], doc["model"], cleaned=True,
                         translated=doc.get("task") == "translate")
    write_text((d / "cleaned.md"), md)
    if (KB / "transcripts" / f"{vid}.md").exists():
        write_text((KB / "transcripts" / f"{vid}.md"), md)


def _chunk_record(i: int, cs: list[dict], warns: list[str]) -> dict:
    kept = paragraphs(cs)
    return {"v": CACHE_VERSION, "index": i, "start": cs[0]["start"], "end": cs[-1]["end"],
            "noise": not kept, "warnings": warns, "sentences": cs,
            "text": "\n\n".join(f"[{fmt_ts(p['start'])}] {p['text']}" for p in kept)}


def _keep(r: dict, remove: list[str]) -> dict:
    """Sentences the AI labelled with a kind of noise you chose to keep go back in, as spoken (no extra calls)."""
    if not any(s.get("by") == "llm" and s.get("category") and s["category"] not in remove for s in r["sentences"]):
        return r
    cs = [{**s, "text": s["src"], "kept": True} if s.get("by") == "llm" and s.get("category")
          and s["category"] not in remove else s for s in r["sentences"]]
    return _chunk_record(r["index"], cs, r["warnings"])


def clean(vid: str, model: str | None = None, workers: int | None = None,
          use_sponsorblock: bool = True, progress=None, on_chunk=None) -> list[dict]:
    """`on_chunk(record)` is called as each chunk finishes, so the next stage can start on it right away.
    `workers` only sizes the thread pool: the shared LLM budget decides how many calls really run."""
    model = model or config.model("clean")
    d = DATA / vid
    cdir = d / "clean"
    cdir.mkdir(exist_ok=True)
    meta = read_json(d / "meta.json")
    doc = read_json(d / "transcript.json")
    paras = doc["paragraphs"]
    pin, sents = _pinned(d, doc)
    names = diarize.names(vid) if diarize.assign(sents, vid) else {}
    sb = sponsorblock.segments(vid, use_sponsorblock, pin["sponsorblock"])
    for s in sents:
        cat = _sb_category(s, sb["segments"], pin["overlap"])
        if cat:
            s.update(text=None, category=cat, by="sponsorblock")
    by_para: dict[int, list[dict]] = {}
    for s in sents:
        by_para.setdefault(s["para"], []).append(s)
    parts = make_chunks(paras, meta.get("chapters"), pin["chunk"], pin["chapters"], pin["tail"])
    chunks = [[s for i in c for s in by_para.get(i, [])] for c in parts]
    ads = re.compile("|".join(pin["ad_phrases"]), re.I) if pin["ad_phrases"] else None
    chunks = [c for c in chunks if c]  # a chunk whose paragraphs all fell inside earlier sentences has nothing to do
    done = [0]

    def run(i: int) -> dict:
        f = cdir / f"{i:03d}.json"
        if f.exists():  # resume support; stale formats are recomputed
            r = read_json(f)
            same = [(s["id"], s["src"]) for s in r["sentences"]] == [(s["id"], s["src"]) for s in chunks[i]]
            if r.get("v") == CACHE_VERSION and same:  # same sentences, same text (e.g. not since re-translated)
                r = _keep(r, pin["remove"])
                done[0] += 1
                progress and progress(done[0], len(chunks))
                on_chunk and on_chunk(r)
                return r
        cs, warns = _clean_chunk(meta, chunks[i], model, names, ads, pin)
        r = _chunk_record(i, cs, warns)
        write_json(f, r)  # the cache keeps the AI's labels as given; kinds kept per Settings are restored below
        r = _keep(r, pin["remove"])
        done[0] += 1
        progress and progress(done[0], len(chunks))
        on_chunk and on_chunk(r)
        return r

    with ThreadPoolExecutor(workers or config.get("clean.parallel")) as ex:
        results = list(ex.map(run, range(len(chunks))))
    for f in cdir.glob("[0-9]*.json"):  # chunks from an older, longer chunking
        if int(f.stem) >= len(chunks):
            f.unlink()

    all_sents = [s for r in results for s in r["sentences"]]
    write_json(d / "clean.json", {"sponsorblock": sb["status"], "sentences": all_sents,
                                  "warnings": [w for r in results for w in r["warnings"]]})
    render(vid)  # speakers are re-joined here from diarization.json, never taken from the cache
    return results
