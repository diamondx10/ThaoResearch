"""Resolve resource paths for both dev and PyInstaller frozen modes."""
from __future__ import annotations

import sys
from pathlib import Path

from utils.paths import APP_ROOT


def get_resource_path(rel_path: str) -> Path:
    """Return absolute path to a bundled resource.

    Resolution order:
      1. PyInstaller ``_MEIPASS/<rel>`` (onefile bundle temp dir).
      2. Directory of ``sys.executable`` (frozen onedir + portable install).
      3. APP_ROOT (project root in dev mode).
    """
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        base = Path(sys._MEIPASS)
        cand = base / rel_path
        if cand.exists():
            return cand
        # Fall through to the on-disk neighbours of the EXE (onedir / portable)
        exe_dir = Path(sys.executable).resolve().parent
        cand = exe_dir / rel_path
        if cand.exists():
            return cand
        return cand  # not found, let caller decide
    return APP_ROOT / rel_path