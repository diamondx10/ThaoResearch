"""Shared helpers for setting the application icon on Tk windows.

The application ships a single icon file (`assets/tray.ico`) that is used
both for the system tray and for every Tk window we create. Tk's
`iconbitmap` only sets ICON_SMALL (title bar); the taskbar / Alt-Tab
uses ICON_BIG. We therefore fire **both** paths so the icon shows up
consistently across the title bar and the taskbar.

Cross-platform note: setting ICON_BIG via `WM_SETICON` is Win32-only.
On Linux/macOS we fall back to `iconbitmap` (which works correctly on
those platforms).
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

try:
    import tkinter as tk
except Exception:  # pragma: no cover - tkinter always present in this app
    tk = None  # type: ignore[assignment]

from utils.resource_path import get_resource_path

_ICON_CANDIDATES = (
    "assets/tray.ico",
    "assets/icon.ico",
    "media/icon.ico",
    "media/icon.png",
)


_cached_icon_path: Optional[Path] = None


def get_app_icon_path() -> Optional[Path]:
    """Return the first icon file that exists on disk, or None."""
    global _cached_icon_path
    if _cached_icon_path is not None:
        return _cached_icon_path
    for rel in _ICON_CANDIDATES:
        try:
            p = get_resource_path(rel)
        except Exception:
            continue
        try:
            if p.exists():
                _cached_icon_path = p
                return p
        except OSError:
            continue
    return None


def is_ico(path: Path) -> bool:
    return path.suffix.lower() == ".ico"


def apply_app_icon(window: "tk.Misc") -> Optional[Path]:
    """Apply the application icon to a Tk window.

    Returns the icon path that was used, or None if no icon was found.

    Safe to call on any Tk widget (Toplevel, Tk root, Frame, etc.).
    """
    icon_path = get_app_icon_path()
    if icon_path is None:
        return None

    # Toplevel/Tk both expose iconbitmap; Frames and other widgets do not.
    set_icon = getattr(window, "iconbitmap", None)
    if set_icon is not None and is_ico(icon_path):
        try:
            set_icon(default=str(icon_path))
        except Exception:
            try:
                set_icon(str(icon_path))
            except Exception:
                pass

    # On Windows, also set ICON_BIG via WM_SETICON so the taskbar / Alt-Tab
    # shows the same icon. Tk's iconbitmap only sets ICON_SMALL.
    if sys.platform == "win32":
        try:
            # Schedule after the window has a real hwnd (TK can take a tick).
            window.after(80, lambda: _set_windows_big_icon(window, icon_path))
        except Exception:
            pass

    return icon_path


def _set_windows_big_icon(window: "tk.Misc", icon_path: Path) -> None:
    """Send WM_SETICON to set ICON_BIG on a Tk window."""
    try:
        import ctypes
        from ctypes import wintypes

        WM_SETICON = 0x0080
        ICON_BIG = 1
        IMAGE_ICON = 1
        LR_LOADFROMFILE = 0x00000010
        LR_DEFAULTSIZE = 0x00000040

        user32 = ctypes.windll.user32
        user32.LoadImageW.restype = wintypes.HANDLE
        user32.LoadImageW.argtypes = [
            wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
            ctypes.c_int, ctypes.c_int, wintypes.UINT,
        ]
        user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        try:
            user32.InvalidateRect.argtypes = [wintypes.HWND, wintypes.LPCVOID, wintypes.BOOL]
        except Exception:
            pass

        # Resolve the actual top-level hwnd (Tk child-canvas walks up).
        try:
            hwnd = int(window.winfo_id())
        except Exception:
            return
        top_hwnd = hwnd
        for _ in range(10):
            try:
                owner = user32.GetParent(top_hwnd)
            except Exception:
                break
            if not owner:
                break
            top_hwnd = owner

        hicon = user32.LoadImageW(
            None, str(icon_path), IMAGE_ICON, 32, 32, LR_LOADFROMFILE | LR_DEFAULTSIZE
        )
        if hicon:
            user32.SendMessageW(top_hwnd, WM_SETICON, ICON_BIG, hicon)
            try:
                user32.InvalidateRect(top_hwnd, None, True)
            except Exception:
                pass
    except Exception:
        # Non-fatal: ICON_BIG is a polish improvement, not a requirement.
        pass
