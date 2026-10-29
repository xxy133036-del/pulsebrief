from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = ROOT / "data"
if not (DATA_ROOT / "sources").exists() and (ROOT.parent / "data" / "sources").exists():
    DATA_ROOT = ROOT.parent / "data"
FEEDS_PATH = DATA_ROOT / "sources" / "feeds.json"
REGISTER_PATH = DATA_ROOT / "sources" / "rights_register.json"
REQUIRED_SCOPES = {
    "feed_metadata_collection",
    "article_text_retrieval",
    "third_party_ai_processing",
    "summary_retention",
    "public_app_display",
}


def load_json(path: Path) -> dict:
    with path.open(encoding="utf-8") as file:
        return json.load(file)


def audit_documents(feeds_document: dict, register: dict) -> tuple[list[dict], list[str]]:
    feeds = feeds_document.get("feeds", [])
    records = register.get("sources", [])
    errors: list[str] = []
    feed_ids = {item.get("id") for item in feeds if item.get("enabled")}
    record_ids = [item.get("source_id") for item in records]
    if len(record_ids) != len(set(record_ids)):
        errors.append("授权台账存在重复 source_id")
    if feed_ids != set(record_ids):
        errors.append(
            f"启用来源与授权台账不一致：缺少 {sorted(feed_ids - set(record_ids))}；"
            f"多出 {sorted(set(record_ids) - feed_ids)}"
        )
    for feed in feeds:
        if "quality_hold" not in feed:
            continue
        source_id = feed.get("id") or "unknown"
        if not isinstance(feed["quality_hold"], bool):
            errors.append(f"{source_id}: quality_hold 必须是布尔值")
            continue
        if not feed["quality_hold"]:
            continue
        reason = feed.get("quality_hold_reason")
        evidence = feed.get("quality_hold_evidence")
        if not isinstance(reason, str) or not reason.strip():
            errors.append(f"{source_id}: 质量暂缓必须记录原因")
        if not isinstance(evidence, str) or not evidence.strip():
            errors.append(f"{source_id}: 质量暂缓必须引用评测证据")
            continue
        evidence_path = Path(evidence)
        if (
            evidence_path.is_absolute()
            or ".." in evidence_path.parts
            or evidence_path.suffix.lower() not in {".md", ".json", ".txt"}
        ):
            errors.append(f"{source_id}: 质量暂缓证据必须使用安全的仓库相对文件路径")
    for record in records:
        scopes = record.get("scopes", {})
        granularity = record.get("license_granularity")
        if granularity not in {"source_wide", "per_article", "metadata_only"}:
            errors.append(f"{record.get('source_id')}: 必须明确许可按来源、单篇文章或仅元数据核验")
        if set(scopes) != REQUIRED_SCOPES:
            errors.append(f"{record.get('source_id')}: 授权范围字段不完整")
        review = record.get("terms_review", {})
        if not review.get("checked_on") or not review.get("official_urls") or not review.get("finding"):
            errors.append(f"{record.get('source_id')}: 官方条款核对记录不完整")
        elif any(
            not isinstance(url, str) or not url.startswith("https://")
            for url in review["official_urls"]
        ):
            errors.append(f"{record.get('source_id')}: 官方条款链接必须使用 HTTPS")
        if record.get("status") == "cleared":
            if granularity == "per_article":
                errors.append(
                    f"{record.get('source_id')}: 单篇许可来源不能通过来源级 cleared 放行"
                )
            if not record.get("reviewed_on") or not record.get("evidence_reference"):
                errors.append(f"{record.get('source_id')}: cleared 必须有审核日期和证据引用")
            if any(scopes.get(scope) != "granted" for scope in REQUIRED_SCOPES):
                errors.append(f"{record.get('source_id')}: cleared 必须覆盖全部必需范围")
        if record.get("status") == "metadata_only":
            metadata_scopes = REQUIRED_SCOPES - {"article_text_retrieval"}
            if granularity != "metadata_only":
                errors.append(f"{record.get('source_id')}: metadata_only 必须使用元数据许可范围")
            if not record.get("reviewed_on") or not record.get("evidence_reference"):
                errors.append(f"{record.get('source_id')}: metadata_only 必须有审核日期和证据引用")
            if any(scopes.get(scope) != "granted" for scope in metadata_scopes):
                errors.append(f"{record.get('source_id')}: metadata_only 必须覆盖元数据处理范围")
            if scopes.get("article_text_retrieval") != "not_granted":
                errors.append(f"{record.get('source_id')}: metadata_only 必须明确禁止读取全文")
    return records, errors


def audit() -> tuple[list[dict], list[str]]:
    return audit_documents(load_json(FEEDS_PATH), load_json(REGISTER_PATH))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Audit source-rights evidence coverage; does not determine legal sufficiency."
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="return non-zero while any enabled source is not fully documented as cleared",
    )
    args = parser.parse_args()
    records, errors = audit()
    for error in errors:
        print(f"ERROR: {error}")
    cleared = [record for record in records if record.get("status") == "cleared"]
    metadata_only = [record for record in records if record.get("status") == "metadata_only"]
    pending_rights = [
        record
        for record in records
        if record.get("status") not in {"cleared", "metadata_only", "verified_but_unreachable"}
    ]
    unreachable = [
        record for record in records if record.get("status") == "verified_but_unreachable"
    ]
    quality_holds = [
        feed
        for feed in load_json(FEEDS_PATH).get("feeds", [])
        if feed.get("quality_hold") is True
    ]
    blocked = [record for record in records if record.get("status") != "cleared"]
    print(
        f"来源授权台账：{len(cleared)} 条完整清权，"
        f"{len(metadata_only)} 条仅元数据范围可用，{len(pending_rights)} 条许可待核，"
        f"{len(unreachable)} 条许可已核但来源不可达，{len(quality_holds)} 条因准确性暂缓。"
    )
    for feed in quality_holds:
        print(f"- {feed.get('id')}: quality hold — {feed.get('quality_hold_reason')}")
    for record in metadata_only:
        print(f"- {record.get('source_id')}: 仅按登记范围使用描述性元数据；全文未授权")
    for record in pending_rights + unreachable:
        print(f"- {record.get('source_id')}: {record.get('status')}")
    if errors:
        return 2
    return 1 if args.strict and blocked else 0


if __name__ == "__main__":
    raise SystemExit(main())
