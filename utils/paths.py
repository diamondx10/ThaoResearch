"""Centralized filesystem paths for ThaoResearch.

Why this exists
---------------
Two different directories get confused in a frozen app, and conflating them
is what made the exe run on the build machine and die everywhere else:

``APP_ROOT``
    Where the executable (or entry script) lives. Read-only in the general
    case: the user may have dropped the portable folder into
    ``C:\\Program Files``, a network share, or a read-only USB stick.
    Bundled assets (``assets/``, ``locales/``) are found relative to this,
    or inside PyInstaller's ``_MEIPASS``.

``DATA_DIR``
    Where everything *mutable* goes: ``config.yaml``, ``init_config.yaml``,
    ``history.db``, ``prices.db``, ``license.dat`` and ``logs/``. This must
    be writable, so it is **resolved at startup with an actual write probe**
    rather than assumed to be ``APP_ROOT/data``.

The old code hardcoded ``DATA_DIR = APP_ROOT / "data"``. When the exe sat
anywhere non-writable, ``logger.setup_logger()`` raised
``PermissionError: [WinError 5]`` before any window existed — and because
release builds are ``--windowed`` (no console), the user saw absolutely
nothing happen on double-click.

Data directory resolution order
-------------------------------
1. An explicit override from ``--data-dir`` or ``$THAORESEARCH_DATA_DIR``
   (applied by ``set_data_dir()`` before anything else imports us).
2. ``APP_ROOT/datadir.txt`` — a one-line pointer file that can ship inside a
   portable folder. Read-only is fine; we only read it.
3. ``<LocalAppData>/ThaoResearch/datadir.txt`` — what the GUI folder picker
   writes, so the choice survives reinstalls and exe moves.
4. ``APP_ROOT/data`` when it passes the write probe — the true portable case
   (own folder, USB stick), everything in one place.
5. ``<LocalAppData>/ThaoResearch/data`` — the fallback that makes an install
   under ``Program Files`` work.
6. ``<Temp>/ThaoResearch/data`` — last resort, so the app still starts and
   can tell the user what went wrong instead of crashing.

Every candidate is probed by creating the directory and writing, reading and
deleting a small file. Nothing here raises on import: a totally unwritable
machine still yields a usable ``DATA_DIR`` value and sets
``DATA_DIR_FALLBACK``, which the UI surfaces.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

__all__ = [
    "APP_ROOT",
    "DATA_DIR",
    "LOG_DIR",
    "ASSETS_DIR",
    "LOCALES_DIR",
    "CONFIG_FILE",
    "INIT_CONFIG_FILE",
    "POINTER_FILENAME",
    "DATA_DIR_SOURCE",
    "DATA_DIR_FALLBACK",
    "resolve_user_path",
    "set_data_dir",
    "save_data_dir_choice",
    "clear_data_dir_choice",
    "is_writable",
    "candidate_data_dirs",
    "default_portable_dir",
    "local_appdata_dir",
    "describe_paths",
]

POINTER_FILENAME = "datadir.txt"
APP_FOLDER_NAME = "ThaoResearch"

# Env var honoured before any config is read, so a launcher or a test can
# redirect all mutable state without touching the command line.
ENV_DATA_DIR = "THAORESEARCH_DATA_DIR"


# =============================================================================
# APP_ROOT — where the program itself lives (may be read-only)
# =============================================================================
def _detect_app_root() -> Path:
    """Return the directory containing the executable or entry script."""
    # 1. Frozen exe -> the folder the user put it in.
    if getattr(sys, "frozen", False):
        try:
            return Path(sys.executable).resolve().parent
        except Exception:
            pass

    # 2. python main.py -> the script's directory, NOT the working directory
    #    (the user can cd anywhere; the install folder cannot move).
    try:
        if sys.argv and sys.argv[0]:
            argv0 = Path(sys.argv[0])
            if not argv0.is_absolute():
                argv0 = (Path(os.getcwd()) / argv0).resolve()
            if argv0.name.lower() in ("python.exe", "pythonw.exe", "python", "pythonw"):
                for a in sys.argv[1:]:
                    if a.lower().endswith(".py"):
                        argv0 = (Path(os.getcwd()) / a).resolve()
                        break
            if argv0.suffix.lower() == ".py" and argv0.exists():
                return argv0.resolve().parent
    except Exception:
        pass

    try:
        main_mod = sys.modules.get("__main__")
        if main_mod is not None and getattr(main_mod, "__file__", None):
            return Path(main_mod.__file__).resolve().parent
    except Exception:
        pass

    return Path(os.getcwd())


APP_ROOT: Path = _detect_app_root()
"""Directory holding the exe / entry script. Treat as possibly read-only."""

ASSETS_DIR: Path = APP_ROOT / "assets"
"""Icons and images shipped with the app (read-only)."""

LOCALES_DIR: Path = APP_ROOT / "locales"
"""Translation .json files (read-only)."""


# =============================================================================
# Write probe
# =============================================================================
def is_writable(path: Path) -> bool:
    """Return True when we can create ``path`` and write a file inside it.

    ``os.access(..., os.W_OK)`` is not enough on Windows: it ignores ACLs and
    reports success on directories that then refuse the write. The only
    reliable probe is to actually create, write, read back and delete.
    """
    try:
        path.mkdir(parents=True, exist_ok=True)
    except Exception:
        return False

    probe = path / ".write_probe"
    try:
        probe.write_text("ok", encoding="utf-8")
        if probe.read_text(encoding="utf-8") != "ok":
            return False
        return True
    except Exception:
        return False
    finally:
        try:
            probe.unlink()
        except Exception:
            pass


# =============================================================================
# Candidate locations
# =============================================================================
def local_appdata_dir() -> Path:
    """Return ``<LocalAppData>/ThaoResearch`` (or a sane non-Windows stand-in)."""
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        # Non-Windows, or a stripped environment.
        home = Path.home()
        base = str(home / ".local" / "share") if os.name != "nt" else str(home)
    return Path(base) / APP_FOLDER_NAME


def default_portable_dir() -> Path:
    """Return ``APP_ROOT/data`` — the preferred spot for a portable install."""
    return APP_ROOT / "data"


def _temp_dir() -> Path:
    return Path(tempfile.gettempdir()) / APP_FOLDER_NAME / "data"


def _read_pointer(pointer: Path) -> Path | None:
    """Read a one-line pointer file. Returns None when absent or unusable.

    Decoded as ``utf-8-sig``: Notepad and PowerShell's ``Set-Content -Encoding
    utf8`` both prepend a UTF-8 BOM, and with plain ``utf-8`` the BOM lands on
    the first character. ``"﻿# comment".startswith("#")`` is False, so the
    comment line was being handed back as the user's data folder.

    Validated before returning: a path with characters Windows forbids is
    treated as no pointer at all rather than passed on to mkdir().
    """
    try:
        if not pointer.is_file():
            return None
        text = pointer.read_text(encoding="utf-8-sig")
    except Exception:
        return None

    for raw in text.splitlines():
        line = raw.strip().lstrip("﻿").strip()
        if not line or line.startswith("#"):
            continue
        try:
            candidate = Path(os.path.expandvars(line)).expanduser()
        except Exception:
            return None
        # Reject the characters Windows will not accept in a path. The drive
        # colon is legitimate, so only look past it.
        tail = str(candidate)[2:] if len(str(candidate)) > 2 else str(candidate)
        if any(ch in tail for ch in '<>"|?*') or ":" in tail:
            return None
        return candidate
    return None


def candidate_data_dirs() -> list[tuple[str, Path]]:
    """Return ``(source_label, path)`` candidates in priority order."""
    out: list[tuple[str, Path]] = []

    env = os.environ.get(ENV_DATA_DIR, "").strip()
    if env:
        out.append(("env", Path(os.path.expandvars(env)).expanduser()))

    portable_pointer = _read_pointer(APP_ROOT / POINTER_FILENAME)
    if portable_pointer:
        out.append(("pointer:portable", portable_pointer))

    user_pointer = _read_pointer(local_appdata_dir() / POINTER_FILENAME)
    if user_pointer:
        out.append(("pointer:user", user_pointer))

    out.append(("portable", default_portable_dir()))
    out.append(("localappdata", local_appdata_dir() / "data"))
    out.append(("temp", _temp_dir()))
    return out


def _resolve_data_dir() -> tuple[Path, str, bool]:
    """Pick the first writable candidate.

    Returns ``(path, source_label, used_fallback)``. ``used_fallback`` is True
    when an explicitly requested location (env var or pointer file) failed its
    probe, so the UI can tell the user their choice is not being honoured.
    """
    explicit_failed = False
    for source, path in candidate_data_dirs():
        if is_writable(path):
            return path, source, explicit_failed
        if source in ("env", "pointer:portable", "pointer:user"):
            explicit_failed = True

    # Nothing is writable. Return the temp candidate anyway so imports do not
    # explode; main() reports the condition through its startup error path.
    return _temp_dir(), "unwritable", True


_override: Path | None = None

DATA_DIR, DATA_DIR_SOURCE, DATA_DIR_FALLBACK = _resolve_data_dir()
"""Writable home for config, databases, license blob and logs."""

LOG_DIR: Path = DATA_DIR / "logs"
"""Rotating log files. Inside DATA_DIR so one chosen folder holds everything."""

CONFIG_FILE: Path = DATA_DIR / "config.yaml"
"""User configuration — the shipped defaults are written here on first run."""

INIT_CONFIG_FILE: Path = DATA_DIR / "init_config.yaml"
"""GUI autosave target, co-located with config.yaml."""


def set_data_dir(path: str | os.PathLike[str] | None) -> Path:
    """Override the data directory for this process and refresh the module.

    Called by ``main()`` for ``--data-dir`` before any database, license or
    logger import has touched the old value. Returns the active ``DATA_DIR``;
    an unwritable override is ignored in favour of the normal chain, and
    ``DATA_DIR_FALLBACK`` is set so the UI can say so.
    """
    global _override, DATA_DIR, LOG_DIR, CONFIG_FILE, INIT_CONFIG_FILE
    global DATA_DIR_SOURCE, DATA_DIR_FALLBACK

    if path:
        candidate = Path(os.path.expandvars(str(path))).expanduser()
        if is_writable(candidate):
            _override = candidate
            DATA_DIR, DATA_DIR_SOURCE, DATA_DIR_FALLBACK = candidate, "cli", False
        else:
            DATA_DIR, DATA_DIR_SOURCE, DATA_DIR_FALLBACK = _resolve_data_dir()
            DATA_DIR_FALLBACK = True
    else:
        DATA_DIR, DATA_DIR_SOURCE, DATA_DIR_FALLBACK = _resolve_data_dir()

    LOG_DIR = DATA_DIR / "logs"
    CONFIG_FILE = DATA_DIR / "config.yaml"
    INIT_CONFIG_FILE = DATA_DIR / "init_config.yaml"
    return DATA_DIR


def save_data_dir_choice(path: str | os.PathLike[str]) -> tuple[bool, str]:
    """Persist the user's folder choice so the next launch uses it.

    Writes the pointer next to the exe when that folder is writable (keeps a
    portable install self-contained on a USB stick), otherwise under
    ``<LocalAppData>/ThaoResearch``. Returns ``(ok, message)``.
    """
    candidate = Path(os.path.expandvars(str(path))).expanduser()
    if not is_writable(candidate):
        return False, f"Cannot write to {candidate}"

    body = (
        "# ThaoResearch data folder. One path per file; lines starting with # are ignored.\n"
        "# Delete this file to go back to the default location.\n"
        f"{candidate}\n"
    )

    for target_dir in (APP_ROOT, local_appdata_dir()):
        try:
            if not is_writable(target_dir):
                continue
            (target_dir / POINTER_FILENAME).write_text(body, encoding="utf-8")
            return True, str(target_dir / POINTER_FILENAME)
        except Exception:
            continue

    return False, "No writable location found to store the choice"


def clear_data_dir_choice() -> list[Path]:
    """Delete every pointer file. Returns the paths actually removed."""
    removed: list[Path] = []
    for target_dir in (APP_ROOT, local_appdata_dir()):
        pointer = target_dir / POINTER_FILENAME
        try:
            if pointer.is_file():
                pointer.unlink()
                removed.append(pointer)
        except Exception:
            pass
    return removed


# =============================================================================
# Read-only resource lookup
# =============================================================================
def resolve_user_path(user_path: str | os.PathLike[str]) -> Path:
    """Resolve a relative path against the places a bundled file can live.

    Order: PyInstaller ``_MEIPASS`` (onefile bundle), the exe's own folder,
    the working directory, then ``APP_ROOT``. Absolute paths pass through.
    Falls back to ``APP_ROOT/<path>`` when nothing exists, so callers can
    still use the result as a write target.
    """
    user_path = Path(user_path)
    if user_path.is_absolute():
        return user_path

    candidates: list[Path] = []

    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / user_path)

    if getattr(sys, "frozen", False):
        try:
            candidates.append(Path(sys.executable).resolve().parent / user_path)
        except Exception:
            pass

    try:
        candidates.append(Path(os.getcwd()) / user_path)
    except Exception:
        pass

    candidates.append(APP_ROOT / user_path)

    for c in candidates:
        try:
            if c.exists():
                return c
        except OSError:
            continue
    return APP_ROOT / user_path


def describe_paths() -> str:
    """Human-readable path report for the About tab, logs and bug reports."""
    lines = [
        f"APP_ROOT        : {APP_ROOT}",
        f"  writable      : {is_writable(APP_ROOT)}",
        f"DATA_DIR        : {DATA_DIR}",
        f"  source        : {DATA_DIR_SOURCE}",
        f"  fallback used : {DATA_DIR_FALLBACK}",
        f"LOG_DIR         : {LOG_DIR}",
        f"CONFIG_FILE     : {CONFIG_FILE}",
        f"frozen          : {bool(getattr(sys, 'frozen', False))}",
    ]
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        lines.append(f"_MEIPASS        : {meipass}")
    return "\n".join(lines)
