import pytest
from fastapi import HTTPException


def test_briefing_payload_backfills_content_type_for_legacy_items():
    from app.api.v1 import briefing_payload
    from app.models import Briefing

    briefing = Briefing(
        id="legacy-briefing",
        task_id="task-id",
        business_date="2026-10-19",
        title="Daily briefing",
        summary="Summary",
        items=[
            {"source_id": "openai-news", "title": "A research announcement"},
            {"source_id": "chinanews-domestic", "title": "A domestic news report"},
        ],
    )

    payload = briefing_payload(briefing, None)

    assert [item["content_type"] for item in payload["items"]] == ["article", "news"]


def test_demo_login_and_subscription_versioning(client):
    session = client.post("/api/v1/auth/demo")
    assert session.status_code == 200
    token = session.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    current = client.get("/api/v1/me", headers=headers)
    assert current.status_code == 200
    assert current.json()["is_demo"] is True

    saved = client.put(
        "/api/v1/subscription",
        headers=headers,
        json={
            "topics": ["模型"],
            "keywords": ["推理"],
            "item_limit": 5,
            "send_time": "08:00",
            "version": 1,
        },
    )
    assert saved.status_code == 200
    assert saved.json()["version"] == 2

    conflict = client.put(
        "/api/v1/subscription",
        headers=headers,
        json={"topics": ["开源"], "item_limit": 5, "send_time": "08:00", "version": 1},
    )
    assert conflict.status_code == 409


def test_first_subscription_can_be_created_for_new_user(client):
    from app.db import SessionLocal
    from app.models import User
    from app.security import create_access_token

    with SessionLocal() as db:
        user = User(email="first-subscription@example.com", display_name="新订阅读者")
        db.add(user)
        db.commit()
        token = create_access_token(user.id)

    response = client.put(
        "/api/v1/subscription",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "topics": ["AI agents"],
            "keywords": ["agent workflow"],
            "item_limit": 5,
            "send_time": "08:00",
            "version": 0,
        },
    )

    assert response.status_code == 200
    assert response.json()["version"] == 1
    assert response.json()["topics"] == ["AI agents"]


