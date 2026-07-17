#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [[ ! -x .venv/bin/python ]]; then
  python3 -m venv .venv
fi

if ! .venv/bin/python -c "import chess" 2>/dev/null; then
  .venv/bin/pip install -r requirements.txt
fi

exec .venv/bin/python app.py
