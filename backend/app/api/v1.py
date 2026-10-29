from __future__ import annotations

import json
import logging
import secrets
import shutil
from datetime import date, datetime, timedelta
from ipaddress import ip_address
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from itsdangerous import BadData, URLSafeTimedSerializer
from sqlalchemy import and_, delete, func, or_, select, text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
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
from app.scheduling import next_send_at, utc_naive_now
from app.schemas import (
    FeedbackCreate,
    FeedbackUpdate,
    ReadStateUpsert,
    SendCodeRequest,
    SessionResponse,
    SubscriptionUpsert,
    TaskCreate,
    VerifyCodeRequest,
)
from app.security import create_access_token, decode_access_token, sign_code, verify_code
from app.services.generation import (
    TaskUnavailable,
    create_generation_task,
    dispatch_task,
    update_next_run,
)
from app.services.mailer import send_login_code
from app.services.news import rights_cleared_source_ids, source_content_type
from app.services.rate_limit import (
    LoginCodeRateLimited,
    RateLimitUnavailable,
    enforce_login_code_rate_limits,
)

log = logging.getLogger("daily_ai_news.api")
router = APIRouter(prefix="/api/v1")
bearer = HTTPBearer(auto_error=False)


def _rate_limit_client_ip(request: Request) -> str:
    """Use Cloudflare's edge-overwritten address header, never generic X-Real-IP."""
    peer_ip = request.client.host if request.client else "unknown"
    forwarded_ip = request.headers.get("cf-connecting-ip", "").strip()
    if forwarded_ip:
        try:
            return str(ip_address(forwarded_ip))
        except ValueError:
            pass
    return peer_ip


@router.get("/public/demo-stories")
def public_demo_stories(limit: int = Query(default=3, ge=1, le=10)) -> dict:
    fixture_path = Path(__file__).resolve().parents[3] / "data" / "demo" / "articles.json"
    fixtures = json.loads(fixture_path.read_text(encoding="utf-8"))
    return {
        "disclosure": "以下内容为虚构演示素材，不对应真实新闻、论文、公告或来源链接。",
        "items": [
            {
                "title": str(item.get("title", ""))[:400],
                "summary": str(item.get("summary", ""))[:1800],
                "category": str(item.get("category", ""))[:64],
                "content_type": "news",
                "is_demo": True,
            }
            for item in fixtures.get("articles", [])[:limit]
            if item.get("title") and item.get("summary")
        ],
    }


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    db: Session = Depends(get_db),
) -> User:
    if not credentials or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sign in to continue.")
    user_id = decode_access_token(credentials.credentials)
    user = db.get(User, user_id) if user_id else None
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="This session has expired."
        )
    return user


def get_admin_user(user: User = Depends(get_current_user)) -> User:
    admin_emails = {
        email.strip().casefold()
        for email in get_settings().admin_emails.split(",")
        if email.strip()
    }
    if not admin_emails or user.email.casefold() not in admin_emails:
        raise HTTPException(status_code=403, detail="Administrator access is required.")
    return user


@router.get("/health")
def health() -> dict:
    settings = get_settings()
    return {
        "status": "ok",
        "mode": "demo" if settings.demo_mode else "live",
        "model_configured": bool(
            settings.llm_base_url and settings.llm_model and settings.llm_api_key
        ),
        "email_configured": bool(
            not settings.demo_mode and settings.smtp_host and settings.smtp_from
        ),
        "local_login_enabled": settings.app_env.lower() == "development",
        "task_backend": "celery" if settings.celery_enabled else "local-background",
        "database": "configured",
    }


