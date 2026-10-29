#!/bin/zsh
# Double-click to open the Transcript KB web app. Close this window to stop it.
cd "$(dirname "$0")"
exec uv run tkb serve
