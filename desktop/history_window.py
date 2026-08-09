"""History window — table view of all sent articles.

Can be used as a standalone Toplevel (via class + show()) or as an
embeddable widget via `build_widget(parent)` that returns a frame
hosting the same controls.
"""
from __future__ import annotations

import logging
import tkinter as tk
from datetime import datetime
from tkinter import ttk
from typing import Callable, Optional

from desktop.article_viewer import open_viewer
from desktop.window_icon import apply_app_icon
from models.article import Article
from utils import database
from utils.i18n import t

logger = logging.getLogger("thaoresearch.desktop.history_window")

COLOR_PRICE = "#2E7D32"
COLOR_URGENT = "#C42B1C"
COLOR_KEYWORD = "#6B6B6B"

COLUMNS = ("time", "commodity", "status", "title", "source")


def _status_for_row(is_price: int, is_urgent: int) -> tuple:
    """Return (label, color) for the Status column."""
    if is_urgent:
        return (t("status.urgent"), COLOR_URGENT)
    if is_price:
        return (t("status.price_alert"), COLOR_PRICE)
    return (t("status.keyword_match"), COLOR_KEYWORD)


def _row_to_article(row: tuple) -> Article:
    """Reconstruct an Article from a sent_articles row."""
    from zoneinfo import ZoneInfo

    pub_date_str = row[7]  # pub_date column
    pub_date = datetime.now(ZoneInfo("Asia/Ho_Chi_Minh"))
    if pub_date_str:
        try:
            dt = datetime.fromisoformat(pub_date_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=ZoneInfo("UTC"))
            pub_date = dt.astimezone(ZoneInfo("Asia/Ho_Chi_Minh"))
        except (TypeError, ValueError):
            pass

    return Article(
        title=row[1] or "",
        link=row[2] or "",
        pub_date=pub_date,
        summary="",
        source=row[6] or "",
        commodity_tags=[row[3]] if row[3] else [],
        triggered_commodity=row[3],
        is_urgent=bool(row[5]),
        is_price_alert=bool(row[4]),
    )


