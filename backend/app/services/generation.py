from pathlib import Path
from uuid import uuid4

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.agent.runner import AgentRunError, run_agent
from app.config import get_settings
from app.db import SessionLocal
from app.models import Briefing, GenerationTask, OutboxEvent, Subscription
from app.scheduling import business_date, next_send_at, utc_naive_now
from app.services.news import select_candidates


class TaskUnavailable(ValueError):
    pass


RETRYABLE_GENERATION_FAILURES = {
    "model_timeout", "model_rate_limited", "model_server_error", "model_network_error",
}


def _accumulate_token_usage(previous: dict | None, current: dict | None) -> dict:
    previous = previous if isinstance(previous, dict) else {}
    current = current if isinstance(current, dict) else {}
    result = {
        name: int(previous.get(name, 0) or 0) + int(current.get(name, 0) or 0)
        for name in ("prompt_tokens", "completion_tokens")
    }
    requests = []
    for source in (previous, current):
        steps = source.get("requests", [])
        if isinstance(steps, list):
            for step in steps:
                if isinstance(step, dict):
                    request = dict(step)
                    request["request"] = len(requests) + 1
                    requests.append(request)
    if requests:
        result["requests"] = requests
    return result


def create_generation_task(db: Session, user_id: str, *, preview: bool) -> GenerationTask:
    subscription = db.scalar(select(Subscription).where(Subscription.user_id == user_id))
    if subscription is None:
        raise TaskUnavailable("Save a subscription before generating a briefing.")
    if not subscription.topics and not subscription.keywords:
        raise TaskUnavailable("Choose at least one topic or keyword before previewing.")
    if not preview and not subscription.enabled:
        raise TaskUnavailable("This subscription is paused.")
    today = business_date()
    if preview:
        since = utc_naive_now().replace(hour=0, minute=0, second=0)
        count = (
            db.scalar(
                select(func.count())
                .select_from(GenerationTask)
                .where(
                    GenerationTask.user_id == user_id,
                    GenerationTask.kind.like("preview%"),
                    GenerationTask.created_at >= since,
                )
            )
            or 0
        )
        if count >= 3:
            raise TaskUnavailable("The daily preview limit has been reached.")
        kind = "preview-" + uuid4().hex[:12]
    else:
        kind = "daily"
        existing = db.scalar(
            select(GenerationTask).where(
                GenerationTask.subscription_id == subscription.id,
                GenerationTask.business_date == today,
                GenerationTask.kind == kind,
            )
        )
        if existing:
            return existing
    task = GenerationTask(
        user_id=user_id,
        subscription_id=subscription.id,
        business_date=today,
        kind=kind,
        status="queued",
        provider="demo" if get_settings().demo_mode else "openai_compatible",
        subscription_version=subscription.version,
    )
    db.add(task)
    db.flush()
    db.add(
        OutboxEvent(
            event_type="generation.requested",
            aggregate_id=task.id,
            payload={"task_id": task.id},
        )
    )
    db.commit()
    db.refresh(task)
    return task


def update_next_run(db: Session, subscription: Subscription, *, new_version: bool) -> None:
    if new_version:
        subscription.version = (subscription.version or 0) + 1
    subscription.updated_at = utc_naive_now()
    subscription.next_run_at = (
        next_send_at(subscription.send_time) if subscription.enabled else None
    )


def dispatch_due_subscriptions() -> int:
    db = SessionLocal()
    now = utc_naive_now()
    today = business_date(now)
    settings = get_settings()
    try:
        due = list(
            db.scalars(
                select(Subscription)
                .where(
                    Subscription.enabled.is_(True),
                    Subscription.next_run_at.is_not(None),
                    Subscription.next_run_at <= now,
                )
                .order_by(Subscription.next_run_at)
                .limit(settings.daily_dispatch_batch_size)
                .with_for_update(skip_locked=True)
            ).all()
        )
        if not due:
            return 0
        due_ids = [subscription.id for subscription in due]
        existing = set(
            db.scalars(
                select(GenerationTask.subscription_id).where(
                    GenerationTask.subscription_id.in_(due_ids),
                    GenerationTask.business_date == today,
                    GenerationTask.kind == "daily",
                )
            ).all()
        )
        new_tasks: list[GenerationTask] = []
        for subscription in due:
            if not subscription.topics and not subscription.keywords:
                subscription.next_run_at = next_send_at(subscription.send_time, after=now)
                continue
            if subscription.id not in existing:
                new_tasks.append(
                    GenerationTask(
                        user_id=subscription.user_id,
                        subscription_id=subscription.id,
                        business_date=today,
                        kind="daily",
                        status="queued",
                        provider="demo" if settings.demo_mode else "openai_compatible",
                        subscription_version=subscription.version,
                    )
                )
            subscription.next_run_at = next_send_at(subscription.send_time, after=now)
        db.add_all(new_tasks)
        db.flush()
        db.add_all(
            [
                OutboxEvent(
                    event_type="generation.requested",
                    aggregate_id=task.id,
                    payload={"task_id": task.id},
                )
                for task in new_tasks
            ]
        )
        db.commit()
        return len(new_tasks)
    finally:
        db.close()


