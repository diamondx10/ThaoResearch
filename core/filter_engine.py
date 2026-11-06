"""Filter engine — date window, blacklist, commodity tagging, price, urgency."""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, time as dtime
from typing import List, Optional, Tuple
from zoneinfo import ZoneInfo

from models.article import Article
from utils import price_tracker

logger = logging.getLogger("thaoresearch.core.filter_engine")

# ---------------------------------------------------------------- date window

#: Where the fetch window lives in config.yaml, and what it means:
#:
#:   fetch_window:
#:     mode: since | range | all
#:     from: '2026-10-26'      # inclusive, local midnight
#:     to:   '2026-11-30'      # inclusive, local end-of-day ("range" only)
#:
#: "since" runs from ``from`` up to now, "range" is bounded at both ends,
#: "all" disables the window. Missing config behaves as DEFAULT_MODE/FROM.
DEFAULT_WINDOW_MODE = "since"
DEFAULT_WINDOW_FROM = "2026-10-26"
WINDOW_MODES = ("since", "range", "all")


def parse_window_date(value) -> Optional[date]:
    """Parse a config date. Accepts ``date``/``datetime``, 'YYYY-MM-DD',
    and the 'DD/MM/YYYY' people type by hand. Returns None if unusable.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    logger.warning("Unrecognised fetch_window date %r — ignoring that bound.", value)
    return None


def resolve_window(config: dict, tz_name: str = "Asia/Ho_Chi_Minh") -> Tuple[Optional[datetime], Optional[datetime]]:
    """Turn the config block into inclusive (start, end) datetimes.

    Either bound may be None, meaning "unbounded in that direction".
    Dates are interpreted in the user's own timezone: ``from`` at 00:00:00
    and ``to`` at 23:59:59.999999, so a single-day range covers that day.
    """
    block = (config or {}).get("fetch_window") or {}
    mode = str(block.get("mode") or DEFAULT_WINDOW_MODE).strip().lower()
    if mode not in WINDOW_MODES:
        logger.warning("Unknown fetch_window mode %r — using %r.", mode, DEFAULT_WINDOW_MODE)
        mode = DEFAULT_WINDOW_MODE
    if mode == "all":
        return (None, None)

    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = None

    start_date = parse_window_date(block.get("from"))
    if start_date is None:
        start_date = parse_window_date(DEFAULT_WINDOW_FROM)
    start = datetime.combine(start_date, dtime.min).replace(tzinfo=tz) if start_date else None

    end = None
    if mode == "range":
        end_date = parse_window_date(block.get("to"))
        if end_date is not None:
            end = datetime.combine(end_date, dtime.max).replace(tzinfo=tz)

    # A reversed range is a typo, not an instruction to match nothing.
    if start and end and end < start:
        logger.warning("fetch_window 'to' is before 'from' — swapping them.")
        start, end = (
            datetime.combine(end.date(), dtime.min).replace(tzinfo=tz),
            datetime.combine(start.date(), dtime.max).replace(tzinfo=tz),
        )
    return (start, end)


def apply_date_window(
    articles: List[Article],
    config: dict,
    tz_name: str = "Asia/Ho_Chi_Minh",
) -> List[Article]:
    """Drop articles published outside the configured fetch window.

    Runs before the keyword filters so the dashboard's "filtered" count
    reflects what the user actually asked to see. An article whose
    pub_date cannot be compared is kept: silently dropping news because a
    feed gave a odd date would be worse than letting one extra through.
    """
    start, end = resolve_window(config, tz_name)
    if start is None and end is None:
        return list(articles)

    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = None

    kept: List[Article] = []
    for article in articles:
        pub = getattr(article, "pub_date", None)
        if not isinstance(pub, datetime):
            kept.append(article)
            continue
        # normalize_pub_dates() makes these aware, but a naive one would
        # raise on comparison, so pin it to the user's timezone first.
        if pub.tzinfo is None and tz is not None:
            pub = pub.replace(tzinfo=tz)
        try:
            if start is not None and pub < start:
                continue
            if end is not None and pub > end:
                continue
        except TypeError:
            kept.append(article)
            continue
        kept.append(article)

    dropped = len(articles) - len(kept)
    if dropped:
        logger.info(
            "Date window %s..%s dropped %d of %d articles.",
            start.date() if start else "-",
            end.date() if end else "now",
            dropped,
            len(articles),
        )
    return kept

# Matches: 90, 90.5, 1,234, 1,234.56, optionally followed by USD/$
_PRICE_RE = re.compile(
    r"\b(\d{1,3}(?:[.,]\d{3})*(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?)\s*(?:USD|\$)?",
    flags=re.IGNORECASE,
)


def _to_float(num_str: str) -> Optional[float]:
    """Convert a matched number string to float; None if invalid."""
    s = num_str.replace(",", "")
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def apply_filters(
    articles: List[Article],
    config: dict,
    record_prices: bool = True,
) -> List[Article]:
    """Apply the 4-step filter pipeline. Returns only matching articles.

    Steps:
        1. Blacklist (drop)
        2. Commodity tagging (assign commodity_tags)
        3. Price extraction (set is_price_alert, triggered_*)
        4. Urgency (set is_urgent)
    """
    filters = config.get("filters", {}) or {}
    commodities = [c for c in (config.get("commodities") or []) if c.get("enabled", True)]

    blacklist = [str(k).lower() for k in (filters.get("blacklist_keywords") or [])]
    high_priority = [str(k).lower() for k in (filters.get("high_priority_keywords") or [])]
    enable_delta = bool(filters.get("enable_delta_alert", False))
    default_delta_threshold = float(filters.get("delta_threshold_pct", 5.0))

    matched: List[Article] = []
    for article in articles:
        text = (article.title + " " + article.summary).lower()

        # Step 1: Blacklist
        if any(b in text for b in blacklist):
            logger.debug("Blacklisted: %s", article.title)
            continue

        # Step 2: Commodity tagging
        tags: List[str] = []
        for c in commodities:
            keywords = [str(k).lower() for k in (c.get("keywords") or [])]
            if any(k in text for k in keywords):
                tags.append(c.get("name", ""))
        article.commodity_tags = tags
        if not tags:
            # Only keep articles that match at least one commodity
            continue

        # Step 3: Price extraction + threshold + delta
        article.is_price_alert = False
        article.triggered_commodity = None
        article.triggered_price = None
        article.triggered_delta_pct = None

        numbers_in_text = _extract_numbers(article.title + " " + article.summary)
        for commodity_name in tags:
            spec = next((c for c in commodities if c.get("name") == commodity_name), None)
            if not spec:
                continue
            threshold = spec.get("price_threshold")
            if threshold is None:
                # Keyword-only commodity
                if not article.is_price_alert:
                    article.triggered_commodity = commodity_name
                continue

            for num in numbers_in_text:
                if num > float(threshold):
                    article.is_price_alert = True
                    article.triggered_commodity = commodity_name
                    article.triggered_price = num
                    # Delta tracking
                    delta_threshold = float(spec.get("delta_threshold_pct") or default_delta_threshold)
                    if record_prices and enable_delta:
                        prev, delta_pct = price_tracker.record_price(commodity_name, num)
                        if delta_pct is not None and abs(delta_pct) >= delta_threshold:
                            article.triggered_delta_pct = delta_pct
                    elif record_prices:
                        price_tracker.record_price(commodity_name, num)
                    break  # first matching number wins

        # Step 4: Urgency
        article.is_urgent = any(hp in text for hp in high_priority)

        matched.append(article)

    logger.info("Filter: %d -> %d articles", len(articles), len(matched))
    return matched


def _extract_numbers(text: str) -> List[float]:
    """Extract plausible price-like numbers from text."""
    if not text:
        return []
    out: List[float] = []
    for m in _PRICE_RE.finditer(text):
        raw = m.group(1)
        val = _to_float(raw)
        if val is not None and 0 < val < 1_000_000:
            out.append(val)
    return out
