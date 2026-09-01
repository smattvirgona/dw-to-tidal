#!/bin/sh
# Builds dw-to-tidal.zip — the thing you send to friends.
set -e
cd "$(dirname "$0")"
rm -rf dist && mkdir -p "dist/DW to TIDAL"
cp dw2tidal_app.py Start.command Start.bat README.md "dist/DW to TIDAL/"
(cd dist && zip -qr dw-to-tidal.zip "DW to TIDAL")
echo "Built dist/dw-to-tidal.zip"
