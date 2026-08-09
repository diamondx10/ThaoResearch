"""Notifier — orchestrates Telegram + Desktop GUI queue + Windows toast.

On Windows the ``desktop_enabled`` flag also fires a real Windows
toast notification (via ``desktop.toast``), independent of the tray
icon. That way the toast still appears when the app is running as a
plain daemon (``--daemon``) without any tray.
"""
from __future__ import annotations

import logging
from typing import List, Optional

from core.telegram_bot import TelegramNotifier
from desktop.gui_queue import gui_queue
from models.article import Article
from utils import database

logger = logging.getLogger("thaoresearch.core.notifier")


def _try_show_toast(article: Article) -> bool:
    """Best-effort: show a Windows toast for ``article``.

    Failures are logged at DEBUG level — toasts are UX, never fatal.
    Returns True if a toast backend reported success.
    """
    try:
        from desktop.toast import show_toast

        return bool(show_toast(article))
    except Exception as e:  # pragma: no cover - never fatal
        logger.debug("Toast dispatch failed: %s", e)
        return False


class Notifier:
    def __init__(self, telegram: Optional[TelegramNotifier], desktop_enabled: bool = True):
        self.telegram = telegram
        self.desktop_enabled = desktop_enabled

    def send_alert(self, article: Article, dry_run: bool = False, force: bool = False) -> bool:
        """Send a single article via Telegram + Desktop GUI + Windows toast.

        Returns True on success (any channel). Skips duplicates by hash
        unless ``force=True`` (in which case the dedup table is bypassed
        and the article is re-sent).
        """
        h = article.compute_hash()
        if not force and database.is_sent(h):
            logger.info("Skipped duplicate: %s", article.title[:60])
            return False

        sent_telegram = False
        sent_desktop = False
        sent_toast = False

        if dry_run:
            logger.info("[DRY-RUN] would send: %s", article.title[:60])
            # In dry-run mode, still display on desktop if available (for visibility)
            if self.desktop_enabled:
                gui_queue.put(("new_article", article))
                sent_desktop = True
                sent_toast = _try_show_toast(article)
        else:
            # Send to Telegram first (background, blocking on async result)
            if self.telegram and self.telegram._app:
                sent_telegram = self.telegram.send_article(article)
                if sent_telegram:
                    logger.info("Telegram sent: %s", article.title[:60])
                else:
                    logger.warning("Telegram send failed: %s", article.title[:60])
            # Push to desktop GUI + fire native toast (if enabled)
            if self.desktop_enabled:
                gui_queue.put(("new_article", article))
                sent_desktop = True
                logger.info("Desktop queue pushed: %s", article.title[:60])
                # Show native Windows toast so the user sees the alert
                # even if the tray icon / dashboard window isn't open.
                sent_toast = _try_show_toast(article)

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
                sent_to_desktop=sent_desktop or sent_toast,
                article_summary=article.summary or "",
            )
        except Exception as e:
            logger.error("Failed to mark article as sent: %s", e)

        return sent_telegram or sent_desktop or sent_toast

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
