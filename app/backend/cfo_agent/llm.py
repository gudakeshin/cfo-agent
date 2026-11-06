"""Anthropic wrapper: structured parse, caching, retries, cost log."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, TypeVar

from anthropic import Anthropic
from pydantic import BaseModel

from cfo_agent.pii import deep_apply
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


class BudgetExhausted(RuntimeError):
    """Raised before a call when the per-run call or cost budget is used up."""


@dataclass
class LlmBudget:
    max_calls: int = 8
    max_cost_usd: float | None = None
    calls: int = 0
    logs: list[CallLog] = field(default_factory=list)

    def remaining(self) -> int:
        if self.max_cost_usd is not None and self.total_cost() >= self.max_cost_usd:
            return 0
        return max(0, self.max_calls - self.calls)

    def check(self) -> None:
        if self.remaining() <= 0:
            raise BudgetExhausted(
                f"LLM budget exhausted (calls {self.calls}/{self.max_calls}, "
                f"cost ${self.total_cost():.2f}"
                + (f"/${self.max_cost_usd:.2f})" if self.max_cost_usd is not None else ")")
            )

    def total_cost(self) -> float:
        return sum(c.cost_usd for c in self.logs)

    def as_dict(self) -> dict:
        return {
            "calls": self.calls,
            "cost_usd": round(self.total_cost(), 4),
            "logs": [c.__dict__ for c in self.logs],
        }


def budget_for_items(max_items: int) -> LlmBudget:
    """Per-run budget sized to the work: connect + (synth + repair) per item + headline."""
    settings = get_settings()
    calls = settings.max_llm_calls_per_run or (1 + 2 * max(0, max_items) + 1)
    return LlmBudget(max_calls=calls, max_cost_usd=settings.max_llm_cost_usd_per_run)


def _pii_map():
    from cfo_agent.pii import current_map

    return current_map()


def _mask_messages(messages: list[dict[str, Any]], amap) -> list[dict[str, Any]]:
    """Mask every string in a message list (text, tool inputs, tool results)."""
    if amap is None:
        return messages
    out = []
    for m in messages:
        content = m["content"]
        if not isinstance(content, (str, list, dict)):
            content = [b for b in (_block_to_dict(x) for x in content) if b]
        elif isinstance(content, list):
            content = [(_block_to_dict(x) or x) if not isinstance(x, dict) else x for x in content]
        out.append({**m, "content": deep_apply(content, amap.mask)})
    return out


def structured_output_mode(provider: str) -> str:
    """'native' (Anthropic structured outputs) or 'tool' (forced tool call — works on
    every platform, including Bedrock and Vertex). STRUCTURED_OUTPUT_MODE overrides."""
    forced = get_settings().structured_output_mode
    if forced in ("native", "tool"):
        return forced
    return "native" if provider == "anthropic" else "tool"


_RESPOND_TOOL = "record_answer"


def _parse_via_tool(client, kwargs: dict[str, Any], schema: type[BaseModel]):
    """Structured output through a single forced tool call whose input schema is the
    Pydantic model; the tool input is validated into `schema`."""
    tool = {
        "name": _RESPOND_TOOL,
        "description": "Record the final answer. Always call this exactly once.",
        "input_schema": schema.model_json_schema(),
    }
    response = client.messages.create(
        **kwargs,
        tools=[tool],
        tool_choice={"type": "tool", "name": _RESPOND_TOOL},
    )
    block = next(
        (b for b in response.content if getattr(b, "type", None) == "tool_use" and b.name == _RESPOND_TOOL),
        None,
    )
    if block is None:
        raise RuntimeError("Model did not return the structured answer")
    data = block.input if isinstance(block.input, dict) else json.loads(block.input)
    response.parsed_output = schema.model_validate(data)
    return response


def make_client(provider: str | None = None):
    """SDK client for a provider. Bedrock and Vertex use the platform's own
    credentials (IAM role / service account), never an Anthropic key."""
    settings = get_settings()
    provider = provider or settings.llm_provider
    common = {"timeout": settings.llm_timeout_s, "max_retries": settings.llm_max_retries}
    if provider == "anthropic":
        return Anthropic(api_key=settings.anthropic_api_key, **common)
    if provider == "bedrock":
        from anthropic import AnthropicBedrock

        kw = {"aws_region": settings.aws_region}
        if settings.aws_profile:
            kw["aws_profile"] = settings.aws_profile
        return AnthropicBedrock(**kw, **common)
    if provider == "vertex":
        from anthropic import AnthropicVertex

        return AnthropicVertex(region=settings.vertex_region, project_id=settings.vertex_project_id, **common)
    raise ValueError(f"Unknown LLM_PROVIDER {provider!r}")


def _client(provider: str | None = None):
    # SDK retries 408/409/429/5xx and connection errors with exponential backoff.
    return make_client(provider)


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
    provider: str | None = None,
) -> T:
    """Structured output via messages.parse() — real Claude API call."""
    settings = get_settings()
    if budget is not None:
        budget.check()

    model_id = model or settings.anthropic_model
    client = _client(provider)
    amap = _pii_map()
    if amap is not None:
        system = deep_apply(system, amap.mask)
        user = amap.mask(user)
    kwargs: dict[str, Any] = {
        "model": model_id,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": user}],
    }
    if isinstance(system, list):
        kwargs["system"] = system
    else:
        kwargs["system"] = system

    mode = structured_output_mode(provider or settings.llm_provider)
    if thinking and mode == "native":
        # Forced tool choice (the portable mode) cannot be combined with thinking.
        kwargs["thinking"] = {"type": "adaptive"}

    t0 = time.time()
    if mode == "native":
        # Anthropic API: native structured outputs
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
    else:
        response = _parse_via_tool(client, kwargs, schema)
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
    if amap is not None:
        parsed = type(parsed).model_validate(deep_apply(parsed.model_dump(), amap.unmask))
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
        amap = _pii_map()
        kwargs: dict[str, Any] = {
            "model": settings.anthropic_model,
            "max_tokens": max_tokens,
            "messages": _mask_messages(msgs, amap),
            "tools": tools,
            "system": deep_apply(system, amap.mask) if amap else system,
        }

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

        # Collect assistant content (unmasked back to real names for local use)
        assistant_content = [b for b in (_block_to_dict(x) for x in response.content) if b]
        if amap is not None:
            assistant_content = deep_apply(assistant_content, amap.unmask)
        msgs.append({"role": "assistant", "content": assistant_content})

        tool_uses = [b for b in assistant_content if b["type"] == "tool_use"]
        text_parts = [b["text"] for b in assistant_content if b["type"] == "text"]
        if text_parts:
            final_text = "\n".join(text_parts)

        if response.stop_reason == "end_turn" or not tool_uses:
            break

        tool_results = []
        for tu in tool_uses:
            result = tool_handler(tu["name"], tu["input"])
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": tu["id"],
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


def _block_to_dict(block: Any) -> dict[str, Any] | None:
    """Normalise an SDK content block to the minimal dict the API accepts back."""
    btype = getattr(block, "type", None) if not isinstance(block, dict) else block.get("type")
    get = (lambda k: block.get(k)) if isinstance(block, dict) else (lambda k: getattr(block, k, None))
    if btype == "text":
        return {"type": "text", "text": get("text") or ""}
    if btype == "tool_use":
        return {"type": "tool_use", "id": get("id"), "name": get("name"), "input": get("input") or {}}
    return None


def stream_round(
    *,
    system: str | list[dict[str, Any]],
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    purpose: str,
    budget: LlmBudget | None = None,
    on_text=None,
    max_tokens: int = 2048,
) -> tuple[list[dict[str, Any]], str | None]:
    """One streamed model turn. Calls on_text(delta) as text arrives; returns the
    turn's content blocks (as dicts) and stop_reason."""
    settings = get_settings()
    if budget is not None:
        budget.check()
    client = _client()
    amap = _pii_map()
    unmasker = None
    if amap is not None:
        from cfo_agent.pii import StreamUnmasker

        unmasker = StreamUnmasker(amap)
        system = deep_apply(system, amap.mask)
        messages = _mask_messages(messages, amap)
    t0 = time.time()
    with client.messages.stream(
        model=settings.anthropic_model,
        max_tokens=max_tokens,
        system=system,
        messages=messages,
        tools=tools,
    ) as stream:
        for text in stream.text_stream:
            if on_text:
                out = unmasker.feed(text) if unmasker else text
                if out:
                    on_text(out)
        final = stream.get_final_message()
    if on_text and unmasker:
        tail = unmasker.flush()
        if tail:
            on_text(tail)
    duration = int((time.time() - t0) * 1000)
    inp, out, cr, cw, cost = _usage_cost(final.usage)
    if budget is not None:
        budget.calls += 1
        budget.logs.append(
            CallLog(
                purpose=f"{purpose}:stream",
                input_tokens=inp,
                output_tokens=out,
                cache_read_tokens=cr,
                cache_write_tokens=cw,
                cost_usd=cost,
                duration_ms=duration,
                model=settings.anthropic_model,
            )
        )
    logger.info("LLM %s (stream) cost=$%.4f tokens_in=%s out=%s", purpose, cost, inp, out)
    blocks = [b for b in (_block_to_dict(x) for x in final.content) if b]
    if amap is not None:
        blocks = deep_apply(blocks, amap.unmask)
    return blocks, final.stop_reason
