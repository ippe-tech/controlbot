# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller: ControlBot.exe (Windows, one-file).

Собирать на Windows:  pyinstaller --noconfirm --clean build\\controlbot.spec
Результат:            dist\\ControlBot.exe
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_all

ROOT = Path(SPECPATH).resolve().parent          # ...\projects\controlbot
SRC = ROOT / "src"

telegram_datas, telegram_binaries, telegram_hidden = collect_all("telegram")

hidden = list(telegram_hidden) + [
    "telegram.ext.job_queue",
    "apscheduler",
    "apscheduler.schedulers.asyncio",
    "apscheduler.triggers.cron",
    "apscheduler.triggers.interval",
    "psutil",
    "PIL",
    "PIL.ImageGrab",
    "PIL.Image",
    "pyperclip",
    "activity_tracker",
    "browser_sites",
    "setup_gui",
    "access_store",
    "tkinter",
    "tkinter.ttk",
    "tkinter.messagebox",
]

a = Analysis(
    [str(SRC / "bot.py")],
    pathex=[str(SRC)],
    binaries=telegram_binaries,
    datas=telegram_datas,
    hiddenimports=hidden,
    hookspath=[],
    runtime_hooks=[],
    excludes=["matplotlib", "numpy", "cv2", "scipy", "pandas"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="ControlBot",
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
