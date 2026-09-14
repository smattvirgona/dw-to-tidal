#!/bin/bash
# DW to TIDAL — macOS launcher. Double-click to open. The app then runs hidden in the background.
# First run downloads a small Python runtime into ~/.dw2tidal (no system changes).
cd "$(dirname "$0")"
DIR="$HOME/.dw2tidal"
export UV_INSTALL_DIR="$DIR/bin" UV_CACHE_DIR="$DIR/uv/cache" UV_PYTHON_INSTALL_DIR="$DIR/uv/python" UV_TOOL_DIR="$DIR/uv/tools" UV_NO_MODIFY_PATH=1 UV_PYTHON_PREFERENCE=only-managed
mkdir -p "$DIR"
UV="$DIR/bin/uv"
export DW2TIDAL_UV="$UV"
if [ ! -x "$UV" ]; then
  echo "First run: downloading the runtime (about a minute)…"
  curl -LsSf https://astral.sh/uv/install.sh | sh || { echo; echo "Download failed. Check your internet connection and try again."; read -r -p "Press Enter to close."; exit 1; }
fi
echo "Opening DW to TIDAL…"
if ! "$UV" run --script dw2tidal_app.py --show; then
  echo; read -r -p "Something went wrong (see above). Press Enter to close."; exit 1
fi
# Close this Terminal window; the app keeps running in the background.
TTY="$(tty)"
nohup osascript -e "delay 0.3" -e "tell application \"Terminal\" to close (every window whose tty of selected tab is \"$TTY\")" >/dev/null 2>&1 &
disown
exit 0
