from __future__ import annotations

import html
import ipaddress
import json
import re
import socket
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from functools import lru_cache
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import feedparser
import httpcore
import httpx

from app.config import get_settings
from app.db import SessionLocal
from app.models import NewsArticle
from app.scheduling import utc_naive_now
from app.services.news import (
    add_article,
    metadata_only_source_ids,
    rights_cleared_source_ids,
    source_quality_hold_ids,
    source_registry,
)

MAX_FEED_BYTES = 8 * 1024 * 1024
MAX_ARTICLE_BYTES = 2 * 1024 * 1024
MAX_ARTICLE_TEXT_CHARS = 20_000
EXTERNAL_REPUBLICATION_PATTERNS = (
    re.compile(r"\bthis (?:article|post) was originally published by\b", re.IGNORECASE),
    re.compile(r"\bthe following version is republished on global voices\b", re.IGNORECASE),
    re.compile(r"\brepublished under a content partnership\b", re.IGNORECASE),
    re.compile(r"\brepublished with permission from\b", re.IGNORECASE),
)
GENERIC_AUTHOR_LABELS = {
    "guest contributor",
    "guest writer",
    "guest post",
    "staff",
    "staff writer",
    "editorial team",
    "editorial staff",
}
RESTRICTED_NEWS_AGENCY_PATTERNS = (
    re.compile(r"美联社|Associated Press", re.IGNORECASE),
    re.compile(r"路透社|\bReuters\b", re.IGNORECASE),
    re.compile(r"法新社|Agence France-Presse|\bAFP\b", re.IGNORECASE),
    re.compile(r"\bAP\s+(?:contributed|contributed reporting|provided)\b", re.IGNORECASE),
)
NEWS_CENTRAL_CATEGORIES = {
    "world": "国际",
    "business": "国际财经",
    "politics": "国际政治",
    "health": "国际健康",
    "entertainment": "国际文化",
    "sports": "国际体育",
    "technology": "国际科技",
    "science": "国际科学",
    "travel": "国际生活",
    "style": "国际文化",
    "lifestyle": "国际生活",
}


def _public_ipv4_addresses(host: str, port: int = 443) -> list[str]:
    try:
        records = socket.getaddrinfo(
            host, port, family=socket.AF_INET, type=socket.SOCK_STREAM
        )
    except OSError as exc:
        raise ValueError("The feed hostname did not resolve to a public IPv4 address.") from exc
    addresses = list(dict.fromkeys(record[4][0] for record in records))
    if not addresses:
        raise ValueError("The feed hostname did not resolve to a public IPv4 address.")
    for address in addresses:
        if not ipaddress.ip_address(address).is_global:
            raise ValueError("The feed resolves to a non-public network address.")
    return addresses


