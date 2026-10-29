from __future__ import annotations

from datetime import timedelta

from celery import shared_task
from sqlalchemy import and_, or_, select, update

from app.config import get_settings
from app.db import SessionLocal
from app.models import (
    Briefing,
    DeliveryAttempt,
    GenerationTask,
    OutboxEvent,
    Subscription,
    User,
)
from app.scheduling import utc_naive_now
from app.services.generation import dispatch_due_subscriptions, dispatch_task
from app.services.mailer import send_briefing_ready
from app.worker.collector import collect_all


def run_scheduled_cycle() -> dict[str, int]:
    created = dispatch_due_subscriptions()
    with SessionLocal() as db:
        task_ids = list(
            db.scalars(
                select(GenerationTask.id)
                .where(GenerationTask.kind == "daily", GenerationTask.status == "queued")
                .order_by(GenerationTask.created_at)
                .limit(100)
            ).all()
        )
    for task_id in task_ids:
        dispatch_task(task_id)
    dispatch_outbox()
    return {"created": created, "processed": len(task_ids)}


@shared_task(name="daily_ai_news.generate_briefing", bind=True, max_retries=2)
def generate_briefing(self, task_id: str) -> dict:
    retry_allowed = self.request.retries < self.max_retries
    try:
        dispatch_task(task_id, retry_allowed=retry_allowed)
        with SessionLocal() as db:
            task = db.get(GenerationTask, task_id)
            waiting_retry = task is not None and task.status == "retry_wait"
    except Exception as exc:
        if not retry_allowed:
            raise
        raise self.retry(exc=exc, countdown=min(60 * (2**self.request.retries), 900)) from exc
    if waiting_retry and retry_allowed:
        countdown = min(30 * (2**self.request.retries), 300)
        raise self.retry(countdown=countdown)
    return {"task_id": task_id, "status": "finished"}


@shared_task(name="daily_ai_news.dispatch_due")
def dispatch_due() -> dict:
    created = dispatch_due_subscriptions()
    settings = get_settings()
    with SessionLocal() as db:
        task_ids = list(
            db.scalars(
                select(GenerationTask.id)
                .where(GenerationTask.kind == "daily", GenerationTask.status == "queued")
                .order_by(GenerationTask.created_at)
                .limit(settings.daily_dispatch_batch_size)
            ).all()
        )
        if task_ids:
            task_ids = list(
                db.scalars(
                    update(GenerationTask)
                    .where(GenerationTask.id.in_(task_ids), GenerationTask.status == "queued")
                    .values(status="enqueued")
                    .returning(GenerationTask.id)
                ).all()
            )
            db.commit()
    sent = 0
    for task_id in task_ids:
        generate_briefing.delay(task_id)
        sent += 1
    return {"created": created, "enqueued": sent}


@shared_task(name="daily_ai_news.collect_feeds")
def collect_feeds() -> dict:
    return collect_all()


@shared_task(name="daily_ai_news.recover_stalled")
def recover_stalled() -> dict:
    settings = get_settings()
    max_agent_seconds = settings.llm_timeout_seconds * (settings.llm_max_turns + 2) + 300
    cutoff = utc_naive_now() - timedelta(seconds=max_agent_seconds)
    with SessionLocal() as db:
        ids = list(
            db.scalars(
                update(GenerationTask)
                .where(
                    or_(
                        and_(
                            GenerationTask.status == "running",
                            GenerationTask.started_at < cutoff,
                        ),
                        and_(
                            GenerationTask.status == "enqueued",
                            GenerationTask.created_at < cutoff,
                        ),
                        and_(
                            GenerationTask.status == "retry_wait",
                            GenerationTask.started_at < cutoff,
                        ),
                    )
                )
                .values(status="queued")
                .returning(GenerationTask.id)
            ).all()
        )
        stale_email_events = db.scalars(
            select(OutboxEvent).where(
                OutboxEvent.event_type == "briefing.published",
                OutboxEvent.status == "processing",
                OutboxEvent.created_at < cutoff,
            )
        ).all()
        for event in stale_email_events:
            briefing = db.get(Briefing, event.aggregate_id)
            if briefing:
                attempts = db.scalars(
                    select(DeliveryAttempt).where(
                        DeliveryAttempt.task_id == briefing.task_id,
                        DeliveryAttempt.channel == "email",
                        DeliveryAttempt.status == "sending",
                    )
                ).all()
                for attempt in attempts:
                    attempt.status = "unknown"
                    attempt.last_error = "Worker stopped before provider acceptance was recorded."
            event.status = "processed"
            event.processed_at = utc_naive_now()
        db.commit()
    for task_id in ids:
        generate_briefing.delay(task_id)
    return {"requeued": len(ids)}


