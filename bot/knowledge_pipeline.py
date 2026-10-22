"""Cross-process exclusion for Knowledge corpus replacement and curation.

The Discord bot and curation worker share the same ``/data``
volume.  Process-local ``asyncio.Lock`` instances therefore cannot prevent a
curation cycle from reading the corpus while a history replacement is in
progress.  This small advisory lock is the common boundary used by those
cross-process operations.
"""

from __future__ import annotations

import fcntl
import os
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Iterator


class KnowledgePipelineBusy(RuntimeError):
    """Raised when another process owns the shared Knowledge pipeline."""


def knowledge_pipeline_lock_path() -> Path:
    configured = os.environ.get("YAGAPON_KNOWLEDGE_PIPELINE_LOCK", "").strip()
    if configured:
        return Path(configured)
    rag_path = Path(os.environ.get("YAGAPON_RAG_DB_PATH", "data/rag.sqlite3"))
    return rag_path.parent / "knowledge-pipeline.lock"


def acquire_knowledge_pipeline_lock() -> IO[str]:
    """Acquire the shared lock without waiting on a Discord event loop."""
    lock_path = knowledge_pipeline_lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = lock_path.open("w", encoding="utf-8")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        lock.close()
        raise KnowledgePipelineBusy("Another Knowledge pipeline run is active") from exc
    except BaseException:
        lock.close()
        raise
    return lock


def release_knowledge_pipeline_lock(lock: IO[str] | None) -> None:
    if lock is None:
        return
    try:
        fcntl.flock(lock, fcntl.LOCK_UN)
    finally:
        lock.close()


@contextmanager
def knowledge_pipeline_lock() -> Iterator[None]:
    lock = acquire_knowledge_pipeline_lock()
    try:
        yield
    finally:
        release_knowledge_pipeline_lock(lock)
