"""The agent loop, written by hand on the Gemini API (google-genai SDK).

Automatic function calling is turned off: we run the loop ourselves so we control
turn limits, token budgets, parallel tool calls, timeouts, retries, and how
failures are reported.
"""
import asyncio
import json
import logging
import random
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from google import genai
from google.genai import errors, types
from pydantic import ValidationError

from ..config import settings
from ..schemas import Itinerary
from .prompts import SYSTEM_PROMPT
from .tools import HANDLERS, SUBMIT_TOOL, TOOL_DEFINITIONS, ToolError

log = logging.getLogger("agent")

Emit = Callable[[str, dict], Awaitable[None]]

client = genai.Client(api_key=settings.gemini_api_key or None)

GEMINI_TOOLS = [types.Tool(function_declarations=[
    types.FunctionDeclaration(name=t["name"], description=t["description"],
                              parameters_json_schema=t["input_schema"])
    for t in TOOL_DEFINITIONS
])]

GENERATE_CONFIG = types.GenerateContentConfig(
    system_instruction=SYSTEM_PROMPT,
    tools=GEMINI_TOOLS,
    max_output_tokens=settings.max_output_tokens,
    automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
)

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class AgentError(Exception):
    """A failure we can explain to the user."""


@dataclass
class Usage:
    turns: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    tool_calls: dict[str, int] = field(default_factory=dict)

    def add(self, meta: Any) -> None:
        if meta is None:
            return
        prompt = meta.prompt_token_count or 0
        cached = getattr(meta, "cached_content_token_count", 0) or 0
        # prompt_token_count includes cached tokens; thinking tokens are billed as output.
        self.input_tokens += prompt - cached
        self.cache_read_tokens += cached
        self.output_tokens += (meta.candidates_token_count or 0) + (getattr(meta, "thoughts_token_count", 0) or 0)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens + self.cache_read_tokens

    @property
    def cost_usd(self) -> float:
        p_in = settings.price_input_per_mtok / 1_000_000
        p_out = settings.price_output_per_mtok / 1_000_000
        return round(
            self.input_tokens * p_in
            + self.cache_read_tokens * p_in * settings.cache_read_multiplier
            + self.output_tokens * p_out,
            6,
        )

    def summary(self) -> dict:
        return {"turns": self.turns, "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens, "cache_read_tokens": self.cache_read_tokens,
                "cost_usd": self.cost_usd, "tool_calls": self.tool_calls}


def _preview(value: Any, limit: int = 400) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return text if len(text) <= limit else text[:limit] + "…"


async def _generate(contents: list[types.Content]) -> Any:
    """One model call, retried with exponential backoff and jitter on 429/5xx."""
    for attempt in range(settings.api_max_retries + 1):
        try:
            return await client.aio.models.generate_content(
                model=settings.planner_model, contents=contents, config=GENERATE_CONFIG)
        except errors.APIError as e:
            if e.code not in RETRYABLE_STATUS or attempt == settings.api_max_retries:
                raise
            delay = min(30.0, 2 ** attempt) + random.uniform(0, 1)
            log.warning("gemini %s, retrying in %.1fs (attempt %d)", e.code, delay, attempt + 1)
            await asyncio.sleep(delay)
    raise AssertionError("unreachable")


async def _run_tool(name: str, args: dict) -> tuple[bool, Any]:
    handler = HANDLERS.get(name)
    if handler is None:
        return False, f"Unknown tool '{name}'."
    try:
        return True, await asyncio.wait_for(handler(**args), timeout=settings.tool_timeout_seconds)
    except ToolError as e:
        return False, str(e)
    except asyncio.TimeoutError:
        return False, f"{name} timed out. Continue without it and note the gap."
    except TypeError as e:
        return False, f"Bad arguments for {name}: {e}"
    except Exception:
        log.exception("tool %s crashed", name)
        return False, f"{name} is unavailable right now. Continue without it and note the gap."


def _check_submission(args: dict) -> tuple[Itinerary | None, str]:
    try:
        return Itinerary.model_validate(args), "Itinerary accepted."
    except ValidationError as e:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or 'itinerary'}: {err['msg']}" for err in e.errors()
        )
        return None, f"Itinerary rejected. Fix these and call {SUBMIT_TOOL} again: {problems}"


def _finish_reason(candidate: Any) -> str:
    reason = getattr(candidate, "finish_reason", None)
    return getattr(reason, "name", str(reason))


async def run_agent_loop(prompt: str, emit: Emit, usage: Usage) -> Itinerary:
    contents: list[types.Content] = [types.Content(role="user", parts=[types.Part(text=prompt)])]

    for turn in range(1, settings.max_turns + 1):
        if usage.total_tokens > settings.run_token_budget:
            raise AgentError("This plan hit its token budget before finishing. Try a shorter trip or simpler request.")

        usage.turns = turn
        await emit("turn", {"turn": turn})
        response = await _generate(contents)
        usage.add(response.usage_metadata)

        candidate = (response.candidates or [None])[0]
        if candidate is None or candidate.content is None or not candidate.content.parts:
            raise AgentError(f"The model returned no answer (reason: {_finish_reason(candidate)}). Try rephrasing.")

        # Append the model turn exactly as returned: Gemini 3 models attach thought
        # signatures to parts, and they must be sent back unchanged.
        contents.append(candidate.content)

        calls = []
        for part in candidate.content.parts:
            if part.text and part.text.strip() and not getattr(part, "thought", False):
                await emit("thought", {"text": part.text.strip()})
            if part.function_call:
                calls.append(part.function_call)

        if not calls:
            nudge = ("You ran out of output space. Continue, more concisely."
                     if _finish_reason(candidate) == "MAX_TOKENS"
                     else f"Finish by calling {SUBMIT_TOOL} with the final plan.")
            contents.append(types.Content(role="user", parts=[types.Part(text=nudge)]))
            continue

        for i, call in enumerate(calls):
            usage.tool_calls[call.name] = usage.tool_calls.get(call.name, 0) + 1
            await emit("tool_call", {"id": str(i), "name": call.name,
                                     "input": None if call.name == SUBMIT_TOOL else dict(call.args or {})})

        # Independent tools run in parallel; the submission is checked locally.
        regular = [(i, c) for i, c in enumerate(calls) if c.name != SUBMIT_TOOL]
        outcomes = await asyncio.gather(*(_run_tool(c.name, dict(c.args or {})) for _, c in regular))
        results: dict[int, tuple[bool, Any]] = {i: o for (i, _), o in zip(regular, outcomes)}

        final: Itinerary | None = None
        for i, call in enumerate(calls):
            if call.name == SUBMIT_TOOL:
                itinerary, message = _check_submission(dict(call.args or {}))
                results[i] = (itinerary is not None, message)
                if itinerary is not None:
                    final = itinerary
                else:
                    await emit("validation_error", {"message": message})

        # One function_response part per function_call, in the same order.
        response_parts = []
        for i, call in enumerate(calls):
            ok, content = results[i]
            await emit("tool_result", {"id": str(i), "name": call.name, "ok": ok, "preview": _preview(content)})
            payload = {"result": content} if ok else {"error": content}
            response_parts.append(types.Part.from_function_response(name=call.name, response=payload))

        if final is not None:
            return final
        contents.append(types.Content(role="user", parts=response_parts))

    raise AgentError(f"The agent didn't finish within {settings.max_turns} steps. Try a simpler request.")
