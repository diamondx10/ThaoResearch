"""Article dataclass - standard format for crawled news articles."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

# Telegram MarkdownV2 reserved chars (used for escaping)
_MARKDOWN_SPECIAL = re.compile(r"([_*\[\]()~`>#+\-=|{}.!\\])")


def _escape_markdown(text: str) -> str:
    """Escape special characters for Telegram Markdown (V1/v2)."""
    if not text:
        return ""
    return _MARKDOWN_SPECIAL.sub(r"\\\1", text)


def _truncate(text: str, max_chars: int) -> str:
    """Truncate text to max_chars, add ellipsis if needed."""
    if not text:
        return ""
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rsplit(" ", 1)[0] + "..."


@dataclass
class Article:
    """Standardized article format produced by parsers and consumed by notifiers."""

    title: str
    link: str
    pub_date: datetime
    summary: str = ""
    source: str = ""
    commodity_tags: List[str] = field(default_factory=list)
    triggered_commodity: Optional[str] = None
    triggered_price: Optional[float] = None
    triggered_delta_pct: Optional[float] = None
    is_urgent: bool = False
    is_price_alert: bool = False

    def compute_hash(self) -> str:
        """Return MD5 hash of title + link for deduplication."""
        return hashlib.md5(f"{self.title}{self.link}".encode("utf-8")).hexdigest()

    def to_telegram_message(self) -> str:
        """Render article as a minimal Telegram message: heading + title + link.

        Format (3 lines):
            [Commodity] — [Title]
            Read full article (link)

        The summary snippet is intentionally hidden — user taps the
        inline `Summary` button to view it on demand.
        """
        lines: List[str] = []

        # Heading: "[Tên hàng hóa]" (with optional Urgent tag) — "[Title]"
        commodity = self.triggered_commodity or self.source or "News"
        urgent_tag = " (Urgent)" if self.is_urgent else ""
        lines.append(f"*{_escape_markdown(commodity)}*{urgent_tag} — {_escape_markdown(self.title)}")

        # Link
        if self.link:
            lines.append(f"[Read full article]({self.link})")

        return "\n".join(lines)

    def to_telegram_reply_markup(self, expanded: bool = False):
        """Inline keyboard for the Telegram message.

        By default the button label is ``Summary ▼`` — tapping it expands
        the message inline (no reload, no extra round trip) and swaps the
        label to ``Summary ▲``.

        When ``expanded=True`` the caller is rendering the post-expand
        view and we label the button ``Summary ▲`` so the user can
        collapse the message back down.

        ``callback_data`` must be <= 64 bytes. We prefix the action so
        the bot dispatcher can branch on ``sum:`` vs ``hide:`` and
        include the article hash so we can look the row up if needed.
        """
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        h = self.compute_hash()
        if expanded:
            label = "Summary ▲"
            callback = f"hide:{h}"
        else:
            label = "Summary ▼"
            callback = f"sum:{h}"
        return InlineKeyboardMarkup(
            [[InlineKeyboardButton(label, callback_data=callback)]]
        )

    def to_telegram_summary_text(self) -> str:
        """Return the summary snippet used by the `📋 Summary` button."""
        if not self.summary:
            return "_No summary available for this article._"
        return f"📋 *Summary*\n\n{_escape_markdown(_truncate(self.summary, 600))}"

    # ------------------------------------------------------------------
    # Windows toast
    # ------------------------------------------------------------------
    def to_toast_title(self) -> str:
        """Window-toast title — shows ONLY the commodity name + article title.

        Telegram format: ``*[Commodity]*(Urgent) — Title``

        The toast title has fewer characters than a Telegram message and
        has no Markdown — we render it as plain text without the asterisks.
        We keep just two pieces of info as the user requested:
            1. The commodity name (e.g. "Soybean Oil", "Gold")
            2. The article title
        User opens the tool to read the full article.
        """
        commodity = self.triggered_commodity or (
            self.commodity_tags[0] if self.commodity_tags else None
        ) or self.source or "News"
        if self.is_urgent:
            return f"{commodity} (Urgent) — {self.title}"
        return f"{commodity} — {self.title}"

    def to_toast_body(self) -> str:
        """Window-toast body — intentionally empty.

        Per UX request the toast shows ONLY commodity + title in the title
        line. The user opens the tool (or clicks the toast) to read the
        full article. Keeping the body empty avoids dumping the URL into
        the notification banner.
        """
        return ""

    def to_short_summary(self) -> str:
        """Return one-line short summary for console logging."""
        tags = ", ".join(self.commodity_tags[:2]) if self.commodity_tags else self.source
        urgent = "Urgent | " if self.is_urgent else ""
        price = ""
        if self.is_price_alert and self.triggered_price is not None:
            price = f" | {self.triggered_price:.2f}"
        title_short = self.title[:80] + ("..." if len(self.title) > 80 else "")
        return f"[{tags}{price}] {urgent}{title_short}"

    def to_console_block(self) -> str:
        """Render article as text-only console block (no color)."""
        sep = "─" * 60
        lines = [
            sep,
            f"Commodity:      {self.triggered_commodity or ', '.join(self.commodity_tags) or '-'}",
            f"Status:         {'Price Alert' if self.is_price_alert else 'Keyword Match'}"
            + ("  (Urgent)" if self.is_urgent else ""),
            f"Source:         {self.source}",
            f"Published:      {self.pub_date.strftime('%Y-%m-%d %H:%M:%S %z')}",
        ]
        if self.is_price_alert and self.triggered_price is not None:
            delta = ""
            if self.triggered_delta_pct is not None:
                delta = f", delta {self.triggered_delta_pct:+.1f}%"
            lines.append(f"Detected:       {self.triggered_price:.2f} USD{delta}")
        if self.commodity_tags:
            lines.append(f"Tags:           {', '.join(self.commodity_tags)}")
        lines.append("")
        lines.append("Title:")
        # Wrap title
        for chunk in _wrap_text(self.title, 58):
            lines.append(f"  {chunk}")
        if self.summary:
            lines.append("")
            lines.append("Content:")
            for chunk in _wrap_text(self.summary, 58):
                lines.append(f"  {chunk}")
        lines.append("")
        lines.append("Link:")
        lines.append(f"  {self.link}")
        lines.append("")
        lines.append("Hash:")
        lines.append(f"  {self.compute_hash()}")
        lines.append(sep)
        return "\n".join(lines)


def _wrap_text(text: str, width: int) -> List[str]:
    """Wrap text into lines of at most `width` chars."""
    result: List[str] = []
    for paragraph in text.split("\n"):
        if not paragraph:
            result.append("")
            continue
        words = paragraph.split()
        current = ""
        for word in words:
            if not current:
                current = word
            elif len(current) + 1 + len(word) <= width:
                current += " " + word
            else:
                result.append(current)
                current = word
        if current:
            result.append(current)
    return result
