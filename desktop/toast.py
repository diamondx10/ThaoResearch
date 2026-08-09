"""Windows toast notifications.

Tries multiple backends in order:
  1. win10toast    - legacy Shell_NotifyIcon balloon (works on Win7/10/11)
  2. win11toast    - newer WinRT toast (needs AUMID + Start Menu shortcut)
  3. plyer         - portable fallback (uses Win10 toast internally)

Failures are logged at DEBUG level — toasts are best-effort UX, never fatal.
"""
from __future__ import annotations

import logging
import platform
from typing import Callable, Optional

from models.article import Article

logger = logging.getLogger("thaoresearch.desktop.toast")


def is_supported() -> bool:
    return platform.system() == "Windows"


def show_toast(article: Article, on_click: Optional[Callable[[Article], None]] = None) -> bool:
    """Show a Windows toast for the given article.

    Returns True if a toast was shown, False otherwise. Never raises.
    The toast title + body mirror what the Telegram channel sends, so
    the user sees the same content on both channels.
    """
    if not is_supported():
        logger.debug("Toast skipped (not Windows)")
        return False

    # Title + body come from Article so they stay in sync with the
    # Telegram message rendering (`to_telegram_message`).
    title = article.to_toast_title()
    body = article.to_toast_body()

    # ------------------------------------------------------------------ 1. win10toast
    # Legacy Shell_NotifyIcon balloon — the most reliable cross-version API.
    # win10toast ships its own default icon, which avoids the AUMID/icon
    # resolution errors that win11toast hits when the app has no Start Menu
    # shortcut.
    try:
        import win10toast  # type: ignore

        notifier = win10toast.ToastNotifier()
        kwargs = {"threaded": True, "duration": 5}
        if on_click is not None:
            kwargs["callback_on_click"] = lambda: on_click(article)
        notifier.show_toast(title=title, msg=body, **kwargs)
        logger.info("Toast shown (win10toast): %s", article.title[:60])
        return True
    except ImportError:
        pass
    except Exception as e:
        logger.debug("win10toast failed: %s", e)

    # ------------------------------------------------------------------ 2. win11toast
    try:
        import win11toast  # type: ignore

        kwargs = {}
        if on_click is not None:
            kwargs["on_click"] = lambda _a=article: on_click(_a)
        win11toast.toast(title, body, **kwargs)
        logger.info("Toast shown (win11toast): %s", article.title[:60])
        return True
    except ImportError:
        pass
    except Exception as e:
        # Demote to DEBUG — Win11 toast is fragile (AUMID/icon) and not
        # critical to the workflow.
        logger.debug("win11toast failed: %s", e)

    # ------------------------------------------------------------------ 3. plyer
    try:
        from plyer import notification  # type: ignore

        kwargs = {"title": title, "message": body, "timeout": 5, "app_name": "ThaoResearch"}
        if on_click is not None:
            # plyer doesn't support click handlers on Windows; log only.
            logger.debug("plyer does not support on_click; click handler dropped")
        notification.notify(**kwargs)
        logger.info("Toast shown (plyer): %s", article.title[:60])
        return True
    except ImportError:
        pass
    except Exception as e:
        logger.debug("plyer notification failed: %s", e)

    logger.debug("No toast backend succeeded; article=%s", article.title[:60])
    return False
