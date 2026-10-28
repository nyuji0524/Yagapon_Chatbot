"""Local exact-match mirror and RAG quality telemetry."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from bot.knowledge_authority import (
    CURRENT_IMPLEMENTATION_AUTHORITIES,
    CURRENT_STATUSES,
    QuestionKind,
    allows_unreviewed_evidence,
    authority_score,
    can_answer_directly,
    direct_authorities,
    is_history_narrative_source,
)

SQLITE_CONNECT_TIMEOUT_SECONDS = 5.0
SQLITE_BUSY_TIMEOUT_MS = 5_000
SQLITE_WRITE_ATTEMPTS = 3
SQLITE_RETRY_BASE_SECONDS = 0.05
DOCUMENTS_FTS_INDEX_VERSION = "1"

_LOW_INFORMATION_QUERY_TERMS = {
    "あった内容",
    "なった点",
    "会議内容",
    "内容",
    "運用",
    "記録",
    "問題",
    "問題点",
    "課題",
}

# Two-kanji subjects are common in short Discord questions, but accepting every
# two-character fragment also adds particles, time words and generic verbs to
# exact search. Keep a small, domain-neutral noun allowlist for high-signal
# questions while the ordinary Japanese extractor remains three characters or
# longer.
_HIGH_INFORMATION_SHORT_QUERY_TERMS = {
    "通知",
    "投票",
    "会場",
    "地図",
    "権限",
    "担当",
    "結果",
    "構造",
    "組織",
    "役職",
    "企画",
    "協賛",
    "新歓",
    "本祭",
    "会議",
    "議題",
    "決定",
    "申請",
    "受付",
    "発券",
    "順位",
    "票数",
    "場所",
    "日程",
    "予算",
    "経費",
    "在庫",
    "部室",
}

def default_rag_db_path() -> Path:
    configured = os.environ.get("YAGAPON_RAG_DB_PATH")
    if configured:
        return Path(configured)
    default_config = Path(__file__).parent.parent / "data" / "server_config.json"
    config_path = Path(os.environ.get("YAGAPON_CONFIG_PATH", default_config))
    return config_path.parent / "rag.sqlite3"


def _metadata_map(metadata: list[dict] | None) -> dict[str, object]:
    result = {}
    for item in metadata or []:
        value = item.get("string_value")
        if value is None:
            value = item.get("numeric_value")
        result[item.get("key", "")] = value
    return result


def _metadata_flag(value: object) -> int:
    """Normalize a producer-owned metadata flag for durable SQLite storage."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value == 1)
    if isinstance(value, str):
        return int(value.strip().casefold() in {"1", "true", "yes", "on"})
    return 0


@dataclass(frozen=True)
class LexicalHit:
    document_key: str
    channel_name: str
    festival: int | None
    source_url: str
    excerpt: str
    score: int
    authority: str = ""
    status: str = ""
    source_type: str = ""
    access_scope: str = ""
    channel_id: int | None = None
    start_at: str = ""
    end_at: str = ""
    content_sha256: str = ""


