"""Crawlers — fetch articles from RSS or HTML sources."""
from __future__ import annotations

import logging
import re
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import List, Optional, Tuple

import feedparser
import requests
from bs4 import BeautifulSoup

from models.article import Article
from utils.errors import (
    DNSError,
    HTMLParseError,
    HTTPClientError,
    HTTPServerError,
    NetworkTimeoutError,
    RateLimitError,
    RSSEmptyError,
    RSSMalformedError,
    SSLError,
)

logger = logging.getLogger("thaoresearch.core.crawler")

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36 "
    "ThaoResearch/1.0"
)

REQUEST_TIMEOUT = 10
MAX_RETRIES = 3
INTER_REQUEST_DELAY = 2  # seconds between successful requests
BACKOFF_BASE = 2  # 1s, 2s, 4s


def _http_get(url: str, headers: Optional[dict] = None, timeout: int = REQUEST_TIMEOUT) -> Optional[bytes]:
    """GET a URL with retry + exponential backoff. Returns bytes or None on failure."""
    hdrs = {"User-Agent": DEFAULT_USER_AGENT}
    if headers:
        hdrs.update(headers)

    last_error: Optional[Exception] = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, headers=hdrs, timeout=timeout)
            if resp.status_code == 429:
                if attempt < MAX_RETRIES:
                    wait = BACKOFF_BASE ** attempt
                    logger.warning("HTTP 429 from %s, sleeping %ss", url, wait)
                    time.sleep(wait)
                    continue
                raise RateLimitError(f"HTTP 429 from {url}")
            if 400 <= resp.status_code < 500:
                raise HTTPClientError(f"HTTP {resp.status_code} from {url}")
            if 500 <= resp.status_code < 600:
                if attempt < MAX_RETRIES:
                    time.sleep(BACKOFF_BASE ** attempt)
                    continue
                raise HTTPServerError(f"HTTP {resp.status_code} from {url}")
            return resp.content
        except requests.exceptions.Timeout as e:
            last_error = e
            if attempt < MAX_RETRIES:
                time.sleep(BACKOFF_BASE ** attempt)
                continue
            logger.error("Timeout fetching %s after %d attempts", url, MAX_RETRIES)
            return None
        except requests.exceptions.SSLError as e:
            logger.error("SSL error fetching %s: %s", url, e)
            return None
        except requests.exceptions.ConnectionError as e:
            last_error = e
            err_str = str(e).lower()
            if "name resolution" in err_str or "dns" in err_str or "getaddrinfo" in err_str:
                logger.error("DNS error fetching %s: %s", url, e)
                return None
            if attempt < MAX_RETRIES:
                time.sleep(BACKOFF_BASE ** attempt)
                continue
            logger.error("Connection error fetching %s after %d attempts: %s", url, MAX_RETRIES, e)
            return None
        except (RSSEmptyError, RSSMalformedError, HTTPClientError, HTTPServerError, RateLimitError):
            raise
        except Exception as e:
            last_error = e
            if attempt < MAX_RETRIES:
                time.sleep(BACKOFF_BASE ** attempt)
                continue
            logger.exception("Unexpected error fetching %s: %s", url, e)
            return None
    if last_error:
        logger.error("Failed fetching %s: %s", url, last_error)
    return None


def _strip_html(html: str) -> str:
    """Strip HTML tags and collapse whitespace."""
    if not html:
        return ""
    try:
        text = BeautifulSoup(html, "html.parser").get_text(separator=" ")
    except Exception:
        text = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", text).strip()


