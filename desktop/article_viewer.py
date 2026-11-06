"""Article Viewer window — text-only Tkinter window with HTML rendering."""
from __future__ import annotations

import logging
import threading
import time
from desktop import theme
import tkinter as tk
import webbrowser
from datetime import datetime
from tkinter import ttk
from typing import Callable, Optional

from desktop.window_icon import apply_app_icon
from models.article import Article
from utils.i18n import t

logger = logging.getLogger("thaoresearch.desktop.article_viewer")




def _format_dt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S %z")


#: Viewers currently on screen. Two jobs:
#:
#:  1. Cascade. Every viewer used to be centred on the same spot, so two
#:     articles opened from two notifications landed exactly on top of each
#:     other and looked like one window.
#:  2. Keep a reference. A non-blocking viewer has no local left holding it,
#:     and once the Python object is collected its callbacks die with it.
#:
#: Entries are removed when the window is destroyed, so the cascade walks
#: back to the start as the user closes things.
_open_viewers: "list" = []

#: Pixels each successive window is shifted, and how many steps before the
#: cascade returns to the origin.
_CASCADE_STEP = 34
_CASCADE_WRAP = 6


def open_count() -> int:
    """How many viewers are currently on screen (used by tests)."""
    return len(_open_viewers)


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
        # No geometry here any more: _fit_to_content() sets it once the
        # widgets exist and their real size is known.
        self.window.minsize(460, 320)
        self.window.configure(bg=theme.COLORS["surface"])
        apply_app_icon(self.window)
        # Force the window to be mapped on screen immediately. Without this,
        # a Toplevel of a withdrawn parent takes a tick before Tk maps it,
        # during which the user sees nothing (especially when `show()` calls
        # `wait_window()` right after construction).
        try:
            self.window.update_idletasks()
        except Exception:
            pass

        # Register before building: _fit_to_content() reads the count to
        # work out this window's cascade offset.
        _open_viewers.append(self)
        try:
            self.window.bind("<Destroy>", self._on_destroy, add="+")
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
        status_color = theme.COLORS["success"] if self.article.is_price_alert else theme.COLORS["fg_muted"]
        status_line_frame = ttk.Frame(header)
        status_line_frame.pack(anchor="w", pady=(2, 0))
        ttk.Label(status_line_frame, text=f"{t('viewer.status_label', default='Status:')}       {status_text}").pack(
            side="left"
        )
        if self.article.is_urgent:
            ttk.Label(
                status_line_frame,
                text=f"  -  {t('status.urgent')}",
                foreground=theme.COLORS["danger"],
                font=theme.font("subheading"),
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
            font=theme.font("title"),
            bg=theme.COLORS["surface"],
            fg=theme.COLORS["fg"],
            wraplength=640,
            justify="left",
            anchor="w",
        )
        title_label.pack(fill="x", pady=(0, 8))

        # Scrollable Text widget for the article body. Per UX request we
        # show ONLY the short RSS summary (no async full-content fetch).
        # The "Open in browser" button is the way to read the whole article.
        text_frame = ttk.Frame(body_frame)
        text_frame.pack(fill="both", expand=True)
        self._body_text = tk.Text(
            text_frame,
            font=theme.font("body"),
            bg=theme.COLORS["surface"],
            fg=theme.COLORS["fg"],
            wrap="word",
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
            padx=2,
            pady=4,
        )
        theme.style_text_widget(self._body_text)
        body_scroll = ttk.Scrollbar(
            text_frame, orient="vertical", command=self._body_text.yview
        )
        self._body_text.configure(yscrollcommand=body_scroll.set)
        self._body_text.pack(side="left", fill="both", expand=True)
        body_scroll.pack(side="right", fill="y")

        # Disable editing but allow selection + copy.
        self._body_text.configure(state="disabled")

        summary = (self.article.summary or "").strip()
        if summary:
            self._set_body_text(summary)
        else:
            self._set_body_text(
                t(
                    "viewer.no_summary",
                    default="(No summary available for this article. Bấm 'Open in browser' để mở bài gốc.)",
                )
            )

        # Mark that no full-content fetch will be kicked off so the legacy
        # _start_full_content_fetch / _poll_content_queue handlers are no-ops.
        self._fetch_in_flight = False

        ttk.Separator(body_frame, orient="horizontal").pack(fill="x", pady=(8, 4))

        link_label = tk.Label(
            body_frame,
            text=self.article.link,
            font=theme.font("link"),
            bg=theme.COLORS["surface"],
            fg=theme.COLORS["accent"],
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

        # Everything exists now, so the window can be sized to it.
        self._wrap_targets = (title_label, link_label)
        self._body_scroll = body_scroll
        self._text_frame = text_frame
        self._fit_to_content()
        self.window.bind("<Configure>", self._on_resize)

    # -------------------------------------------------------- adaptive layout
    #: Comfortable reading column. Long titles do not widen the window --
    #: they wrap -- so this stays fixed and only the height responds.
    _WIDTH = 740
    _MIN_H = 320
    _MAX_H = 980
    #: Body lines below/above which the Text stops shrinking or growing.
    _MIN_BODY_LINES = 3
    _MAX_BODY_LINES = 26

    def _wrapped_line_count(self, text: str, pixel_width: int) -> int:
        """How many display lines ``text`` needs at ``pixel_width``.

        Measured with the body font rather than guessed from a character
        count, so it holds for Vietnamese and Korean too.
        """
        import tkinter.font as tkfont

        try:
            font = tkfont.Font(font=self._body_text.cget("font"))
            sample = "abcdefghijklmnopqrstuvwxyz ABCDEFGHIJKLMNOPQRSTUVWXYZ"
            avg_char = max(1.0, font.measure(sample) / len(sample))
        except Exception:
            avg_char = 7.0
        per_line = max(20, int(pixel_width / avg_char))
        lines = 0
        for paragraph in (text or "").split("\n"):
            length = len(paragraph.strip())
            lines += 1 if length == 0 else -(-length // per_line)  # ceil
        return max(1, lines)

    def _fit_to_content(self) -> None:
        """Size the window to the article instead of a fixed 700x800.

        A two-line RSS blurb used to open the same 800px-tall window as a
        long story, leaving most of it empty, while a long piece got that
        same window and simply scrolled. Now the body Text is given a line
        count measured from the real text, the window asks Tk how tall that
        makes it, and the result is clamped so it is never cramped, never
        taller than the screen, and never taller than it needs to be.

        The scrollbar is packed only when the body actually overflows, so a
        short article shows no dead scrollbar track.
        """
        try:
            body = self._body_text.get("1.0", "end-1c")
        except Exception:
            body = ""

        usable = self._WIDTH - 72  # window padding + scrollbar gutter
        needed = self._wrapped_line_count(body, usable)
        shown = max(self._MIN_BODY_LINES, min(needed, self._MAX_BODY_LINES))

        try:
            self._body_text.configure(height=shown)
        except Exception:
            pass

        # Only show the scrollbar when there is something to scroll to.
        # Unpack first and re-pack if needed rather than testing
        # winfo_ismapped(): before the window is mapped that reports False
        # even for an already-packed widget, so the dead scrollbar survived.
        try:
            self._body_scroll.pack_forget()
            if needed > shown:
                self._body_scroll.pack(side="right", fill="y")
        except Exception:
            pass

        self._apply_wraplength(self._WIDTH)

        try:
            self.window.update_idletasks()
            req_h = self.window.winfo_reqheight()
            screen_h = self.window.winfo_screenheight()
            height = max(self._MIN_H, min(req_h, self._MAX_H, screen_h - 120))

            # Centre on screen, then cascade: two articles opened from two
            # notifications must not land on the same pixel, or they look
            # like a single window and the user has to close one to find
            # the other.
            screen_w = self.window.winfo_screenwidth()
            step = (max(0, len(_open_viewers) - 1) % _CASCADE_WRAP) * _CASCADE_STEP
            x = max(0, (screen_w - self._WIDTH) // 2 + step)
            y = max(0, (screen_h - height) // 3 + step)
            # Never push a window off the bottom/right of the screen.
            x = min(x, max(0, screen_w - self._WIDTH))
            y = min(y, max(0, screen_h - height - 40))
            self.window.geometry(f"{self._WIDTH}x{height}+{x}+{y}")
        except Exception:
            self.window.geometry(f"{self._WIDTH}x700")

    def _apply_wraplength(self, window_width: int) -> None:
        """Keep the title and link wrapping to the window, not to 640px.

        Both labels used a hardcoded wraplength, so widening the window
        left a ragged 640px-wide column of text with empty space beside it.
        """
        wrap = max(260, window_width - 80)
        for label in getattr(self, "_wrap_targets", ()):  # title, link
            try:
                label.configure(wraplength=wrap)
            except Exception:
                pass

    def _on_resize(self, event) -> None:
        """Reflow on user resize. Only reacts to the window's own events."""
        if event.widget is not self.window:
            return
        if getattr(self, "_last_width", None) == event.width:
            return
        self._last_width = event.width
        self._apply_wraplength(event.width)

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

    def _on_destroy(self, event=None):
        """Drop out of the open-viewer registry once the window is gone.

        ``<Destroy>`` also fires for every child widget, so only act on the
        event for the toplevel itself.
        """
        try:
            if event is not None and event.widget is not self.window:
                return
        except Exception:
            pass
        try:
            _open_viewers.remove(self)
        except ValueError:
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

    def show(self, block: bool = True):
        """Put the viewer on screen.

        ``block=True`` (the default, kept for existing callers) waits until
        the window is closed. ``block=False`` returns immediately.

        Anything opening viewers from an event dispatch must pass
        ``block=False``. ``wait_window`` does not return until the user
        closes that window, so a dispatcher that blocked on the first
        article could not get to the second: opening "Read more" on two
        notifications appeared to produce one window, and the second only
        turned up after the first was dismissed.
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
        if not block:
            return self
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
        return self


def open_viewer(
    article: Article,
    on_send_telegram: Optional[Callable[[Article], None]] = None,
    parent: Optional[tk.Misc] = None,
):
    """Convenience helper to open a viewer (Toplevel of parent when given)."""
    viewer = ArticleViewer(article, on_send_telegram=on_send_telegram, parent=parent)
    viewer.show()
