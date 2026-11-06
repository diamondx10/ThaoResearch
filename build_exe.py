"""PyInstaller build script for ThaoResearch.

Run:    python build_exe.py
Output: dist/ThaoResearch.exe (Windows) or dist/ThaoResearch (Linux/macOS)

Bypass-license build (no license required at runtime):
    python build_exe.py --bypass-license
    → embeds BYPASS_LICENSE=1 into the frozen exe
    → runs with --bypass-license flag automatically
    → utils/license.py sees the env var and returns valid immediately
    → user just double-clicks the exe, no license needed

Outputs:
    dist/ThaoResearch.exe                   (normal build)
    dist/ThaoResearch-BypassLicense-1.0.0.exe  (bypass build)
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ASSETS = ROOT / "assets"
CREDENTIALS = ROOT / "utils" / "credentials.py"
EMBED_SCRIPT = ROOT / "scripts" / "embed_credentials.py"


def _ensure_credentials() -> bool:
    """Make sure ``utils/credentials.py`` holds real values before freezing.

    Returns True when the exe can be built with credentials baked in.

    The module is gitignored (it carries the live bot token), so a fresh
    clone will not have it. We regenerate it from ``.env`` when possible and
    return False when there is nothing to generate from, so the caller can
    warn instead of shipping a silently mute build.
    """
    import re
    import subprocess

    # Match the assignment lines only. A substring search would always hit,
    # because the generated module lists the placeholders in its own
    # ``_PLACEHOLDERS`` set.
    _ASSIGN = r'^{name}\s*=\s*"([^"]*)"'
    _PLACEHOLDERS = {"", "YOUR_BOT_TOKEN_HERE", "YOUR_CHAT_ID_HERE"}

    def _usable() -> bool:
        if not CREDENTIALS.exists():
            return False
        try:
            text = CREDENTIALS.read_text(encoding="utf-8")
        except OSError:
            return False
        for name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
            m = re.search(_ASSIGN.format(name=name), text, re.MULTILINE)
            if m is None or m.group(1).strip() in _PLACEHOLDERS:
                return False
        return True

    if _usable():
        print("[OK] utils/credentials.py present - credentials will be embedded.")
        return True

    if not EMBED_SCRIPT.exists():
        print(f"[WARN] {EMBED_SCRIPT.name} not found; cannot generate credentials.")
        return False

    if not (ROOT / ".env").exists():
        print("[WARN] No .env found - copy .env.example to .env and fill it in,")
        print("       then run: python scripts/embed_credentials.py")
        return False

    print("[..] Generating utils/credentials.py from .env ...")
    result = subprocess.run([sys.executable, str(EMBED_SCRIPT)], cwd=str(ROOT))
    if result.returncode != 0 or not _usable():
        print("[WARN] Credential generation failed.")
        return False
    return True


def _build_args(bypass_license: bool = False) -> list[str]:
    """Build the PyInstaller argument list.

    When ``bypass_license=True`` the frozen exe will:
      - Set ``BYPASS_LICENSE=1`` via --env so license checks are skipped
        automatically (utils/license.py reads this at module import time).
      - Pass ``--bypass-license`` to main.py so the CLI also skips checks.
    """
    sep = ";" if sys.platform == "win32" else ":"
    args: list[str] = [
        "--noconfirm",
        "--onefile",
        "--windowed",
        "--name", "ThaoResearch",
        "--paths", str(ROOT),
        "--add-data", f"{ROOT / 'locales'}{sep}locales",
        # NOTE: config files (config.yaml, init_config.yaml) are NOT bundled.
        # Portable builds need to create them at first run from defaults in
        # main.py. Config files live next to the exe in the data/ folder.
    ]

    # Credentials live in utils/credentials.py, generated from .env by
    # scripts/embed_credentials.py and compiled into the exe so end users do
    # not need their own .env. The module is gitignored, so a fresh clone has
    # to generate it before building -- do that here rather than shipping an
    # exe whose Telegram alerts silently never fire.
    if not _ensure_credentials():
        print("[WARN] Building WITHOUT embedded Telegram credentials.")
        print("       The exe will look for a .env next to it at runtime.")
    else:
        # The import lives inside a function body in main.load_env(), so pin it
        # explicitly instead of relying on PyInstaller's static analysis.
        args += ["--hidden-import", "utils.credentials"]

    # Bundle assets folder (icons, etc.)
    if ASSETS.exists():
        args += ["--add-data", f"{ASSETS}{sep}assets"]

    # License scripts bundled for in-app license dialog
    sep = ";" if sys.platform == "win32" else ":"
    for script in ("license_generator.py", "license_checker.py"):
        script_path = ROOT / script
        if script_path.exists():
            args += ["--add-data", f"{script_path}{sep}."]

    # sv-ttk ships its theme as .tcl + .png data files that nothing imports,
    # so PyInstaller's static analysis cannot see them. Without this the exe
    # starts but falls back to the stock ttk theme.
    args += ["--collect-data", "sv_ttk"]

    # tzdata is pure data too, and zoneinfo loads it through importlib at
    # runtime. Missing it means ZoneInfo("Asia/Ho_Chi_Minh") raises on the
    # first scheduler tick -- on Windows there is no system tz database.
    args += ["--collect-data", "tzdata"]

    # Hidden imports for libraries PyInstaller sometimes misses
    args += [
        "--hidden-import", "desktop.theme",
        "--hidden-import", "sv_ttk",
        "--hidden-import", "apscheduler.jobstores.memory",
        "--hidden-import", "apscheduler.executors.pool",
        "--hidden-import", "apscheduler.triggers.interval",
        "--hidden-import", "apscheduler.triggers.cron",
        "--hidden-import", "telegram.ext._application",
        # Tkinter widgets
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
        # Windows notifications
        "--hidden-import", "win11toast",
        "--hidden-import", "win10toast",
        "--hidden-import", "plyer",
        "--hidden-import", "plyer.platforms.win",
        "--hidden-import", "plyer.platforms.win.notification",
        "--hidden-import", "win32api",
        "--hidden-import", "win32con",
        "--hidden-import", "win32gui",
        "--hidden-import", "win32gui_struct",
        # Collect all submodules
        "--collect-all", "apscheduler",
        "--collect-all", "pystray",
        "--collect-all", "tkhtmlview",
    ]

    # Icon on Windows
    if sys.platform == "win32":
        ico = ASSETS / "tray.ico"
        if ico.exists():
            args += ["--icon", str(ico)]

    # Runtime hook: runs before main.py is loaded, so BYPASS_LICENSE is
    # set before utils/license.py evaluates _BYPASS_LICENSE at import time.
    if bypass_license:
        runtime_hook = str(ROOT / "runtime_hook.py")
        if Path(runtime_hook).exists():
            args += ["--runtime-hook", runtime_hook]

    # Entry point. Both normal and bypass builds use the same entry.
    args.append(str(ROOT / "main.py"))
    return args


def main():
    parser = argparse.ArgumentParser(
        description="Build ThaoResearch executable with PyInstaller.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  python build_exe.py                    # normal build (license required)
  python build_exe.py --bypass-license  # pre-activated build (no license needed)
""",
    )
    parser.add_argument(
        "--bypass-license",
        action="store_true",
        help="Build a pre-activated exe. "
        "The built exe starts without any license key or file. "
        "Output: dist/ThaoResearch-BypassLicense-1.0.0.exe",
    )
    bypass = parser.parse_args().bypass_license

    args = _build_args(bypass_license=bypass)

    if bypass:
        print(
            "\n=== BYPASS-LICENSE BUILD ===\n"
            "  - BYPASS_LICENSE=1 is embedded via runtime hook.\n"
            "  - No license key or file is required at runtime.\n"
            "  - Output: dist/ThaoResearch-BypassLicense-1.0.0.exe\n"
        )

    print("Running PyInstaller with args:")
    for a in args:
        print(f"  {a}")
    print()

    try:
        import PyInstaller.__main__ as pyi_main  # type: ignore
    except ImportError:
        print("PyInstaller is not installed. Run: pip install pyinstaller")
        sys.exit(1)

    pyi_main.run(args)

    out_dir = ROOT / "dist"
    if not out_dir.exists():
        print("\nBuild failed — no output directory.")
        sys.exit(1)

    normal_exe = out_dir / "ThaoResearch.exe"
    if not normal_exe.exists():
        print(f"\nBuild failed — {normal_exe} not found.")
        sys.exit(1)

    # Rename bypass build so it coexists with the normal build.
    if bypass:
        bypass_exe = out_dir / "ThaoResearch-BypassLicense-1.0.0.exe"
        shutil.move(str(normal_exe), str(bypass_exe))
        exe_path = bypass_exe
        print(f"\nBuild complete: {exe_path}")
    else:
        exe_path = normal_exe
        print(f"\nBuild complete: {exe_path}")

    size_mb = exe_path.stat().st_size / (1024 * 1024)
    print(f"  Size: {size_mb:.1f} MB")


if __name__ == "__main__":
    main()
