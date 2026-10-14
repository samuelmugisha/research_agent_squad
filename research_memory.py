"""
Long-term memory: the squad's past research reports, searchable by the agents.

Every finished report is split into chunks, embedded and stored in its own
Chroma index (memory_db/), separate from the user's knowledge base. Scout can
call the search_past_research tool to find what the squad already learned,
so new reports build on earlier work and cite it ("Past report: <topic>").

The index is derived data: it can always be rebuilt from the saved history
(rebuild()), which is what happens after the app restores from a backup.
"""

import os
import re
import threading
import warnings
from datetime import datetime
from pathlib import Path

from langchain_core.documents import Document

from research_assistant import AIResearchAssistant

DATA_DIR = Path(os.getenv("DATA_DIR") or Path(__file__).parent)
MEMORY_DIR = DATA_DIR / "memory_db"
MAX_CONTENT_CHARS = 1500
CITATION = re.compile(r"\[\d+\](?:\[\d+\])*")

_store: AIResearchAssistant | None = None
_lock = threading.Lock()


def _memory() -> AIResearchAssistant:
    global _store
    with _lock:
        if _store is None:
            _store = AIResearchAssistant(persist_directory=str(MEMORY_DIR))
    return _store


def _collection():
    return _memory().vectorstore._collection


def count() -> int:
    """Number of reports in memory."""
    metadatas = _collection().get(include=["metadatas"])["metadatas"]
    return len({m.get("run_id") for m in metadatas if m})


def report_url(run_id: str) -> str:
    return f"/runs/{run_id}/print"


def add_report(run_id: str, topic: str, created_at: str, state: dict) -> int:
    """Index one finished report (replacing any earlier copy of it)."""
    report = state.get("report", "")
    if not report.strip():
        return 0
    # Citation numbers refer to the old report's own source list; drop them
    text = f"# {topic}\n\n{CITATION.sub('', report)}"
    _collection().delete(where={"run_id": run_id})
    return _memory().add_documents([Document(
        page_content=text,
        metadata={"run_id": run_id, "topic": topic, "created_at": created_at,
                  "quality": float(state.get("quality_score") or 0.0)},
    )])


def delete(run_id: str) -> None:
    _collection().delete(where={"run_id": run_id})


def rebuild(reports: list[tuple[dict, dict]]) -> int:
    """Re-index (run, state) pairs from history, e.g. after a restore."""
    added = 0
    for run, state in reports:
        added += bool(add_report(run["id"], run["topic"], run["created_at"], state))
    return added


def search(query: str, k: int = 4) -> list[dict]:
    """Find passages from past reports; same shape as web_search() results."""
    if _collection().count() == 0:
        return []
    min_relevance = float(os.getenv("MEMORY_MIN_RELEVANCE", "0.3"))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        hits = _memory().vectorstore.similarity_search_with_relevance_scores(query, k=k)

    results = []
    for doc, score in hits:
        if score < min_relevance:
            continue
        meta = doc.metadata
        try:
            when = datetime.fromisoformat(meta.get("created_at", "")).strftime("%d %b %Y")
        except ValueError:
            when = ""
        results.append({
            "title": f"Past report: {meta.get('topic', 'earlier research')}" + (f" ({when})" if when else ""),
            "url": report_url(meta.get("run_id", "")),
            "content": doc.page_content[:MAX_CONTENT_CHARS],
            "score": round(score, 3),
            "kind": "memory",
            "meta": when,
        })
    return results
