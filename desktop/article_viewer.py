"""Article Viewer window — text-only Tkinter window with HTML rendering."""
from __future__ import annotations

import logging
import threading
import time
import tkinter as tk
import webbrowser
from datetime import datetime
from tkinter import ttk
from typing import Callable, Optional

from desktop.window_icon import apply_app_icon
from models.article import Article
from utils.i18n import t

logger = logging.getLogger("thaoresearch.desktop.article_viewer")


COLOR_BG = "#FFFFFF"
COLOR_FG = "#1F1F1F"
COLOR_MUTED = "#6B6B6B"
COLOR_BORDER = "#E0E0E0"
COLOR_SUCCESS = "#2E7D32"
COLOR_DANGER = "#C42B1C"


def _format_dt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S %z")


class ArticleViewer:
    """Window showing one article with full metadata and action buttons.

    If ``parent`` is supplied the viewer is a Toplevel of that window (preferred
    — works correctly across multiple open/close cycles and avoids the
    "second viewer doesn't open" bug caused by stacking multiple Tk() roots).
    Otherwise a transient Tk root is created (legacy standalone mode).
    """

    def __init__(
        self,
        article: Article,
        on_send_telegram: Optional[Callable[[Article], None]] = None,
        on_next: Optional[Callable[[], None]] = None,
        parent: Optional[tk.Misc] = None,
    ):
        self.article = article
        self.on_send_telegram = on_send_telegram
        self.on_next = on_next
        self._owns_root = parent is None
        # Track whether we should mark the parent transient. We skip this when
        # the parent is withdrawn (hidden) — on Windows, a Toplevel with
        # ``transient(withdrawn_root)`` will not appear on screen because the
        # parent's invisibility suppresses the child's display. The tray's
        # hidden Tk root is the most common case where this matters.
        parent_is_withdrawn = False
        if self._owns_root:
            self.window = tk.Tk()
        else:
            self.window = tk.Toplevel(parent)
            try:
                parent_is_withdrawn = bool(parent.winfo_viewable() == 0)
            except Exception:
                parent_is_withdrawn = False
            if not parent_is_withdrawn:
                try:
                    self.window.transient(parent)
                except Exception:
                    pass
            else:
                logger.debug(
                    "Parent is withdrawn — skipping transient() so viewer "
                    "remains visible"
                )
        self.window.title("Article - ThaoResearch")
        self.window.geometry("700x800")
        self.window.minsize(500, 600)
        self.window.configure(bg=COLOR_BG)
        apply_app_icon(self.window)
        # Force the window to be mapped on screen immediately. Without this,
        # a Toplevel of a withdrawn parent takes a tick before Tk maps it,
        # during which the user sees nothing (especially when `show()` calls
        # `wait_window()` right after construction).
        try:
            self.window.update_idletasks()
        except Exception:
            pass
        self._build_ui()

    def _build_ui(self):
        # Header frame (metadata)
        header = ttk.Frame(self.window, padding=(20, 16, 20, 8))
        header.pack(fill="x")

        commodity = self.article.triggered_commodity or (
            self.article.commodity_tags[0] if self.article.commodity_tags else "-"
        )

        ttk.Label(header, text=f"{t('viewer.commodity_label', default='Commodity:')}    {commodity}").pack(anchor="w")

        status_text = t("status.price_alert") if self.article.is_price_alert else t("status.keyword_match")
        status_color = COLOR_SUCCESS if self.article.is_price_alert else COLOR_MUTED
        status_line_frame = ttk.Frame(header)
        status_line_frame.pack(anchor="w", pady=(2, 0))
        ttk.Label(status_line_frame, text=f"{t('viewer.status_label', default='Status:')}       {status_text}").pack(
            side="left"
        )
        if self.article.is_urgent:
            ttk.Label(
                status_line_frame,
                text=f"  -  {t('status.urgent')}",
                foreground=COLOR_DANGER,
                font=("Segoe UI", 10, "bold"),
            ).pack(side="left")

        ttk.Label(
            header, text=f"{t('viewer.source_label')}    {self.article.source}"
        ).pack(anchor="w", pady=(2, 0))
        ttk.Label(
            header, text=f"{t('viewer.published_label')} {_format_dt(self.article.pub_date)}"
        ).pack(anchor="w", pady=(2, 0))

        if self.article.is_price_alert and self.article.triggered_price is not None:
            ttk.Label(
                header,
                text=f"{t('viewer.price_label')}   {self.article.triggered_price:.2f} USD",
            ).pack(anchor="w", pady=(2, 0))

        if self.article.commodity_tags:
            ttk.Label(
                header, text=f"{t('viewer.tags_label')}      {', '.join(self.article.commodity_tags)}"
            ).pack(anchor="w", pady=(2, 0))

        ttk.Separator(header, orient="horizontal").pack(fill="x", pady=(10, 0))

        # Body — title + scrollable full-article content
        body_frame = ttk.Frame(self.window, padding=(20, 12, 20, 4))
        body_frame.pack(fill="both", expand=True)

        title_label = tk.Label(
            body_frame,
            text=self.article.title,
            font=("Segoe UI", 16, "bold"),
            bg=COLOR_BG,
            fg=COLOR_FG,
            wraplength=640,
            justify="left",
            anchor="w",
        )
        title_label.pack(fill="x", pady=(0, 8))

        # Scrollable Text widget for the full article body. We start with the
        # RSS summary so the user sees something immediately, then replace it
        # with the fetched full content when the background HTTP request
        # completes.
        text_frame = ttk.Frame(body_frame)
        text_frame.pack(fill="both", expand=True)
        self._body_text = tk.Text(
            text_frame,
            font=("Segoe UI", 11),
            bg=COLOR_BG,
            fg=COLOR_FG,
            wrap="word",
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
            padx=2,
            pady=4,
        )
        body_scroll = ttk.Scrollbar(
            text_frame, orient="vertical", command=self._body_text.yview
        )
        self._body_text.configure(yscrollcommand=body_scroll.set)
        self._body_text.pack(side="left", fill="both", expand=True)
        body_scroll.pack(side="right", fill="y")

        # Disable editing but allow selection + copy.
        self._body_text.configure(state="disabled")

        initial_body = (self.article.summary or "").strip()
        if not initial_body:
            initial_body = t(
                "viewer.loading_content",
                default="Đang tải nội dung bài báo…",
            )
        self._set_body_text(initial_body)

        # Asynchronously fetch the full article body so the user can read the
        # whole page inline (instead of just the short RSS description).
        # Falls back to the existing summary if the fetch fails. We schedule
        # the fetch via ``after`` so it only fires once Tk is in its main
        # loop — otherwise the worker thread can't marshal results back via
        # ``window.after(0, ...)``.
        self._fetch_in_flight = False
        if self.article.link:
            try:
                self.window.after(150, self._start_full_content_fetch)
            except tk.TclError:
                # Window destroyed already — skip silently.
                pass

        ttk.Separator(body_frame, orient="horizontal").pack(fill="x", pady=(8, 4))

        link_label = tk.Label(
            body_frame,
            text=self.article.link,
            font=("Segoe UI", 9, "underline"),
            bg=COLOR_BG,
            fg="#0066CC",
            cursor="hand2",
            wraplength=640,
            justify="left",
            anchor="w",
        )
        link_label.pack(fill="x")
        link_label.bind("<Button-1>", lambda _e: self._open_browser())

        # Buttons
        button_frame = ttk.Frame(self.window, padding=(20, 10, 20, 16))
        button_frame.pack(fill="x")
        ttk.Button(
            button_frame,
            text=t("viewer.btn_open_browser"),
            command=self._open_browser,
            width=20,
        ).pack(side="left", padx=4)
        if self.on_next:
            ttk.Button(
                button_frame,
                text=t("viewer.btn_next"),
                command=self._on_next_article,
                width=15,
            ).pack(side="left", padx=4)
        ttk.Button(
            button_frame,
            text=t("viewer.btn_close"),
            command=self._close,
            width=10,
        ).pack(side="right", padx=4)

    # ---------------------------------------------------------------- helpers
    def _set_body_text(self, body: str) -> None:
        """Replace the body Text widget contents and re-disable editing."""
        try:
            self._body_text.configure(state="normal")
            self._body_text.delete("1.0", "end")
            self._body_text.insert("1.0", body or "")
            self._body_text.configure(state="disabled")
        except tk.TclError:
            # Window was destroyed before we got here.
            pass

    def _start_full_content_fetch(self) -> None:
        """Kick off a background thread that downloads + extracts the full
        article body, then pushes it back onto the Tk thread for display.

        Tk widgets can only be touched from the main thread, so we don't
        use ``window.after()`` directly from the worker — that crashes with
        ``RuntimeError: main thread is not in main loop`` if the fetch
        completes before the Tk mainloop starts pumping events. Instead
        we stash the result in a thread-safe queue and let a small
        ``_poll_content_queue`` handler run on the Tk thread pick it up.
        """
        if self._fetch_in_flight:
            return
        self._fetch_in_flight = True
        import queue as _queue

        self._content_queue: _queue.Queue = _queue.Queue()
        url = self.article.link

        def worker():
            try:
                from core.content_fetcher import fetch_full_content
                full = fetch_full_content(url)
            except Exception as e:
                logger.debug("full-content fetch crashed for %s: %s", url, e)
                full = None
            try:
                self._content_queue.put_nowait(full)
            except Exception:
                pass

        threading.Thread(target=worker, daemon=True, name="full-content").start()
        # Begin polling the result queue from the Tk thread.
        try:
            self.window.after(200, self._poll_content_queue)
        except tk.TclError:
            pass

    def _poll_content_queue(self) -> None:
        """Drain the worker→Tk queue and update the body Text widget."""
        try:
            full = self._content_queue.get_nowait()
        except Exception:
            full = None
            got_result = False
        else:
            got_result = True
        if got_result:
            self._fetch_in_flight = False
            self._on_full_content_loaded(full)
            return
        # Not done yet — keep polling until we hit a timeout (~20s).
        if not hasattr(self, "_poll_started"):
            self._poll_started = time.time()
        if time.time() - getattr(self, "_poll_started", time.time()) > 20:
            logger.debug("full-content fetch timed out")
            self._fetch_in_flight = False
            return
        try:
            self.window.after(250, self._poll_content_queue)
        except tk.TclError:
            pass

    def _on_full_content_loaded(self, full: Optional[str]) -> None:
        """Called on the Tk thread once the background fetch completes."""
        self._fetch_in_flight = False
        if not full:
            # Show a soft notice so the user knows the fetch failed but keep
            # the original summary visible.
            existing = (self.article.summary or "").strip()
            if existing:
                notice = t(
                    "viewer.full_content_unavailable",
                    default="\n\n(Không tải được nội dung đầy đủ — bấm 'Open in browser' để mở bài gốc.)",
                )
                self._set_body_text(existing + notice)
            return
        self._set_body_text(full)

    def _close(self):
        """Destroy this viewer's window and, if we own the root, the Tk root too."""
        try:
            self.window.destroy()
        except tk.TclError:
            pass
        if self._owns_root:
            try:
                # When we created the Tk root, quit the interpreter so a
                # subsequent ArticleViewer() can spin up a fresh root cleanly.
                self.window.quit()
            except tk.TclError:
                pass

    def _on_close_x(self):
        """Handle the WM_DELETE_WINDOW event.

        Default Tk behaviour when the user clicks the X is to destroy the
        widget. For Toplevel children of a host Tk root that is itself
        running a mainloop (e.g. the main window) that's fine. But for a
        Toplevel parented to a withdrawn (hidden) root, X still works just
        the same — Tk destroys the widget and ``wait_window`` returns.
        """
        try:
            self.window.destroy()
        except tk.TclError:
            pass

    def _open_browser(self):
        if self.article.link:
            try:
                webbrowser.open(self.article.link)
            except Exception as e:
                logger.warning("Failed to open browser: %s", e)

    def _on_next_article(self):
        self._close()
        if self.on_next:
            self.on_next()

    def show(self):
        """Block the calling thread until the viewer is closed.

        For Toplevel viewers, ``wait_window`` keeps the window interactive
        without spinning up a nested mainloop (which would deadlock if the
        caller is already on the Tk thread).
        """
        # On Windows, a Toplevel whose parent is withdrawn will not steal
        # focus or appear on top of other windows. Make sure the viewer
        # is deiconified, lifted, and focused so the user actually sees it.
        try:
            self.window.deiconify()
            self.window.lift()
            self.window.focus_force()
        except tk.TclError:
            pass
        if self._owns_root:
            try:
                self.window.mainloop()
            finally:
                try:
                    self.window.destroy()
                except tk.TclError:
                    pass
        else:
            try:
                self.window.wait_window()
            except tk.TclError:
                pass


def open_viewer(
    article: Article,
    on_send_telegram: Optional[Callable[[Article], None]] = None,
    parent: Optional[tk.Misc] = None,
):
    """Convenience helper to open a viewer (Toplevel of parent when given)."""
    viewer = ArticleViewer(article, on_send_telegram=on_send_telegram, parent=parent)
    viewer.show()
