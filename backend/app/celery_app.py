from celery import Celery

from app.config import get_settings

settings = get_settings()
celery_app = Celery(
    "daily_ai_news",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["app.worker.tasks"],
)
celery_app.conf.update(
    timezone="Asia/Shanghai",
    enable_utc=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_track_started=True,
    result_expires=3600,
    beat_schedule={
        "dispatch-due-briefings-every-minute": {
            "task": "daily_ai_news.dispatch_due",
            "schedule": 60.0,
        },
        "recover-stalled-briefings-every-minute": {
            "task": "daily_ai_news.recover_stalled",
            "schedule": 60.0,
        },
        "collect-approved-feeds": {"task": "daily_ai_news.collect_feeds", "schedule": 1800.0},
        "dispatch-briefing-outbox": {"task": "daily_ai_news.dispatch_outbox", "schedule": 10.0},
    },
)
