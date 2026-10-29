"""Every setting in one place: where data lives, and every value the Settings page can change.

Each setting is read when it's used, so a change applies without a restart: video jobs are separate processes
and pick it up from their next video, and the app reads it on its next use. A value comes from, in order:
    1. its environment variable, if set (e.g. TKB_JOBS=2 uv run tkb serve): the page then shows it as locked;
    2. data/.settings.json, written by the Settings page;
    3. the default below.
Where the library lives (TKB_HOME) and the lock folder (TKB_LOCKS) can only be set by environment variable,
since the settings file itself lives in the library.

Settings that shape how a video is cleaned (splitting, what counts as noise, SponsorBlock) never change a video
you already have: each video is pinned to the values it was cleaned with (see tkb.pipeline.clean), so they
apply to new videos and to Redo. Paragraph breaks are fixed when a video is transcribed.
"""
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent  # the project folder (code, README, tests)
# TKB_HOME relocates all state (tests and benchmarks use a scratch home, never the real library).
HOME = Path(os.environ["TKB_HOME"]).resolve() if os.environ.get("TKB_HOME") else ROOT
DATA = HOME / "data"  # per-video working files, queue, tasks, Discover memory
KB = HOME / "kb"      # the Markdown knowledge base
LOCKS = Path(os.environ.get("TKB_LOCKS", Path.home() / ".cache" / "tkb" / "locks"))
SETTINGS_FILE = DATA / ".settings.json"
# Inside a container, local model servers (Ollama, LM Studio) run on the host, not on "localhost".
LOCAL = "host.docker.internal" if os.path.exists("/.dockerenv") else "localhost"

SECRETS_FILE = DATA / ".secrets.json"  # API keys: owner-only (0600), never sent to the page, never logged

# AI providers. A model is written "provider/model" (e.g. "openai/gpt-4.1", "ollama/llama3.1:8b",
# "openrouter/meta-llama/llama-3.3-70b-instruct"); a name without a known provider is a Claude Code model ("haiku").
PROVIDERS = {
    "claude-code": "Claude Code (your claude login)",
    "anthropic": "Anthropic API",
    "openai": "OpenAI",
    "gemini": "Google Gemini",
    "openrouter": "OpenRouter",
    "ollama": "Ollama (local)",
    "lmstudio": "LM Studio (local)",
    "custom": "Any OpenAI-compatible API",
    "command": "A command-line tool",
}
MODEL_PART = re.compile(r"[A-Za-z0-9][\w.:@/\-\[\]]{0,150}")  # never starts with "-": it can be a CLI argument
PRESETS = (("claude-code/sonnet", "Claude Code: Sonnet"), ("claude-code/opus", "Claude Code: Opus"),
           ("claude-code/haiku", "Claude Code: Haiku"), ("claude-code/fable", "Claude Code: Fable"))
FAST = {"claude-code": "claude-code/haiku"}  # quick jobs on a provider with a known fast model
EMBED_PROVIDERS = ("openai", "gemini", "ollama", "lmstudio", "custom")  # providers with an embeddings endpoint


def model_ref(name: str) -> str:
    """A model name as "provider/model": a name without a known provider is a Claude Code model."""
    name = name.strip()
    if not name:
        return ""
    provider, _, rest = name.partition("/")
    return name if provider in PROVIDERS and rest else f"claude-code/{name}"


@dataclass(frozen=True)
class Setting:
    key: str
    section: str
    label: str
    help: str
    default: Any
    kind: str     # int, float, bool, choice, multi (several of the choices), list (lines), model, text
    choices: tuple[tuple[Any, str], ...] = ()
    lo: float | None = None        # int/float: the range; list: how many lines
    hi: float | None = None
    env: str | None = None
    applies: str = "now"           # now | next video | redo (new videos and Redo) | transcribe | detect
    advanced: bool = False         # shown under "Show advanced"
    item: str | None = None        # list: a regex every line must match
    step: float | None = None      # float: the input's step
    test: str | None = None        # a provider whose connection the page can test from this setting


