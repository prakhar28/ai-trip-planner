"""Fast tests that need no API key, Postgres, or Redis."""
import json
from types import SimpleNamespace

import pytest
from google.genai import types

from app.agent import loop
from app.agent.tools import TOOL_DEFINITIONS, calculate_budget, ToolError
from app.schemas import Itinerary


def valid_plan(**overrides) -> dict:
    plan = {
        "destination": "Lisbon, Portugal", "currency": "USD", "total_budget": 1200,
        "estimated_total": 900, "budget_breakdown": {"lodging": 500, "food": 250, "activities": 150},
        "weather_note": "Mild, some rain.",
        "days": [{"day": 1, "title": "Alfama", "stops": [
            {"name": "São Jorge Castle", "description": "Views over the city", "estimated_cost": 18}]}],
    }
    plan.update(overrides)
    return plan


# ---------- deterministic tools ----------

async def test_calculate_budget_sums_by_category():
    out = await calculate_budget(1000, [
        {"category": "Food", "amount": 40.5}, {"category": "food", "amount": 10},
        {"category": "lodging", "amount": 600},
    ])
    assert out["by_category"] == {"food": 50.5, "lodging": 600}
    assert out["estimated_total"] == 650.5
    assert out["remaining"] == 349.5
    assert out["over_budget"] is False


async def test_calculate_budget_rejects_bad_input():
    with pytest.raises(ToolError):
        await calculate_budget(1000, [])
    with pytest.raises(ToolError):
        await calculate_budget(1000, [{"category": "food", "amount": -5}])


# ---------- output validation ----------

def test_valid_itinerary_passes():
    Itinerary.model_validate(valid_plan())


@pytest.mark.parametrize("overrides, fragment", [
    ({"estimated_total": 1500, "budget_breakdown": {"lodging": 1500}}, "over the"),
    ({"estimated_total": 700}, "sums to"),
    ({"currency": "dollars"}, "pattern"),
])
def test_invalid_itineraries_explain_the_problem(overrides, fragment):
    _, message = loop._check_submission(valid_plan(**overrides))
    assert "rejected" in message and fragment in message


def test_tool_schemas_are_self_contained():
    assert "$ref" not in json.dumps(TOOL_DEFINITIONS)


# ---------- the loop, with a scripted fake Gemini ----------

def fake_response(*parts, finish="STOP"):
    content = types.Content(role="model", parts=list(parts))
    return SimpleNamespace(
        candidates=[SimpleNamespace(content=content, finish_reason=SimpleNamespace(name=finish))],
        usage_metadata=SimpleNamespace(prompt_token_count=1000, cached_content_token_count=200,
                                       candidates_token_count=150, thoughts_token_count=50),
    )


def call(name, args):
    return types.Part(function_call=types.FunctionCall(name=name, args=args))


async def test_loop_recovers_from_rejected_submission(monkeypatch):
    over_budget = valid_plan(estimated_total=1500, budget_breakdown={"lodging": 1500})
    script = iter([
        fake_response(types.Part(text="Checking the budget."),
                      call("calculate_budget", {"total_budget": 1200, "items": [{"category": "food", "amount": 900}]})),
        fake_response(call("submit_itinerary", over_budget)),
        fake_response(call("submit_itinerary", valid_plan())),
    ])
    sent = []

    async def fake_generate(contents):
        sent.append(list(contents))  # snapshot per call
        return next(script)

    monkeypatch.setattr(loop, "_generate", fake_generate)
    events = []

    async def emit(kind, data):
        events.append(kind)

    usage = loop.Usage()
    itinerary = await loop.run_agent_loop("4 days in Lisbon, $1200", emit, usage)

    assert itinerary.destination == "Lisbon, Portugal"
    assert usage.turns == 3
    assert usage.tool_calls == {"calculate_budget": 1, "submit_itinerary": 2}
    assert usage.input_tokens == 3 * 800 and usage.cache_read_tokens == 3 * 200
    assert usage.output_tokens == 3 * 200
    assert "validation_error" in events and "thought" in events

    # Turn 1's tool result went back as a structured function_response.
    first_result = sent[1][-1].parts[0].function_response
    assert first_result.name == "calculate_budget"
    assert first_result.response["result"]["estimated_total"] == 900
    # The rejection reason was sent back to the model as an error.
    rejection = sent[2][-1].parts[0].function_response.response
    assert "error" in rejection and "over the" in rejection["error"]


async def test_loop_stops_at_max_turns(monkeypatch):
    async def never_finishes(contents):
        return fake_response(types.Part(text="Thinking..."))

    monkeypatch.setattr(loop, "_generate", never_finishes)
    monkeypatch.setattr(loop.settings, "max_turns", 3)

    async def emit(kind, data):
        pass

    with pytest.raises(loop.AgentError, match="3 steps"):
        await loop.run_agent_loop("trip please", emit, loop.Usage())


async def test_blocked_response_fails_cleanly(monkeypatch):
    async def blocked(contents):
        return SimpleNamespace(candidates=[SimpleNamespace(content=None, finish_reason=SimpleNamespace(name="SAFETY"))],
                               usage_metadata=None)

    monkeypatch.setattr(loop, "_generate", blocked)

    async def emit(kind, data):
        pass

    with pytest.raises(loop.AgentError, match="SAFETY"):
        await loop.run_agent_loop("trip please", emit, loop.Usage())


async def test_retries_on_rate_limit(monkeypatch):
    from google.genai import errors
    attempts = {"n": 0}

    class FakeModels:
        async def generate_content(self, **kwargs):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise errors.ClientError(429, {"error": {"message": "rate limited", "status": "RESOURCE_EXHAUSTED"}})
            return "ok"

    monkeypatch.setattr(loop, "client", SimpleNamespace(aio=SimpleNamespace(models=FakeModels())))

    async def no_sleep(_):
        pass

    monkeypatch.setattr(loop.asyncio, "sleep", no_sleep)
    assert await loop._generate([]) == "ok" and attempts["n"] == 3