class RagStore:
    def __init__(self, path: Path | None = None):
        self.path = path or default_rag_db_path()
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            self.path,
            timeout=SQLITE_CONNECT_TIMEOUT_SECONDS,
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
        return connection

    @staticmethod
    def _is_busy_error(exc: sqlite3.OperationalError) -> bool:
        message = str(exc).casefold()
        return "locked" in message or "busy" in message

    def _write(self, operation):
        """Run one SQLite transaction with bounded retries for lock contention."""
        for attempt in range(SQLITE_WRITE_ATTEMPTS):
            try:
                with self._lock, self._connect() as connection:
                    return operation(connection)
            except sqlite3.OperationalError as exc:
                if not self._is_busy_error(exc) or attempt == SQLITE_WRITE_ATTEMPTS - 1:
                    raise
                time.sleep(SQLITE_RETRY_BASE_SECONDS * (2**attempt))
        raise AssertionError("unreachable")

    def _initialize(self):
        def initialize(connection):
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS documents (
                    document_key TEXT PRIMARY KEY,
                    remote_name TEXT,
                    guild_id INTEGER NOT NULL,
                    channel_id INTEGER,
                    channel_name TEXT,
                    parent_id INTEGER,
                    parent_name TEXT,
                    category_id INTEGER,
                    category_name TEXT,
                    category TEXT,
                    festival INTEGER,
                    source_type TEXT,
                    status TEXT,
                    authority TEXT,
                    access_scope TEXT,
                    auto_promote INTEGER NOT NULL DEFAULT 0,
                    schema TEXT,
                    source_url TEXT,
                    start_at TEXT,
                    end_at TEXT,
                    upload_payload_sha256 TEXT,
                    body TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS documents_guild_festival
                    ON documents(guild_id, festival);
                CREATE INDEX IF NOT EXISTS documents_remote_name
                    ON documents(remote_name);
                CREATE TABLE IF NOT EXISTS rag_store_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
                    body,
                    content='documents',
                    content_rowid='rowid',
                    tokenize='trigram'
                );
                CREATE TRIGGER IF NOT EXISTS documents_fts_after_insert
                AFTER INSERT ON documents BEGIN
                    INSERT INTO documents_fts(rowid, body) VALUES (new.rowid, new.body);
                END;
                CREATE TRIGGER IF NOT EXISTS documents_fts_after_delete
                AFTER DELETE ON documents BEGIN
                    INSERT INTO documents_fts(documents_fts, rowid, body)
                    VALUES ('delete', old.rowid, old.body);
                END;
                CREATE TRIGGER IF NOT EXISTS documents_fts_after_update
                AFTER UPDATE OF body ON documents BEGIN
                    INSERT INTO documents_fts(documents_fts, rowid, body)
                    VALUES ('delete', old.rowid, old.body);
                    INSERT INTO documents_fts(rowid, body) VALUES (new.rowid, new.body);
                END;
                CREATE TABLE IF NOT EXISTS queries (
                    id TEXT PRIMARY KEY,
                    guild_id INTEGER NOT NULL,
                    actor_id INTEGER,
                    channel_id INTEGER,
                    question_hash TEXT NOT NULL,
                    question_preview TEXT,
                    answer_preview TEXT,
                    festival INTEGER,
                    metadata_filter TEXT,
                    citations_json TEXT NOT NULL,
                    lexical_keys_json TEXT NOT NULL,
                    latency_ms INTEGER NOT NULL,
                    no_answer INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS queries_guild_created
                    ON queries(guild_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS response_messages (
                    message_id INTEGER PRIMARY KEY,
                    query_id TEXT NOT NULL,
                    guild_id INTEGER NOT NULL,
                    channel_id INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS feedback (
                    query_id TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    rating TEXT NOT NULL,
                    emoji TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(query_id, user_id)
                );
                CREATE TABLE IF NOT EXISTS pending_document_deletions (
                    remote_name TEXT PRIMARY KEY,
                    document_key TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS pending_deletions_document
                    ON pending_document_deletions(document_key);
                CREATE TABLE IF NOT EXISTS pending_upload_operations (
                    document_key TEXT PRIMARY KEY,
                    store_name TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    body TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    superseded_remote_names_json TEXT NOT NULL,
                    operation_name TEXT,
                    remote_name TEXT,
                    state TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS pending_upload_operations_state
                    ON pending_upload_operations(state, updated_at);
                """
            )
            columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(documents)").fetchall()
            }
            migrations = {
                "category": "TEXT",
                "parent_id": "INTEGER",
                "parent_name": "TEXT",
                "category_id": "INTEGER",
                "category_name": "TEXT",
                "access_scope": "TEXT",
                "auto_promote": "INTEGER NOT NULL DEFAULT 0",
                "schema": "TEXT",
                "upload_payload_sha256": "TEXT",
            }
            for column, sql_type in migrations.items():
                if column not in columns:
                    connection.execute(
                        f"ALTER TABLE documents ADD COLUMN {column} {sql_type}"
                    )

            # ``category`` is the original category-name field.  Keep it in
            # sync with the explicit lineage name so existing databases and
            # older callers remain readable while ID-based policy checks use
            # the new fields.
            connection.execute(
                """
                UPDATE documents SET category_name = category
                WHERE coalesce(category_name, '') = ''
                  AND coalesce(category, '') != ''
                """
            )
            connection.execute(
                """
                UPDATE documents SET category = category_name
                WHERE coalesce(category, '') = ''
                  AND coalesce(category_name, '') != ''
                """
            )
            indexed_version = connection.execute(
                "SELECT value FROM rag_store_meta WHERE key = 'documents_fts_version'"
            ).fetchone()
            if indexed_version is None or indexed_version["value"] != DOCUMENTS_FTS_INDEX_VERSION:
                # Existing installations predate the FTS triggers. Rebuild the
                # index exactly once; subsequent writes are maintained by the
                # triggers above. This turns every RAG question from a full
                # lower(body) scan into an indexed lookup.
                connection.execute(
                    "INSERT INTO documents_fts(documents_fts) VALUES ('rebuild')"
                )
                connection.execute(
                    """
                    INSERT INTO rag_store_meta(key, value)
                    VALUES ('documents_fts_version', ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    (DOCUMENTS_FTS_INDEX_VERSION,),
                )

        self._write(initialize)

    def upsert_document(
        self,
        remote_name: str,
        text: str,
        metadata: list[dict] | None,
        *,
        superseded_remote_names: list[str] | tuple[str, ...] = (),
        completed_upload_content_sha256: str | None = None,
        upload_payload_sha256: str | None = None,
    ):
        """Persist a remote document and its replacement cleanup intent atomically.

        File Search upload happens before this local commit.  Recording the new
        mirror row and every superseded remote in one SQLite transaction means a
        process crash cannot leave the new row durable without also leaving the
        old remote queued for deletion.
        """
        values = _metadata_map(metadata)
        document_key = str(values.get("document_key") or hashlib.sha256(text.encode()).hexdigest()[:24])
        category_name = str(values.get("category_name") or values.get("category") or "")
        legacy_category = str(values.get("category") or category_name)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        pending = tuple(
            dict.fromkeys(
                str(name)
                for name in superseded_remote_names
                if name and str(name) != remote_name
            )
        )

        def upsert(connection):
            pending_upload_exists = False
            if completed_upload_content_sha256 is not None:
                upload = connection.execute(
                    """
                    SELECT content_sha256, payload_sha256
                    FROM pending_upload_operations WHERE document_key = ?
                    """,
                    (document_key,),
                ).fetchone()
                if upload is None:
                    existing = connection.execute(
                        """
                        SELECT remote_name, body, upload_payload_sha256
                        FROM documents WHERE document_key = ?
                        """,
                        (document_key,),
                    ).fetchone()
                    if not (
                        existing is not None
                        and existing["remote_name"] == remote_name
                        and existing["body"] == text
                        and existing["upload_payload_sha256"]
                        == upload_payload_sha256
                    ):
                        raise RuntimeError(
                            "Completed File Search upload intent is missing"
                        )
                elif (
                    upload["content_sha256"] != completed_upload_content_sha256
                    or upload["payload_sha256"] != upload_payload_sha256
                ):
                    raise RuntimeError(
                        "Completed File Search upload intent has changed"
                    )
                else:
                    pending_upload_exists = True

            connection.execute(
                """
                INSERT INTO documents (
                    document_key, remote_name, guild_id, channel_id, channel_name,
                    parent_id, parent_name, category_id, category_name, category,
                    festival, source_type, status, authority, source_url,
                    access_scope, auto_promote, schema, start_at, end_at,
                    upload_payload_sha256, body, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(document_key) DO UPDATE SET
                    remote_name=excluded.remote_name,
                    guild_id=excluded.guild_id,
                    channel_id=excluded.channel_id,
                    channel_name=excluded.channel_name,
                    parent_id=excluded.parent_id,
                    parent_name=excluded.parent_name,
                    category_id=excluded.category_id,
                    category_name=excluded.category_name,
                    category=excluded.category,
                    festival=excluded.festival,
                    source_type=excluded.source_type,
                    status=excluded.status,
                    authority=excluded.authority,
                    access_scope=excluded.access_scope,
                    auto_promote=excluded.auto_promote,
                    schema=excluded.schema,
                    source_url=excluded.source_url,
                    start_at=excluded.start_at,
                    end_at=excluded.end_at,
                    upload_payload_sha256=excluded.upload_payload_sha256,
                    body=excluded.body,
                    updated_at=excluded.updated_at
                """,
                (
                    document_key,
                    remote_name,
                    int(values.get("guild_id") or 0),
                    int(values["channel_id"]) if values.get("channel_id") else None,
                    str(values.get("channel_name") or ""),
                    int(values["parent_id"]) if values.get("parent_id") else None,
                    str(values.get("parent_name") or ""),
                    int(values["category_id"]) if values.get("category_id") else None,
                    category_name,
                    legacy_category,
                    int(values["festival"]) if values.get("festival") is not None else None,
                    str(values.get("source_type") or values.get("source") or "unknown"),
                    str(values.get("status") or "raw"),
                    str(values.get("authority") or "unknown"),
                    str(values.get("source_url") or ""),
                    str(values.get("access_scope") or ""),
                    _metadata_flag(values.get("auto_promote")),
                    str(values.get("schema") or ""),
                    str(values.get("start_at") or ""),
                    str(values.get("end_at") or ""),
                    upload_payload_sha256,
                    text,
                    now,
                ),
            )
            connection.executemany(
                """
                INSERT OR REPLACE INTO pending_document_deletions
                    (remote_name, document_key, created_at)
                VALUES (?, ?, ?)
                """,
                ((name, document_key, now) for name in pending),
            )
            if pending_upload_exists:
                resolved = connection.execute(
                    """
                    DELETE FROM pending_upload_operations
                    WHERE document_key = ? AND content_sha256 = ?
                      AND payload_sha256 = ?
                    """,
                    (
                        document_key,
                        completed_upload_content_sha256,
                        upload_payload_sha256,
                    ),
                )
                if resolved.rowcount != 1:
                    raise RuntimeError(
                        "Completed File Search upload intent disappeared"
                    )

        self._write(upsert)

    @staticmethod
    def upload_payload_sha256(
        store_name: str,
        display_name: str,
        text: str,
        metadata: list[dict] | None,
    ) -> str:
        """Return the stable identity of one non-idempotent upload request."""
        serialized = json.dumps(
            {
                "store_name": store_name,
                "display_name": display_name,
                "body": text,
                "metadata": list(metadata or ()),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def claim_upload_operation(
        self,
        *,
        document_key: str,
        store_name: str,
        display_name: str,
        text: str,
        metadata: list[dict] | None,
        superseded_remote_names: list[str] | tuple[str, ...] = (),
    ) -> tuple[dict, bool]:
        """Durably claim an upload identity before its non-idempotent request.

        The returned boolean is true only for the process that inserted the
        claim. Other processes must resume/reconcile the returned record and
        must not start another upload.
        """
        if not document_key:
            raise ValueError("document_key is required for a durable upload claim")
        metadata_json = json.dumps(
            list(metadata or ()),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        content_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
        payload_sha256 = self.upload_payload_sha256(
            store_name,
            display_name,
            text,
            metadata,
        )
        requested_superseded = tuple(
            dict.fromkeys(str(name) for name in superseded_remote_names if name)
        )
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")

        def claim(connection):
            inserted = connection.execute(
                """
                INSERT OR IGNORE INTO pending_upload_operations (
                    document_key, store_name, display_name, content_sha256,
                    payload_sha256, body, metadata_json,
                    superseded_remote_names_json, operation_name, remote_name,
                    state, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, 'initiating', ?, ?)
                """,
                (
                    document_key,
                    store_name,
                    display_name,
                    content_sha256,
                    payload_sha256,
                    text,
                    metadata_json,
                    json.dumps(requested_superseded),
                    now,
                    now,
                ),
            ).rowcount == 1
            row = connection.execute(
                "SELECT * FROM pending_upload_operations WHERE document_key = ?",
                (document_key,),
            ).fetchone()
            if row is None:
                raise RuntimeError("Failed to persist File Search upload intent")
            result = dict(row)
            if result["payload_sha256"] == payload_sha256:
                existing_superseded = json.loads(
                    result["superseded_remote_names_json"] or "[]"
                )
                merged = tuple(
                    dict.fromkeys([*existing_superseded, *requested_superseded])
                )
                if list(merged) != existing_superseded:
                    encoded = json.dumps(merged)
                    connection.execute(
                        """
                        UPDATE pending_upload_operations
                        SET superseded_remote_names_json = ?, updated_at = ?
                        WHERE document_key = ? AND payload_sha256 = ?
                        """,
                        (encoded, now, document_key, payload_sha256),
                    )
                    result["superseded_remote_names_json"] = encoded
            return result, inserted

        return self._write(claim)

    def get_upload_operation(self, document_key: str) -> dict | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM pending_upload_operations WHERE document_key = ?",
                (document_key,),
            ).fetchone()
        return dict(row) if row is not None else None

    def record_upload_operation(
        self,
        document_key: str,
        payload_sha256: str,
        operation_name: str,
    ) -> None:
        if not operation_name:
            raise ValueError("operation_name is required")
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")

        def record(connection):
            updated = connection.execute(
                """
                UPDATE pending_upload_operations
                SET operation_name = ?, state = 'accepted', updated_at = ?
                WHERE document_key = ? AND payload_sha256 = ?
                """,
                (operation_name, now, document_key, payload_sha256),
            )
            if updated.rowcount != 1:
                raise RuntimeError("File Search upload intent is missing or changed")

        self._write(record)

    def record_upload_result(
        self,
        document_key: str,
        payload_sha256: str,
        remote_name: str,
    ) -> None:
        if not remote_name:
            raise ValueError("remote_name is required")
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")

        def record(connection):
            updated = connection.execute(
                """
                UPDATE pending_upload_operations
                SET remote_name = ?, state = 'completed', updated_at = ?
                WHERE document_key = ? AND payload_sha256 = ?
                """,
                (remote_name, now, document_key, payload_sha256),
            )
            if updated.rowcount != 1:
                raise RuntimeError("File Search upload intent is missing or changed")

        self._write(record)

    def resolve_upload_operation(
        self,
        document_key: str,
        payload_sha256: str,
    ) -> bool:
        """Remove a rejected/failed intent only when its identity still matches."""
        def resolve(connection):
            deleted = connection.execute(
                """
                DELETE FROM pending_upload_operations
                WHERE document_key = ? AND payload_sha256 = ?
                """,
                (document_key, payload_sha256),
            )
            return deleted.rowcount == 1

        return bool(self._write(resolve))

    def delete_remote_documents(self, remote_names: list[str]):
        if not remote_names:
            return
        placeholders = ",".join("?" for _ in remote_names)

        def delete(connection):
            connection.execute(
                f"DELETE FROM documents WHERE remote_name IN ({placeholders})",  # noqa: S608
                remote_names,
            )

        self._write(delete)

    def delete_source_documents(self, source_type: str):
        """Replace a code-shipped source without touching synced Knowledge documents."""
        def delete(connection):
            connection.execute("DELETE FROM documents WHERE source_type = ?", (source_type,))

        self._write(delete)

    def queue_document_deletion(self, document_key: str, remote_name: str) -> None:
        """Remember a superseded File Search document until deletion succeeds."""
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")

        def queue(connection):
            connection.execute(
                """
                INSERT OR REPLACE INTO pending_document_deletions
                    (remote_name, document_key, created_at)
                VALUES (?, ?, ?)
                """,
                (remote_name, document_key, now),
            )

        self._write(queue)

    def pending_document_deletions(self, document_key: str | None = None) -> list[str]:
        with self._lock, self._connect() as connection:
            if document_key is None:
                rows = connection.execute(
                    """
                    SELECT remote_name FROM pending_document_deletions
                    ORDER BY created_at, remote_name
                    """
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT remote_name FROM pending_document_deletions
                    WHERE document_key = ? ORDER BY created_at, remote_name
                    """,
                    (document_key,),
                ).fetchall()
        return [str(row["remote_name"]) for row in rows]

    def resolve_document_deletions(self, remote_names: list[str]) -> None:
        if not remote_names:
            return
        placeholders = ",".join("?" for _ in remote_names)

        def resolve(connection):
            connection.execute(
                f"DELETE FROM pending_document_deletions "  # noqa: S608
                f"WHERE remote_name IN ({placeholders})",
                remote_names,
            )

        self._write(resolve)

    def get_document(self, document_key: str) -> dict | None:
        """Return the local mirror row used to make remote replacement idempotent."""
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM documents WHERE document_key = ?", (document_key,)
            ).fetchone()
        return dict(row) if row is not None else None

    def restore_document_snapshots(
        self,
        document_keys: list[str] | tuple[str, ...],
        snapshots: list[dict] | tuple[dict, ...],
    ) -> None:
        """Atomically restore mirror rows after a failed remote generation.

        Uploading a source generation updates the exact-match mirror one document
        at a time.  If a later upload fails, removing the successful new remotes
        would otherwise leave those document keys missing locally.  Replacing the
        affected key set and its pre-upload snapshots in one transaction restores
        the complete previous generation without exposing a partially restored
        mirror to readers.
        """
        keys = tuple(dict.fromkeys(str(key) for key in document_keys if key))
        if not keys:
            return
        allowed_keys = set(keys)
        rows = tuple(snapshot for snapshot in snapshots if snapshot is not None)
        if any(str(row.get("document_key") or "") not in allowed_keys for row in rows):
            raise ValueError("Snapshot document key is outside the restore set")

        placeholders = ",".join("?" for _ in keys)
        columns = (
            "document_key",
            "remote_name",
            "guild_id",
            "channel_id",
            "channel_name",
            "parent_id",
            "parent_name",
            "category_id",
            "category_name",
            "category",
            "festival",
            "source_type",
            "status",
            "authority",
            "access_scope",
            "auto_promote",
            "schema",
            "source_url",
            "start_at",
            "end_at",
            "upload_payload_sha256",
            "body",
            "updated_at",
        )
        value_placeholders = ",".join("?" for _ in columns)

        def restore(connection):
            connection.execute(
                f"DELETE FROM documents WHERE document_key IN ({placeholders})",  # noqa: S608
                keys,
            )
            connection.executemany(
                f"INSERT INTO documents ({','.join(columns)}) "  # noqa: S608
                f"VALUES ({value_placeholders})",
                (tuple(row.get(column) for column in columns) for row in rows),
            )

        self._write(restore)

    def document_references(self, guild_id: int) -> list[dict]:
        """Return the minimal local mirror identity needed for drift audits."""
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT document_key, remote_name, guild_id, source_type
                FROM documents WHERE guild_id IN (?, 0)
                ORDER BY document_key
                """,
                (guild_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def backfill_inventory(self, guild_id: int) -> list[dict]:
        """Build the trusted Discord replacement inventory from SQLite.

        The scheduled six-hour refresh used to enumerate every remote File
        Search document before reading a single Discord channel. The API is
        capped at 20 documents per page, so a corpus with thousands of chunks
        generated hundreds of HTTP calls and temporarily starved the bot.
        Every upload is committed to this mirror before its cursor advances;
        use that durable identity for routine incremental replacement. An
        explicit rebuild still performs the remote reconciliation path.
        """
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT document_key, remote_name, guild_id, channel_id,
                       channel_name, source_type, status, authority,
                       access_scope, schema, start_at, end_at
                FROM documents
                WHERE guild_id = ?
                  AND source_type = 'discord_conversation'
                  AND channel_id IS NOT NULL
                  AND remote_name IS NOT NULL
                  AND remote_name != ''
                ORDER BY document_key
                """,
                (guild_id,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            metadata = {
                key: item[key]
                for key in (
                    "document_key",
                    "guild_id",
                    "channel_id",
                    "channel_name",
                    "source_type",
                    "status",
                    "authority",
                    "access_scope",
                    "schema",
                    "start_at",
                    "end_at",
                )
                if item.get(key) not in (None, "")
            }
            result.append(
                {
                    "name": item["remote_name"],
                    "display_name": f"#{item.get('channel_name') or item['channel_id']}",
                    "metadata": metadata,
                    "state": "ACTIVE",
                }
            )
        return result

    def list_curation_documents(self, *, limit: int = 1000) -> list[dict]:
        """Return raw, locally mirrored source documents eligible for draft curation."""
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM documents
                WHERE source_type IN ('discord_conversation', 'meeting_transcript')
                  AND authority != 'curated'
                  AND length(trim(body)) >= 200
                ORDER BY coalesce(start_at, updated_at), document_key
                LIMIT ?
                """,
                (max(1, min(limit, 10_000)),),
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def query_terms(question: str, glossary: dict | None = None) -> list[str]:
        terms = []
        normalized = question.casefold()
        for label, entry in (glossary or {}).items():
            aliases = entry.get("aliases", []) if isinstance(entry, dict) else []
            for value in [label, *aliases]:
                if value and value.casefold() in normalized:
                    terms.extend([label, *aliases])
                    break
        terms.extend(re.findall(r"[A-Za-z][A-Za-z0-9_.:/-]{2,}|[0-9a-f]{7,40}", question))
        stripped = re.sub(r"[？?！!。、,\s]", "", question)
        stripped = re.sub(r"(?:について|とは|を教えて|教えてください|知りたい|どうなっている|どうなってる)$", "", stripped)
        # Japanese questions have no whitespace, so an entire sentence is too
        # specific for exact matching. Keep subjects such as ``ITラボ`` and
        # ``整理券管理者`` as lexical fallback terms.
        segments = re.split(
            r"(?:について|として|では|には|から|まで|より|って|とは|の|は|が|を|に|で|(?<!こ)と|も|へ)",
            stripped,
        )
        stop_terms = {
            "これら",
            "それら",
            "こちら",
            "そちら",
            "どちら",
            "お願い",
            "今回",
            "現在",
            "読み方",
            "施設概要",
            "概要",
            "意味",
            *_LOW_INFORMATION_QUERY_TERMS,
        }
        for segment in segments:
            segment = re.sub(
                r"(?:どんなこと|どこ|いつ|だれ|誰|なぜ|どう|何|教えて|ください|"
                r"されていますか|していましたか|されますか|しますか|ですか|でしたか).*$",
                "",
                segment,
            )
            if (
                3 <= len(segment) <= 24
                or segment in _HIGH_INFORMATION_SHORT_QUERY_TERMS
            ) and segment not in stop_terms:
                terms.append(segment)
                # Preserve a compound for precision, but also expose its
                # subject when a common operational suffix would otherwise
                # hide exact matches such as ``整理券`` inside ``整理券運用``.
                compound = re.fullmatch(
                    r"(.{2,})(運用|問題点|課題|保存先|開催状況)",
                    segment,
                )
                if compound:
                    # Keep the concrete subject and the compound itself.  A
                    # suffix such as ``運用`` occurs in almost every internal
                    # document and lets large source families crowd out the
                    # actual meeting/conversation evidence.
                    terms.append(compound.group(1))
        # Preserve short, information-bearing domain cues that the generic
        # Japanese particle splitter would otherwise drop or glue together.
        # These are vocabulary classes rather than answers; they only improve
        # deterministic recovery when semantic File Search is unavailable.
        cue_pattern = re.compile(
            r"新歓(?:Web)?|大賞企画|Y-?1(?:グランプリ)?|ランキング|ranking|"
            r"Firestore|firestore|map|外部|他局|要求|票数|得票|順位|構造",
            re.IGNORECASE,
        )
        terms.extend(match.group(0) for match in cue_pattern.finditer(question))
        # The festival number already drives the metadata filter in the RAG
        # pipeline.  Treating ``27th`` itself as an exact-search subject makes
        # the operating-year reference appear beside unrelated 27th topics.
        # Keep it only when the question is genuinely about period mapping.
        asks_about_period = re.search(
            r"(?:年度(?:区分)?|回次|期間|いつから|いつまで|開始日|終了日|切り替|何th)",
            question,
            re.IGNORECASE,
        )
        if not asks_about_period:
            terms = [
                term
                for term in terms
                if not re.fullmatch(r"(?:第)?\d{1,3}(?:th|回)", term, re.IGNORECASE)
            ]
        unique = []
        seen = set()
        for term in terms:
            term = term.strip()
            if term in _LOW_INFORMATION_QUERY_TERMS:
                continue
            normalized_term = term.casefold()
            if term and normalized_term not in seen:
                seen.add(normalized_term)
                unique.append(term)
        return unique[:12]

    def search_exact(
        self,
        guild_id: int,
        terms: list[str],
        *,
        festival: int | None = None,
        target_date: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        allowed_channel_ids: tuple[int, ...] | list[int] | set[int] | None = None,
        allow_unrestricted_conversations: bool = False,
        source_types: tuple[str, ...] | list[str] | set[str] | None = None,
        question_kind: str | None = None,
        limit: int = 4,
    ) -> list[LexicalHit]:
        clean_terms = [term for term in terms if len(term) >= 2][:12]
        is_history = str(question_kind or "").casefold() == "history"
        if target_date is not None and (date_from is not None or date_to is not None):
            raise ValueError("target_date and date range are mutually exclusive")
        for label, value in (("date_from", date_from), ("date_to", date_to)):
            if value is not None:
                try:
                    datetime.strptime(value, "%Y-%m-%d")
                except ValueError as exc:
                    raise ValueError(f"{label} must be YYYY-MM-DD") from exc
        if date_from is not None and date_to is not None and date_from > date_to:
            raise ValueError("date_from must not be after date_to")
        if not clean_terms and date_from is None and date_to is None:
            return []
        # guild_id=0 is reserved for reviewed references shipped with the bot.
        # Those records are safe to share across configured guilds.
        conditions = ["(guild_id = ? OR guild_id = 0)"]
        params: list[object] = [guild_id]
        if not allow_unrestricted_conversations:
            from bot.discord_visibility import normalized_channel_ids

            channel_ids = normalized_channel_ids(allowed_channel_ids)
            visibility = ["access_scope = 'guild'"]
            if channel_ids:
                placeholders = ",".join("?" for _ in channel_ids)
                visibility.append(
                    f"(access_scope = 'channel' AND channel_id IN ({placeholders}))"
                )
                params.extend(channel_ids)
            conditions.append("(" + " OR ".join(visibility) + ")")
        if festival is not None:
            # Explicitly reviewed/curated documents with no festival are
            # year-independent contracts. Raw unlabelled guild documents stay
            # excluded from year-specific questions.
            conditions.append(
                "(festival = ? OR (festival IS NULL AND authority IN "
                "('canonical', 'curated', 'reviewed_record') AND status IN "
                "('active', 'historical', 'approved', 'verified', 'current') AND "
                "source_type IN ('builtin_reference', 'knowledge_repository')))"
            )
            params.append(festival)
        if target_date is not None:
            conditions.append("substr(start_at, 1, 10) = ?")
            params.append(target_date)
        if date_from is not None:
            conditions.append(
                "coalesce(nullif(substr(end_at, 1, 10), ''), "
                "nullif(substr(start_at, 1, 10), '')) >= ?"
            )
            params.append(date_from)
        if date_to is not None:
            conditions.append(
                "coalesce(nullif(substr(start_at, 1, 10), ''), "
                "nullif(substr(end_at, 1, 10), '')) <= ?"
            )
            params.append(date_to)
        selected_source_types = tuple(
            dict.fromkeys(
                value
                for value in (str(item).strip() for item in source_types or ())
                if value and re.fullmatch(r"[a-z0-9_-]{1,64}", value)
            )
        )
        if source_types is not None:
            if not selected_source_types:
                return []
            placeholders = ",".join("?" for _ in selected_source_types)
            conditions.append(f"source_type IN ({placeholders})")
            params.extend(selected_source_types)
        normalized_kind: QuestionKind | None = None
        if question_kind is not None:
            try:
                normalized_kind = QuestionKind(str(question_kind).strip().casefold())
            except ValueError:
                normalized_kind = None
        authorities = (
            direct_authorities(normalized_kind)
            if normalized_kind is not None
            else ()
        )
        current_implementation_authorities = sorted(
            CURRENT_IMPLEMENTATION_AUTHORITIES.intersection(authorities)
        )
        if (
            normalized_kind is not None
            and not allows_unreviewed_evidence(normalized_kind)
            and current_implementation_authorities
        ):
            authority_placeholders = ",".join(
                "?" for _ in current_implementation_authorities
            )
            status_placeholders = ",".join("?" for _ in CURRENT_STATUSES)
            conditions.append(
                f"(authority NOT IN ({authority_placeholders}) "
                f"OR status IN ({status_placeholders}))"
            )
            params.extend(current_implementation_authorities)
            params.extend(sorted(CURRENT_STATUSES))
        if normalized_kind is not None and not allows_unreviewed_evidence(normalized_kind):
            # The caller applies the same direct-evidence policy after this
            # query. Apply it before SQLite's candidate LIMIT as well, or a
            # large repository-history generation can crowd every current
            # schema/implementation record out of the candidate set before
            # the caller gets a chance to reject the history rows.
            placeholders = ",".join("?" for _ in authorities)
            conditions.append(f"authority IN ({placeholders})")
            params.extend(authorities)
            conditions.append("status NOT IN ('draft', 'raw', 'deprecated')")
        # FTS5's trigram tokenizer supports substring search for Japanese and
        # Latin text while avoiding a full scan of every document body. Terms
        # shorter than three characters cannot be represented by the trigram
        # index; retain the legacy scan only when no indexed term is available.
        indexed_terms = [term for term in clean_terms if len(term) >= 3]
        use_fts = bool(indexed_terms)
        if use_fts:
            fts_query = " OR ".join(
                f'"{term.replace(chr(34), chr(34) * 2)}"' for term in indexed_terms
            )
            conditions.append("documents_fts MATCH ?")
            params.append(fts_query)
        elif clean_terms:
            term_conditions = []
            for term in clean_terms:
                term_conditions.append("instr(lower(body), lower(?)) > 0")
                params.append(term)
            conditions.append("(" + " OR ".join(term_conditions) + ")")
        # Rank lexical relevance in SQLite *before* applying LIMIT.  The old
        # query first selected ``limit * 4`` recent/authoritative rows and only
        # then scored them in Python.  Repositories with many history chunks
        # could therefore starve a much more relevant Discord or meeting
        # record before it was ever considered.
        relevance_parts = []
        relevance_params: list[object] = []
        body_expression = "d.body" if use_fts else "body"
        for term in clean_terms:
            weight = 100 + min(len(term), 24) * 4
            relevance_parts.append(
                "CASE WHEN instr(lower("
                f"{body_expression}), lower(?)) > 0 THEN {weight} ELSE 0 END"
            )
            relevance_params.append(term)
        # Bare ``ORDER BY 0`` is parsed as a result-column ordinal by SQLite.
        relevance_sql = (
            " + ".join(relevance_parts) if relevance_parts else "CAST(0 AS INTEGER)"
        )
        order_sql = (
            f" ORDER BY ({relevance_sql}) DESC, CASE authority "
            + "WHEN 'canonical' THEN 3 WHEN 'curated' THEN 2 "
            + "WHEN 'reviewed_record' THEN 1 ELSE 0 END DESC, "
            + "CASE WHEN status IN ('active', 'historical', 'approved', 'verified', 'current') "
            + "THEN 1 ELSE 0 END DESC, end_at DESC LIMIT ?"
        )
        candidate_limit = max(64, min(limit * 8, 2_048))

        def fetch_rows(
            extra_condition: str = "",
            extra_params: tuple[object, ...] = (),
            *,
            row_limit: int = candidate_limit,
        ):
            where = list(conditions)
            if extra_condition:
                where.append(extra_condition)
            source_sql = (
                "documents AS d JOIN documents_fts ON documents_fts.rowid = d.rowid"
                if use_fts
                else "documents"
            )
            select_sql = "SELECT d.*" if use_fts else "SELECT *"
            sql = (
                select_sql
                + " FROM "
                + source_sql
                + " WHERE "
                + " AND ".join(where)
                + order_sql
            )
            query_params = [
                *params,
                *extra_params,
                *relevance_params,
                row_limit,
            ]
            return connection.execute(sql, query_params).fetchall()

        with self._lock, self._connect() as connection:
            rows = list(fetch_rows())
            if is_history and limit >= 2:
                # Keep this SQL equivalent to is_history_narrative_source().
                # SQLite cannot call the Python predicate used after retrieval.
                record_rows = fetch_rows(
                    "(source_type = ? OR substr(source_type, 1, 8) = 'meeting_')",
                    ("discord_conversation",),
                    row_limit=max(8, limit * 2),
                )
                known_keys = {row["document_key"] for row in rows}
                rows.extend(
                    row for row in record_rows if row["document_key"] not in known_keys
                )
            elif (
                normalized_kind is not None
                and not allows_unreviewed_evidence(normalized_kind)
            ):
                # Preserve a bounded lane for source snapshots that are known
                # to represent the deployed implementation.  Relevance-first
                # SQL retrieval can otherwise fill its candidate window with
                # many longer repository-context chunks before the authority
                # ranker below ever sees the deployed record.
                deployed_rows = fetch_rows(
                    "authority = ? AND status = ?",
                    ("deployed_implementation", "deployed"),
                    row_limit=max(8, limit * 2),
                )
                known_keys = {row["document_key"] for row in rows}
                rows.extend(
                    row for row in deployed_rows if row["document_key"] not in known_keys
                )
        hits = []
        for row in rows:
            body = row["body"]
            positions = [body.casefold().find(term.casefold()) for term in clean_terms]
            positions = [position for position in positions if position >= 0]
            start = max(0, (min(positions) if positions else 0) - 220)
            excerpt = body[start:start + 900].strip()
            matched_terms = sum(
                100 + min(len(term), 24) * 4
                for term in clean_terms
                if term.casefold() in body.casefold()
            )
            authority_bonus = {
                "canonical": 30,
                "curated": 20,
                "reviewed_record": 10,
            }.get(row["authority"], 0)
            status_bonus = 10 if row["status"] in {
                "active", "historical", "approved", "verified", "current"
            } else 0
            hits.append(LexicalHit(
                document_key=row["document_key"],
                channel_name=row["channel_name"] or "",
                festival=row["festival"],
                source_url=row["source_url"] or "",
                excerpt=excerpt,
                score=matched_terms + authority_bonus + status_bonus,
                authority=row["authority"] or "",
                status=row["status"] or "",
                source_type=row["source_type"] or "",
                access_scope=row["access_scope"] or "",
                channel_id=row["channel_id"],
                start_at=row["start_at"] or "",
                end_at=row["end_at"] or "",
                content_sha256=hashlib.sha256(body.encode("utf-8")).hexdigest(),
            ))
        if (
            normalized_kind is not None
            and not allows_unreviewed_evidence(normalized_kind)
        ):
            authority_ranked = sorted(
                hits,
                key=lambda hit: (
                    can_answer_directly(
                        normalized_kind,
                        hit.authority,
                        hit.status,
                    ),
                    authority_score(
                        normalized_kind,
                        hit.authority,
                        hit.status,
                    ),
                    hit.score,
                ),
                reverse=True,
            )
            selected: list[LexicalHit] = []
            selected_keys: set[str] = set()
            selected_authorities: set[str] = set()
            for hit in authority_ranked:
                if not can_answer_directly(
                    normalized_kind,
                    hit.authority,
                    hit.status,
                ) or hit.authority in selected_authorities:
                    continue
                selected.append(hit)
                selected_keys.add(hit.document_key)
                selected_authorities.add(hit.authority)
                if len(selected) >= limit:
                    return selected

            relevance_ranked = sorted(
                hits,
                key=lambda hit: (
                    can_answer_directly(
                        normalized_kind,
                        hit.authority,
                        hit.status,
                    ),
                    hit.score,
                    authority_score(
                        normalized_kind,
                        hit.authority,
                        hit.status,
                    ),
                ),
                reverse=True,
            )
            for hit in relevance_ranked:
                if hit.document_key in selected_keys:
                    continue
                selected.append(hit)
                selected_keys.add(hit.document_key)
                if len(selected) >= limit:
                    break
            hits = selected
        else:
            hits.sort(key=lambda hit: hit.score, reverse=True)
        if not is_history or limit < 2:
            return hits[:limit]

        # A HISTORY answer normally needs both the implementation change and
        # the discussion/meeting that explains why it happened. Reserve a
        # bounded share for the latter without weakening ACL, festival, date,
        # or term predicates used by both SQL queries above.
        record_hits = [
            hit for hit in hits if is_history_narrative_source(hit.source_type)
        ]
        reserve = min(len(record_hits), max(1, limit // 4))
        selected = record_hits[:reserve]
        selected_keys = {hit.document_key for hit in selected}
        selected.extend(
            hit
            for hit in hits
            if hit.document_key not in selected_keys
        )
        return sorted(selected[:limit], key=lambda hit: hit.score, reverse=True)

    def find_approved_topic(self, topic_id: str, *, festival: int) -> dict | None:
        """Find a reviewed Knowledge article that reconciles an open topic draft."""
        if not topic_id or not 1 <= festival <= 999:
            return None
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """SELECT * FROM documents
                   WHERE guild_id = 0
                     AND source_type = 'knowledge_repository'
                     AND authority IN ('canonical','curated','reviewed_record')
                     AND status IN ('active','historical','approved','verified','current')
                     AND festival = ?
                     AND instr(lower(body), lower(?)) > 0
                   ORDER BY updated_at DESC LIMIT 1""",
                (festival, topic_id),
            ).fetchone()
        return dict(row) if row else None

    def record_query(
        self,
        *,
        guild_id: int,
        actor_id: int | None,
        channel_id: int | None,
        question: str,
        answer: str,
        festival: int | None,
        metadata_filter: str | None,
        citations: list[str],
        lexical_keys: list[str],
        latency_ms: int,
        no_answer: bool,
    ) -> str:
        query_id = uuid.uuid4().hex
        keep_content = os.environ.get("YAGAPON_RAG_TRACE_CONTENT", "false").lower() == "true"

        def preview(value: str) -> str | None:
            return value[:1000] if keep_content else None

        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO queries (
                    id, guild_id, actor_id, channel_id, question_hash,
                    question_preview, answer_preview, festival, metadata_filter,
                    citations_json, lexical_keys_json, latency_ms, no_answer, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    query_id,
                    guild_id,
                    actor_id,
                    channel_id,
                    hashlib.sha256(question.encode()).hexdigest(),
                    preview(question),
                    preview(answer),
                    festival,
                    metadata_filter,
                    json.dumps(citations, ensure_ascii=False),
                    json.dumps(lexical_keys, ensure_ascii=False),
                    latency_ms,
                    int(no_answer),
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                ),
            )
        return query_id

    def bind_response_message(self, query_id: str, guild_id: int, channel_id: int, message_id: int):
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO response_messages VALUES (?, ?, ?, ?)",
                (message_id, query_id, guild_id, channel_id),
            )

    def record_feedback(self, message_id: int, user_id: int, emoji: str, rating: str) -> bool:
        with self._lock, self._connect() as connection:
            response = connection.execute(
                "SELECT query_id FROM response_messages WHERE message_id = ?", (message_id,)
            ).fetchone()
            if response is None:
                return False
            connection.execute(
                """
                INSERT INTO feedback VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(query_id, user_id) DO UPDATE SET
                    rating=excluded.rating, emoji=excluded.emoji, created_at=excluded.created_at
                """,
                (
                    response["query_id"],
                    user_id,
                    rating,
                    emoji,
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                ),
            )
        return True

    def remove_feedback(self, message_id: int, user_id: int, emoji: str) -> bool:
        """Remove a rating only when it matches the removed Discord reaction."""
        with self._lock, self._connect() as connection:
            response = connection.execute(
                "SELECT query_id FROM response_messages WHERE message_id = ?", (message_id,)
            ).fetchone()
            if response is None:
                return False
            cursor = connection.execute(
                "DELETE FROM feedback WHERE query_id = ? AND user_id = ? AND emoji = ?",
                (response["query_id"], user_id, emoji),
            )
        return cursor.rowcount > 0

    def summary(self, guild_id: int) -> dict:
        with self._lock, self._connect() as connection:
            query_row = connection.execute(
                """
                SELECT count(*) AS total,
                       sum(no_answer) AS no_answer,
                       avg(latency_ms) AS latency_ms
                FROM queries WHERE guild_id = ?
                """,
                (guild_id,),
            ).fetchone()
            feedback_rows = connection.execute(
                """
                SELECT feedback.rating, count(*) AS count
                FROM feedback JOIN queries ON queries.id = feedback.query_id
                WHERE queries.guild_id = ? GROUP BY feedback.rating
                """,
                (guild_id,),
            ).fetchall()
            documents = connection.execute(
                "SELECT count(*) FROM documents WHERE guild_id = ?", (guild_id,)
            ).fetchone()[0]
            festival_rows = connection.execute(
                """
                SELECT festival, count(*) AS count FROM documents
                WHERE guild_id = ? GROUP BY festival ORDER BY festival DESC
                """,
                (guild_id,),
            ).fetchall()
            source_rows = connection.execute(
                """
                SELECT source_type, status, count(*) AS count FROM documents
                WHERE guild_id = ? GROUP BY source_type, status ORDER BY count(*) DESC
                """,
                (guild_id,),
            ).fetchall()
        feedback = {row["rating"]: row["count"] for row in feedback_rows}
        rated = sum(feedback.values())
        return {
            "queries": query_row["total"] or 0,
            "no_answer": query_row["no_answer"] or 0,
            "average_latency_ms": round(query_row["latency_ms"] or 0),
            "feedback": feedback,
            "positive_rate": round(feedback.get("positive", 0) / rated, 3) if rated else None,
            "local_documents": documents,
            "documents_by_festival": {
                str(row["festival"]) if row["festival"] is not None else "unknown": row["count"]
                for row in festival_rows
            },
            "documents_by_source": [dict(row) for row in source_rows],
        }

    def recent_queries(self, guild_id: int, limit: int = 50) -> list[dict]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, actor_id, channel_id, question_preview, answer_preview,
                       festival, metadata_filter, citations_json, lexical_keys_json,
                       latency_ms, no_answer, created_at,
                       (SELECT count(*) FROM feedback WHERE feedback.query_id = queries.id
                          AND rating = 'positive') AS positive,
                       (SELECT count(*) FROM feedback WHERE feedback.query_id = queries.id
                          AND rating = 'partial') AS partial,
                       (SELECT count(*) FROM feedback WHERE feedback.query_id = queries.id
                          AND rating = 'negative') AS negative
                FROM queries WHERE guild_id = ? ORDER BY created_at DESC LIMIT ?
                """,
                (guild_id, limit),
            ).fetchall()
        return [
            {
                **dict(row),
                "citations": json.loads(row["citations_json"]),
                "lexical_keys": json.loads(row["lexical_keys_json"]),
                "no_answer": bool(row["no_answer"]),
            }
            for row in rows
        ]