SECTIONS = [
    ("models", "AI models", "Which model does each job, from any provider."),
    ("providers", "AI providers", "Connect the providers your models come from. Keys stay on this Mac."),
    ("noise", "Noise removal", "What gets cut from transcripts."),
    ("transcription", "Transcription", "How videos are turned into text."),
    ("speakers", "Speakers", "How speakers are detected."),
    ("cleaning", "Splitting", "How transcripts are cut into sentences and parts for the AI."),
    ("notes", "Notes", "How notes are written."),
    ("capacity", "Speed", "How much runs at the same time."),
    ("discover", "Discover", "How videos are found and picked."),
    ("search", "Search and Ask", "How your library is searched and questions are answered."),
    ("watch", "Watched topics", "Topics that Discover runs on a schedule."),
    ("reliability", "Reliability", "How failures are retried."),
    ("mcp", "MCP server", "What an AI app gets when it reads your library over MCP."),
    ("history", "History", "What the app keeps."),
    ("prompts", "Prompts", "The instructions the AI gets for each job."),
]

NOISE = (("sponsor", "Sponsor and ad reads"), ("self_promotion", "Self-promotion"),
         ("engagement", "Like, subscribe, comment"), ("intro_outro", "Intros and sign-offs"),
         ("filler", "Filler and banter"), ("off_topic", "Off-topic"))
SPONSORBLOCK = (("sponsor", "Sponsor"), ("selfpromo", "Self-promotion"), ("interaction", "Like and subscribe"),
                ("intro", "Intro"), ("outro", "Outro"), ("preview", "Preview or recap"),
                ("music_offtopic", "Non-music in music videos"), ("filler", "Filler tangent"))
KINDS = ("concept", "technique", "tool", "person", "organization", "claim", "example", "other")
AD_PHRASES = (r"sponsor(?:ed|ing|s)?\b", "brought to you by", "promo code", r"use (?:my |the )?code\b", r"\d+% off",
              "free trial", r"link (?:is )?(?:in|down in|below in) the description", "in the description below",
              "affiliate link", "patreon", r"merch\b", r"smash (?:that|the) like",
              r"hit (?:that|the) (?:like|subscribe|bell)", r"don'?t forget to subscribe")
STOP_WORDS = tuple(sorted({
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "can", "do", "does", "for", "from", "has", "have", "how",
    "i", "if", "in", "into", "is", "it", "its", "of", "on", "or", "so", "that", "the", "their", "them", "there",
    "these", "they", "this", "to", "was", "we", "what", "when", "where", "which", "who", "why", "will", "with",
    "you", "your"}))

