# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for "DW to TIDAL".
#   macOS:   pyinstaller dw2tidal.spec   ->  dist/DW to TIDAL.app
#   Windows: pyinstaller dw2tidal.spec   ->  dist/DW to TIDAL.exe
# Build on the platform you are targeting (PyInstaller does not cross-compile).
import sys
from PyInstaller.utils.hooks import collect_submodules, collect_data_files

APP_NAME = "DW to TIDAL"

a = Analysis(
    ["dw2tidal_app.py"],
    pathex=[],
    binaries=[],
    datas=collect_data_files("tidalapi") + collect_data_files("certifi"),
    hiddenimports=collect_submodules("tidalapi") + ["isodate", "mpegdash", "ratelimit", "dateutil"],
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)

if sys.platform == "darwin":
    exe = EXE(
        pyz, a.scripts, [],
        exclude_binaries=True,
        name=APP_NAME,
        console=False,           # no Terminal window; the browser tab is the UI
        upx=False,
        target_arch=None,        # native arch of the build machine
    )
    coll = COLLECT(exe, a.binaries, a.datas, name=APP_NAME, upx=False)
    app = BUNDLE(
        coll,
        name=f"{APP_NAME}.app",
        bundle_identifier="local.dw2tidal",
        info_plist={
            "CFBundleDisplayName": APP_NAME,
            "CFBundleShortVersionString": "1.0.0",
            "LSMinimumSystemVersion": "12.0",
            "NSHighResolutionCapable": True,
            "LSUIElement": False,
        },
    )
else:
    exe = EXE(
        pyz, a.scripts, a.binaries, a.datas, [],
        name=APP_NAME,
        console=False,           # no console window on Windows
        onefile=True,
        upx=False,
    )
