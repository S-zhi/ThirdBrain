#!/usr/bin/env bash
set -euo pipefail

generation_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$generation_dir"

uv run python -u run.py \
  --docs_dir ./md \
  --count "${COUNT:-300}" \
  --workers "${WORKERS:-10}"
