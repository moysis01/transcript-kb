"""All YouTube access goes through here: one shared concurrency budget, and back-off on rate limits."""
import random
import re
import time

import yt_dlp

from tkb import config
from tkb.core import log as logs
from tkb.core.resources import slot

log = logs.get("youtube")

JS_RUNTIMES: dict[str, dict[str, dict]] = {"js_runtimes": {"deno": {}, "bun": {}, "node": {}}}  # YouTube needs JS


class Unavailable(Exception):
    """The video can't be fetched (removed, private, age-gated, members-only...)."""


class RateLimited(Exception):
    """YouTube kept answering 429 Too Many Requests after all retries."""


class _Silent:
    def debug(self, msg: str) -> None: pass
    def warning(self, msg: str) -> None: pass
    def error(self, msg: str) -> None: pass


def _is_rate_limit(e: Exception) -> bool:
    s = str(e)
    return "429" in s or "Too Many Requests" in s or "rate-limit" in s.lower()


TRANSIENT = ("HTTP Error 403", "HTTP Error 5", "timed out", "Connection reset", "Remote end closed",
             "IncompleteRead", "fragment", "Unable to download", "Got error")


def _is_transient(e: Exception) -> bool:
    """Mid-download failures YouTube throws under load; a retry usually works. A 404 never does."""
    return any(t in str(e) for t in TRANSIENT) and "HTTP Error 404" not in str(e)


# Failures that usually mean YouTube changed and yt-dlp needs updating, not that the video is gone.
OUTDATED = ("Unable to extract", "Signature extraction failed", "nsig extraction failed", "Requested format is not",
            "Failed to extract any player response", "Precondition check failed")
UPDATE_HINT = " (YouTube may have changed: try `uv run tkb update`)"


def _message(e: Exception) -> str:
    """YouTube's own reason, without yt-dlp's 'ERROR: [youtube] <id>:' prefix."""
    msg = re.sub(r"^ERROR:\s*(\[[^\]]+\]\s*[\w-]+:\s*)?", "", str(e).strip())[:240]
    return msg + UPDATE_HINT if any(o in msg for o in OUTDATED) else msg


def extract(url: str, opts: dict | None = None, download: bool = False) -> dict:
    """yt-dlp extract_info with a YouTube slot, exponential back-off with jitter on 429s."""
    # The operating system's trusted certificates, not yt-dlp's own bundle.
    o = {**JS_RUNTIMES, "compat_opts": ["no-certifi"], "quiet": True, "no_warnings": True, "noprogress": True,
         "logger": _Silent(), **(opts or {})}
    retries, longest = config.get("youtube.retries"), config.get("youtube.max_wait")
    for attempt in range(retries + 1):
        try:
            with slot("youtube"), yt_dlp.YoutubeDL(o) as y:
                return y.extract_info(url, download=download)
        except yt_dlp.utils.DownloadError as e:
            if (_is_rate_limit(e) or _is_transient(e)) and attempt < retries:
                wait = min(longest, 2 ** attempt * 3) * (0.7 + random.random() * 0.6)
                log.warning("YouTube %s; retrying in %.0fs (%s)", "rate limit" if _is_rate_limit(e) else "error",
                            wait, _message(e)[:120])
                time.sleep(wait)
                continue
            if _is_rate_limit(e):
                raise RateLimited(_message(e)) from e
            raise Unavailable(_message(e)) from e
    raise AssertionError("unreachable")
