"""Speech-to-text with Whisper -> raw + LLM-ready transcript.

Two engines run the same Whisper models with the same options: MLX on Apple Silicon (the Metal GPU), and
faster-whisper everywhere else (CPU, or an NVIDIA GPU with CUDA), e.g. in Docker. Settings → Transcription
picks one; "auto" takes MLX where it's installed.
"""
import gc
import os
import platform
import re
import sys

from tkb import config
from tkb.config import DATA
from tkb.core.resources import slot
from tkb.core.storage import read_json, write_json, write_text
from tkb.core.text import fmt_ts, yq

MODELS = {
    # Measured against human captions on 3 videos (6,684 words), on an Apple Silicon Mac (MLX):
    # turbo: 1.99% word error rate, 21x real time (2.17% / 12x with word timestamps). Can't translate.
    # large-v3: 2.47%, 9x real time (3.38% / 7x with word timestamps). Used for translation.
    "mlx": {"turbo": "mlx-community/whisper-large-v3-turbo", "large-v3": "mlx-community/whisper-large-v3-mlx"},
    "faster-whisper": {"turbo": "turbo", "large-v3": "large-v3"},  # its own names for the same models
}


def engine() -> str:
    """The transcription engine: as set in Settings, or for "auto" MLX where it's installed (Apple Silicon)."""
    chosen = config.get("asr.engine")
    if chosen != "auto":
        return chosen
    mac = sys.platform == "darwin" and platform.machine() == "arm64"
    try:
        import importlib.util
        has_mlx = importlib.util.find_spec("mlx_whisper") is not None
    except (ImportError, ValueError):
        has_mlx = False
    return "mlx" if mac and has_mlx else "faster-whisper"


def resolve_model(name: str, eng: str | None = None) -> str:
    """A short name ("turbo", "large-v3") as the engine's model, or a model of the engine's own as it is. A model
    made for the other engine is refused here, rather than failing deep inside a job."""
    eng = eng or engine()
    model = MODELS[eng].get(name, name)
    mlx_model = "mlx" in model.lower()
    if (eng == "mlx") != mlx_model and "/" in model:
        other = "an MLX" if mlx_model else "a faster-whisper (CTranslate2)"
        raise ValueError(f"{model} is {other} model, but transcription uses {eng}: pick turbo or large-v3, or a model "
                         f"for {eng} (Settings → Transcription)")
    return model


def default_model() -> str:
    """The speech model chosen in Settings (or TKB_ASR): turbo unless changed."""
    return resolve_model(config.get("asr.model"))
UNTITLED = re.compile(r"<Untitled Chapter \d+>")  # YouTube's placeholder name
# Paragraphs (Settings, read per transcription and stored in transcript.json, so existing videos never change):
# a silence of para.gap seconds starts one; para.chars is a soft cap broken only at a sentence end; para.hard_chars
# a hard cap, for text without punctuation.


def _prompt(meta: dict) -> str:
    """Bias the first decoding window toward the video's vocabulary.

    mlx-whisper drops the prompt after window 1 when condition_on_previous_text=False,
    so later name/jargon fixes are left to the clean stage (which sees the description)."""
    bits = [meta.get("title") or "", meta.get("channel") or ""]
    bits += (meta.get("tags") or [])[:12]
    return (". ".join(b for b in bits if b))[:400]


def _clean_segments(segs: list[dict]) -> list[dict]:
    """Drop likely hallucinations: silence-decoded text and runaway repeats."""
    out, last, run = [], None, 0
    for s in segs:
        text = s["text"].strip()
        if not text:
            continue
        if s.get("no_speech_prob", 0) > 0.6 and s.get("avg_logprob", 0) < -1.0:
            continue
        run = run + 1 if text == last else 0
        last = text
        if run >= 2:  # same line 3+ times in a row
            continue
        seg = {"start": s["start"], "end": s["end"], "text": text}
        if s.get("words"):
            seg["words"] = [{"start": round(w["start"], 2), "end": round(w["end"], 2), "word": w["word"]}
                            for w in s["words"]]
        out.append(seg)
    return out


