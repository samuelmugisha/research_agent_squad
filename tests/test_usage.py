"""Cost tracking: no network calls; LLM results are built by hand."""

import uuid

from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult
from langchain_core.runnables import RunnableLambda

import history
import usage


def llm_result(input_tokens: int, output_tokens: int, cost: float | None = None) -> LLMResult:
    token_usage = {"prompt_tokens": input_tokens, "completion_tokens": output_tokens}
    if cost is not None:
        token_usage["cost"] = cost
    message = AIMessage(
        content="ok",
        usage_metadata={"input_tokens": input_tokens, "output_tokens": output_tokens,
                        "total_tokens": input_tokens + output_tokens},
        response_metadata={"token_usage": token_usage},
    )
    return LLMResult(generations=[[ChatGeneration(message=message)]])


def simulate_call(tracker: usage.UsageTracker, step: str, result: LLMResult) -> None:
    run_id = uuid.uuid4()
    tracker.on_chat_model_start({}, [[]], run_id=run_id, metadata={"langgraph_node": step})
    tracker.on_llm_end(result, run_id=run_id)


def test_tracker_records_billed_cost_per_step():
    tracker = usage.UsageTracker()
    simulate_call(tracker, "supervisor", llm_result(100, 20, cost=0.001))
    simulate_call(tracker, "search_agent", llm_result(200, 40, cost=0.002))
    simulate_call(tracker, "search_agent", llm_result(300, 60, cost=0.003))
    summary = tracker.summary()
    assert summary["llm_calls"] == 3
    assert summary["input_tokens"] == 600 and summary["output_tokens"] == 120
    assert summary["cost"] == 0.006 and not summary["estimated"]
    assert summary["by_step"]["search_agent"]["calls"] == 2
    assert summary["by_step"]["search_agent"]["cost"] == 0.005


def test_tracker_estimates_when_cost_missing(monkeypatch):
    monkeypatch.setenv("MODEL_PRICE_INPUT", "1.0")   # $1 per million tokens
    monkeypatch.setenv("MODEL_PRICE_OUTPUT", "2.0")
    tracker = usage.UsageTracker()
    simulate_call(tracker, "analyst", llm_result(1_000_000, 500_000))
    summary = tracker.summary()
    assert summary["estimated"]
    assert summary["cost"] == 2.0


def test_usage_label_overrides_node_name():
    tracker = usage.UsageTracker()
    run_id = uuid.uuid4()
    tracker.on_chat_model_start({}, [[]], run_id=run_id, metadata={"usage_label": "safety_guard"})
    tracker.on_llm_end(llm_result(10, 5, cost=0.0001), run_id=run_id)
    assert "safety_guard" in tracker.summary()["by_step"]


def test_report_external_reaches_tracker_through_callbacks():
    tracker = usage.UsageTracker()

    def fake_search(_):
        usage.report_external("web_search", {"prompt_tokens": 50, "completion_tokens": 10, "cost": 0.02})
        return []

    RunnableLambda(fake_search).invoke("q", config={"callbacks": [tracker]})
    assert tracker.summary()["by_step"]["web_search"]["cost"] == 0.02


def test_report_external_outside_a_run_is_ignored():
    usage.report_external("web_search", {"prompt_tokens": 1, "completion_tokens": 1, "cost": 0.1})


def test_merge_adds_steps():
    a = {"llm_calls": 1, "input_tokens": 10, "output_tokens": 5, "cost": 0.01, "estimated": False,
         "by_step": {"supervisor": {"calls": 1, "input_tokens": 10, "output_tokens": 5, "cost": 0.01}}}
    b = {"llm_calls": 1, "input_tokens": 20, "output_tokens": 5, "cost": 0.02, "estimated": True,
         "by_step": {"follow_up_chat": {"calls": 1, "input_tokens": 20, "output_tokens": 5, "cost": 0.02}}}
    total = usage.merge(a, b)
    assert total["cost"] == 0.03 and total["llm_calls"] == 2 and total["estimated"]
    assert set(total["by_step"]) == {"supervisor", "follow_up_chat"}


def test_history_stores_run_cost_and_adds_chat_usage():
    run_id = uuid.uuid4().hex
    history.start_run(run_id, "test topic", ["web"])
    tracker = usage.UsageTracker()
    simulate_call(tracker, "report_writer", llm_result(100, 50, cost=0.004))
    history.finish_run(run_id, {"sources": [], "report": "r", "quality_score": 0.8}, 1.0, tracker.summary())
    assert history.get_run(run_id)["cost"] == 0.004

    chat = usage.UsageTracker()
    simulate_call(chat, "follow_up_chat", llm_result(10, 10, cost=0.001))
    total = history.add_usage(run_id, chat.summary())
    run = history.get_run(run_id)
    assert run["cost"] == 0.005 == total["cost"]
    assert "follow_up_chat" in run["usage"]["by_step"]
