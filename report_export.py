"""
Export a saved research run.

- document()     -> the read-only payload behind the print/PDF and share pages
- to_markdown()  -> the report as Markdown with a numbered Sources list

Sources from the user's private knowledge base keep their title but lose
their link, so exported and shared reports never point at private files.
"""

import re
from datetime import datetime
from urllib.parse import urlparse


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "research-report"


def public_sources(sources: list[dict]) -> list[dict]:
    public = []
    for source in sources:
        source = {key: source.get(key, "") for key in ("id", "title", "url", "kind", "meta")}
        if source["kind"] in ("kb", "memory"):  # private documents and reports
            source["url"] = ""
        public.append(source)
    return public


def document(run: dict, state: dict) -> dict:
    sources = public_sources(state.get("sources", []))
    return {
        "topic": run["topic"],
        "created_at": run["created_at"],
        "seconds": run["seconds"],
        "quality": state.get("quality_score"),
        "report": state.get("report", ""),
        "sources": sources,
    }


def _source_line(source: dict) -> str:
    if source["kind"] == "kb":
        detail = "private document (knowledge base)"
    elif source["kind"] == "memory":
        detail = "earlier research report"
    elif source["kind"] == "academic":
        detail = source["meta"] or "academic paper"
    else:
        detail = urlparse(source["url"]).netloc.removeprefix("www.")
    title = f"[{source['title']}]({source['url']})" if source["url"] else source["title"]
    return f"{source['id']}. {title} — {detail}"


def to_markdown(doc: dict) -> str:
    created = datetime.fromisoformat(doc["created_at"])
    meta = [f"Researched {created:%B} {created.day}, {created.year}", f"{len(doc['sources'])} sources"]
    if doc["quality"] is not None:
        meta.append(f"quality score {doc['quality']:.2f}")
    meta_line = f"> {' · '.join(meta)} · Research Agent Squad"

    report = doc["report"].strip()
    if report.startswith("# "):
        title, _, body = report.partition("\n")
    else:
        title, body = f"# {doc['topic']}", report

    parts = [title, meta_line, body.strip()]
    if doc["sources"]:
        parts.append("## Sources\n\n" + "\n".join(_source_line(s) for s in doc["sources"]))
    return "\n\n".join(parts) + "\n"
