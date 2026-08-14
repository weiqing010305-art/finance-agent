from __future__ import annotations

import time
from dataclasses import dataclass


_SLIDING_WINDOW = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local member = ARGV[4]
redis.call('ZREMRANGEBYSCORE', key, '-inf', now - window)
local count = redis.call('ZCARD', key)
if count >= limit then return {0, count} end
redis.call('ZADD', key, now, member)
redis.call('PEXPIRE', key, window)
return {1, count + 1}
"""


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    count: int
    degraded: bool = False


class RedisSlidingWindowLimiter:
    def __init__(self, redis_client):
        self.redis = redis_client

    def check(
        self, *, scope: str, identity: str, limit: int, window_seconds: int,
        fail_closed: bool,
    ) -> RateLimitDecision:
        now = int(time.time() * 1000)
        try:
            allowed, count = self.redis.eval(
                _SLIDING_WINDOW, 1, f"finscope:rate:{scope}:{identity}", now,
                window_seconds * 1000, limit, f"{now}-{time.time_ns()}",
            )
            return RateLimitDecision(bool(allowed), int(count))
        except Exception:
            return RateLimitDecision(not fail_closed, 0, degraded=True)
