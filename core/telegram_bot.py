"""Telegram Bot API wrapper with retry + exponential backoff.

Uses the `python-telegram-bot` library (v20.x) under the hood. Falls back to a
direct `requests.post` implementation when the bot cannot be initialized
(e.g., empty token), so that notifier code can still run in degraded mode.
"""
from __future__ import annotations

import asyncio
import logging
import re
import threading
from typing import List, Optional, Tuple

from models.article import Article

logger = logging.getLogger("thaoresearch.core.telegram_bot")

TOKEN_PATTERN = re.compile(r"^\d+:[A-Za-z0-9_-]{35,}$")

MAX_RETRIES = 3
BACKOFF_BASE = 2  # 1s, 2s, 4s

# Telegram message size limit (Markdown mode)
TELEGRAM_MSG_LIMIT = 4096


def validate_token(token: str) -> bool:
    """Return True if token matches the standard BotFather format."""
    if not token:
        return False
    return bool(TOKEN_PATTERN.match(token.strip()))


def validate_chat_id(chat_id: str) -> bool:
    """Return True if chat_id looks like an integer (positive or negative)."""
    if not chat_id:
        return False
    try:
        int(str(chat_id).strip())
        return True
    except (TypeError, ValueError):
        return False


class TelegramNotifier:
    """Wrapper around python-telegram-bot's Application for sending messages.

    Gracefully degrades to a no-op when token/chat_id are missing — call sites
    can simply call `send_article()` and check the returned bool.
    """

    def __init__(self, token: str, chat_id: str):
        self.token = (token or "").strip()
        self.chat_id = (chat_id or "").strip()
        self._app = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop_thread: Optional[threading.Thread] = None
        self._polling_started = False

        if not self.token or not self.chat_id:
            logger.warning("Telegram credentials missing — alerts will be skipped")
            return
        if not validate_token(self.token):
            logger.warning("Telegram token has invalid format — alerts will be skipped")
            return

        try:
            from telegram import Bot  # noqa: F401
            from telegram.ext import Application

            self._app = Application.builder().token(self.token).build()
            logger.info("TelegramNotifier initialized (chat_id=%s)", self._mask(self.chat_id))
        except Exception as e:
            logger.error("Failed to init TelegramNotifier: %s", e)
            self._app = None

    # ------------------------------------------------------------------ public

    def send_article(self, article: Article) -> bool:
        """Send one article. Returns True on success."""
        if not self._app:
            return False
        text = article.to_telegram_message()
        markup = article.to_telegram_reply_markup()
        return self.send_message(text, reply_markup=markup)

    def send_message(self, text: str, reply_markup=None) -> bool:
        """Send a raw message with retry. Returns True on success."""
        if not self._app:
            return False
        loop = self._ensure_loop()
        if loop is None:
            return False
        coro = self._send_with_retries(text, reply_markup=reply_markup)
        try:
            future = asyncio.run_coroutine_threadsafe(coro, loop)
            return bool(future.result(timeout=30))
        except Exception as e:
            logger.error("Telegram send_message failed: %s", e)
            return False

    def test_connection(self) -> Tuple[bool, str]:
        """Verify token + chat_id by calling getMe + sendMessage.

        Returns (ok, error_message). When ok=True, error_message is empty.
        """
        if not self._app:
            return (False, "Telegram credentials missing or token format invalid")
        loop = self._ensure_loop()
        if loop is None:
            return (False, "Could not start async loop")
        try:
            future = asyncio.run_coroutine_threadsafe(self._test(), loop)
            ok, msg = future.result(timeout=20)
            return (ok, msg)
        except Exception as e:
            return (False, str(e))

    def shutdown(self) -> None:
        """Stop the background event loop (call at app exit)."""
        if self._app and self._polling_started:
            try:
                future = asyncio.run_coroutine_threadsafe(
                    self._app.updater.stop(), self._loop
                )
                future.result(timeout=5)
            except Exception:
                pass
            try:
                future = asyncio.run_coroutine_threadsafe(
                    self._app.stop(), self._loop
                )
                future.result(timeout=5)
            except Exception:
                pass
            self._polling_started = False
        if self._loop and not self._loop.is_closed():
            try:
                self._loop.call_soon_threadsafe(self._loop.stop)
            except Exception:
                pass

    # ----------------------------------------------------------------- internal

    def _ensure_loop(self) -> Optional[asyncio.AbstractEventLoop]:
        """Run a background asyncio loop in a dedicated thread."""
        if self._loop and not self._loop.is_closed():
            return self._loop
        try:
            self._loop = asyncio.new_event_loop()

            def runner():
                asyncio.set_event_loop(self._loop)
                self._loop.run_forever()

            self._loop_thread = threading.Thread(target=runner, daemon=True, name="tg-loop")
            self._loop_thread.start()
            return self._loop
        except Exception as e:
            logger.error("Failed to start asyncio loop: %s", e)
            return None

    async def _send_with_retries(self, text: str, reply_markup=None) -> bool:
        """Async: try to send the message with exponential backoff."""
        from telegram.error import (
            BadRequest as TG_BadRequest,
            ChatMigrated,
            Forbidden,
            NetworkError as TGNetworkError,
            RetryAfter,
            TimedOut,
        )

        last_error: Optional[Exception] = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                await self._app.bot.send_message(
                    chat_id=self.chat_id,
                    text=text[:TELEGRAM_MSG_LIMIT],
                    parse_mode="Markdown",
                    disable_web_page_preview=False,
                    reply_markup=reply_markup,
                )
                return True
            except RetryAfter as e:
                wait = int(getattr(e, "retry_after", BACKOFF_BASE ** attempt))
                logger.warning("Telegram RetryAfter: wait %ss", wait)
                await asyncio.sleep(wait)
                last_error = e
            except (TimedOut, TGNetworkError) as e:
                last_error = e
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(BACKOFF_BASE ** attempt)
            except Forbidden as e:
                # v20.7: Forbidden covers both 401 Unauthorized and 403 Forbidden
                logger.error("Telegram 401/403: %s", e)
                return False
            except ChatMigrated as e:
                logger.error("Telegram chat migrated: %s", e)
                return False
            except TG_BadRequest as e:
                msg = str(e).lower()
                if "chat not found" in msg or "chat_id" in msg and "invalid" in msg:
                    logger.error("Telegram chat not found: %s", e)
                    return False
                # Try once more without Markdown (might be a parse error)
                if attempt == 1:
                    try:
                        await self._app.bot.send_message(
                            chat_id=self.chat_id,
                            text=text[:TELEGRAM_MSG_LIMIT],
                            disable_web_page_preview=False,
                            reply_markup=reply_markup,
                        )
                        return True
                    except Exception as e2:
                        last_error = e2
                else:
                    logger.error("Telegram BadRequest: %s", e)
                    return False
            except Exception as e:
                last_error = e
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(BACKOFF_BASE ** attempt)
        logger.error("Telegram retries exhausted: %s", last_error)
        return False

    async def _test(self) -> Tuple[bool, str]:
        """Async: validate the connection."""
        from telegram.error import (
            ChatMigrated,
            Forbidden,
            NetworkError as TGNetworkError,
            TimedOut,
        )
        try:
            me = await self._app.bot.get_me()
            await self._app.bot.send_message(
                chat_id=self.chat_id, text="Test message from ThaoResearch"
            )
            return (True, f"Bot @{me.username} connected to chat {self._mask(self.chat_id)}")
        except Forbidden as e:
            return (False, f"401/403 Forbidden: {e}")
        except ChatMigrated as e:
            return (False, f"Chat migrated: {e}")
        except Exception as e:
            # v20.7 raises BadRequest (not ChatNotFound) when chat_id is invalid
            msg = str(e).lower()
            if "chat not found" in msg or "invalid chat" in msg:
                return (False, f"Chat not found: {e}")
            if "unauthorized" in msg or "401" in msg or "forbidden" in msg or "403" in msg:
                return (False, f"401/403 Unauthorized: {e}")
            if isinstance(e, (TimedOut, TGNetworkError)):
                return (False, f"Network error: {e}")
            return (False, str(e))

    @staticmethod
    def _mask(value: str) -> str:
        if not value:
            return "***"
        if len(value) <= 6:
            return "***"
        return f"{value[:4]}***{value[-3:]}"

    # ---------------------------------------------------------------- callbacks

    def register_callback_handler(self) -> bool:
        """Register inline-keyboard callbacks and start receiving updates.

        We MUST call `start_polling()` so the bot actually receives
        CallbackQuery updates from Telegram. Without it, button presses
        spin forever on "Loading..." because the server has nowhere to
        deliver the update.

        Calling this multiple times is safe — it will only start polling
        once.
        """
        if not self._app:
            return False
        loop = self._ensure_loop()
        if loop is None:
            return False
        try:
            from telegram.ext import CallbackQueryHandler

            self._app.add_handler(
                CallbackQueryHandler(self._on_inline_callback)
            )
            logger.info("Telegram callback handler registered")

            if self._polling_started:
                return True

            async def _start():
                await self._app.initialize()
                await self._app.start()
                await self._app.updater.start_polling(
                    drop_pending_updates=True,
                    allowed_updates=["callback_query", "message"],
                )
                self._polling_started = True
                logger.info("Telegram polling started")

            future = asyncio.run_coroutine_threadsafe(_start(), loop)
            future.result(timeout=15)
            return True
        except Exception as e:
            logger.error("Failed to start Telegram polling: %s", e)
            return False

    async def _on_inline_callback(self, update, context):
        """Handle `sum:<hash>` / `hide:<hash>` button presses.

        Telegram allows exactly ONE `answer()` per callback query. Calling
        it twice raises `BadRequest: Query is too old` and lets the button
        spin forever on "Loading...". We therefore:
        1. For `sum:`: try to show the summary as a popup alert (one shot).
           If the summary is too long, edit the original message to show
           the summary inline + a Hide button.
        2. For `hide:`: remove the inline keyboard entirely.
        """
        query = getattr(update, "callback_query", None)
        if query is None:
            return
        try:
            data = (query.data or "").strip()
            if not (data.startswith("sum:") or data.startswith("hide:")):
                return
            is_expand = data.startswith("sum:")
            article_hash = data.split(":", 1)[1]

            # Fetch the original row so we can re-render the heading + link.
            heading = None
            link = ""
            summary = ""
            try:
                from utils import database
                with database.get_cursor() as cur:
                    cur.execute(
                        "SELECT article_title, article_url, article_summary "
                        "FROM sent_articles WHERE article_hash = ? LIMIT 1",
                        (article_hash,),
                    )
                    row = cur.fetchone()
                if row:
                    heading = row[0] or "Article"
                    link = row[1] or ""
                    summary = (row[2] or "").strip()
            except Exception as e:
                logger.warning("callback DB lookup failed: %s", e)

            original = getattr(query.message, "text", "") or ""

            if is_expand:
                # Expand: rebuild the message with summary spliced in, and
                # swap the inline button to "Summary ▲" (callback `hide:`).
                new_text = self._compose_summary_view(
                    heading=heading,
                    original_text=original,
                    link=link,
                    summary=summary,
                )
                # Try Markdown first; fall back to plain text on edge chars.
                try:
                    await query.edit_message_text(
                        text=new_text[:TELEGRAM_MSG_LIMIT],
                        parse_mode="Markdown",
                        reply_markup=self._summary_reply_markup(
                            article_hash, expanded=True
                        ),
                    )
                except Exception as e:
                    logger.debug(
                        "edit_message_text (md) failed: %s — retrying plain", e
                    )
                    try:
                        await query.edit_message_text(
                            text=self._strip_markdown(new_text)[:TELEGRAM_MSG_LIMIT],
                            reply_markup=self._summary_reply_markup(
                                article_hash, expanded=True
                            ),
                        )
                    except Exception as e2:
                        logger.warning("edit_message_text plain failed: %s", e2)
            else:
                # Collapse: rebuild the short view (heading + link only),
                # drop the inline keyboard to "Summary ▼".
                new_text = self._compose_short_view(
                    heading=heading, link=link, original_text=original
                )
                try:
                    await query.edit_message_text(
                        text=new_text[:TELEGRAM_MSG_LIMIT],
                        parse_mode="Markdown",
                        reply_markup=self._summary_reply_markup(
                            article_hash, expanded=False
                        ),
                    )
                except Exception as e:
                    logger.debug(
                        "edit_message_text (md) failed: %s — retrying plain", e
                    )
                    try:
                        await query.edit_message_text(
                            text=self._strip_markdown(new_text)[:TELEGRAM_MSG_LIMIT],
                            reply_markup=self._summary_reply_markup(
                                article_hash, expanded=False
                            ),
                        )
                    except Exception as e2:
                        logger.warning("edit_message_text plain failed: %s", e2)

            # Single, silent answer — the message itself is already updated.
            try:
                await query.answer()
            except Exception:
                pass
        except Exception as e:
            logger.error("Inline callback failed: %s", e)
            try:
                await query.answer(text="Error: could not load summary", show_alert=True)
            except Exception:
                pass

    @staticmethod
    def _strip_markdown(text: str) -> str:
        """Remove Telegram Markdown v1 markers so the message is safe to send."""
        if not text:
            return ""
        return (
            text.replace("**", "")
            .replace("__", "")
            .replace("*", "")
            .replace("_", "")
        )

    def _compose_summary_view(
        self,
        heading,
        original_text: str,
        link: str,
        summary: str,
    ) -> str:
        """Build the updated message text.

        Goal: keep the existing `[Tên hàng hóa] - [Title]` heading line
        and the `Read full article` link at the bottom unchanged. Only
        the summary body is added between them.

        The link is reconstructed from the DB column (`link`) so it is
        always present, even if the original message text didn't contain
        a link line (e.g. server-side stripping, multiple edits, etc.).
        """
        from models.article import _escape_markdown

        # Fallback when we couldn't read the DB row at all.
        if not heading:
            if summary:
                return (original_text or "").rstrip() + "\n\n" + summary
            return original_text or ""

        # Build a clean heading line. We escape only the heading text
        # so the title's special chars don't break Markdown parsing.
        heading_line = f"*{_escape_markdown(heading)}*"

        # We always rebuild the link from the DB value so it is present
        # even if original_text was missing it.
        link_line = f"[Read full article]({link})" if link else ""

        # If we have the original message, keep its heading line(s) verbatim
        # (so the user's `[Commodity] - [Title]` stays identical) and just
        # splice the summary in before the link.
        if original_text:
            lines = original_text.split("\n")
            body_lines = []
            for line in lines:
                if "[Read full article]" in line:
                    continue  # drop the old link — we'll re-append from DB
                body_lines.append(line)
            # Strip trailing blank lines from the body
            while body_lines and not body_lines[-1].strip():
                body_lines.pop()

            # Find the index right after the first non-empty line (the heading).
            insert_idx = 0
            for i, line in enumerate(body_lines):
                if line.strip():
                    insert_idx = i + 1
                    break

            new_lines = body_lines[:insert_idx]
            if summary:
                new_lines.append("")  # blank line before summary
                new_lines.append(summary)
            if link_line:
                new_lines.append("")
                new_lines.append(link_line)
            return "\n".join(new_lines)

        # No original message — synthesise from heading + link + summary.
        parts = [heading_line]
        if summary:
            parts.append("")
            parts.append(summary)
        if link_line:
            parts.append("")
            parts.append(link_line)
        return "\n".join(parts)

    @staticmethod
    def _truncate(text: str, max_chars: int) -> str:
        if not text:
            return ""
        if len(text) <= max_chars:
            return text
        return text[: max_chars - 3].rsplit(" ", 1)[0] + "..."

    def _lookup_summary(self, article_hash: str) -> str:
        """Fetch the **summary snippet** (no heading, no link) from DB by hash.

        Returns just the description body, or an empty string if the row
        doesn't exist or has no summary. Callers are responsible for
        appending it to the original short message.
        """
        try:
            from utils import database
            with database.get_cursor() as cur:
                cur.execute(
                    "SELECT article_summary FROM sent_articles "
                    "WHERE article_hash = ? LIMIT 1",
                    (article_hash,),
                )
                row = cur.fetchone()
            if not row:
                return ""
            return (row[0] or "").strip()
        except Exception as e:
            logger.warning("lookup_summary failed: %s", e)
        return ""

    @staticmethod
    def _summary_reply_markup(article_hash: str, expanded: bool):
        """Build the inline keyboard for the summary expand/collapse buttons.

        The label and ``callback_data`` flip between
        ``Summary ▼`` (sum:…) and ``Summary ▲`` (hide:…) so the user can
        toggle the inline summary without a network reload.
        """
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        if expanded:
            label = "Summary \u25b2"
            callback = f"hide:{article_hash}"
        else:
            label = "Summary \u25bc"
            callback = f"sum:{article_hash}"
        return InlineKeyboardMarkup(
            [[InlineKeyboardButton(label, callback_data=callback)]]
        )

    @staticmethod
    def _compose_short_view(heading: Optional[str], link: str, original_text: str) -> str:
        """Rebuild the **collapsed** short view of the article message.

        Used when the user taps ``Summary ▲`` to hide the inline summary
        snippet — we want to restore the original compact form
        (``heading`` + ``link``) without having to re-fetch anything.

        Falls back to ``original_text`` when neither heading nor a
        recognisable link is available, so we never lose the user's
        context on collapse.
        """
        from models.article import _escape_markdown

        heading_line = (
            f"*{_escape_markdown(heading)}*" if heading else ""
        )
        link_line = f"[Read full article]({link})" if link else ""

        if heading_line:
            parts = [heading_line]
            if link_line:
                parts.append(link_line)
            return "\n".join(parts)
        # Fallback: original text minus any previously-spliced summary.
        if original_text:
            lines = original_text.split("\n")
            # Drop everything between the first non-empty line (heading)
            # and the ``[Read full article]`` line.
            trimmed: List[str] = []
            kept_heading = False
            for line in lines:
                if not kept_heading and line.strip():
                    trimmed.append(line)
                    kept_heading = True
                    continue
                if "[Read full article]" in line:
                    trimmed.append(line)
                    # Continue so any trailing blank lines are stripped.
                    continue
                if kept_heading and line.strip() and "[Read full article]" not in line:
                    # This is part of the previously-spliced summary — drop it.
                    continue
                if not line.strip():
                    continue
            if link_line and not any("[Read full article]" in x for x in trimmed):
                trimmed.append(link_line)
            if trimmed:
                return "\n".join(trimmed)
        return original_text or ""
