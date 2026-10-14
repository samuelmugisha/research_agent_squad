"""
Live web search for the Scout agent.

Backends (picked automatically):
- Tavily      -> when TAVILY_API_KEY is set in .env
- OpenRouter  -> otherwise, when OPENAI_BASE_URL points at OpenRouter
                 (uses its "web" plugin with the existing API key)

web_search() returns a list of {"title", "url", "content"} dicts.
"""

import os

import httpx
from langsmith.wrappers import wrap_openai
from openai import OpenAI

import usage

MAX_CONTENT_CHARS = 1500


def search_provider() -> str:
    if os.getenv("TAVILY_API_KEY"):
        return "tavily"
    if "openrouter.ai" in os.getenv("OPENAI_BASE_URL", ""):
        return "openrouter"
    return "none"


def _tavily_search(query: str, max_results: int) -> list[dict]:
    response = httpx.post(
        "https://api.tavily.com/search",
        headers={"Authorization": f"Bearer {os.environ['TAVILY_API_KEY']}"},
        json={"query": query, "max_results": max_results, "search_depth": "basic"},
        timeout=30,
    )
    response.raise_for_status()
    return [
        {"title": r.get("title", ""), "url": r.get("url", ""), "content": r.get("content", "")}
        for r in response.json().get("results", [])
    ]


def _openrouter_search(query: str, max_results: int) -> list[dict]:
    client = wrap_openai(OpenAI())  # traced in LangSmith when tracing is on
    response = client.chat.completions.create(
        model=os.getenv("CHAT_MODEL", "openai/gpt-4o-mini"),
        messages=[
            {
                "role": "user",
                "content": f"Search the web for: {query}\nSummarize the most relevant, recent facts.",
            }
        ],
        extra_body={"plugins": [{"id": "web", "max_results": max_results}]},
    )
    # The web plugin is billed per result; OpenRouter includes the cost here
    usage.report_external("web_search", response.usage.model_dump() if response.usage else None)
    annotations = response.choices[0].message.annotations or []
    results = []
    for annotation in annotations:
        citation = annotation.model_dump().get("url_citation") or {}
        results.append(
            {
                "title": citation.get("title", ""),
                "url": citation.get("url", ""),
                "content": citation.get("content", ""),
            }
        )
    return results


def web_search(query: str, max_results: int | None = None) -> list[dict]:
    """Search the live web and return de-duplicated results with real URLs."""
    max_results = max_results or int(os.getenv("SEARCH_MAX_RESULTS", "5"))
    provider = search_provider()

    if provider == "tavily":
        raw = _tavily_search(query, max_results)
    elif provider == "openrouter":
        raw = _openrouter_search(query, max_results)
    else:
        raise RuntimeError(
            "No web search configured. Set TAVILY_API_KEY in .env, or use an "
            "OpenRouter key with OPENAI_BASE_URL=https://openrouter.ai/api/v1."
        )

    results, seen = [], set()
    for r in raw:
        url = r["url"]
        if not url.startswith(("http://", "https://")) or url in seen:
            continue
        seen.add(url)
        results.append(
            {
                "title": r["title"] or url,
                "url": url,
                "content": r["content"][:MAX_CONTENT_CHARS],
            }
        )
    return results
