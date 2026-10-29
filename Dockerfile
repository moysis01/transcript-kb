# transcript-kb in a container: the web app (and every `tkb` command) on Linux, amd64 or arm64.
# Transcription uses faster-whisper on the CPU; on an Apple Silicon Mac, running natively is much faster (MLX uses
# the GPU, which Docker can't). See README → Docker.
#
#   docker compose up -d          then open http://127.0.0.1:8765
#
# Build arguments: CLAUDE_CODE (install Claude Code, the default AI provider: "true"/"false") and
# CLAUDE_CODE_VERSION (pinned).

ARG PYTHON=3.13

# ---------------------------------------------------------------- dependencies (cached until uv.lock changes)
FROM python:${PYTHON}-slim-bookworm AS build
COPY --from=ghcr.io/astral-sh/uv:0.12.19 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-dev --no-install-project
COPY pyproject.toml uv.lock README.md ./
COPY tkb ./tkb
# (reinstalled every time: uv would otherwise reuse a cached build of an unchanged version number)
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-editable --reinstall-package transcript-kb

# ---------------------------------------------------------------- Claude Code (optional)
FROM debian:bookworm-slim AS claude
ARG CLAUDE_CODE=true
ARG CLAUDE_CODE_VERSION=2.1.292
RUN if [ "$CLAUDE_CODE" = "true" ]; then \
      apt-get update && apt-get install -y --no-install-recommends ca-certificates curl && \
      curl -fsSL https://claude.ai/install.sh | bash -s "$CLAUDE_CODE_VERSION" && \
      cp -L /root/.local/bin/claude /claude; \
    else touch /claude; fi

# ---------------------------------------------------------------- the image
FROM python:${PYTHON}-slim-bookworm
# Static ffmpeg + ffprobe for yt-dlp's audio extraction (half the size of Debian's ffmpeg and its libraries),
# and a JavaScript runtime, which yt-dlp needs for YouTube.
COPY --from=mwader/static-ffmpeg:7.1 /ffmpeg /ffprobe /usr/local/bin/
COPY --from=denoland/deno:bin-2.5.6 /deno /usr/local/bin/deno
COPY --from=claude /claude /usr/local/bin/claude
RUN [ -s /usr/local/bin/claude ] || rm /usr/local/bin/claude; \
    useradd --uid 1000 --create-home tkb && mkdir -p /library /cache && chown tkb:tkb /library /cache
COPY --from=build --chown=tkb:tkb /app/.venv /app/.venv

# The library (videos, notes, settings, keys) and the model caches live in volumes; locks stay in the container.
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1 \
    TKB_HOME=/library TKB_LOCKS=/tmp/tkb-locks HOME=/cache HF_HOME=/cache/huggingface
VOLUME ["/library", "/cache"]
USER tkb
WORKDIR /library
ENV TKB_PORT=8765
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s CMD ["python", "-c", "import os, urllib.request; \
urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"TKB_PORT\"]}/api/jobs', timeout=4)"]
# Inside the container it listens on every interface; publish it on the host's 127.0.0.1 only (compose does).
CMD ["tkb", "serve", "--host", "0.0.0.0", "--no-open"]
