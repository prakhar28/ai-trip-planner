import asyncio
import logging

from arq import func
from arq.connections import RedisSettings

from . import events
from .agent.loop import AgentError, Usage, run_agent_loop
from .config import settings
from .db import TERMINAL_STATUSES, Run, SessionLocal, init_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("worker")


async def _save(run_id: str, **fields) -> None:
    async with SessionLocal() as s:
        run = await s.get(Run, run_id)
        for k, v in fields.items():
            setattr(run, k, v)
        await s.commit()


async def run_agent(ctx: dict, run_id: str) -> None:
    async with SessionLocal() as s:
        run = await s.get(Run, run_id)
        # Idempotent: a duplicate or replayed job must never run the agent twice.
        if run is None or run.status in TERMINAL_STATUSES or run.status == "running":
            log.info("skip run=%s status=%s", run_id, run and run.status)
            return
        run.status = "running"
        prompt = run.prompt
        await s.commit()

    await events.publish(run_id, "status", {"status": "running"})
    log.info("start run=%s", run_id)

    async def emit(event_type: str, data: dict) -> None:
        await events.publish(run_id, event_type, data)

    usage = Usage()
    usage_fields = lambda: dict(turns=usage.turns, input_tokens=usage.input_tokens,  # noqa: E731
                                output_tokens=usage.output_tokens,
                                cache_read_tokens=usage.cache_read_tokens, cost_usd=usage.cost_usd)
    try:
        itinerary = await asyncio.wait_for(run_agent_loop(prompt, emit, usage),
                                           timeout=settings.run_timeout_seconds)
    except Exception as e:  # noqa: BLE001 - every failure must end the run cleanly
        if isinstance(e, AgentError):
            message = str(e)
        elif isinstance(e, asyncio.TimeoutError):
            message = f"Planning took longer than {settings.run_timeout_seconds} seconds and was stopped."
        else:
            log.exception("run=%s crashed", run_id)
            message = "Something went wrong while planning. Try again."
        await _save(run_id, status="failed", error=message, **usage_fields())
        await events.publish(run_id, "error", {"message": message, "usage": usage.summary()})
        log.info("failed run=%s usage=%s", run_id, usage.summary())
        return

    result = itinerary.model_dump()
    await _save(run_id, status="completed", result=result, **usage_fields())
    await events.publish(run_id, "done", {"itinerary": result, "usage": usage.summary()})
    log.info("done run=%s usage=%s", run_id, usage.summary())


async def startup(ctx: dict) -> None:
    await init_db()


class WorkerSettings:
    # max_tries=1: a whole-run retry re-spends tokens. API calls already retry inside the SDK.
    functions = [func(run_agent, max_tries=1)]
    on_startup = startup
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    max_jobs = settings.worker_max_jobs
    job_timeout = settings.run_timeout_seconds + 30
