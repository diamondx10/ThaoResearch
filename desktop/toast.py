"""Windows toast notifications.

Backends (tried in order, first success wins):

  1. windows-toasts (WinRT API; works on Win10/11 if notifications are
     enabled for the app — even without a registered Start Menu shortcut).
  2. win11toast    (legacy WinRT — needs AUMID, often broken).
  3. win10toast    (Shell_NotifyIcon balloon — broken on Py3.12+).
  4. plyer         (portable fallback — toast lands in Action Center but
     does NOT pop up on Win10 build 1903+).

All failures are logged at DEBUG level. Toasts are best-effort UX, never fatal.

Per-article dedup
-----------------
When the app has both the tray icon AND the dashboard window running, the
``new_article`` gui_queue event is delivered to both Tk threads (tray and
main window), and each one calls ``show_toast(article)``. Without dedup
the user would see two banners per article — once per subscriber.

To prevent that we keep a small in-memory LRU of recently-shown article
hashes. The first subscriber wins; subsequent calls within the dedup
window return False without showing anything. The window is short enough
that legitimate re-sends (e.g. force=True from CLI) still work — and the
DB-level ``is_sent`` check upstream already prevents double-sends on the
normal path.
"""
from __future__ import annotations

import logging
import platform
import threading
import time
from collections import OrderedDict
from typing import Callable, Optional

from models.article import Article

logger = logging.getLogger("thaoresearch.desktop.toast")


# AUMID — stable across builds. Bumping this forces Windows to forget
# the cached app entry and re-resolve from the .exe + icon.
AUMID = "ThaoResearch.Watcher.2fa1e1d9"

# Dedup window — within this many seconds, the same article hash can
# only fire ONE toast across all Tk subscribers. Long enough to swallow
# the tray+main double-fire, short enough that manual retries (e.g.
# user clicks "Resend" in History tab) still surface.
_DEDUP_TTL_SEC = 30.0
_DEDUP_MAX = 128

_recent_lock = threading.Lock()
_recent_hashes: "OrderedDict[str, float]" = OrderedDict()


def _mark_recent(article_hash: str) -> bool:
    """Record ``article_hash`` as "just toasted".

    Returns ``True`` if this is the FIRST sighting in the dedup window
    (caller should display the toast), ``False`` if a duplicate that
    should be suppressed.
    """
    now = time.monotonic()
    with _recent_lock:
        # Drop expired entries.
        cutoff = now - _DEDUP_TTL_SEC
        while _recent_hashes:
            oldest_key, oldest_ts = next(iter(_recent_hashes.items()))
            if oldest_ts < cutoff:
                _recent_hashes.popitem(last=False)
            else:
                break
        if article_hash in _recent_hashes:
            # Duplicate within the dedup window — suppress.
            return False
        _recent_hashes[article_hash] = now
        # Bound the cache size.
        while len(_recent_hashes) > _DEDUP_MAX:
            _recent_hashes.popitem(last=False)
        return True


def is_supported() -> bool:
    return platform.system() == "Windows"


def _set_aumid() -> None:
    """Set the AppUserModelID so the toast appears under "ThaoResearch"
    in the Windows Action Center, not under "Python.exe". Best-effort.
    """
    if platform.system() != "Windows":
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(AUMID)
    except Exception:
        pass


_aumid_registered = False


def _register_aumid() -> bool:
    """Register the AUMID under HKCU so toasts can carry action buttons.

    Windows will only route a toast's button press back to an unpackaged
    app if the notifier's AUMID is one it recognises. ``InteractableWindowsToaster``
    otherwise falls back to Command Prompt's AUMID, and the banner then
    renders with "Command Prompt" as both the app name and the heading --
    verified on screen, and obviously not shippable.

    Registering is a single per-user registry key (no admin rights, no
    Start Menu shortcut, nothing outside HKCU):

        HKCU\\SOFTWARE\\Classes\\AppUserModelId\\<AUMID>
            DisplayName = ThaoResearch
            IconUri     = ...\\assets\\tray.ico

    Returns True when the app may use the interactive toaster. Best-effort:
    a locked-down machine that refuses the write just loses the button, not
    the notification.
    """
    global _aumid_registered
    if _aumid_registered:
        return True
    if platform.system() != "Windows":
        return False
    try:
        import winreg

        key_path = rf"SOFTWARE\Classes\AppUserModelId\{AUMID}"
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key_path) as key:
            winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, "ThaoResearch")
            icon = _icon_path()
            if icon is not None:
                winreg.SetValueEx(key, "IconUri", 0, winreg.REG_SZ, str(icon))
        _aumid_registered = True
        logger.debug("Registered toast AUMID %s", AUMID)
        return True
    except Exception as e:
        logger.debug("Could not register AUMID (%s); falling back to a plain toast", e)
        return False