@router.get("/health/ready")
def readiness(db: Session = Depends(get_db)) -> dict:
    settings = get_settings()
    try:
        db.execute(
            text(
                "SELECT 1 FROM users, subscriptions, news_articles, "
                "generation_tasks, briefings LIMIT 0"
            )
        )
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Database is not ready.") from exc

    queue_status = "disabled"
    if settings.celery_enabled:
        from redis import Redis

        client = Redis.from_url(
            settings.redis_url,
            socket_connect_timeout=2,
            socket_timeout=2,
            health_check_interval=10,
        )
        try:
            client.ping()
            queue_status = "ready"
        except Exception as exc:
            raise HTTPException(status_code=503, detail="Task queue is not ready.") from exc
        finally:
            client.close()
    return {"status": "ready", "database": "ready", "task_queue": queue_status}


@router.post("/auth/demo", response_model=SessionResponse)
def demo_session(db: Session = Depends(get_db)) -> dict:
    settings = get_settings()
    if settings.app_env.lower() != "development":
        raise HTTPException(status_code=404, detail="The local demo login is disabled.")
    email = "demo@ai-news.test"
    user = db.scalar(select(User).where(User.email == email))
    if user is None:
        user = User(email=email, display_name="演示用户", is_demo=True)
        db.add(user)
        db.flush()
    subscription = db.scalar(select(Subscription).where(Subscription.user_id == user.id))
    if subscription is None:
        subscription = Subscription(
            user_id=user.id,
            topics=["模型", "工具", "开源"],
            keywords=["AI"],
            item_limit=5,
            detail_level="detailed",
            send_time="08:00",
            enabled=True,
            next_run_at=next_send_at("08:00"),
        )
        db.add(subscription)
    db.commit()
    return {
        "access_token": create_access_token(user.id),
        "user": user_payload(user),
        "demo_mode": settings.demo_mode,
    }


@router.post("/auth/send-code", status_code=202)
def request_login_code(
    body: SendCodeRequest, request: Request, db: Session = Depends(get_db)
) -> dict:
    settings = get_settings()
    if settings.demo_mode:
        raise HTTPException(
            status_code=409, detail="This local build uses the explicitly labeled demo sign-in."
        )
    email = str(body.email).casefold()
    client_ip = _rate_limit_client_ip(request)
    try:
        enforce_login_code_rate_limits(
            email=email,
            client_ip=client_ip,
            redis_url=settings.redis_url,
            secret=settings.auth_secret,
            email_hour_limit=settings.login_code_email_hour_limit,
            ip_hour_limit=settings.login_code_ip_hour_limit,
        )
    except LoginCodeRateLimited as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except RateLimitUnavailable as exc:
        log.error("login_code_rate_limit_unavailable")
        raise HTTPException(
            status_code=503, detail="Email verification is temporarily unavailable."
        ) from exc
    last = db.scalar(
        select(VerificationCode)
        .where(VerificationCode.email == email, VerificationCode.purpose == "login")
        .order_by(VerificationCode.sent_at.desc())
        .limit(1)
    )
    now = utc_naive_now()
    if last and (now - last.sent_at).total_seconds() < 60:
        raise HTTPException(
            status_code=429, detail="Wait one minute before requesting another code."
        )
    code = f"{secrets.randbelow(1_000_000):06d}"
    record = VerificationCode(
        email=email,
        purpose="login",
        code_hash=sign_code(email, code),
        sent_at=now,
        expires_at=now + timedelta(minutes=10),
    )
    db.add(record)
    db.commit()
    try:
        send_login_code(email, code)
    except Exception as exc:
        record.consumed = True
        db.commit()
        log.warning(
            "login_code_delivery_failed",
            extra={"email_domain": email.rpartition("@")[2], "error_type": type(exc).__name__},
        )
        raise HTTPException(
            status_code=503, detail="Email verification is temporarily unavailable."
        ) from exc
    return {
        "message": "If this address can receive mail, a one-time code is on its way.",
        "expires_in_seconds": 600,
    }


