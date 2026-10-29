import importlib.util
from pathlib import Path

from app.services.news import (
    cleared_source_ids_from_register,
    metadata_only_source_ids,
    metadata_only_source_ids_from_register,
    rights_cleared_source_ids,
    source_quality_hold_ids,
)

ROOT = Path(__file__).resolve().parents[2]
SCRIPT_CANDIDATES = [ROOT / "scripts" / "check_source_rights.py", ROOT / "backend" / "scripts" / "check_source_rights.py"]
SCRIPT_PATH = next(path for path in SCRIPT_CANDIDATES if path.exists())
SPEC = importlib.util.spec_from_file_location("check_source_rights", SCRIPT_PATH)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def test_rights_register_covers_enabled_feeds_and_documents_free_clearance():
    records, errors = module.audit()

    assert errors == []
    assert len(records) == len(module.load_json(module.FEEDS_PATH)["feeds"])
    assert {record["source_id"] for record in records if record["status"] == "cleared"} == {
        "eu-horizon-magazine",
        "global-voices",
        "easy-mandarin-news",
        "anoni-news-zh-cn",
        "traveldaily-cn",
        "news-central-site",
    }
    assert {record["source_id"] for record in records if record["status"] == "metadata_only"} == {
        "arxiv-cs-ai",
        "arxiv-cs-lg",
    }
    for record in records:
        assert record["license_granularity"] in {"source_wide", "per_article", "metadata_only"}
        review = record["terms_review"]
        assert review["checked_on"] in {
            "2026-10-19",
            "2026-10-20",
            "2026-10-22",
            "2026-10-23",
            "2026-10-24",
        }
        assert review["official_urls"]
        assert all(url.startswith("https://") for url in review["official_urls"])
        assert review["finding"]


def test_runtime_rights_allowlist_contains_only_source_with_full_clearance():
    assert rights_cleared_source_ids() == {
        "eu-horizon-magazine",
        "global-voices",
        "easy-mandarin-news",
        "anoni-news-zh-cn",
        "traveldaily-cn",
        "news-central-site",
    }


def test_documented_accuracy_hold_stays_separate_from_license_clearance():
    feed = next(
        item
        for item in module.load_json(module.FEEDS_PATH)["feeds"]
        if item["id"] == "easy-mandarin-news"
    )
    records = module.load_json(module.REGISTER_PATH)["sources"]
    rights_record = next(item for item in records if item["source_id"] == "easy-mandarin-news")

    assert feed["quality_hold"] is True
    assert feed["quality_hold_reason"].strip()
    assert feed["quality_hold_evidence"] == (
        "artifacts/quality-evaluation/2026-10-24-qwen-source-accuracy-hold.md"
    )
    assert source_quality_hold_ids() == {"easy-mandarin-news"}
    assert rights_record["status"] == "cleared"


def test_quality_hold_requires_reason_and_existing_repository_evidence():
    feed_doc = module.load_json(module.FEEDS_PATH)
    register_doc = module.load_json(module.REGISTER_PATH)
    feed = next(item for item in feed_doc["feeds"] if item["id"] == "easy-mandarin-news")
    feed["quality_hold_reason"] = " "
    feed["quality_hold_evidence"] = "../outside.md"

    _records, errors = module.audit_documents(feed_doc, register_doc)

    assert any("质量暂缓必须记录原因" in error for error in errors)
    assert any("仓库相对文件路径" in error for error in errors)


def test_arxiv_is_cleared_only_for_cc0_descriptive_metadata():
    expected = {"arxiv-cs-ai", "arxiv-cs-lg"}
    document = module.load_json(module.REGISTER_PATH)
    assert metadata_only_source_ids() == expected
    assert metadata_only_source_ids_from_register(document) == expected
    assert expected.isdisjoint(rights_cleared_source_ids())
    assert all(
        record["scopes"]["article_text_retrieval"] == "not_granted"
        for record in document["sources"]
        if record["source_id"] in expected
    )


def test_cleared_source_requires_documented_evidence_for_every_scope(monkeypatch):
    feed_doc = module.load_json(module.FEEDS_PATH)
    register_doc = module.load_json(module.REGISTER_PATH)
    record = register_doc["sources"][0]
    record["status"] = "cleared"

    monkeypatch.setattr(module, "load_json", lambda path: feed_doc if path == module.FEEDS_PATH else register_doc)
    _records, errors = module.audit()

    assert any("审核日期和证据引用" in error for error in errors)
    assert any("必须覆盖全部必需范围" in error for error in errors)


def test_source_terms_review_is_required_for_each_enabled_feed(monkeypatch):
    feed_doc = module.load_json(module.FEEDS_PATH)
    register_doc = module.load_json(module.REGISTER_PATH)
    register_doc["sources"][0].pop("terms_review")

    monkeypatch.setattr(module, "load_json", lambda path: feed_doc if path == module.FEEDS_PATH else register_doc)
    _records, errors = module.audit()

    assert any("官方条款核对记录不完整" in error for error in errors)


def test_per_article_license_source_cannot_be_cleared_at_source_level():
    document = module.load_json(module.REGISTER_PATH)
    record = next(item for item in document["sources"] if item["source_id"] == "arxiv-cs-ai")
    record.update(
        {
            "status": "cleared",
            "license_granularity": "per_article",
            "reviewed_on": "2026-10-20",
            "evidence_reference": "https://example.test/evidence",
            "scopes": {scope: "granted" for scope in module.REQUIRED_SCOPES},
        }
    )

    assert "arxiv-cs-ai" not in cleared_source_ids_from_register(document)
    _records, errors = module.audit_documents(
        module.load_json(module.FEEDS_PATH), document
    )
    assert any("单篇许可来源不能通过来源级 cleared 放行" in error for error in errors)


def test_malformed_register_fails_closed():
    assert cleared_source_ids_from_register([]) == set()
    assert cleared_source_ids_from_register({"sources": None}) == set()
