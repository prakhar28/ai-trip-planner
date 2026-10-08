"""Run events on Redis Streams.

Streams (not plain pub/sub) give us replay for free: a client that connects late,
or reconnects with Last-Event-ID, gets every event it missed, with no race between
"read history" and "subscribe".
"""
import json

from redis.asyncio import Redis

from .config import settings

STREAM_TTL_SECONDS = 60 * 60 * 24
FINAL_EVENT_TYPES = {"done", "error"}

redis = Redis.from_url(settings.redis_url, decode_responses=True)


def stream_key(run_id: str) -> str:
    return f"run:{run_id}:events"


async def publish(run_id: str, event_type: str, data: dict | None = None) -> None:
    key = stream_key(run_id)
    await redis.xadd(key, {"type": event_type, "data": json.dumps(data or {}, default=str)},
                     maxlen=2000, approximate=True)
    await redis.expire(key, STREAM_TTL_SECONDS)


async def read(run_id: str, last_id: str, block_ms: int = 15_000) -> list[tuple[str, dict]]:
    resp = await redis.xread({stream_key(run_id): last_id}, block=block_ms, count=100)
    if not resp:
        return []
    _, entries = resp[0]
    return entries


async def exists(run_id: str) -> bool:
    return bool(await redis.exists(stream_key(run_id)))
