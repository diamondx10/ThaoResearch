"""PyInstaller build script for ThaoResearch.

Run:    python build_exe.py
Output: dist/ThaoResearch.exe (Windows) or dist/ThaoResearch (Linux/macOS)
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ASSETS = ROOT / "assets"


def main():
    try:
        import PyInstaller.__main__ as pyi_main  # type: ignore
    except ImportError:
        print("PyInstaller is not installed. Run: pip install pyinstaller")
        sys.exit(1)

    args = [
        "--noconfirm",
        "--onefile",
        "--windowed",
        "--name", "ThaoResearch",
        "--paths", str(ROOT),
        "--add-data", f"{ROOT / 'locales'}{';' if sys.platform == 'win32' else ':'}locales",
        "--add-data", f"{ROOT / 'config.yaml'}{';' if sys.platform == 'win32' else ':'}.",
    ]

    # Bundle .env if present (used as a fallback for Telegram credentials
    # in frozen builds). The user's real .env on disk always takes
    # precedence — this just gives the bundled exe a sensible default.
    env_file = ROOT / ".env"
    if env_file.exists():
        sep = ";" if sys.platform == "win32" else ":"
        args += ["--add-data", f"{env_file}{sep}."]

    # Bundle assets folder if present (icons, etc.)
    if ASSETS.exists():
        sep = ";" if sys.platform == "win32" else ":"
        args += ["--add-data", f"{ASSETS}{sep}assets"]

    # License scripts live at the project root and are imported at
    # runtime by ``utils/license.py`` (which the GUI calls when the user
    # submits or views a license key). Without these PyInstaller won't
    # include them in the .exe, so ``import license_checker`` fails and
    # the Submit / View License Info buttons silently do nothing.
    sep = ";" if sys.platform == "win32" else ":"
    for script in ("license_generator.py", "license_checker.py"):
        script_path = ROOT / script
        if script_path.exists():
            args += ["--add-data", f"{script_path}{sep}."]

    # Hidden imports for libraries that PyInstaller sometimes misses
    args += [
        "--hidden-import", "apscheduler.jobstores.memory",
        "--hidden-import", "apscheduler.executors.pool",
        "--hidden-import", "apscheduler.triggers.interval",
        "--hidden-import", "apscheduler.triggers.cron",
        "--hidden-import", "telegram.ext._application",
        # Tkinter widgets used by the GUI
        "--hidden-import", "tkinter",
        "--hidden-import", "tkinter.ttk",
        "--hidden-import", "tkinter.scrolledtext",
        "--hidden-import", "tkinter.filedialog",
        "--hidden-import", "tkinter.messagebox",
        # pystray + PIL
        "--hidden-import", "pystray",
        "--hidden-import", "PIL",
        "--hidden-import", "PIL.Image",
        "--hidden-import", "PIL.ImageDraw",
        "--hidden-import", "PIL.ImageFont",
        # Windows-specific desktop notifications
        "--hidden-import", "win11toast",
        "--hidden-import", "win10toast",
        "--hidden-import", "plyer",
        "--hidden-import", "plyer.platforms.win",
        "--hidden-import", "plyer.platforms.win.notification",
        "--hidden-import", "win32api",
        "--hidden-import", "win32con",
        "--hidden-import", "win32gui",
        "--hidden-import", "win32gui_struct",
        # Schedule trigger classes (APSJobStore / cron helpers)
        "--collect-all", "apscheduler",
        "--collect-all", "pystray",
        # tkhtmlview (used in ArticleViewer if available)
        "--collect-all", "tkhtmlview",
    ]

    if sys.platform == "win32":
        # Use the .ico if available
        ico = ASSETS / "tray.ico"
        if ico.exists():
            args += ["--icon", str(ico)]

    args.append(str(ROOT / "main.py"))

    print("Running PyInstaller with args:")
    for a in args:
        print(f"  {a}")
    pyi_main.run(args)

    print("\nBuild complete. Output:")
    out_dir = ROOT / "dist"
    if out_dir.exists():
        for f in out_dir.iterdir():
            print(f"  {f}")


if __name__ == "__main__":
    main()