def test_personal_data_export_contains_owned_records_only(client):
    from app.db import SessionLocal
    from app.models import Briefing, Feedback, GenerationTask, ReadState, Subscription, User
    from app.scheduling import utc_naive_now
    from app.security import create_access_token

    now = utc_naive_now()
    with SessionLocal() as db:
        owner = User(email="export-owner@example.test", display_name="Export Owner")
        other = User(email="export-other@example.test", display_name="Other")
        db.add_all([owner, other])
        db.flush()
        subscription = Subscription(user_id=owner.id, topics=["AI"], keywords=["agents"])
        other_subscription = Subscription(user_id=other.id, topics=["Private"])
        db.add_all([subscription, other_subscription])
        db.flush()
        task = GenerationTask(
            user_id=owner.id,
            subscription_id=subscription.id,
            business_date=now.date().isoformat(),
            kind="daily",
            status="published",
            tool_calls=[{"name": "search_news", "arguments": {"query": "AI agents"}}],
            token_usage={"prompt_tokens": 12, "completion_tokens": 3},
        )
        db.add(task)
        db.flush()
        briefing = Briefing(
            user_id=owner.id,
            subscription_id=subscription.id,
            task_id=task.id,
            business_date=now.date().isoformat(),
            title="Owner briefing",
            summary="Owner summary",
            is_demo=True,
            items=[{"title": "Owner article", "source_name": "Example", "url": "https://example.test"}],
            news_cutoff_at=now,
        )
        db.add(briefing)
        db.flush()
        db.add_all(
            [
                ReadState(user_id=owner.id, briefing_id=briefing.id, is_read=True, is_bookmarked=True),
                Feedback(
                    user_id=owner.id,
                    briefing_id=briefing.id,
                    kind="correction",
                    detail="请复核事件日期",
                ),
            ]
        )
        db.commit()
        token = create_access_token(owner.id)

    assert client.get("/api/v1/me/export").status_code == 401
    response = client.get(
        "/api/v1/me/export", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 200
    export = response.json()
    assert export["account"]["email"] == "export-owner@example.test"
    assert export["subscription"]["topics"] == ["AI"]
    assert len(export["generation_tasks"]) == 1
    assert export["generation_tasks"][0]["token_usage"]["prompt_tokens"] == 12
    assert export["briefings"][0]["items"][0]["source_name"] == "Example"
    assert export["read_states"][0]["is_bookmarked"] is True
    assert export["feedback"][0]["detail"] == "请复核事件日期"
    serialized = response.text
    assert "export-other@example.test" not in serialized
    assert "Private" not in serialized
    assert "登录令牌" in export["excluded_security_data"]


def test_unlicensed_legacy_briefings_are_hidden_from_history_detail_and_export(client):
    from app.db import SessionLocal
    from app.models import Briefing, Feedback, GenerationTask, Subscription, User
    from app.scheduling import utc_naive_now
    from app.security import create_access_token

    with SessionLocal() as db:
        user = User(email="rights-filter@example.test", display_name="Rights Filter")
        db.add(user)
        db.flush()
        subscription = Subscription(user_id=user.id, topics=["news"])
        db.add(subscription)
        db.flush()
        task = GenerationTask(
            user_id=user.id,
            subscription_id=subscription.id,
            business_date="2026-10-19",
            kind="daily",
            status="published",
        )
        db.add(task)
        db.flush()
        briefing = Briefing(
            user_id=user.id,
            subscription_id=subscription.id,
            task_id=task.id,
            business_date="2026-10-19",
            kind="daily",
            status="published",
            provider="legacy",
            is_demo=False,
            news_cutoff_at=utc_naive_now(),
            title="Uncleared old briefing",
            summary="Old summary based on an unreviewed source.",
            items=[{"source_id": "unreviewed-source", "title": "Old article"}],
        )
        db.add(briefing)
        db.flush()
        db.add(Feedback(user_id=user.id, briefing_id=briefing.id, kind="correction"))
        db.commit()
        briefing_id = briefing.id
        token = create_access_token(user.id)

    headers = {"Authorization": f"Bearer {token}"}
    assert client.get("/api/v1/briefings", headers=headers).json()["items"] == []
    assert client.get(f"/api/v1/briefings/{briefing_id}", headers=headers).status_code == 404
    assert client.get("/api/v1/me/feedback", headers=headers).json()["items"] == []
    export = client.get("/api/v1/me/export", headers=headers).json()
    assert export["briefings"] == []


def test_email_reminders_require_a_verified_email(client):
    session = client.post("/api/v1/auth/demo").json()
    headers = {"Authorization": f"Bearer {session['access_token']}"}
    current = client.get("/api/v1/subscription", headers=headers).json()

    response = client.put(
        "/api/v1/subscription",
        headers=headers,
        json={
            "topics": ["AI"],
            "keywords": [],
            "item_limit": 5,
            "send_time": "08:00",
            "notify_enabled": True,
            "version": current["version"],
        },
    )

    assert response.status_code == 403
    assert "Verify this email" in response.json()["detail"]


def test_email_reminders_require_smtp_configuration(client, monkeypatch):
    from app.api import v1
    from app.config import get_settings
    from app.db import SessionLocal
    from app.models import User

    session = client.post("/api/v1/auth/demo").json()
    headers = {"Authorization": f"Bearer {session['access_token']}"}
    with SessionLocal() as db:
        user = db.query(User).filter_by(email="demo@ai-news.test").one()
        user.email_verified = True
        db.commit()

    settings = get_settings().model_copy(
        update={"demo_mode": False, "smtp_host": "", "smtp_from": ""}
    )
    monkeypatch.setattr(v1, "get_settings", lambda: settings)
    current = client.get("/api/v1/subscription", headers=headers).json()
    response = client.put(
        "/api/v1/subscription",
        headers=headers,
        json={
            "topics": ["AI"],
            "keywords": [],
            "item_limit": 5,
            "send_time": "08:00",
            "notify_enabled": True,
            "version": current["version"],
        },
    )

    assert response.status_code == 409
    assert "SMTP is configured" in response.json()["detail"]


def test_successful_email_code_login_marks_address_verified(client, monkeypatch):
    from app.api import v1
    from app.config import get_settings

    delivered_codes = []
    monkeypatch.setattr(v1, "send_login_code", lambda email, code: delivered_codes.append((email, code)))
    monkeypatch.setattr(v1, "enforce_login_code_rate_limits", lambda **_kwargs: None)
    settings = get_settings().model_copy(update={"demo_mode": False})
    monkeypatch.setattr(v1, "get_settings", lambda: settings)

    requested = client.post(
        "/api/v1/auth/send-code", json={"email": "Verified@Example.com"}
    )
    assert requested.status_code == 202
    assert delivered_codes[0][0] == "verified@example.com"

    verified = client.post(
        "/api/v1/auth/verify-code",
        json={
            "email": "verified@example.com",
            "code": delivered_codes[0][1],
            "display_name": "已验证读者",
        },
    )

    assert verified.status_code == 200
    assert verified.json()["user"]["email_verified"] is True


def test_login_code_rate_limit_is_returned_without_sending_mail(client, monkeypatch):
    from app.api import v1
    from app.config import get_settings
    from app.services.rate_limit import LoginCodeRateLimited

    settings = get_settings().model_copy(update={"demo_mode": False})
    monkeypatch.setattr(v1, "get_settings", lambda: settings)

    def limited(**_kwargs):
        raise LoginCodeRateLimited("Wait before retrying.")

    monkeypatch.setattr(
        v1,
        "enforce_login_code_rate_limits",
        limited,
    )
    mail_calls = []
    monkeypatch.setattr(v1, "send_login_code", lambda *_args: mail_calls.append(True))

    response = client.post("/api/v1/auth/send-code", json={"email": "reader@example.com"})

    assert response.status_code == 429
    assert response.json()["detail"] == "Wait before retrying."
    assert mail_calls == []


def test_login_code_rate_limit_ignores_generic_x_real_ip(client, monkeypatch):
    from app.api import v1
    from app.config import get_settings
    from app.services.rate_limit import LoginCodeRateLimited

    settings = get_settings().model_copy(update={"demo_mode": False})
    monkeypatch.setattr(v1, "get_settings", lambda: settings)
    observed = {}

    def limited(**kwargs):
        observed.update(kwargs)
        raise LoginCodeRateLimited("Wait before retrying.")

    monkeypatch.setattr(v1, "enforce_login_code_rate_limits", limited)
    response = client.post(
        "/api/v1/auth/send-code",
        json={"email": "reader@example.com"},
        headers={"X-Real-IP": "198.51.100.77"},
    )

    assert response.status_code == 429
    assert observed["client_ip"] == "testclient"


def test_login_code_rate_limit_uses_valid_cloudflare_client_ip(client, monkeypatch):
    from app.api import v1
    from app.config import get_settings
    from app.services.rate_limit import LoginCodeRateLimited

    settings = get_settings().model_copy(update={"demo_mode": False})
    monkeypatch.setattr(v1, "get_settings", lambda: settings)
    observed = {}

    def limited(**kwargs):
        observed.update(kwargs)
        raise LoginCodeRateLimited("Wait before retrying.")

    monkeypatch.setattr(v1, "enforce_login_code_rate_limits", limited)
    response = client.post(
        "/api/v1/auth/send-code",
        json={"email": "reader@example.com"},
        headers={
            "CF-Connecting-IP": "203.0.113.25",
            "X-Real-IP": "198.51.100.77",
        },
    )

    assert response.status_code == 429
    assert observed["client_ip"] == "203.0.113.25"


def test_login_code_rate_limit_rejects_malformed_cloudflare_ip_header(client, monkeypatch):
    from app.api import v1
    from app.config import get_settings
    from app.services.rate_limit import LoginCodeRateLimited

    settings = get_settings().model_copy(update={"demo_mode": False})
    monkeypatch.setattr(v1, "get_settings", lambda: settings)
    observed = {}

    def limited(**kwargs):
        observed.update(kwargs)
        raise LoginCodeRateLimited("Wait before retrying.")

    monkeypatch.setattr(v1, "enforce_login_code_rate_limits", limited)
    response = client.post(
        "/api/v1/auth/send-code",
        json={"email": "reader@example.com"},
        headers={
            "CF-Connecting-IP": "not-an-ip",
            "X-Real-IP": "198.51.100.77",
        },
    )

    assert response.status_code == 429
    assert observed["client_ip"] == "testclient"


def test_login_code_rate_limit_fails_closed_on_redis_outage(client, monkeypatch):
    from app.api import v1
    from app.config import get_settings
    from app.services.rate_limit import RateLimitUnavailable

    settings = get_settings().model_copy(update={"demo_mode": False})
    monkeypatch.setattr(v1, "get_settings", lambda: settings)

    def unavailable(**_kwargs):
        raise RateLimitUnavailable("unavailable")

    monkeypatch.setattr(
        v1,
        "enforce_login_code_rate_limits",
        unavailable,
    )

    response = client.post("/api/v1/auth/send-code", json={"email": "reader@example.com"})

    assert response.status_code == 503
    assert response.json()["detail"] == "Email verification is temporarily unavailable."


def test_local_login_is_available_with_live_model_mode(client, monkeypatch):
    from app.api import v1
    from app.config import get_settings

    settings = get_settings().model_copy(update={"demo_mode": False})
    monkeypatch.setattr(v1, "get_settings", lambda: settings)

    response = client.post("/api/v1/auth/demo")

    assert response.status_code == 200
    assert response.json()["demo_mode"] is False
    assert response.json()["user"]["is_demo"] is True


def test_local_login_is_disabled_outside_development(client, monkeypatch):
    from app.api import v1
    from app.config import get_settings

    settings = get_settings().model_copy(update={"app_env": "production", "demo_mode": False})
    monkeypatch.setattr(v1, "get_settings", lambda: settings)

    response = client.post("/api/v1/auth/demo")

    assert response.status_code == 404


def test_demo_mode_does_not_claim_live_provider_delivery(client):
    health = client.get("/api/v1/health").json()
    assert health["mode"] == "demo"
    assert health["task_backend"] == "local-background"
    assert "push_configured" not in health
    assert health["local_login_enabled"] is True
    assert health["email_configured"] is False
    assert client.put("/api/v1/devices", json={}).status_code == 404


def test_readiness_checks_database(client):
    response = client.get("/api/v1/health/ready")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "database": "ready",
        "task_queue": "disabled",
    }


