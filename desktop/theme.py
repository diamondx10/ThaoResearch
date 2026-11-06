"""Visual theme for the ThaoResearch desktop UI.

Why a module instead of per-file constants
------------------------------------------
The GUI is ~170 ttk widget instances across five files, and each file carried
its own copy of the same hex codes (``#FFFFFF``, ``#6B6B6B``, ``#C42B1C``...).
Changing anything meant editing four places and hoping they stayed in sync,
and nothing could react to a dark theme.

Everything visual now comes from here: one palette, one font scale, one call
to apply it.

Which library, and why
----------------------
The UI is built on ``tkinter.ttk``: 49 Labels, 48 Frames, 36 Buttons, plus
4 Treeviews, 2 Notebooks, Spinboxes, Comboboxes and a Progressbar.

* **sv-ttk** (chosen) is a pure *theme* for the stock ttk widgets -- the
  Windows 11 / WinUI "Sun Valley" look. ``sv_ttk.set_theme()`` restyles every
  existing widget with no code changes, Treeview and Notebook included, and
  brings a real dark mode. MIT, ~2.6k stars.
* **CustomTkinter** was rejected: it replaces widgets with its own
  (``CTkButton`` and friends), so adopting it means rewriting the whole GUI --
  and it has no Treeview equivalent, which four of our screens need.
* **ttkbootstrap** also themes ttk, but wants its own ``Window`` class and
  ``bootstyle`` keywords, a bigger change for a look no better suited to a
  Windows-only tool.

If ``sv_ttk`` is not installed the app still runs: ``apply()`` falls back to
the stock ``vista``/``clam`` theme and the palette below keeps the custom
widgets coherent.
"""
from __future__ import annotations

import logging
import sys
import tkinter as tk
from tkinter import ttk
from typing import Dict, Literal

logger = logging.getLogger("thaoresearch.desktop.theme")

Mode = Literal["light", "dark"]

# =============================================================================
# Palette
# =============================================================================
# Base values track sv-ttk's own colors so themed and custom widgets agree:
#   light  fg #1c1c1c  bg #fafafa  accent #005fb8
#   dark   fg #fafafa  bg #1c1c1c  accent #57c8ff
#
# Semantic names only -- no widget should hardcode a hex code again.
_LIGHT: Dict[str, str] = {
    "bg": "#fafafa",          # window background
    "surface": "#ffffff",     # cards, text areas, rows
    "surface_alt": "#f3f3f3",  # zebra striping, inset panels
    "border": "#d8d8d8",
    "fg": "#1c1c1c",          # primary text
    "fg_muted": "#5d5d5d",    # secondary text, timestamps
    "fg_disabled": "#a0a0a0",
    "accent": "#005fb8",      # links, focus, primary action
    "accent_fg": "#ffffff",
    "success": "#0f7b33",     # price alerts, "sent"
    "warning": "#9a6700",
    "warning_bg": "#fff8dc",  # highlight row background
    "danger": "#c42b1c",      # failures, urgent
    "selection_bg": "#2f60d8",
    "selection_fg": "#ffffff",
}

_DARK: Dict[str, str] = {
    "bg": "#1c1c1c",
    "surface": "#252525",
    "surface_alt": "#2d2d2d",
    "border": "#3a3a3a",
    "fg": "#fafafa",
    "fg_muted": "#a8a8a8",
    "fg_disabled": "#595959",
    "accent": "#57c8ff",
    "accent_fg": "#102030",
    "success": "#5ad68a",
    "warning": "#e3b341",
    "warning_bg": "#3a3222",
    "danger": "#ff6b5e",
    "selection_bg": "#2f60d8",
    "selection_fg": "#ffffff",
}

# Active palette. Rebound by apply(); import the module and read
# ``theme.COLORS["fg"]`` rather than copying values out at import time.
COLORS: Dict[str, str] = dict(_LIGHT)
MODE: Mode = "light"

# =============================================================================
# Type scale
# =============================================================================
_FAMILY = "Segoe UI" if sys.platform == "win32" else "TkDefaultFont"