def _icon_path():
    """Absolute path to the tray icon, or None. Works frozen and from source.

    ``_MEIPASS`` is checked too: in a --onefile build the assets are
    unpacked there, so the icon is still found if the folder beside the
    .exe is missing or the user moved only the .exe.
    """
    import sys
    from pathlib import Path

    candidates = []
    try:
        from utils import paths

        candidates += [
            paths.ASSETS_DIR / "tray.ico",
            paths.APP_ROOT / "assets" / "tray.ico",
        ]
    except Exception:
        pass

    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / "assets" / "tray.ico")

    for candidate in candidates:
        try:
            if candidate.is_file():
                return candidate.resolve()
        except Exception:
            continue
    return None


def unregister_aumid() -> bool:
    """Remove the HKCU AUMID key. Provided so the app can leave no trace."""
    try:
        import winreg

        winreg.DeleteKey(
            winreg.HKEY_CURRENT_USER, rf"SOFTWARE\Classes\AppUserModelId\{AUMID}"
        )
        return True
    except Exception:
        return False


# WinRT dispatches the activation callback from its own thread and does not
# hold a strong reference to the Toast, so a toast that goes out of scope
# stops calling back. Keep the last few alive.
_live_toasts: "OrderedDict[int, object]" = OrderedDict()
_LIVE_MAX = 16


def _show_with_windows_toasts(title: str, body: str, article: Article, on_click: Optional[Callable[[Article], None]]) -> bool:
    """Backend 1: windows-toasts (preferred).

    When a click handler is supplied we use ``InteractableWindowsToaster``
    with our own registered AUMID so the banner can carry a "Read more"
    button, and route both that button and a click on the banner body back
    to ``on_click``.

    This used to build a plain ``WindowsToaster`` and ignore ``on_click``
    entirely -- the parameter was accepted, threaded through all four
    backends, and silently dropped here, so clicking a notification did
    nothing at all.

    If the AUMID cannot be registered we fall back to the old plain toast:
    no button, but the notification still appears under the right name.
    """
    try:
        from windows_toasts import (
            InteractableWindowsToaster,
            Toast,
            ToastButton,
            WindowsToaster,
        )

        _set_aumid()

        interactive = on_click is not None and _register_aumid()

        toast = Toast()
        toast.text_fields = [title, body]

        if interactive:
            toaster = InteractableWindowsToaster("ThaoResearch", notifierAUMID=AUMID)
            toast.AddAction(ToastButton(_read_more_label(), "readmore"))

            def _activated(args, _article=article, _cb=on_click):
                # Fires on a WinRT thread. ``on_click`` must therefore be
                # thread-safe; callers hand us one that only enqueues onto
                # gui_queue so the window is built on a Tk thread.
                try:
                    _cb(_article)
                except Exception as exc:
                    logger.debug("Toast activation handler failed: %s", exc)

            toast.on_activated = _activated

            # Hold a reference: WinRT keeps none, and a collected Toast
            # stops delivering its callback.
            _live_toasts[id(toast)] = toast
            while len(_live_toasts) > _LIVE_MAX:
                _live_toasts.popitem(last=False)
        else:
            # Default args = Windows auto-assigns an AUMID based on the
            # process. Enough to display a toast grouped under
            # "ThaoResearch", but it cannot carry buttons.
            toaster = WindowsToaster("ThaoResearch")

        # show_toast() returns True on success, None on unverified.
        # We consider both "success" because the OS may suppress the
        # toast for focus/quiet reasons — that's not our bug.
        result = toaster.show_toast(toast)
        logger.info(
            "Toast shown (windows-toasts, %s): %s",
            "interactive" if interactive else "plain",
            title[:60],
        )
        return result is not False
    except ImportError:
        logger.debug("windows-toasts not installed")
    except Exception as e:
        logger.debug("windows-toasts failed: %s", e)
    return False