@router.post("/auth/verify-code", response_model=SessionResponse)
def verify_login_code(body: VerifyCodeRequest, db: Session = Depends(get_db)) -> dict:
    email = str(body.email).casefold()
    now = utc_naive_now()
    record = db.scalar(
        select(VerificationCode)
        .where(
            VerificationCode.email == email,
            VerificationCode.purpose == "login",
            VerificationCode.consumed.is_(False),
        )
        .order_by(VerificationCode.sent_at.desc())
        .limit(1)
    )
    if (
        record is None
        or record.expires_at <= now
        or record.attempts >= 5
        or not verify_code(email, body.code, record.code_hash)
    ):
        if record is not None:
            record.attempts = min(record.attempts + 1, 5)
            db.commit()
        raise HTTPException(status_code=400, detail="The sign-in code is invalid or expired.")
    record.consumed = True
    user = db.scalar(select(User).where(User.email == email))
    if user is None:
        user = User(email=email, display_name=body.display_name or email.partition("@")[0][:80])
        db.add(user)
        db.flush()
    user.email_verified = True
    if not user.is_active:
        raise HTTPException(status_code=403, detail="This account is disabled.")
    db.commit()
    return {
        "access_token": create_access_token(user.id),
        "user": user_payload(user),
        "demo_mode": False,
    }


@router.get("/me")
def current_profile(user: User = Depends(get_current_user)) -> dict:
    return user_payload(user)