@shared_task(name="daily_ai_news.dispatch_outbox")
def dispatch_outbox() -> dict:
    settings = get_settings()
    if not settings.smtp_host or not settings.smtp_from:
        return {"status": "provider_unconfigured", "accepted": 0, "unknown": 0}
    claimed_ids: list[str] = []
    with SessionLocal() as db:
        pending_ids = list(
            db.scalars(
                select(OutboxEvent.id)
                .where(
                    OutboxEvent.event_type == "briefing.published",
                    OutboxEvent.status == "pending",
                )
                .order_by(OutboxEvent.created_at)
                .limit(100)
            ).all()
        )
        for event_id in pending_ids:
            result = db.execute(
                update(OutboxEvent)
                .where(OutboxEvent.id == event_id, OutboxEvent.status == "pending")
                .values(status="processing")
            )
            if result.rowcount == 1:
                claimed_ids.append(event_id)
        db.commit()
    accepted = unknown = 0
    for event_id in claimed_ids:
        outcome = _dispatch_briefing_event(event_id, settings)
        accepted += outcome == "accepted"
        unknown += outcome == "unknown"
    return {"status": "processed", "accepted": accepted, "unknown": unknown}


def _dispatch_briefing_event(event_id: str, settings) -> str:
    with SessionLocal() as db:
        event = db.get(OutboxEvent, event_id)
        if event is None or event.status != "processing":
            return "suppressed"
        briefing = db.get(Briefing, event.aggregate_id)
        subscription = db.get(Subscription, briefing.subscription_id) if briefing else None
        user = db.get(User, briefing.user_id) if briefing else None
        if (
            briefing is None
            or briefing.kind != "daily"
            or subscription is None
            or user is None
            or not subscription.notify_enabled
            or not user.email_verified
            or utc_naive_now() - briefing.created_at > timedelta(hours=24)
        ):
            event.status = "processed"
            event.processed_at = utc_naive_now()
            db.commit()
            return "suppressed"

        dedupe_key = f"email:{briefing.id}:{user.email.casefold()}"
        attempt = db.scalar(select(DeliveryAttempt).where(DeliveryAttempt.dedupe_key == dedupe_key))
        if attempt is not None:
            # A prior SMTP attempt may have reached the provider before its worker stopped.
            # Never resend an ambiguous message automatically.
            if attempt.status == "sending":
                attempt.status = "unknown"
                attempt.last_error = "Previous worker stopped before provider acceptance was recorded."
                db.commit()
            event.status = "processed"
            event.processed_at = utc_naive_now()
            db.commit()
            return "unknown" if attempt.status == "unknown" else "suppressed"

        attempt = DeliveryAttempt(
            task_id=briefing.task_id,
            user_id=user.id,
            channel="email",
            status="sending",
            dedupe_key=dedupe_key,
        )
        db.add(attempt)
        db.commit()
        attempt_id = attempt.id
        email = user.email
        title = briefing.title
        web_url = settings.base_url

    try:
        send_briefing_ready(email, title, web_url)
    except Exception as exc:
        with SessionLocal() as db:
            attempt = db.get(DeliveryAttempt, attempt_id)
            event = db.get(OutboxEvent, event_id)
            if attempt:
                attempt.status = "unknown"
                attempt.last_error = type(exc).__name__[:200]
            if event:
                event.status = "processed"
                event.processed_at = utc_naive_now()
            db.commit()
        return "unknown"

    with SessionLocal() as db:
        attempt = db.get(DeliveryAttempt, attempt_id)
        event = db.get(OutboxEvent, event_id)
        if attempt:
            attempt.status = "accepted"
        if event:
            event.status = "processed"
            event.processed_at = utc_naive_now()
        db.commit()
    return "accepted"
