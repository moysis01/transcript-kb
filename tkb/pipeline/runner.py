"""Runs the pipeline for one video (stage by stage, cached, timed), or several in parallel processes."""
import argparse
import contextlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor

from tkb.config import DATA
from tkb.core.resources import blocked
from tkb.core.storage import read_json, write_json
from tkb.pipeline import clean as clean_mod
from tkb.pipeline import diarize, kb
from tkb.pipeline import ingest as ingest_mod
from tkb.pipeline import transcribe as tr
from tkb.pipeline.clean import render, split_sentences

STAGES = ["ingest", "speakers", "transcribe", "clean", "kb"]


Emit = Callable[..., None]


def run_video(vid: str, a: argparse.Namespace, emit: Emit) -> None:
    """Run the stages `a.stage` selects for one video, each cached, timed and reported through `emit`."""
    _VideoRun(vid, a, emit).run()


class _VideoRun:
    def __init__(self, vid: str, a: argparse.Namespace, emit: Emit):
        self.vid, self.a, self.emit = vid, a, emit
        self.dir = DATA / vid
        self.timings: dict[str, float] = {}
        self.waits: dict[str, float] = {}

    def wants(self, stage: str) -> bool:
        return self.a.stage in (stage, "all")

    def progress(self, stage: str) -> Callable[[int, int], None]:
        return lambda done, total: self.emit("progress", vid=self.vid, stage=stage, done=done, total=total)

    def info(self, msg: str) -> None:
        self.emit("info", vid=self.vid, msg=msg)

    @contextlib.contextmanager
    def stage(self, name: str) -> Iterator[None]:
        """Times a stage, and how much of that time the video only waited for capacity (a free slot)."""
        self.emit("stage", vid=self.vid, stage=name)
        t0, b0 = time.monotonic(), blocked.seconds()
        yield
        self.timings[name] = round(time.monotonic() - t0, 1)
        self.waits[name] = round(min(blocked.seconds() - b0, self.timings[name]), 1)
        self.emit("stage_done", vid=self.vid, stage=name, seconds=self.timings[name], waited=self.waits[name])

    def run(self) -> None:
        a = self.a
        if a.redo:  # keep audio + transcript, redo the LLM stages
            shutil.rmtree(self.dir / "clean", ignore_errors=True)
        if a.redo or a.redo_kb:
            shutil.rmtree(self.dir / "notes", ignore_errors=True)
            (self.dir / "synth.json").unlink(missing_ok=True)
        if self.wants("ingest"):
            self.ingest()
        if self.wants("speakers") and a.speakers:  # needs only audio, so it runs before transcription
            self.speakers()
        if self.wants("transcribe"):
            self.transcribe()
        with ThreadPoolExecutor(8) as prefetch:  # notes for each chunk start as soon as that chunk is cleaned
            early: list[Future] = []
            if self.wants("clean"):
                self.clean((lambda r: early.append(prefetch.submit(kb.extract_chunk, self.vid, r, a.llm_model)))
                           if self.wants("kb") else None)
            if self.wants("kb"):
                self.knowledge_base(early)
        self.save_timings()
        self.emit("done", vid=self.vid, timings=self.timings, waited=self.waits)

    def ingest(self) -> None:
        with self.stage("ingest"):
            m = ingest_mod.ingest(self.vid)
        self.emit("meta", vid=self.vid, title=m["title"], duration=m["duration"])

    def speakers(self) -> None:
        a, dz_file = self.a, self.dir / "diarization.json"
        with self.stage("speakers"):
            fresh = bool(a.num_speakers or a.redo or not dz_file.exists())  # cached unless asked to re-detect
            dz = diarize.diarize(self.vid, a.num_speakers, progress=self.progress("speakers")) if fresh \
                else read_json(dz_file)
        self.info(f"{len(dz['share'])} speaker(s)" + (" detected" if fresh else " (cached)"))
        if (self.dir / "transcript.json").exists() and (fresh or not (self.dir / "speakers.json").exists()):
            self.name_speakers()
            if fresh and (self.dir / "kb.json").exists() and a.stage == "speakers":
                kb.build(self.vid, a.llm_model)  # cached notes: refreshes the note's Speakers section, no LLM call

    def name_speakers(self) -> None:
        doc = read_json(self.dir / "transcript.json")
        sents = split_sentences(doc["segments"], doc["paragraphs"])
        diarize.assign(sents, self.vid)
        diarize.name_speakers(self.vid, sents, self.a.llm_model)
        names = diarize.names(self.vid)
        if names:
            self.info("speakers: " + ", ".join(names.values()))
        if (self.dir / "clean.json").exists():
            render(self.vid)  # an existing transcript picks up the speakers without new LLM calls

    def transcribe(self) -> None:
        a, dz_file = self.a, self.dir / "diarization.json"
        multi = a.speakers and dz_file.exists() and read_json(dz_file)["multi"]
        with self.stage("transcribe"):
            doc = tr.transcribe(self.vid, a.asr_model, a.force_asr, words=multi, translate=a.translate)
        if doc.get("task") == "translate":
            self.info(f"translated from {doc['language']} to English")
        self.info(f"{len(doc['paragraphs'])} paragraphs, language={doc['language']}")
        if multi and not (self.dir / "speakers.json").exists():
            self.name_speakers()

    def clean(self, on_chunk: Callable[[dict], None] | None) -> None:
        with self.stage("clean"):
            res = clean_mod.clean(self.vid, self.a.llm_model, use_sponsorblock=self.a.sponsorblock,
                                  progress=self.progress("clean"), on_chunk=on_chunk)
        sents = [s for r in res for s in r["sentences"]]
        removed = [s for s in sents if not s.get("text")]
        for r in res:
            for w in r["warnings"]:
                self.emit("warning", vid=self.vid, msg=w)
        self.info(f"{len(res)} chunks, {len(removed)}/{len(sents)} sentences removed "
                  f"(audit: data/{self.vid}/removed.md)")

    def knowledge_base(self, early: list[Future]) -> None:
        with self.stage("kb"):
            for f in early:  # surface errors from the early extraction; kb.build redoes any that failed
                try:
                    f.result()
                except Exception as e:  # noqa: BLE001 - reported, and redone by kb.build below
                    self.emit("warning", vid=self.vid, msg=f"early extraction retried: {e}")
            slug = kb.build(self.vid, self.a.llm_model, progress=self.progress("kb"))
        self.info(f"kb/videos/{slug}.md")

    def save_timings(self) -> None:
        if not self.timings or not self.dir.exists():
            return
        f = self.dir / "timings.json"
        prev = read_json(f) if f.exists() else {}
        # stage -> seconds; "waited" holds how much of each stage was spent waiting for capacity
        write_json(f, {**prev, **self.timings, "waited": {**prev.get("waited", {}), **self.waits}})