def test_readiness_fails_when_core_database_tables_are_missing():
    from app.api.v1 import readiness

    class MissingSchemaSession:
        def execute(self, _statement):
            raise RuntimeError("relation does not exist")

    with pytest.raises(HTTPException) as error:
        readiness(db=MissingSchemaSession())

    assert error.value.status_code == 503


def test_anonymous_user_cannot_access_briefings(client):
    assert client.get("/api/v1/briefings").status_code == 401


def test_visitors_can_read_only_clearly_labeled_public_demo_stories(client):
    response = client.get("/api/v1/public/demo-stories")

    assert response.status_code == 200
    payload = response.json()
    assert "虚构演示素材" in payload["disclosure"]
    assert payload["items"]
    assert len(payload["items"]) <= 3
    assert all(item["is_demo"] is True for item in payload["items"])
    assert all("演示素材" in item["title"] for item in payload["items"])
    assert all("source_url" not in item for item in payload["items"])


def test_account_deletion_removes_owned_data_and_related_outbox_records(
    client, db, tmp_path, monkeypatch
):
    from app.api import v1
    from app.models import (
        Briefing,
        DeliveryAttempt,
        EmailOutbox,
        Feedback,
        GenerationTask,
        OutboxEvent,
        ReadState,
        Subscription,
        User,
        VerificationCode,
    )
    from app.scheduling import utc_naive_now
    from app.security import create_access_token

    now = utc_naive_now()
    user = User(email="delete-me@example.test", display_name="Delete Me", email_verified=True)
    db.add(user)
    db.flush()
    subscription = Subscription(user_id=user.id, topics=["AI"], keywords=["agents"])
    db.add(subscription)
    db.flush()
    task = GenerationTask(
        user_id=user.id,
        subscription_id=subscription.id,
        business_date=now.date().isoformat(),
        kind="daily",
        status="published",
    )
    db.add(task)
    db.flush()
    briefing = Briefing(
        user_id=user.id,
        subscription_id=subscription.id,
        task_id=task.id,
        business_date=now.date().isoformat(),
        kind="daily",
        status="published",
        provider="test",
        news_cutoff_at=now,
        title="AI briefing",
        summary="Summary",
    )
    verification = VerificationCode(
        email=user.email.upper(),
        purpose="login",
        code_hash="test-hash",
        expires_at=now,
        sent_at=now,
    )
    db.add_all([briefing, verification])
    db.flush()
    db.add_all(
        [
            ReadState(user_id=user.id, briefing_id=briefing.id),
            Feedback(user_id=user.id, briefing_id=briefing.id, kind="other"),
            DeliveryAttempt(
                task_id=task.id,
                user_id=user.id,
                channel="email",
                status="pending",
                dedupe_key="email:test-briefing:delete-me@example.test",
            ),
            OutboxEvent(
                event_type="generation.requested",
                aggregate_id=task.id,
                payload={"task_id": task.id},
            ),
            OutboxEvent(
                event_type="briefing.published",
                aggregate_id=briefing.id,
                payload={"briefing_id": briefing.id},
            ),
            EmailOutbox(email=user.email, verification_code_id=verification.id),
        ]
    )
    db.commit()
    workspace = tmp_path / task.id
    (workspace / "outputs").mkdir(parents=True)
    (workspace / "outputs" / "draft.json").write_text("{}", encoding="utf-8")
    settings = v1.get_settings().model_copy(update={"tool_task_root": str(tmp_path)})
    monkeypatch.setattr(v1, "get_settings", lambda: settings)

    headers = {"Authorization": f"Bearer {create_access_token(user.id)}"}
    response = client.delete("/api/v1/me", headers=headers)

    assert response.status_code == 204
    assert not workspace.exists()
    assert client.get("/api/v1/me", headers=headers).status_code == 401
    db.expire_all()
    for model in (
        User,
        Subscription,
        GenerationTask,
        Briefing,
        ReadState,
        Feedback,
        DeliveryAttempt,
        OutboxEvent,
        VerificationCode,
        EmailOutbox,
    ):
        assert db.query(model).count() == 0, model.__tablename__


