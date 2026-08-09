"""Filter engine — blacklist, commodity tagging, price threshold, urgency."""
from __future__ import annotations

import logging
import re
from typing import List, Optional

from models.article import Article
from utils import price_tracker

logger = logging.getLogger("thaoresearch.core.filter_engine")

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
