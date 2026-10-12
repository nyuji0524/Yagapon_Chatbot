"""Durable operational events, jobs, and dead letters without payload content."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path


def default_operations_db_path() -> Path:
    configured = os.environ.get("YAGAPON_OPERATIONS_DB_PATH")
    if configured:
        return Path(configured)
    config_path = Path(
        os.environ.get(
            "YAGAPON_CONFIG_PATH",
            Path(__file__).parent.parent / "data" / "server_config.json",
        )
    )
    return config_path.parent / "operations.sqlite3"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class OperationalStore:
    """Small SQLite store for restart-safe status and privacy-safe metrics."""

    def __init__(self, path: Path | None = None):
        self.path = path or default_operations_db_path()
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self):
        with self._lock, self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS operational_events (
                    id TEXT PRIMARY KEY,
                    component TEXT NOT NULL,
                    event TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    guild_id INTEGER,
                    item_key TEXT,
                    duration_ms INTEGER,
                    details_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS operational_events_created
                    ON operational_events(created_at DESC);
                CREATE INDEX IF NOT EXISTS operational_events_component
                    ON operational_events(component, event, outcome);
                CREATE TABLE IF NOT EXISTS operational_jobs (
                    id TEXT PRIMARY KEY,
                    component TEXT NOT NULL,
                    status TEXT NOT NULL,
                    guild_id INTEGER,
                    progress_json TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    finished_at TEXT
                );
                CREATE INDEX IF NOT EXISTS operational_jobs_updated
                    ON operational_jobs(updated_at DESC);
                CREATE TABLE IF NOT EXISTS dead_letters (
                    id TEXT PRIMARY KEY,
                    component TEXT NOT NULL,
                    item_key TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL,
                    error_type TEXT NOT NULL,
                    details_json TEXT NOT NULL,
                    next_retry_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(component, item_key)
                );
                """
            )

    def record_event(
        self,
        component: str,
        event: str,
        outcome: str,
        *,
        guild_id: int | None = None,
        item_key: str | None = None,
        duration_ms: int | None = None,
        details: dict | None = None,
    ) -> str:
        event_id = uuid.uuid4().hex
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO operational_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event_id,
                    component,
                    event,
                    outcome,
                    guild_id,
                    item_key,
                    duration_ms,
                    json.dumps(details or {}, ensure_ascii=False),
                    _now(),
                ),
            )
        return event_id

    def start_job(
        self,
        component: str,
        *,
        guild_id: int | None = None,
        job_id: str | None = None,
        progress: dict | None = None,
    ) -> str:
        job_id = job_id or uuid.uuid4().hex
        now = _now()
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO operational_jobs VALUES (?, ?, ?, ?, ?, ?, ?, NULL)",
                (
                    job_id,
                    component,
                    "running",
                    guild_id,
                    json.dumps(progress or {}, ensure_ascii=False),
                    now,
                    now,
                ),
            )
        return job_id

    def update_job(self, job_id: str, status: str, progress: dict) -> None:
        now = _now()
        finished_at = now if status in {"completed", "failed", "cancelled"} else None
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE operational_jobs
                SET status = ?, progress_json = ?, updated_at = ?,
                    finished_at = COALESCE(?, finished_at)
                WHERE id = ?
                """,
                (status, json.dumps(progress, ensure_ascii=False), now, finished_at, job_id),
            )

    def get_job(self, job_id: str) -> dict | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM operational_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result.update(json.loads(result.pop("progress_json")))
        result["job_id"] = result.pop("id")
        return result

    def dead_letter(
        self,
        component: str,
        item_key: str,
        *,
        attempts: int,
        error_type: str,
        details: dict | None = None,
        retry_after_seconds: int | None = None,
    ) -> None:
        now = datetime.now(timezone.utc)
        next_retry_at = (
            (now + timedelta(seconds=retry_after_seconds)).isoformat(timespec="seconds")
            if retry_after_seconds is not None
            else None
        )
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO dead_letters (
                    id, component, item_key, status, attempts, error_type,
                    details_json, next_retry_at, created_at, updated_at
                ) VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?)
                ON CONFLICT(component, item_key) DO UPDATE SET
                    status='pending', attempts=excluded.attempts,
                    error_type=excluded.error_type, details_json=excluded.details_json,
                    next_retry_at=excluded.next_retry_at, updated_at=excluded.updated_at
                """,
                (
                    uuid.uuid4().hex,
                    component,
                    item_key,
                    attempts,
                    error_type,
                    json.dumps(details or {}, ensure_ascii=False),
                    next_retry_at,
                    now.isoformat(timespec="seconds"),
                    now.isoformat(timespec="seconds"),
                ),
            )

    def resolve_dead_letter(self, component: str, item_key: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE dead_letters SET status='resolved', updated_at=? WHERE component=? AND item_key=?",
                (_now(), component, item_key),
            )

    def pending_dead_letters(self, limit: int = 100) -> list[dict]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM dead_letters WHERE status='pending'
                ORDER BY updated_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            {**dict(row), "details": json.loads(row["details_json"])}
            for row in rows
        ]

    def recent_events(self, limit: int = 100) -> list[dict]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM operational_events ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {**dict(row), "details": json.loads(row["details_json"])}
            for row in rows
        ]

    def summary(self, hours: int = 24) -> dict:
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT component, event, outcome, count(*) AS count,
                       avg(duration_ms) AS average_duration_ms
                FROM operational_events WHERE created_at >= ?
                GROUP BY component, event, outcome ORDER BY count(*) DESC
                """,
                (since,),
            ).fetchall()
            running = connection.execute(
                "SELECT count(*) FROM operational_jobs WHERE status='running'"
            ).fetchone()[0]
            dead_letters = connection.execute(
                "SELECT count(*) FROM dead_letters WHERE status='pending'"
            ).fetchone()[0]
        return {
            "window_hours": hours,
            "events": [dict(row) for row in rows],
            "running_jobs": running,
            "pending_dead_letters": dead_letters,
        }
