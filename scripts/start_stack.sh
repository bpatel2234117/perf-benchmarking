#!/usr/bin/env bash
# Start tracker + workflow runner + Streamlit (dashboard foreground by default).
# See scripts/start_stack.py --help

set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
exec python3 scripts/start_stack.py "$@"
