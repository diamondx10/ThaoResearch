"""Resolve resource paths for both dev and PyInstaller frozen modes."""
from __future__ import annotations

import sys
from pathlib import Path


def get_resource_path(rel_path: str) -> Path:
    """Return absolute path to a bundled resource.

    - When running from source: relative to project root (cwd).
    - When running as PyInstaller bundle (sys.frozen): inside sys._MEIPASS.
    """
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        base = Path(sys._MEIPASS)
    else:
        base = Path(__file__).resolve().parent.parent
    return base / rel_path