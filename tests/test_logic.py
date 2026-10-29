import numpy as np

from tkb import config
from tkb.pipeline import clean, diarize
from tkb.services import discover, sources


def test_sentence_split_keeps_ellipsis_and_uses_word_times():
    segs = [{"start": 0, "end": 2, "text": "Everyone knows if you shake it...",
             "words": [{"start": 0, "end": 0.5, "word": " Everyone"}, {"start": 0.5, "end": 0.9, "word": " knows"},
                       {"start": 0.9, "end": 1.0, "word": " if"}, {"start": 1.0, "end": 1.2, "word": " you"},
                       {"start": 1.2, "end": 1.5, "word": " shake"}, {"start": 1.5, "end": 2.0, "word": " it..."}]},
            {"start": 2, "end": 4, "text": "it explodes. Pi is 3.14!",
             "words": [{"start": 2.0, "end": 2.2, "word": " it"}, {"start": 2.2, "end": 2.8, "word": " explodes."},
                       {"start": 3.0, "end": 3.2, "word": " Pi"}, {"start": 3.2, "end": 3.4, "word": " is"},
                       {"start": 3.4, "end": 4.0, "word": " 3.14!"}]}]
    out = clean.split_sentences(segs, [{"start": 0}])
    assert [s["src"] for s in out] == ["Everyone knows if you shake it... it explodes.", "Pi is 3.14!"]
    assert out[1]["start"] == 3.0 and out[1]["end"] == 4.0


def test_youtube_filter_encoding_matches_verified_values():
    # date=this month, duration=4-20 min: bytes 12 06 08 04 10 01 18 03
    assert discover._sp("medium", "month", "relevance") == "EgYIBBABGAM="
    assert discover._sp("any", "any", "popularity").startswith("CAMS")  # sort=view count first


def test_bare_ids_must_look_like_ids():
    ids = [e["id"] for e in sources.expand("interesting programming LPZh9BOjkQs aircAruvnKk")[0]]
    assert ids == ["LPZh9BOjkQs", "aircAruvnKk"]


def _clusters(k, n=40, dim=64, seed=0):
    rng = np.random.default_rng(seed)
    centers = rng.normal(size=(k, dim))
    pts = np.vstack([c + 0.15 * rng.normal(size=(n, dim)) for c in centers])
    return pts / np.linalg.norm(pts, axis=1, keepdims=True)


def test_speaker_count_from_eigengap():
    for k in (1, 2, 3, 4):
        assert diarize._spectral(_clusters(k), None)[0] == k


def test_short_audio_is_one_speaker_unless_told():
    assert diarize._spectral(_clusters(2, n=5), None)[0] == 1
    assert diarize._spectral(_clusters(2, n=5), 2)[0] == 2


def test_name_grounding_tolerates_asr_spelling():
    assert diarize._close("karl", "carl") and not diarize._close("john", "jon") and not diarize._close("bob", "rob")


def test_unpunctuated_speech_is_split_into_short_sentences():
    """Hindi/Hinglish transcripts often have no sentence punctuation at all."""
    segs = [{"start": i * 4.0, "end": i * 4.0 + 3.9, "text": "जेव से मैंने ये कर दिया और ये बन गया तो पहला question"}
            for i in range(30)]
    out = clean.split_sentences(segs, [{"start": 0}, {"start": 60}])
    assert len(out) >= 10
    assert all(len(s["src"].split()) <= config.get("sentence.max_words") for s in out)
    assert " ".join(s["src"] for s in out) == " ".join(s["text"] for s in segs)  # nothing lost or duplicated
    assert {s["para"] for s in out} == {0, 1}  # the second paragraph gets its own sentences


def test_danda_ends_a_sentence():
    out = clean.split_sentences([{"start": 0, "end": 4, "text": "यह पहला वाक्य है। यह दूसरा है।"}], [{"start": 0}])
    assert [s["src"] for s in out] == ["यह पहला वाक्य है।", "यह दूसरा है।"]


def test_videos_cleaned_before_the_sentence_cap_keep_their_split(home):
    """Re-running an old video must reuse its cleaning cache (no Claude calls), so its old split is kept."""
    from tkb.core.storage import write_json
    ws = [{"start": i * 0.3, "end": i * 0.3 + 0.3, "word": f" w{i}"} for i in range(60)]
    ws[-1]["word"] += "."
    words_ = "".join(w["word"] for w in ws).strip()
    doc = {"segments": [{"start": 0, "end": 18, "text": words_, "words": ws}], "paragraphs": [{"start": 0}]}
    d = home / "data" / "OLDVIDEO001"
    (d / "clean").mkdir(parents=True)
    pin, sents = clean._pinned(d, doc)
    assert len(sents) > 1 and pin["sentence"], "a new video gets the cap"
    (d / "clean" / clean.PIN).unlink()
    write_json(d / "clean" / "000.json", {})  # cleaned, uncapped, before the cap (and pins) existed
    write_json(d / "clean.json", {"sentences": [{"src": words_}]})
    pin, sents = clean._pinned(d, doc)
    assert [s["src"] for s in sents] == [words_] and pin["sentence"] is None


def test_https_trusts_the_os_certificate_store(monkeypatch):
    # HTTPS and yt-dlp use the operating system's certificate store, not certifi's bundle
    import ssl

    import truststore
    import yt_dlp

    import tkb.cli  # noqa: F401  (injects the OS trust store)
    from tkb.core import youtube
    assert ssl.SSLContext is truststore.SSLContext
    seen = {}

    class YDL:
        def __init__(self, o):
            seen.update(o)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download):
            return {}

    monkeypatch.setattr(yt_dlp, "YoutubeDL", YDL)
    youtube.extract("https://www.youtube.com/watch?v=jNQXAC9IVRw")
    assert "no-certifi" in seen["compat_opts"]