def paragraphs(segs: list[dict]) -> list[dict]:
    gap, soft, hard = config.get("para.gap"), config.get("para.chars"), config.get("para.hard_chars")
    paras, cur = [], None
    for s in segs:
        if cur and (s["start"] - cur["end"] > gap or len(cur["text"]) > hard or (
                len(cur["text"]) > soft and re.search(r"[.?!。？！]\"?$", cur["text"]))):
            paras.append(cur)
            cur = None
        if cur is None:
            cur = {"start": s["start"], "end": s["end"], "text": s["text"]}
        else:
            cur["text"] += " " + s["text"]
            cur["end"] = s["end"]
    if cur:
        paras.append(cur)
    return paras


def render_markdown(meta: dict, paras: list[dict], language: str, model: str,
                    cleaned: bool = False, translated: bool = False) -> str:
    chapters = sorted((c for c in meta.get("chapters") or [] if not UNTITLED.match(c["title"])),
                      key=lambda c: c["start_time"])
    head = ["---", f"video_id: {meta['id']}", f"title: {yq(meta['title'])}",
            f"channel: {yq(meta.get('channel'))}", f"url: {meta['webpage_url']}",
            f"upload_date: {meta.get('upload_date')}", f"duration: {yq(fmt_ts(meta['duration']))}",
            *(["language: en", f"translated_from: {language}"] if translated else [f"language: {language}"]),
            f"asr_model: {model}",
            f"stage: {'cleaned' if cleaned else 'raw'}", "---", "", f"# {meta['title']}", ""]
    body, ci = [], 0
    for p in paras:
        title = None
        while ci < len(chapters) and chapters[ci]["start_time"] <= p["start"] + 1:
            title = chapters[ci]["title"]  # chapters with no remaining paragraphs are skipped
            ci += 1
        if title:
            body += [f"## {title}", ""]
        who = f"**{p['speaker']}:** " if p.get("speaker") else ""
        body += [f"[{fmt_ts(p['start'])}] {who}{p['text']}", ""]
    return "\n".join(head + body)


def _load(eng: str):  # -> the engine's module
    if eng == "mlx":
        import mlx_whisper  # heavy imports, only when needed
        return mlx_whisper
    import faster_whisper
    return faster_whisper


_fw_models: dict = {}  # faster-whisper: models loaded in this process


