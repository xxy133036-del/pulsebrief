import socket
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest
from app.worker import collector
from app.worker.collector import (
    _entry_category,
    _entry_evidence,
    _has_named_author,
    _parse_date,
    _parse_feed_document,
    _read_limited_response,
    _validated_article_url,
    _validated_feed_url,
    extract_article_text,
    extract_easy_mandarin_article_text,
    fetch_article_text,
)


@pytest.mark.parametrize(
    ("section", "expected"),
    [("Sports", "国际体育"), ("Business", "国际财经"), ("Science", "国际科学")],
)
def test_news_central_source_category_is_translated_for_chinese_profiles(section, expected):
    assert _entry_category({"tags": [{"term": section}]}, {"id": "news-central-site"}) == expected


@pytest.mark.parametrize(
    ("author", "expected"),
    [
        ("Oiwan Lam", True),
        ("Guest Contributor", False),
        (" guest   writer ", False),
        ("", False),
    ],
)
def test_global_voices_named_author_guard(author, expected):
    assert _has_named_author(author) is expected


@pytest.fixture(autouse=True)
def allow_fixture_feeds(monkeypatch):
    monkeypatch.setattr(
        collector,
        "rights_cleared_source_ids",
        lambda: {
            "test-feed",
            "huggingface-blog",
            "global-voices",
            "voa-chinese-news",
            "easy-mandarin-news",
            "traveldaily-cn",
            "news-central-site",
        },
    )


def _address(address: str, family: int) -> tuple:
    return family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, 443)


def test_feed_validation_uses_public_ipv4_even_when_dns64_ipv6_is_present(monkeypatch):
    calls = []

    def resolve(host, port, *, family, type):
        calls.append((host, port, family, type))
        return [_address("1.1.1.1", socket.AF_INET)]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)

    _validated_feed_url("https://news.example/feed.xml")

    assert calls == [("news.example", 443, socket.AF_INET, socket.SOCK_STREAM)]


def test_feed_validation_rejects_private_ipv4(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [_address("10.0.0.8", socket.AF_INET)],
    )

    with pytest.raises(ValueError, match="non-public"):
        _validated_feed_url("https://news.example/feed.xml")


def test_feed_validation_rejects_a_hostname_with_any_private_dns_answer(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            _address("1.1.1.1", socket.AF_INET),
            _address("192.168.1.10", socket.AF_INET),
        ],
    )

    with pytest.raises(ValueError, match="non-public"):
        _validated_feed_url("https://mixed.example/feed.xml")


def test_feed_socket_connects_to_the_validated_ip_without_a_second_dns_lookup(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [_address("1.1.1.1", socket.AF_INET)],
    )
    connected_hosts = []
    stream = object()

    def connect(_backend, host, _port, **_kwargs):
        connected_hosts.append(host)
        return stream

    monkeypatch.setattr(collector.httpcore.SyncBackend, "connect_tcp", connect)

    result = collector.PinnedPublicIPv4Backend().connect_tcp("feed.example", 443)

    assert result is stream
    assert connected_hosts == ["1.1.1.1"]


def test_pinned_feed_backend_rejects_mixed_dns_before_opening_a_socket(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            _address("1.1.1.1", socket.AF_INET),
            _address("10.0.0.5", socket.AF_INET),
        ],
    )
    monkeypatch.setattr(
        collector.httpcore.SyncBackend,
        "connect_tcp",
        lambda *_args, **_kwargs: pytest.fail("socket must not open for mixed DNS answers"),
    )

    with pytest.raises(ValueError, match="non-public"):
        collector.PinnedPublicIPv4Backend().connect_tcp("feed.example", 443)


@pytest.mark.parametrize(
    "url",
    [
        "http://news.example/feed.xml",
        "https://user:pass@news.example/feed.xml",
        "https://news.example:8443/feed.xml",
        "file:///etc/passwd",
        "https://localhost/feed.xml",
        "https://metadata.google.internal/feed.xml",
    ],
)
def test_feed_validation_rejects_unsafe_url_shapes(monkeypatch, url):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [_address("1.1.1.1", socket.AF_INET)],
    )

    with pytest.raises(ValueError):
        _validated_feed_url(url)


def test_feed_response_is_capped_while_streaming():
    response = httpx.Response(200, content=b"12345")
    response.headers.pop("content-length")

    with pytest.raises(ValueError, match="configured size limit"):
        _read_limited_response(response, limit=4)