FONTS: Dict[str, tuple] = {
    "display": (_FAMILY, 18, "bold"),   # dashboard counters
    "title": (_FAMILY, 14, "bold"),     # window / section titles
    "heading": (_FAMILY, 11, "bold"),   # card titles, group labels
    "subheading": (_FAMILY, 10, "bold"),
    "body": (_FAMILY, 10),
    "body_sm": (_FAMILY, 9),
    "caption": (_FAMILY, 8),            # paths, hints
    "link": (_FAMILY, 9, "underline"),
    "mono": ("Consolas" if sys.platform == "win32" else "TkFixedFont", 9),
}


def font(name: str) -> tuple:
    """Return a font tuple from the scale, falling back to body."""
    return FONTS.get(name, FONTS["body"])


def resolve_mode(config: dict | None = None) -> Mode:
    """Decide light or dark from ``general.theme`` in the config.

    Accepts ``light``, ``dark`` or ``system``; ``system`` reads the Windows
    "apps use dark mode" preference and falls back to light when it cannot
    be read. Anything unrecognised is treated as light, so a hand-edited
    config never leaves the UI unreadable.
    """
    requested = "system"
    if config:
        requested = str((config.get("general") or {}).get("theme", "system")).lower()

    if requested == "dark":
        return "dark"
    if requested == "light":
        return "light"

    # system
    if sys.platform == "win32":
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
            ) as key:
                apps_use_light, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
                return "light" if apps_use_light else "dark"
        except Exception as e:
            logger.debug("could not read the Windows theme preference: %s", e)
    return "light"


# =============================================================================
# Apply
# =============================================================================
def apply(root: tk.Misc, mode: Mode = "light") -> Mode:
    """Theme ``root`` and every widget created under it. Returns the mode used.

    Safe to call more than once (switching mode at runtime) and safe to call
    when ``sv_ttk`` is missing.
    """
    global COLORS, MODE

    COLORS = dict(_DARK if mode == "dark" else _LIGHT)
    MODE = mode

    # --- ttk widgets -----------------------------------------------------
    themed = False
    try:
        import sv_ttk

        sv_ttk.set_theme(mode, root=root)
        themed = True
    except Exception as e:
        logger.info("sv_ttk unavailable (%s) - using the stock ttk theme", e)
        try:
            style = ttk.Style(root)
            for candidate in ("vista", "winnative", "clam", "default"):
                if candidate in style.theme_names():
                    style.theme_use(candidate)
                    break
        except Exception:
            pass

    _style_overrides(root)
    _widget_defaults(root)

    if mode == "dark":
        # The DWM call needs a real HWND, which does not exist until the
        # window is mapped. apply() runs before _build_ui(), so do it now
        # (harmless if early) and again after the event loop has created
        # the window -- the second attempt is the one that usually lands.
        _dark_titlebar(root)

        def _retry() -> None:
            # The window may be gone by the time this fires (fast close, or a
            # test that builds and destroys). winfo_exists() keeps Tk from
            # logging "invalid command name ...<lambda>".
            try:
                if root.winfo_exists():
                    _dark_titlebar(root)
            except Exception:
                pass

        try:
            root.after(120, _retry)
            root.after(600, _retry)
        except Exception:
            pass
    return MODE


def _style_overrides(root: tk.Misc) -> None:
    """Project-specific ttk styles layered on top of the base theme."""
    try:
        style = ttk.Style(root)
    except Exception:
        return

    c = COLORS

    # Treeview: taller rows read better for article lists, and the stock
    # theme's row height crowds two-line titles.
    try:
        style.configure("Treeview", rowheight=26, borderwidth=0)
        style.configure(
            "Treeview.Heading", font=font("subheading"), padding=(6, 4)
        )
        style.map(
            "Treeview",
            background=[("selected", c["selection_bg"])],
            foreground=[("selected", c["selection_fg"])],
        )
    except Exception:
        pass

    # Semantic label styles, so call sites say what a label *means*.
    for name, key in (
        ("Muted.TLabel", "fg_muted"),
        ("Success.TLabel", "success"),
        ("Danger.TLabel", "danger"),
        ("Warning.TLabel", "warning"),
        ("Accent.TLabel", "accent"),
    ):
        try:
            style.configure(name, foreground=c[key])
        except Exception:
            pass

    try:
        style.configure("Caption.TLabel", foreground=c["fg_muted"], font=font("caption"))
        style.configure("Heading.TLabel", font=font("heading"))
        style.configure("Title.TLabel", font=font("title"))
        style.configure("Display.TLabel", font=font("display"))
        style.configure("Link.TLabel", foreground=c["accent"], font=font("link"))
        # Card: a bordered surface panel for the dashboard tiles.
        style.configure(
            "Card.TFrame",
            background=c["surface"],
            relief="solid",
            borderwidth=1,
        )
        style.configure("Card.TLabel", background=c["surface"], foreground=c["fg"])
    except Exception:
        pass


