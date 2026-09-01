@echo off
REM Windows build -> dist\DW to TIDAL.exe
cd /d "%~dp0"
if not exist .venv py -3 -m venv .venv
.venv\Scripts\pip install -q -r requirements.txt -r requirements-dev.txt
.venv\Scripts\pyinstaller --noconfirm --clean dw2tidal.spec
echo Built: dist\DW to TIDAL.exe