@router.get("/me/export")
def export_my_data(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    visible_briefings = set()
    subscription = db.scalar(select(Subscription).where(Subscription.user_id == user.id))
    tasks = list(
        db.scalars(
            select(GenerationTask)
            .where(GenerationTask.user_id == user.id)
            .order_by(GenerationTask.created_at)
        ).all()
    )
    briefings = list(
        db.scalars(
            select(Briefing)
            .where(Briefing.user_id == user.id)
            .order_by(Briefing.created_at)
        ).all()
    )
    briefings = [briefing for briefing in briefings if briefing_is_displayable(briefing)]
    visible_briefings = {briefing.id for briefing in briefings}
    states = list(
        db.scalars(
            select(ReadState)
            .where(ReadState.user_id == user.id)
            .order_by(ReadState.updated_at)
        ).all()
    )
    state_by_briefing = {
        state.briefing_id: state for state in states if state.briefing_id in visible_briefings
    }
    feedback_rows = db.execute(
        select(Feedback, Briefing)
        .join(Briefing, Briefing.id == Feedback.briefing_id)
        .where(Feedback.user_id == user.id)
        .where(Feedback.briefing_id.in_(visible_briefings))
        .order_by(Feedback.created_at)
    ).all()
    deliveries = list(
        db.scalars(
            select(DeliveryAttempt)
            .where(DeliveryAttempt.user_id == user.id)
            .order_by(DeliveryAttempt.created_at)
        ).all()
    )
    return {
        "export_version": 1,
        "exported_at": utc_naive_now(),
        "account": {
            "id": user.id,
            "email": user.email,
            "display_name": user.display_name,
            "email_verified": user.email_verified,
            "created_at": user.created_at,
        },
        "subscription": subscription_payload(subscription) if subscription else None,
        "generation_tasks": [task_payload(task) for task in tasks],
        "briefings": [
            briefing_payload(briefing, state_by_briefing.get(briefing.id))
            for briefing in briefings
        ],
        "read_states": [
            {
                "briefing_id": state.briefing_id,
                "is_read": state.is_read,
                "is_bookmarked": state.is_bookmarked,
                "updated_at": state.updated_at,
            }
            for state in states
            if state.briefing_id in visible_briefings
        ],
        "feedback": [feedback_payload(feedback, briefing) for feedback, briefing in feedback_rows],
        "delivery_attempts": [
            {
                "id": delivery.id,
                "task_id": delivery.task_id,
                "installation_id": delivery.installation_id,
                "channel": delivery.channel,
                "status": delivery.status,
                "provider_receipt_id": delivery.provider_receipt_id,
                "last_error": delivery.last_error,
                "created_at": delivery.created_at,
            }
            for delivery in deliveries
        ],
        "excluded_security_data": [
            "登录令牌",
            "邮箱验证码及验证码哈希",
            "邮件投递队列中的临时认证内容",
        ],
    }


@router.delete("/me", status_code=204)
def delete_account(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> None:
    email = user.email.casefold()
    task_ids = list(
        db.scalars(select(GenerationTask.id).where(GenerationTask.user_id == user.id)).all()
    )
    briefing_ids = select(Briefing.id).where(Briefing.user_id == user.id)
    verification_code_ids = select(VerificationCode.id).where(
        func.lower(VerificationCode.email) == email
    )
    db.execute(
        delete(OutboxEvent).where(
            or_(
                and_(
                    OutboxEvent.event_type == "generation.requested",
                    OutboxEvent.aggregate_id.in_(task_ids),
                ),
                and_(
                    OutboxEvent.event_type == "briefing.published",
                    OutboxEvent.aggregate_id.in_(briefing_ids),
                ),
            )
        )
    )
    db.execute(
        delete(EmailOutbox).where(
            or_(
                func.lower(EmailOutbox.email) == email,
                EmailOutbox.verification_code_id.in_(verification_code_ids),
            )
        )
    )
    db.execute(delete(VerificationCode).where(func.lower(VerificationCode.email) == email))
    db.delete(user)
    db.commit()
    workspace_root = Path(get_settings().tool_task_root)
    if not workspace_root.is_absolute():
        workspace_root = Path.cwd() / workspace_root
    workspace_root = workspace_root.resolve()
    for task_id in task_ids:
        workspace = workspace_root / task_id
        try:
            if workspace.is_symlink():
                workspace.unlink()
            elif workspace.is_dir() and workspace.resolve().is_relative_to(workspace_root):
                shutil.rmtree(workspace)
        except OSError:
            log.exception("account_task_workspace_cleanup_failed", extra={"task_id": task_id})


@router.get("/subscription")
def read_subscription(
    user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> dict:
    subscription = db.scalar(select(Subscription).where(Subscription.user_id == user.id))
    return subscription_payload(subscription) if subscription else {"configured": False}


@router.put("/subscription")
def save_subscription(
    body: SubscriptionUpsert, user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> dict:
    subscription = db.scalar(select(Subscription).where(Subscription.user_id == user.id))
    if subscription is not None and body.version != subscription.version:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "This subscription changed on another device. Reload and retry.",
                "current_version": subscription.version,
            },
        )
    if subscription is None and body.version not in (0, 1):
        raise HTTPException(status_code=409, detail="The subscription version is not valid.")
    if body.enabled and not body.topics and not body.keywords:
        raise HTTPException(
            status_code=422, detail="Choose a topic or keyword, or pause the subscription."
        )
    enabling_email = body.notify_enabled and (subscription is None or not subscription.notify_enabled)
    if enabling_email:
        if not user.email_verified:
            raise HTTPException(
                status_code=403, detail="Verify this email address before enabling email reminders."
            )
        settings = get_settings()
        if settings.demo_mode or not settings.smtp_host or not settings.smtp_from:
            raise HTTPException(
                status_code=409, detail="Email reminders are unavailable until SMTP is configured."
            )
    if subscription is None:
        subscription = Subscription(user_id=user.id)
        db.add(subscription)
    for field, value in body.model_dump(exclude={"version"}).items():
        setattr(subscription, field, value)
    update_next_run(db, subscription, new_version=True)
    db.commit()
    db.refresh(subscription)
    return subscription_payload(subscription)


@router.post("/briefing-tasks", status_code=202)
def create_task(
    body: TaskCreate,
    background: BackgroundTasks,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    try:
        task = create_generation_task(db, user.id, preview=body.kind == "preview")
    except TaskUnavailable as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if task.status in {"queued", "failed"}:
        settings = get_settings()
        if settings.celery_enabled:
            try:
                from app.celery_app import celery_app

                celery_app.send_task("daily_ai_news.generate_briefing", args=[task.id])
            except Exception as exc:
                log.exception("generation_queue_unavailable", extra={"task_id": task.id})
                raise HTTPException(status_code=503, detail="Task queue unavailable.") from exc
        else:
            background.add_task(dispatch_task, task.id)
    return task_payload(task)


@router.get("/briefing-tasks/{task_id}")
def get_task(
    task_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> dict:
    task = db.scalar(
        select(GenerationTask).where(
            GenerationTask.id == task_id, GenerationTask.user_id == user.id
        )
    )
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found.")
    return task_payload(task)


@router.get("/briefings")
def list_briefings(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    limit: int = Query(default=30, ge=1, le=100),
    before: str | None = Query(default=None, max_length=2048),
    cursor: str | None = Query(default=None, max_length=2048),
    q: str = Query(default="", max_length=200),
    bookmarked: bool = False,
    is_read: bool | None = None,
) -> dict:
    q = q.strip()
    scope = {"user_id": user.id, "q": q, "bookmarked": bookmarked, "is_read": is_read}
    serializer = URLSafeTimedSerializer(get_settings().auth_secret, salt="briefing-history-v1")
    query = (
        select(Briefing, ReadState)
        .outerjoin(
            ReadState,
            and_(ReadState.briefing_id == Briefing.id, ReadState.user_id == user.id),
        )
        .where(Briefing.user_id == user.id)
        .order_by(Briefing.business_date.desc(), Briefing.created_at.desc(), Briefing.id.desc())
        .limit(limit + 1)
    )
    if q:
        escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        query = query.where(Briefing.title.ilike(f"%{escaped}%", escape="\\"))
    if bookmarked:
        query = query.where(ReadState.is_bookmarked.is_(True))
    if is_read is True:
        query = query.where(ReadState.is_read.is_(True))
    elif is_read is False:
        query = query.where(or_(ReadState.is_read.is_(False), ReadState.id.is_(None)))
    if cursor and before:
        raise HTTPException(status_code=422, detail="Use cursor or before, not both.")
    position = cursor or before
    if position:
        try:
            # Keep manually supplied legacy date cutoffs working. Returned cursors are opaque.
            if before and len(before) == 10:
                cutoff_date = date.fromisoformat(before).isoformat()
                query = query.where(Briefing.business_date < cutoff_date)
            else:
                payload = serializer.loads(position, max_age=86400)
                if payload["scope"] != scope:
                    raise ValueError("Cursor scope changed")
                day, created, briefing_id = payload["position"]
                day = date.fromisoformat(day).isoformat()
                created = datetime.fromisoformat(created)
                UUID(briefing_id)
                if created.tzinfo is not None:
                    raise ValueError("Cursor timestamp must be UTC-naive")
                query = query.where(
                    or_(
                        Briefing.business_date < day,
                        and_(Briefing.business_date == day, Briefing.created_at < created),
                        and_(
                            Briefing.business_date == day,
                            Briefing.created_at == created,
                            Briefing.id < briefing_id,
                        ),
                    )
                )
        except (BadData, ValueError, TypeError, KeyError, AttributeError) as exc:
            raise HTTPException(
                status_code=422, detail="History cursor is invalid or expired. Reload the list."
            ) from exc
    rows = list(db.execute(query).all())
    more = len(rows) > limit
    rows = rows[:limit]
    visible_rows = [
        (briefing, state) for briefing, state in rows if briefing_is_displayable(briefing)
    ]
    next_cursor = None
    if more and rows:
        last = rows[-1][0]
        next_cursor = serializer.dumps(
            {"scope": scope, "position": [last.business_date, last.created_at.isoformat(), last.id]}
        )
    return {
        "items": [briefing_payload(briefing, state) for briefing, state in visible_rows],
        "has_more": more,
        "next_cursor": next_cursor,
        "next_before": next_cursor,
    }


@router.get("/briefings/{briefing_id}")
def get_briefing(
    briefing_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> dict:
    briefing = db.scalar(
        select(Briefing).where(Briefing.id == briefing_id, Briefing.user_id == user.id)
    )
    if briefing is None:
        raise HTTPException(status_code=404, detail="Briefing not found.")
    if not briefing_is_displayable(briefing):
        raise HTTPException(status_code=404, detail="Briefing not found.")
    state = db.scalar(
        select(ReadState).where(ReadState.user_id == user.id, ReadState.briefing_id == briefing.id)
    )
    return briefing_payload(briefing, state)


@router.put("/briefings/{briefing_id}/state")
def upsert_read_state(
    briefing_id: str,
    body: ReadStateUpsert,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    briefing = db.scalar(
        select(Briefing).where(Briefing.id == briefing_id, Briefing.user_id == user.id)
    )
    if briefing is None:
        raise HTTPException(status_code=404, detail="Briefing not found.")
    if not briefing_is_displayable(briefing):
        raise HTTPException(status_code=404, detail="Briefing not found.")
    row = db.scalar(
        select(ReadState).where(ReadState.user_id == user.id, ReadState.briefing_id == briefing.id)
    )
    if row is None:
        row = ReadState(user_id=user.id, briefing_id=briefing.id)
        db.add(row)
    if body.is_read is not None:
        row.is_read = body.is_read
    if body.is_bookmarked is not None:
        row.is_bookmarked = body.is_bookmarked
    row.updated_at = utc_naive_now()
    db.commit()
    db.refresh(row)
    return {
        "briefing_id": briefing.id,
        "is_read": row.is_read,
        "is_bookmarked": row.is_bookmarked,
        "updated_at": row.updated_at,
    }


@router.post("/briefings/{briefing_id}/feedback", status_code=201)
def create_feedback(
    briefing_id: str,
    body: FeedbackCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    briefing = db.scalar(
        select(Briefing).where(Briefing.id == briefing_id, Briefing.user_id == user.id)
    )
    if briefing is None:
        raise HTTPException(status_code=404, detail="Briefing not found.")
    if not briefing_is_displayable(briefing):
        raise HTTPException(status_code=404, detail="Briefing not found.")
    feedback = Feedback(
        user_id=user.id,
        briefing_id=briefing.id,
        item_index=body.item_index,
        kind=body.kind,
        detail=body.detail,
    )
    db.add(feedback)
    db.commit()
    return {"accepted": True, "feedback_id": feedback.id}


@router.get("/me/feedback")
def list_my_feedback(
    user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> dict:
    rows = db.execute(
        select(Feedback, Briefing)
        .join(Briefing, Briefing.id == Feedback.briefing_id)
        .where(Feedback.user_id == user.id)
        .order_by(Feedback.created_at.desc())
        .limit(100)
    ).all()
    return {
        "items": [
            feedback_payload(feedback, briefing)
            for feedback, briefing in rows
            if briefing_is_displayable(briefing)
        ]
    }


@router.get("/admin/feedback")
def list_feedback_for_review(
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=100),
    _admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
) -> dict:
    statement = (
        select(Feedback, Briefing)
        .join(Briefing, Briefing.id == Feedback.briefing_id)
        .order_by(Feedback.created_at.desc())
        .limit(limit)
    )
    if status_filter:
        if status_filter not in {"open", "in_review", "resolved", "dismissed"}:
            raise HTTPException(status_code=422, detail="Unsupported feedback status.")
        statement = statement.where(Feedback.status == status_filter)
    rows = db.execute(statement).all()
    return {
        "items": [
            feedback_payload(feedback, briefing)
            for feedback, briefing in rows
            if briefing_is_displayable(briefing)
        ]
    }


@router.patch("/admin/feedback/{feedback_id}")
def update_feedback_for_review(
    feedback_id: str,
    body: FeedbackUpdate,
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
) -> dict:
    feedback = db.get(Feedback, feedback_id)
    if feedback is None:
        raise HTTPException(status_code=404, detail="Feedback not found.")
    briefing = db.get(Briefing, feedback.briefing_id)
    if briefing is None or not briefing_is_displayable(briefing):
        raise HTTPException(status_code=404, detail="Feedback not found.")
    if body.status in {"resolved", "dismissed"} and not body.reply.strip():
        raise HTTPException(
            status_code=422,
            detail="Add a reply before resolving or dismissing feedback.",
        )
    feedback.status = body.status
    feedback.reply = body.reply.strip()
    feedback.handled_by_user_id = admin.id
    feedback.updated_at = utc_naive_now()
    db.commit()
    db.refresh(feedback)
    return feedback_payload(feedback, briefing)


def feedback_payload(feedback: Feedback, briefing: Briefing) -> dict:
    return {
        "id": feedback.id,
        "briefing_id": briefing.id,
        "briefing_title": briefing.title,
        "business_date": briefing.business_date,
        "item_index": feedback.item_index,
        "kind": feedback.kind,
        "detail": feedback.detail,
        "status": feedback.status,
        "reply": feedback.reply,
        "created_at": feedback.created_at,
        "updated_at": feedback.updated_at,
    }


@router.get("/agent/tool-traces")
def list_agent_traces(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    limit: int = Query(default=20, ge=1, le=100),
) -> dict:
    tasks = list(
        db.scalars(
            select(GenerationTask)
            .where(GenerationTask.user_id == user.id)
            .order_by(GenerationTask.created_at.desc())
            .limit(limit)
        ).all()
    )
    return {"items": [task_payload(task) for task in tasks]}


def user_payload(user: User) -> dict:
    admin_emails = {
        email.strip().casefold()
        for email in get_settings().admin_emails.split(",")
        if email.strip()
    }
    return {
        "id": user.id,
        "email": user.email,
        "display_name": user.display_name,
        "is_demo": user.is_demo,
        "email_verified": user.email_verified,
        "is_admin": user.email.casefold() in admin_emails,
        "created_at": user.created_at,
    }


def subscription_payload(subscription: Subscription) -> dict:
    return {
        "configured": True,
        "id": subscription.id,
        "topics": subscription.topics,
        "keywords": subscription.keywords,
        "excluded_keywords": subscription.excluded_keywords,
        "match_all": subscription.match_all,
        "semantic_expansion": subscription.semantic_expansion,
        "item_limit": subscription.item_limit,
        "detail_level": subscription.detail_level,
        "send_time": subscription.send_time,
        "enabled": subscription.enabled,
        "notify_enabled": subscription.notify_enabled,
        "version": subscription.version,
        "next_run_at": subscription.next_run_at,
    }


def task_payload(task: GenerationTask) -> dict:
    return {
        "id": task.id,
        "status": task.status,
        "kind": task.kind,
        "provider": task.provider,
        "stop_reason": task.stop_reason,
        "error_message": task.error_message,
        "created_at": task.created_at,
        "finished_at": task.finished_at,
        "tools": task.tool_calls or [],
        "token_usage": task.token_usage or {},
    }


def briefing_payload(briefing: Briefing, state: ReadState | None) -> dict:
    items = []
    for raw_item in briefing.items or []:
        if not isinstance(raw_item, dict):
            continue
        item = dict(raw_item)
        item.setdefault("content_type", source_content_type(str(item.get("source_id", ""))))
        items.append(item)
    return {
        "id": briefing.id,
        "task_id": briefing.task_id,
        "business_date": briefing.business_date,
        "kind": briefing.kind,
        "status": briefing.status,
        "provider": briefing.provider,
        "is_demo": briefing.is_demo,
        "title": briefing.title,
        "summary": briefing.summary,
        "items": items,
        "created_at": briefing.created_at,
        "is_read": state.is_read if state else False,
        "is_bookmarked": state.is_bookmarked if state else False,
    }


def briefing_is_displayable(briefing: Briefing) -> bool:
    """Fail closed for stored live content without complete recorded source clearance."""
    if briefing.is_demo:
        return True
    raw_items = briefing.items or []
    if not raw_items:
        return False
    cleared_sources = rights_cleared_source_ids()
    return all(
        isinstance(item, dict)
        and bool(item.get("source_id"))
        and str(item["source_id"]) in cleared_sources
        for item in raw_items
    )
