#!/bin/sh
# macOS build -> dist/DW to TIDAL.app
set -e
cd "$(dirname "$0")"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt -r requirements-dev.txt
.venv/bin/pyinstaller --noconfirm --clean dw2tidal.spec
echo "Built: dist/DW to TIDAL.app"
