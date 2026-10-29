from app.config import get_settings
from app.db import SessionLocal
from app.models import Briefing, DeliveryAttempt, GenerationTask, OutboxEvent, Subscription, User
from app.scheduling import utc_naive_now
from app.services import mailer
from app.worker import tasks
from sqlalchemy import select


def _published_daily_briefing() -> tuple[str, str]:
    with SessionLocal() as db:
        user = User(email="reader@example.test", email_verified=True)
        db.add(user)
        db.flush()
        subscription = Subscription(
            user_id=user.id, topics=["AI"], enabled=True, notify_enabled=True
        )
        db.add(subscription)
        db.flush()
        task = GenerationTask(
            user_id=user.id,
            subscription_id=subscription.id,
            business_date="2026-10-14",
            kind="daily",
            status="published",
        )
        db.add(task)
        db.flush()
        briefing = Briefing(
            user_id=user.id,
            subscription_id=subscription.id,
            task_id=task.id,
            business_date="2026-10-14",
            kind="daily",
            provider="test",
            news_cutoff_at=utc_naive_now(),
            title="每日 AI 新闻",
        )
        db.add(briefing)
        db.flush()
        event = OutboxEvent(
            event_type="briefing.published",
            aggregate_id=briefing.id,
            payload={"briefing_id": briefing.id, "task_id": task.id},
        )
        db.add(event)
        db.commit()
        return briefing.id, event.id


def test_daily_briefing_email_is_opt_in_and_idempotent(monkeypatch):
    settings = get_settings().model_copy(
        update={
            "demo_mode": False,
            "smtp_host": "smtp.example.test",
            "smtp_from": "briefings@example.test",
            "base_url": "https://news.example.test",
        }
    )
    monkeypatch.setattr(tasks, "get_settings", lambda: settings)
    deliveries = []

    def fake_send(email, title, web_url):
        deliveries.append((email, title, web_url))

    monkeypatch.setattr(tasks, "send_briefing_ready", fake_send)
    briefing_id, _ = _published_daily_briefing()

    first = tasks.dispatch_outbox()
    with SessionLocal() as db:
        db.add(
            OutboxEvent(
                event_type="briefing.published",
                aggregate_id=briefing_id,
                payload={"briefing_id": briefing_id},
            )
        )
        db.commit()
    duplicate = tasks.dispatch_outbox()

    assert first == {"status": "processed", "accepted": 1, "unknown": 0}
    assert duplicate == {"status": "processed", "accepted": 0, "unknown": 0}
    assert deliveries == [
        ("reader@example.test", "每日 AI 新闻", "https://news.example.test")
    ]
    with SessionLocal() as db:
        attempt = db.scalar(select(DeliveryAttempt))
        assert attempt is not None
        assert attempt.channel == "email"
        assert attempt.status == "accepted"


def test_ambiguous_smtp_failure_is_not_retried(monkeypatch):
    settings = get_settings().model_copy(
        update={"demo_mode": False, "smtp_host": "smtp.example.test", "smtp_from": "briefings@example.test"}
    )
    monkeypatch.setattr(tasks, "get_settings", lambda: settings)
    sends = 0

    def fail_send(*_args):
        nonlocal sends
        sends += 1
        raise TimeoutError("provider acceptance could not be confirmed")

    monkeypatch.setattr(tasks, "send_briefing_ready", fail_send)
    briefing_id, _ = _published_daily_briefing()

    first = tasks.dispatch_outbox()
    with SessionLocal() as db:
        db.add(
            OutboxEvent(
                event_type="briefing.published",
                aggregate_id=briefing_id,
                payload={"briefing_id": briefing_id},
            )
        )
        db.commit()
    duplicate = tasks.dispatch_outbox()

    assert first == {"status": "processed", "accepted": 0, "unknown": 1}
    assert duplicate == {"status": "processed", "accepted": 0, "unknown": 1}
    assert sends == 1
    with SessionLocal() as db:
        attempt = db.scalar(select(DeliveryAttempt))
        assert attempt is not None
        assert attempt.status == "unknown"
        assert attempt.last_error == "TimeoutError"


def test_briefing_email_contains_only_a_site_link(monkeypatch):
    settings = get_settings().model_copy(
        update={
            "smtp_host": "smtp.example.test",
            "smtp_from": "briefings@example.test",
            "smtp_user": "mailer",
            "smtp_password": "test-password",
            "smtp_port": 587,
            "smtp_starttls": False,
            "base_url": "https://news.example.test",
        }
    )
    monkeypatch.setattr(mailer, "get_settings", lambda: settings)
    sent_messages = []

    class FakeSMTP:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def starttls(self, **_kwargs):
            pass

        def login(self, *_args):
            pass

        def send_message(self, message):
            sent_messages.append(message)

    monkeypatch.setattr(mailer.smtplib, "SMTP", lambda *_args, **_kwargs: FakeSMTP())

    receipt = mailer.send_briefing_ready(
        "reader@example.test", "今日 AI 简报\r\nBcc: attacker@example.test", settings.base_url
    )

    assert receipt is None
    assert len(sent_messages) == 1
    message = sent_messages[0]
    assert message["To"] == "reader@example.test"
    assert message.get("Bcc") is None
    assert "https://news.example.test" in message.get_content()
    assert "订阅关键词" in message.get_content()
