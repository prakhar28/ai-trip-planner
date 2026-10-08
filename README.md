# Trip Agent

A production-style AI agent that plans trips that actually fit a budget. You describe the trip;
the agent checks the weather, converts currency, does the budget maths with real code (not the
model), and returns a day-by-day itinerary that must pass schema and budget validation before
the user ever sees it.

The trip planner is the vehicle. The point of this repo is the engineering around the agent:
queueing, streaming, guardrails, cost tracking, and an eval suite that gates changes.

## Architecture

```
React (Vite) ──POST /runs──▶ FastAPI ──enqueue──▶ Redis queue ──▶ arq workers (scale horizontally)
     ▲                         │                                       │
     │                         │                                       ├─ agent loop (Gemini API)  
     └──── SSE /runs/{id}/stream ◀── Redis Streams (replayable events) ◀┤   ├─ get_weather (Open-Meteo, cached)
                               │                                       │   ├─ convert_currency (Frankfurter, cached)
                               └──── Postgres (runs, usage, cost) ◀────┘   ├─ calculate_budget (pure Python)
                                                                            └─ submit_itinerary (Pydantic-validated)
```

## What makes it production-grade

| Concern | How it's handled |
| --- | --- |
| Long-running work | `POST /runs` returns immediately; agents run in a worker pool, never in the request |
| Duplicate submits | `Idempotency-Key` header + unique DB constraint; queue job id = run id |
| Live progress | SSE backed by Redis Streams, so late or reconnecting clients replay missed events via `Last-Event-ID` |
| Runaway runs | Turn limit, per-run token budget, per-tool timeout, whole-run timeout |
| Flaky APIs | Exponential backoff with jitter on 429/5xx; tool failures become recoverable messages to the model |
| Bad output | Final answer is a tool call validated by Pydantic: day numbering, breakdown sums, and the budget cap. Rejections go back to the model to fix |
| LLM arithmetic | The model never adds numbers; `calculate_budget` does |
| Cost | Input, output (including thinking), and cached tokens plus USD cost recorded per run |
| Latency | Independent tool calls run in parallel; tool results cached in Redis |
| Regressions | Golden-set eval suite with a pass-rate gate (see below) |

## Run it

```bash
cp .env.example .env            # add GEMINI_API_KEY and model prices
docker compose up --build       # api on :8000, worker, postgres, redis
docker compose up --scale worker=3   # more workers

cd frontend && npm install && npm run dev   # http://localhost:5173
```

## Tests and evals

```bash
cd backend
pip install -r requirements-dev.txt
GEMINI_API_KEY=test pytest -q                # fast, no real key: tools, validation, loop with a scripted fake model
python -m evals.run_evals --min-pass-rate 0.85   # real model, needs GEMINI_API_KEY + Redis
```

The eval suite (`backend/evals/cases.json`) covers normal trips, a non-USD budget, a very tight
budget, a vague request, and a prompt-injection attempt. Each case is scored on: completed,
correct day count, correct currency, budget matches the request, stays within budget, used the
weather tool, used the budget tool. It also reports first-try-valid rate, p50/p95 latency,
average cost, and average turns, and writes a JSON report so runs can be compared.

## Results

_Fill this in from your eval reports. This table is what reviewers look at first._

| Metric | Value |
| --- | --- |
| Eval pass rate | – |
| First-try valid rate | – |
| p50 / p95 latency | – |
| Avg cost per plan | – |
| Load test (concurrent runs, p95) | – |

## Roadmap

- [x] v1: queue + workers, SSE streaming, guardrails, validated output, cost tracking
- [x] Eval harness v0 with deterministic checks and a CI gate
- [ ] LLM-as-judge eval for plan quality (realism, interest match), calibrated against hand labels
- [ ] MCP server exposing the trip tools, usable from Claude Desktop
- [ ] Observability: OpenTelemetry traces per run and tool call
- [ ] Eval + run dashboard in React: traces, cost, latency, and pass rate across prompt versions
- [ ] Provider layer: run the same evals on Claude vs Gemini and publish the comparison
- [ ] Web search tool, model routing (cheap model for summarising), per-user rate limiting
- [ ] Load test (k6) with published numbers
- [ ] Known gaps: transactional outbox for enqueue, reaper for runs stuck in `running`, auth, Alembic migrations
