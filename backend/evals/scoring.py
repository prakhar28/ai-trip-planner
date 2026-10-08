"""Pure scoring logic, kept separate so it can be unit-tested without an API key."""
from statistics import median


def score_case(expect: dict, itinerary: dict | None, usage: dict, error: str | None) -> dict:
    tools = usage.get("tool_calls", {})
    checks = {
        "completed": itinerary is not None and error is None,
        "day_count": bool(itinerary) and len(itinerary["days"]) == expect["days"],
        "currency": bool(itinerary) and itinerary["currency"] == expect["currency"],
        "budget_matches_request": bool(itinerary) and abs(itinerary["total_budget"] - expect["budget"]) < 1,
        "within_budget": bool(itinerary) and itinerary["estimated_total"] <= expect["budget"],
        "used_weather_tool": tools.get("get_weather", 0) > 0,
        "used_budget_tool": tools.get("calculate_budget", 0) > 0,
    }
    return {
        "checks": checks,
        "passed": all(checks.values()),
        "first_try_valid": tools.get("submit_itinerary", 0) == 1 and checks["completed"],
    }


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(pct / 100 * len(ordered)) - 1))
    return ordered[index]


def summarize(results: list[dict]) -> dict:
    n = len(results) or 1
    latencies = [r["latency_s"] for r in results]
    check_names = results[0]["score"]["checks"].keys() if results else []
    return {
        "cases": len(results),
        "pass_rate": round(sum(r["score"]["passed"] for r in results) / n, 3),
        "first_try_valid_rate": round(sum(r["score"]["first_try_valid"] for r in results) / n, 3),
        "per_check_pass_rate": {
            name: round(sum(r["score"]["checks"][name] for r in results) / n, 3) for name in check_names
        },
        "latency_p50_s": round(median(latencies), 2) if latencies else 0,
        "latency_p95_s": round(percentile(latencies, 95), 2),
        "avg_cost_usd": round(sum(r["usage"].get("cost_usd", 0) for r in results) / n, 5),
        "avg_turns": round(sum(r["usage"].get("turns", 0) for r in results) / n, 2),
    }
