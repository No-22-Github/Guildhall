#!/usr/bin/env bash
# One terminal for both services; no browser is opened.
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
for command in uv pnpm; do
  if ! command -v "$command" >/dev/null 2>&1; then
    echo "Missing dependency: $command. Install it before running scripts/dev.sh." >&2
    exit 1
  fi
done
exec uv run --project "$ROOT/backend" python "$ROOT/scripts/dev.py" "$@"
