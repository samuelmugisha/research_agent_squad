"""
Knowledge base: the user's own documents, indexed for the Scout agent.

Reuses AIResearchAssistant's vector store (OpenAI embeddings + splitter + Chroma)
from research_assistant.py. It persists to ./knowledge_db rather than
./research_db because the research_assistant.py demo wipes ./research_db.

Original files are kept in ./knowledge_db/files so citations can link to them.
"""

import os
import re
import threading
import warnings
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from langchain_community.document_loaders import PyPDFLoader
from langchain_core.documents import Document

from research_assistant import AIResearchAssistant

# DATA_DIR lets a deployment keep data elsewhere (e.g. persistent storage)
DATA_DIR = Path(os.getenv("DATA_DIR") or Path(__file__).parent)
KB_DIR = DATA_DIR / "knowledge_db"
FILES_DIR = KB_DIR / "files"
ALLOWED_EXTENSIONS = {".pdf", ".txt", ".md", ".markdown"}
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_CONTENT_CHARS = 1500

_assistant: AIResearchAssistant | None = None
_lock = threading.Lock()


def _store() -> AIResearchAssistant:
    global _assistant
    with _lock:
        if _assistant is None:
            FILES_DIR.mkdir(parents=True, exist_ok=True)
            _assistant = AIResearchAssistant(persist_directory=str(KB_DIR / "chroma"))
    return _assistant


def _collection():
    return _store().vectorstore._collection


def safe_filename(name: str) -> str:
    stem, ext = os.path.splitext(Path(name).name)
    stem = re.sub(r"[^\w\- .()]+", "_", stem).strip(" .") or "document"
    return f"{stem[:100]}{ext.lower()}"


def file_path(source: str) -> Path:
    return FILES_DIR / Path(source).name


def file_url(source: str, page: int | None = None) -> str:
    url = f"/api/kb/files/{quote(source)}"
    return f"{url}#page={page}" if page else url


def _load(path: Path, ext: str) -> tuple[list[Document], int]:
    """Return the non-empty pages/texts of a file and its total page count."""
    if ext == ".pdf":
        pages = PyPDFLoader(str(path)).load()
        return [p for p in pages if p.page_content.strip()], len(pages)
    text = path.read_text(encoding="utf-8", errors="replace")
    return ([Document(page_content=text)] if text.strip() else []), 0


def add_file(filename: str, data: bytes, kind: str = "upload", added_at: str | None = None) -> dict:
    """Save and index a file. Re-uploading the same name replaces it."""
    name = safe_filename(filename)
    ext = Path(name).suffix
    if ext not in ALLOWED_EXTENSIONS:
        raise ValueError(f"{name}: unsupported type. Use PDF, TXT or Markdown.")
    if not data.strip():
        raise ValueError(f"{name}: file is empty.")
    if len(data) > MAX_FILE_BYTES:
        raise ValueError(f"{name}: larger than {MAX_FILE_BYTES // (1024 * 1024)} MB.")

    store = _store()
    path = file_path(name)
    tmp_path = path.with_name(path.name + ".uploading")
    tmp_path.write_bytes(data)
    try:
        docs, page_count = _load(tmp_path, ext)
    except Exception as exc:
        tmp_path.unlink(missing_ok=True)
        raise ValueError(f"{name}: could not read file ({exc}).") from exc
    if not docs:
        tmp_path.unlink(missing_ok=True)
        raise ValueError(f"{name}: no text found. Scanned PDFs need OCR first.")
    tmp_path.replace(path)

    _collection().delete(where={"source": name})

    added_at = added_at or datetime.now().isoformat(timespec="seconds")
    for doc in docs:
        page = doc.metadata.get("page")
        doc.metadata = {
            "source": name,
            "kind": kind,
            "added_at": added_at,
            "size": len(data),
            "pages": page_count,
        }
        if page is not None:
            doc.metadata["page"] = page + 1  # PyPDF pages are 0-based

    chunks = store.add_documents(docs)
    return {"source": name, "chunks": chunks}


def add_note(title: str, text: str) -> dict:
    name = safe_filename(title)
    if Path(name).suffix not in ALLOWED_EXTENSIONS:
        name += ".md"
    return add_file(name, f"# {title.strip()}\n\n{text.strip()}\n".encode(), kind="note")


def list_documents() -> list[dict]:
    docs: dict[str, dict] = {}
    for meta in _collection().get(include=["metadatas"])["metadatas"]:
        if not meta or "source" not in meta:
            continue
        source = meta["source"]
        doc = docs.setdefault(source, {
            "source": source,
            "kind": meta.get("kind", "upload"),
            "added_at": meta.get("added_at", ""),
            "size": meta.get("size", 0),
            "pages": meta.get("pages", 0),
            "chunks": 0,
            "url": file_url(source),
        })
        doc["chunks"] += 1
    return sorted(docs.values(), key=lambda d: d["added_at"], reverse=True)


def delete_document(source: str) -> bool:
    name = Path(source).name
    found = bool(_collection().get(where={"source": name}, limit=1)["ids"])
    _collection().delete(where={"source": name})
    file_path(name).unlink(missing_ok=True)
    return found


def chunk_count() -> int:
    return _collection().count()


def search(query: str, k: int = 4) -> list[dict]:
    """Find relevant passages; returns the same shape as web_search()."""
    if chunk_count() == 0:
        return []
    min_relevance = float(os.getenv("KB_MIN_RELEVANCE", "0.25"))
    with warnings.catch_warnings():
        # Unrelated chunks score below 0, which LangChain warns about; we filter them
        warnings.simplefilter("ignore", UserWarning)
        hits = _store().vectorstore.similarity_search_with_relevance_scores(query, k=k)

    results = []
    for doc, score in hits:
        if score < min_relevance:
            continue
        source = doc.metadata.get("source", "document")
        page = doc.metadata.get("page")
        results.append({
            "title": f"{source} (p. {page})" if page else source,
            "url": file_url(source, page),
            "content": doc.page_content[:MAX_CONTENT_CHARS],
            "score": round(score, 3),
            "kind": "kb",
        })
    return results
