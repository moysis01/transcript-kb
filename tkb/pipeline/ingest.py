"""Download audio + metadata for a YouTube video."""
from tkb.config import DATA
from tkb.core import youtube
from tkb.core.storage import read_json, write_json
from tkb.core.text import video_id_from

META_KEYS = ("id", "title", "channel", "uploader", "upload_date", "duration",
             "description", "chapters", "tags", "webpage_url", "language")


def ingest(url: str) -> dict:
    vid = video_id_from(url)
    out = DATA / vid
    out.mkdir(parents=True, exist_ok=True)
    opts = {
        "format": "bestaudio/best",
        "outtmpl": str(out / "audio.%(ext)s"),
        "noplaylist": True,
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "wav"}],
        # Whisper wants 16 kHz mono; this makes decoding faster and deterministic.
        "postprocessor_args": {"extractaudio": ["-ar", "16000", "-ac", "1"]},
    }
    url = f"https://www.youtube.com/watch?v={vid}"
    if not (out / "audio.wav").exists():
        info = youtube.extract(url, {**opts, "concurrent_fragment_downloads": 4}, download=True)
    elif (out / "meta.json").exists():  # resume: nothing to fetch
        return read_json(out / "meta.json")
    else:
        info = youtube.extract(url, {"skip_download": True})
    meta = {k: info.get(k) for k in META_KEYS}
    meta["id"] = vid
    write_json(out / "meta.json", meta)
    return meta
