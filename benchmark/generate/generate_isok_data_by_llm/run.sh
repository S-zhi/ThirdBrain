#!/usr/bin/env bash
set -euo pipefail

generation_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$generation_dir"

uv run python -u run.py --workers "${WORKERS:-7}" "$@"
