"""Anthropic wrapper: structured parse, caching, retries, cost log."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, TypeVar

from anthropic import Anthropic
from pydantic import BaseModel

from cfo_agent.settings import get_settings

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

# Approximate USD per MTok (Opus 4.5 / 4.6 ballpark; logged for demo audit)
INPUT_COST_PER_MTOK = 5.0
OUTPUT_COST_PER_MTOK = 25.0
CACHE_READ_COST_PER_MTOK = 0.5
CACHE_WRITE_COST_PER_MTOK = 6.25


@dataclass
class CallLog:
    purpose: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    duration_ms: int = 0
    model: str = ""


@dataclass
class LlmBudget:
    max_calls: int = 8
    calls: int = 0
    logs: list[CallLog] = field(default_factory=list)

    def remaining(self) -> int:
        return max(0, self.max_calls - self.calls)

    def total_cost(self) -> float:
        return sum(c.cost_usd for c in self.logs)

    def as_dict(self) -> dict:
        return {
            "calls": self.calls,
            "cost_usd": round(self.total_cost(), 4),
            "logs": [c.__dict__ for c in self.logs],
        }


def _client() -> Anthropic:
    return Anthropic(api_key=get_settings().anthropic_api_key)


def _usage_cost(usage: Any) -> tuple[int, int, int, int, float]:
    inp = int(getattr(usage, "input_tokens", 0) or 0)
    out = int(getattr(usage, "output_tokens", 0) or 0)
    cache_read = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
    cache_write = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
    cost = (
        (inp - cache_read) / 1e6 * INPUT_COST_PER_MTOK
        + out / 1e6 * OUTPUT_COST_PER_MTOK
        + cache_read / 1e6 * CACHE_READ_COST_PER_MTOK
        + cache_write / 1e6 * CACHE_WRITE_COST_PER_MTOK
    )
    return inp, out, cache_read, cache_write, cost


def parse(
    schema: type[T],
    *,
    system: str | list[dict[str, Any]],
    user: str,
    purpose: str,
    budget: LlmBudget | None = None,
    thinking: bool = False,
    max_tokens: int = 4096,
    model: str | None = None,
) -> T:
    """Structured output via messages.parse() — real Claude API call."""
    settings = get_settings()
    if budget and budget.remaining() <= 0:
        raise RuntimeError(f"LLM call budget exhausted ({budget.max_calls})")

    model_id = model or settings.anthropic_model
    client = _client()
    kwargs: dict[str, Any] = {
        "model": model_id,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": user}],
    }
    if isinstance(system, list):
        kwargs["system"] = system
    else:
        kwargs["system"] = system

    if thinking:
        kwargs["thinking"] = {"type": "adaptive"}

    t0 = time.time()
    # anthropic SDK: messages.parse with output_format
    try:
        response = client.messages.parse(
            **kwargs,
            output_format=schema,
        )
    except TypeError:
        # Fallback for SDK versions using different API
        response = client.beta.messages.parse(
            **kwargs,
            output_format=schema,
        )
    duration = int((time.time() - t0) * 1000)

    inp, out, cr, cw, cost = _usage_cost(response.usage)
    log = CallLog(
        purpose=purpose,
        input_tokens=inp,
        output_tokens=out,
        cache_read_tokens=cr,
        cache_write_tokens=cw,
        cost_usd=cost,
        duration_ms=duration,
        model=model_id,
    )
    if budget is not None:
        budget.calls += 1
        budget.logs.append(log)
    logger.info("LLM %s cost=$%.4f tokens_in=%s out=%s", purpose, cost, inp, out)

    parsed = getattr(response, "parsed_output", None)
    if parsed is None:
        parsed = getattr(response, "parsed", None)
    if parsed is None:
        raise RuntimeError(f"LLM parse returned no structured output for {purpose}")
    return parsed  # type: ignore[return-value]


def complete_with_tools(
    *,
    system: str | list[dict[str, Any]],
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    purpose: str,
    budget: LlmBudget | None = None,
    max_rounds: int | None = None,
    tool_handler,
    max_tokens: int = 4096,
) -> tuple[str, list[dict[str, Any]]]:
    """Tool-use loop. tool_handler(name, args) -> str."""
    settings = get_settings()
    max_rounds = max_rounds or settings.max_tool_rounds
    client = _client()
    msgs = list(messages)
    final_text = ""

    for _round in range(max_rounds):
        if budget and budget.remaining() <= 0:
            break
        kwargs: dict[str, Any] = {
            "model": settings.anthropic_model,
            "max_tokens": max_tokens,
            "messages": msgs,
            "tools": tools,
        }
        if isinstance(system, list):
            kwargs["system"] = system
        else:
            kwargs["system"] = system

        t0 = time.time()
        response = client.messages.create(**kwargs)
        duration = int((time.time() - t0) * 1000)
        inp, out, cr, cw, cost = _usage_cost(response.usage)
        if budget is not None:
            budget.calls += 1
            budget.logs.append(
                CallLog(
                    purpose=f"{purpose}:round",
                    input_tokens=inp,
                    output_tokens=out,
                    cache_read_tokens=cr,
                    cache_write_tokens=cw,
                    cost_usd=cost,
                    duration_ms=duration,
                    model=settings.anthropic_model,
                )
            )

        # Collect assistant content
        assistant_content = response.content
        msgs.append({"role": "assistant", "content": assistant_content})

        tool_uses = [b for b in assistant_content if b.type == "tool_use"]
        text_parts = [b.text for b in assistant_content if hasattr(b, "text") and b.type == "text"]
        if text_parts:
            final_text = "\n".join(text_parts)

        if response.stop_reason == "end_turn" or not tool_uses:
            break

        tool_results = []
        for tu in tool_uses:
            result = tool_handler(tu.name, tu.input)
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": tu.id,
                    "content": result if isinstance(result, str) else json.dumps(result),
                }
            )
        msgs.append({"role": "user", "content": tool_results})

    return final_text, msgs


def cached_system_block(text: str) -> dict[str, Any]:
    return {
        "type": "text",
        "text": text,
        "cache_control": {"type": "ephemeral"},
    }
