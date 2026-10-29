from datetime import timedelta

from app.agent.runner import AgentRunError
from app.db import SessionLocal
from app.models import Briefing, GenerationTask, Subscription, User
from app.scheduling import utc_naive_now
from app.services import generation
from app.worker import tasks
from app.worker.tasks import run_scheduled_cycle
from sqlalchemy import select


def test_due_daily_task_is_processed_by_the_in_process_scheduler():
    with SessionLocal() as db:
        user = User(email="schedule@example.test", display_name="Schedule test")
        db.add(user)
        db.flush()
        subscription = Subscription(
            user_id=user.id,
            topics=["AI"],
            keywords=[],
            enabled=True,
            send_time="08:00",
            next_run_at=utc_naive_now() - timedelta(minutes=1),
        )
        db.add(subscription)
        db.commit()

    result = run_scheduled_cycle()

    assert result == {"created": 1, "processed": 1}
    with SessionLocal() as db:
        task = db.scalar(select(GenerationTask).where(GenerationTask.kind == "daily"))
        assert task is not None
        assert task.status in {"published", "no_match"}


def test_no_candidate_task_finishes_as_no_match_without_calling_the_agent(monkeypatch):
    from app.config import Settings

    task_id = _queued_task()
    settings = Settings(
        demo_mode=False,
        auth_secret="test-secret-not-for-production-0123456789",
        llm_api_key="test-key",
        llm_model="test-model",
        llm_base_url="https://llm.example.test/v1",
    )
    agent_calls = []
    monkeypatch.setattr(generation, "get_settings", lambda: settings)
    monkeypatch.setattr(generation, "select_candidates", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        generation,
        "run_agent",
        lambda **_kwargs: agent_calls.append("called"),
    )

    generation.dispatch_task(task_id)

    with SessionLocal() as db:
        task = db.get(GenerationTask, task_id)
        briefings = db.scalars(select(Briefing).where(Briefing.task_id == task_id)).all()
        assert task is not None
        assert task.status == "no_match"
        assert task.stop_reason == "no_matching_candidates"
        assert task.provider == "openai_compatible"
        assert task.tool_calls == []
        assert briefings == []
    assert agent_calls == []


def _queued_task() -> str:
    with SessionLocal() as db:
        user = User(email="lease@example.test", display_name="Lease test")
        db.add(user)
        db.flush()
        subscription = Subscription(user_id=user.id, topics=["AI"], enabled=True)
        db.add(subscription)
        db.flush()
        task = GenerationTask(
            user_id=user.id,
            subscription_id=subscription.id,
            business_date="2026-10-14",
            kind="preview-fence-test",
            status="queued",
        )
        db.add(task)
        db.commit()
        return task.id


def _published_result() -> dict:
    return {
        "provider": "demo",
        "status": "published",
        "tool_calls": [],
        "usage": {},
        "stop_reason": "completed",
        "title": "Test briefing",
        "summary": "A supported test summary.",
        "items": [],
    }


def test_stale_worker_result_is_discarded_after_task_was_requeued(monkeypatch):
    task_id = _queued_task()
    monkeypatch.setattr(generation, "select_candidates", lambda *_args, **_kwargs: [object()])

    def stale_result(**_kwargs):
        with SessionLocal() as db:
            task = db.get(GenerationTask, task_id)
            task.status = "queued"
            db.commit()
        return _published_result()

    monkeypatch.setattr(generation, "run_agent", stale_result)

    generation.dispatch_task(task_id)

    with SessionLocal() as db:
        task = db.get(GenerationTask, task_id)
        assert task.status == "queued"
        assert task.attempts == 1
        assert db.scalar(select(Briefing).where(Briefing.task_id == task_id)) is None


def test_stale_worker_exception_cannot_fail_newer_attempt(monkeypatch):
    task_id = _queued_task()
    monkeypatch.setattr(generation, "select_candidates", lambda *_args, **_kwargs: [object()])

    def stale_error(**_kwargs):
        with SessionLocal() as db:
            task = db.get(GenerationTask, task_id)
            task.attempts += 1
            db.commit()
        raise AgentRunError("stale attempt failed", reason="model_error")

    monkeypatch.setattr(generation, "run_agent", stale_error)

    generation.dispatch_task(task_id)

    with SessionLocal() as db:
        task = db.get(GenerationTask, task_id)
        assert task.status == "running"
        assert task.attempts == 2
        assert task.stop_reason == ""


