"""The instructions sent to the AI: one Markdown file per task, kept out of the code so they're easy to edit.
The Settings page can replace any of them; the file stays the default (Reset brings it back)."""
from pathlib import Path

from tkb import config


def default(name: str) -> str:
    """The prompt text from `<name>.md` (the file's final newline is not part of the prompt)."""
    return (Path(__file__).parent / f"{name}.md").read_text(encoding="utf-8").removesuffix("\n")


def prompt(name: str) -> str:
    """The prompt as it is now: the Settings page's version if there is one, else the file."""
    return config.get(f"prompt.{name}") or default(name)