def exit_with_parent() -> None:
    """A job started by the web app must not outlive it (the restarted app resumes the job)."""
    ppid = os.getppid()

    def watch() -> None:
        while True:
            time.sleep(2)
            if os.getppid() != ppid:
                os._exit(1)
    threading.Thread(target=watch, daemon=True).start()


def child_args(a) -> list[str]:
    """The same choices for a child process; a model left unset is looked up in Settings by the child."""
    args = ["--stage", a.stage]
    for flag, value in (("--asr-model", a.asr_model), ("--llm-model", a.llm_model)):
        if value:
            args += [flag, value]
    args += ["--sponsorblock" if a.sponsorblock else "--no-sponsorblock", "--translate" if a.translate else
             "--no-translate", "--speakers" if a.speakers else "--no-speakers"]
    for flag, on in (("--force-asr", a.force_asr), ("--redo", a.redo), ("--redo-kb", a.redo_kb)):
        if on:
            args.append(flag)
    return args + (["--num-speakers", str(a.num_speakers)] if a.num_speakers else [])


def run_many(vids: list[str], a, emit) -> int:
    """Several videos at once, each in its own process (like the web app); stages share resource slots."""
    out = threading.Lock()
    print(f"Processing {len(vids)} videos, up to {a.jobs} at a time")

    def one(vid: str) -> bool:
        p = subprocess.Popen([sys.executable, "-m", "tkb.cli", "run", "--jsonl", *child_args(a), "--", vid],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        ok = True
        assert p.stdout is not None  # started with stdout=PIPE
        for line in p.stdout:
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            name = ev.pop("event")
            ok = ok and name != "error"
            if name == "progress":
                continue  # interleaved progress bars from several videos are unreadable
            with out:
                print(f"{vid} ", end="")
                emit(name, **ev)
        return p.wait() == 0 and ok

    with ThreadPoolExecutor(a.jobs) as ex:
        results = list(ex.map(one, vids))
    print(f"Finished: {sum(results)} ok, {len(results) - sum(results)} failed")
    return 0 if all(results) else 1

