from __future__ import annotations

import argparse
import csv
import json
import re
import statistics
from collections import Counter
from pathlib import Path

from app.agent.runner import _numeric_facts
from app.db import SessionLocal
from app.models import NewsArticle
from sqlalchemy import select


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarize an Agent quality evaluation run.")
    parser.add_argument("run_dir", type=Path)
    args = parser.parse_args()

    manifest = json.loads((args.run_dir / "manifest.json").read_text(encoding="utf-8"))
    with (args.run_dir / "samples.jsonl").open(encoding="utf-8") as file:
        samples = [json.loads(line) for line in file if line.strip()]
    profile_by_id = {profile["id"]: profile for profile in manifest["profiles"]}
    profile_candidates = {
        profile["id"]: set(profile["candidate_ids"]) for profile in manifest["profiles"]
    }
    article_ids = {
        article_id for sample in samples for article_id in sample.get("candidate_ids", [])
    }
    db = SessionLocal()
    try:
        articles = {
            article.id: article
            for article in db.scalars(select(NewsArticle).where(NewsArticle.id.in_(article_ids)))
        }
    finally:
        db.close()

    status_counts = Counter(sample.get("status", "missing") for sample in samples)
    error_counts = Counter(
        sample.get("error_reason", sample.get("error_type", "unknown"))
        for sample in samples
        if sample.get("status") == "error"
    )
    output_items = []
    fact_claims = []
    invalid_item_ids = 0
    missing_source_urls = 0
    overlength_items = 0
    duplicate_ids = 0
    unsupported_numeric_claims = []
    unsupported_overview_numeric_claims = []
    for sample in samples:
        sample_items = sample.get("items", [])
        ids = [item.get("article_id") for item in sample_items]
        duplicate_ids += len(ids) != len(set(ids))
        profile = profile_by_id[sample["profile_id"]]
        for item in sample_items:
            article_id = item.get("article_id")
            article = articles.get(article_id)
            if article is None or article_id not in profile_candidates[sample["profile_id"]]:
                invalid_item_ids += 1
                continue
            summary = str(item.get("summary", ""))
            source = " ".join((article.title or "", article.summary or "", article.content or ""))
            unsupported = sorted(_numeric_facts(summary) - _numeric_facts(source))
            if unsupported:
                unsupported_numeric_claims.append(
                    {
                        "sample_index": sample["index"],
                        "profile_id": sample["profile_id"],
                        "article_id": article_id,
                        "unsupported_values": unsupported,
                    }
                )
            overlength_items += len(summary) > 240
            missing_source_urls += not str(item.get("source_url", "")).startswith("https://")
            output_items.append(
                {
                    "sample_index": sample["index"],
                    "profile_id": sample["profile_id"],
                    "topics": ", ".join(profile["topics"]),
                    "keywords": ", ".join(profile["keywords"]),
                    "article_id": article_id,
                    "title_zh": str(item.get("title_zh", "")),
                    "title": article.title,
                    "source_name": str(item.get("source_name", "")),
                    "source_url": article.url,
                    "summary": summary,
                    "source_excerpt": " ".join(
                        (article.summary or article.content or "").split()
                    )[:3000],
                    "manual_relevance": "",
                    "manual_title_clarity": "",
                    "manual_event_coverage": "",
                    "manual_source_attribution": "",
                    "review_notes": "",
                }
            )
            evidence_text = " ".join(
                (article.summary or article.content or "").split()
            )[:3000]
            for claim_index, claim in enumerate(_split_claims(summary), start=1):
                fact_claims.append(
                    {
                        "sample_index": sample["index"],
                        "profile_id": profile["id"],
                        "article_id": article_id,
                        "title": article.title,
                        "source_url": article.url,
                        "claim_index": claim_index,
                        "claim_text": claim,
                        "source_text": evidence_text,
                        "is_key_fact": "",
                        "manual_support": "",
                        "review_notes": "",
                    }
                )
        if sample_items:
            selected_source = " ".join(
                " ".join(
                    (
                        articles[item["article_id"]].title or "",
                        articles[item["article_id"]].summary or "",
                        articles[item["article_id"]].content or "",
                    )
                )
                for item in sample_items
                if item.get("article_id") in articles
            )
            overview_text = re.sub(
                r"本期收录\s+\d+\s+条匹配新闻[：:]?", "", str(sample.get("summary", ""))
            )
            unsupported_overview = sorted(
                _numeric_facts(overview_text) - _numeric_facts(selected_source)
            )
            if unsupported_overview:
                unsupported_overview_numeric_claims.append(
                    {
                        "sample_index": sample["index"],
                        "unsupported_values": unsupported_overview,
                    }
                )

    no_match_rows = []
    for sample in samples:
        if sample.get("status") != "no_match":
            continue
        profile = profile_by_id[sample["profile_id"]]
        titles = [
            articles[article_id].title
            for article_id in sample.get("candidate_ids", [])
            if article_id in articles
        ]
        candidate_sources = []
        for article_id in sample.get("candidate_ids", []):
            article = articles.get(article_id)
            if article is None:
                continue
            candidate_sources.append(f"{article.title} — {article.url}")
        no_match_rows.append(
            {
                "sample_index": sample["index"],
                "profile_id": sample["profile_id"],
                "topics": ", ".join(profile["topics"]),
                "keywords": ", ".join(profile["keywords"]),
                "candidate_count": len(sample.get("candidate_ids", [])),
                "candidate_titles": " | ".join(titles),
                "candidate_sources": "\n".join(candidate_sources),
                "model_summary": sample.get("summary", ""),
                "manual_no_match_correct": "",
                "review_notes": "",
            }
        )

    candidate_profiles: dict[str, list[str]] = {}
    for profile in manifest["profiles"]:
        for article_id in profile["candidate_ids"]:
            candidate_profiles.setdefault(article_id, []).append(profile["id"])
    candidate_rows = []
    for article_id, article in sorted(articles.items(), key=lambda row: row[1].title or ""):
        profiles = [
            profile_by_id[profile_id]
            for profile_id in candidate_profiles.get(article_id, [])
            if profile_id in profile_by_id
        ]
        candidate_rows.append(
            {
                "article_id": article_id,
                "title": article.title,
                "source_url": article.url,
                "candidate_profile_count": len(profiles),
                "profile_ids": "; ".join(profile["id"] for profile in profiles),
                "profile_preferences": " | ".join(
                    f"{profile['id']}: topics={', '.join(profile['topics'])}; "
                    f"keywords={', '.join(profile['keywords'])}; "
                    f"excluded={', '.join(profile['excluded_keywords']) or '(none)'}"
                    for profile in profiles
                ),
                "source_text": " ".join(
                    (article.summary or article.content or "").split()
                )[:3000],
                "manual_relevance": "",
                "review_notes": "",
            }
        )

    elapsed = [float(sample["elapsed_seconds"]) for sample in samples if "elapsed_seconds" in sample]
    sorted_elapsed = sorted(elapsed)
    report = {
        "sample_rows": len(samples),
        "profile_count": len(manifest["profiles"]),
        "distinct_candidate_materials": len(manifest["candidate_materials"]),
        "status_counts": dict(status_counts),
        "error_reasons": dict(error_counts),
        "retryable_error_samples": sum(
            sample.get("status") == "error" and bool(sample.get("retryable"))
            for sample in samples
        ),
        "published_item_count": len(output_items),
        "candidate_membership_violations": invalid_item_ids,
        "missing_https_source_urls": missing_source_urls,
        "overlength_summaries_over_240_chars": overlength_items,
        "duplicate_article_ids_within_briefings": duplicate_ids,
        "unsupported_numeric_claims_against_full_candidate_text": unsupported_numeric_claims,
        "unsupported_overview_numeric_claims_against_published_item_sources": (
            unsupported_overview_numeric_claims
        ),
        "tool_names": dict(
            Counter(
                call.get("tool", "unknown")
                for sample in samples
                for call in sample.get("tool_calls", [])
            )
        ),
        "tool_calls_failed": sum(
            call.get("ok") is False
            for sample in samples
            for call in sample.get("tool_calls", [])
        ),
        "format_repair_attempted_samples": sum(
            bool(sample.get("format_repair_attempted"))
            for sample in samples
        ),
        "format_repair_succeeded_samples": sum(
            bool(sample.get("format_repair_used"))
            for sample in samples
        ),
        "token_usage_recorded_samples": sum(
            isinstance(sample.get("usage"), dict) for sample in samples
        ),
        "token_usage_missing_samples": sum(
            not isinstance(sample.get("usage"), dict) for sample in samples
        ),
        "total_prompt_tokens": sum(
            int(sample.get("usage", {}).get("prompt_tokens", 0) or 0)
            for sample in samples
        ),
        "total_completion_tokens": sum(
            int(sample.get("usage", {}).get("completion_tokens", 0) or 0)
            for sample in samples
        ),
        "elapsed_p50_seconds": statistics.median(elapsed) if elapsed else None,
        "elapsed_p95_seconds": sorted_elapsed[min(94, len(sorted_elapsed) - 1)]
        if sorted_elapsed
        else None,
        "annotation_scope": "No human relevance/fact labels are inferred by this analyzer.",
    }
    (args.run_dir / "analysis.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    for file_name, rows in (
        ("manual-review-items.csv", output_items),
        ("manual-review-no-match.csv", no_match_rows),
        ("manual-review-fact-claims.csv", fact_claims),
        ("manual-review-candidates.csv", candidate_rows),
    ):
        fieldnames = list(rows[0]) if rows else []
        with (args.run_dir / file_name).open(
            "w", encoding="utf-8-sig", newline=""
        ) as file:
            writer = csv.DictWriter(file, fieldnames=fieldnames)
            if fieldnames:
                writer.writeheader()
                writer.writerows(rows)
    print(json.dumps(report, ensure_ascii=False))
    return 0


def _split_claims(summary: str) -> list[str]:
    """Split a summary into reviewable sentences without splitting decimal numbers."""
    claims = re.split(r"(?<=[。！？!?；;])\s*|(?<!\d)\.(?!\d)\s+", summary)
    return [claim.strip() for claim in claims if claim.strip()]


if __name__ == "__main__":
    raise SystemExit(main())