class PinnedPublicIPv4Backend(httpcore.SyncBackend):
    """Resolve once, validate every answer, then connect directly to a validated IP."""

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options=None,
    ):
        if port != 443:
            raise OSError("RSS connections are restricted to public HTTPS port 443.")
        addresses = _public_ipv4_addresses(host, port)
        failures = []
        for address in addresses:
            try:
                return super().connect_tcp(
                    address,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except OSError as exc:
                failures.append(exc)
        raise OSError("Unable to connect to a validated public feed address.") from failures[-1]


class _CoreResponseStream(httpx.SyncByteStream):
    def __init__(self, response: httpcore.Response):
        self.response = response

    def __iter__(self):
        yield from self.response.stream

    def close(self) -> None:
        self.response.close()


class PinnedFeedTransport(httpx.BaseTransport):
    """HTTPX adapter that keeps URL/TLS hostnames while pinning TCP to checked IPs."""

    def __init__(self):
        self.pool = httpcore.ConnectionPool(
            local_address="0.0.0.0",  # noqa: S104 - bind only for outbound IPv4 sockets
            network_backend=PinnedPublicIPv4Backend(),
        )

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        core_request = httpcore.Request(
            method=request.method,
            url=httpcore.URL(
                scheme=request.url.raw_scheme,
                host=request.url.raw_host,
                port=request.url.port,
                target=request.url.raw_path,
            ),
            headers=request.headers.raw,
            content=request.stream,
            extensions=request.extensions,
        )
        response = self.pool.handle_request(core_request)
        return httpx.Response(
            status_code=response.status,
            headers=response.headers,
            stream=_CoreResponseStream(response),
            extensions=response.extensions,
            request=request,
        )

    def close(self) -> None:
        self.pool.close()


class PlainText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.chunks: list[str] = []
        self.skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() in {"script", "style", "svg", "iframe"}:
            self.skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in {"script", "style", "svg", "iframe"} and self.skip_depth:
            self.skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self.skip_depth:
            self.chunks.append(data)


class ArticleBodyText(HTMLParser):
    """Extract readable text only from semantic article/main containers."""

    BODY_TAGS = {"article", "main"}
    SKIP_TAGS = {"script", "style", "svg", "iframe", "nav", "footer", "header", "form"}
    BLOCK_TAGS = {"p", "h1", "h2", "h3", "h4", "li", "blockquote", "br", "section"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.chunks: list[str] = []
        self._skip_stack: list[str] = []
        self._body_depth = 0
        self.found_body = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        if tag in self.SKIP_TAGS:
            self._skip_stack.append(tag)
            return
        if self._skip_stack:
            return
        if tag in self.BODY_TAGS:
            self._body_depth += 1
            self.found_body = True
        if self._body_depth and tag in self.BLOCK_TAGS:
            self.chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if self._skip_stack:
            if tag == self._skip_stack[-1]:
                self._skip_stack.pop()
            return
        if tag in self.BODY_TAGS and self._body_depth:
            self._body_depth -= 1
        if self._body_depth and tag in self.BLOCK_TAGS:
            self.chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._body_depth and not self._skip_stack:
            self.chunks.append(data)


class EasyMandarinIndex(HTMLParser):
    """Read dated Chinese story cards from the publisher's public index page."""

    ARTICLE_PATH = re.compile(r"^/(?:en/)?articles/[A-Za-z0-9_-]{8,96}/?$")

    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.entries: list[dict[str, str]] = []
        self.current: dict[str, str] | None = None
        self.in_title = False
        self._entry_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        tag = tag.casefold()
        if tag == "a" and self.current is None:
            href = attributes.get("href") or ""
            path = urlsplit(href).path
            if self.ARTICLE_PATH.fullmatch(path):
                story_path = path if path.startswith("/en/") else f"/en{path}"
                self.current = {"link": urljoin(self.base_url, story_path), "title": ""}
                self._entry_depth = 1
                return
        elif self.current is not None:
            if tag == "a":
                self._entry_depth += 1
            if tag in {"h2", "h3"} and (attributes.get("lang") or "").casefold().startswith("zh"):
                self.in_title = True
            if (
                tag == "time"
                and attributes.get("datetime")
                and "published" not in self.current
            ):
                self.current["published"] = str(attributes["datetime"])

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if self.current is None:
            return
        if tag in {"h2", "h3"}:
            self.in_title = False
        if tag == "a":
            self._entry_depth -= 1
            if self._entry_depth <= 0:
                if self.current.get("title") and self.current.get("published"):
                    self.entries.append(self.current)
                self.current = None
                self._entry_depth = 0
                self.in_title = False

    def handle_data(self, data: str) -> None:
        if self.current is not None and self.in_title:
            self.current["title"] += data


class TravelDailyIndex(HTMLParser):
    """Read dated article cards from TravelDaily category pages."""

    ARTICLE_PATH = re.compile(r"^/article/\d+/?$")

    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.entries: list[dict[str, str]] = []
        self.current: dict[str, str] | None = None
        self.depth = 0
        self.in_title = False
        self.in_date = False
        self.title_chunks: list[str] = []
        self.date_chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        tag = tag.casefold()
        if tag == "a" and self.current is None:
            href = attributes.get("href") or ""
            if self.ARTICLE_PATH.fullmatch(urlsplit(href).path):
                self.current = {"link": urljoin(self.base_url, href)}
                self.depth = 1
                self.title_chunks = []
                self.date_chunks = []
                return
        elif self.current is not None:
            if tag == "a":
                self.depth += 1
            classes = set((attributes.get("class") or "").split())
            if tag in {"h2", "h3"} and any("title" in name.casefold() for name in classes):
                self.in_title = True
            if tag == "div" and any("meta_left" in name for name in classes):
                self.in_date = True

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if self.current is None:
            return
        if tag in {"h2", "h3"}:
            self.in_title = False
        if tag == "div" and self.in_date:
            self.in_date = False
        if tag == "a":
            self.depth -= 1
            if self.depth <= 0:
                title = to_plain_text(" ".join(self.title_chunks), 400)
                age = to_plain_text(" ".join(self.date_chunks), 100)
                published = _relative_chinese_age_to_iso(age)
                if title and published:
                    self.current.update({"title": title, "published": published})
                    self.entries.append(self.current)
                self.current = None
                self.depth = 0
                self.in_title = False
                self.in_date = False

    def handle_data(self, data: str) -> None:
        if self.current is None:
            return
        if self.in_title:
            self.title_chunks.append(data)
        if self.in_date:
            self.date_chunks.append(data)


def _relative_chinese_age_to_iso(value: str) -> str:
    """Convert TravelDaily's relative card date to an ISO timestamp; reject unknown dates."""
    match = re.search(r"(\d+)\s*(分钟|小时|天|周)前", value)
    if not match:
        return ""
    amount = int(match.group(1))
    unit = match.group(2)
    delta = {
        "分钟": timedelta(minutes=amount),
        "小时": timedelta(hours=amount),
        "天": timedelta(days=amount),
        "周": timedelta(weeks=amount),
    }[unit]
    return (utc_naive_now() - delta).isoformat()


def extract_traveldaily_article_text(document: str) -> str:
    return extract_traveldaily_article(document)[0]


def extract_traveldaily_article(document: str) -> tuple[str, str]:
    """Accept only TravelDaily-authored pages that display their CC BY-NC notice."""
    if not re.search(r"class=[\"'][^\"']*articleCopyRightDescLink[^\"']*[\"'][^>]*>\s*CC BY-NC 4\.0\s*</a>", document, re.IGNORECASE):
        return "", ""
    author_match = re.search(
        r"class=[\"'][^\"']*authorTop[^\"']*[\"'][^>]*>\s*环球旅讯\s+([^<]+)</span>",
        document,
        re.IGNORECASE,
    )
    if not author_match:
        return "", ""
    body_match = re.search(
        r"<div[^>]*class=[\"'][^\"']*articleBody[^\"']*[\"'][^>]*>(.*?)</div>\s*<div[^>]*class=[\"'][^\"']*articleCopyRight",
        document[:MAX_ARTICLE_BYTES],
        re.IGNORECASE | re.DOTALL,
    )
    if not body_match:
        return "", ""
    body = to_plain_text(body_match.group(1), MAX_ARTICLE_TEXT_CHARS)
    if len(body) < 120:
        return "", ""
    return body, to_plain_text(author_match.group(1), 256)


class EasyMandarinArticleText(HTMLParser):
    """Extract the English source-paragraph translation, excluding pinyin/UI noise."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.chunks: list[str] = []
        self.article_depth = 0
        self.paragraph_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        attributes = dict(attrs)
        if tag == "article":
            self.article_depth += 1
        elif tag == "p" and self.article_depth and self.paragraph_depth == 0:
            classes = set((attributes.get("class") or "").split())
            if {"font-serif", "italic"}.issubset(classes):
                self.paragraph_depth = 1
                self.chunks.append("\n")
        elif self.paragraph_depth and tag == "p":
            self.paragraph_depth += 1

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag == "p" and self.paragraph_depth:
            self.paragraph_depth -= 1
            if self.paragraph_depth == 0:
                self.chunks.append("\n")
        elif tag == "article" and self.article_depth:
            self.article_depth -= 1

    def handle_data(self, data: str) -> None:
        if self.article_depth and self.paragraph_depth:
            self.chunks.append(data)


def extract_article_text(document: str, limit: int = MAX_ARTICLE_TEXT_CHARS) -> str:
    parser = ArticleBodyText()
    try:
        parser.feed(document[:MAX_ARTICLE_BYTES])
    except Exception:
        return ""
    if not parser.found_body:
        return ""
    paragraphs = [re.sub(r"\s+", " ", part).strip() for part in parser.chunks]
    text = " ".join(part for part in paragraphs if part)
    text = re.sub(r"\s+([,.;:!?，。！？；：])", r"\1", text)
    return text[:limit]


def extract_easy_mandarin_article_text(
    document: str, limit: int = MAX_ARTICLE_TEXT_CHARS
) -> str:
    parser = EasyMandarinArticleText()
    try:
        parser.feed(document[:MAX_ARTICLE_BYTES])
    except Exception:
        return ""
    paragraphs = [re.sub(r"\s+", " ", part).strip() for part in parser.chunks]
    text = " ".join(part for part in paragraphs if part)
    text = re.sub(r"\s+([,.;:!?，。！？；：])", r"\1", text)
    return text[:limit]


def to_plain_text(value: str, limit: int = 5000) -> str:
    parser = PlainText()
    try:
        parser.feed(value[:30_000])
    except Exception:
        pass
    text = html.unescape(" ".join(parser.chunks))
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\s+([,.;:!?，。！？；：])", r"\1", text)
    return text[:limit]


def _validated_feed_url(value: str) -> None:
    parts = urlsplit(value)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.port not in (None, 443)
    ):
        raise ValueError("A feed URL must use public HTTPS on port 443.")
    if parts.hostname.casefold() in {"localhost", "metadata.google.internal"}:
        raise ValueError("Internal network hostnames are not permitted.")
    _public_ipv4_addresses(parts.hostname)


def _validated_article_url(value: str, allowed_hosts: list[str]) -> str:
    parts = urlsplit(value)
    host = (parts.hostname or "").casefold().rstrip(".")
    allowed = {str(item).casefold().rstrip(".") for item in allowed_hosts}
    if (
        parts.scheme != "https"
        or not host
        or host not in allowed
        or parts.username
        or parts.password
        or parts.port not in (None, 443)
        or len(value) > 2048
    ):
        raise ValueError("The article URL is outside its source's HTTPS host allowlist.")
    _public_ipv4_addresses(host)
    return value


def _read_limited_response(response: httpx.Response, limit: int = MAX_FEED_BYTES) -> bytes:
    content_length = response.headers.get("content-length")
    if content_length:
        try:
            declared_size = int(content_length)
        except ValueError:
            declared_size = 0
        if declared_size > limit:
            raise ValueError("The response exceeds its configured size limit.")

    chunks = []
    size = 0
    for chunk in response.iter_bytes():
        size += len(chunk)
        if size > limit:
            raise ValueError("The response exceeds its configured size limit.")
        chunks.append(chunk)
    return b"".join(chunks)


def fetch_article_text(
    value: str, allowed_hosts: list[str], source_id: str | None = None
) -> str:
    url = _validated_article_url(value, allowed_hosts)
    return _fetch_article_text_cached(url, tuple(allowed_hosts), source_id or "")


def fetch_article_with_author(
    value: str, allowed_hosts: list[str], source_id: str
) -> tuple[str, str]:
    """Fetch a licensed publisher article and its byline in one request."""
    url = _validated_article_url(value, allowed_hosts)
    return _fetch_article_with_author_cached(url, tuple(allowed_hosts), source_id)


@lru_cache(maxsize=512)
def _fetch_article_text_cached(
    value: str, allowed_hosts: tuple[str, ...], source_id: str = ""
) -> str:
    direct_text = ""
    try:
        direct_text = _fetch_article_from_publisher(value, source_id)
    except httpx.HTTPError:
        pass
    return direct_text


@lru_cache(maxsize=512)
def _fetch_article_with_author_cached(
    value: str, allowed_hosts: tuple[str, ...], source_id: str
) -> tuple[str, str]:
    if source_id != "traveldaily-cn":
        return _fetch_article_text_cached(value, allowed_hosts, source_id), ""
    document = _fetch_article_document(value)
    return extract_traveldaily_article(document) if document else ("", "")


def _fetch_article_from_publisher(value: str, source_id: str = "") -> str:
    document = _fetch_article_document(value)
    if not document:
        return ""
    if source_id == "easy-mandarin-news":
        return extract_easy_mandarin_article_text(document)
    if source_id == "traveldaily-cn":
        return extract_traveldaily_article_text(document)
    return extract_article_text(document)


def _fetch_article_document(value: str) -> str:
    settings = get_settings()
    with httpx.Client(
        transport=PinnedFeedTransport(),
        timeout=httpx.Timeout(min(settings.feed_request_timeout_seconds, 10)),
        follow_redirects=False,
        trust_env=False,
        headers={"User-Agent": "DailyAINewsAssistant/0.1 (approved article reader)"},
    ) as client:
        with client.stream("GET", value) as response:
            response.raise_for_status()
            content_type = response.headers.get("content-type", "").casefold()
            if "text/html" not in content_type and "application/xhtml+xml" not in content_type:
                return ""
            raw = _read_limited_response(response, MAX_ARTICLE_BYTES)
            encoding = response.encoding or "utf-8"
            document = raw.decode(encoding, errors="replace")
    return document


def _entry_evidence(entry: dict) -> tuple[str, str]:
    summary = to_plain_text(str(entry.get("summary", "")), 5000)
    content_values = entry.get("content") or []
    body_html = max(
        (str(item.get("value", "")) for item in content_values if isinstance(item, dict)),
        key=len,
        default="",
    )
    content = to_plain_text(body_html, 20_000) or summary
    if not summary and content:
        summary = content[:5000]
    return summary, content


def _entry_category(entry: dict, feed: dict) -> str:
    if feed.get("id") == "news-central-site":
        tags = entry.get("tags") or []
        for tag in tags:
            if not isinstance(tag, dict):
                continue
            section = str(tag.get("term", "")).casefold().strip()
            if section in NEWS_CENTRAL_CATEGORIES:
                return NEWS_CENTRAL_CATEGORIES[section]
    return str(feed.get("category", "ai"))


def _is_external_republication(source_id: str, content: str) -> bool:
    """Reject syndicated posts when a source grants reuse only for its own work."""
    if source_id != "global-voices":
        return False
    disclosure_text = content[:3000]
    return any(pattern.search(disclosure_text) for pattern in EXTERNAL_REPUBLICATION_PATTERNS)


def _has_named_author(author: str) -> bool:
    normalized = " ".join(author.casefold().split())
    return bool(normalized) and normalized not in GENERIC_AUTHOR_LABELS


def contains_restricted_agency_material(source_id: str, content: str) -> bool:
    """Fail closed for feeds whose terms exclude third-party news-agency material."""
    feed = source_registry().get(source_id, {})
    if feed.get("exclude_third_party_agencies") is not True:
        return False
    return any(pattern.search(content) for pattern in RESTRICTED_NEWS_AGENCY_PATTERNS)


def _parse_date(entry: dict) -> datetime:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed:
        from time import mktime

        return datetime.fromtimestamp(mktime(parsed), tz=UTC).replace(tzinfo=None)
    raw = entry.get("published") or entry.get("updated")
    if raw:
        try:
            value = parsedate_to_datetime(raw)
            return value.astimezone(UTC).replace(tzinfo=None) if value.tzinfo else value
        except (TypeError, ValueError, OverflowError):
            try:
                value = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
                return value.astimezone(UTC).replace(tzinfo=None) if value.tzinfo else value
            except (TypeError, ValueError, OverflowError):
                pass
    return utc_naive_now()


def _has_published_date(entry: dict) -> bool:
    return bool(
        entry.get("published_parsed")
        or entry.get("updated_parsed")
        or entry.get("published")
        or entry.get("updated")
    )


def _parse_feed_document(raw: bytes, feed: dict) -> list[dict]:
    if feed.get("format") != "html_index":
        return list(feedparser.parse(raw).entries)
    if feed.get("parser") == "traveldaily":
        parser = TravelDailyIndex(str(feed["url"]))
        try:
            parser.feed(raw.decode("utf-8", errors="replace")[:MAX_FEED_BYTES])
        except Exception:
            return []
        return parser.entries
    parser = EasyMandarinIndex(str(feed["url"]))
    try:
        parser.feed(raw.decode("utf-8", errors="replace")[:MAX_FEED_BYTES])
    except Exception:
        return []
    return parser.entries


def collect_feed(feed: dict) -> dict:
    source_id = str(feed.get("id", ""))
    full_text_sources = rights_cleared_source_ids()
    metadata_sources = metadata_only_source_ids()
    metadata_only = source_id in metadata_sources and source_id not in full_text_sources
    if source_id not in full_text_sources and not (
        metadata_only and feed.get("evidence_scope") == "metadata_abstract"
    ):
        return {"source_id": feed.get("id", "unknown"), "status": "skipped_rights"}
    _validated_feed_url(feed["url"])
    settings = get_settings()
    with httpx.Client(
        transport=PinnedFeedTransport(),
        timeout=httpx.Timeout(settings.feed_request_timeout_seconds),
        follow_redirects=False,
        trust_env=False,
        headers={"User-Agent": "DailyAINewsAssistant/0.1 (approved RSS reader)"},
    ) as client:
        with client.stream("GET", feed["url"]) as response:
            response.raise_for_status()
            entries = _parse_feed_document(_read_limited_response(response), feed)
    db = SessionLocal()
    inserted = updated = skipped_no_evidence = skipped_invalid_url = 0
    skipped_stale_title_only = 0
    skipped_stale_article = 0
    skipped_missing_date = 0
    skipped_missing_author = 0
    skipped_external_republication = 0
    skipped_third_party_agencies = 0
    entry_limit = min(300, max(1, int(feed.get("max_entries", 300))))
    try:
        for entry in entries[:entry_limit]:
            title = to_plain_text(str(entry.get("title", "")), 400)
            author = to_plain_text(
                str(entry.get("author") or (entry.get("author_detail") or {}).get("name") or ""),
                256,
            )
            url = str(entry.get("link", "")).strip()
            summary, content = _entry_evidence(entry)
            if not title:
                continue
            if feed.get("exclude_external_republication") is True and _is_external_republication(
                feed["id"], content
            ):
                skipped_external_republication += 1
                continue
            if contains_restricted_agency_material(
                feed["id"], " ".join((title, summary, content))
            ):
                skipped_third_party_agencies += 1
                continue
            if (
                feed.get("require_author") is True
                and feed.get("require_article_byline") is not True
                and (
                    not author
                    or (
                        feed.get("require_named_author") is True
                        and not _has_named_author(author)
                    )
                )
            ):
                skipped_missing_author += 1
                continue
            try:
                parts = urlsplit(url)
                valid_url = (
                    parts.scheme == "https"
                    and bool(parts.hostname)
                    and not parts.username
                    and not parts.password
                    and parts.port in (None, 443)
                    and len(url) <= 2048
                )
            except ValueError:
                valid_url = False
            if not valid_url:
                skipped_invalid_url += 1
                continue
            if feed.get("fetch_article_body") is True and not metadata_only:
                max_age_hours = max(1, int(feed.get("title_only_max_age_hours", 72)))
                if not _has_published_date(entry):
                    skipped_missing_date += 1
                    continue
                if _parse_date(entry) < utc_naive_now() - timedelta(hours=max_age_hours):
                    skipped_stale_article += 1
                    continue
            existing = (
                db.query(NewsArticle)
                .filter(NewsArticle.source_id == feed["id"], NewsArticle.url == url)
                .first()
            )
            if feed.get("fetch_article_body") is True and not metadata_only and not (summary or content):
                if (
                    existing
                    and existing.content
                    and not (feed.get("require_article_byline") is True and not existing.author)
                ):
                    content = existing.content
                    summary = existing.summary or content[:5000]
                    author = existing.author or author
                else:
                    if feed.get("require_article_byline") is True:
                        content, author = fetch_article_with_author(
                            url, feed.get("article_hosts", []), feed.get("id")
                        )
                        if not author:
                            skipped_missing_author += 1
                            continue
                    else:
                        content = fetch_article_text(
                            url, feed.get("article_hosts", []), feed.get("id")
                        )
                    summary = content[:5000] if content else ""
            if not summary and not content and feed.get("allow_title_only") is not True:
                skipped_no_evidence += 1
                continue
            if not summary and not content and feed.get("allow_title_only") is True:
                max_age_hours = max(1, int(feed.get("title_only_max_age_hours", 48)))
                if not _has_published_date(entry) or _parse_date(entry) < (
                    utc_naive_now() - timedelta(hours=max_age_hours)
                ):
                    skipped_stale_title_only += 1
                    continue
            add_article(
                db,
                source_id=feed["id"],
                title=title,
                summary=summary,
                content=content,
                url=url,
                category=_entry_category(entry, feed),
                published_at=_parse_date(entry),
                author=author,
                is_demo=False,
            )
            updated += 1 if existing else 0
            inserted += 0 if existing else 1
        db.commit()
        return {
            "source_id": feed["id"],
            "status": "ok",
            "received": len(entries),
            "processed": min(len(entries), entry_limit),
            "inserted": inserted,
            "updated": updated,
            "skipped_no_evidence": skipped_no_evidence,
            "skipped_invalid_url": skipped_invalid_url,
            "skipped_stale_title_only": skipped_stale_title_only,
            "skipped_stale_article": skipped_stale_article,
            "skipped_missing_date": skipped_missing_date,
            "skipped_missing_author": skipped_missing_author,
            "skipped_external_republication": skipped_external_republication,
            "skipped_third_party_agencies": skipped_third_party_agencies,
        }
    finally:
        db.close()


def collect_all() -> dict:
    path = Path(__file__).resolve().parents[3] / "data" / "sources" / "feeds.json"
    source_file = json.loads(path.read_text(encoding="utf-8"))
    results = []
    cleared_source_ids = rights_cleared_source_ids()
    metadata_source_ids = metadata_only_source_ids()
    quality_hold_ids = source_quality_hold_ids()
    for feed in source_file["feeds"]:
        if feed.get("enabled") is not True:
            continue
        if feed.get("id") in quality_hold_ids:
            results.append({"source_id": feed.get("id", "unknown"), "status": "skipped_quality"})
            continue
        if feed.get("id") not in cleared_source_ids and not (
            feed.get("id") in metadata_source_ids
            and feed.get("evidence_scope") == "metadata_abstract"
        ):
            results.append({"source_id": feed.get("id", "unknown"), "status": "skipped_rights"})
            continue
        try:
            results.append(collect_feed(feed))
        except Exception as exc:
            results.append(
                {
                    "source_id": feed.get("id", "unknown"),
                    "status": "failed",
                    "error": type(exc).__name__,
                }
            )
    return {
        "results": results,
        "successes": sum(row["status"] == "ok" for row in results),
        "failures": sum(row["status"] == "failed" for row in results),
        "skipped_rights": sum(row["status"] == "skipped_rights" for row in results),
        "skipped_quality": sum(row["status"] == "skipped_quality" for row in results),
    }
