"""
Saved research history in SQLite (data/research_history.sqlite).

- LangGraph's SqliteSaver checkpoints every run's full graph state, using the
  run id as the thread_id. load_state() reads the final state back.
- A `runs` table holds the summary the sidebar lists (topic, status, score...).
- `runs_fts` (FTS5) indexes topics and report text for search.
- `chat_messages` keeps follow-up conversations so they survive restarts.
"""

import json
import os
import re
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver

import usage as usage_lib

# DATA_DIR lets a deployment keep data elsewhere (e.g. persistent storage)
DATA_DIR = Path(os.getenv("DATA_DIR") or Path(__file__).parent)
DB_PATH = DATA_DIR / "data" / "research_history.sqlite"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

# Snippet highlight markers (control characters never appear in reports)
MARK_START, MARK_END = "\x02", "\x03"

# The checkpointer gets its own connection; SqliteSaver locks it internally
checkpointer = SqliteSaver(sqlite3.connect(DB_PATH, check_same_thread=False))
checkpointer.setup()

_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
_conn.row_factory = sqlite3.Row
_lock = threading.Lock()

with _lock, _conn:
    _conn.execute("PRAGMA journal_mode=WAL")
    _conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS runs (
            id TEXT PRIMARY KEY,
            topic TEXT NOT NULL,
            status TEXT NOT NULL,          -- running | done | error
            error TEXT,
            sources TEXT NOT NULL,         -- JSON list of searched source types
            created_at TEXT NOT NULL,
            seconds REAL,
            quality REAL,
            source_count INTEGER DEFAULT 0,
            paper_count INTEGER DEFAULT 0,
            kb_count INTEGER DEFAULT 0
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS runs_fts USING fts5(
            topic, report, run_id UNINDEXED
        );
        CREATE TABLE IF NOT EXISTS chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            role TEXT NOT NULL,            -- user | assistant
            content TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS chat_by_run ON chat_messages(run_id, id);
        """
    )
    # Added after the first release: token for the public read-only share link
    columns = {row["name"] for row in _conn.execute("PRAGMA table_info(runs)")}
    if "share_token" not in columns:
        _conn.execute("ALTER TABLE runs ADD COLUMN share_token TEXT")
    # Added with cost tracking: token usage summary (JSON) and total USD cost
    # Added with RBAC: who started the run (NULL for runs from before accounts)
    if "user_id" not in columns:
        _conn.execute("ALTER TABLE runs ADD COLUMN user_id INTEGER")
    if "usage" not in columns:
        _conn.execute("ALTER TABLE runs ADD COLUMN usage TEXT")
        _conn.execute("ALTER TABLE runs ADD COLUMN cost REAL")
    _conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS runs_by_share ON runs(share_token)")
    # Runs still marked "running" were cut off by a server restart
    _conn.execute(
        "UPDATE runs SET status = 'error', error = 'Interrupted by a server restart' "
        "WHERE status = 'running'"
    )


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def thread_config(run_id: str) -> dict:
    return {"configurable": {"thread_id": run_id}}


def start_run(run_id: str, topic: str, sources: list[str], user_id: int | None = None) -> None:
    with _lock, _conn:
        _conn.execute(
            "INSERT INTO runs (id, topic, status, sources, created_at, user_id) VALUES (?, ?, 'running', ?, ?, ?)",
            (run_id, topic, json.dumps(sources), _now(), user_id),
        )


def finish_run(run_id: str, state: dict, seconds: float, usage: dict | None = None) -> None:
    sources = state.get("sources", [])
    count = lambda kind: sum(1 for s in sources if s.get("kind") == kind)
    with _lock, _conn:
        _conn.execute(
            "UPDATE runs SET status = 'done', seconds = ?, quality = ?, source_count = ?, "
            "paper_count = ?, kb_count = ?, usage = ?, cost = ? WHERE id = ?",
            (seconds, state.get("quality_score"), len(sources), count("academic"), count("kb"),
             json.dumps(usage) if usage else None, usage["cost"] if usage else None, run_id),
        )
        _conn.execute(
            "INSERT INTO runs_fts (topic, report, run_id) "
            "SELECT topic, ?, id FROM runs WHERE id = ?",
            (state.get("report", ""), run_id),
        )


def assign_unowned_runs(user_id: int) -> int:
    """Give runs from before user accounts existed an owner."""
    with _lock, _conn:
        return _conn.execute("UPDATE runs SET user_id = ? WHERE user_id IS NULL", (user_id,)).rowcount


def fail_run(run_id: str, error: str, usage: dict | None = None) -> None:
    """Mark a run failed. Failed runs still cost money, so usage is kept."""
    with _lock, _conn:
        _conn.execute(
            "UPDATE runs SET status = 'error', error = ?, usage = ?, cost = ? "
            "WHERE id = ? AND status = 'running'",
            (error, json.dumps(usage) if usage else None, usage["cost"] if usage else None, run_id),
        )


def add_usage(run_id: str, extra: dict) -> dict | None:
    """Add usage (e.g. a follow-up chat answer) to a run's total."""
    with _lock, _conn:
        row = _conn.execute("SELECT usage FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            return None
        total = usage_lib.merge(json.loads(row["usage"]) if row["usage"] else None, extra)
        _conn.execute("UPDATE runs SET usage = ?, cost = ? WHERE id = ?",
                      (json.dumps(total), total["cost"], run_id))
    return total


def _row(row: sqlite3.Row) -> dict:
    run = dict(row)
    run["sources"] = json.loads(run["sources"])
    run["usage"] = json.loads(run["usage"]) if run.get("usage") else None
    return run


def _fts_query(text: str) -> str:
    """Turn free text into a safe FTS5 prefix query: "word1"* "word2"*"""
    words = re.findall(r"\w+", text.lower())
    return " ".join(f'"{w}"*' for w in words[:12])


def list_runs(query: str = "", limit: int = 100) -> list[dict]:
    with _lock:
        match = _fts_query(query)
        if match:
            rows = _conn.execute(
                f"""
                SELECT runs.*, snippet(runs_fts, 1, '{MARK_START}', '{MARK_END}', '…', 14) AS snippet
                FROM runs_fts JOIN runs ON runs.id = runs_fts.run_id
                WHERE runs_fts MATCH ?
                ORDER BY rank LIMIT ?
                """,
                (match, limit),
            ).fetchall()
        elif query.strip():
            rows = []
        else:
            rows = _conn.execute(
                "SELECT *, NULL AS snippet FROM runs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
    return [_row(r) for r in rows]


def get_run(run_id: str) -> dict | None:
    with _lock:
        row = _conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    return _row(row) if row else None


def load_state(run_id: str) -> dict | None:
    """The final graph state from the LangGraph checkpoint."""
    saved = checkpointer.get_tuple(thread_config(run_id))
    return dict(saved.checkpoint["channel_values"]) if saved else None


def set_share_token(run_id: str, token: str | None) -> None:
    with _lock, _conn:
        _conn.execute("UPDATE runs SET share_token = ? WHERE id = ?", (token, run_id))


def get_run_by_share_token(token: str) -> dict | None:
    with _lock:
        row = _conn.execute(
            "SELECT * FROM runs WHERE share_token = ? AND status = 'done'", (token,)
        ).fetchone()
    return _row(row) if row else None


def add_chat_message(run_id: str, role: str, content: str) -> None:
    with _lock, _conn:
        _conn.execute(
            "INSERT INTO chat_messages (run_id, role, content, created_at) VALUES (?, ?, ?, ?)",
            (run_id, role, content, _now()),
        )


def get_chat(run_id: str) -> list[dict]:
    with _lock:
        rows = _conn.execute(
            "SELECT role, content, created_at FROM chat_messages WHERE run_id = ? ORDER BY id",
            (run_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def delete_run(run_id: str) -> bool:
    with _lock, _conn:
        found = _conn.execute("DELETE FROM runs WHERE id = ?", (run_id,)).rowcount > 0
        _conn.execute("DELETE FROM runs_fts WHERE run_id = ?", (run_id,))
        _conn.execute("DELETE FROM chat_messages WHERE run_id = ?", (run_id,))
    checkpointer.delete_thread(run_id)
    return found
