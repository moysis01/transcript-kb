"""Logging: the web app keeps a rotating log in data/.logs/tkb.log; everything else logs to stderr.

Only events and errors are logged (job started/finished/failed, task errors, request failures,
YouTube back-offs), never transcript text or prompts. Job subprocesses log to stderr, which the
app keeps with the job, so their stdout stays a clean stream of progress events.
"""
import logging
import logging.handlers

from tkb.config import DATA

FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def get(name: str) -> logging.Logger:
    return logging.getLogger(f"tkb.{name}")


def to_file() -> None:
    """Called once by the web app."""
    root = logging.getLogger("tkb")
    if any(isinstance(h, logging.handlers.RotatingFileHandler) for h in root.handlers):
        return
    (DATA / ".logs").mkdir(parents=True, exist_ok=True)
    h = logging.handlers.RotatingFileHandler(DATA / ".logs" / "tkb.log", maxBytes=1_000_000, backupCount=3,
                                             encoding="utf-8")
    h.setFormatter(logging.Formatter(FORMAT))
    root.addHandler(h)
    root.setLevel(logging.INFO)


def to_stderr(level: int = logging.WARNING) -> None:
    """Called by the CLI: warnings and errors go to stderr (never stdout, which may carry events)."""
    root = logging.getLogger("tkb")
    if not root.handlers:
        h = logging.StreamHandler()
        h.setFormatter(logging.Formatter(FORMAT))
        root.addHandler(h)
        root.setLevel(level)
