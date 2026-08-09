"""Fetch + extract the **full** body text of a news article URL.

The crawlers only persist the RSS description (truncated to 500 chars) so the
desktop GUI has only a short snippet to show. This module fetches the article
URL itself, parses the HTML, and extracts a readable body using a few common
selectors (e.g. ``<article>``, ``[itemprop="articleBody"]``, fallbacks).

Returns plain text — safe to drop into a Tk Text widget or send to a Text
without HTML escaping concerns.
"""
from __future__ import annotations

import logging
import re
from typing import List, Optional

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger("thaoresearch.core.content_fetcher")

# Common article body selectors, in priority order. Most news sites use one
# of these for the main article container. We try the most specific first
# and fall back to broader selectors.
_BODY_SELECTORS: List[str] = [
    "article",
    "[itemprop='articleBody']",
    "[property='article:body']",
    "[role='article']",
    "main article",
    ".article-body",
    ".article-content",
    ".story-body",
    ".entry-content",
    ".post-content",
    ".content article",
    "#article-body",
    "#content article",
    "main",
    ".content",
    "#content",
]

# Tags we strip out completely (nav, ads, related stories, share buttons…).
_REMOVE_SELECTORS: List[str] = [
    "script", "style", "noscript", "iframe",
    "nav", "header", "footer",
    "aside",
    ".advertisement", ".ad", ".ads", ".adsbygoogle",
    ".share", ".social", ".sharing", ".share-buttons",
    ".related", ".related-posts", ".recommended",
    ".newsletter", ".subscribe", ".paywall",
    "[role='navigation']", "[role='banner']", "[role='complementary']",
    "form", "button",
]


def _strip_html(html: str) -> str:
    """Convert HTML to clean plain text using BeautifulSoup."""
    soup = BeautifulSoup(html or "", "html.parser")
    for sel in _REMOVE_SELECTORS:
        for el in soup.select(sel):
            try:
                el.decompose()
            except Exception:
                pass
    text = soup.get_text(separator="\n", strip=True)
    # Collapse 3+ blank lines into 1, and trim trailing whitespace per line.
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = "\n".join(line.rstrip() for line in text.splitlines())
    return text.strip()


def extract_body_text(html: str, base_url: str = "") -> str:
    """Try multiple selectors to pull the article body out of an HTML page.

    Returns the best matching container's text content. If nothing matches,
    returns the cleaned full-page text (best-effort).
    """
    soup = BeautifulSoup(html or "", "html.parser")

    # Drop noise up front so selectors get a smaller tree to scan.
    for sel in _REMOVE_SELECTORS:
        for el in soup.select(sel):
            try:
                el.decompose()
            except Exception:
                pass

    for selector in _BODY_SELECTORS:
        try:
            node = soup.select_one(selector)
        except Exception:
            node = None
        if not node:
            continue
        # Skip empty / trivially short matches.
        try:
            text = node.get_text(separator="\n", strip=True)
        except Exception:
            continue
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = "\n".join(line.rstrip() for line in text.splitlines()).strip()
        if len(text) >= 200:  # require at least ~200 chars to be useful
            return text

    # Last resort: dump the whole document text.
    text = soup.get_text(separator="\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = "\n".join(line.rstrip() for line in text.splitlines()).strip()
    return text


def fetch_full_content(url: str, timeout: float = 12.0, max_chars: int = 30000) -> Optional[str]:
    """Download ``url`` and return the extracted article body as plain text.

    Returns ``None`` on any error so the caller can decide whether to fall
    back to the RSS summary or surface an error message. ``max_chars`` caps
    the returned text length to avoid filling a Tk Text widget with a
    100k-character article.
    """
    if not url:
        return None
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    try:
        resp = requests.get(url, headers=headers, timeout=timeout)
    except requests.RequestException as e:
        logger.warning("fetch_full_content: request failed for %s: %s", url, e)
        return None
    if resp.status_code != 200:
        logger.warning("fetch_full_content: HTTP %s for %s", resp.status_code, url)
        return None
    try:
        text = extract_body_text(resp.text, base_url=url)
    except Exception as e:
        logger.warning("fetch_full_content: parse failed for %s: %s", url, e)
        return None
    if not text:
        return None
    if len(text) > max_chars:
        text = text[:max_chars].rsplit(" ", 1)[0] + "\n\n[…content truncated…]"
    return text