_S = [
    # ---- AI models
    Setting("model.default", "models", "Default model", "Used for every job below set to Default. Any provider: "
            "provider/model, e.g. openai/gpt-4.1 or ollama/llama3.1.", "claude-code/sonnet", "model", PRESETS,
            env="TKB_MODEL"),
    Setting("model.clean", "models", "Removing noise", "Cuts ads, filler and off-topic talk.", "", "model", PRESETS,
            applies="next video"),
    Setting("model.notes", "models", "Writing notes", "Summaries, takeaways and concept notes.", "", "model",
            PRESETS, applies="next video"),
    Setting("model.speakers", "models", "Naming speakers", "Works out who is speaking.", "", "model", PRESETS,
            applies="next video"),
    Setting("model.discover", "models", "Discover: picking videos", "Scores and picks the videos.", "", "model",
            PRESETS),
    Setting("model.phrasing", "models", "Discover: extra searches", "A quick job: by default the provider's fast "
            "model (Haiku on Claude Code), else the default model.", "", "model", PRESETS),
    Setting("model.ask", "models", "Ask", "Answers your questions.", "", "model", PRESETS),

    # ---- AI providers (keys: from the environment variable if set, else saved privately; never shown again)
    Setting("provider.anthropic.key", "providers", "Anthropic API key", "For anthropic/… models.", "", "secret",
            env="ANTHROPIC_API_KEY", test="anthropic"),
    Setting("provider.anthropic.url", "providers", "Anthropic API address", "", "https://api.anthropic.com", "url",
            advanced=True),
    Setting("provider.openai.key", "providers", "OpenAI API key", "For openai/… models.", "", "secret",
            env="OPENAI_API_KEY", test="openai"),
    Setting("provider.openai.url", "providers", "OpenAI API address", "", "https://api.openai.com/v1", "url",
            advanced=True),
    Setting("provider.gemini.key", "providers", "Google Gemini API key", "For gemini/… models.", "", "secret",
            env="GEMINI_API_KEY", test="gemini"),
    Setting("provider.gemini.url", "providers", "Gemini API address", "Its OpenAI-compatible endpoint.",
            "https://generativelanguage.googleapis.com/v1beta/openai", "url", advanced=True),
    Setting("provider.openrouter.key", "providers", "OpenRouter API key", "For openrouter/… models: hundreds of "
            "models behind one key.", "", "secret", env="OPENROUTER_API_KEY", test="openrouter"),
    Setting("provider.openrouter.url", "providers", "OpenRouter API address", "", "https://openrouter.ai/api/v1",
            "url", advanced=True),
    Setting("provider.ollama.url", "providers", "Ollama address", "For ollama/… models running on your machine.",
            f"http://{LOCAL}:11434", "url", test="ollama"),
    Setting("provider.ollama.context", "providers", "Ollama context length", "Tokens. Too small and long parts are "
            "silently cut.", 32768, "int", lo=2048, hi=1048576, advanced=True),
    Setting("provider.lmstudio.url", "providers", "LM Studio address", "For lmstudio/… models. Load them with a "
            "context length of 16k tokens or more.", f"http://{LOCAL}:1234/v1", "url", test="lmstudio"),
    Setting("provider.custom.url", "providers", "OpenAI-compatible address", "For custom/… models: Groq, Mistral, "
            "DeepSeek, Together, vLLM… e.g. https://api.groq.com/openai/v1", "", "url", test="custom"),
    Setting("provider.custom.key", "providers", "OpenAI-compatible API key", "If it needs one.", "", "secret",
            env="CUSTOM_AI_API_KEY"),
    Setting("provider.command.line", "providers", "Command-line tool", "For command/… models: a command that reads "
            "the request on its input and prints the answer; {model} is replaced, e.g. llm -m {model}", "", "line",
            test="command", advanced=True),
    Setting("ai.timeout", "providers", "Longest wait for an answer", "Seconds.", 600, "int", lo=30, hi=3600,
            advanced=True),
    Setting("ai.max_tokens", "providers", "Longest answer", "Tokens (Anthropic API needs a limit).", 16000, "int",
            lo=1000, hi=128000, advanced=True),

    # ---- Noise removal
    Setting("noise.remove", "noise", "Cut these", "Unticked kinds are kept in the transcript.",
            tuple(k for k, _ in NOISE), "multi", NOISE, applies="redo"),
    Setting("sponsorblock", "noise", "Use SponsorBlock", "Default for new videos: cut segments the community marked.",
            True, "bool"),
    Setting("sponsorblock.categories", "noise", "SponsorBlock segments cut", "",
            ("sponsor", "selfpromo", "interaction"), "multi", SPONSORBLOCK, applies="redo"),
    Setting("sponsorblock.overlap", "noise", "SponsorBlock overlap", "How much of a sentence must fall inside a "
            "marked segment to cut it.", 0.5, "float", lo=0.1, hi=1.0, step=0.05, applies="redo", advanced=True),
    Setting("clean.rewrite_ratio", "noise", "Reject rewrites longer than", "Times the original sentence's length "
            "(plus 5 words): the AI is meant to clean a sentence, not write a new one.", 2.0, "float", lo=1.0, hi=5.0,
            step=0.1, applies="redo", advanced=True),
    Setting("clean.check_numbers", "noise", "Flag added numbers", "Warn when a cleaned sentence has numbers the "
            "speaker didn't say.", True, "bool", applies="redo", advanced=True),
    Setting("clean.context_chars", "noise", "Video description shown", "Characters of the video's description the "
            "AI sees, for spelling names right.", 1200, "int", lo=0, hi=10000, applies="redo", advanced=True),
    Setting("clean.parallel", "noise", "Parts cleaned at once", "Per video; the AI requests limit still applies.",
            8, "int", lo=1, hi=32, applies="next video", advanced=True),
    Setting("noise.ad_phrases", "noise", "Ad warning phrases", "Kept sentences matching these are flagged for you "
            "to check. One regular expression per line.", AD_PHRASES, "list", lo=0, hi=100, item=r".{1,200}",
            applies="redo", advanced=True),

    # ---- Transcription
    Setting("asr.model", "transcription", "Speech model", "Turbo was faster and more accurate in tests. Or any "
            "model for the engine: e.g. mlx-community/whisper-small-mlx (MLX), small or distil-large-v3 "
            "(faster-whisper).", "turbo", "speech", (("turbo", "Turbo"), ("large-v3", "Large v3")), env="TKB_ASR",
            applies="next video"),
    Setting("asr.engine", "transcription", "Speech engine", "MLX uses an Apple Silicon Mac's GPU; faster-whisper runs "
            "anywhere (CPU, or an NVIDIA GPU), e.g. in Docker. Auto picks MLX where it's installed.", "auto", "choice",
            (("auto", "Auto"), ("mlx", "MLX (Apple Silicon)"), ("faster-whisper", "faster-whisper (CPU or NVIDIA)")),
            env="TKB_ASR_ENGINE", applies="next video"),
    Setting("asr.device", "transcription", "faster-whisper: device", "Auto uses an NVIDIA GPU if there is one.",
            "auto", "choice", (("auto", "Auto"), ("cpu", "CPU"), ("cuda", "NVIDIA GPU (CUDA)")), env="TKB_ASR_DEVICE",
            applies="next video", advanced=True),
    Setting("asr.threads", "transcription", "faster-whisper: CPU threads", "0 = half the cores (speaker detection "
            "uses the rest).", 0, "int", lo=0, hi=128, env="TKB_ASR_THREADS", applies="next video", advanced=True),
    Setting("translate", "transcription", "Translate to English", "Default for new videos.", False, "bool",
            env="TKB_TRANSLATE"),
    Setting("para.gap", "transcription", "New paragraph after a pause of", "Seconds.", 1.5, "float", lo=0.3, hi=10,
            step=0.1, applies="transcribe", advanced=True),
    Setting("para.chars", "transcription", "Paragraph length", "Characters before a paragraph ends at the next "
            "sentence.", 700, "int", lo=200, hi=5000, applies="transcribe", advanced=True),
    Setting("para.hard_chars", "transcription", "Longest paragraph", "Characters, for speech without punctuation.",
            1600, "int", lo=400, hi=10000, applies="transcribe", advanced=True),

    # ---- Speakers
    Setting("speakers", "speakers", "Detect speakers", "Label who says what in interviews and podcasts.",
            True, "bool", applies="next video"),
    Setting("speakers.max", "speakers", "Most speakers", "The most speakers detection will look for.", 8,
            "int", lo=2, hi=12, applies="detect"),
    Setting("speakers.min_speech", "speakers", "Speech needed to tell speakers apart", "Seconds. With less, a video "
            "counts as one speaker unless you give the number.", 50, "int", lo=10, hi=600, applies="detect",
            advanced=True),
    Setting("speakers.window", "speakers", "Voice sample length", "Seconds of speech per voice sample.", 2.5,
            "float", lo=1, hi=6, step=0.5, applies="detect", advanced=True),
    Setting("speakers.neighbours", "speakers", "Voices compared", "Share of the most similar voice samples each one "
            "is compared with.", 0.1, "float", lo=0.02, hi=0.5, step=0.01, applies="detect", advanced=True),
    Setting("speakers.restarts", "speakers", "Grouping attempts", "Tries at grouping voices; the best is kept.", 8,
            "int", lo=1, hi=50, applies="detect", advanced=True),
    Setting("speakers.min_on", "speakers", "Shortest speech", "Seconds of speech that count as speech.", 0.3, "float",
            lo=0.05, hi=3, step=0.05, applies="detect", advanced=True),
    Setting("speakers.min_off", "speakers", "Shortest pause", "Seconds of silence that split speech.", 0.5, "float",
            lo=0.05, hi=3, step=0.05, applies="detect", advanced=True),
    Setting("speakers.name_lines", "speakers", "Naming: lines per speaker", "Each speaker's first lines the AI reads "
            "to work out their name.", 10, "int", lo=2, hi=60, applies="detect", advanced=True),
    Setting("speakers.name_opening", "speakers", "Naming: opening read", "Sentences from the start, where people "
            "are usually introduced.", 60, "int", lo=0, hi=400, applies="detect", advanced=True),
    Setting("speakers.sensitivity", "speakers", "Separation sensitivity", "Higher finds more speakers.", 0.15,
            "float", lo=0.05, hi=0.4, step=0.01, applies="detect", advanced=True),

    # ---- Splitting (pinned per video: changes apply to new videos and Redo)
    Setting("chunk.max_words", "cleaning", "Part size", "Words sent to the AI at once.", 2200, "int", lo=500,
            hi=6000, applies="redo"),
    Setting("chunk.min_words", "cleaning", "Smallest part", "Words; a chapter starts a new part only after this.",
            600, "int", lo=100, hi=3000, applies="redo", advanced=True),
    Setting("chunk.chapters", "cleaning", "Start a part at each chapter", "Uses the video's chapters as natural "
            "breaks, once a part has the smallest size.", True, "bool", applies="redo"),
    Setting("chunk.tail", "cleaning", "Merge a short last part", "Into the one before, when smaller than this share "
            "of the smallest part.", 0.3333, "float", lo=0, hi=1, step=0.05, applies="redo", advanced=True),
    Setting("sentence.max_words", "cleaning", "Longest sentence", "Words. Longer ones, as in unpunctuated speech, "
            "are split.", 40, "int", lo=15, hi=200, applies="redo", advanced=True),
    Setting("sentence.max_secs", "cleaning", "Longest sentence time", "Seconds.", 20, "int", lo=5, hi=120,
            applies="redo", advanced=True),
    Setting("sentence.target_words", "cleaning", "Split long sentences into", "Words per piece.", 25, "int", lo=8,
            hi=100, applies="redo", advanced=True),
    Setting("sentence.target_secs", "cleaning", "Split long sentences every", "Seconds.", 15, "int", lo=3, hi=60,
            applies="redo", advanced=True),

    # ---- Notes
    Setting("notes.kinds", "notes", "Kinds of concept", "Every concept gets one. Lowercase, one per line; "
            "\"other\" is always kept.", KINDS, "list", lo=2, hi=20, item=r"[a-z][a-z0-9-]{1,30}", applies="redo"),

    Setting("notes.merge_synonyms", "notes", "Merge synonyms", "One concept for names that mean the same thing "
            "(the other names become aliases).", True, "bool", applies="redo"),
    Setting("notes.link_existing", "notes", "Link to existing concepts", "New videos reuse the concepts already in "
            "your library, so notes connect across videos.", True, "bool", applies="redo"),
    Setting("notes.parallel", "notes", "Parts read at once", "Per video; the AI requests limit still applies.", 8,
            "int", lo=1, hi=32, applies="next video", advanced=True),

    # ---- Speed
    Setting("jobs", "capacity", "Videos at once", "Videos in progress at the same time.", 8, "int", lo=1, hi=16,
            env="TKB_JOBS"),
    Setting("ahead", "capacity", "Downloading and transcribing at once", "Can't be more than videos at once.", 3,
            "int", lo=1, hi=8, env="TKB_AHEAD"),
    Setting("limit.youtube", "capacity", "YouTube requests at once", "More can get you rate-limited.", 3, "int",
            lo=1, hi=6, env="TKB_YOUTUBE"),
    Setting("limit.gpu", "capacity", "Transcriptions at once", "One uses the whole GPU.", 1, "int", lo=1, hi=2,
            env="TKB_GPU"),
    Setting("limit.cpu", "capacity", "Speaker detections at once", "", 2, "int", lo=1, hi=4, env="TKB_CPU"),
    Setting("limit.llm", "capacity", "AI requests at once", "", 6, "int", lo=1, hi=12, env="TKB_LLM"),

    # ---- Discover
    Setting("discover.min_score", "discover", "Lowest score picked", "Out of 100.", 45, "int", lo=0, hi=95),
    Setting("discover.per_channel", "discover", "Picks per channel", "For keyword searches.", 2, "int", lo=1, hi=20),
    Setting("discover.max_picks", "discover", "Most picks", "The largest choice in the Pick menu.", 20, "int",
            lo=5, hi=50),
    Setting("discover.phrasings", "discover", "Extra searches per search", "Other ways to search the same topic, "
            "written by the AI.", 2, "int", lo=0, hi=4),
    Setting("discover.shorts", "discover", "Include Shorts", "In keyword searches.", False, "bool"),
    Setting("discover.min_length", "discover", "Shortest video", "Seconds, in keyword searches.", 120, "int", lo=0,
            hi=1800),
    Setting("discover.order", "discover", "Order picks by", "After the lowest score and channel limits.", "score",
            "choice", (("score", "AI score"), ("views", "Most viewed"), ("recent", "Newest"))),
    Setting("discover.cover_searches", "discover", "Every search gets a pick", "With several searches, the best of "
            "each is picked first (if good enough), so one broad search can't crowd out the rest.", True, "bool"),
    Setting("discover.read_per_pick", "discover", "Candidates read per pick", "How many candidates are read in "
            "detail for each video you ask for.", 2, "int", lo=1, hi=6, advanced=True),
    Setting("discover.context_chars", "discover", "Description shown per candidate", "Characters of each "
            "candidate's description the AI sees when scoring.", 500, "int", lo=0, hi=5000, advanced=True),
    Setting("discover.results", "discover", "Results per search", "Read from each YouTube results page.", 25,
            "int", lo=5, hi=60, advanced=True),
    Setting("discover.read_per_search", "discover", "Candidates read per search", "Read in detail before scoring.",
            8, "int", lo=3, hi=30, advanced=True),
    Setting("discover.read_max", "discover", "Most candidates read", "In detail, across all searches.", 48, "int",
            lo=10, hi=120, advanced=True),
    Setting("channel.per_tab", "discover", "Channel: items read per tab", "Videos, Shorts, Live and Posts.", 300,
            "int", lo=30, hi=1000),
    Setting("channel.playlists", "discover", "Channel: playlists read", "", 10, "int", lo=0, hi=50),
    Setting("channel.per_playlist", "discover", "Channel: items read per playlist", "", 100, "int", lo=10, hi=500,
            advanced=True),
    Setting("channel.per_source", "discover", "Channel: always read from each source", "Items from each of Videos, "
            "Shorts, Live, playlists and Posts.", 2, "int", lo=0, hi=10, advanced=True),
    Setting("confirm_above", "discover", "Ask before adding more than", "Videos added at once from links.", 50,
            "int", lo=5, hi=1000),

    # ---- Search and Ask
    Setting("search.model", "search", "Search by meaning", "Finds passages that say the same thing in other words. "
            "A local model, or a provider's embeddings (e.g. openai/text-embedding-3-small, ollama/nomic-embed-text): "
            "these send your whole library to it once.", "snowflake/snowflake-arctic-embed-xs", "embed",
            (("snowflake/snowflake-arctic-embed-xs", "On: fast local model"),
             ("BAAI/bge-small-en-v1.5", "On: BGE local model"), ("off", "Off: keywords only")), env="TKB_EMBED"),
    Setting("search.strictness", "search", "Meaning matches", "How close a passage must be to count.", "strict",
            "choice", (("strict", "Strict"), ("normal", "Normal"), ("loose", "Loose"))),
    Setting("search.results", "search", "Search results shown", "", 40, "int", lo=10, hi=200),
    Setting("ask.transcript", "search", "Ask: passages read", "From transcripts.", 10, "int", lo=0, hi=40),
    Setting("ask.concepts", "search", "Ask: concept notes read", "", 6, "int", lo=0, hi=20),
    Setting("ask.videos", "search", "Ask: video summaries read", "", 3, "int", lo=0, hi=20),
    Setting("ask.chars", "search", "Ask: longest passage", "Characters of each source the AI reads.", 1500, "int",
            lo=300, hi=8000, advanced=True),
    Setting("search.keyword_weight", "search", "Keyword ranking weight", "How much exact words count in the "
            "combined ranking.", 1.0, "float", lo=0, hi=5, step=0.1, advanced=True),
    Setting("search.meaning_weight", "search", "Meaning ranking weight", "How much meaning counts in the combined "
            "ranking.", 1.0, "float", lo=0, hi=5, step=0.1, advanced=True),
    Setting("search.video_boost", "search", "Video summary boost", "How much video summaries are favoured in "
            "keyword results (1 = not at all).", 1.0, "float", lo=0.1, hi=3, step=0.1, advanced=True),
    Setting("search.transcript_boost", "search", "Transcript boost", "How much transcript passages are favoured "
            "in keyword results.", 1.0, "float", lo=0.1, hi=3, step=0.1, advanced=True),
    Setting("search.depth", "search", "Ranking depth", "How far down each ranking is combined.", 100, "int", lo=10,
            hi=1000, advanced=True),
    Setting("ask.check_citations", "search", "Ask: remove unsupported citations", "Drop citations to sources the AI "
            "wasn't given.", True, "bool"),
    Setting("search.concept_boost", "search", "Concept boost", "How much concept notes are favoured in keyword "
            "results (1 = not at all).", 1.3, "float", lo=1, hi=3, step=0.1, advanced=True),
    Setting("search.k1", "search", "Keyword ranking: term saturation", "BM25 k1.", 1.2, "float", lo=0.1, hi=3,
            step=0.1, advanced=True),
    Setting("search.b", "search", "Keyword ranking: length normalisation", "BM25 b.", 0.75, "float", lo=0, hi=1,
            step=0.05, advanced=True),
    Setting("search.fusion", "search", "Ranking fusion constant", "Reciprocal rank fusion k: lower favours each "
            "ranking's top results.", 60, "int", lo=1, hi=200, advanced=True),
    Setting("search.stop_words", "search", "Ignored words", "Too common to search for. One per line.", STOP_WORDS,
            "list", lo=0, hi=500, item=r"\w{1,30}", advanced=True),

    # ---- Watched topics
    Setting("watch.picks", "watch", "Videos per run", "Default when you watch a topic.", 3, "int", lo=1, hi=10),
    Setting("watch.retry_minutes", "watch", "Retry a failed run after", "Minutes.", 60, "int", lo=5, hi=1440),
    Setting("watch.check_minutes", "watch", "Check for due topics every", "Minutes, while the app is open.", 5,
            "int", lo=1, hi=120, advanced=True),

    # ---- Reliability
    Setting("youtube.retries", "reliability", "YouTube retries", "After a rate limit or a dropped download.", 4,
            "int", lo=0, hi=10),
    Setting("youtube.max_wait", "reliability", "Longest wait between YouTube retries", "Seconds.", 60, "int", lo=5,
            hi=600, advanced=True),
    Setting("claude.retries", "reliability", "AI attempts", "Per request, before giving up.", 3, "int", lo=1,
            hi=8),

    # ---- MCP server
    Setting("mcp.hit_chars", "mcp", "Search result length", "Characters of each search result.", 600, "int", lo=100,
            hi=5000),
    Setting("mcp.excerpt_chars", "mcp", "Transcript excerpt length", "Characters per transcript request.", 15000,
            "int", lo=2000, hi=100000),

    # ---- History
    Setting("tasks.keep", "history", "Saved results kept", "Discover and Ask results, per kind.", 50, "int",
            lo=5, hi=500),
    Setting("queue.keep", "history", "Queue entries kept", "Finished and failed jobs remembered.", 200, "int",
            lo=20, hi=2000),
]
PROMPTS = {"clean": "Removing noise", "extract": "Concept notes", "synthesize": "Video summary",
           "name_speakers": "Naming speakers", "discover_queries": "Discover: extra searches",
           "discover_rank": "Discover: scoring", "discover_channel": "Discover: channel shortlist", "ask": "Ask"}
