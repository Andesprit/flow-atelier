#!/usr/bin/env bash
set -euo pipefail

cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
uv run --locked --no-dev --group docs python scripts/docs/generate_reference.py docs/reference
exec uv run --locked --no-dev --group docs zensical build --config-file zensical.toml "$@"
