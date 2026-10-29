from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

BUSINESS_ZONE = ZoneInfo("Asia/Shanghai")


def utc_naive_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def next_send_at(send_time: str, *, after: datetime | None = None) -> datetime:
    now = after or utc_naive_now()
    local_now = now.replace(tzinfo=UTC).astimezone(BUSINESS_ZONE)
    hour, minute = (int(part) for part in send_time.split(":"))
    target = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= local_now + timedelta(seconds=1):
        target += timedelta(days=1)
    return target.astimezone(UTC).replace(tzinfo=None)


def business_date(at: datetime | None = None) -> str:
    now = (at or utc_naive_now()).replace(tzinfo=UTC)
    return now.astimezone(BUSINESS_ZONE).date().isoformat()
