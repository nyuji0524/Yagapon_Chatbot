"""Local exact-match mirror and RAG quality telemetry."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


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


@dataclass(frozen=True)
class LexicalHit:
    document_key: str
    channel_name: str
    festival: int | None
    source_url: str
    excerpt: str
    score: int


class RagStore:
    def __init__(self, path: Path | None = None):
        self.path = path or default_rag_db_path()
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
                CREATE TABLE IF NOT EXISTS documents (
                    document_key TEXT PRIMARY KEY,
                    remote_name TEXT,
                    guild_id INTEGER NOT NULL,
                    channel_id INTEGER,
                    channel_name TEXT,
                    festival INTEGER,
                    source_type TEXT,
                    status TEXT,
                    authority TEXT,
                    source_url TEXT,
                    start_at TEXT,
                    end_at TEXT,
                    body TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS documents_guild_festival
                    ON documents(guild_id, festival);
                CREATE INDEX IF NOT EXISTS documents_remote_name
                    ON documents(remote_name);
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
                """
            )

    def upsert_document(self, remote_name: str, text: str, metadata: list[dict] | None):
        values = _metadata_map(metadata)
        document_key = str(values.get("document_key") or hashlib.sha256(text.encode()).hexdigest()[:24])
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO documents (
                    document_key, remote_name, guild_id, channel_id, channel_name,
                    festival, source_type, status, authority, source_url,
                    start_at, end_at, body, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(document_key) DO UPDATE SET
                    remote_name=excluded.remote_name,
                    guild_id=excluded.guild_id,
                    channel_id=excluded.channel_id,
                    channel_name=excluded.channel_name,
                    festival=excluded.festival,
                    source_type=excluded.source_type,
                    status=excluded.status,
                    authority=excluded.authority,
                    source_url=excluded.source_url,
                    start_at=excluded.start_at,
                    end_at=excluded.end_at,
                    body=excluded.body,
                    updated_at=excluded.updated_at
                """,
                (
                    document_key,
                    remote_name,
                    int(values.get("guild_id") or 0),
                    int(values["channel_id"]) if values.get("channel_id") else None,
                    str(values.get("channel_name") or ""),
                    int(values["festival"]) if values.get("festival") is not None else None,
                    str(values.get("source_type") or values.get("source") or "unknown"),
                    str(values.get("status") or "raw"),
                    str(values.get("authority") or "unknown"),
                    str(values.get("source_url") or ""),
                    str(values.get("start_at") or ""),
                    str(values.get("end_at") or ""),
                    text,
                    now,
                ),
            )

    def delete_remote_documents(self, remote_names: list[str]):
        if not remote_names:
            return
        placeholders = ",".join("?" for _ in remote_names)
        with self._lock, self._connect() as connection:
            connection.execute(
                f"DELETE FROM documents WHERE remote_name IN ({placeholders})",  # noqa: S608
                remote_names,
            )

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
        if 2 <= len(stripped) <= 40:
            terms.append(stripped)
        unique = []
        seen = set()
        for term in terms:
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
        limit: int = 4,
    ) -> list[LexicalHit]:
        clean_terms = [term for term in terms if len(term) >= 2][:12]
        if not clean_terms:
            return []
        conditions = ["guild_id = ?"]
        params: list[object] = [guild_id]
        if festival is not None:
            conditions.append("festival = ?")
            params.append(festival)
        term_conditions = []
        for term in clean_terms:
            term_conditions.append("instr(lower(body), lower(?)) > 0")
            params.append(term)
        conditions.append("(" + " OR ".join(term_conditions) + ")")
        params.append(limit * 4)
        sql = (
            "SELECT * FROM documents WHERE " + " AND ".join(conditions)
            + " ORDER BY end_at DESC LIMIT ?"
        )
        with self._lock, self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        hits = []
        for row in rows:
            body = row["body"]
            positions = [body.casefold().find(term.casefold()) for term in clean_terms]
            positions = [position for position in positions if position >= 0]
            start = max(0, (min(positions) if positions else 0) - 220)
            excerpt = body[start:start + 900].strip()
            matched_terms = sum(
                1 for term in clean_terms if term.casefold() in body.casefold()
            )
            authority_bonus = 20 if row["authority"] == "curated" else 0
            status_bonus = 10 if row["status"] in {"approved", "verified", "current"} else 0
            hits.append(LexicalHit(
                document_key=row["document_key"],
                channel_name=row["channel_name"] or "",
                festival=row["festival"],
                source_url=row["source_url"] or "",
                excerpt=excerpt,
                score=matched_terms * 100 + authority_bonus + status_bonus,
            ))
        hits.sort(key=lambda hit: hit.score, reverse=True)
        return hits[:limit]

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