def _fw_model(fw, name: str):
    if name not in _fw_models:
        import ctranslate2
        device = config.get("asr.device")
        if device == "auto":
            device = "cuda" if ctranslate2.get_cuda_device_count() else "cpu"
        threads = config.get("asr.threads") or max(4, (os.cpu_count() or 8) // 2)
        _fw_models[name] = fw.WhisperModel(name, device=device, compute_type="int8" if device == "cpu" else "float16",
                                           cpu_threads=threads)
    return _fw_models[name]


def _detect_language(mod, audio, model: str) -> str:
    """The spoken language, from the first 30 seconds (the same check Whisper itself makes)."""
    if mod.__name__ == "faster_whisper":
        return _fw_model(mod, model).detect_language(mod.decode_audio(str(audio)))[0]
    import mlx.core as mx
    T = sys.modules["mlx_whisper.transcribe"]
    m = T.ModelHolder.get_model(model, mx.float16)
    mel = T.log_mel_spectrogram(str(audio), n_mels=m.dims.n_mels, padding=T.N_SAMPLES)
    _, probs = m.detect_language(T.pad_or_trim(mel, T.N_FRAMES, axis=-2).astype(mx.float16))
    return max(probs, key=probs.get)


OPTIONS = {  # the same decoding on both engines
    # Each window decodes independently, which stops repetition loops from propagating through a long video.
    "condition_on_previous_text": False,
    "temperature": (0.0, 0.2, 0.4, 0.6, 0.8, 1.0),
    "compression_ratio_threshold": 2.4,
    "no_speech_threshold": 0.6,
}


def _run_whisper(mod, audio, model: str, meta: dict, words: bool, task: str = "transcribe",
                 language: str | None = None) -> dict:
    """{"language", "segments": [{"start", "end", "text", "no_speech_prob", "avg_logprob", "words"?}]}"""
    if mod.__name__ == "faster_whisper":
        return _run_faster_whisper(mod, audio, model, meta, words, task, language)
    mlx_whisper = mod
    return mlx_whisper.transcribe(
        str(audio),
        path_or_hf_repo=model,
        initial_prompt=_prompt(meta),
        **OPTIONS,
        verbose=False,  # tqdm progress bar on stderr (the web app parses it)
        word_timestamps=words,
        task=task,
        language=language,
    )


def _run_faster_whisper(fw, audio, model: str, meta: dict, words: bool, task: str, language: str | None) -> dict:
    segments, info = _fw_model(fw, model).transcribe(
        str(audio), initial_prompt=_prompt(meta), **OPTIONS, vad_filter=False, word_timestamps=words, task=task,
        language=language)
    out, shown = [], -1
    for s in segments:  # decoded lazily, one window at a time
        seg = {"start": s.start, "end": s.end, "text": s.text, "no_speech_prob": s.no_speech_prob,
               "avg_logprob": s.avg_logprob}
        if words and s.words:
            seg["words"] = [{"start": w.start, "end": w.end, "word": w.word} for w in s.words]
        out.append(seg)
        pct = min(100, int(100 * s.end / max(info.duration, 1)))
        if pct != shown:  # the same "NN%|" the web app reads from mlx-whisper's progress bar
            print(f"\r{pct}%|", end="", file=sys.stderr, flush=True)
            shown = pct
    print(file=sys.stderr, flush=True)
    return {"language": info.language, "segments": out}


def _release(mod) -> None:
    """The engine keeps its model (~1.5-3 GB) for the life of the process; free it so a job that has
    moved on to its LLM stages doesn't hold memory another job's transcription needs."""
    if mod.__name__ == "faster_whisper":
        _fw_models.clear()
        gc.collect()
        return
    import mlx.core as mx
    holder = sys.modules["mlx_whisper.transcribe"].ModelHolder  # the package re-exports the function
    holder.model, holder.model_path = None, None
    gc.collect()
    mx.clear_cache()


def transcribe(vid: str, model: str | None = None, force: bool = False, words: bool = False,
               translate: bool | None = None) -> dict:
    """`words`: word-level timestamps, for exact sentence timing when several people speak. They cost a
    little accuracy, so solo videos go without.
    `translate`: a non-English video is translated to English by Whisper itself (always with large-v3:
    turbo was trained without translation). Word timestamps are skipped then, as they don't align with
    translated text. An existing untranslated transcript is redone when translation is asked for."""
    out = DATA / vid
    eng = engine()
    model = resolve_model(model or config.get("asr.model"), eng)
    translate = config.get("translate") if translate is None else translate
    if (out / "transcript.json").exists() and not force:
        doc = read_json(out / "transcript.json")
        if not (translate and doc.get("language") not in (None, "en") and doc.get("task") != "translate"):
            return doc
    mod = _load(eng)
    meta = read_json(out / "meta.json")
    task, language = "transcribe", None
    with slot("gpu"):
        try:
            if translate:
                language = _detect_language(mod, out / "audio.wav", MODELS[eng]["large-v3"])
                if language != "en":
                    task, model, words = "translate", MODELS[eng]["large-v3"], False
            res = _run_whisper(mod, out / "audio.wav", model, meta, words, task, language)
        finally:
            _release(mod)
    segs = _clean_segments(res["segments"])
    paras = paragraphs(segs)
    doc = {"language": res.get("language"), "task": task, "model": model, "segments": segs, "paragraphs": paras}
    write_json(out / "transcript.json", doc)
    write_text((out / "transcript.md"), render_markdown(meta, paras, doc["language"], model,
                                                        translated=task == "translate"))
    return doc
