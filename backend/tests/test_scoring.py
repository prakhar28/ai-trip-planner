from evals.scoring import percentile, score_case, summarize

EXPECT = {"days": 2, "currency": "USD", "budget": 700}
PLAN = {"days": [{}, {}], "currency": "USD", "total_budget": 700, "estimated_total": 650}
USAGE = {"tool_calls": {"get_weather": 1, "calculate_budget": 1, "submit_itinerary": 1}, "turns": 3, "cost_usd": 0.01}


def test_perfect_case_passes():
    s = score_case(EXPECT, PLAN, USAGE, None)
    assert s["passed"] and s["first_try_valid"]


def test_skipping_budget_tool_fails_even_with_valid_plan():
    usage = {"tool_calls": {"get_weather": 1, "submit_itinerary": 1}}
    s = score_case(EXPECT, PLAN, usage, None)
    assert not s["passed"] and s["checks"]["used_budget_tool"] is False


def test_failed_run_scores_zero():
    s = score_case(EXPECT, None, {"tool_calls": {}}, "AgentError: ran out of turns")
    assert not s["checks"]["completed"] and not s["passed"]


def test_summary_metrics():
    ok = {"latency_s": 10, "usage": USAGE, "score": score_case(EXPECT, PLAN, USAGE, None)}
    bad = {"latency_s": 30, "usage": USAGE, "score": score_case(EXPECT, None, USAGE, "boom")}
    summary = summarize([ok, ok, ok, bad])
    assert summary["pass_rate"] == 0.75
    assert summary["latency_p95_s"] == 30
    assert percentile([1, 2, 3, 4], 50) == 2