def build_widget(parent: tk.Misc, on_open_viewer: Optional[Callable[[Article], None]] = None) -> ttk.Frame:
    """Build a History table inside `parent` (e.g. a ttk.Frame). Returns the frame.

    Exposes `widget.refresh()` so callers can reload data on demand.
    """
    frame = ttk.Frame(parent)

    # --- Top: filter + search + refresh ---
    top = ttk.Frame(frame, padding=(12, 10))
    top.pack(fill="x")

    ttk.Label(top, text="Filter:").pack(side="left")
    filter_var = tk.StringVar(value=t("history.filter_all", default="All"))
    filter_combo = ttk.Combobox(
        top,
        textvariable=filter_var,
        values=[
            t("history.filter_all"),
            t("status.price_alert"),
            t("status.urgent"),
            t("status.keyword_match"),
        ],
        state="readonly",
        width=15,
    )
    filter_combo.pack(side="left", padx=(5, 16))

    ttk.Label(top, text="Search:").pack(side="left")
    search_var = tk.StringVar()
    search_entry = ttk.Entry(top, textvariable=search_var, width=30)
    search_entry.pack(side="left", padx=(5, 16))

    def do_refresh():
        refresh()

    filter_combo.bind("<<ComboboxSelected>>", lambda _e: do_refresh())
    search_entry.bind("<Return>", lambda _e: do_refresh())
    ttk.Button(top, text=t("history.refresh", default="Refresh"), command=do_refresh).pack(
        side="left"
    )

    # --- Table ---
    table_frame = ttk.Frame(frame, padding=(12, 0, 12, 0))
    table_frame.pack(fill="both", expand=True)

    tree = ttk.Treeview(
        table_frame,
        columns=COLUMNS,
        show="headings",
        height=20,
    )
    for col, label, width, anchor in (
        ("time", t("history.col_time"), 150, "w"),
        ("commodity", t("history.col_commodity"), 120, "w"),
        ("status", t("history.col_status"), 100, "w"),
        ("title", t("history.col_title"), 450, "w"),
        ("source", t("history.col_source"), 80, "w"),
    ):
        tree.heading(col, text=label)
        tree.column(col, width=width, anchor=anchor, stretch=(col == "title"))

    tree.tag_configure("urgent", foreground=COLOR_URGENT)
    tree.tag_configure("price", foreground=COLOR_PRICE)
    tree.tag_configure("keyword", foreground=COLOR_KEYWORD)

    vsb = ttk.Scrollbar(table_frame, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=vsb.set)
    tree.pack(side="left", fill="both", expand=True)
    vsb.pack(side="right", fill="y")

    def _on_double_click(_event):
        sel = tree.selection()
        if not sel:
            return
        try:
            rows = database.get_history(limit=500)
        except Exception:
            return
        iid_index = sel[0]
        children = tree.get_children()
        if iid_index not in children:
            return
        idx = children.index(iid_index)
        if idx >= len(rows):
            return
        article = _row_to_article(rows[idx])
        if on_open_viewer:
            try:
                on_open_viewer(article)
            except Exception as e:
                logger.warning("on_open_viewer failed: %s", e)
        else:
            open_viewer(article, parent=parent)

    tree.bind("<Double-1>", _on_double_click)

    # --- Footer ---
    bottom = ttk.Frame(frame, padding=(12, 8, 12, 12))
    bottom.pack(fill="x")
    ttk.Label(bottom, text=t("history.double_click_hint")).pack(side="left")

    def refresh():
        for iid in tree.get_children():
            tree.delete(iid)
        chosen = filter_var.get()
        if chosen == t("status.price_alert"):
            status_filter = "price"
        elif chosen == t("status.urgent"):
            status_filter = "urgent"
        elif chosen == t("status.keyword_match"):
            status_filter = "keyword"
        else:
            status_filter = None

        search = search_var.get().strip() or None
        try:
            rows = database.get_history(limit=500, status_filter=status_filter, search=search)
        except Exception as e:
            logger.exception("Failed to load history: %s", e)
            rows = []

        if not rows:
            tree.insert("", "end", values=("-", "-", "-", t("history.empty"), "-"))
            return

        for row in rows:
            is_price = int(row[4] or 0)
            is_urgent = int(row[5] or 0)
            label, _color = _status_for_row(is_price, is_urgent)
            tag = "urgent" if is_urgent else ("price" if is_price else "keyword")
            time_str = row[8] or ""  # sent_at
            try:
                dt = datetime.fromisoformat(time_str)
                time_str = dt.strftime("%Y-%m-%d %H:%M")
            except (TypeError, ValueError):
                pass
            tree.insert(
                "",
                "end",
                values=(time_str, row[3] or "-", label, row[1] or "", row[6] or "-"),
                tags=(tag,),
            )

    # Initial load
    refresh()

    # Expose refresh for the host to call later (e.g. after Save in Settings tab).
    frame.refresh = refresh  # type: ignore[attr-defined]
    frame.tree = tree  # type: ignore[attr-defined]
    return frame


class HistoryWindow:
    """Standalone Toplevel hosting the History table."""

    def __init__(
        self,
        on_open_viewer: Optional[Callable[[Article], None]] = None,
        parent: Optional[tk.Misc] = None,
    ):
        self.on_open_viewer = on_open_viewer
        self._owns_root = parent is None
        if self._owns_root:
            self.window = tk.Tk()
        else:
            self.window = tk.Toplevel(parent)
            try:
                self.window.transient(parent)
            except Exception:
                pass
        self.window.title(t("history.title", default="History - ThaoResearch"))
        self.window.geometry("1000x600")
        self.window.minsize(800, 400)
        apply_app_icon(self.window)
        self._widget = build_widget(self.window, on_open_viewer=on_open_viewer)
        self._widget.pack(fill="both", expand=True)
        self._widget.refresh()

    def show(self):
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


def open_history(parent: Optional[tk.Misc] = None):
    HistoryWindow(parent=parent).show()