def test_user_cannot_read_or_update_another_users_briefing_or_task(client):
    from app.db import SessionLocal
    from app.models import Briefing, GenerationTask, Subscription, User
    from app.scheduling import utc_naive_now
    from app.security import create_access_token

    with SessionLocal() as db:
        owner = User(email="briefing-owner@example.test", display_name="Owner")
        other = User(email="briefing-other@example.test", display_name="Other")
        db.add_all([owner, other])
        db.flush()
        subscription = Subscription(user_id=owner.id, topics=["AI"])
        db.add(subscription)
        db.flush()
        task = GenerationTask(
            user_id=owner.id,
            subscription_id=subscription.id,
            business_date="2026-10-14",
            kind="daily",
            status="published",
        )
        db.add(task)
        db.flush()
        briefing = Briefing(
            user_id=owner.id,
            subscription_id=subscription.id,
            task_id=task.id,
            business_date="2026-10-14",
            kind="daily",
            status="published",
            provider="live",
            is_demo=False,
            news_cutoff_at=utc_naive_now(),
            title="Private briefing",
            summary="Owner-only content.",
            items=[],
        )
        db.add(briefing)
        db.commit()
        briefing_id = briefing.id
        task_id = task.id
        other_token = create_access_token(other.id)

    headers = {"Authorization": f"Bearer {other_token}"}

    assert client.get(f"/api/v1/briefings/{briefing_id}", headers=headers).status_code == 404
    assert client.get(f"/api/v1/briefing-tasks/{task_id}", headers=headers).status_code == 404
    assert (
        client.put(
            f"/api/v1/briefings/{briefing_id}/state",
            headers=headers,
            json={"is_bookmarked": True},
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"/api/v1/briefings/{briefing_id}/feedback",
            headers=headers,
            json={"kind": "irrelevant", "detail": "Should not be stored."},
        ).status_code
        == 404
    )
    assert client.get("/api/v1/briefings", headers=headers).json()["items"] == []
    assert client.get("/api/v1/agent/tool-traces", headers=headers).json()["items"] == []


