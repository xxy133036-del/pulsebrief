from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal
from app.models import NewsArticle, Subscription, User
from app.scheduling import next_send_at
from app.services.news import add_article


def seed_demo() -> int:
    settings = get_settings()
    if not settings.demo_mode:
        return 0
    fixture_path = Path(__file__).resolve().parents[2] / "data" / "demo" / "articles.json"
    fixtures = json.loads(fixture_path.read_text(encoding="utf-8"))["articles"]
    db = SessionLocal()
    try:
        user = db.scalar(select(User).where(User.email == "demo@ai-news.test"))
        if user is None:
            user = User(email="demo@ai-news.test", display_name="演示用户", is_demo=True)
            db.add(user)
            db.flush()
        subscription = db.scalar(select(Subscription).where(Subscription.user_id == user.id))
        if subscription is None:
            subscription = Subscription(
                user_id=user.id,
                topics=["模型", "工具", "开源"],
                keywords=["AI"],
                excluded_keywords=[],
                item_limit=5,
                detail_level="detailed",
                send_time="08:00",
                enabled=True,
                next_run_at=next_send_at("08:00"),
            )
            db.add(subscription)
        count = 0
        now = datetime.now(UTC).replace(tzinfo=None)
        for fixture in fixtures:
            existing = db.scalar(
                select(NewsArticle.id).where(
                    NewsArticle.source_id == fixture["source_id"], NewsArticle.url == fixture["url"]
                )
            )
            if existing:
                continue
            add_article(db, **fixture, published_at=now, is_demo=True)
            count += 1
        db.commit()
        return count
    finally:
        db.close()


if __name__ == "__main__":
    if get_settings().demo_mode:
        print(f"Loaded {seed_demo()} explicitly labeled local demo stories.")
    else:
        from app.worker.collector import collect_all

        result = collect_all()
        print(json.dumps(result, ensure_ascii=False))
