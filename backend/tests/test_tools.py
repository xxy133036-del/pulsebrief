import json

import pytest
from app.agent import tools
from app.agent.tools import ToolContext, ToolError, invoke_tool, prepare_task_workspace, search_news


@pytest.fixture
def context(tmp_path):
    root = tmp_path / "task"
    prepare_task_workspace(
        root,
        [
            {
                "id": "news-1",
                "title": "Open AI model",
                "summary": "A new model.",
                "url": "https://example.com",
            }
        ],
    )
    return ToolContext(task_id="task-1", root=root, candidates=[])


def test_required_tools_are_registered_and_can_read_write(context):
    assert {"list_dir", "read_file", "search_content", "write_file", "bash"}.issubset(
        {
            item["function"]["name"]
            for item in __import__(
                "app.agent.tools", fromlist=["tool_definitions"]
            ).tool_definitions()
        }
    )
    listing = invoke_tool("list_dir", {"path": "input"}, context)
    assert any(row["name"] == "evidence.json" for row in listing)
    contents = invoke_tool("read_file", {"path": "input/evidence.json"}, context)
    assert json.loads(contents["content"])["articles"][0]["id"] == "news-1"
    matches = invoke_tool("search_content", {"keyword": "new model", "directory": "input"}, context)
    assert len(matches["matches"]) >= 1
    saved = invoke_tool(
        "write_file", {"path": "outputs/briefing.json", "content": '{"ok":true}'}, context
    )
    assert saved["sha256"]
    assert (
        invoke_tool("bash", {"command": "python -m json.tool outputs/briefing.json"}, context)[
            "valid_json"
        ]
        is True
    )


@pytest.mark.parametrize(
    "path",
    ["../secrets.txt", "input/../../secrets.txt", "C:/Windows/win.ini", "input\\articles.json"],
)
def test_path_traversal_is_rejected(context, path):
    with pytest.raises(ToolError):
        invoke_tool("read_file", {"path": path}, context)


@pytest.mark.parametrize(
    "path",
    ["input/" + "a" * 4096, "input/" + "目录" * 128, "input/evidence\x00.json"],
)
def test_path_resource_limits_are_rejected(context, path):
    with pytest.raises(ToolError, match="Path"):
        invoke_tool("read_file", {"path": path}, context)


def test_symlinked_input_file_is_rejected(context):
    link = context.root / "input" / "linked.json"
    try:
        link.symlink_to(context.root / "input" / "evidence.json")
    except OSError as exc:
        pytest.skip(f"Symlinks are unavailable in this environment: {exc}")

    with pytest.raises(ToolError, match="Symbolic links"):
        invoke_tool("read_file", {"path": "input/linked.json"}, context)


def test_symlinked_directory_is_rejected_for_listing_and_search(context):
    link = context.root / "input" / "linked-dir"
    try:
        link.symlink_to(context.root / "input", target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"Symlinks are unavailable in this environment: {exc}")

    with pytest.raises(ToolError, match="Symbolic links"):
        invoke_tool("list_dir", {"path": "input/linked-dir"}, context)
    with pytest.raises(ToolError, match="Symbolic links"):
        invoke_tool(
            "search_content",
            {"keyword": "news", "directory": "input/linked-dir"},
            context,
        )


def test_read_file_rejects_symlink_swap_after_path_validation(context, tmp_path, monkeypatch):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "article.txt").write_text("race-canary-secret", encoding="utf-8")
    nested = context.root / "input" / "nested"
    nested.mkdir()
    (nested / "article.txt").write_text("safe evidence", encoding="utf-8")

    original_path = tools._path
    swapped = False

    def swap_directory_after_validation(ctx, raw, *, write=False):
        nonlocal swapped
        checked = original_path(ctx, raw, write=write)
        if raw == "input/nested" and not swapped:
            nested.rename(context.root / "input" / "nested-original")
            nested.symlink_to(outside, target_is_directory=True)
            swapped = True
        return checked

    monkeypatch.setattr(tools, "_path", swap_directory_after_validation)

    with pytest.raises(ToolError, match="Symbolic links"):
        tools.read_file(context, "input/nested/article.txt")


def test_write_through_symlinked_output_directory_is_rejected(context):
    link = context.root / "outputs" / "linked"
    try:
        link.symlink_to(context.root / "input", target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"Symlinks are unavailable in this environment: {exc}")

    with pytest.raises(ToolError, match="Symbolic links"):
        invoke_tool(
            "write_file",
            {"path": "outputs/linked/briefing.txt", "content": "no"},
            context,
        )


def test_bash_is_not_a_general_shell(context):
    for command in (
        "whoami",
        "python -m json.tool outputs/briefing.json; whoami",
        "python -c pass",
    ):
        with pytest.raises(ToolError):
            invoke_tool("bash", {"command": command}, context)


def test_bash_rejects_oversized_allowlisted_command(context):
    command = "python -m json.tool outputs/" + "a" * 300 + ".json"
    with pytest.raises(ToolError, match="Only"):
        invoke_tool("bash", {"command": command}, context)


