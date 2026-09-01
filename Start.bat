@echo off
REM DW to TIDAL - Windows launcher. Double-click to run.
REM First run downloads a small Python runtime into %USERPROFILE%\.dw2tidal (no system changes).
cd /d "%~dp0"
set "DIR=%USERPROFILE%\.dw2tidal"
set "UV_INSTALL_DIR=%DIR%\bin"
set "UV_CACHE_DIR=%DIR%\uv\cache"
set "UV_PYTHON_INSTALL_DIR=%DIR%\uv\python"
set "UV_TOOL_DIR=%DIR%\uv\tools"
set "UV_NO_MODIFY_PATH=1"
set "UV_PYTHON_PREFERENCE=only-managed"
if not exist "%DIR%" mkdir "%DIR%"
set "UV=%DIR%\bin\uv.exe"
if not exist "%UV%" (
  echo First run: downloading the runtime ^(about a minute^)...
  powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex"
  if not exist "%UV%" (
    echo.
    echo Download failed. Check your internet connection and try again.
    pause
    exit /b 1
  )
)
echo Starting DW to TIDAL - keep this window open. Close it to quit.
"%UV%" run --script dw2tidal_app.py
pause
