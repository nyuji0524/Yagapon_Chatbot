from pathlib import Path

import pytest

from bot.knowledge_pipeline import (
    KnowledgePipelineBusy,
    acquire_knowledge_pipeline_lock,
    knowledge_pipeline_lock,
    knowledge_pipeline_lock_path,
    release_knowledge_pipeline_lock,
)


def test_pipeline_lock_defaults_to_rag_volume(monkeypatch, tmp_path):
    rag_path = tmp_path / "rag.sqlite3"
    monkeypatch.setenv("YAGAPON_RAG_DB_PATH", str(rag_path))
    monkeypatch.delenv("YAGAPON_KNOWLEDGE_PIPELINE_LOCK", raising=False)

    assert knowledge_pipeline_lock_path() == tmp_path / "knowledge-pipeline.lock"


def test_pipeline_lock_excludes_another_process_path(monkeypatch, tmp_path):
    lock_path = tmp_path / "pipeline.lock"
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_PIPELINE_LOCK", str(lock_path))

    with knowledge_pipeline_lock():
        with pytest.raises(KnowledgePipelineBusy):
            acquire_knowledge_pipeline_lock()

    acquired = acquire_knowledge_pipeline_lock()
    release_knowledge_pipeline_lock(acquired)
    assert Path(lock_path).exists()