_S += [Setting(f"prompt.{name}", "prompts", label, "", None, "text",
               applies="next video" if name in ("clean", "extract", "synthesize", "name_speakers") else "now")
       for name, label in PROMPTS.items()]
SETTINGS = {s.key: s for s in _S}

_saved: dict = {"mtime": None, "values": {}}


def saved() -> dict:
    """The values saved by the Settings page (re-read whenever the file changes)."""
    try:
        mtime = SETTINGS_FILE.stat().st_mtime
    except OSError:
        return {}
    if mtime != _saved["mtime"]:
        try:
            values = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            values = {}
        _saved.update(mtime=mtime, values=values if isinstance(values, dict) else {})
    return _saved["values"]


def parse(s: Setting, raw: Any) -> Any:
    """A raw value (from the page, the file or the environment) as the setting's type; ValueError if invalid."""
    if s.kind == "bool":
        if isinstance(raw, str):
            return raw.strip().lower() in ("1", "true", "yes", "on")
        return bool(raw)
    if s.kind in ("int", "float"):
        if isinstance(raw, bool):
            raise ValueError(f"{s.label}: not a number")
        try:
            n = int(raw) if s.kind == "int" else round(float(raw), 4)
        except (TypeError, ValueError) as e:
            raise ValueError(f"{s.label}: not a number") from e
        if (s.lo is not None and n < s.lo) or (s.hi is not None and n > s.hi):
            raise ValueError(f"{s.label}: between {_num(s.lo)} and {_num(s.hi)}")
        return n
    if s.kind in ("multi", "list"):
        items = raw.splitlines() if isinstance(raw, str) else list(raw)
        if s.kind == "list":
            items = [str(x).strip() for x in items if str(x).strip()]
        return _items(s, items)
    if s.kind == "choice":
        if raw not in [c for c, _ in s.choices]:
            raise ValueError(f"{s.label}: not one of the options")
        return raw
    if s.kind == "text":
        if not str(raw).strip():
            raise ValueError(f"{s.label}: can't be empty")
        return str(raw)
    text = str(raw).strip()
    if s.kind == "model":
        ref = model_ref(text)
        if ref and not MODEL_PART.fullmatch(ref.partition("/")[2]):
            raise ValueError(f"{s.label}: not a model name (write provider/model, e.g. openai/gpt-4.1)")
        return ref
    if s.kind == "secret":
        if len(text) > 500 or any(c.isspace() for c in text):
            raise ValueError(f"{s.label}: not a key")
        return text
    if s.kind == "url":
        if text and not re.fullmatch(r"https?://[^\s/?#]+(/[^\s?#]*)?", text):
            raise ValueError(f"{s.label}: not a web address (http://… or https://…)")
        return text.rstrip("/")
    if s.kind == "line":
        import shlex
        try:
            words = shlex.split(text)
        except ValueError as e:
            raise ValueError(f"{s.label}: {e}") from e
        if text and (not words or words[0].startswith("-")):
            raise ValueError(f"{s.label}: start with the program's name")
        return text
    if s.kind == "speech":  # a name ("turbo", "small"), or a model repository "owner/name"
        if not re.fullmatch(r"[A-Za-z0-9][\w.\-]{0,80}(/[\w.\-]{1,100})?", text):
            raise ValueError(f"{s.label}: turbo, large-v3, or a model like mlx-community/whisper-small-mlx")
        return text
    if s.kind == "embed":  # off, a local (fastembed) model "owner/name", or a provider's ("openai/…")
        if text != "off" and not re.fullmatch(r"[\w.\-]{1,80}/[\w.:@/\-]{1,150}", text):
            raise ValueError(f"{s.label}: off, a local model, or provider/model")
        if text.partition("/")[0] in PROVIDERS and text.partition("/")[0] not in EMBED_PROVIDERS:
            raise ValueError(f"{s.label}: {PROVIDERS[text.partition('/')[0]]} has no embeddings")
        return text
    return text


