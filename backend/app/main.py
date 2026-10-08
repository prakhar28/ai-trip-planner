import json
from contextlib import asynccontextmanager

from arq import create_pool
from arq.connections import RedisSettings
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from . import events
from .config import settings
from .db import TERMINAL_STATUSES, Run, SessionLocal, init_db
from .schemas import RunCreate, RunOut


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    app.state.queue = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    yield
    await app.state.queue.close()


app = FastAPI(title="Trip Agent", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins.split(","),
                   allow_methods=["*"], allow_headers=["*"])


@app.get("/healthz")
async def healthz():
    async with SessionLocal() as s:
        await s.execute(text("select 1"))
    await events.redis.ping()
    return {"ok": True}


@app.post("/runs", response_model=RunOut, status_code=202)
async def create_run(body: RunCreate, idempotency_key: str | None = Header(default=None, max_length=128)):
    async with SessionLocal() as s:
        if idempotency_key:
            existing = await s.scalar(select(Run).where(Run.idempotency_key == idempotency_key))
            if existing:
                return existing
        run = Run(prompt=body.prompt, idempotency_key=idempotency_key)
        s.add(run)
        try:
            await s.commit()
        except IntegrityError:  # two identical requests raced; return the winner
            await s.rollback()
            return await s.scalar(select(Run).where(Run.idempotency_key == idempotency_key))

    # Known gap: if the process dies between commit and enqueue, the run stays "queued".
    # Fix later with a transactional outbox or a reaper that re-enqueues stale runs.
    await events.publish(run.id, "status", {"status": "queued"})
    await app.state.queue.enqueue_job("run_agent", run.id, _job_id=run.id)
    return run


@app.get("/runs/{run_id}", response_model=RunOut)
async def get_run(run_id: str):
    async with SessionLocal() as s:
        run = await s.get(Run, run_id)
    if run is None:
        raise HTTPException(404, "Run not found")
    return run


def _sse(event_type: str, data: str, event_id: str | None = None) -> str:
    head = f"id: {event_id}\n" if event_id else ""
    return f"{head}event: {event_type}\ndata: {data}\n\n"


@app.get("/runs/{run_id}/stream")
async def stream_run(run_id: str, request: Request,
                     last_event_id: str | None = Header(default=None)):
    async with SessionLocal() as s:
        run = await s.get(Run, run_id)
    if run is None:
        raise HTTPException(404, "Run not found")

    async def generate():
        # Finished long ago and the event stream expired: answer from the database.
        if run.status in TERMINAL_STATUSES and not await events.exists(run_id):
            if run.status == "completed":
                yield _sse("done", json.dumps({"itinerary": run.result}))
            else:
                yield _sse("error", json.dumps({"message": run.error}))
            return

        cursor = last_event_id or "0"  # EventSource resends Last-Event-ID on reconnect
        while not await request.is_disconnected():
            entries = await events.read(run_id, cursor)
            if not entries:
                yield ": keepalive\n\n"
                continue
            for entry_id, fields in entries:
                cursor = entry_id
                yield _sse(fields["type"], fields["data"], entry_id)
                if fields["type"] in events.FINAL_EVENT_TYPES:
                    return

    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
