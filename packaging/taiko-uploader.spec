# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for the standalone Windows uploader binary.
#
# Build:  pyinstaller packaging/taiko-uploader.spec
# Output: dist/taiko-uploader.exe (onefile, no console window)
#
# We exclude the entire server-side + analysis surface of the taiko_trainer
# package because the uploader only needs uploader.py + uploader_gui.py.
# Without excludes the binary would drag in FastAPI, uvicorn, osrparse, etc
# — bloats the .exe to 200 MB+ for zero benefit. With them it's ~50-70 MB.

from PyInstaller.utils.hooks import collect_submodules


hiddenimports = [
    # pystray picks its backend at import time — force the Windows one.
    "pystray._win32",
    # Pillow's tkinter integration is dynamic; PyInstaller doesn't always see it.
    "PIL._tkinter_finder",
]


excludes = [
    # Server + heavy dependencies the uploader never touches
    "fastapi", "starlette", "uvicorn", "uvicorn.protocols",
    "python_multipart", "multipart",
    "osrparse",
    "itsdangerous",
    # Every taiko_trainer submodule except the two we actually need
    "taiko_trainer.server",
    "taiko_trainer.workflow",
    "taiko_trainer.osu_parser",
    "taiko_trainer.osr_parser",
    "taiko_trainer.features",
    "taiko_trainer.scoring",
    "taiko_trainer.report",
    "taiko_trainer.player",
    "taiko_trainer.judgment",
    "taiko_trainer.classification",
    "taiko_trainer.suggest",
    "taiko_trainer.sessions",
    "taiko_trainer.migrate",
    "taiko_trainer.ingest",
    "taiko_trainer.parity",
    "taiko_trainer.kddk_patterns",
    "taiko_trainer.cheese",
    "taiko_trainer.db",
    "taiko_trainer.mods",
    "taiko_trainer.osu_api",
    "taiko_trainer.smoke",
    "taiko_trainer.validate",
    "taiko_trainer.analyze",
    "taiko_trainer.models",
    "taiko_trainer.config",
    "taiko_trainer.auth",
    # Added post-initial-spec:
    "taiko_trainer.cleanup",
    "taiko_trainer.scan_lazer",
]


a = Analysis(
    ["launcher.py"],
    pathex=["../src"],
    binaries=[],
    datas=[],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="taiko-uploader",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,          # UPX shrinks the exe if the ubuntu/windows runner has it
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,     # no cmd window — GUI-only
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,         # can add a .ico later if we want
)
