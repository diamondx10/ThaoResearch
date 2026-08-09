"""Internationalization (i18n) — loads locale JSON files and provides t() helper."""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
from pathlib import Path
from typing import Dict, Optional

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
      3. CWD/locales (dev mode).
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
    """Translate a key. Falls back to English, then to the key itself.

    >>> t("common.ok")
    'OK'
    >>> t("toast.title_alert", commodity="Gold", status="Price Alert")
    'ThaoResearch - Gold Price Alert'
    """
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
        # Last resort: return the key itself
        return key

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
