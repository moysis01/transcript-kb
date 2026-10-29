"""The concept-note format (kb/concepts/*.md), read tolerantly and rewritten conservatively.

A note is YAML-style front matter (name, kind, aliases, plus anything the user adds, such as
Obsidian tags) followed by a Markdown body. The body holds one generated section per source
video, fenced by `<!-- src:VIDEO -->` ... `<!-- /src:VIDEO -->`. Only those sections and the three
generated properties are ever rewritten; everything else a person writes in the note is kept.
"""
import json
import re
from dataclasses import dataclass, field

_BLOCK = r"<!-- src:{vid} -->\n.*?<!-- /src:{vid} -->\n"
_ANY_BLOCK = re.compile(r"<!-- src:(\S+) -->\n.*?<!-- /src:\1 -->\n", re.S)


@dataclass
class Note:
    header: list[tuple[str, list[str]]] = field(default_factory=list)  # (key, its raw lines), in file order
    body: str = ""

    # ------------------------------------------------------------ reading
    @classmethod
    def parse(cls, text: str) -> "Note":
        m = re.match(r"---\n(.*?)^---\n", text, re.S | re.M)
        if not m:  # no front matter at all (e.g. wiped in an editor): it's all body
            return cls([], text)
        header: list[tuple[str, list[str]]] = []
        for line in m.group(1).splitlines(keepends=True):
            km = re.match(r"([A-Za-z_][\w-]*):", line)
            if km or not header:
                header.append((km.group(1) if km else "", [line]))
            else:  # continuation of a multi-line property (e.g. a YAML list)
                header[-1][1].append(line)
        return cls(header, text[m.end():])

    def raw(self, key: str) -> str | None:
        for k, lines in self.header:
            if k == key:
                return lines[0].split(":", 1)[1].strip()
        return None

    def name(self, slug: str) -> str:
        try:
            value = json.loads(self.raw("name") or "")
            if isinstance(value, str) and value.strip():
                return value
        except ValueError:
            if (r := self.raw("name")):  # an unquoted name typed by hand
                return r
        return slug.replace("-", " ").title()

    def kind(self) -> str:
        return self.raw("kind") or "concept"

    def aliases(self) -> list[str]:
        try:
            value = json.loads(self.raw("aliases") or "[]")
        except ValueError:
            return []
        return [a for a in value if isinstance(a, str)] if isinstance(value, list) else []

    def sources(self) -> list[str]:
        return [m.group(1) for m in _ANY_BLOCK.finditer(self.body)]

    def has_user_text(self) -> bool:
        """Anything besides the generated sections and the title heading."""
        rest = _ANY_BLOCK.sub("", self.body)
        rest = re.sub(r"^# .*$", "", rest, count=1, flags=re.M)
        return bool(rest.strip())

    # ------------------------------------------------------------ writing
    def set(self, key: str, value: str) -> None:
        line = f"{key}: {value}\n"
        for i, (k, _) in enumerate(self.header):
            if k == key:
                self.header[i] = (key, [line])
                return
        self.header.append((key, [line]))

    def put_section(self, vid: str, section: str) -> None:
        """Replace this video's section in place, or add it after the others."""
        pattern = re.compile(_BLOCK.format(vid=re.escape(vid)), re.S)
        if pattern.search(self.body):
            self.body = pattern.sub(lambda _: section, self.body, count=1)
        else:
            self.body = self.body.rstrip("\n") + "\n\n" + section if self.body.strip() else self.body + section

    def drop_section(self, vid: str) -> bool:
        new = re.sub(_BLOCK.format(vid=re.escape(vid)) + r"\n?", "", self.body, flags=re.S)
        changed, self.body = new != self.body, new
        return changed

    def render(self) -> str:
        if not self.header:
            return self.body
        return "---\n" + "".join("".join(lines) for _, lines in self.header) + "---\n" + self.body


def new_note(name: str, kind: str) -> Note:
    from tkb.core.text import yq
    return Note([("name", [f"name: {yq(name)}\n"]), ("kind", [f"kind: {kind}\n"]), ("aliases", ["aliases: []\n"])],
                f"\n# {name}\n\n")
