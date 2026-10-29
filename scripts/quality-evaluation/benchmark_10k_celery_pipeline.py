from __future__ import annotations

import argparse
import os
import shutil
import time
from datetime import timedelta
from pathlib import Path
from uuid import uuid4


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Exercise due-task dispatch and Celery using isolated demo-mode Redis DB 15."
    )
    parser.add_argument("--subscriptions", type=int, default=10_000)
    parser.add_argument("--timeout-seconds", type=int, default=900)
    parser.add_argument("--redis-url", default="redis://redis:6379/15")
    parser.add_argument(
        "--match-demo-candidates",
        action="store_true",
        help="Publish one synthetic, explicitly labeled demo briefing per subscription.",
    )
    args = parser.parse_args()
    if not 1 <= args.subscriptions <= 10_000 or args.timeout_seconds < 60:
        parser.error("subscriptions must be 1..10000 and timeout-seconds at least 60")

    os.environ["REDIS_URL"] = args.redis_url

    from app.celery_app import celery_app
    from app.config import get_settings
    from app.db import SessionLocal
    from app.models import Briefing, GenerationTask, NewsArticle, OutboxEvent, Subscription, User
    from app.scheduling import utc_naive_now
    from app.services.news import add_article
    from app.worker.tasks import dispatch_due
    from redis import Redis
    from sqlalchemy import delete, func, or_, select

    settings = get_settings()
    celery_app.conf.update(broker_url=args.redis_url, result_backend=args.redis_url)
    celery_app.set_current()
    if not settings.demo_mode:
        raise SystemExit("Refusing to run: DEMO_MODE must be true so no model is called.")
    if settings.database_url.startswith("sqlite"):
        raise SystemExit("Refusing to run: this benchmark requires the isolated Compose PostgreSQL service.")

    run_id = uuid4().hex
    email_prefix = f"capacity-{run_id}-"
    topic = "智能体" if args.match_demo_candidates else f"no-match-capacity-probe-{run_id}"
    article_source_id = f"capacity-probe-{run_id}"
    redis = Redis.from_url(args.redis_url)
    redis.ping()
    redis.flushdb()
    started = time.perf_counter()
    deadline = started + args.timeout_seconds
    seeded = False
    try:
        now = utc_naive_now()
        with SessionLocal() as db:
            if args.match_demo_candidates:
                add_article(
                    db,
                    source_id=article_source_id,
                    title="容量测试：智能体工作流程",
                    summary=(
                        "这是一条仅用于容量测试的智能体演示新闻，验证工具检索、来源关联、简报写入和任务状态。"
                    ),
                    content=(
                        "智能体演示素材仅用于本机压测，不代表实时新闻或外部事实。"
                        "订阅任务应通过新闻检索工具找到该来源并生成带演示标记的简报。"
                    ),
                    url=f"https://example.com/capacity/{run_id}",
                    category="智能体",
                    published_at=now,
                    is_demo=True,
                )
            users = [
                User(
                    email=f"{email_prefix}{index}@invalid.test",
                    display_name="Capacity probe",
                    is_active=True,
                    is_demo=True,
                )
                for index in range(args.subscriptions)
            ]
            db.add_all(users)
            db.flush()
            db.add_all(
                [
                    Subscription(
                        user_id=user.id,
                        topics=[topic],
                        keywords=[],
                        excluded_keywords=[],
                        enabled=True,
                        notify_enabled=False,
                        item_limit=1,
                        next_run_at=now - timedelta(minutes=1),
                    )
                    for user in users
                ]
            )
            db.commit()
        seeded = True
        seed_seconds = time.perf_counter() - started

        dispatch_started = time.perf_counter()
        dispatch_result = celery_app.tasks[dispatch_due.name]()
        dispatch_seconds = time.perf_counter() - dispatch_started
        if dispatch_result.get("created") != args.subscriptions or dispatch_result.get(
            "enqueued"
        ) != args.subscriptions:
            raise RuntimeError(f"Unexpected dispatch result: {dispatch_result}")

        previous_done = -1
        while time.perf_counter() < deadline:
            with SessionLocal() as db:
                counts = dict(
                    db.execute(
                        select(GenerationTask.status, func.count(GenerationTask.id))
                        .join(User, User.id == GenerationTask.user_id)
                        .where(User.email.like(f"{email_prefix}%"))
                        .group_by(GenerationTask.status)
                    ).all()
                )
            done = sum(counts.get(status, 0) for status in ("published", "no_match", "failed", "cancelled"))
            if done != previous_done:
                print(f"completed={done}/{args.subscriptions} states={counts}", flush=True)
                previous_done = done
            if done == args.subscriptions:
                total_seconds = time.perf_counter() - started
                expected_status = "published" if args.match_demo_candidates else "no_match"
                with SessionLocal() as diagnostics_db:
                    failed_tasks = diagnostics_db.scalars(
                        select(GenerationTask)
                        .join(User, User.id == GenerationTask.user_id)
                        .where(
                            User.email.like(f"{email_prefix}%"),
                            GenerationTask.status == "failed",
                        )
                        .order_by(GenerationTask.created_at)
                        .limit(10)
                    ).all()
                result = {
                    "subscriptions": args.subscriptions,
                    "scenario": expected_status,
                    "demo_mode": settings.demo_mode,
                    "external_model_requests": 0,
                    "demo_agent_runs": (
                        args.subscriptions if args.match_demo_candidates else 0
                    ),
                    "notifications_enabled": False,
                    "seed_seconds": round(seed_seconds, 3),
                    "dispatch_seconds": round(dispatch_seconds, 3),
                    "end_to_end_seconds": round(total_seconds, 3),
                    "terminal_states": counts,
                    "failure_samples": [
                        {
                            "stop_reason": task.stop_reason,
                            "error_message": task.error_message,
                        }
                        for task in failed_tasks
                    ],
                    "worker_concurrency_is_external_configuration": True,
                }
                print(f"RESULT={result}", flush=True)
                return 0 if counts.get(expected_status, 0) == args.subscriptions else 2
            time.sleep(2)
        raise TimeoutError(f"Capacity probe timed out; last states: {counts}")
    finally:
        if seeded:
            workspace_root = Path(settings.tool_task_root)
            if not workspace_root.is_absolute():
                workspace_root = Path.cwd() / workspace_root
            workspace_root = workspace_root.resolve()
            with SessionLocal() as db:
                task_ids = list(
                    db.scalars(
                        select(GenerationTask.id)
                        .join(User, User.id == GenerationTask.user_id)
                        .where(User.email.like(f"{email_prefix}%"))
                    ).all()
                )
                briefing_ids = list(
                    db.scalars(
                        select(Briefing.id)
                        .join(User, User.id == Briefing.user_id)
                        .where(User.email.like(f"{email_prefix}%"))
                    ).all()
                )
                db.execute(
                    delete(OutboxEvent).where(
                        or_(
                            OutboxEvent.aggregate_id.in_(task_ids),
                            OutboxEvent.aggregate_id.in_(briefing_ids),
                        )
                    )
                )
                db.execute(delete(User).where(User.email.like(f"{email_prefix}%")))
                if args.match_demo_candidates:
                    article = db.scalar(
                        select(NewsArticle).where(NewsArticle.source_id == article_source_id)
                    )
                    if article:
                        db.delete(article)
                db.commit()
            for task_id in task_ids:
                workspace = workspace_root / task_id
                try:
                    resolved = workspace.resolve(strict=True)
                except FileNotFoundError:
                    continue
                if resolved.parent == workspace_root and resolved.is_dir() and not workspace.is_symlink():
                    shutil.rmtree(resolved)
        redis.flushdb()
        redis.close()


if __name__ == "__main__":
    raise SystemExit(main())
