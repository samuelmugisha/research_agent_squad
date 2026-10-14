"""
LangSmith tracing (observability).

LangChain and LangGraph send traces to LangSmith automatically when these are
set in .env (the older LANGCHAIN_* names also work):

    LANGSMITH_TRACING=true
    LANGSMITH_API_KEY=lsv2_...
    LANGSMITH_PROJECT=research-agent-squad

Each research run is one trace. Its LangSmith run id is the app's run id, so a
report in History links straight to its trace, which shows every agent, LLM
call, tool call, prompt, token count and latency.
"""

import logging
import os
from uuid import UUID

log = logging.getLogger("uvicorn.error")


def enabled() -> bool:
    flag = os.getenv("LANGSMITH_TRACING") or os.getenv("LANGCHAIN_TRACING_V2") or ""
    key = os.getenv("LANGSMITH_API_KEY") or os.getenv("LANGCHAIN_API_KEY")
    return flag.lower() == "true" and bool(key)


def project() -> str:
    return os.getenv("LANGSMITH_PROJECT") or os.getenv("LANGCHAIN_PROJECT") or "default"


def run_config(run_id: str, topic: str, sources: list[str], callbacks: list) -> dict:
    """Config for one research run: names, tags and metadata make the trace
    easy to find and filter in LangSmith; run_id makes it linkable."""
    return {
        "run_id": UUID(run_id),
        "run_name": "research_run",
        "tags": ["research", *sources],
        "metadata": {"app_run_id": run_id, "topic": topic, "sources": sources},
        "callbacks": callbacks,
    }


def trace_url(run_id: str) -> str | None:
    """The LangSmith page for a run's trace, or None if it can't be found."""
    if not enabled():
        return None
    try:
        from langsmith import Client

        return Client().read_run(run_id).url
    except Exception as exc:
        log.warning("Could not find LangSmith trace %s: %s", run_id, exc)
        return None