def _widget_defaults(root: tk.Misc) -> None:
    """Colour the plain-``tk`` widgets sv-ttk does not reach.

    ``tk.Text``, ``tk.Canvas``, ``tk.Listbox`` and ``tk.Toplevel`` are not ttk
    widgets, so a ttk theme leaves them bright white in dark mode. Setting the
    Tk option database means widgets created later pick these up without each
    call site passing colours.
    """
    c = COLORS
    opts = {
        "*Text.background": c["surface"],
        "*Text.foreground": c["fg"],
        "*Text.insertBackground": c["fg"],
        "*Text.selectBackground": c["selection_bg"],
        "*Text.selectForeground": c["selection_fg"],
        "*Text.highlightThickness": "0",
        "*Text.borderWidth": "1",
        "*Text.relief": "solid",
        "*Listbox.background": c["surface"],
        "*Listbox.foreground": c["fg"],
        "*Panedwindow.background": c["bg"],
        "*PanedWindow.background": c["bg"],
        "*Canvas.background": c["bg"],
        "*Canvas.highlightThickness": "0",
        "*Toplevel.background": c["bg"],
        "*Frame.background": c["bg"],
        "*Label.background": c["bg"],
        "*Label.foreground": c["fg"],
    }
    for pattern, value in opts.items():
        try:
            root.option_add(pattern, value)
        except Exception:
            continue

    # The root window itself.
    try:
        top = root.winfo_toplevel()
        top.configure(background=c["bg"])
    except Exception:
        pass


_titlebar_done: set = set()


def _dark_titlebar(root: tk.Misc) -> None:
    """Darken the Windows title bar so it matches a dark client area.

    ttk cannot touch the title bar: it belongs to the window manager. On
    Windows 10+ the DWM exposes an attribute for it. Best effort only --
    a failure just leaves a light title bar, which is cosmetic.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes

        top = root.winfo_toplevel()
        top.update_idletasks()

        # winfo_id() gives Tk's child HWND; the frame that owns the title bar
        # is its parent. On some Tk builds winfo_id() is already the top-level,
        # so try the parent first and fall back to the id itself.
        child = ctypes.windll.user32.GetParent(top.winfo_id())
        targets = [h for h in (child, top.winfo_id()) if h]
        if not targets:
            return
        if any(h in _titlebar_done for h in targets):
            return  # already dark; re-applying would flash the window again

        applied = False
        for hwnd in targets:
            # DWMWA_USE_IMMERSIVE_DARK_MODE is 20 on Windows 10 2004+ and
            # Windows 11; it was 19 on build 1809.
            for attr in (20, 19):
                value = ctypes.c_int(1)
                res = ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, attr, ctypes.byref(value), ctypes.sizeof(value)
                )
                if res == 0:
                    applied = True
                    break
            if applied:
                break

        if applied:
            _titlebar_done.add(hwnd)
            # DWM only repaints the frame on a show/size change. Do this once:
            # repeating it makes the window visibly flash on startup.
            try:
                if top.winfo_viewable():
                    top.withdraw()
                    top.deiconify()
            except Exception:
                pass
    except Exception as e:
        logger.debug("dark title bar not applied: %s", e)


def style_text_widget(widget: tk.Text) -> None:
    """Apply the palette to a ``tk.Text`` that already exists.

    The option database only affects widgets created after ``apply()``, so
    anything built earlier needs this.
    """
    c = COLORS
    try:
        widget.configure(
            background=c["surface"],
            foreground=c["fg"],
            insertbackground=c["fg"],
            selectbackground=c["selection_bg"],
            selectforeground=c["selection_fg"],
            highlightthickness=0,
            borderwidth=1,
            relief="solid",
        )
    except Exception:
        pass