def test_search_news_returns_compact_evidence_without_source_urls(context):
    context.candidates = [
        {
            "id": "news-1",
            "title": "AI model release",
            "source_id": "official-feed",
            "category": "model",
            "published_at": "2026-10-14T00:00:00Z",
            "matched_topics": ["AI agents"],
            "summary": "evidence " + "x" * 4000,
            "content": "duplicate " + "y" * 4000,
            "url": "https://example.com/private-source-path",
        }
    ]

    result = search_news(context, "AI model")

    assert result["count"] == 1
    assert len(result["articles"][0]["evidence"]) == 1800
    assert "url" not in result["articles"][0]
    assert result["articles"][0]["id"] == "news-1"
    assert result["articles"][0]["matched_topics"] == ["AI agents"]


def test_metadata_abstract_read_never_fetches_paper_page(monkeypatch, context):
    from app.agent import tools

    context.source_article_hosts["arxiv-cs-ai"] = ["arxiv.org"]
    context.candidates = [
        {
            "id": "arxiv-1",
            "title": "A paper title",
            "source_id": "arxiv-cs-ai",
            "source_name": "arXiv cs.AI",
            "category": "research",
            "published_at": "2026-10-22T00:00:00Z",
            "matched_topics": ["AI"],
            "summary": "This abstract describes the research question and reported result.",
            "content": "",
            "evidence_scope": "metadata_abstract",
            "url": "https://arxiv.org/abs/1234.56789",
        }
    ]

    def reject_fetch(*_args, **_kwargs):
        raise AssertionError("metadata-only source must not fetch the paper page")

    monkeypatch.setattr("app.worker.collector.fetch_article_text", reject_fetch, raising=False)
    result = tools.read_article(context, "arxiv-1")
    search = tools.search_news(context, "paper")

    assert result["content_level"] == "metadata_abstract"
    assert result["article_text"].startswith("This abstract")
    assert search["articles"][0]["evidence_scope"] == "metadata_abstract"


def test_news_central_reads_complete_cc_by_story_from_licensed_rss(context, monkeypatch):
    from app.agent import tools

    complete_story = (
        "A substantial full story with event details and context. " * 30
        + "Yahoo News: https://publisher.example/story"
    )
    context.source_article_hosts["news-central-site"] = ["newscentral.site"]
    context.candidates = [
        {
            "id": "ncs-1",
            "title": "A full story",
            "source_id": "news-central-site",
            "source_name": "News Central Site",
            "category": "国际",
            "summary": "A short RSS description.",
            "content": complete_story,
            "url": "https://newscentral.site/a-full-story/",
        }
    ]

    def reject_fetch(*_args, **_kwargs):
        raise AssertionError("the licensed full story is already included in RSS")

    monkeypatch.setattr("app.worker.collector.fetch_article_text", reject_fetch)
    result = tools.read_article(context, "ncs-1")

    assert result["content_level"] == "full_article"
    assert result["source_name"] == "News Central Site"
    assert result["article_text"] == complete_story
    assert "Yahoo News" in result["article_text"]


def test_search_news_finds_candidate_by_server_matched_topic(context):
    context.candidates = [
        {
            "id": "alias-topic-1",
            "title": "Agent workflow release",
            "source_id": "official-feed",
            "category": "technology",
            "published_at": "2026-10-14T00:00:00Z",
            "matched_topics": ["智能体"],
            "summary": "A source-grounded release note.",
            "content": "The release adds multi-step task planning.",
        }
    ]

    result = search_news(context, "智能体")

    assert result["count"] == 1
    assert result["articles"][0]["id"] == "alias-topic-1"
    assert result["articles"][0]["matched_topics"] == ["智能体"]


def test_search_news_prefers_relevant_excerpt_from_full_rss_content(context):
    context.candidates = [
        {
            "id": "news-2",
            "title": "Engineering update",
            "source_id": "official-feed",
            "category": "tools",
            "published_at": "2026-10-14T00:00:00Z",
            "summary": "Short generic summary.",
            "content": ("A general product update. " * 200)
            + "The release adds Qwen function calling and JSON output controls. ",
            "url": "https://example.com/article",
        }
    ]

    result = search_news(context, "Qwen function calling")

    evidence = result["articles"][0]["evidence"]
    assert len(evidence) <= 1800
    assert "Qwen function calling" in evidence
    assert "Short generic summary." not in evidence


def test_search_news_excerpt_does_not_split_decimal_metrics(context):
    context.candidates = [
        {
            "id": "news-metrics",
            "title": "Secret detection benchmark",
            "source_id": "official-feed",
            "category": "security",
            "published_at": "2026-10-14T00:00:00Z",
            "summary": "The model improved precision.",
            "content": ("Unrelated release notes remain unchanged. " * 160)
            + "Owner-weighted alert precision improved 11.5%. Median scan time fell 86%.",
            "url": "https://example.com/metrics",
        }
    ]

    result = search_news(context, "owner weighted alert precision")

    evidence = result["articles"][0]["evidence"]
    assert "11.5%" in evidence
    assert "11. … 5%" not in evidence
