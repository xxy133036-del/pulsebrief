from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import NewsArticle, Subscription
from app.scheduling import utc_naive_now

TERM_ALIASES = {
    "国内": ("domestic", "china", "chinese", "mainland china", "in china"),
    "财经": ("finance", "financial", "business"),
    "社会": ("society", "social", "community"),
    "体育": ("sport", "sports", "athletics"),
    "文化": ("culture", "cultural", "arts", "entertainment"),
    "生活": ("lifestyle", "daily life", "everyday life"),
    "环境": ("environment", "environmental", "ecology", "climate"),
    "健康": ("health", "healthcare", "wellness"),
    "国际": (
        "international",
        "foreign affairs",
        "global",
        "world",
        "overseas",
        "abroad",
        "united states",
        "russia",
        "ukraine",
        "france",
        "germany",
        "japan",
        "south korea",
        "india",
        "united kingdom",
        "philippines",
        "europe",
        "middle east",
        "美国",
        "俄罗斯",
        "乌克兰",
        "法国",
        "德国",
        "日本",
        "韩国",
        "印度",
        "英国",
        "菲律宾",
        "欧洲",
        "中东",
        "海外",
        "外交",
        "跨境",
    ),
    "政策": ("policy", "policies", "regulation", "regulatory"),
    "地方": ("local", "regional", "province", "provincial", "municipal"),
    "政务": (
        "government",
        "governance",
        "public administration",
        "local authority",
        "local authorities",
    ),
    "外交": ("diplomacy", "diplomatic", "foreign affairs"),
    "经济": ("economy", "economic", "economics"),
    "公共服务": ("public services", "public service", "government services"),
    "就业": ("employment", "jobs", "labor", "labour", "workforce"),
    "产业": ("industry", "industries", "industrial", "manufacturing", "sectors"),
    "消费": ("consumer", "consumption", "retail", "consumer spending"),
    "交通": ("transport", "transportation", "transit", "public transit", "railway"),
    "文化遗产": ("cultural heritage", "heritage"),
    "艺术": ("art", "arts", "artist", "artists", "exhibition"),
    "足球": ("football", "soccer"),
    "全民健身": (
        "mass sports",
        "public fitness",
        "community sports",
        "recreational sports",
        "fitness",
        "exercise",
        "physical activity",
    ),
    "教育": ("education", "educational"),
    "学校": ("school", "schools", "primary school", "secondary school"),
    "大学": ("university", "universities", "college", "colleges", "higher education"),
    "法治": ("law", "legal", "justice", "judicial"),
    "法院": ("court", "courts", "judiciary", "judge", "judges"),
    "权益": ("rights", "civil rights", "consumer protection", "human rights"),
    "科研": ("research", "scientific research", "researchers", "research study"),
    "科技": ("technology", "technologies", "science", "innovation"),
    "人工智能": ("artificial intelligence", "ai", "machine learning"),
    "大模型": (
        "large language model",
        "large language models",
        "llm",
        "llms",
        "foundation model",
        "foundation models",
        "generative ai",
    ),
    "智能体": ("ai agent", "ai agents", "agentic ai", "agentic", "agents"),
    "ai agent": ("ai agents", "agentic ai", "agentic"),
    "ai agents": ("ai agent", "agentic ai", "agentic"),
}


@lru_cache(maxsize=1)
def source_registry() -> dict[str, dict]:
    path = Path(__file__).resolve().parents[3] / "data" / "sources" / "feeds.json"
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {
        str(feed.get("id")): feed
        for feed in document.get("feeds", [])
        if isinstance(feed, dict) and feed.get("id")
    }


def source_quality_hold_ids() -> set[str]:
    """Return sources temporarily withheld after a documented accuracy failure."""
    return {
        source_id
        for source_id, feed in source_registry().items()
        if feed.get("quality_hold") is True
    }


def cleared_source_ids_from_register(document: object) -> set[str]:
    """Return sources cleared at the same granularity as their content licenses."""
    if not isinstance(document, dict) or not isinstance(document.get("sources"), list):
        return set()
    required_scopes = {
        "feed_metadata_collection",
        "article_text_retrieval",
        "third_party_ai_processing",
        "summary_retention",
        "public_app_display",
    }
    cleared = set()
    for record in document["sources"]:
        if not isinstance(record, dict) or not record.get("source_id"):
            continue
        scopes = record.get("scopes")
        if (
            record.get("status") == "cleared"
            and record.get("license_granularity") == "source_wide"
            and record.get("reviewed_on")
            and record.get("evidence_reference")
            and isinstance(scopes, dict)
            and all(scopes.get(scope) == "granted" for scope in required_scopes)
        ):
            cleared.add(str(record["source_id"]))
    return cleared