def test_failed_agent_run_persists_model_token_usage(monkeypatch):
    task_id = _queued_task()
    monkeypatch.setattr(generation, "select_candidates", lambda *_args, **_kwargs: [object()])

    def failed_run(**_kwargs):
        raise AgentRunError(
            "The model returned invalid JSON.",
            reason="invalid_format_correction",
            usage={
                "prompt_tokens": 1200,
                "completion_tokens": 87,
                "requests": [
                    {"request": 1, "phase": "final_response", "prompt_tokens": 1200, "completion_tokens": 87}
                ],
            },
            format_repair_attempted=True,
        )

    monkeypatch.setattr(generation, "run_agent", failed_run)

    generation.dispatch_task(task_id)

    with SessionLocal() as db:
        task = db.get(GenerationTask, task_id)
        assert task.status == "failed"
        assert task.stop_reason == "invalid_format_correction"
        assert task.token_usage == {
            "prompt_tokens": 1200,
            "completion_tokens": 87,
            "requests": [
                {"request": 1, "phase": "final_response", "prompt_tokens": 1200, "completion_tokens": 87}
            ],
        }

    monkeypatch.setattr(generation, "run_agent", lambda **_kwargs: {
        **_published_result(),
        "usage": {
            "prompt_tokens": 300,
            "completion_tokens": 22,
            "requests": [
                {"request": 1, "phase": "tool_selection", "prompt_tokens": 300, "completion_tokens": 22}
            ],
        },
    })
    generation.dispatch_task(task_id)

    with SessionLocal() as db:
        task = db.get(GenerationTask, task_id)
        assert task.status == "published"
        assert task.token_usage == {
            "prompt_tokens": 1500,
            "completion_tokens": 109,
            "requests": [
                {"request": 1, "phase": "final_response", "prompt_tokens": 1200, "completion_tokens": 87},
                {"request": 2, "phase": "tool_selection", "prompt_tokens": 300, "completion_tokens": 22},
            ],
        }


def test_task_retries_only_transient_provider_failures_with_bounded_backoff(monkeypatch):
    task_id = _queued_task()
    retry_calls = []

    class RetryScheduled(Exception):
        pass

    def fail_dispatch(current_task_id, *, retry_allowed):
        assert retry_allowed is True
        with SessionLocal() as db:
            task = db.get(GenerationTask, current_task_id)
            task.status = "retry_wait"
            task.stop_reason = "model_rate_limited"
            db.commit()

    def schedule_retry(**kwargs):
        retry_calls.append(kwargs)
        raise RetryScheduled

    monkeypatch.setattr(tasks, "dispatch_task", fail_dispatch)
    monkeypatch.setattr(tasks.generate_briefing, "retry", schedule_retry)

    try:
        tasks.generate_briefing.run(task_id)
    except RetryScheduled:
        pass
    else:
        raise AssertionError("A rate-limited generation must be retried.")

    assert retry_calls == [{"countdown": 30}]


def test_task_does_not_retry_permanent_provider_failures(monkeypatch):
    task_id = _queued_task()
    retry_calls = []

    def fail_dispatch(current_task_id, *, retry_allowed):
        with SessionLocal() as db:
            task = db.get(GenerationTask, current_task_id)
            task.status = "failed"
            task.stop_reason = "model_auth_error"
            db.commit()

    monkeypatch.setattr(tasks, "dispatch_task", fail_dispatch)
    monkeypatch.setattr(tasks.generate_briefing, "retry", lambda **kwargs: retry_calls.append(kwargs))

    result = tasks.generate_briefing.run(task_id)

    assert result == {"task_id": task_id, "status": "finished"}
    assert retry_calls == []


def test_transient_failure_waits_for_retry_then_publishes_without_stale_error(monkeypatch):
    task_id = _queued_task()
    monkeypatch.setattr(generation, "select_candidates", lambda *_args, **_kwargs: [object()])

    def limited(**_kwargs):
        raise AgentRunError(
            "Rate limited", reason="model_rate_limited",
            usage={"prompt_tokens": 100, "completion_tokens": 5},
        )

    monkeypatch.setattr(generation, "run_agent", limited)
    generation.dispatch_task(task_id, retry_allowed=True)
    with SessionLocal() as db:
        task = db.get(GenerationTask, task_id)
        assert task.status == "retry_wait"
        assert task.finished_at is None
        assert task.stop_reason == "model_rate_limited"

    monkeypatch.setattr(generation, "run_agent", lambda **_kwargs: _published_result())
    generation.dispatch_task(task_id, retry_allowed=False)
    with SessionLocal() as db:
        task = db.get(GenerationTask, task_id)
        assert task.status == "published"
        assert task.attempts == 2
        assert task.error_message == ""
        assert task.token_usage == {"prompt_tokens": 100, "completion_tokens": 5}


