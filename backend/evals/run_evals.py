"""Run the golden set against the real agent and gate on the pass rate.

    python -m evals.run_evals --min-pass-rate 0.85 --concurrency 4

Needs GEMINI_API_KEY and Redis (for the tool cache). Writes a JSON report to
evals/reports/ so results can be compared across prompt or model changes.
"""
import argparse
import asyncio
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from app.agent.loop import Usage, run_agent_loop
from app.config import settings
from evals.scoring import score_case, summarize

HERE = Path(__file__).parent


async def run_case(case: dict, sem: asyncio.Semaphore) -> dict:
    async with sem:
        usage, itinerary, error = Usage(), None, None

        async def emit(_type: str, _data: dict) -> None:
            pass

        started = time.perf_counter()
        try:
            result = await asyncio.wait_for(run_agent_loop(case["prompt"], emit, usage),
                                            timeout=settings.run_timeout_seconds)
            itinerary = result.model_dump()
        except Exception as e:  # noqa: BLE001 - a failed case is data, not a crash
            error = f"{type(e).__name__}: {e}"
        latency = time.perf_counter() - started

        summary = usage.summary()
        score = score_case(case["expect"], itinerary, summary, error)
        mark = "PASS" if score["passed"] else "FAIL"
        failed = [k for k, v in score["checks"].items() if not v]
        print(f"{mark}  {case['id']:<22} {latency:6.1f}s  ${summary['cost_usd']:.4f}  {', '.join(failed)}")
        return {"id": case["id"], "latency_s": round(latency, 2), "usage": summary,
                "score": score, "error": error}


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--min-pass-rate", type=float, default=0.85)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--only", help="Run a single case id")
    args = parser.parse_args()

    cases = json.loads((HERE / "cases.json").read_text())
    if args.only:
        cases = [c for c in cases if c["id"] == args.only]

    sem = asyncio.Semaphore(args.concurrency)
    results = await asyncio.gather(*(run_case(c, sem) for c in cases))
    summary = summarize(results)

    report_dir = HERE / "reports"
    report_dir.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report = {"model": settings.planner_model, "created_at": stamp, "summary": summary, "results": results}
    path = report_dir / f"{stamp}.json"
    path.write_text(json.dumps(report, indent=2))

    print("\n" + json.dumps(summary, indent=2))
    print(f"Report: {path}")
    return 0 if summary["pass_rate"] >= args.min_pass_rate else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
