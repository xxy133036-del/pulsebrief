from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as file:
        return list(csv.DictReader(file))


def label_counts(rows: list[dict[str, str]], field: str, allowed: set[str]) -> dict[str, int]:
    counts = {label: 0 for label in sorted(allowed)}
    for row in rows:
        value = row.get(field, "").strip().lower()
        if not value:
            continue
        if value not in allowed:
            raise ValueError(f"Invalid {field} label {value!r}; expected one of {sorted(allowed)}")
        counts[value] += 1
    return counts


def ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def main() -> int:
    parser = argparse.ArgumentParser(description="Score completed human Agent quality-review CSVs.")
    parser.add_argument("run_dir", type=Path)
    args = parser.parse_args()

    items = read_rows(args.run_dir / "manual-review-items.csv")
    candidates = read_rows(args.run_dir / "manual-review-candidates.csv")
    claims = read_rows(args.run_dir / "manual-review-fact-claims.csv")
    no_match = read_rows(args.run_dir / "manual-review-no-match.csv")

    item_relevance = label_counts(
        items, "manual_relevance", {"relevant", "partly_relevant", "irrelevant"}
    )
    candidate_relevance = label_counts(
        candidates, "manual_relevance", {"relevant", "partly_relevant", "irrelevant"}
    )
    no_match_correctness = label_counts(no_match, "manual_no_match_correct", {"yes", "no"})
    title_clarity = label_counts(items, "manual_title_clarity", {"clear", "unclear"})
    event_coverage = label_counts(
        items, "manual_event_coverage", {"complete", "partial", "missing"}
    )
    source_attribution = label_counts(
        items, "manual_source_attribution", {"explicit", "missing"}
    )
    key_fact_counts = {label: 0 for label in ("supported", "partly_supported", "unsupported", "unverifiable")}
    key_fact_rows = 0
    incomplete_key_fact_rows = 0
    for row in claims:
        key_label = row.get("is_key_fact", "").strip().lower()
        support_label = row.get("manual_support", "").strip().lower()
        if key_label and key_label not in {"yes", "no"}:
            raise ValueError(f"Invalid is_key_fact label {key_label!r}; expected yes or no")
        if support_label and support_label not in key_fact_counts:
            raise ValueError(
                f"Invalid manual_support label {support_label!r}; "
                f"expected one of {sorted(key_fact_counts)}"
            )
        if key_label == "yes":
            key_fact_rows += 1
            if not support_label:
                incomplete_key_fact_rows += 1
            else:
                key_fact_counts[support_label] += 1

    reviewed_item_count = sum(item_relevance.values())
    reviewed_candidate_count = sum(candidate_relevance.values())
    reviewed_no_match_count = sum(no_match_correctness.values())
    reviewed_key_fact_count = sum(key_fact_counts.values())
    profile_ids = {
        profile_id
        for row in candidates
        if row.get("manual_relevance", "").strip()
        for profile_id in row.get("profile_ids", "").split(";")
        if profile_id.strip()
    }
    result = {
        "human_review_only": True,
        "accepted_outputs_reviewed": reviewed_item_count,
        "accepted_output_relevance": item_relevance,
        "accepted_output_relevance_rate": ratio(item_relevance["relevant"], reviewed_item_count),
        "titles_reviewed": sum(title_clarity.values()),
        "title_clarity": title_clarity,
        "clear_title_rate": ratio(title_clarity["clear"], sum(title_clarity.values())),
        "event_coverage_reviewed": sum(event_coverage.values()),
        "event_coverage": event_coverage,
        "complete_event_coverage_rate": ratio(
            event_coverage["complete"], sum(event_coverage.values())
        ),
        "source_attribution_reviewed": sum(source_attribution.values()),
        "source_attribution": source_attribution,
        "explicit_source_attribution_rate": ratio(
            source_attribution["explicit"], sum(source_attribution.values())
        ),
        "distinct_candidate_materials_reviewed": reviewed_candidate_count,
        "candidate_material_relevance": candidate_relevance,
        "candidate_material_relevance_rate": ratio(
            candidate_relevance["relevant"], reviewed_candidate_count
        ),
        "profiles_covered_by_reviewed_candidate_materials": len(profile_ids),
        "key_facts_reviewed": reviewed_key_fact_count,
        "key_fact_support": key_fact_counts,
        "key_fact_support_rate": ratio(key_fact_counts["supported"], reviewed_key_fact_count),
        "key_fact_rows_missing_support_label": incomplete_key_fact_rows,
        "no_match_samples_reviewed": reviewed_no_match_count,
        "no_match_correctness": no_match_correctness,
        "no_match_correct_rate": ratio(no_match_correctness["yes"], reviewed_no_match_count),
        "pending": {
            "accepted_output_relevance": len(items) - reviewed_item_count,
            "title_clarity": len(items) - sum(title_clarity.values()),
            "event_coverage": len(items) - sum(event_coverage.values()),
            "source_attribution": len(items) - sum(source_attribution.values()),
            "distinct_candidate_materials": len(candidates) - reviewed_candidate_count,
            "key_claim_classification": sum(not row.get("is_key_fact", "").strip() for row in claims),
            "no_match_correctness": len(no_match) - reviewed_no_match_count,
        },
        "acceptance_note": (
            "These are human-entered labels. A06 is not declared passed by this script; "
            "review coverage, profile balance, duplicate-event checks, and the documented "
            "acceptance rules still require review."
        ),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
