from __future__ import annotations

import hashlib
import hmac

from redis import Redis
from redis.exceptions import RedisError

_INCREMENT_LIMITS = """
for index, key in ipairs(KEYS) do
    local count = tonumber(redis.call('GET', key) or '0')
    local maximum = tonumber(ARGV[(index - 1) * 2 + 1])
    if count >= maximum then
        return 0
    end
end
for index, key in ipairs(KEYS) do
    local count = redis.call('INCR', key)
    local ttl = tonumber(ARGV[(index - 1) * 2 + 2])
    if count == 1 then
        redis.call('EXPIRE', key, ttl)
    end
end
return 1
"""

EMAIL_PER_MINUTE = 1
EMAIL_PER_HOUR = 5
IP_PER_HOUR = 30


class LoginCodeRateLimited(ValueError):
    pass


class RateLimitUnavailable(RuntimeError):
    pass


def _key(secret: str, scope: str, value: str) -> str:
    digest = hmac.new(secret.encode(), f"{scope}:{value}".encode(), hashlib.sha256).hexdigest()
    return f"daily-ai-news:auth-code:v1:{scope}:{digest}"


def enforce_login_code_rate_limits(
    *,
    email: str,
    client_ip: str,
    redis_url: str,
    secret: str,
    email_hour_limit: int = EMAIL_PER_HOUR,
    ip_hour_limit: int = IP_PER_HOUR,
) -> None:
    keys = [
        _key(secret, "email-minute", email),
        _key(secret, "email-hour", email),
        _key(secret, "ip-hour", client_ip),
    ]
    redis = Redis.from_url(redis_url, socket_connect_timeout=2, socket_timeout=2)
    try:
        allowed = redis.eval(
            _INCREMENT_LIMITS,
            len(keys),
            *keys,
            EMAIL_PER_MINUTE,
            60,
            email_hour_limit,
            3600,
            ip_hour_limit,
            3600,
        )
    except RedisError as exc:
        raise RateLimitUnavailable("The sign-in rate-limit service is unavailable.") from exc
    finally:
        redis.close()
    if int(allowed) != 1:
        raise LoginCodeRateLimited("Too many sign-in code requests. Please try again later.")
