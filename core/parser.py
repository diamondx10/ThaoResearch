"""Article normalization & date conversion to local timezone."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Iterable, List, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from models.article import Article

logger = logging.getLogger("thaoresearch.core.parser")

DEFAULT_TZ = "Asia/Ho_Chi_Minh"


def to_local(dt: datetime, tz_name: str = DEFAULT_TZ) -> datetime:
    """Convert a datetime to the given IANA timezone. Naive datetimes assumed UTC."""
    if dt is None:
        return datetime.now(ZoneInfo(tz_name))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    try:
        target = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        logger.warning("Unknown timezone '%s', using UTC", tz_name)
        target = timezone.utc
    return dt.astimezone(target)


def normalize_pub_dates(articles: Iterable[Article], tz_name: str = DEFAULT_TZ) -> List[Article]:
    """Convert all articles' pub_date to the local timezone (in-place)."""
    for a in articles:
        a.pub_date = to_local(a.pub_date, tz_name)
    return list(articles)


def parse_date_string(s: str, tz_name: str = DEFAULT_TZ) -> Optional[datetime]:
    """Parse a free-form date string into a local datetime. Returns None on failure."""
    if not s:
        return None
    from email.utils import parsedate_to_datetime

    try:
        dt = parsedate_to_datetime(s)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    return to_local(dt, tz_name)