def test_article_extractor_keeps_main_story_and_skips_navigation_and_script():
    document = """
    <html><body><nav>Menu must not appear</nav><main><h1>Story heading</h1>
    <p>The event and important result.</p><script>tracking secret</script>
    <footer>Footer must not appear</footer></main></body></html>
    """

    text = extract_article_text(document)

    assert "Story heading" in text
    assert "important result" in text
    assert "Menu must not appear" not in text
    assert "tracking secret" not in text
    assert "Footer must not appear" not in text


def test_article_url_requires_exact_allowlisted_public_https_host(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [_address("1.1.1.1", socket.AF_INET)],
    )

    assert _validated_article_url("https://openai.com/news/story", ["openai.com"])
    with pytest.raises(ValueError, match="allowlist"):
        _validated_article_url("https://openai.com.evil.example/story", ["openai.com"])


def test_article_reader_does_not_forward_publisher_url_to_third_party_when_blocked(monkeypatch):
    url = "https://openai.com/index/blocked-unique-regression/"
    monkeypatch.setattr(collector, "_validated_article_url", lambda value, hosts: value)
    monkeypatch.setattr(
        collector,
        "_fetch_article_from_publisher",
        lambda *_args: (_ for _ in ()).throw(httpx.HTTPStatusError("blocked", request=None, response=None)),
    )
    assert fetch_article_text(url, ["openai.com"]) == ""


def test_easy_mandarin_index_parser_keeps_chinese_title_and_fresh_story_url():
    index = """<html><a href="/en/articles/AbCdEf123456"><div><h3 lang="zh-Hans"><span>中国</span>议题进入美国中期选举</h3><p>English title</p><time dateTime="2026-10-21T01:10:59.263Z">Oct 21</time><time dateTime="2026-10-19T18:51:26Z">Original date</time></div></a><a href="/about"><h3 lang="zh">Not a story</h3></a></html>""".encode()

    entries = _parse_feed_document(
        index,
        {"format": "html_index", "url": "https://easymandarin.news/en"},
    )

    assert len(entries) == 1
    assert entries[0]["title"] == "中国议题进入美国中期选举"
    assert entries[0]["link"] == "https://easymandarin.news/en/articles/AbCdEf123456"
    assert entries[0]["published"] == "2026-10-21T01:10:59.263Z"
    assert _parse_date(entries[0]) == datetime(2026, 10, 21, 1, 10, 59, 263000)


def test_easy_mandarin_article_extractor_uses_full_translation_paragraphs_only():
    document = """<article><h1 lang="zh-Hans"><div class="text-xs">zhōng guó</div><span>中国新闻</span></h1><div id="controls">HSK settings must not appear</div><p class="font-serif italic text-muted-foreground">The first complete event paragraph.</p><p class="font-serif italic text-muted-foreground">The outcome and next step.</p><a href="https://www.voanews.com/story">Original reporting link</a><audio src="https://example.test/audio.mp3"></audio></article><footer>Footer noise</footer>"""

    text = extract_easy_mandarin_article_text(document)

    assert text == "The first complete event paragraph. The outcome and next step."
    assert "HSK" not in text
    assert "zhōng guó" not in text
    assert "Original reporting link" not in text


def test_traveldaily_index_parser_keeps_fresh_dated_article_cards_only():
    index = b'''<a href="/article/190902/"><div><h3 class="newsFlashTitle">\xe6\x96\x87\xe6\x97\x85\xe7\x9b\xae\xe7\x9a\x84\xe5\x9c\xb0\xe6\x96\xb0\xe5\x8a\xa8\xe6\x80\x81</h3><div class="meta_left"><span>1\xe5\xa4\xa9\xe5\x89\x8d</span></div></div></a><a href="/about/"><h3 class="title">\xe4\xb8\x8d\xe6\x98\xaf\xe6\x96\x87\xe7\xab\xa0</h3></a>'''

    entries = _parse_feed_document(
        index,
        {
            "format": "html_index",
            "parser": "traveldaily",
            "url": "https://www.traveldaily.cn/destination/",
        },
    )

    assert len(entries) == 1
    assert entries[0]["title"] == "文旅目的地新动态"
    assert entries[0]["link"] == "https://www.traveldaily.cn/article/190902/"
    assert _parse_date(entries[0]) > collector.utc_naive_now() - timedelta(days=2)


