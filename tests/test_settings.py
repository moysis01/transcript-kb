"""Settings: environment beats the page, which beats the default; changes apply without a restart."""
import json
import os
import subprocess
import sys

import pytest

from tkb import config
from tkb.core import llm
from tkb.services import settings

from fakes import FakeClaude


def test_a_value_comes_from_the_environment_then_the_page_then_the_default(monkeypatch):
    assert config.get("jobs") == 8
    settings.save({"jobs": 4})
    assert config.get("jobs") == 4
    monkeypatch.setenv("TKB_JOBS", "2")
    assert config.get("jobs") == 2
    with pytest.raises(ValueError, match="environment variable TKB_JOBS"):
        settings.save({"jobs": 5})
    field = next(f for s in settings.page()["sections"] for f in s["fields"] if f["key"] == "jobs")
    assert field["env"] == "TKB_JOBS" and field["value"] == 2


@pytest.mark.parametrize("change, why", [
    ({"nope": 1}, "unknown setting"),
    ({"jobs": 99}, "between 1 and 16"),
    ({"model.clean": "--dangerously-skip-permissions"}, "not a model name"),
    ({"model.clean": "opus; rm -rf ~"}, "not a model name"),
    ({"model.default": ""}, "choose a model"),
    ({"asr.model": "not a model"}, "turbo, large-v3, or a model like"),
    ({"prompt.ask": "   "}, "can't be empty"),
    ({"jobs": 2, "ahead": 3}, "can't be more than videos at once"),
    ({"chunk.min_words": 3000, "chunk.max_words": 2000}, "can't be more than part size"),
    ({"discover.max_picks": 50, "discover.read_max": 40}, "can't be more than most candidates read"),
    ({"notes.kinds": "concept\nBad Kind"}, "isn't allowed"),
    ({"noise.ad_phrases": "promo code\n(unclosed"}, "isn't a valid pattern"),
    ({"noise.remove": ["sponsor", "gossip"]}, "not one of the options"),
    ({"search.k1": "lots"}, "not a number"),
])
def test_bad_values_are_refused_and_nothing_is_saved(change, why):
    with pytest.raises(ValueError, match=why):
        settings.save(change)
    assert not config.SETTINGS_FILE.exists()


def test_search_models_can_be_local_or_a_providers():
    s = config.SETTINGS["search.model"]
    assert config.parse(s, "openai/text-embedding-3-small") == "openai/text-embedding-3-small"
    assert config.parse(s, "nomic-ai/nomic-embed-text-v1.5") == "nomic-ai/nomic-embed-text-v1.5"  # a local model
    with pytest.raises(ValueError, match="has no embeddings"):
        config.parse(s, "anthropic/some-model")


def test_reset_and_saving_the_default_leave_nothing_behind():
    settings.save({"jobs": 4, "discover.min_score": 60})
    settings.save({"jobs": None, "discover.min_score": 45})  # a reset, and the default typed in
    assert json.loads(config.SETTINGS_FILE.read_text()) == {}


def test_models_and_prompts_apply_to_the_next_call(monkeypatch):
    from test_claude_paths import _cleaned_video

    from tkb.services import search
    fake = FakeClaude()
    monkeypatch.setattr(llm, "call", fake)
    from tkb.core.storage import write_json
    _cleaned_video("VVVVVVVVVV1", ["Calibrated probabilities make routing easy."])
    write_json(config.DATA / "VVVVVVVVVV1" / "kb.json", {"tldr": "", "takeaways": [], "tags": [], "concepts": []})
    settings.save({"model.ask": "opus", "prompt.ask": "Answer in one word."})
    fake.on("ask", lambda m: "Yes.")  # registered under the edited prompt: the call must use it
    search.ask("calibrated probabilities")
    assert fake.models == ["claude-code/opus"]
    settings.save({"model.ask": None, "model.default": "fable"})
    search.ask("calibrated probabilities")
    assert fake.models[-1] == "claude-code/fable", "a task left on Default follows the default model"


def test_video_jobs_see_a_change_without_a_restart():
    settings.save({"model.clean": "haiku", "speakers.max": 5})
    code = "from tkb import config; print(config.model('clean'), config.get('speakers.max'))"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=os.environ, check=True)
    assert out.stdout.split() == ["claude-code/haiku", "5"]


def test_jobs_from_the_app_never_fall_back_to_the_default():
    from tkb.app import jobs
    settings.save({"translate": True, "sponsorblock": False})
    cmd = jobs._command({"vid": "VVVVVVVVVV1", "sponsorblock": True, "translate": False})
    assert "--no-translate" in cmd and "--sponsorblock" in cmd


def test_command_line_choices_left_out_come_from_settings():
    from tkb import cli
    settings.save({"translate": True, "speakers": False})
    a = cli.build_parser().parse_args(["run", "VVVVVVVVVV1"])
    cli._settle(a)
    assert (a.translate, a.speakers, a.sponsorblock, a.jobs, a.llm_model) == (True, False, True, 8, None)
    a = cli.build_parser().parse_args(["run", "VVVVVVVVVV1", "--no-translate", "--no-sponsorblock"])
    cli._settle(a)
    assert (a.translate, a.sponsorblock) == (False, False)


def test_the_queue_follows_the_capacity_settings():
    from tkb.app import jobs
    q = jobs.Jobs.__new__(jobs.Jobs)  # just the scheduling rule, no dispatcher thread
    q.max_running = q.ahead = None
    q.jobs = [{"state": "running", "stage": "clean"}, {"state": "running", "stage": "transcribe"}]
    assert q._room()
    settings.save({"jobs": 2, "ahead": 1})
    assert not q._room()
