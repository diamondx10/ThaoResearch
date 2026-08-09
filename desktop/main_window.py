"""Main application window — Tk root hosting a 3-tab Notebook.

Tabs (in display order):
    0. Settings  (default selected) — full config.yaml editor
    1. Dashboard — counters + countdown + log tail + action buttons
    2. History   — list of sent articles

The window is created on the Tk main thread. When embedded in the
tray-driven daemon, it lives in the tray's Tk background thread.
"""
from __future__ import annotations

import logging
import sys
import threading
import tkinter as tk
from datetime import datetime, timezone
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any, Callable, Optional

from utils import database, i18n, price_tracker
from utils.i18n import t

logger = logging.getLogger("thaoresearch.desktop.main_window")


CARD_BG = "#FFFFFF"
CARD_BORDER = "#E0E0E0"
COLOR_OK = "#2E7D32"
COLOR_WARN = "#C42B1C"
COLOR_MUTED = "#6B6B6B"


class MainWindow:
    """Top-level Tk window with 3 tabs."""

    def __init__(
        self,
        config: dict,
        config_path: Path,
        on_run_now: Optional[Callable[[], None]] = None,
        on_fetch_now: Optional[Callable[[], None]] = None,
        on_toggle_loop: Optional[Callable[[bool], None]] = None,
        on_clear_cache: Optional[Callable[[], list]] = None,
        on_open_article: Optional[Callable[[object], None]] = None,
        on_config_saved: Optional[Callable[[dict], None]] = None,
        scheduler: Optional[Any] = None,
        job_id: str = "main_job",
    ):
        self.config = config
        self.config_path = Path(config_path)
        self.on_run_now = on_run_now
        self.on_fetch_now = on_fetch_now
        self.on_toggle_loop = on_toggle_loop
        self.on_clear_cache = on_clear_cache
        self.on_open_article = on_open_article
        self.on_config_saved = on_config_saved
        # scheduler + job_id are used to render the "Next run in" countdown
        self.scheduler = scheduler
        self.job_id = job_id
        # Default: paused. The dashboard's "Resume Loop" button kicks
        # the scheduler off. The host can override this with
        # ``set_loop_state(True)`` if it wants the app to start running.
        self._loop_running = False
        self._fetch_in_flight = False
        self.window = tk.Tk()
        self.window.title(t("main.title", default="ThaoResearch"))
        self.window.minsize(820, 560)
        self.window.protocol("WM_DELETE_WINDOW", self._on_close)
        # Set window icon (works both in dev and PyInstaller frozen)
        from desktop.window_icon import apply_app_icon
        self._icon_path = apply_app_icon(self.window)
        self._build_ui()
        # Force geometry AFTER children are packed (otherwise some Tk themes
        # shrink the window to 1x1+0+0).
        self.window.geometry("980x700")
        self.window.update_idletasks()
        # On Windows, set ICON_BIG (used by the taskbar / Alt-Tab) via
        # WM_SETICON — Tk's iconbitmap only sets ICON_SMALL (title bar).
        if sys.platform == "win32":
            try:
                self.window.after(100, self._set_windows_big_icon)
            except Exception:
                pass
        # Settings is the default landing tab (per UX request).
        # Periodic dashboard refresh
        self._refresh_dashboard()

    def _set_windows_big_icon(self):
        """Set ICON_BIG on the Tk window via WM_SETICON so the taskbar shows
        the same icon as the .exe (Tk's iconbitmap only sets ICON_SMALL).
        """
        if sys.platform != "win32" or self._icon_path is None:
            return
        try:
            import ctypes
            from ctypes import wintypes

            # Win32 constants
            WM_SETICON = 0x0080
            ICON_SMALL = 0
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

            hwnd = int(self.window.winfo_id())
            # Get the toplevel HWND (Tk uses child window for canvas)
            GW_OWNER = 4
            # Walk up parents to find the top-level frame window
            top_hwnd = hwnd
            for _ in range(10):
                owner = user32.GetParent(top_hwnd)
                if not owner:
                    break
                top_hwnd = owner

            icon_path = str(self._icon_path)
            hicon = user32.LoadImageW(
                None, icon_path, IMAGE_ICON, 32, 32, LR_LOADFROMFILE | LR_DEFAULTSIZE
            )
            if hicon:
                user32.SendMessageW(top_hwnd, WM_SETICON, ICON_BIG, hicon)
                # Also force a redraw of the title bar
                user32.InvalidateRect(top_hwnd, None, True)
                logger.debug("Set ICON_BIG via WM_SETICON (hwnd=%s, hicon=%s)", top_hwnd, hicon)
        except Exception as e:
            logger.debug("Failed to set ICON_BIG: %s", e)

    # ------------------------------------------------------------- UI build
    def _build_ui(self):
        notebook = ttk.Notebook(self.window)
        notebook.pack(fill="both", expand=True, padx=10, pady=(10, 4))
        self.notebook = notebook

        # Tabs in display order; first added = first shown. Settings goes first.
        self.dashboard_frame = ttk.Frame(notebook)
        notebook.add(self.dashboard_frame, text=t("main.tab_dashboard", default="Dashboard"))

        self.settings_frame = ttk.Frame(notebook)
        notebook.add(self.settings_frame, text=t("main.tab_settings", default="Settings"))

        self.history_frame = ttk.Frame(notebook)
        notebook.add(self.history_frame, text=t("main.tab_history", default="History"))

        self._build_dashboard_tab()
        self._build_settings_tab()
        self._build_history_tab()

        # Sync the toggle button with the actual default state. The
        # button was created with a placeholder label; the *real*
        # initial label is decided by ``_loop_running`` (False = paused
        # by default).
        try:
            self.set_loop_state(self._loop_running)
        except Exception:
            pass

        # Bottom status bar
        status = ttk.Frame(self.window, padding=(10, 4, 10, 8))
        status.pack(fill="x")
        self.status_var = tk.StringVar(value=self._status_text())
        ttk.Label(status, textvariable=self.status_var, foreground=COLOR_MUTED).pack(side="left")
        ttk.Button(status, text=t("common.close", default="Close"), command=self._on_close, width=10).pack(side="right")

    def _select_settings_tab(self):
        try:
            self.notebook.select(0)
        except tk.TclError:
            pass

    # ------------------------------------------------------------ Settings
    def _build_settings_tab(self):
        from desktop.settings_window import build_widget
        from utils.resource_path import get_resource_path

        # Header strip with logo + app name (uses media/icon.png)
        header = ttk.Frame(self.settings_frame, padding=(12, 8, 12, 8))
        header.pack(fill="x", side="top")
        try:
            png_path = get_resource_path("media/icon.png")
            if png_path.exists():
                from PIL import Image, ImageTk
                im = Image.open(png_path).convert("RGBA")
                im.thumbnail((48, 48))
                self._brand_logo = ImageTk.PhotoImage(im)
                ttk.Label(header, image=self._brand_logo).pack(side="left", padx=(0, 10))
        except Exception as e:
            logger.debug("Could not render brand logo: %s", e)
        ttk.Label(
            header,
            text=f"{t('main.title', default='ThaoResearch')}",
            font=("Segoe UI", 14, "bold"),
        ).pack(side="left")
        ttk.Label(
            header,
            text="  •  Commodity News Intelligence",
            foreground="#6B6B6B",
            font=("Segoe UI", 10),
        ).pack(side="left", padx=(4, 0))

        self._settings_widget = build_widget(
            self.settings_frame,
            config=self.config,
            config_path=self.config_path,
            on_save=self._on_settings_saved,
            show_outer_buttons=False,
        )
        self._settings_widget.pack(fill="both", expand=True)

        # Save bar at the bottom of the tab
        bar = ttk.Frame(self.settings_frame, padding=(12, 4, 12, 8))
        bar.pack(fill="x", side="bottom")
        ttk.Button(bar, text=t("settings.btn_cancel"), command=self._reload_settings_tab, width=12).pack(
            side="right", padx=4
        )
        ttk.Button(bar, text=t("settings.btn_save"), command=self._save_settings_tab, width=12).pack(
            side="right", padx=4
        )

    def _reload_settings_tab(self):
        """Rebuild the Settings tab from the current on-disk config."""
        self._settings_widget.destroy()
        try:
            import yaml

            with open(self.config_path, encoding="utf-8") as f:
                self.config = yaml.safe_load(f) or {}
        except Exception as e:
            logger.warning("Failed to reload config.yaml: %s", e)
        self._build_settings_tab()

    def _save_settings_tab(self):
        ok = self._settings_widget.save_and_close()
        if not ok:
            return
        # Reload in-memory config and let host react (apply autostart / notifier etc.)
        try:
            import yaml

            with open(self.config_path, encoding="utf-8") as f:
                self.config = yaml.safe_load(f) or {}
        except Exception as e:
            logger.warning("Failed to reload config.yaml after save: %s", e)
        # Refresh history view to reflect any config-driven changes
        if hasattr(self, "_history_widget"):
            try:
                self._history_widget.refresh()
            except Exception:
                pass
        if self.on_config_saved:
            try:
                self.on_config_saved(self.config)
            except Exception as e:
                logger.warning("on_config_saved failed: %s", e)
        self.status_var.set(self._status_text(saved=True))

    def _on_settings_saved(self, new_cfg):
        # Invoked from inside ``build_widget`` when its own outer Save is
        # clicked (we set ``show_outer_buttons=False`` so this is rare,
        # but the license ``Submit`` button also routes through here).
        # Delegate to the host's ``on_config_saved`` so license
        # activation in the embedded settings tab also rebuilds the
        # notifier (Telegram + toast + recent_articles).
        if self.on_config_saved:
            try:
                self.on_config_saved(self.config)
            except Exception as e:
                logger.warning("on_config_saved (from settings) failed: %s", e)

    # ----------------------------------------------------------- Dashboard
    def _build_dashboard_tab(self):
        outer = ttk.Frame(self.dashboard_frame, padding=12)
        outer.pack(fill="both", expand=True)

        cards = ttk.Frame(outer)
        cards.pack(fill="x")

        self.card_total_sent = self._make_card(cards, t("main.card_total_sent", default="Total Sent"), "0")
        self.card_total_sent.pack(side="left", padx=(0, 8), fill="x", expand=True)

        self.card_today = self._make_card(cards, t("main.card_today", default="Today"), "0")
        self.card_today.pack(side="left", padx=8, fill="x", expand=True)

        self.card_prices = self._make_card(cards, t("main.card_prices", default="Tracked Commodities"), "0")
        self.card_prices.pack(side="left", padx=8, fill="x", expand=True)

        # Replace the old "Status" card with a "Next Run" countdown card.
        # The card shows mm:ss remaining until the next scheduled fetch, or
        # "--:--" when the loop is paused / no scheduler is wired in.
        self.card_next_run = self._make_card(
            cards,
            t("main.card_next_run", default="Next Run"),
            "--:--",
        )
        self.card_next_run.pack(side="left", padx=(8, 0), fill="x", expand=True)

        # Controls row 1: Fetch Now + Run Now toggle + Clear Cache
        ctrls = ttk.Frame(outer, padding=(0, 12, 0, 0))
        ctrls.pack(fill="x")

        self.btn_fetch_now = ttk.Button(
            ctrls,
            text=t("main.btn_fetch_now", default="Fetch Now"),
            command=self._fetch_now,
            width=14,
        )
        self.btn_fetch_now.pack(side="left")

        self.btn_loop = ttk.Button(
            ctrls,
            text=t("main.btn_pause_loop", default="Pause Loop"),
            command=self._toggle_loop,
            width=14,
        )
        self.btn_loop.pack(side="left", padx=8)

        self.btn_clear_cache = ttk.Button(
            ctrls,
            text=t("main.btn_clear_cache", default="Clear Cache"),
            command=self._clear_cache,
            width=14,
        )
        self.btn_clear_cache.pack(side="left")

        ttk.Button(
            ctrls,
            text=t("main.btn_refresh", default="Refresh"),
            command=self._refresh_dashboard,
            width=12,
        ).pack(side="left", padx=8)

        # Progress row — only visible while a fetch is running. Hidden by
        # default via grid_remove() so it doesn't reserve vertical space.
        progress_row = ttk.Frame(outer, padding=(0, 6, 0, 0))
        progress_row.pack(fill="x")
        progress_row.grid_columnconfigure(1, weight=1)
        self.progress_label = ttk.Label(
            progress_row, text="", foreground=COLOR_MUTED, width=18, anchor="w",
        )
        self.progress_label.grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.progress_bar = ttk.Progressbar(
            progress_row, mode="indeterminate", length=200,
        )
        self.progress_bar.grid(row=0, column=1, sticky="ew")
        self.progress_bar.grid_remove()  # hidden until _fetch_now starts

        # Log tail
        log_header = ttk.Frame(outer)
        log_header.pack(fill="x", pady=(12, 4))
        self._log_label = ttk.Label(
            log_header,
            text=t("main.log_label", default="Recent log (logs/app.log)"),
            foreground=COLOR_MUTED,
        )
        self._log_label.pack(side="left", anchor="w")
        self.btn_clear_log = ttk.Button(
            log_header,
            text=t("main.btn_clear_log", default="Clear Log"),
            command=self._clear_log,
            width=12,
        )
        self.btn_clear_log.pack(side="right")

        log_frame = ttk.Frame(outer)
        log_frame.pack(fill="both", expand=True)
        self.log_text = tk.Text(log_frame, height=10, wrap="none", font=("Consolas", 9))
        self.log_text.configure(state="disabled")
        log_vsb = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_vsb.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        log_vsb.pack(side="right", fill="y")

        # --------------------------------------------------------- Recent Articles
        # Resizable pane (drag the sash above to grow/shrink). Shows the latest
        # articles pushed via the gui_queue ("new_article" events).
        # Features:
        #   * Single click   → mark as read (de-bold)
        #   * Double click   → open ArticleViewer (same handler as toast)
        #   * Hover          → tooltip with summary
        #   * Filter combobox → All / Unread / Read
        #   * Buttons        → mark all read / unread, clear list

        # Resizable container: top = log tail (fill), bottom = articles pane.
        # tk.PanedWindow lets the user drag the divider to resize the panes.
        self._dashboard_panes = tk.PanedWindow(
            outer, orient="vertical", sashwidth=6, sashrelief="raised",
            opaqueresize=True, bd=0,
        )
        self._dashboard_panes.pack(fill="both", expand=True)

        # --- Top pane: log tail (already built above) -------------------------
        log_holder = ttk.Frame(self._dashboard_panes)
        self._dashboard_panes.add(log_holder, stretch="always", minsize=80)
        log_holder_inner = ttk.Frame(log_holder)
        log_holder_inner.pack(fill="both", expand=True)
        # Re-parent the existing log_text + scrollbar into log_holder_inner.
        # We can't change Tk master at runtime; instead we recreate a Text
        # widget inside the new holder. The old widget is destroyed.
        old_log_text = self.log_text
        old_log_vsb = log_vsb
        self.log_text = tk.Text(
            log_holder_inner, height=10, wrap="none", font=("Consolas", 9),
        )
        self.log_text.configure(state="disabled")
        self.log_text.pack(in_=log_holder_inner, side="left", fill="both", expand=True)
        new_vsb = ttk.Scrollbar(
            log_holder_inner, orient="vertical", command=self.log_text.yview
        )
        self.log_text.configure(yscrollcommand=new_vsb.set)
        new_vsb.pack(in_=log_holder_inner, side="right", fill="y")
        try:
            old_log_text.destroy()
        except Exception:
            pass
        try:
            old_log_vsb.destroy()
        except Exception:
            pass

        # --- Bottom pane: recent articles ------------------------------------
        art_holder = ttk.Frame(self._dashboard_panes)
        self._dashboard_panes.add(art_holder, stretch="never", minsize=140)
        art_holder_inner = ttk.Frame(art_holder)
        art_holder_inner.pack(fill="both", expand=True)

        art_header = ttk.Frame(art_holder_inner)
        art_header.pack(fill="x", pady=(8, 4))
        ttk.Label(
            art_header,
            text=t("main.recent_articles", default="Recent Articles"),
            foreground=COLOR_MUTED,
        ).pack(side="left", anchor="w")

        # Filter + actions row
        actions = ttk.Frame(art_header)
        actions.pack(side="right")

        self._article_filter_var = tk.StringVar(value="all")
        self._article_filter_combo = ttk.Combobox(
            actions,
            textvariable=self._article_filter_var,
            values=["all", "unread", "read"],
            width=10,
            state="readonly",
        )
        self._article_filter_combo.pack(side="left", padx=(0, 6))
        self._article_filter_combo.bind(
            "<<ComboboxSelected>>", lambda _e: self._refresh_recent_articles_view()
        )

        ttk.Button(
            actions, text="Mark all read", width=12,
            command=lambda: self._mark_all_read(True),
        ).pack(side="left", padx=2)
        ttk.Button(
            actions, text="Mark all unread", width=13,
            command=lambda: self._mark_all_read(False),
        ).pack(side="left", padx=2)
        ttk.Button(
            actions, text="Clear", width=8,
            command=self._clear_recent_articles,
        ).pack(side="left", padx=2)

        # Treeview — supports per-row font weights via tags, multi-column.
        tree_frame = ttk.Frame(art_holder_inner)
        tree_frame.pack(fill="both", expand=True)
        self.articles_tree = ttk.Treeview(
            tree_frame,
            columns=("status", "commodity", "title"),
            show="headings",
            selectmode="browse",
            height=8,
        )
        self.articles_tree.heading("status", text="")
        self.articles_tree.heading("commodity", text="Commodity")
        self.articles_tree.heading("title", text="Title")
        self.articles_tree.column("status", width=70, stretch=False, anchor="w")
        self.articles_tree.column("commodity", width=140, stretch=False, anchor="w")
        self.articles_tree.column("title", width=400, stretch=True, anchor="w")
        art_tv_vsb = ttk.Scrollbar(
            tree_frame, orient="vertical", command=self.articles_tree.yview
        )
        self.articles_tree.configure(yscrollcommand=art_tv_vsb.set)
        self.articles_tree.pack(side="left", fill="both", expand=True)
        art_tv_vsb.pack(side="right", fill="y")

        # Visual tags — unread = bold + dark text, read = normal weight.
        self.articles_tree.tag_configure("unread", font=("Segoe UI", 9, "bold"))
        self.articles_tree.tag_configure("read", font=("Segoe UI", 9))
        self.articles_tree.tag_configure("urgent", foreground="#C42B1C")
        self.articles_tree.tag_configure("price", foreground="#1565C0")
        self.articles_tree.tag_configure("news", foreground="#1F1F1F")

        # Bindings: click toggles read; double-click opens viewer; hover shows
        # tooltip with summary.
        self.articles_tree.bind("<Button-1>", self._on_recent_click)
        self.articles_tree.bind("<Double-Button-1>", self._on_recent_article_open)
        self.articles_tree.bind("<Return>", self._on_recent_article_open)
        self.articles_tree.bind("<Motion>", self._on_recent_motion)
        self.articles_tree.bind("<Leave>", self._on_recent_leave)
        # Keyboard shortcuts: u = mark unread, r = mark read
        self.articles_tree.bind("<Key-r>", lambda _e: self._mark_selected_read(True))
        self.articles_tree.bind("<Key-u>", lambda _e: self._mark_selected_read(False))

        # Tooltip overlay — created lazily, hidden until hover
        self._article_tip = None
        self._article_tip_after_id = None
        self._article_tip_article_id = None

        # In-memory ring buffer of (article_id, article, status_label,
        # commodity, title, read_bool). Capped at 50 so the tree stays fast.
        self._recent_articles: list = []
        self._recent_articles_max = 50

        # Subscribe to gui_queue so we receive our own copy of every event
        # (independent of the tray's Tk thread subscriber). Without this,
        # the tray's poll loop and our poll loop would race for the same
        # `queue.Queue` item and the loser would never see the event.
        self._gui_subscriber = None
        try:
            from desktop.gui_queue import gui_queue
            self._gui_subscriber = gui_queue.subscribe("main_window")
        except Exception as e:
            logger.debug("gui_queue subscribe failed: %s", e)

    # ----------------------------------------------------------- Recent Articles API
    def _article_key(self, article) -> str:
        """Stable identifier for an article — used to track read state."""
        try:
            return article.compute_hash()
        except Exception:
            try:
                return f"{article.title}|{article.link}"
            except Exception:
                return str(id(article))

    def _on_recent_click(self, event):
        """Single click on a row toggles its read state and selects it."""
        row = self.articles_tree.identify_row(event.y)
        if not row:
            return
        self.articles_tree.selection_set(row)
        # Toggle read on click — click to "I've seen this"
        self._mark_selected_read(True)

    def _on_recent_motion(self, event):
        """Show a tooltip with the article summary after hovering 400ms."""
        row = self.articles_tree.identify_row(event.y)
        if not row:
            self._hide_article_tip()
            return
        try:
            idx = int(row)
        except (TypeError, ValueError):
            return
        if idx < 0 or idx >= len(self._recent_articles):
            return
        _art_id, article, _status, _commodity, _title, _read = self._recent_articles[idx]
        # Cancel any pending tooltip request, schedule a new one.
        if self._article_tip_after_id is not None:
            try:
                self.window.after_cancel(self._article_tip_after_id)
            except Exception:
                pass
        self._article_tip_article_id = id(article)
        self._article_tip_after_id = self.window.after(
            400, lambda: self._show_article_tip(article, event)
        )

    def _on_recent_leave(self, _event=None):
        self._hide_article_tip()

    def _show_article_tip(self, article, event):
        self._article_tip_after_id = None
        try:
            if self._article_tip is None or not self._article_tip.winfo_exists():
                self._article_tip = tk.Toplevel(self.window)
                self._article_tip.wm_overrideredirect(True)
                self._article_tip_label = tk.Label(
                    self._article_tip,
                    text="",
                    justify="left",
                    background="#FFF8DC",
                    foreground="#1F1F1F",
                    relief="solid",
                    borderwidth=1,
                    font=("Segoe UI", 9),
                    padx=8,
                    pady=6,
                    wraplength=420,
                )
                self._article_tip_label.pack()
            summary = (article.summary or article.title or "").strip()
            if not summary:
                summary = "(no summary)"
            lines = []
            commodity = (
                article.triggered_commodity
                or (article.commodity_tags[0] if article.commodity_tags else None)
                or article.source
                or "News"
            )
            lines.append(f"[{commodity}] {article.source}")
            if article.is_price_alert and article.triggered_price is not None:
                lines.append(
                    f"Detected: {article.triggered_price:.2f} USD"
                )
            lines.append("")
            lines.append(summary[:600] + ("..." if len(summary) > 600 else ""))
            self._article_tip_label.configure(text="\n".join(lines))
            # Position near the cursor but inside the window
            x = self.window.winfo_pointerx() + 14
            y = self.window.winfo_pointery() + 18
            self._article_tip.wm_geometry(f"+{x}+{y}")
            self._article_tip.deiconify()
        except Exception as e:
            logger.debug("tooltip show failed: %s", e)

    def _hide_article_tip(self):
        if self._article_tip_after_id is not None:
            try:
                self.window.after_cancel(self._article_tip_after_id)
            except Exception:
                pass
            self._article_tip_after_id = None
        if self._article_tip is not None:
            try:
                self._article_tip.withdraw()
            except Exception:
                pass

    def _selected_recent_index(self):
        sel = self.articles_tree.selection()
        if not sel:
            return None
        try:
            return int(sel[0])
        except (TypeError, ValueError):
            return None

    def _mark_selected_read(self, read: bool):
        idx = self._selected_recent_index()
        if idx is None or idx < 0 or idx >= len(self._recent_articles):
            return
        art_id, article, status, commodity, title, _read = self._recent_articles[idx]
        self._recent_articles[idx] = (art_id, article, status, commodity, title, read)
        self._refresh_recent_articles_view()

    def _mark_all_read(self, read: bool):
        if not self._recent_articles:
            return
        self._recent_articles = [
            (a, b, c, d, e, read)
            for (a, b, c, d, e, _r) in self._recent_articles
        ]
        self._refresh_recent_articles_view()

    def _clear_recent_articles(self):
        self._recent_articles.clear()
        self._refresh_recent_articles_view()

    def _refresh_recent_articles_view(self):
        """Re-render the Treeview according to current filter and read state."""
        try:
            flt = (self._article_filter_var.get() or "all").lower()
        except Exception:
            flt = "all"
        try:
            self.articles_tree.delete(*self.articles_tree.get_children())
        except tk.TclError:
            return
        for idx, (_art_id, article, status, commodity, title, read) in enumerate(
            self._recent_articles
        ):
            if flt == "unread" and read:
                continue
            if flt == "read" and not read:
                continue
            # tag = (unread|read) + (urgent|price|news)
            kind = (
                "price" if getattr(article, "is_price_alert", False)
                else "urgent" if getattr(article, "is_urgent", False)
                else "news"
            )
            tag = ("unread" if not read else "read", kind)
            self.articles_tree.insert(
                "", "end", iid=str(idx),
                values=(status, commodity, title),
                tags=tag,
            )

    def _on_recent_article_open(self, _event=None):
        """Double-click / Enter → open ArticleViewer for the selected row."""
        idx = self._selected_recent_index()
        if idx is None or idx < 0 or idx >= len(self._recent_articles):
            return
        _art_id, article, _status, _commodity, _title, _read = self._recent_articles[idx]
        if self.on_open_article:
            try:
                self.on_open_article(article)
            except Exception as e:
                logger.warning("on_open_article failed: %s", e)
        # Opening the viewer implicitly marks the article as read.
        self._mark_selected_read(True)

    def _add_recent_article(self, article) -> None:
        """Push an article into the Recent Articles panel (newest first).

        De-duplicates by article hash so the same article pushed multiple
        times doesn't spam the list.
        """
        try:
            commodity = (
                article.triggered_commodity
                or (article.commodity_tags[0] if article.commodity_tags else None)
                or article.source
                or "News"
            )
            if article.is_price_alert:
                status = "[PRICE]"
            elif article.is_urgent:
                status = "[URGENT]"
            else:
                status = "[NEWS]"
            title = (article.title or "").replace("\n", " ")
            if len(title) > 110:
                title = title[:107] + "..."
        except Exception:
            status, commodity, title = "[?]", "?", "(article)"

        art_id = self._article_key(article)
        # Dedupe: if an article with the same hash is already present at the
        # top, skip the push.
        if self._recent_articles and self._recent_articles[0][0] == art_id:
            return
        self._recent_articles.insert(0, (art_id, article, status, commodity, title, False))
        if len(self._recent_articles) > self._recent_articles_max:
            self._recent_articles = self._recent_articles[: self._recent_articles_max]
        self._refresh_recent_articles_view()

    def _poll_gui_queue(self) -> None:
        """Drain gui_queue events that affect this window.

        We only handle ``new_article`` here — ``show_history``, ``show_main``,
        etc. belong to the tray's Tk root, not the MainWindow.

        The gui_queue is now a pub/sub broadcaster: each Tk thread
        (tray + MainWindow) gets its own subscriber queue, so we no
        longer race against the tray's Tk thread over who gets to
        consume the event first.
        """
        try:
            from desktop.gui_queue import gui_queue
        except Exception:
            return
        events = self._gui_subscriber.drain() if self._gui_subscriber else gui_queue.drain()
        for event in events:
            if not event:
                continue
            etype, payload = event
            if etype in ("new_article", "toast"):
                if payload is not None:
                    self._add_recent_article(payload)
            elif etype == "fetch_started":
                # A scheduled run kicked in — show the progress bar so the
                # user can see the dashboard is busy. The "fetch_done" event
                # below will close it.
                self._show_progress(reason=str(payload or "scheduled"))
            elif etype == "fetch_done":
                # The scheduler-driven path forwards ``job_execute``'s
                # stats dict (with "sent", "skipped", "errors", etc.).
                # The Fetch Now button path forwards ``{"error": str|None}``
                # so we can show a "Fetch failed" label if the worker
                # crashed. Both code paths share this handler.
                err = None
                if isinstance(payload, dict):
                    e = payload.get("error")
                    if e:
                        err = Exception(e)
                self._hide_progress(err=err)
                # ``_on_fetch_done`` re-enables the Fetch Now button and
                # refreshes the dashboard counters. Calling it here
                # guarantees the UI state is reset even if the worker
                # thread was the Fetch Now button (which previously
                # used an unsafe ``Tk.after`` from a background thread).
                self._on_fetch_done(err)

    def _make_card(self, parent, title, value):
        frame = tk.Frame(parent, bg=CARD_BG, highlightthickness=1, highlightbackground=CARD_BORDER)
        ttk.Label(frame, text=title, foreground=COLOR_MUTED, background=CARD_BG).pack(
            anchor="w", padx=12, pady=(10, 0)
        )
        val_lbl = ttk.Label(frame, text=value, font=("Segoe UI", 18, "bold"), background=CARD_BG)
        val_lbl.pack(anchor="w", padx=12, pady=(0, 2))
        # Optional sub-label rendered below the main value, used by the Next Run
        # card to show the current scheduler mode and slot. Hidden by default.
        sub_lbl = ttk.Label(frame, text="", foreground=COLOR_MUTED, background=CARD_BG,
                            font=("Segoe UI", 8))
        sub_lbl.pack(anchor="w", padx=12, pady=(0, 10))
        frame._value_label = val_lbl  # type: ignore[attr-defined]
        frame._sub_label = sub_lbl    # type: ignore[attr-defined]
        return frame

    def _set_card(self, frame, value, color=None, sub=None):
        lbl = frame._value_label  # type: ignore[attr-defined]
        lbl.configure(text=str(value))
        if color:
            lbl.configure(foreground=color)
        if sub is not None:
            sub_lbl = getattr(frame, "_sub_label", None)
            if sub_lbl is not None:
                sub_lbl.configure(text=str(sub))

    def _refresh_dashboard(self):
        try:
            total = database.count_sent()
        except Exception as e:
            logger.debug("count_sent failed: %s", e)
            total = 0
        try:
            today = database.count_today() if hasattr(database, "count_today") else 0
        except Exception:
            today = 0
        try:
            commodities = sum(1 for c in self.config.get("commodities", []) if c.get("enabled", True))
        except Exception:
            commodities = 0

        self._set_card(self.card_total_sent, str(total))
        self._set_card(self.card_today, str(today))
        self._set_card(self.card_prices, str(commodities))
        main, sub = self._next_run_display()
        self._set_card(self.card_next_run, main, sub=sub)

        self._poll_gui_queue()
        self._reload_log_tail()
        # Schedule next refresh (1s — the countdown needs sub-minute granularity)
        try:
            self.window.after(1000, self._refresh_dashboard)
        except tk.TclError:
            pass

    def _scheduler_slot_label(self, now: Optional[datetime] = None) -> str:
        """Return a short text describing the current scheduler slot.

        Returns one of (depending on mode and time):
            - "Mode: fixed • 5 min"
            - "Mode: adaptive • Business hours (3 min)"
            - "Mode: adaptive • Evening (10 min)"
            - "Mode: adaptive • Weekend (30 min)"
            - "Mode: adaptive • Off (weekend)"   (when off_on_weekend + weekend)
        """
        try:
            from zoneinfo import ZoneInfo

            scheduler_cfg = (self.config or {}).get("scheduler") or {}
            mode = scheduler_cfg.get("mode", "adaptive")
            if mode == "fixed":
                fixed = int(scheduler_cfg.get("fixed_interval_minutes", 5))
                return f"Mode: fixed \u2022 {fixed} min"

            tz_name = (
                (scheduler_cfg.get("adaptive") or {}).get("timezone")
                or (self.config or {}).get("general", {}).get("timezone", "Asia/Ho_Chi_Minh")
            )
            try:
                tz = ZoneInfo(tz_name)
            except Exception:
                tz = ZoneInfo("UTC")
            adaptive = scheduler_cfg.get("adaptive") or {}
            now = (now or datetime.now()).astimezone(tz)
            is_weekend = now.weekday() >= 5
            if is_weekend and adaptive.get("off_on_weekend", False):
                return "Mode: adaptive \u2022 Off (weekend)"
            if is_weekend:
                return f"Mode: adaptive \u2022 Weekend ({int(adaptive.get('weekend', 30))} min)"
            hour = now.hour
            start = int(adaptive.get("business_hours_start", 8))
            end = int(adaptive.get("business_hours_end", 20))
            if start <= hour < end:
                return (
                    f"Mode: adaptive \u2022 Business hours "
                    f"({int(adaptive.get('weekday_business_hours', 3))} min)"
                )
            return f"Mode: adaptive \u2022 Evening ({int(adaptive.get('weekday_evening', 10))} min)"
        except Exception:
            return ""

    def _next_run_display(self) -> tuple:
        """Return (main_text, sub_text) for the Next Run card.

        Main text is the mm:ss countdown; sub text is the mode/slot label.
        Either can be "--:--" / "" respectively when the loop is paused.
        """
        return self._next_run_text(), self._scheduler_slot_label()

    def _next_run_text(self) -> str:
        """Compute a human-readable mm:ss countdown to the next scheduled run.

        Returns "--:--" if the loop is paused or the scheduler/job is unknown.
        Returns "Running..." when a fetch is in flight, and "Rescheduling..."
        when the next run time is in the past but the scheduler hasn't yet
        pushed a fresh trigger (APScheduler sometimes lags a tick).

        If the next run is more than 15s in the past and the loop is running,
        we also fire a manual fetch (same handler as the "Fetch Now" button)
        so the scheduler can't silently stall.
        """
        if not self._loop_running:
            return "--:--"
        if self.scheduler is None:
            return "--:--"
        if self._fetch_in_flight:
            return "Running\u2026"
        try:
            job = self.scheduler.get_job(self.job_id)
        except Exception:
            job = None
        if job is None or job.next_run_time is None:
            return "--:--"
        try:
            now = datetime.now(timezone.utc) if job.next_run_time.tzinfo else datetime.now()
            nrt = job.next_run_time
            delta = (nrt - now).total_seconds()
        except Exception:
            return "--:--"
        if delta <= -15:
            # Scheduler is more than 15s late publishing the new
            # next_run_time. Trigger a manual fetch so the user doesn't have
            # to click "Fetch Now" by hand. We dedupe with a flag so we only
            # fire once per staleness episode.
            self._maybe_auto_fetch(reason="scheduler-stalled")
            return "Rescheduling\u2026"
        if delta <= -5:
            return "Rescheduling\u2026"
        if delta <= 0:
            # Within a 5s window — likely the job is firing right now.
            return "Running\u2026"
        minutes, seconds = divmod(int(delta), 60)
        if minutes >= 60:
            hours, minutes = divmod(minutes, 60)
            return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        return f"{minutes:02d}:{seconds:02d}"

    def _maybe_auto_fetch(self, reason: str = ""):
        """Kick off a background fetch if one isn't already in flight.

        Used by the dashboard when the scheduler appears stuck. The flag
        resets the moment a fetch starts, so we won't fire repeatedly.
        """
        if self._fetch_in_flight:
            return
        if not (self.on_fetch_now or self.on_run_now):
            return
        logger.info("Auto-fetch triggered (%s)", reason or "manual")
        try:
            self._fetch_now(reason="auto")
        except Exception as e:
            logger.debug("auto fetch_now failed: %s", e)

    def set_loop_state(self, running: bool):
        """Update the loop toggle button label + Next-Run card state.

        Called by the host when the scheduler is paused/resumed externally
        (e.g. by the system tray menu).
        """
        self._loop_running = bool(running)
        try:
            self.btn_loop.configure(
                text=t("main.btn_resume_loop", default="Resume Loop")
                if not self._loop_running
                else t("main.btn_pause_loop", default="Pause Loop")
            )
        except tk.TclError:
            pass
        # Force a refresh so the countdown reflects the new state immediately
        main, sub = self._next_run_display()
        self._set_card(self.card_next_run, main, sub=sub)

    def _reload_log_tail(self):
        # Pick the newest app*.log file (handles post-Clear-Log rotation).
        from utils.logger import latest_log_path
        log_path = latest_log_path()
        if log_path is None:
            return
        try:
            # Read last ~80 lines efficiently
            with open(log_path, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                block = 8192 if size > 8192 else size
                f.seek(size - block)
                tail = f.read().decode("utf-8", errors="replace")
            lines = tail.splitlines()[-80:]
            self.log_text.configure(state="normal")
            self.log_text.delete("1.0", "end")
            self.log_text.insert("1.0", "\n".join(lines))
            self.log_text.see("end")
            self.log_text.configure(state="disabled")
            # Refresh the header label so user can tell which file they're
            # looking at (matters after Clear Log rotates the file).
            try:
                self._log_label.configure(
                    text=f"Recent log ({log_path.name})"
                )
            except tk.TclError:
                pass
        except Exception as e:
            logger.debug("log tail refresh failed: %s", e)

    def _fetch_now(self, reason: str = "manual"):
        """Single-shot pipeline run on a worker thread. Does NOT touch the
        scheduler loop. UI stays responsive: the Fetch Now button shows a
        progress bar + label while the worker runs.

        ``reason`` is "manual" when triggered by the button click and
        "auto" when triggered by the scheduler-stall watchdog — the label
        text reflects this so the user knows why the fetch is running.

        Implementation note: ``Tk.after`` is **not** thread-safe when
        called from a background thread on some Tk builds (the callback
        silently never fires, which leaves the progress bar spinning
        forever). Instead we use the existing ``gui_queue`` broadcast
        channel that the scheduler-driven ``job_execute`` already uses
        to signal completion — the main-thread poll loop in
        ``_poll_gui_events`` handles ``fetch_done`` and re-enables the
        button + hides the progress bar.
        """
        if self._fetch_in_flight:
            return
        self._fetch_in_flight = True
        try:
            self.btn_fetch_now.configure(state="disabled")
        except tk.TclError:
            pass
        self._show_progress(reason=reason)

        def _worker():
            """Run on a background thread. ``job_execute`` itself publishes
            ``fetch_started`` + ``fetch_done`` to ``gui_queue`` — we only
            need to make sure errors here also surface to the main thread.
            """
            err = None
            try:
                if self.on_fetch_now:
                    self.on_fetch_now()
                elif self.on_run_now:
                    self.on_run_now()
            except Exception as e:
                err = e
                logger.exception("Fetch worker crashed: %s", e)

            # Signal completion to the main thread via the SAME queue
            # the scheduler uses. This is a thread-safe round-trip and
            # works regardless of whether the worker ran the full
            # pipeline or short-circuited via an exception.
            try:
                from desktop.gui_queue import gui_queue
                gui_queue.put(("fetch_done", {"error": str(err) if err else None}))
            except Exception:
                # Last resort: log + fall back to the unsafe ``after``
                # only if the Tk thread is still alive. If even that
                # fails the user can always click the button again —
                # the next click will refire because
                # ``_fetch_in_flight`` is reset in
                # ``_handle_fetch_done`` (which is now poll-driven).
                logger.warning(
                    "Could not publish fetch_done to gui_queue; trying after()."
                )
                try:
                    self.window.after(0, self._on_fetch_done, err)
                except Exception:
                    pass

        threading.Thread(target=_worker, daemon=True, name="fetch-now").start()

    def _on_fetch_done(self, err):
        """Called on the Tk thread when the fetch worker finishes."""
        self._fetch_in_flight = False
        try:
            self.btn_fetch_now.configure(state="normal")
        except tk.TclError:
            pass
        try:
            self.progress_bar.stop()
            self.progress_bar.grid_remove()
            if err is None:
                self.progress_label.configure(text=t("main.fetch_done", default="Fetch complete"))
            else:
                self.progress_label.configure(
                    text=t("main.fetch_failed", default="Fetch failed: {err}").format(err=err)
                )
            # Clear the "Fetch complete / failed" label after a few seconds
            self.window.after(4000, lambda: self._safe_label_clear())
        except tk.TclError:
            pass
        # Refresh counters + countdown + log tail immediately
        self._refresh_dashboard()

    def _safe_label_clear(self):
        try:
            self.progress_label.configure(text="")
        except tk.TclError:
            pass

    def _show_progress(self, reason: str = "scheduled"):
        """Show the progress bar + label without touching _fetch_in_flight.

        Used both by the Fetch Now button (via _fetch_now) and by the
        scheduler's "fetch_started" gui_queue event so that scheduled
        runs also show the same animated progress bar.
        """
        try:
            self.progress_bar.grid()
            self.progress_bar.start(12)
            label_text = (
                t("main.fetch_progress_auto", default="Auto-fetching\u2026 (scheduler)")
                if reason == "auto" or reason == "scheduled"
                else t("main.fetch_progress", default="Fetching\u2026")
            )
            self.progress_label.configure(text=label_text)
        except tk.TclError:
            pass

    def _hide_progress(self, err=None):
        """Hide the progress bar and refresh the dashboard. Safe to call
        from any Tk context (button handler, scheduled-run callback, …)"""
        try:
            self.progress_bar.stop()
            self.progress_bar.grid_remove()
            if err is None:
                self.progress_label.configure(
                    text=t("main.fetch_done", default="Fetch complete")
                )
            else:
                self.progress_label.configure(
                    text=t("main.fetch_failed", default="Fetch failed: {err}").format(err=err)
                )
            self.window.after(4000, self._safe_label_clear)
        except tk.TclError:
            pass
        try:
            self._refresh_dashboard()
        except Exception:
            pass

    def _unlock_fetch(self):
        # Backwards-compat shim — kept for callers that may still schedule it.
        self._on_fetch_done(None)

    def _clear_log(self):
        """Rotate the current app.log to app_<timestamp>.log and start fresh.

        The Dashboard will immediately switch to reading the new (empty) log
        file. The old log is preserved in `logs/app_<timestamp>.log` for
        audit / debugging.
        """
        from utils.logger import rotate_log_file

        archive = None
        try:
            archive = rotate_log_file()
        except Exception as e:
            logger.warning("rotate_log_file failed: %s", e)
        if archive is None:
            # Fallback: at least wipe the on-screen tail
            try:
                self.log_text.configure(state="normal")
                self.log_text.delete("1.0", "end")
                self.log_text.configure(state="disabled")
            except tk.TclError:
                pass
            return
        # Force an immediate tail reload so the dashboard shows the empty
        # fresh file right away (don't wait up to 1s for the periodic refresh).
        self._reload_log_tail()

    def _toggle_loop(self):
        """Flip the scheduler loop on/off. The host callback receives the
        target state (True = running, False = paused)."""
        target = not self._loop_running
        if self.on_toggle_loop:
            try:
                self.on_toggle_loop(target)
            except Exception as e:
                messagebox.showerror("ThaoResearch", f"Toggle loop failed: {e}")
                return
        # Optimistically update UI; host can call set_loop_state() later to
        # confirm.
        self.set_loop_state(target)

    def _clear_cache(self):
        if not self.on_clear_cache:
            messagebox.showinfo(
                "ThaoResearch",
                "Clear cache is not wired in this build.",
            )
            return
        if not messagebox.askyesno(
            "Clear cache",
            "Delete data/history.db, data/prices.db and all logs?\n"
            "This cannot be undone.",
        ):
            return
        try:
            results = self.on_clear_cache() or []
        except Exception as e:
            messagebox.showerror("ThaoResearch", f"Clear cache failed: {e}")
            return
        removed = sum(1 for _, status in results if status.startswith("removed"))
        messagebox.showinfo(
            "ThaoResearch",
            f"Cache cleared. Removed {removed} item(s).",
        )
        # Repaint counters + log tail
        self._refresh_dashboard()

    # ----------------------------------------------------------- History
    def _build_history_tab(self):
        from desktop.history_window import build_widget

        self._history_widget = build_widget(
            self.history_frame, on_open_viewer=self._on_open_article_clicked
        )
        self._history_widget.pack(fill="both", expand=True)

    def _on_open_article_clicked(self, article):
        if self.on_open_article:
            try:
                self.on_open_article(article)
            except Exception as e:
                logger.warning("on_open_article failed: %s", e)

    # ---------------------------------------------------------------- Misc
    def _status_text(self, saved=False):
        lang = self.config.get("general", {}).get("language", "en")
        return (
            f"{t('main.status_ready', default='Ready')} | "
            f"{t('main.lang', default='Language')}: {lang} | "
            f"{t('main.config', default='Config')}: {self.config_path}"
            + (" | " + t("main.saved_indicator", default="Saved") if saved else "")
        )

    def _on_close(self):
        # Hide instead of destroying so tray can re-show it.
        try:
            self.window.withdraw()
        except tk.TclError:
            pass

    def show(self):
        """Run Tk mainloop on the current thread."""
        try:
            self.window.mainloop()
        finally:
            try:
                self.window.destroy()
            except tk.TclError:
                pass

    def show_window(self):
        """Show the window (used when triggered by tray)."""
        try:
            self.window.deiconify()
            self.window.lift()
            self.window.focus_force()
        except tk.TclError:
            pass

    def start_in_thread(self) -> threading.Thread:
        """Start the Tk mainloop in a background thread and return it."""
        t = threading.Thread(target=self.show, daemon=True, name="main-window")
        t.start()
        return t


def open_main_window(
    config: dict,
    config_path: Path,
    on_run_now: Optional[Callable[[], None]] = None,
    on_fetch_now: Optional[Callable[[], None]] = None,
    on_toggle_loop: Optional[Callable[[bool], None]] = None,
    on_clear_cache: Optional[Callable[[], list]] = None,
    on_open_article: Optional[Callable[[object], None]] = None,
    on_config_saved: Optional[Callable[[dict], None]] = None,
    scheduler: Optional[Any] = None,
    job_id: str = "main_job",
):
    """Convenience helper for opening MainWindow as a standalone Tk window."""
    win = MainWindow(
        config=config,
        config_path=config_path,
        on_run_now=on_run_now,
        on_fetch_now=on_fetch_now,
        on_toggle_loop=on_toggle_loop,
        on_clear_cache=on_clear_cache,
        on_open_article=on_open_article,
        on_config_saved=on_config_saved,
        scheduler=scheduler,
        job_id=job_id,
    )
    win.show()