def test_traveldaily_article_requires_own_byline_and_per_page_license_marker():
    body = "<p>" + "这是一篇用于验证文章正文提取与授权标记的段落。" * 12 + "</p>"
    allowed = (
        '<span class="articleAuthorTop">环球旅讯 张记者</span>'
        '<div class="articleBody">' + body + '</div>'
        '<div class="articleCopyright"><a class="articleCopyRightDescLink">CC BY-NC 4.0</a></div>'
    )
    unlicensed = allowed.replace("CC BY-NC 4.0", "版权所有")
    third_party = allowed.replace("环球旅讯 张记者", "新华社 记者")

    article_text, author = collector.extract_traveldaily_article(allowed)

    assert len(article_text) >= 120
    assert author == "张记者"
    assert collector.extract_traveldaily_article(unlicensed) == ("", "")
    assert collector.extract_traveldaily_article(third_party) == ("", "")


def test_fetch_article_text_keeps_source_id_for_source_specific_extraction(monkeypatch):
    observed = []
    monkeypatch.setattr(collector, "_validated_article_url", lambda value, _hosts: value)
    monkeypatch.setattr(
        collector,
        "_fetch_article_from_publisher",
        lambda value, source_id="": observed.append((value, source_id)) or "readable article text",
    )

    result = fetch_article_text(
        "https://easymandarin.news/en/articles/AbCdEf123456",
        ["easymandarin.news"],
        source_id="easy-mandarin-news",
    )

    assert result == "readable article text"
    assert observed == [
        ("https://easymandarin.news/en/articles/AbCdEf123456", "easy-mandarin-news")
    ]


def test_feed_response_accepts_bytes_within_limit():
    response = httpx.Response(200, content=b"1234")

    assert _read_limited_response(response, limit=4) == b"1234"


def test_entry_evidence_keeps_full_rss_content_and_strips_markup():
    summary, content = _entry_evidence(
        {
            "summary": "Short summary",
            "content": [{"value": "<p>Full <strong>story</strong>.</p><script>ignore()</script>"}],
        }
    )

    assert summary == "Short summary"
    assert content == "Full story."


def test_entry_content_can_supply_summary_when_feed_omits_it():
    summary, content = _entry_evidence(
        {"content": [{"value": "<p>Full story without a description.</p>"}]}
    )

    assert summary == "Full story without a description."
    assert content == summary


