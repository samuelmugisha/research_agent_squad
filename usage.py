"""
Token usage and cost tracking for each research run.

UsageTracker is a LangChain callback handler. Passed in a run's config, it
sees every chat-model call made by every agent (including the parallel
Scouts, the safety guard and follow-up chat) and records tokens and cost per
step. Steps are named after the LangGraph node that made the call, or after
the "usage_label" metadata for calls made outside the graph.

Cost:
- With OpenRouter, each response includes the real amount billed ("cost"),
  so the tracker records actual spend.
- Otherwise cost is estimated from MODEL_PRICE_INPUT / MODEL_PRICE_OUTPUT
  (USD per million tokens; defaults are gpt-4o-mini's prices) and the
  summary is marked "estimated".

Web searches through OpenRouter's web plugin are billed per result and are
usually the largest cost. web_search.py reports them with report_external(),
which dispatches a LangChain custom event that the tracker records.
"""

import os
import threading
from typing import Any
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler, dispatch_custom_event

USAGE_EVENT = "usage_cost"
STEP_KEYS = ("calls", "input_tokens", "output_tokens", "cost")


def _price(name: str, default: float) -> float:
    return float(os.getenv(name, default)) / 1_000_000


def empty() -> dict:
    return {"llm_calls": 0, "input_tokens": 0, "output_tokens": 0, "cost": 0.0,
            "estimated": False, "by_step": {}}


def merge(total: dict | None, extra: dict | None) -> dict:
    """Add one usage summary to another (e.g. follow-up chat onto a run)."""
    result = empty()
    for part in (total, extra):
        if not part:
            continue
        for key in ("llm_calls", "input_tokens", "output_tokens", "cost"):
            result[key] += part.get(key, 0)
        result["estimated"] |= bool(part.get("estimated"))
        for step, values in part.get("by_step", {}).items():
            into = result["by_step"].setdefault(step, dict.fromkeys(STEP_KEYS, 0))
            for key in STEP_KEYS:
                into[key] += values.get(key, 0)
    result["cost"] = round(result["cost"], 6)
    for values in result["by_step"].values():
        values["cost"] = round(values["cost"], 6)
    return result


class UsageTracker(BaseCallbackHandler):
    """Collects token usage and cost from every LLM call in a run. Thread-safe,
    because Scouts and their tool calls run in parallel."""

    def __init__(self):
        self._lock = threading.Lock()
        self._steps: dict[UUID, str] = {}
        self._usage = empty()

    # Remember which step each model call belongs to
    def on_chat_model_start(self, serialized, messages, *, run_id: UUID, metadata=None, **kwargs):
        metadata = metadata or {}
        step = metadata.get("usage_label") or metadata.get("langgraph_node") or "other"
        with self._lock:
            self._steps[run_id] = step

    def on_llm_end(self, response, *, run_id: UUID, **kwargs):
        input_tokens = output_tokens = 0
        cost = None
        for generations in response.generations:
            for gen in generations:
                message = getattr(gen, "message", None)
                usage = getattr(message, "usage_metadata", None) or {}
                input_tokens += usage.get("input_tokens", 0)
                output_tokens += usage.get("output_tokens", 0)
                billed = (getattr(message, "response_metadata", None) or {}).get("token_usage", {}) or {}
                if billed.get("cost") is not None:
                    cost = (cost or 0.0) + float(billed["cost"])
        if not (input_tokens or output_tokens) and response.llm_output:
            billed = response.llm_output.get("token_usage") or {}
            input_tokens = billed.get("prompt_tokens", 0)
            output_tokens = billed.get("completion_tokens", 0)
            if billed.get("cost") is not None:
                cost = float(billed["cost"])

        estimated = cost is None
        if estimated:
            cost = (input_tokens * _price("MODEL_PRICE_INPUT", 0.15)
                    + output_tokens * _price("MODEL_PRICE_OUTPUT", 0.60))
        with self._lock:
            step = self._steps.pop(run_id, "other")
        self._add(step, input_tokens, output_tokens, cost, estimated)

    def on_custom_event(self, name: str, data: Any, **kwargs):
        if name == USAGE_EVENT:
            self._add(data["step"], data.get("input_tokens", 0), data.get("output_tokens", 0),
                      float(data.get("cost", 0.0)), bool(data.get("estimated")))

    def _add(self, step: str, input_tokens: int, output_tokens: int, cost: float, estimated: bool):
        with self._lock:
            u = self._usage
            u["llm_calls"] += 1
            u["input_tokens"] += input_tokens
            u["output_tokens"] += output_tokens
            u["cost"] += cost
            u["estimated"] |= estimated
            s = u["by_step"].setdefault(step, dict.fromkeys(STEP_KEYS, 0))
            s["calls"] += 1
            s["input_tokens"] += input_tokens
            s["output_tokens"] += output_tokens
            s["cost"] += cost

    def summary(self) -> dict:
        with self._lock:
            return merge(self._usage, None)


def report_external(step: str, usage: dict | None) -> None:
    """Report usage from a call made outside LangChain (e.g. the raw OpenAI
    client used for OpenRouter web search). No-op outside a tracked run."""
    if not usage:
        return
    data = {
        "step": step,
        "input_tokens": usage.get("prompt_tokens", 0),
        "output_tokens": usage.get("completion_tokens", 0),
    }
    if usage.get("cost") is not None:
        data["cost"] = usage["cost"]
    else:
        data["estimated"] = True
        data["cost"] = (data["input_tokens"] * _price("MODEL_PRICE_INPUT", 0.15)
                        + data["output_tokens"] * _price("MODEL_PRICE_OUTPUT", 0.60))
    try:
        dispatch_custom_event(USAGE_EVENT, data)
    except RuntimeError:
        pass  # called outside a LangChain run (e.g. a standalone script)
