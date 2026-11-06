"""Internationalization (i18n) — loads locale JSON files and provides t() helper."""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
from pathlib import Path
from typing import Dict, Optional

from utils.paths import APP_ROOT

logger = logging.getLogger("thaoresearch.utils.i18n")

DEFAULT_LANG = "en"
SUPPORTED_LANGS = ("en", "vi", "ko")

_cache: Dict[str, dict] = {}
_lock = threading.Lock()
_current_lang: str = DEFAULT_LANG


def _resolve_locale_dir() -> Path:
    """Find the locales/ directory.

    Search order:
      1. PyInstaller _MEIPASS/locales (the bundle dir for --onefile).
      2. Next to sys.executable/locales (frozen onedir layout).
      3. APP_ROOT/locales (portable install layout — main install dir).
      4. CWD/locales (dev mode).
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidate = Path(meipass) / "locales"
        if candidate.exists():
            return candidate
    try:
        exe_dir = Path(sys.executable).resolve().parent
        for base in (exe_dir, exe_dir / "_internal"):
            cand = base / "locales"
            if cand.exists():
                return cand
    except Exception:
        pass
    if (APP_ROOT / "locales").exists():
        return APP_ROOT / "locales"
    return Path("locales")


LOCALE_DIR = _resolve_locale_dir()


def _load_locale(lang: str) -> dict:
    """Load a locale JSON file. Returns empty dict on failure (logs warning)."""
    if lang in _cache:
        return _cache[lang]
    path = LOCALE_DIR / f"{lang}.json"
    if not path.exists():
        logger.warning("Locale file not found: %s", path)
        _cache[lang] = {}
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.error("Failed to load locale %s: %s", path, e)
        _cache[lang] = {}
        return {}
    _cache[lang] = data
    return data


def set_language(lang: str) -> None:
    """Set the active language for the current process."""
    global _current_lang
    if lang not in SUPPORTED_LANGS:
        logger.warning("Unsupported language '%s', falling back to '%s'", lang, DEFAULT_LANG)
        lang = DEFAULT_LANG
    with _lock:
        _current_lang = lang
        _load_locale(lang)


def get_language() -> str:
    """Return the current language code."""
    return _current_lang


def t(key: str, **kwargs) -> str:
    """Translate a key. Falls back to English, then to ``default``, then to
    the key itself.

    ``default`` is the caller's own English fallback text. Around 50 call
    sites already pass it -- ``t("main.title", default="ThaoResearch")`` --
    so it must be honoured rather than forwarded to ``str.format``:
    without this, a key missing from every locale file renders the raw
    dotted key in the UI (a real bug: the Dashboard progress label read
    "main.fetch_progress_auto"). It is popped out of ``kwargs`` so it can
    never be mistaken for a format placeholder.

    >>> t("common.ok")
    'OK'
    >>> t("toast.title_alert", commodity="Gold", status="Price Alert")
    'ThaoResearch - Gold Price Alert'
    >>> t("no.such.key", default="Fallback")
    'Fallback'
    >>> t("no.such.key")
    'no.such.key'
    """
    default = kwargs.pop("default", None)

    with _lock:
        data = _cache.get(_current_lang)
        if data is None:
            data = _load_locale(_current_lang)
        val = data.get(key) if data else None

    if val is None and _current_lang != DEFAULT_LANG:
        # Fallback to English
        en = _cache.get(DEFAULT_LANG)
        if en is None:
            en = _load_locale(DEFAULT_LANG)
        val = en.get(key) if en else None

    if val is None:
        # The caller's own fallback, else the key itself. Falling through
        # to the same ``format`` below keeps a default like
        # "Fetch failed: {err}" intact when the call site formats it later
        # (the KeyError is caught and the raw string handed back).
        val = default if default is not None else key

    try:
        return val.format(**kwargs)
    except (KeyError, IndexError):
        return val


def reload_locales() -> None:
    """Clear the locale cache (useful after editing locale files)."""
    with _lock:
        _cache.clear()


def init_language(lang: Optional[str] = None) -> str:
    """Initialize language from arg, env var, or default. Returns active lang."""
    if lang is None:
        lang = os.environ.get("THRESEARCH_LANG") or os.environ.get("LANG", DEFAULT_LANG)
        # Normalize: e.g., "vi_VN" -> "vi"
        lang = lang.split("_")[0].split(".")[0].lower()
    if lang not in SUPPORTED_LANGS:
        lang = DEFAULT_LANG
    set_language(lang)
    return lang
