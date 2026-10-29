import pytest
from app.services.rate_limit import (
    LoginCodeRateLimited,
    RateLimitUnavailable,
    enforce_login_code_rate_limits,
)
from redis.exceptions import ConnectionError as RedisConnectionError


class FakeRedis:
    def __init__(self):
        self.counts = {}
        self.keys = []

    def eval(self, script, num_keys, *values):
        assert "redis.call('GET'" in script
        assert "redis.call('INCR'" in script
        keys = values[:num_keys]
        self.keys.extend(keys)
        limits = values[num_keys:]
        for index, key in enumerate(keys):
            if self.counts.get(key, 0) >= int(limits[index * 2]):
                return 0
        for key in keys:
            self.counts[key] = self.counts.get(key, 0) + 1
        return 1

    def close(self):
        pass


def _enforce(email: str, client_ip: str = "203.0.113.5") -> None:
    enforce_login_code_rate_limits(
        email=email,
        client_ip=client_ip,
        redis_url="redis://unused",
        secret="test-secret",
    )


def test_email_cannot_request_more_than_one_code_per_minute(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr("app.services.rate_limit.Redis.from_url", lambda *_a, **_k: fake)

    _enforce("reader@example.test")
    with pytest.raises(LoginCodeRateLimited):
        _enforce("reader@example.test")


def test_hourly_email_limit_applies_across_minute_windows(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr("app.services.rate_limit.Redis.from_url", lambda *_a, **_k: fake)

    for _ in range(5):
        _enforce("reader@example.test")
        minute_key = next(key for key in fake.keys if ":email-minute:" in key)
        fake.counts[minute_key] = 0  # simulate expiry of the one-minute bucket

    with pytest.raises(LoginCodeRateLimited):
        _enforce("reader@example.test")


def test_hourly_ip_limit_applies_across_distinct_addresses(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr("app.services.rate_limit.Redis.from_url", lambda *_a, **_k: fake)

    for index in range(30):
        _enforce(f"reader-{index}@example.test")
    with pytest.raises(LoginCodeRateLimited):
        _enforce("reader-31@example.test")
    assert all("reader-" not in key for key in fake.keys)
    assert all("203.0.113.5" not in key for key in fake.keys)

    ip_key = next(key for key in fake.keys if ":ip-hour:" in key)
    fake.counts[ip_key] = 0  # simulate expiry of the IP window
    _enforce("reader-31@example.test")


def test_rate_limiter_fails_closed_when_redis_is_unavailable(monkeypatch):
    class UnavailableRedis(FakeRedis):
        def eval(self, *_args):
            raise RedisConnectionError("simulated outage")

    fake = UnavailableRedis()
    monkeypatch.setattr("app.services.rate_limit.Redis.from_url", lambda *_a, **_k: fake)

    with pytest.raises(RateLimitUnavailable):
        _enforce("reader@example.test")
