"""Tools the agent can call.

Each external call is: cached in Redis, bounded by a timeout, and turned into a
clean error message the model can recover from (never a stack trace).
"""
import hashlib
import json
from typing import Any, Awaitable, Callable

import httpx

from ..config import settings
from ..events import redis
from ..schemas import Itinerary

HTTP = httpx.AsyncClient(timeout=10.0, follow_redirects=True,
                         headers={"User-Agent": "trip-agent/0.1"})


class ToolError(Exception):
    """An expected failure. The message goes back to the model."""


async def cached(namespace: str, params: dict, fetch: Callable[[], Awaitable[Any]]) -> Any:
    raw = json.dumps(params, sort_keys=True).lower()
    key = f"toolcache:{namespace}:{hashlib.sha256(raw.encode()).hexdigest()[:24]}"
    hit = await redis.get(key)
    if hit is not None:
        return json.loads(hit)
    value = await fetch()
    await redis.set(key, json.dumps(value), ex=settings.tool_cache_ttl_seconds)
    return value


# ---------------- get_weather (Open-Meteo, no API key) ----------------

async def get_weather(city: str) -> dict:
    async def fetch() -> dict:
        geo = await HTTP.get("https://geocoding-api.open-meteo.com/v1/search",
                             params={"name": city, "count": 1})
        geo.raise_for_status()
        results = geo.json().get("results") or []
        if not results:
            raise ToolError(f"Could not find a place called '{city}'. Try the city name only.")
        place = results[0]
        fc = await HTTP.get("https://api.open-meteo.com/v1/forecast", params={
            "latitude": place["latitude"], "longitude": place["longitude"],
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max",
            "timezone": "auto", "forecast_days": 16,
        })
        fc.raise_for_status()
        daily = fc.json()["daily"]
        return {
            "place": f"{place['name']}, {place.get('country', '')}".strip(", "),
            "note": "Forecast covers only the next 16 days.",
            "days": [
                {"date": d, "max_c": hi, "min_c": lo, "rain_chance_pct": rain}
                for d, hi, lo, rain in zip(daily["time"], daily["temperature_2m_max"],
                                           daily["temperature_2m_min"],
                                           daily["precipitation_probability_max"])
            ],
        }
    return await cached("weather", {"city": city}, fetch)


# ---------------- convert_currency (Frankfurter, no API key) ----------------

async def convert_currency(amount: float, from_currency: str, to_currency: str) -> dict:
    src, dst = from_currency.upper(), to_currency.upper()
    if src == dst:
        return {"amount": amount, "rate": 1.0, "converted": amount, "currency": dst}

    async def fetch() -> dict:
        r = await HTTP.get("https://api.frankfurter.app/latest", params={"from": src, "to": dst})
        if r.status_code == 404:
            raise ToolError(f"Unsupported currency pair {src}->{dst}.")
        r.raise_for_status()
        return {"rate": r.json()["rates"][dst], "date": r.json().get("date")}

    rate = await cached("fx", {"from": src, "to": dst}, fetch)
    return {"amount": amount, "from": src, "to": dst, "rate": rate["rate"],
            "rate_date": rate.get("date"), "converted": round(amount * rate["rate"], 2)}


# ---------------- calculate_budget (pure Python, no LLM math) ----------------

async def calculate_budget(total_budget: float, items: list[dict]) -> dict:
    if total_budget <= 0:
        raise ToolError("total_budget must be greater than 0.")
    if not items:
        raise ToolError("items is empty. Send every cost line item.")
    by_category: dict[str, float] = {}
    for item in items:
        amount = float(item.get("amount", 0))
        if amount < 0:
            raise ToolError(f"Negative amount in item {item}.")
        category = str(item.get("category", "other")).strip().lower() or "other"
        by_category[category] = round(by_category.get(category, 0.0) + amount, 2)
    total = round(sum(by_category.values()), 2)
    return {
        "by_category": by_category,
        "estimated_total": total,
        "total_budget": total_budget,
        "remaining": round(total_budget - total, 2),
        "over_budget": total > total_budget,
    }


# ---------------- Tool registry ----------------

def _inline_refs(schema: dict) -> dict:
    """Pydantic emits $defs/$ref; inline them so the tool schema is self-contained."""
    defs = schema.pop("$defs", {})

    def resolve(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                return resolve(dict(defs[node["$ref"].split("/")[-1]]))
            return {k: resolve(v) for k, v in node.items()}
        if isinstance(node, list):
            return [resolve(x) for x in node]
        return node

    return resolve(schema)


SUBMIT_TOOL = "submit_itinerary"

TOOL_DEFINITIONS = [
    {
        "name": "get_weather",
        "description": "Get a daily weather forecast (next 16 days) for a city.",
        "input_schema": {
            "type": "object",
            "properties": {"city": {"type": "string", "description": "City name, e.g. 'Lisbon'"}},
            "required": ["city"],
        },
    },
    {
        "name": "convert_currency",
        "description": "Convert an amount between two ISO 4217 currencies using the latest rate.",
        "input_schema": {
            "type": "object",
            "properties": {
                "amount": {"type": "number"},
                "from_currency": {"type": "string"},
                "to_currency": {"type": "string"},
            },
            "required": ["amount", "from_currency", "to_currency"],
        },
    },
    {
        "name": "calculate_budget",
        "description": "Sum cost line items by category and compare with the budget. "
                       "Always use this instead of doing arithmetic yourself.",
        "input_schema": {
            "type": "object",
            "properties": {
                "total_budget": {"type": "number"},
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "category": {"type": "string", "description": "lodging, food, activities, transport, other"},
                            "description": {"type": "string"},
                            "amount": {"type": "number"},
                        },
                        "required": ["category", "amount"],
                    },
                },
            },
            "required": ["total_budget", "items"],
        },
    },
    {
        "name": SUBMIT_TOOL,
        "description": "Submit the final itinerary. Call exactly once when the plan is complete.",
        "input_schema": _inline_refs(Itinerary.model_json_schema()),
    },
]

HANDLERS: dict[str, Callable[..., Awaitable[Any]]] = {
    "get_weather": get_weather,
    "convert_currency": convert_currency,
    "calculate_budget": calculate_budget,
}
