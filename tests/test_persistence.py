"""Backup/restore and long-term memory. Hugging Face and embeddings are faked."""

import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from langchain_core.embeddings import DeterministicFakeEmbedding

import persistence
import research_memory
from research_assistant import AIResearchAssistant


# ---------- persistence ----------

@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(persistence, "DB_PATH", tmp_path / "data" / "research_history.sqlite")
    monkeypatch.setattr(persistence, "KB_FILES", tmp_path / "knowledge_db" / "files")
    monkeypatch.setattr(persistence, "RESTORED_MANIFEST", tmp_path / "data" / "kb_manifest.json")
    monkeypatch.setattr(persistence, "_state", {"restored": False, "last_backup": None, "last_error": None})
    monkeypatch.setenv("HF_DATASET_REPO", "user/backup")
    monkeypatch.setenv("HF_TOKEN", "hf_test")
    return tmp_path


def make_db(path: Path, topic: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE runs (topic TEXT)")
    conn.execute("INSERT INTO runs VALUES (?)", (topic,))
    conn.commit()
    conn.close()


def test_disabled_without_settings(monkeypatch):
    monkeypatch.delenv("HF_DATASET_REPO", raising=False)
    assert not persistence.enabled()
    assert not persistence.restore()


def test_restore_downloads_backup_onto_empty_disk(data_dir):
    def fake_download(repo, repo_type, local_dir, token):
        backup = Path(local_dir)
        make_db(backup / "research_history.sqlite", "restored topic")
        (backup / "kb" / "files").mkdir(parents=True)
        (backup / "kb" / "files" / "notes.md").write_text("# notes")
        (backup / "kb" / "manifest.json").write_text(json.dumps({"notes.md": {"kind": "note", "added_at": "2026-01-01"}}))

    with patch.object(persistence, "_api"), patch("huggingface_hub.snapshot_download", side_effect=fake_download):
        assert persistence.restore()
    rows = sqlite3.connect(persistence.DB_PATH).execute("SELECT topic FROM runs").fetchall()
    assert rows == [("restored topic",)]
    assert (persistence.KB_FILES / "notes.md").read_text() == "# notes"
    assert persistence.restored_manifest()["notes.md"]["kind"] == "note"


def test_restore_never_overwrites_existing_data(data_dir):
    make_db(persistence.DB_PATH, "local")
    with patch("huggingface_hub.snapshot_download") as download:
        assert not persistence.restore()
        download.assert_not_called()


def test_backup_uploads_snapshot_and_mirrors_deletions(data_dir):
    make_db(persistence.DB_PATH, "to back up")
    persistence.KB_FILES.mkdir(parents=True)
    (persistence.KB_FILES / "a.md").write_text("A")
    uploaded = {}

    def fake_upload(folder_path, **kwargs):
        folder = Path(folder_path)
        uploaded["files"] = sorted(str(p.relative_to(folder)) for p in folder.rglob("*") if p.is_file())
        uploaded["db"] = sqlite3.connect(folder / "research_history.sqlite").execute("SELECT topic FROM runs").fetchall()
        uploaded["kwargs"] = kwargs

    api = MagicMock()
    api.upload_folder.side_effect = fake_upload
    kb = MagicMock()
    kb.list_documents.return_value = [{"source": "a.md", "kind": "upload", "added_at": "x"}]
    with patch.object(persistence, "_api", return_value=api), patch.dict("sys.modules", {"knowledge_base": kb}):
        assert persistence.backup_now()
    assert uploaded["files"] == ["kb/files/a.md", "kb/manifest.json", "research_history.sqlite"]
    assert uploaded["db"] == [("to back up",)]
    assert uploaded["kwargs"]["delete_patterns"] == ["kb/**"]
    assert persistence.status()["last_backup"]


def test_fingerprint_changes_when_data_changes(data_dir):
    make_db(persistence.DB_PATH, "one")
    before = persistence._fingerprint()
    persistence.KB_FILES.mkdir(parents=True)
    (persistence.KB_FILES / "new.md").write_text("new")
    assert persistence._fingerprint() != before


# ---------- long-term memory ----------

@pytest.fixture
def memory(tmp_path, monkeypatch):
    store = AIResearchAssistant(persist_directory=str(tmp_path / "memory_db"))
    store.vectorstore._embedding_function = DeterministicFakeEmbedding(size=64)
    monkeypatch.setattr(research_memory, "_store", store)
    monkeypatch.setenv("MEMORY_MIN_RELEVANCE", "-1e9")  # fake embeddings: keep every hit
    return research_memory


def test_memory_add_search_and_delete(memory):
    state = {"report": "## Findings\n- Heat pump grants rose to £7,500 in 2023 [1][2].", "quality_score": 0.9}
    memory.add_report("run1", "UK heat pumps", "2026-10-01T10:00:00", state)
    assert memory.count() == 1
    hits = memory.search("heat pump grants")
    assert hits and hits[0]["kind"] == "memory"
    assert hits[0]["url"] == "/runs/run1/print"
    assert hits[0]["title"].startswith("Past report: UK heat pumps")
    assert "[1]" not in hits[0]["content"]  # old citation numbers are stripped
    memory.add_report("run1", "UK heat pumps", "2026-10-01T10:00:00", state)  # re-index replaces
    assert memory.count() == 1
    memory.delete("run1")
    assert memory.count() == 0


def test_memory_rebuild_from_history(memory):
    reports = [({"id": f"r{i}", "topic": f"topic {i}", "created_at": "2026-10-01T10:00:00"},
                {"report": f"Report number {i}"}) for i in range(3)]
    assert memory.rebuild(reports) == 3
    assert memory.count() == 3
