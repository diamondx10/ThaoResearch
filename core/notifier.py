"""Notifier — orchestrates Telegram + Desktop GUI queue.

The actual Windows toast notification is fired by the gui_queue
subscribers (``main.py._dispatch`` / ``desktop.tray._dispatch_event``)
when they receive a ``new_article`` event. Doing the toast there —
rather than here in the worker thread — guarantees the toast fires
**exactly once per article** no matter how many Tk subscribers are
running (tray, main window, both). The worker just records intent;
the Tk side is responsible for the side-effects.

Keeping the toast on the Tk side also has the side benefit of avoiding
fire-and-forget WinRT calls from a non-Tk thread, which on some
Windows builds crash silently inside ``windows_toasts``.
"""
from __future__ import annotations

import logging
from typing import List, Optional

from core.telegram_bot import TelegramNotifier
from desktop.gui_queue import gui_queue
from models.article import Article
from utils import database

logger = logging.getLogger("thaoresearch.core.notifier")


class Notifier:
    def __init__(self, telegram: Optional[TelegramNotifier], desktop_enabled: bool = True):
        self.telegram = telegram
        self.desktop_enabled = desktop_enabled

    def send_alert(self, article: Article, dry_run: bool = False, force: bool = False) -> bool:
        """Send a single article via Telegram + Desktop GUI.

        Returns True on success (any channel). Skips duplicates by hash
        unless ``force=True`` (in which case the dedup table is bypassed
        and the article is re-sent).

        The Windows toast itself is fired by the gui_queue subscribers
        (see module docstring) — so it runs exactly once per article
        instead of N times (1× here + 1× per Tk subscriber).
        """
        h = article.compute_hash()
        if not force and database.is_sent(h):
            logger.info("Skipped duplicate: %s", article.title[:60])
            return False

        sent_telegram = False
        sent_desktop = False

        if dry_run:
            logger.info("[DRY-RUN] would send: %s", article.title[:60])
            # In dry-run mode, still display on desktop if available (for visibility).
            if self.desktop_enabled:
                gui_queue.put(("new_article", article))
                sent_desktop = True
        else:
            # Send to Telegram first (background, blocking on async result)
            if self.telegram and self.telegram._app:
                sent_telegram = self.telegram.send_article(article)
                if sent_telegram:
                    logger.info("Telegram sent: %s", article.title[:60])
                else:
                    logger.warning("Telegram send failed: %s", article.title[:60])
            # Push to desktop GUI. Tk subscribers (tray + main window) will
            # each call ``show_toast`` ONCE for this event — but they do it
            # from the *same* Tk subscriber set, so each subscriber
            # dedupes against a shared in-memory cache so the user only
            # sees ONE toast banner for this article.
            if self.desktop_enabled:
                gui_queue.put(("new_article", article))
                sent_desktop = True
                logger.info("Desktop queue pushed: %s", article.title[:60])

        # Record in DB (so we don't re-send even if one channel fails)
        try:
            database.mark_sent(
                article_hash=h,
                article_title=article.title,
                article_url=article.link,
                commodity=article.triggered_commodity or (article.commodity_tags[0] if article.commodity_tags else ""),
                is_price_alert=article.is_price_alert,
                is_urgent=article.is_urgent,
                source=article.source,
                pub_date=article.pub_date,
                sent_to_telegram=sent_telegram,
                sent_to_desktop=sent_desktop,
                article_summary=article.summary or "",
            )
        except Exception as e:
            logger.error("Failed to mark article as sent: %s", e)

        return sent_telegram or sent_desktop

    def send_alerts(self, articles: List[Article], dry_run: bool = False) -> dict:
        """Send multiple articles. Returns stats dict."""
        stats = {"sent": 0, "skipped": 0, "errors": 0}
        for article in articles:
            try:
                ok = self.send_alert(article, dry_run=dry_run)
                if ok:
                    stats["sent"] += 1
                else:
                    stats["skipped"] += 1
            except Exception as e:
                stats["errors"] += 1
                logger.exception("Notifier error for %s: %s", article.title[:60], e)
        return stats
