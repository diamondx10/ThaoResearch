"""Centralized filesystem paths for ThaoResearch.

Why this exists
---------------
When the application is shipped as a **portable install** (one folder, exe
+ data + config together), the user can place the folder anywhere (e.g.
``C:\\Program Files\\ThaoResearch``, a USB stick, or the Desktop). Every
data file (``data/history.db``, ``data/prices.db``, ``data/license.dat``,
``.env``, ``assets/``, ``locales/``) must live next to the executable.

Resolution order for any user-facing path (``data/foo.db``, ``.env``...):

1. The directory containing the running executable
   (``Path(sys.executable).resolve().parent``).
2. The current working directory (legacy behavior — keeps ``python main.py``
   working from the project root).
3. The PyInstaller ``_MEIPASS`` directory for bundled read-only assets.

This module deliberately does NOT touch ``%APPDATA%`` — ThaoResearch is a
single-folder portable app, not a Windows-Store-style sandboxed one.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

__all__ = ["APP_ROOT", "DATA_DIR", "ASSETS_DIR", "LOCALES_DIR",
           "CONFIG_FILE", "INIT_CONFIG_FILE", "resolve_user_path"]


def _detect_app_root() -> Path:
    """Return the directory that should host ``data/`` and friends.

    Preference:
    - If running frozen (PyInstaller bundle): directory of ``sys.executable``.
    - Otherwise: directory of the entry script (``sys.argv[0]``), so the
      install/portable folder is identified by where the .py/.exe lives —
      *not* by the current working directory (which the user can change).
    - Last resort: working directory.
    """
    # 1. Frozen EXE -> live next to the EXE so the install folder is portable.
    if getattr(sys, "frozen", False):
        try:
            return Path(sys.executable).resolve().parent
        except Exception:
            pass

    # 2. python main.py / python -m main -> directory of the entry script
    #    (sys.argv[0] is the script the user invoked; resolve it relative
    #    to CWD if needed).
    try:
        if sys.argv and sys.argv[0]:
            argv0 = Path(sys.argv[0])
            if not argv0.is_absolute():
                argv0 = (Path(os.getcwd()) / argv0).resolve()
            # Skip python.exe / pythonw.exe itself — we want the *script*.
            if argv0.name.lower() in ("python.exe", "pythonw.exe", "python", "pythonw"):
                # Look for the next arg that ends in .py
                for a in sys.argv[1:]:
                    if a.lower().endswith(".py"):
                        argv0 = (Path(os.getcwd()) / a).resolve()
                        break
            if argv0.suffix.lower() == ".py" and argv0.exists():
                return argv0.resolve().parent
    except Exception:
        pass

    try:
        main_file = sys.modules.get("__main__")
        if main_file is not None and getattr(main_file, "__file__", None):
            return Path(main_file.__file__).resolve().parent
    except Exception:
        pass

    # 3. Last resort: working directory.
    return Path(os.getcwd())


APP_ROOT: Path = _detect_app_root()
"""Root directory for all mutable + bundled files (next to the EXE)."""

DATA_DIR: Path = APP_ROOT / "data"
"""SQLite DBs and license blob live here. Created on demand."""

ASSETS_DIR: Path = APP_ROOT / "assets"
"""Icons, fonts, images shipped with the app."""

LOCALES_DIR: Path = APP_ROOT / "locales"
"""Translation .json files."""

CONFIG_FILE: Path = DATA_DIR / "config.yaml"
"""User configuration file - primary location (data folder)."""

INIT_CONFIG_FILE: Path = DATA_DIR / "init_config.yaml"
"""Autosaved configuration file - same location as config.yaml for portability."""


def resolve_user_path(user_path: str | os.PathLike[str]) -> Path:
    """Resolve a user-supplied (or relative) path against the app root.

    Search order:
    - For frozen (portable) apps: EXE location FIRST, then CWD
    - For dev runs: CWD first, then APP_ROOT

    This ensures portable builds always find files next to the exe,
    even when launched from a different working directory.
    """
    user_path = Path(user_path)

    # 1. Explicit absolute paths are returned as-is.
    if user_path.is_absolute():
        return user_path

    candidates: list[Path] = []

    # For frozen apps: check EXE location FIRST (portable priority)
    if getattr(sys, "frozen", False):
        try:
            exe_dir = Path(sys.executable).resolve().parent
            candidates.append(exe_dir / user_path)
        except Exception:
            pass

    # 2. CWD-relative (works in dev, fallback for portable)
    try:
        candidates.append(Path(os.getcwd()) / user_path)
    except Exception:
        pass

    # 3. APP_ROOT (next to exe, or project root in dev)
    try:
        candidates.append(APP_ROOT / user_path)
    except Exception:
        pass

    # 4. PyInstaller's _MEIPASS for bundled read-only assets
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / user_path)

    for c in candidates:
        try:
            if c.exists():
                return c
        except OSError:
            continue
    # Default: relative to APP_ROOT
    return APP_ROOT / user_path