def _parse_pub_date(entry) -> Optional[datetime]:
    """Best-effort parse of a feedparser entry's published date."""
    for attr in ("published_parsed", "updated_parsed", "created_parsed"):
        parsed = entry.get(attr)
        if parsed:
            try:
                return datetime(*parsed[:6], tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue
    raw = entry.get("published") or entry.get("updated")
    if raw:
        from email.utils import parsedate_to_datetime

        try:
            dt = parsedate_to_datetime(raw)
            if dt is not None:
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt
        except (TypeError, ValueError):
            pass
    return None


class BaseCrawler(ABC):
    """Abstract base for all crawlers."""

    def __init__(self, source_config: dict):
        self.name = source_config.get("name", "?")
        self.url = source_config.get("url", "")
        self.config = source_config

    @abstractmethod
    def fetch(self) -> List[Article]:
        """Fetch and return a list of Article objects."""

    def safe_fetch(self) -> Tuple[List[Article], Optional[str]]:
        """Wrapper for scheduler: returns (articles, error_msg). Never raises."""
        try:
            articles = self.fetch()
            # Throttle successful requests to be polite
            time.sleep(INTER_REQUEST_DELAY)
            return (articles, None)
        except (RSSEmptyError, RSSMalformedError) as e:
            logger.warning("[%s] %s", e.code, e)
            return ([], f"[{e.code}] {e}")
        except (HTTPClientError, HTTPServerError, RateLimitError) as e:
            logger.warning("[%s] %s: %s", e.code, self.name, e)
            return ([], f"[{e.code}] {e}")
        except (NetworkTimeoutError, DNSError, SSLError) as e:
            logger.warning("[%s] %s: %s", e.code, self.name, e)
            return ([], f"[{e.code}] {e}")
        except Exception as e:
            logger.exception("[%s] Unexpected error for %s: %s", "E9999", self.name, e)
            return ([], f"[E9999] Unexpected error: {e}")


class RssCrawler(BaseCrawler):
    """Crawl an RSS / Atom feed."""

    def fetch(self) -> List[Article]:
        content = _http_get(self.url)
        if content is None:
            raise NetworkTimeoutError(f"Failed to fetch {self.url}")

        feed = feedparser.parse(content)
        if getattr(feed, "bozo", False) and not feed.entries:
            raise RSSMalformedError(f"Feed {self.url} is not valid XML")
        if not feed.entries:
            raise RSSEmptyError(f"Feed {self.url} returned 0 entries")

        articles: List[Article] = []
        for entry in feed.entries[:50]:  # cap at 50 per source per run
            title = (entry.get("title") or "").strip()
            link = (entry.get("link") or "").strip()
            if not title or not link:
                logger.debug("Skipping entry with missing title/link in %s", self.name)
                continue
            summary_raw = entry.get("summary") or entry.get("description") or ""
            summary = _strip_html(summary_raw)[:500]
            pub = _parse_pub_date(entry) or datetime.now(timezone.utc)
            articles.append(
                Article(
                    title=title,
                    link=link,
                    pub_date=pub,
                    summary=summary,
                    source=self.name,
                )
            )
        logger.info("Source '%s': fetched %d articles", self.name, len(articles))
        return articles


class HtmlCrawler(BaseCrawler):
    """Crawl an HTML page by extracting og:title, og:description, og:url."""

    def fetch(self) -> List[Article]:
        content = _http_get(self.url)
        if content is None:
            raise NetworkTimeoutError(f"Failed to fetch {self.url}")

        try:
            soup = BeautifulSoup(content, "html.parser")
        except Exception as e:
            raise HTMLParseError(f"Could not parse HTML from {self.url}: {e}") from e

        # Collect articles via standard RSS-like meta tags or explicit article selectors
        articles: List[Article] = []

        # 1. If user provided explicit CSS selectors, use them
        selectors = self.config.get("selectors") or {}
        article_sel = selectors.get("article")

        if article_sel:
            for node in soup.select(article_sel)[:20]:
                a = self._extract_from_node(node)
                if a:
                    articles.append(a)
        else:
            # 2. Use og: meta tags (single-page) OR scan <article> elements
            og_title = (soup.find("meta", attrs={"property": "og:title"}) or {}).get("content", "") if soup else ""
            if og_title:
                og_desc = (soup.find("meta", attrs={"property": "og:description"}) or {}).get("content", "")
                og_url = (soup.find("meta", attrs={"property": "og:url"}) or {}).get("content", "") or self.url
                articles.append(
                    Article(
                        title=og_title.strip(),
                        link=og_url.strip(),
                        pub_date=datetime.now(timezone.utc),
                        summary=_strip_html(og_desc)[:500],
                        source=self.name,
                    )
                )
            else:
                # 3. Fallback: scan <article> or <h2>/<h3> with <a>
                for node in soup.select("article, .story, .headline")[:20]:
                    a = self._extract_from_node(node)
                    if a:
                        articles.append(a)

        if not articles:
            raise HTMLParseError(f"Could not extract any article from {self.url}")
        logger.info("Source '%s': fetched %d articles", self.name, len(articles))
        return articles

    def _extract_from_node(self, node) -> Optional[Article]:
        """Try to extract title + link from an HTML node."""
        title_tag = node.find(["h1", "h2", "h3", "h4"]) or node.find("a")
        if title_tag is None:
            return None
        title = title_tag.get_text(strip=True)
        link_tag = node.find("a", href=True)
        link = link_tag["href"] if link_tag else (self.url if title_tag.name == "a" else "")
        if not title or not link:
            return None
        if link.startswith("/"):
            # Resolve relative URL against base
            from urllib.parse import urljoin
            link = urljoin(self.url, link)
        summary_node = node.find("p")
        summary = _strip_html(summary_node.get_text()) if summary_node else ""
        return Article(
            title=title,
            link=link,
            pub_date=datetime.now(timezone.utc),
            summary=summary[:500],
            source=self.name,
        )


def build_crawler(source_config: dict) -> BaseCrawler:
    """Factory: create the right crawler for a source config dict."""
    stype = (source_config.get("type") or "rss").lower()
    if stype == "rss":
        return RssCrawler(source_config)
    if stype == "html":
        return HtmlCrawler(source_config)
    raise ValueError(f"Unknown source type: {stype}")
