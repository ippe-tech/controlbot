# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller: Helper24Sync.exe (Windows, one-file, без окна консоли).

Собирать на Windows:  pyinstaller --noconfirm --clean build\\helper24_sync.spec
Результат:            dist\\Helper24Sync.exe
"""

from pathlib import Path

ROOT = Path(SPECPATH).resolve().parent

a = Analysis(
    [str(ROOT / "personal" / "helper24_sync.py")],
    pathex=[str(ROOT / "personal")],
    binaries=[],
    datas=[],
    hiddenimports=[
        "cryptography",
        "cryptography.fernet",
        "cryptography.hazmat.primitives.ciphers",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "numpy", "pandas"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="Helper24Sync",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
