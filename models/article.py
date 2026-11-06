"""Article dataclass - standard format for crawled news articles."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

# Telegram MarkdownV2 reserved chars (used for escaping)
# Telegram has two incompatible Markdown dialects and they need different
# escaping:
#
#   Markdown (V1)  -- only _ * [ ` are markers. A backslash before anything
#                     else is shown to the reader as a literal backslash.
#   MarkdownV2     -- the long list below is reserved and must all be escaped.
#
# We send with parse_mode="Markdown" (V1), but this module used to escape with
# the V2 set, so every alert arrived reading "U\.S\. soybean oil climbed 3\.2%".
# Keep the two sets apart and use the one that matches the parse mode.
_MARKDOWN_V1_SPECIAL = re.compile(r"([_*\[`])")
_MARKDOWN_V2_SPECIAL = re.compile(r"([_*\[\]()~`>#+\-=|{}.!\\])")


def _escape_markdown(text: str) -> str:
    """Escape text for Telegram ``parse_mode="Markdown"`` (V1)."""
    if not text:
        return ""
    return _MARKDOWN_V1_SPECIAL.sub(r"\\\1", text)


def _escape_markdown_v2(text: str) -> str:
    """Escape text for Telegram ``parse_mode="MarkdownV2"``."""
    if not text:
        return ""
    return _MARKDOWN_V2_SPECIAL.sub(r"\\\1", text)


def _collapse_ws(text: str) -> str:
    """Flatten newlines and runs of spaces into single spaces.

    RSS summaries often arrive with embedded markup and hard wraps; a teaser
    that keeps them renders as a ragged block on a phone.
    """
    if not text:
        return ""
    return " ".join(text.split())


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

    # Characters of RSS summary shown in the collapsed teaser. Short enough
    # to stay readable at a glance on a phone lock screen.
    TEASER_CHARS = 180

    def to_telegram_heading(self) -> str:
        """The first line: ``*Commodity* (Urgent) - Title``."""
        commodity = self.triggered_commodity or self.source or "News"
        urgent_tag = " (Urgent)" if self.is_urgent else ""
        return (
            f"*{_escape_markdown(commodity)}*{urgent_tag} "
            f"— {_escape_markdown(self.title)}"
        )

    def to_telegram_message(self) -> str:
        """Render the collapsed teaser: heading, short snippet, link.

        Built to be read on a phone without opening anything:

            *Soybean Oil* (Urgent) - U.S. soybean oil gushes into biofuel...
            NOPA data showed crush margins widening as renewable diesel...
            Read full article

        Tapping *Read more* expands the real article body in place -- see
        ``to_telegram_reply_markup`` and the pager in ``core.telegram_bot``.
        """
        lines: List[str] = [self.to_telegram_heading()]

        teaser = _truncate(_collapse_ws(self.summary), self.TEASER_CHARS)
        if teaser:
            lines.append("")
            lines.append(_escape_markdown(teaser))

        if self.link:
            lines.append("")
            lines.append(f"[Read full article]({self.link})")

        return "\n".join(lines)

    def to_telegram_reply_markup(self, expanded: bool = False):
        """Inline keyboard for the collapsed (or freshly expanded) message.

        Telegram caps ``callback_data`` at 64 bytes. Ours is
        ``more:<32-char md5>:<page>`` (about 40 bytes), leaving room for the
        page index the pager needs.

        The older ``sum:`` / ``hide:`` prefixes are still handled by the
        callback dispatcher, so messages sent by a previous version keep
        working after an upgrade.
        """
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        h = self.compute_hash()
        if expanded:
            return InlineKeyboardMarkup(
                [[InlineKeyboardButton("Read less ▲", callback_data=f"less:{h}")]]
            )
        return InlineKeyboardMarkup(
            [[InlineKeyboardButton("Read more ▼", callback_data=f"more:{h}:0")]]
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
