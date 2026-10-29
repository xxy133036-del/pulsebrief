from __future__ import annotations

import argparse
import json
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from app.agent.runner import AgentRunError, run_agent
from app.config import get_settings
from app.db import SessionLocal
from app.models import Subscription
from app.services.news import select_candidates

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PROFILES = ROOT / "artifacts/quality-evaluation/general-news-profiles.json"


def _profile_candidates(db, profile: dict, detail_level: str):
    subscription = Subscription(
        topics=profile["topics"],
        keywords=profile["keywords"],
        excluded_keywords=profile["excluded_keywords"],
        match_all=profile["match_all"],
        semantic_expansion=False,
        item_limit=5,
        detail_level=detail_level,
    )
    return select_candidates(db, subscription, demo_mode=False, limit=50)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run real-model Agent samples against frozen local news candidates."
    )
    parser.add_argument("--samples", type=int, default=100)
    parser.add_argument("--profiles", type=Path, default=DEFAULT_PROFILES)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--indexes", help="Comma-separated sample indexes to run or retry.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--detail-level", choices=("concise", "detailed"), default="detailed",
        help="Summary mode to evaluate; defaults to the production detailed mode.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "artifacts/quality-evaluation/2026-10-14-100-sample-run",
    )
    args = parser.parse_args()
    if args.samples < 1:
        parser.error("--samples must be positive")
    if not 1 <= args.concurrency <= 8:
        parser.error("--concurrency must be between 1 and 8")
    if args.indexes:
        try:
            requested_indexes = sorted({int(value) for value in args.indexes.split(",")})
        except ValueError:
            parser.error("--indexes must be comma-separated integers")
        if not requested_indexes or any(
            index < 1 or index > args.samples for index in requested_indexes
        ):
            parser.error("--indexes values must be between 1 and --samples")
    else:
        requested_indexes = list(range(1, args.samples + 1))

    settings = get_settings()
    if settings.demo_mode:
        raise SystemExit("Refusing to run a quality evaluation with the demo provider.")
    profiles = json.loads(args.profiles.read_text(encoding="utf-8-sig"))
    if len(profiles) < 30:
        raise SystemExit(f"Expected at least 30 preference profiles; found {len(profiles)}.")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    samples_path = args.output_dir / "samples.jsonl"
    manifest_path = args.output_dir / "manifest.json"
    previous_manifest = None
    if args.resume and manifest_path.exists():
        previous_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        # Historical runs predate this setting and always used concise summaries.
        if previous_manifest.get("detail_level", "concise") != args.detail_level:
            raise SystemExit("Cannot resume with a different summary detail level.")
    db = SessionLocal()
    try:
        profile_rows = []
        candidate_union = {}
        for profile in profiles:
            candidates = _profile_candidates(db, profile, args.detail_level)
            for article in candidates:
                candidate_union[article.id] = article
            profile_rows.append((profile, candidates))

        missing_profiles = [profile["id"] for profile, candidates in profile_rows if not candidates]
        coverage = {
            "checked_at": datetime.now(UTC).isoformat(),
            "detail_level": args.detail_level,
            "profile_count": len(profiles),
            "candidate_material_count": len(candidate_union),
            "missing_profiles": missing_profiles,
            "profiles": [
                {"id": profile["id"], "candidate_count": len(candidates)}
                for profile, candidates in profile_rows
            ],
            "ready": not missing_profiles and len(candidate_union) >= 200,
        }
        (args.output_dir / "candidate-coverage.json").write_text(
            json.dumps(coverage, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        if not coverage["ready"]:
            raise SystemExit(
                f"Candidate coverage incomplete: {len(missing_profiles)} empty profiles; "
                f"{len(candidate_union)}/200 distinct materials. See candidate-coverage.json."
            )
        # The database session is no longer needed during network calls. The ORM rows contain
        # only scalar columns and remain readable after the session closes.
        db.expunge_all()
        manifest = {
            "created_at": datetime.now(UTC).isoformat(),
            "provider": settings.llm_model,
            "demo_mode": settings.demo_mode,
            "detail_level": args.detail_level,
            "sample_target": args.samples,
            "sample_indexes": requested_indexes,
            "concurrency": args.concurrency,
            "profile_count": len(profiles),
            "candidate_material_count": len(candidate_union),
            "profiles": [
                {
                    "id": profile["id"],
                    "topics": profile["topics"],
                    "keywords": profile["keywords"],
                    "excluded_keywords": profile["excluded_keywords"],
                    "match_all": profile["match_all"],
                    "candidate_ids": [article.id for article in candidates],
                }
                for profile, candidates in profile_rows
            ],
            "candidate_materials": [
                {
                    "id": article.id,
                    "title": article.title,
                    "source_id": article.source_id,
                    "source_url": article.url,
                    "published_at": article.published_at.isoformat(),
                }
                for article in candidate_union.values()
            ],
        }
        if previous_manifest is not None:
            old_profile_ids = {
                profile["id"]: profile["candidate_ids"]
                for profile in previous_manifest.get("profiles", [])
            }
            current_profile_ids = {
                profile["id"]: profile["candidate_ids"] for profile in manifest["profiles"]
            }
            if old_profile_ids != current_profile_ids:
                raise SystemExit(
                    "Cannot resume because the frozen profile candidate sets have changed."
                )
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    finally:
        db.close()

    if args.dry_run:
        print(
            json.dumps(
                {
                    "dry_run": True,
                    "sample_target": args.samples,
                    "scheduled_indexes": len(requested_indexes),
                    "profile_count": len(profile_rows),
                    "candidate_material_count": len(candidate_union),
                    "output_dir": str(args.output_dir),
                },
                ensure_ascii=False,
            )
        )
        return 0

    existing_rows = {}
    if args.resume and samples_path.exists():
        with samples_path.open(encoding="utf-8") as samples_file:
            for line in samples_file:
                if line.strip():
                    row = json.loads(line)
                    existing_rows[int(row["index"])] = row
    completed_rows = dict(existing_rows)
    success_count = sum(
        row.get("status") in {"published", "no_match"} for row in existing_rows.values()
    )
    input_tokens = sum(
        int(row.get("usage", {}).get("prompt_tokens", 0) or 0)
        for row in existing_rows.values()
    )
    output_tokens = sum(
        int(row.get("usage", {}).get("completion_tokens", 0) or 0)
        for row in existing_rows.values()
    )

    def run_sample(index: int) -> dict:
        profile_index = (index - 1) % len(profile_rows)
        repetition = (index - 1) // len(profile_rows) + 1
        profile, candidates = profile_rows[profile_index]
        task_id = f"quality-{profile['id']}-{repetition}-{uuid4().hex[:8]}"
        subscription = {
            "topics": profile["topics"],
            "keywords": profile["keywords"],
            "excluded_keywords": profile["excluded_keywords"],
            "match_all": profile["match_all"],
            "semantic_expansion": False,
            "item_limit": 5,
            "detail_level": args.detail_level,
        }
        started = time.monotonic()
        try:
            result = run_agent(
                task_id=task_id,
                articles=candidates,
                subscription=subscription,
                settings=settings,
                workspace_base=Path(workspaces),
            )
            usage = result.get("usage", {})
            return {
                "index": index,
                "profile_id": profile["id"],
                "profile_repetition": repetition,
                "task_id": task_id,
                "candidate_ids": [article.id for article in candidates],
                "status": result.get("status"),
                "title": result.get("title"),
                "summary": result.get("summary"),
                "items": result.get("items", []),
                "tool_calls": [
                    {
                        "tool": call.get("tool"),
                        "ok": call.get("ok"),
                        "argument_keys": call.get("argument_keys", []),
                    }
                    for call in result.get("tool_calls", [])
                ],
                "usage": usage,
                "stop_reason": result.get("stop_reason"),
                "evidence_repair_used": result.get("evidence_repair_used", False),
                "evidence_rejections": result.get("evidence_rejections", []),
                "format_repair_used": result.get("format_repair_used", False),
                "elapsed_seconds": round(time.monotonic() - started, 3),
            }
        except AgentRunError as exc:
            return {
                "index": index,
                "profile_id": profile["id"],
                "profile_repetition": repetition,
                "task_id": task_id,
                "candidate_ids": [article.id for article in candidates],
                "status": "error",
                "error_reason": exc.reason,
                "error_type": type(exc).__name__,
                "error_message": str(exc)[:300],
                "usage": exc.usage,
                "format_repair_attempted": exc.format_repair_attempted,
                "retryable": exc.retryable,
                "elapsed_seconds": round(time.monotonic() - started, 3),
            }
        except Exception as exc:
            return {
                "index": index,
                "profile_id": profile["id"],
                "profile_repetition": repetition,
                "task_id": task_id,
                "candidate_ids": [article.id for article in candidates],
                "status": "error",
                "error_type": type(exc).__name__,
                "elapsed_seconds": round(time.monotonic() - started, 3),
            }

    with tempfile.TemporaryDirectory(prefix="daily-ai-news-quality-") as workspaces:
        open_mode = "a" if args.resume and samples_path.exists() else "w"
        with samples_path.open(open_mode, encoding="utf-8", newline="\n") as samples_file:
            pending_indexes = [index for index in requested_indexes if index not in existing_rows]
            with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
                futures = {executor.submit(run_sample, index): index for index in pending_indexes}
                for future in as_completed(futures):
                    row = future.result()
                    completed_rows[int(row["index"])] = row
                    success_count += row.get("status") in {"published", "no_match"}
                    input_tokens += int(row.get("usage", {}).get("prompt_tokens", 0) or 0)
                    output_tokens += int(
                        row.get("usage", {}).get("completion_tokens", 0) or 0
                    )
                    samples_file.write(json.dumps(row, ensure_ascii=False) + "\n")
                    samples_file.flush()
                    print(
                        f"{len(completed_rows)}/{args.samples} index={row['index']} "
                        f"profile={row['profile_id']} "
                        f"status={row['status']} elapsed={row['elapsed_seconds']:.1f}s",
                        flush=True,
                    )

    summary = {
        "sample_target": args.samples,
        "scheduled_indexes": len(requested_indexes),
        "sample_rows": len(completed_rows),
        "successful_results": success_count,
        "profile_count": len(profile_rows),
        "candidate_material_count": len(candidate_union),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "samples_file": samples_path.name,
    }
    manifest["run_summary"] = summary
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if success_count == len(requested_indexes) else 1


if __name__ == "__main__":
    raise SystemExit(main())