def test_demo_preview_runs_agent_and_publishes_labeled_briefing(client, monkeypatch):
    from app.api import v1
    from app.config import get_settings
    from app.db import SessionLocal
    from app.models import Feedback, User
    from app.security import create_access_token
    from app.seed_demo import seed_demo

    session = client.post("/api/v1/auth/demo").json()
    seed_demo()
    headers = {"Authorization": f"Bearer {session['access_token']}"}
    current = client.get("/api/v1/subscription", headers=headers).json()
    update = client.put(
        "/api/v1/subscription",
        headers=headers,
        json={
            "topics": ["AI"],
            "keywords": [],
            "item_limit": 5,
            "send_time": "08:00",
            "version": current["version"],
        },
    )
    assert update.status_code == 200

    task = client.post("/api/v1/briefing-tasks", headers=headers, json={"kind": "preview"})
    assert task.status_code == 202
    task_id = task.json()["id"]
    final_task = client.get(f"/api/v1/briefing-tasks/{task_id}", headers=headers).json()
    assert final_task["status"] == "published"
    assert final_task["tools"][0]["tool"] == "search_news"
    assert final_task["token_usage"]["prompt_tokens"] == 0
    assert final_task["token_usage"]["completion_tokens"] == 0
    assert [request["phase"] for request in final_task["token_usage"]["requests"]] == [
        "tool_selection",
        "final_response",
    ]

    briefing = client.get("/api/v1/briefings", headers=headers).json()["items"][0]
    assert briefing["is_demo"] is True
    assert "演示素材" in briefing["summary"]
    assert briefing["items"][0]["source_url"].startswith("https://example.com/")

    feedback = client.post(
        f"/api/v1/briefings/{briefing['id']}/feedback",
        headers=headers,
        json={
            "item_index": 0,
            "kind": "incorrect",
            "detail": "测试更正：请核实来源中的模型名称。",
        },
    )
    assert feedback.status_code == 201
    assert feedback.json()["accepted"] is True

    mine = client.get("/api/v1/me/feedback", headers=headers)
    assert mine.status_code == 200
    assert mine.json()["items"][0]["status"] == "open"
    assert mine.json()["items"][0]["reply"] == ""

    assert client.get("/api/v1/admin/feedback", headers=headers).status_code == 403
    settings = get_settings().model_copy(update={"admin_emails": "demo@ai-news.test"})
    monkeypatch.setattr(v1, "get_settings", lambda: settings)
    assert client.get("/api/v1/me", headers=headers).json()["is_admin"] is True
    queue = client.get("/api/v1/admin/feedback?status=open", headers=headers)
    assert queue.status_code == 200
    assert queue.json()["items"][0]["briefing_title"] == briefing["title"]
    with SessionLocal() as db:
        other_user = User(email="feedback-reader@example.test")
        db.add(other_user)
        db.commit()
        other_headers = {"Authorization": f"Bearer {create_access_token(other_user.id)}"}
    assert client.get("/api/v1/me/feedback", headers=other_headers).json()["items"] == []
    assert client.get("/api/v1/admin/feedback", headers=other_headers).status_code == 403
    feedback_id = feedback.json()["feedback_id"]
    missing_reply = client.patch(
        f"/api/v1/admin/feedback/{feedback_id}",
        headers=headers,
        json={"status": "resolved", "reply": ""},
    )
    assert missing_reply.status_code == 422
    resolved = client.patch(
        f"/api/v1/admin/feedback/{feedback_id}",
        headers=headers,
        json={"status": "resolved", "reply": "已核实并完成更正。"},
    )
    assert resolved.status_code == 200
    assert resolved.json()["status"] == "resolved"
    assert client.get("/api/v1/me/feedback", headers=headers).json()["items"][0]["reply"] == "已核实并完成更正。"
    with SessionLocal() as db:
        saved_feedback = db.get(Feedback, feedback_id)
        assert saved_feedback.handled_by_user_id is not None

    state = client.put(
        f"/api/v1/briefings/{briefing['id']}/state",
        headers=headers,
        json={"is_read": True, "is_bookmarked": True},
    )
    assert state.status_code == 200
    listed = client.get("/api/v1/briefings", headers=headers).json()["items"][0]
    assert listed["is_read"] is True
    assert listed["is_bookmarked"] is True