def metadata_only_source_ids_from_register(document: object) -> set[str]:
    """Return sources cleared only for descriptive metadata, never full article text."""
    if not isinstance(document, dict) or not isinstance(document.get("sources"), list):
        return set()
    required_scopes = {
        "feed_metadata_collection",
        "third_party_ai_processing",
        "summary_retention",
        "public_app_display",
    }
    cleared = set()
    for record in document["sources"]:
        if not isinstance(record, dict) or not record.get("source_id"):
            continue
        scopes = record.get("scopes")
        if (
            record.get("status") == "metadata_only"
            and record.get("license_granularity") == "metadata_only"
            and record.get("reviewed_on")
            and record.get("evidence_reference")
            and isinstance(scopes, dict)
            and all(scopes.get(scope) == "granted" for scope in required_scopes)
            and scopes.get("article_text_retrieval") == "not_granted"
        ):
            cleared.add(str(record["source_id"]))
    return cleared


@lru_cache(maxsize=1)
def rights_cleared_source_ids() -> set[str]:
    """Return only sources with recorded clearance for the complete processing path."""
    path = Path(__file__).resolve().parents[3] / "data" / "sources" / "rights_register.json"
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    return cleared_source_ids_from_register(document)


@lru_cache(maxsize=1)
def metadata_only_source_ids() -> set[str]:
    path = Path(__file__).resolve().parents[3] / "data" / "sources" / "rights_register.json"
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    return metadata_only_source_ids_from_register(document)


def source_metadata_only(source_id: str) -> bool:
    return source_id in metadata_only_source_ids()


def source_display_name(source_id: str) -> str:
    feed = source_registry().get(source_id, {})
    return str(feed.get("name") or source_id or "新闻来源")


def source_content_type(source_id: str) -> str:
    """Return the curated source class, defaulting legacy sources to specialist articles."""
    feed = source_registry().get(source_id, {})
    value = str(feed.get("content_type") or "article")
    return value if value in {"news", "article"} else "article"


def source_republish_details(source_id: str) -> dict[str, str]:
    feed = source_registry().get(source_id, {})
    return {
        key: str(feed[key])
        for key in ("license_name", "license_url", "republish_credit", "change_notice", "evidence_scope")
        if feed.get(key)
    }


def source_article_hosts() -> dict[str, list[str]]:
    return {
        source_id: [str(host).casefold().rstrip(".") for host in feed.get("article_hosts", [])]
        for source_id, feed in source_registry().items()
        if isinstance(feed.get("article_hosts"), list)
    }


def strip_source_id_from_summary(summary: str, source_id: str) -> str:
    """Remove model-leaked internal feed identifiers from user-facing prose."""
    if not summary or not source_id:
        return summary
    identifier = re.escape(source_id)
    summary = re.sub(
        rf"\s*[（(]\s*来源\s*[:：]\s*{identifier}\s*[)）]",
        "",
        summary,
        flags=re.IGNORECASE,
    )
    summary = re.sub(
        rf"来源\s*[:：]\s*{identifier}(?=\s|[，。；;,.!?！？]|$)",
        "",
        summary,
        flags=re.IGNORECASE,
    )
    return re.sub(r"\s{2,}", " ", summary).strip()


