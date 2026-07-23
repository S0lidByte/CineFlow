#!/bin/sh
# Keep uv.lock aligned with pyproject.toml whenever either is staged.
set -e
if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required to sync the lockfile. Install: https://docs.astral.sh/uv/" >&2
  exit 1
fi
uv lock
git add uv.lock
