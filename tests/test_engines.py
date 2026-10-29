"""Two transcription engines, one result: MLX on Apple Silicon, faster-whisper anywhere else (e.g. in Docker).
faster-whisper is replaced by a stand-in here, so these tests run offline on any machine."""
import platform
import sys
import types
from collections import namedtuple

import pytest

from tkb.pipeline import transcribe as tr
from tkb.services import settings

Seg = namedtuple("Seg", "start end text no_speech_prob avg_logprob words")
Word = namedtuple("Word", "start end word probability")


@pytest.fixture
def fake_fw(monkeypatch):
    """A faster_whisper module that 'decodes' two segments, and records how its model was loaded."""
    fw = types.ModuleType("faster_whisper")
    loaded = []

    class WhisperModel:
        def __init__(self, name, **kw):
            loaded.append((name, kw))

        def transcribe(self, audio, **opts):
            self.opts = opts
            hello = [Word(0.0, 1.0, " Hello", 0.9), Word(1.0, 2.0, " there.", 0.9)]
            segs = [Seg(0.0, 2.0, " Hello there.", 0.01, -0.1, hello),
                    Seg(2.0, 4.0, " Bye.", 0.02, -0.2, [Word(2.0, 4.0, " Bye.", 0.9)])]
            return iter(segs), types.SimpleNamespace(language="en", duration=4.0)

    fw.WhisperModel, fw.loaded = WhisperModel, loaded
    monkeypatch.setitem(sys.modules, "ctranslate2", types.SimpleNamespace(get_cuda_device_count=lambda: 0))
    tr._fw_models.clear()
    yield fw
    tr._fw_models.clear()


def test_auto_takes_mlx_on_apple_silicon_and_faster_whisper_elsewhere():
    mac = sys.platform == "darwin" and platform.machine() == "arm64"
    assert tr.engine() == ("mlx" if mac else "faster-whisper")
    settings.save({"asr.engine": "faster-whisper"})
    assert tr.engine() == "faster-whisper"


def test_faster_whisper_gives_the_same_shape_as_mlx(fake_fw, capsys):
    res = tr._run_whisper(fake_fw, "audio.wav", "turbo", {"title": "Greetings"}, words=True)
    assert res["language"] == "en" and [s["text"] for s in res["segments"]] == [" Hello there.", " Bye."]
    assert res["segments"][0]["words"][1] == {"start": 1.0, "end": 2.0, "word": " there."}
    assert {"no_speech_prob", "avg_logprob"} <= set(res["segments"][0])
    assert "100%|" in capsys.readouterr().err, "the progress the web app reads"
    name, kw = fake_fw.loaded[0]
    assert name == "turbo" and kw["device"] == "cpu" and kw["compute_type"] == "int8"
    opts = tr._fw_models["turbo"].opts
    assert opts["condition_on_previous_text"] is False and opts["vad_filter"] is False and opts["initial_prompt"]
    tr._release(fake_fw)
    assert not tr._fw_models, "the model is freed for the next job"


def test_a_model_for_the_other_engine_is_refused_clearly():
    assert tr.resolve_model("turbo", "faster-whisper") == "turbo"
    assert tr.resolve_model("turbo", "mlx") == "mlx-community/whisper-large-v3-turbo"
    assert tr.resolve_model("distil-large-v3", "faster-whisper") == "distil-large-v3"
    with pytest.raises(ValueError, match="is an MLX model, but transcription uses faster-whisper"):
        tr.resolve_model("mlx-community/whisper-small-mlx", "faster-whisper")
    with pytest.raises(ValueError, match="is a faster-whisper"):
        tr.resolve_model("Systran/faster-whisper-small", "mlx")
    settings.save({"asr.engine": "faster-whisper"})
    with pytest.raises(ValueError, match="is an MLX model"):
        settings.save({"asr.model": "mlx-community/whisper-small-mlx"})
