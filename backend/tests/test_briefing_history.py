from datetime import datetime

from app.models import Briefing, GenerationTask, ReadState, Subscription, User
from app.security import create_access_token


def seed_history(db):
    owner = User(email="history-owner@example.test")
    other = User(email="history-other@example.test")
    db.add_all([owner, other])
    db.flush()
    owned = []
    for person in (owner, other):
        subscription = Subscription(user_id=person.id, topics=["AI"])
        db.add(subscription)
        db.flush()
        titles = (
            ["OpenAI update", "Literal 100%_match", "OpenAI older", "Other", "OpenAI tools", "Other", "OpenAI archive"]
            if person is owner
            else ["OpenAI private"]
        )
        for index, title in enumerate(titles):
            day = "2026-10-14" if index == 6 else "2026-10-15"
            task = GenerationTask(
                user_id=person.id, subscription_id=subscription.id, business_date=day,
                kind=f"preview-{index}", status="published",
            )
            db.add(task)
            db.flush()
            briefing = Briefing(
                user_id=person.id, subscription_id=subscription.id, task_id=task.id,
                business_date=day, kind=task.kind, status="published", provider="test",
                is_demo=True, news_cutoff_at=datetime(2026, 10, 15), created_at=datetime(2026, 10, 15, 8),
                title=title, summary="History test fixture", items=[],
            )
            db.add(briefing)
            db.flush()
            if person is owner:
                owned.append(briefing)
                if index in {0, 1, 4, 6}:
                    db.add(ReadState(
                        user_id=owner.id, briefing_id=briefing.id,
                        is_read=index in {0, 6}, is_bookmarked=index in {0, 4, 6},
                    ))
    db.commit()
    return owner, other, owned


def headers(user):
    return {"Authorization": f"Bearer {create_access_token(user.id)}"}


def test_history_cursor_keeps_same_day_and_timestamp_rows_without_gaps(client, db):
    owner, _, owned = seed_history(db)
    collected = []
    params = {"limit": 2}
    for _ in range(10):
        response = client.get("/api/v1/briefings", headers=headers(owner), params=params)
        assert response.status_code == 200
        page = response.json()
        collected.extend(item["id"] for item in page["items"])
        if not page["has_more"]:
            assert page["next_cursor"] is None
            break
        assert page["next_before"] == page["next_cursor"]
        params["before"] = page["next_before"]
    expected = sorted(owned, key=lambda row: (row.business_date, row.created_at, row.id), reverse=True)
    assert collected == [row.id for row in expected]
    assert len(set(collected)) == len(owned)


def test_history_filters_search_all_dates_and_treat_wildcards_literally(client, db):
    owner, _, owned = seed_history(db)
    response = client.get(
        "/api/v1/briefings", headers=headers(owner),
        params={"q": "openai", "bookmarked": True, "is_read": False},
    )
    assert [item["id"] for item in response.json()["items"]] == [owned[4].id]
    unread = client.get(
        "/api/v1/briefings", headers=headers(owner), params={"is_read": False},
    ).json()["items"]
    assert {item["id"] for item in unread} == {owned[i].id for i in (1, 2, 3, 4, 5)}
    literal = client.get(
        "/api/v1/briefings", headers=headers(owner), params={"q": "100%_"},
    ).json()["items"]
    assert [item["id"] for item in literal] == [owned[1].id]
    legacy = client.get(
        "/api/v1/briefings", headers=headers(owner), params={"before": "2026-10-15"},
    ).json()["items"]
    assert [item["id"] for item in legacy] == [owned[6].id]


def test_history_cursor_rejects_changed_filters_other_users_and_invalid_values(client, db):
    owner, other, _ = seed_history(db)
    first = client.get(
        "/api/v1/briefings", headers=headers(owner), params={"limit": 1, "q": "OpenAI"},
    ).json()
    cursor = first["next_cursor"]
    for user, params in (
        (owner, {"cursor": cursor, "q": "Other"}),
        (other, {"cursor": cursor, "q": "OpenAI"}),
        (owner, {"cursor": "broken" + cursor, "q": "OpenAI"}),
        (owner, {"cursor": cursor, "before": "2026-10-15", "q": "OpenAI"}),
        (owner, {"before": "2026-99-99"}),
    ):
        response = client.get("/api/v1/briefings", headers=headers(user), params=params)
        assert response.status_code == 422