def test_collect_feed_saves_encoded_content_and_reports_entries_without_evidence(monkeypatch):
    rss = b"""<?xml version="1.0"?><rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel>
    <item><title>Qwen release</title><link>https://news.example/qwen</link><author>Jane Example</author><description>Brief summary</description><content:encoded>&lt;p&gt;Detailed source evidence about Qwen tool calling.&lt;/p&gt;</content:encoded></item>
    <item><title>Empty announcement</title><link>https://news.example/empty</link></item>
    <item><title>Invalid URL</title><link>https://user@news.example/story</link><description>Should not be saved.</description></item>
    </channel></rss>"""

    class Response:
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def raise_for_status(self):
            return None

        def iter_bytes(self):
            yield rss

    client_options = {}

    class Client:
        def __init__(self, **kwargs):
            client_options.update(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def stream(self, *_args, **_kwargs):
            return Response()

    class Query:
        def filter(self, *_args):
            return self

        def first(self):
            return None

    class Database:
        def query(self, *_args):
            return Query()

        def commit(self):
            return None

        def close(self):
            return None

    saved = []
    monkeypatch.setattr(collector, "_validated_feed_url", lambda _url: None)
    monkeypatch.setattr(collector.httpx, "Client", Client)
    monkeypatch.setattr(collector, "SessionLocal", Database)
    monkeypatch.setattr(collector, "add_article", lambda _db, **values: saved.append(values))

    result = collector.collect_feed(
        {"id": "test-feed", "url": "https://feed.example/rss", "category": "research"}
    )

    assert result["received"] == 3
    assert result["inserted"] == 1
    assert result["skipped_no_evidence"] == 1
    assert result["skipped_invalid_url"] == 1
    assert saved[0]["summary"] == "Brief summary"
    assert saved[0]["content"] == "Detailed source evidence about Qwen tool calling."
    assert saved[0]["author"] == "Jane Example"
    assert client_options["follow_redirects"] is False
    assert client_options["trust_env"] is False


def test_collect_feed_can_keep_title_only_candidates_for_required_full_text_read(monkeypatch):
    recent_date = format_datetime(datetime.now(UTC) - timedelta(hours=1))
    stale_date = format_datetime(datetime.now(UTC) - timedelta(days=5))
    rss = f"""<?xml version=\"1.0\"?><rss version=\"2.0\"><channel>
    <item><title>Open model release</title><link>https://huggingface.co/blog/open-model</link><pubDate>{recent_date}</pubDate></item>
    <item><title>Old model release</title><link>https://huggingface.co/blog/old-model</link><pubDate>{stale_date}</pubDate></item>
    </channel></rss>""".encode()

    class Response:
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def raise_for_status(self):
            return None

        def iter_bytes(self):
            yield rss

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def stream(self, *_args, **_kwargs):
            return Response()

    class Query:
        def filter(self, *_args):
            return self

        def first(self):
            return None

    class Database:
        def query(self, *_args):
            return Query()

        def commit(self):
            return None

        def close(self):
            return None

    saved = []
    monkeypatch.setattr(collector, "_validated_feed_url", lambda _url: None)
    monkeypatch.setattr(collector.httpx, "Client", Client)
    monkeypatch.setattr(collector, "SessionLocal", Database)
    monkeypatch.setattr(collector, "add_article", lambda _db, **values: saved.append(values))

    result = collector.collect_feed(
        {
            "id": "huggingface-blog",
            "url": "https://huggingface.co/blog/feed.xml",
            "category": "open-source",
            "allow_title_only": True,
            "title_only_max_age_hours": 48,
        }
    )

    assert result["inserted"] == 1
    assert result["skipped_no_evidence"] == 0
    assert result["skipped_stale_title_only"] == 1
    assert saved[0]["title"] == "Open model release"
    assert saved[0]["summary"] == ""
    assert saved[0]["content"] == ""


def test_collect_feed_enriches_cleared_easy_mandarin_index_entries(monkeypatch):
    published = datetime.now(UTC).isoformat()
    index = (
        '<a href="/en/articles/abcdefgh1234"><h2 lang="zh">中国养老服务新职业</h2>'
        f'<time datetime="{published}"></time></a>'
    ).encode()

    class Response:
        headers = {"content-type": "text/html; charset=utf-8"}

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def raise_for_status(self):
            return None

        def iter_bytes(self):
            yield index

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def stream(self, *_args, **_kwargs):
            return Response()

    class ExistingArticle:
        summary = ""
        content = ""

    class Query:
        def filter(self, *_args):
            return self

        def first(self):
            return ExistingArticle()

    class Database:
        def query(self, *_args):
            return Query()

        def commit(self):
            return None

        def close(self):
            return None

    fetched = []
    saved = []
    monkeypatch.setattr(collector, "_validated_feed_url", lambda _url: None)
    monkeypatch.setattr(collector.httpx, "Client", Client)
    monkeypatch.setattr(collector, "SessionLocal", Database)
    monkeypatch.setattr(
        collector,
        "fetch_article_text",
        lambda url, hosts, source_id: fetched.append((url, hosts, source_id))
        or "Licensed source evidence about new elderly care occupations.",
    )
    monkeypatch.setattr(collector, "add_article", lambda _db, **values: saved.append(values))

    result = collector.collect_feed(
        {
            "id": "easy-mandarin-news",
            "url": "https://easymandarin.news/en",
            "format": "html_index",
            "article_hosts": ["easymandarin.news"],
            "category": "中国及国际新闻（AI 二次整理）",
            "fetch_article_body": True,
            "allow_title_only": False,
        }
    )

    assert result["updated"] == 1
    assert result["skipped_no_evidence"] == 0
    assert fetched == [
        (
            "https://easymandarin.news/en/articles/abcdefgh1234",
            ["easymandarin.news"],
            "easy-mandarin-news",
        )
    ]
    assert saved[0]["summary"].startswith("Licensed source evidence")
    assert saved[0]["content"] == "Licensed source evidence about new elderly care occupations."

    monkeypatch.setattr(
        collector,
        "_parse_feed_document",
        lambda *_args: [
            {
                "title": "未标注日期的旧文章",
                "link": "https://easymandarin.news/en/articles/undated123456",
            }
        ],
    )
    result = collector.collect_feed(
        {
            "id": "easy-mandarin-news",
            "url": "https://easymandarin.news/en",
            "format": "html_index",
            "article_hosts": ["easymandarin.news"],
            "category": "中国及国际新闻（AI 二次整理）",
            "fetch_article_body": True,
            "allow_title_only": False,
        }
    )

    assert result["updated"] == 0
    assert result["inserted"] == 0
    assert result["skipped_missing_date"] == 1
    assert len(fetched) == 1
    assert len(saved) == 1


def test_collect_feed_respects_configured_entry_limit(monkeypatch):
    rss = b"""<?xml version="1.0"?><rss version="2.0"><channel>
    <item><title>First</title><link>https://news.example/1</link><description>Evidence one</description></item>
    <item><title>Second</title><link>https://news.example/2</link><description>Evidence two</description></item>
    <item><title>Third</title><link>https://news.example/3</link><description>Evidence three</description></item>
    </channel></rss>"""

    class Response:
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def raise_for_status(self):
            return None

        def iter_bytes(self):
            yield rss

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def stream(self, *_args, **_kwargs):
            return Response()

    class Query:
        def filter(self, *_args):
            return self

        def first(self):
            return None

    class Database:
        def query(self, *_args):
            return Query()

        def commit(self):
            return None

        def close(self):
            return None

    saved = []
    monkeypatch.setattr(collector, "_validated_feed_url", lambda _url: None)
    monkeypatch.setattr(collector.httpx, "Client", Client)
    monkeypatch.setattr(collector, "SessionLocal", Database)
    monkeypatch.setattr(collector, "add_article", lambda _db, **values: saved.append(values))

    result = collector.collect_feed(
        {
            "id": "test-feed",
            "url": "https://feed.example/rss",
            "max_entries": 2,
        }
    )

    assert result["received"] == 3
    assert result["processed"] == 2
    assert result["inserted"] == 2
    assert [row["title"] for row in saved] == ["First", "Second"]


def test_collect_feed_skips_source_without_full_rights_clearance(monkeypatch):
    monkeypatch.setattr(collector, "rights_cleared_source_ids", lambda: set())
    monkeypatch.setattr(
        collector,
        "_validated_feed_url",
        lambda _url: pytest.fail("Uncleared feeds must not make a network request."),
    )

    result = collector.collect_feed(
        {"id": "uncleared-feed", "url": "https://feed.example/rss"}
    )

    assert result == {"source_id": "uncleared-feed", "status": "skipped_rights"}


def test_collect_feed_requires_named_byline_when_feed_requires_one(monkeypatch):
    rss = b"""<?xml version=\"1.0\"?><rss version=\"2.0\"><channel>
    <item><title>Article without author</title><link>https://news.example/story</link><description>Evidence</description></item>
    <item><title>Article with generic author label</title><link>https://news.example/guest-story</link><author>Guest Contributor</author><description>Evidence</description></item>
    </channel></rss>"""

    class Response:
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def raise_for_status(self):
            return None

        def iter_bytes(self):
            yield rss

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def stream(self, *_args, **_kwargs):
            return Response()

    class Database:
        def commit(self):
            return None

        def close(self):
            return None

    monkeypatch.setattr(collector, "_validated_feed_url", lambda _url: None)
    monkeypatch.setattr(collector.httpx, "Client", Client)
    monkeypatch.setattr(collector, "SessionLocal", Database)
    monkeypatch.setattr(
        collector,
        "add_article",
        lambda *_args, **_kwargs: pytest.fail("Missing byline must be skipped."),
    )

    result = collector.collect_feed(
        {
            "id": "test-feed",
            "url": "https://feed.example/rss",
            "require_author": True,
            "require_named_author": True,
        }
    )

    assert result["inserted"] == 0
    assert result["skipped_missing_author"] == 2


def test_global_voices_republication_filter_rejects_partner_copy_but_keeps_native_byline():
    assert collector._is_external_republication(
        "global-voices",
        "This article was originally published by Hong Kong Free Press. "
        "The following version is republished on Global Voices under a content partnership agreement.",
    )
    assert not collector._is_external_republication(
        "global-voices",
        "Originally published on Global Voices. Written by Daria Dergacheva. "
        "A Global Voices report about digital surveillance.",
    )
    assert not collector._is_external_republication(
        "another-source",
        "This article was originally published by another publisher.",
    )


def test_voa_filter_rejects_explicit_third_party_agency_material():
    assert collector.contains_restricted_agency_material(
        "voa-chinese-news", "据路透社报道，消息人士表示……"
    )
    assert collector.contains_restricted_agency_material(
        "voa-chinese-news", "The Associated Press contributed reporting."
    )
    assert collector.contains_restricted_agency_material(
        "voa-chinese-news", "法新社提供的材料显示……"
    )
    assert not collector.contains_restricted_agency_material(
        "voa-chinese-news", "美国之音记者在北京报道了这起事件。"
    )
    assert not collector.contains_restricted_agency_material(
        "another-source", "路透社报道"
    )


def test_collect_feed_skips_voa_third_party_content_before_storage(monkeypatch):
    rss = """<?xml version=\"1.0\"?><rss version=\"2.0\"><channel>
    <item><title>Agency story</title><link>https://www.voachinese.com/story</link>
    <description><![CDATA[据路透社报道，相关消息今天发布。]]></description></item>
    </channel></rss>""".encode()

    class Response:
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def raise_for_status(self):
            return None

        def iter_bytes(self):
            yield rss

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def stream(self, *_args, **_kwargs):
            return Response()

    class Database:
        def commit(self):
            return None

        def close(self):
            return None

    monkeypatch.setattr(collector, "_validated_feed_url", lambda _url: None)
    monkeypatch.setattr(collector.httpx, "Client", Client)
    monkeypatch.setattr(collector, "SessionLocal", Database)
    monkeypatch.setattr(
        collector,
        "add_article",
        lambda *_args, **_kwargs: pytest.fail("Third-party agency material must not be stored."),
    )

    result = collector.collect_feed(
        {
            "id": "voa-chinese-news",
            "url": "https://www.voachinese.com/api/rss",
            "exclude_third_party_agencies": True,
        }
    )

    assert result["inserted"] == 0
    assert result["skipped_third_party_agencies"] == 1


def test_collect_feed_skips_global_voices_partner_republication_before_storage(monkeypatch):
    rss = b"""<?xml version=\"1.0\"?><rss version=\"2.0\"><channel>
    <item><title>Partner story</title><link>https://globalvoices.org/story</link>
    <author>Partner Publisher</author><description><![CDATA[This article was originally published by Partner Publisher. The following version is republished on Global Voices under a content partnership agreement. Full text.]]></description></item>
    </channel></rss>"""

    class Response:
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def raise_for_status(self):
            return None

        def iter_bytes(self):
            yield rss

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def stream(self, *_args, **_kwargs):
            return Response()

    class Database:
        def commit(self):
            return None

        def close(self):
            return None

    monkeypatch.setattr(collector, "_validated_feed_url", lambda _url: None)
    monkeypatch.setattr(collector.httpx, "Client", Client)
    monkeypatch.setattr(collector, "SessionLocal", Database)
    monkeypatch.setattr(
        collector,
        "add_article",
        lambda *_args, **_kwargs: pytest.fail("Partner-republished text must not be stored."),
    )

    result = collector.collect_feed(
        {
            "id": "global-voices",
            "url": "https://globalvoices.org/feed/",
            "require_author": True,
            "exclude_external_republication": True,
        }
    )

    assert result["inserted"] == 0
    assert result["skipped_external_republication"] == 1


def test_collect_all_does_not_fetch_any_current_uncleared_source(monkeypatch):
    monkeypatch.setattr(collector, "rights_cleared_source_ids", lambda: set())
    monkeypatch.setattr(collector, "metadata_only_source_ids", lambda: set())
    monkeypatch.setattr(collector, "source_quality_hold_ids", lambda: set())
    monkeypatch.setattr(
        collector,
        "collect_feed",
        lambda _feed: pytest.fail("Uncleared feeds must not be collected."),
    )

    result = collector.collect_all()

    assert result["successes"] == 0
    assert result["failures"] == 0
    assert result["skipped_rights"] == len(collector.source_registry())
    assert result["skipped_quality"] == 0


def test_collect_all_skips_quality_held_sources_without_fetching(monkeypatch):
    held_source_id = "easy-mandarin-news"
    monkeypatch.setattr(collector, "rights_cleared_source_ids", lambda: {held_source_id})
    monkeypatch.setattr(collector, "metadata_only_source_ids", lambda: set())
    monkeypatch.setattr(collector, "source_quality_hold_ids", lambda: {held_source_id})
    monkeypatch.setattr(
        collector,
        "collect_feed",
        lambda feed: pytest.fail(f"Quality-held source must not be collected: {feed['id']}"),
    )

    result = collector.collect_all()

    held = next(row for row in result["results"] if row["source_id"] == held_source_id)
    assert held["status"] == "skipped_quality"
    assert result["skipped_quality"] == 1
