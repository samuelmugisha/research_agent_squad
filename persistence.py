"""
Backup and restore of the app's data to a private Hugging Face dataset.

Hugging Face Spaces on the free tier have temporary disks: everything is
wiped when the Space restarts or is redeployed. When HF_DATASET_REPO and
HF_TOKEN are set, this module keeps the data in a private dataset repo:

- restore()  runs on startup, before the database is opened. If the local
             disk is empty, it downloads the last backup: the research
             history database and the uploaded knowledge-base documents.
- start()    runs a background thread that checks every BACKUP_INTERVAL
             seconds whether anything changed and, if so, uploads a
             consistent snapshot (SQLite's online backup API) in one commit.
- Vector indexes (knowledge base, past research) are derived data, so they
  aren't backed up: after a restore they are rebuilt from the documents and
  saved reports (see research_ui_server.rebuild_indexes).

Locally these variables aren't set, and the app simply uses its local files.

Settings:
    HF_DATASET_REPO=user/research-agent-squad-data
    HF_TOKEN=hf_...            (write access to that dataset)
    BACKUP_INTERVAL=30         seconds between change checks
"""

import json
import logging
import os
import shutil
import sqlite3
import tempfile
import threading
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger("uvicorn.error")

DATA_DIR = Path(os.getenv("DATA_DIR") or Path(__file__).parent)
DB_PATH = DATA_DIR / "data" / "research_history.sqlite"
KB_FILES = DATA_DIR / "knowledge_db" / "files"
RESTORED_MANIFEST = DATA_DIR / "data" / "kb_manifest.json"

_state = {"restored": False, "last_backup": None, "last_error": None}
_lock = threading.Lock()  # one backup at a time
_stop = threading.Event()


def repo_id() -> str | None:
    return os.getenv("HF_DATASET_REPO") or None


def enabled() -> bool:
    return bool(repo_id() and os.getenv("HF_TOKEN"))


def status() -> dict:
    return {"enabled": enabled(), "repo": repo_id() if enabled() else None,
            "restored": _state["restored"], "last_backup": _state["last_backup"],
            "last_error": _state["last_error"]}


def _api():
    from huggingface_hub import HfApi
    return HfApi(token=os.getenv("HF_TOKEN"))


# ============================================================
# Restore
# ============================================================

def restore() -> bool:
    """Download the last backup onto an empty disk. Must run before history.py
    opens the database. Returns True if data was restored."""
    if not enabled() or DB_PATH.exists():
        return False
    from huggingface_hub import snapshot_download

    try:
        _api().create_repo(repo_id(), repo_type="dataset", private=True, exist_ok=True)
        with tempfile.TemporaryDirectory() as tmp:
            snapshot_download(repo_id(), repo_type="dataset", local_dir=tmp,
                              token=os.getenv("HF_TOKEN"))
            backup = Path(tmp)
            if not (backup / "research_history.sqlite").exists():
                log.info("Backup repo %s is empty; starting fresh.", repo_id())
                return False
            DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup / "research_history.sqlite", DB_PATH)
            if (backup / "kb" / "files").is_dir():
                shutil.copytree(backup / "kb" / "files", KB_FILES, dirs_exist_ok=True)
            if (backup / "kb" / "manifest.json").exists():
                shutil.copy2(backup / "kb" / "manifest.json", RESTORED_MANIFEST)
        _state["restored"] = True
        log.info("Restored research history and documents from %s", repo_id())
        return True
    except Exception as exc:
        _state["last_error"] = f"restore failed: {exc}"
        log.error("Restore from %s failed: %s", repo_id(), exc)
        return False


def restored_manifest() -> dict:
    """{filename: {kind, added_at}} for documents restored from a backup."""
    try:
        return json.loads(RESTORED_MANIFEST.read_text())
    except (OSError, ValueError):
        return {}


# ============================================================
# Backup
# ============================================================

def _fingerprint() -> tuple:
    """Changes whenever the database or the uploaded documents change."""
    paths = [DB_PATH, DB_PATH.with_name(DB_PATH.name + "-wal")]
    if KB_FILES.is_dir():
        paths += sorted(p for p in KB_FILES.iterdir() if p.is_file() and not p.name.endswith(".uploading"))
    return tuple((p.name, p.stat().st_mtime_ns, p.stat().st_size) for p in paths if p.exists())


def backup_now() -> bool:
    """Upload a consistent snapshot of the database and documents."""
    if not enabled() or not DB_PATH.exists():
        return False
    import knowledge_base

    with _lock, tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        # The online backup API copies a live database safely, WAL included
        source = sqlite3.connect(DB_PATH)
        target = sqlite3.connect(folder / "research_history.sqlite")
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()

        (folder / "kb" / "files").mkdir(parents=True)
        manifest = {d["source"]: {"kind": d["kind"], "added_at": d["added_at"]}
                    for d in knowledge_base.list_documents()}
        for name in manifest:
            path = KB_FILES / name
            if path.is_file():
                shutil.copy2(path, folder / "kb" / "files" / name)
        (folder / "kb" / "manifest.json").write_text(json.dumps(manifest, indent=1))

        _api().upload_folder(
            folder_path=str(folder), repo_id=repo_id(), repo_type="dataset",
            commit_message="Backup research history and documents",
            delete_patterns=["kb/**"],  # mirror deleted documents
        )
    _state["last_backup"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    _state["last_error"] = None
    return True


def _loop(interval: float) -> None:
    last = _fingerprint()
    while not _stop.wait(interval):
        current = _fingerprint()
        if current == last:
            continue
        try:
            backup_now()
            last = current
            log.info("Backed up research data to %s", repo_id())
        except Exception as exc:
            _state["last_error"] = f"backup failed: {exc}"
            log.error("Backup to %s failed (will retry): %s", repo_id(), exc)


def start() -> None:
    if enabled():
        interval = float(os.getenv("BACKUP_INTERVAL", "30"))
        threading.Thread(target=_loop, args=(interval,), daemon=True, name="backup").start()


def stop() -> None:
    """Final backup on shutdown, so the last few seconds of changes aren't lost."""
    _stop.set()
    if enabled():
        try:
            backup_now()
        except Exception as exc:
            log.error("Final backup failed: %s", exc)
