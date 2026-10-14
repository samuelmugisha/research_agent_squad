"""
Academic search for the Scout agent: recent journal articles and preprints.

Backends (free, no API key):
- OpenAlex  -> peer-reviewed journal articles and conference papers
- arXiv     -> preprints (latest research, not yet peer-reviewed)

scholarly_search() returns the same {"title", "url", "content"} shape as
web_search(), plus "kind": "academic" and a short "meta" line
(venue · date · citations · authors) for the UI.

Settings (.env):
    SCHOLAR_YEARS=3          only papers published in the last N years
    SCHOLAR_MAX_RESULTS=4    papers per backend per query
"""

import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

import httpx

MAX_CONTENT_CHARS = 1500
ATOM = {"a": "http://www.w3.org/2005/Atom"}
STOPWORDS = set(
    "a an and are as at be by for from how in into is it of on or that the their this "
    "to vs what when where which who why with latest recent new current impact role "
    "using use based study studies research review overview effect effects challenges "
    "limitations benefits advantages potential practices best building future trends "
    "developments applications case analysis comparison between versus".split()
)

# arXiv asks clients to wait ~3s between requests; Scouts run in parallel
_arxiv_lock = threading.Lock()
_arxiv_last_call = 0.0


def _since() -> date:
    years = float(os.getenv("SCHOLAR_YEARS", "3"))
    return date.today() - timedelta(days=int(365 * years))


def _authors(names: list[str]) -> str:
    if not names:
        return ""
    return names[0] + (" et al." if len(names) > 2 else f" & {names[1]}" if len(names) == 2 else "")


def _abstract_from_index(index: dict | None) -> str:
    """OpenAlex stores abstracts as {word: [positions]}; rebuild the text."""
    if not index:
        return ""
    words = sorted((pos, word) for word, positions in index.items() for pos in positions)
    return " ".join(word for _, word in words)


def _normalize_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]", "", title.lower())


def search_openalex(query: str, max_results: int) -> list[dict]:
    response = httpx.get(
        "https://api.openalex.org/works",
        params={
            "search": query,
            "filter": f"from_publication_date:{_since().isoformat()},has_abstract:true,is_retracted:false",
            "per-page": max_results * 2,
            "select": "id,doi,title,publication_date,cited_by_count,primary_location,authorships,abstract_inverted_index,type",
        },
        timeout=20,
    )
    response.raise_for_status()

    results = []
    for work in response.json().get("results", []):
        if not work.get("title"):
            continue
        location = work.get("primary_location") or {}
        venue = (location.get("source") or {}).get("display_name") or (work.get("type") or "article").replace("-", " ").capitalize()
        authors = [a["author"]["display_name"] for a in work.get("authorships", []) if a.get("author")]
        published = work.get("publication_date", "")
        citations = work.get("cited_by_count", 0)
        meta = " · ".join(filter(None, [
            venue,
            published[:7],
            f"{citations} citation{'s' if citations != 1 else ''}",
            _authors(authors),
        ]))
        results.append({
            "title": work["title"],
            "url": work.get("doi") or location.get("landing_page_url") or work["id"],
            "content": f"{meta}\nAbstract: {_abstract_from_index(work.get('abstract_inverted_index'))}",
            "meta": meta,
            "published": published,
            "kind": "academic",
        })
    return results


def _keywords(query: str, max_terms: int = 5) -> list[str]:
    terms = [w for w in re.findall(r"[a-z0-9\-]+", query.lower()) if w not in STOPWORDS and len(w) > 2]
    return list(dict.fromkeys(terms))[:max_terms]


def _matches_topic(text: str, keywords: list[str], min_share: float = 0.6) -> bool:
    """arXiv matches loosely (e.g. "fasting" ~ "fast"); require most keywords to appear."""
    words = set(re.findall(r"[a-z0-9\-]+", text.lower()))
    hits = sum(any(w.startswith(k[:6]) for w in words) for k in keywords)
    return hits >= max(1, round(len(keywords) * min_share))


def _arxiv_query(query: str, max_terms: int) -> str:
    terms = _keywords(query, max_terms)
    since = _since().strftime("%Y%m%d")
    today = date.today().strftime("%Y%m%d")
    clauses = [f"all:{t}" for t in terms] + [f"submittedDate:[{since}0000 TO {today}2359]"]
    return " AND ".join(clauses)


def _arxiv_get(search_query: str, max_results: int) -> list[ET.Element]:
    global _arxiv_last_call
    with _arxiv_lock:
        wait = 3.0 - (time.monotonic() - _arxiv_last_call)
        if wait > 0:
            time.sleep(wait)
        try:
            response = httpx.get(
                "https://export.arxiv.org/api/query",
                params={"search_query": search_query, "sortBy": "relevance", "max_results": max_results},
                timeout=20,
            )
        finally:
            _arxiv_last_call = time.monotonic()
    response.raise_for_status()
    return ET.fromstring(response.text).findall("a:entry", ATOM)


def search_arxiv(query: str, max_results: int) -> list[dict]:
    # Strict match on up to 5 keywords first; loosen to 3 if nothing comes back
    entries = _arxiv_get(_arxiv_query(query, 5), max_results)
    if not entries:
        entries = _arxiv_get(_arxiv_query(query, 3), max_results)

    keywords = _keywords(query)
    results = []
    for entry in entries:
        text = lambda tag: " ".join((entry.findtext(f"a:{tag}", "", ATOM) or "").split())
        authors = [" ".join(a.findtext("a:name", "", ATOM).split()) for a in entry.findall("a:author", ATOM)]
        if not _matches_topic(f"{text('title')} {text('summary')}", keywords):
            continue
        published = text("published")[:10]
        meta = " · ".join(filter(None, ["arXiv preprint", published[:7], _authors(authors)]))
        results.append({
            "title": text("title"),
            "url": text("id").replace("http://", "https://"),
            "content": f"{meta} (not peer-reviewed)\nAbstract: {text('summary')}",
            "meta": meta,
            "published": published,
            "kind": "academic",
        })
    return results


def scholarly_search(query: str, max_results: int | None = None) -> list[dict]:
    """Search OpenAlex and arXiv in parallel; a failing backend is skipped."""
    max_results = max_results or int(os.getenv("SCHOLAR_MAX_RESULTS", "4"))
    backends = {"OpenAlex": search_openalex, "arXiv": search_arxiv}

    results = []
    with ThreadPoolExecutor(max_workers=len(backends)) as pool:
        futures = {name: pool.submit(fn, query, max_results) for name, fn in backends.items()}
        for name, future in futures.items():
            try:
                results.extend(future.result()[:max_results])
            except Exception as exc:
                print(f"[scholarly_search] {name} failed for {query!r}: {exc}")

    # The same paper often appears twice (e.g. journal + translated edition)
    unique, seen = [], set()
    for r in results:
        key = _normalize_title(r["title"])
        if key and key not in seen:
            seen.add(key)
            r["content"] = r["content"][:MAX_CONTENT_CHARS]
            unique.append(r)
    return unique
