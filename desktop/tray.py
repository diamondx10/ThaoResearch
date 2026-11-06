"""System tray icon and menu (pystray)."""
from __future__ import annotations

import logging
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable, Optional

from PIL import Image, ImageDraw

from desktop.gui_queue import gui_queue
from utils.i18n import t
from utils import paths
from utils.paths import APP_ROOT

logger = logging.getLogger("thaoresearch.desktop.tray")


def _make_icon_image(size: int = 64) -> Image.Image:
    """Load the bundled tray icon (assets/tray.ico).

    Falls back to an inline-drawn "TR" mark if the .ico file is missing
    (e.g. running from source before the icon was generated). This is the
    single source of truth for the tray icon — keep `assets/tray.ico` in
    sync with `assets/make_icon.py`.
    """
    # Try bundled .ico first. resource_path() handles both dev (cwd) and
    # PyInstaller frozen (sys._MEIPASS) layouts.
    try:
        from utils.resource_path import get_resource_path
        candidates = ["assets/tray.ico", "assets/icon.ico", "media/icon.ico"]
        for rel in candidates:
            p = get_resource_path(rel)
            if p.exists():
                img = Image.open(str(p))
                # Normalize to RGBA so pystray can render on any system
                if img.mode != "RGBA":
                    img = img.convert("RGBA")
                if img.size != (size, size):
                    img = img.resize((size, size), Image.LANCZOS)
                logger.debug("Loaded tray icon from %s (%dx%d)", p, size, size)
                return img
        logger.warning("No tray icon found in %s; using fallback.", candidates)
    except Exception as e:
        logger.warning("Failed to load tray icon from assets: %s; using fallback.", e)

    # ---- Fallback: hand-drawn "TR" on white ---------------------------------
    img = Image.new("RGBA", (size, size), (255, 255, 255, 255))
    draw = ImageDraw.Draw(img)
    draw.rectangle([(0, 0), (size - 1, size - 1)], outline=(31, 31, 31), width=2)
    try:
        from PIL import ImageFont
        font = ImageFont.load_default()
    except Exception:
        font = None
    text = "TR"
    if font:
        try:
            bbox = draw.textbbox((0, 0), text, font=font)
            tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
            x = (size - tw) // 2 - bbox[0]
            y = (size - th) // 2 - bbox[1]
            draw.text((x, y), text, fill=(31, 31, 31), font=font)
        except Exception:
            draw.text((size // 4, size // 4), text, fill=(31, 31, 31))
    else:
        draw.text((size // 4, size // 4), text, fill=(31, 31, 31))
    return img


def _open_folder(path: Path):
    try:
        if sys.platform == "win32":
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except Exception as e:
        logger.warning("Failed to open %s: %s", path, e)


class TrayApp:
    """pystray-based tray icon. Drives a Tk main loop in a background thread for windows."""

    def __init__(
        self,
        on_run_now: Callable[[], None],
        on_show_history: Callable[[], None],
        on_show_settings: Callable[[], None],
        on_exit: Callable[[], None],
        on_open_article: Callable[[object], None],
        get_status_text: Callable[[], str],
    ):
        self.on_run_now = on_run_now
        self.on_show_history = on_show_history
        self.on_show_settings = on_show_settings
        self.on_exit = on_exit
        self.on_open_article = on_open_article
        self.get_status_text = get_status_text
        self._icon = None
        self._stop_event = threading.Event()
        self._tk_thread: Optional[threading.Thread] = None
        self._tk_root = None
        self._tk_lock = threading.Lock()

    def _build_menu(self):
        import pystray

        status_text = self.get_status_text()
        return pystray.Menu(
            pystray.MenuItem(
                f"ThaoResearch v1.0.0\n{status_text}",
                None,
                enabled=False,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(t("tray.run_now"), lambda _i, _it: self.on_run_now()),
            pystray.MenuItem(t("tray.view_history"), lambda _i, _it: self.on_show_history()),
            pystray.MenuItem(t("tray.settings"), lambda _i, _it: self.on_show_settings()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                t("tray.open_logs"),
                lambda _i, _it: _open_folder(paths.LOG_DIR),
            ),
            pystray.MenuItem(
                t("tray.open_data"),
                lambda _i, _it: _open_folder(paths.DATA_DIR),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(t("tray.exit"), lambda _i, _it: self._request_exit()),
        )

    def _request_exit(self):
        self._stop_event.set()
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:
                pass

    def _refresh_menu(self):
        if self._icon is not None:
            try:
                self._icon.menu = self._build_menu()
                self._icon.update_menu()
            except Exception as e:
                logger.debug("Failed to refresh menu: %s", e)

    # ----------------------------------------------------- Tk background thread
    def _start_tk_loop(self):
        """Run a Tk mainloop in a background thread; the tray runs in main thread."""
        import tkinter as tk

        self._tk_root = tk.Tk()
        self._tk_root.withdraw()  # hidden root
        gui_queue.bind_tk_root(self._tk_root)
        # Subscribe so we have our own private queue — the legacy
        # `gui_queue.get()` is no longer shared with MainWindow.
        self._gui_subscriber = gui_queue.subscribe("tray")

        def poll():
            """Poll the gui_queue for events and dispatch."""
            try:
                while True:
                    event = self._gui_subscriber.get(timeout=0.1)
                    if event is None:
                        break
                    etype, payload = event
                    self._dispatch_event(etype, payload)
            except Exception:
                pass
            if not self._stop_event.is_set():
                self._tk_root.after(100, poll)

        self._tk_root.after(100, poll)

        try:
            self._tk_root.mainloop()
        except Exception as e:
            logger.error("Tk mainloop crashed: %s", e)
        finally:
            try:
                self._tk_root.destroy()
            except Exception:
                pass

    def _dispatch_event(self, etype: str, payload):
        """Handle an event off the gui_queue in the Tk thread."""
        from desktop.article_viewer import ArticleViewer
        from desktop.history_window import HistoryWindow
        from desktop.main_window import MainWindow
        from desktop.settings_window import SettingsWindow

        try:
            if etype == "new_article":
                # Show a toast (in background) and let user click tray to view next
                from desktop.toast import show_toast

                try:
                    show_toast(payload, on_click=lambda a: self._open_article_window(a))
                except Exception:
                    pass
            elif etype == "open_viewer":
                self._open_article_window(payload)
            elif etype == "show_history":
                HistoryWindow(on_open_viewer=self.on_open_article, parent=self._tk_root).show()
            elif etype == "show_settings":
                # SettingsWindow needs current config — caller passes via
                # gui_queue payload. Tuple shape:
                #     (config, config_path)            — legacy (2-tuple)
                #     (config, config_path, on_save)   — current (3-tuple)
                # The third element is the ``config_saved`` callback so
                # the dialog can notify the host (e.g. license activation
                # mid-session → rebuild notifier without restart).
                if payload and len(payload) >= 3:
                    cfg, cfg_path, save_cb = payload
                elif payload and len(payload) == 2:
                    cfg, cfg_path = payload
                    save_cb = None
                else:
                    cfg, cfg_path = (payload if payload else ({}, paths.CONFIG_FILE))
                    save_cb = None
                SettingsWindow(cfg, cfg_path, on_save=save_cb).show()
            elif etype == "show_main_window":
                # payload = (config, config_path, on_run_now, on_open_article, on_config_saved)
                # or None (use defaults).
                if payload:
                    cfg, cfg_path, run_cb, open_cb, save_cb = payload
                else:
                    cfg, cfg_path, run_cb, open_cb, save_cb = (
                        {},
                        paths.CONFIG_FILE,
                        None,
                        self.on_open_article,
                        None,
                    )
                # Lazy import to avoid circular dep
                MainWindow(
                    config=cfg,
                    config_path=cfg_path,
                    on_run_now=run_cb,
                    on_open_article=open_cb,
                    on_config_saved=save_cb,
                ).show()
            elif etype == "status":
                # Update menu status
                self._refresh_menu()
            elif etype == "shutdown":
                self._request_exit()
        except Exception as e:
            logger.exception("Error dispatching event %s: %s", etype, e)

    def _open_article_window(self, article):
        from desktop.article_viewer import ArticleViewer

        ArticleViewer(article, parent=self._tk_root).show()

    # ----------------------------------------------------------------- public
    def start_blocking(self):
        """Start the tray (blocks the calling thread)."""
        # Start Tk background thread
        self._tk_thread = threading.Thread(target=self._start_tk_loop, daemon=True, name="tk-gui")
        self._tk_thread.start()

        import pystray

        self._icon = pystray.Icon(
            "ThaoResearch",
            icon=_make_icon_image(),
            title="ThaoResearch",
            menu=self._build_menu(),
        )
        try:
            self._icon.run()
        except KeyboardInterrupt:
            pass
        finally:
            self._request_exit()

    def stop(self):
        self._request_exit()