def dispatch_task(task_id: str, *, retry_allowed: bool = False) -> None:
    db = SessionLocal()
    settings = get_settings()
    claimed_attempt: int | None = None
    try:
        claimed = db.execute(
            update(GenerationTask)
            .where(
                GenerationTask.id == task_id,
                GenerationTask.status.in_({"queued", "enqueued", "failed", "retry_wait"}),
            )
            .values(
                status="running", started_at=utc_naive_now(), attempts=GenerationTask.attempts + 1,
                finished_at=None, stop_reason="", error_message="",
            )
        )
        if claimed.rowcount != 1:
            db.rollback()
            return
        db.commit()
        task = db.get(GenerationTask, task_id)
        if task is None:
            return
        claimed_attempt = task.attempts
        db.execute(
            update(OutboxEvent)
            .where(
                OutboxEvent.event_type == "generation.requested",
                OutboxEvent.aggregate_id == task_id,
                OutboxEvent.status == "pending",
            )
            .values(status="processed", processed_at=utc_naive_now())
        )
        subscription = db.get(Subscription, task.subscription_id)
        if subscription is None or (task.kind == "daily" and not subscription.enabled):
            task.status = "cancelled"
            task.stop_reason = "subscription_paused"
            task.finished_at = utc_naive_now()
            db.commit()
            return
        task.provider = "demo" if settings.demo_mode else "openai_compatible"
        db.commit()
        if not subscription.topics and not subscription.keywords:
            task.status = "no_match"
            task.stop_reason = "no_subscription_preferences"
            task.finished_at = utc_naive_now()
            db.commit()
            return
        candidates = select_candidates(
            db,
            subscription,
            demo_mode=settings.demo_mode,
            limit=min(subscription.item_limit, 15),
        )
        preferences = {
            "topics": subscription.topics or [],
            "keywords": subscription.keywords or [],
            "excluded_keywords": subscription.excluded_keywords or [],
            "match_all": subscription.match_all,
            "semantic_expansion": subscription.semantic_expansion,
            "item_limit": subscription.item_limit,
            "detail_level": subscription.detail_level,
        }
        if not candidates:
            result = {
                "provider": task.provider,
                "status": "no_match",
                "title": "今天没有匹配的 AI 新闻",
                "summary": "当前已采集内容没有满足这份订阅的新闻。",
                "items": [],
                "tool_calls": [],
                "stop_reason": "no_matching_candidates",
            }
        else:
            workspace_base = Path(settings.tool_task_root)
            if not workspace_base.is_absolute():
                workspace_base = Path.cwd() / workspace_base
            result = run_agent(
                task_id=task.id,
                articles=candidates,
                subscription=preferences,
                settings=settings,
                workspace_base=workspace_base,
            )
        task = _lock_current_claim(db, task_id, claimed_attempt)
        if task is None:
            return
        task.provider = result["provider"]
        task.tool_calls = result["tool_calls"]
        task.token_usage = _accumulate_token_usage(task.token_usage, result.get("usage", {}))
        task.stop_reason = result["stop_reason"]
        task.status = result["status"]
        task.finished_at = utc_naive_now()
        if result["status"] == "published":
            demo = settings.demo_mode
            briefing = Briefing(
                user_id=task.user_id,
                subscription_id=task.subscription_id,
                task_id=task.id,
                business_date=task.business_date,
                kind=task.kind,
                status="published",
                provider=result["provider"],
                is_demo=demo,
                news_cutoff_at=task.started_at or utc_naive_now(),
                title=result["title"] or "AI 每日简报",
                summary=("【演示素材 · 非实时新闻】" if demo else "【AI 生成内容】")
                + result["summary"],
                items=result["items"],
            )
            db.add(briefing)
            db.flush()
            db.add(
                OutboxEvent(
                    event_type="briefing.published",
                    aggregate_id=briefing.id,
                    payload={"user_id": task.user_id, "briefing_id": briefing.id},
                )
            )
        db.commit()
    except AgentRunError as exc:
        db.rollback()
        task = _lock_current_claim(db, task_id, claimed_attempt)
        if task:
            will_retry = retry_allowed and exc.reason in RETRYABLE_GENERATION_FAILURES
            task.status = "retry_wait" if will_retry else "failed"
            task.stop_reason = exc.reason
            task.error_message = str(exc)[:1000]
            task.token_usage = _accumulate_token_usage(task.token_usage, exc.usage)
            task.finished_at = None if will_retry else utc_naive_now()
            db.commit()
    except Exception as exc:
        db.rollback()
        task = _lock_current_claim(db, task_id, claimed_attempt)
        if task:
            task.status = "retry_wait" if retry_allowed else "failed"
            task.stop_reason = "worker_error"
            task.error_message = f"{type(exc).__name__}: generation could not be completed."[:1000]
            task.finished_at = None if retry_allowed else utc_naive_now()
            db.commit()
        raise
    finally:
        db.close()


def _lock_current_claim(
    db: Session, task_id: str, attempt: int | None
) -> GenerationTask | None:
    if attempt is None:
        db.rollback()
        return None
    task = db.scalar(
        select(GenerationTask)
        .where(GenerationTask.id == task_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if task is None or task.status != "running" or task.attempts != attempt:
        db.rollback()
        return None
    return task