def as_utc_naive(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def terms_text(article: NewsArticle) -> str:
    return normalize_text(" ".join((article.title or "", article.summary or "", article.content or "")))


def normalize_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def term_variants(value: str) -> tuple[str, ...]:
    normalized = normalize_text(value)
    return (normalized, *TERM_ALIASES.get(normalized, ())) if normalized else ()


def _contains_term(text: str, term: str) -> bool:
    normalized_text = normalize_text(text)
    normalized_term = normalize_text(term)
    if not normalized_text or not normalized_term:
        return False
    if any("a" <= char <= "z" for char in normalized_term):
        pattern = rf"(?<!\w){re.escape(normalized_term)}(?!\w)"
        return re.search(pattern, normalized_text, flags=re.IGNORECASE) is not None
    return normalized_term in normalized_text


def _topic_matches(article: NewsArticle, variants: tuple[str, ...]) -> bool:
    prominent_text = normalize_text(
        " ".join((article.title or "", article.summary or "", article.category or ""))
    )
    if any(_contains_term(prominent_text, alias) for alias in variants):
        return True

    content = normalize_text(article.content or "")
    occurrences = 0
    for alias in variants:
        normalized_alias = normalize_text(alias)
        if not normalized_alias:
            continue
        if normalized_alias == "social":
            # "social media giant" is a common description of Meta, not a society topic.
            content_for_alias = re.sub(r"\bsocial(?:\s+|-)media\b", "", content)
        else:
            content_for_alias = content
        if any("a" <= char <= "z" for char in normalized_alias):
            pattern = rf"(?<!\w){re.escape(normalized_alias)}(?!\w)"
            occurrences += len(re.findall(pattern, content_for_alias, flags=re.IGNORECASE))
        else:
            occurrences += content_for_alias.count(normalized_alias)
        if occurrences >= 2:
            return True
    return False


def select_candidates(
    db: Session, subscription: Subscription, *, demo_mode: bool = False, limit: int = 100
) -> list[NewsArticle]:
    cutoff = utc_naive_now() - timedelta(days=2)
    scan_limit = 1000
    query = (
        select(NewsArticle)
        .where(NewsArticle.published_at >= cutoff)
        .order_by(NewsArticle.published_at.desc())
        .limit(scan_limit)
    )
    articles = list(db.scalars(query).all())
    cleared_source_ids = rights_cleared_source_ids()
    quality_hold_ids = source_quality_hold_ids()
    exclude_terms = [normalize_text(term) for term in (subscription.excluded_keywords or [])]
    include_terms = [term_variants(term) for term in (subscription.keywords or [])]
    topics = [term_variants(term) for term in (subscription.topics or [])]
    matched: list[NewsArticle] = []
    for article in articles:
        if article.is_demo != demo_mode:
            continue
        if not article.is_demo and article.source_id not in cleared_source_ids:
            continue
        if not article.is_demo and article.source_id in quality_hold_ids:
            continue
        content = terms_text(article)
        if any(term and term in content for term in exclude_terms):
            continue
        term_matches = [
            any(_contains_term(content, alias) for alias in variants) for variants in include_terms
        ]
        if include_terms and (
            not all(term_matches) if subscription.match_all else not any(term_matches)
        ):
            continue
        if topics and not any(
            _topic_matches(article, variants)
            for variants in topics
        ):
            continue
        matched.append(article)
        if len(matched) >= limit:
            break
    return matched


def article_payload(article: NewsArticle) -> dict:
    summary = article.summary or ""
    content = article.content or ""
    if content.strip() == summary.strip():
        content = ""
    return {
        "id": article.id,
        "source_id": article.source_id,
        "source_name": source_display_name(article.source_id),
        "content_type": source_content_type(article.source_id),
        **({"evidence_scope": "metadata_abstract"} if source_metadata_only(article.source_id) else {}),
        "title": article.title,
        "author": article.author or "",
        "summary": summary[:1800],
        "content": content[:20_000],
        "url": article.url,
        "category": article.category,
        "published_at": article.published_at.isoformat() + "Z",
        "is_demo": article.is_demo,
    }


def add_article(
    db: Session,
    *,
    source_id: str,
    title: str,
    summary: str,
    content: str,
    url: str,
    category: str,
    published_at: datetime,
    author: str = "",
    is_demo: bool = False,
) -> NewsArticle:
    clean_url = url.strip()
    fingerprint = hashlib.sha256(" ".join(title.casefold().split()).encode("utf-8")).hexdigest()
    existing = db.scalar(
        select(NewsArticle).where(NewsArticle.source_id == source_id, NewsArticle.url == clean_url)
    )
    if existing:
        existing.title = title[:400]
        existing.author = author[:256]
        existing.summary = summary[:5000]
        existing.content = content[:20000]
        existing.category = category[:64]
        existing.published_at = as_utc_naive(published_at) or utc_naive_now()
        existing.fetched_at = utc_naive_now()
        existing.fingerprint = fingerprint
        db.flush()
        return existing
    article = NewsArticle(
        source_id=source_id[:96],
        title=title[:400],
        author=author[:256],
        summary=summary[:5000],
        content=content[:20000],
        url=clean_url[:2048],
        category=category[:64],
        published_at=as_utc_naive(published_at) or utc_naive_now(),
        fingerprint=fingerprint,
        is_demo=is_demo,
    )
    db.add(article)
    db.flush()
    return article


def resolve_candidate_items(
    output: dict, candidates: list[NewsArticle], item_limit: int
) -> list[dict]:
    by_id = {article.id: article for article in candidates}
    resolved = []
    seen = set()
    for raw_item in output.get("items", [])[:item_limit]:
        if not isinstance(raw_item, dict):
            continue
        article = by_id.get(str(raw_item.get("article_id", "")))
        if article is None or article.id in seen:
            continue
        summary = strip_source_id_from_summary(
            str(raw_item.get("summary", "")).strip(), article.source_id
        )
        if len(summary) < 12:
            continue
        seen.add(article.id)
        certainty = str(raw_item.get("certainty", "source_checked")).casefold()
        if certainty not in {"high", "medium", "low", "source_checked", "demo"}:
            certainty = "source_checked"
        resolved.append(
            {
                "article_id": article.id,
                "title": str(raw_item.get("title_zh") or article.title).strip()[:200],
                "original_title": article.title,
                "author": article.author or "",
                "summary": summary[:1600],
                "reason": str(raw_item.get("reason", "符合当前订阅偏好。"))[:500],
                "certainty": certainty,
                "source_id": article.source_id,
                "source_name": source_display_name(article.source_id),
                "content_type": source_content_type(article.source_id),
                "source_url": article.url,
                **source_republish_details(article.source_id),
                "published_at": article.published_at.isoformat() + "Z",
                "is_demo": article.is_demo,
            }
        )
    return resolved