def _read_more_label() -> str:
    """Button caption, localized, with a safe English default."""
    try:
        from utils.i18n import t

        return t("toast.btn_read_more", default="Read more")
    except Exception:
        return "Read more"


def _show_with_win11toast(title: str, body: str, article: Article, on_click: Optional[Callable[[Article], None]]) -> bool:
    try:
        import win11toast  # type: ignore

        kwargs = {}
        if on_click is not None:
            kwargs["on_click"] = lambda _a=article: on_click(_a)
        win11toast.toast(title, body, **kwargs)
        logger.info("Toast shown (win11toast): %s", title[:60])
        return True
    except ImportError:
        pass
    except Exception as e:
        logger.debug("win11toast failed: %s", e)
    return False


def _show_with_win10toast(title: str, body: str, article: Article, on_click: Optional[Callable[[Article], None]]) -> bool:
    try:
        import win10toast  # type: ignore

        notifier = win10toast.ToastNotifier()
        kwargs = {"threaded": True, "duration": 5}
        if on_click is not None:
            kwargs["callback_on_click"] = lambda: on_click(article)
        # win10toast uses Shell_NotifyIcon balloons. Title max 64 chars, body max 256.
        notifier.show_toast(
            title=title[:64],
            msg=body[:256],
            **kwargs,
        )
        logger.info("Toast shown (win10toast): %s", title[:60])
        return True
    except ImportError:
        pass
    except Exception as e:
        logger.debug("win10toast failed: %s", e)
    return False


def _show_with_plyer(title: str, body: str, article: Article, on_click: Optional[Callable[[Article], None]]) -> bool:
    try:
        from plyer import notification  # type: ignore

        # plyer on Windows wraps Shell_NotifyIcon — same 64/256 char limits.
        kwargs = {
            "title": title[:64],
            "message": body[:256],
            "timeout": 5,
            "app_name": "ThaoResearch",
        }
        if on_click is not None:
            logger.debug("plyer does not support on_click; click handler dropped")
        notification.notify(**kwargs)
        logger.info("Toast shown (plyer): %s", title[:60])
        return True
    except ImportError:
        pass
    except Exception as e:
        logger.debug("plyer notification failed: %s", e)
    return False


def show_toast(article: Article, on_click: Optional[Callable[[Article], None]] = None) -> bool:
    """Show a Windows toast for the given article.

    Returns True if a toast was shown (or queued for display), False otherwise.
    Never raises. The toast title + body mirror what the Telegram channel sends.

    Note: Windows 10/11 may pop up a banner, or quietly add to the Action
    Center, or silently suppress — that depends on the user's Focus Assist
    and per-app notification settings. The best we can do is call the API.

    Per-article dedup: see module docstring. If this article has already
    been toasted within ``_DEDUP_TTL_SEC`` seconds (e.g. the tray
    subscriber fired first and the main-window subscriber is now
    catching up), we silently drop the second call so the user only
    sees ONE banner per article.
    """
    if not is_supported():
        logger.debug("Toast skipped (not Windows)")
        return False

    # Dedup before computing title/body — cheaper than building strings
    # we'll never use.
    try:
        article_hash = article.compute_hash()
    except Exception:
        article_hash = None
    if article_hash and not _mark_recent(article_hash):
        logger.debug(
            "Toast suppressed (duplicate within %ss window): %s",
            _DEDUP_TTL_SEC, article.title[:60],
        )
        return False

    title = article.to_toast_title()
    body = article.to_toast_body()

    # Try each backend in order. Each helper returns True on success.
    for fn in (
        _show_with_windows_toasts,
        _show_with_win11toast,
        _show_with_win10toast,
        _show_with_plyer,
    ):
        try:
            if fn(title, body, article, on_click):
                return True
        except Exception as e:
            logger.debug("Backend %s crashed: %s", fn.__name__, e)

    logger.debug("No toast backend succeeded; article=%s", article.title[:60])
    return False