#!/bin/bash
# DW to TIDAL — macOS launcher. Double-click to run.
# First run downloads a small Python runtime into ~/.dw2tidal (no system changes).
cd "$(dirname "$0")"
DIR="$HOME/.dw2tidal"
export UV_INSTALL_DIR="$DIR/bin" UV_CACHE_DIR="$DIR/uv/cache" UV_PYTHON_INSTALL_DIR="$DIR/uv/python" UV_TOOL_DIR="$DIR/uv/tools" UV_NO_MODIFY_PATH=1 UV_PYTHON_PREFERENCE=only-managed
mkdir -p "$DIR"
UV="$DIR/bin/uv"
if [ ! -x "$UV" ]; then
  echo "First run: downloading the runtime (about a minute)…"
  curl -LsSf https://astral.sh/uv/install.sh | sh || { echo; echo "Download failed. Check your internet connection and try again."; read -r -p "Press Enter to close."; exit 1; }
fi
echo "Starting DW to TIDAL — keep this window open. Close it to quit."
exec "$UV" run --script dw2tidal_app.py
