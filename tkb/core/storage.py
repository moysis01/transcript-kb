"""Crash-safe file I/O for the JSON and Markdown state, plus a cross-process lock."""
import contextlib
import fcntl
import json
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_text(path: Path, text: str) -> None:
    """Atomic: write a sibling temp file, then rename over the target. Concurrent readers see the old file
    or the new one, never half of one, and a crash never leaves a truncated file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def write_json(path: Path, obj) -> None:
    write_text(path, json.dumps(obj, ensure_ascii=False, indent=2))


@contextlib.contextmanager
def locked(path: Path) -> Iterator[None]:
    """Exclusive lock for read-modify-write of a state file, across threads and processes."""
    lock = path.with_name(f".{path.name}.lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "w") as f:  # a fresh open file per caller, so threads in one process exclude each other too
        fcntl.flock(f, fcntl.LOCK_EX)
        yield

