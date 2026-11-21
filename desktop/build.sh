#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."
test "$(uname -sm)" = "Darwin arm64"
# The CLI and wheel already ship the production dashboard in flow_atelier/dist.
uv run --locked pyinstaller scripts/atelier_entry.py --onedir --name atelier --noconfirm \
  --distpath desktop/build --workpath desktop/build/work --specpath desktop/build \
  --copy-metadata flow-atelier --collect-data flow_atelier --collect-all uvicorn \
  --collect-all apscheduler --collect-all tzdata
cd desktop
cargo tauri build --bundles app -- --locked
app="target/release/bundle/macos/Flow Atelier.app"
# Preserve Python.framework symlinks and the loader's adjacent _internal directory.
ditto build/atelier "$app/Contents/Resources/atelier"
codesign --force --sign - "$app"