def _num(x: float | None) -> str:
    return "" if x is None else f"{x:g}"


def _items(s: Setting, items: list) -> tuple:
    """A multi (some of the choices) or a list (lines, each matching `item`), validated, in order, without repeats."""
    out = tuple(dict.fromkeys(items))
    if s.kind == "multi":
        allowed = [c for c, _ in s.choices]
        if any(x not in allowed for x in out):
            raise ValueError(f"{s.label}: not one of the options")
        return tuple(c for c in allowed if c in out)
    if s.lo is not None and len(out) < s.lo or s.hi is not None and len(out) > s.hi:
        raise ValueError(f"{s.label}: between {_num(s.lo)} and {_num(s.hi)} lines")
    if s.item and (bad := next((x for x in out if not re.fullmatch(s.item, x)), None)):
        raise ValueError(f"{s.label}: {bad[:40]!r} isn't allowed")
    if s.key == "noise.ad_phrases":
        for x in out:
            try:
                re.compile(x)
            except re.error as e:
                raise ValueError(f"{s.label}: {x[:40]!r} isn't a valid pattern ({e})") from e
    return out


def from_env(key: str) -> Any:
    s = SETTINGS[key]
    raw = os.environ.get(s.env) if s.env else None
    if raw in (None, ""):
        return None
    try:
        return parse(s, raw)
    except ValueError:
        return None


_secrets: dict = {"mtime": None, "values": {}}


def secrets() -> dict:
    """The API keys saved by the Settings page (a separate, owner-only file)."""
    try:
        mtime = SECRETS_FILE.stat().st_mtime
    except OSError:
        return {}
    if mtime != _secrets["mtime"]:
        try:
            values = json.loads(SECRETS_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            values = {}
        _secrets.update(mtime=mtime, values=values if isinstance(values, dict) else {})
    return _secrets["values"]


def get(key: str) -> Any:
    """The setting's current value: environment, then the Settings page, then the default."""
    s = SETTINGS[key]
    if (v := from_env(key)) is not None:
        return v
    if s.kind == "secret":
        return str(secrets().get(key) or "")
    if key in (file := saved()):
        try:
            return parse(s, file[key])
        except (ValueError, TypeError):
            pass
    return s.default


def model(task: str) -> str:
    """The model for a task (clean, notes, speakers, discover, phrasing, ask), as "provider/model". A task left on
    Default follows the default model; a quick job (phrasing) takes its provider's fast model if it has one."""
    if own := get(f"model.{task}"):
        return own
    default = get("model.default")
    return FAST.get(default.partition("/")[0], default) if task == "phrasing" else default