def test_exhausted_transient_failure_is_terminal_and_not_retried(monkeypatch):
    task_id = _queued_task()
    monkeypatch.setattr(generation, "select_candidates", lambda *_args, **_kwargs: [object()])

    def limited(**_kwargs):
        raise AgentRunError("Rate limited", reason="model_rate_limited")

    monkeypatch.setattr(generation, "run_agent", limited)
    monkeypatch.setattr(tasks.generate_briefing.request, "retries", 2)
    retries = []
    monkeypatch.setattr(tasks.generate_briefing, "retry", lambda **kwargs: retries.append(kwargs))
    tasks.generate_briefing.run(task_id)
    with SessionLocal() as db:
        task = db.get(GenerationTask, task_id)
        assert task.status == "failed"
        assert task.finished_at is not None
    assert retries == []


def test_recovery_requeues_a_lost_retry_message(monkeypatch):
    task_id = _queued_task()
    with SessionLocal() as db:
        task = db.get(GenerationTask, task_id)
        task.status = "retry_wait"
        task.started_at = utc_naive_now() - timedelta(hours=2)
        db.commit()
    requeued = []
    monkeypatch.setattr(tasks.generate_briefing, "delay", lambda value: requeued.append(value))
    result = tasks.recover_stalled()
    assert result["requeued"] == 1
    assert requeued == [task_id]


def test_recovery_does_not_restart_a_task_within_model_timeout_budget(monkeypatch):
    task_id = _queued_task()
    with SessionLocal() as db:
        task = db.get(GenerationTask, task_id)
        task.status = "running"
        task.started_at = utc_naive_now() - timedelta(minutes=20)
        db.commit()
    queued = []
    monkeypatch.setattr(tasks.generate_briefing, "delay", lambda value: queued.append(value))

    result = tasks.recover_stalled()

    assert result == {"requeued": 0}
    assert queued == []
    with SessionLocal() as db:
        assert db.get(GenerationTask, task_id).status == "running"


def test_dispatch_due_enqueues_only_tasks_it_atomically_claimed(monkeypatch):
    task_id = _queued_task()
    with SessionLocal() as db:
        db.get(GenerationTask, task_id).kind = "daily"
        db.commit()
    enqueued = []
    monkeypatch.setattr(tasks.generate_briefing, "delay", lambda value: enqueued.append(value))

    first = tasks.dispatch_due()
    second = tasks.dispatch_due()

    assert first == {"created": 0, "enqueued": 1}
    assert second == {"created": 0, "enqueued": 0}
    assert enqueued == [task_id]
    with SessionLocal() as db:
        assert db.get(GenerationTask, task_id).status == "enqueued"


def test_lost_enqueued_message_recovers_and_duplicate_delivery_publishes_once(monkeypatch):
    task_id = _queued_task()
    old = utc_naive_now() - timedelta(hours=1)
    with SessionLocal() as db:
        task = db.get(GenerationTask, task_id)
        task.kind = "daily"
        task.status = "enqueued"
        task.created_at = old
        db.commit()

    requeued = []
    monkeypatch.setattr(tasks.generate_briefing, "delay", lambda value: requeued.append(value))
    recovered = tasks.recover_stalled()

    assert recovered == {"requeued": 1}
    assert requeued == [task_id]

    monkeypatch.setattr(generation, "select_candidates", lambda *_args, **_kwargs: [object()])
    monkeypatch.setattr(generation, "run_agent", lambda **_kwargs: _published_result())
    generation.dispatch_task(task_id)
    generation.dispatch_task(task_id)

    with SessionLocal() as db:
        task = db.get(GenerationTask, task_id)
        briefings = db.scalars(select(Briefing).where(Briefing.task_id == task_id)).all()
        assert task.status == "published"
        assert len(briefings) == 1
