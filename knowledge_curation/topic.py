"""Durable, fail-closed primitives for recurring topic curation.

The model may propose a topic and prose, but it never decides repository paths,
festival/bureau routing, evidence identities, or access boundaries.  Those are
derived here from trusted metadata and persisted independently from a single
worker run.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Literal

OPEN_DELIVERY_STATUSES = frozenset({"pending", "failed", "queued", "published"})
RETRYABLE_DELIVERY_STATUSES = frozenset({"pending", "failed"})
TERMINAL_SOURCE_STATUSES = frozenset(
    {"no-knowledge", "delivered", "blocked-sensitive-content", "blocked-access", "blocked-routing"}
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime | None) -> datetime:
    current = value or _utc_now()
    if current.tzinfo is None:
        return current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def normalize_topic(value: str) -> str:
    """Normalize a model-proposed label without trying to infer its meaning."""
    normalized = unicodedata.normalize("NFKC", value).casefold().strip()
    normalized = re.sub(r"[\s\u3000]+", " ", normalized)
    normalized = re.sub(r"[\s\-_/・:：,，.。!?！？()（）\[\]【】]+", "-", normalized)
    return normalized.strip("-")[:200]


def _ascii_slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value).strip("-")[:40]


@dataclass(frozen=True)
class TopicIdentity:
    festival: int
    bureau: str
    normalized_topic: str
    slug: str
    topic_id: str


def deterministic_topic_identity(festival: int, bureau: str, topic: str) -> TopicIdentity:
    """Build a stable filesystem-safe identity from trusted routing plus a label."""
    if not 1 <= festival <= 999:
        raise ValueError("festival must be between 1 and 999")
    safe_bureau = _ascii_slug(normalize_topic(bureau))
    normalized = normalize_topic(topic)
    if not safe_bureau or not normalized:
        raise ValueError("bureau and topic are required")
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:10]
    readable = _ascii_slug(normalized) or "topic"
    slug = f"{readable}-{digest}"
    return TopicIdentity(
        festival=festival,
        bureau=safe_bureau,
        normalized_topic=normalized,
        slug=slug,
        topic_id=f"{festival}th-{safe_bureau}-{slug}",
    )


@dataclass(frozen=True)
class EvidenceItem:
    evidence_id: str
    document_key: str
    source_type: str
    authority: str
    status: str
    access_scope: str
    channel_id: int | None
    source_url: str
    excerpt: str
    content_sha256: str
    start_at: str = ""
    end_at: str = ""

    def fingerprint_record(self) -> dict[str, object]:
        return {
            "document_key": self.document_key,
            "source_type": self.source_type,
            "authority": self.authority,
            "status": self.status,
            "content_sha256": self.content_sha256,
            "access_scope": self.access_scope,
            "channel_id": self.channel_id,
            "start_at": self.start_at,
            "end_at": self.end_at,
        }


def evidence_fingerprint(evidence: Iterable[EvidenceItem]) -> str:
    """Hash evidence identity/content, independent of model prose and list order."""
    records = sorted(
        (item.fingerprint_record() for item in evidence),
        key=lambda item: (
            str(item["source_type"]),
            str(item["document_key"]),
            str(item["content_sha256"]),
        ),
    )
    payload = json.dumps(records, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class AccessDecision:
    scope: Literal["guild", "channel", "restricted"]
    channel_id: int | None
    reason: str


def intersect_access_scopes(evidence: Iterable[EvidenceItem]) -> AccessDecision:
    """Return the most restrictive representable access scope.

    A single Knowledge file cannot express an intersection of multiple private
    Discord channels.  Such a bundle is therefore restricted and publication is
    denied instead of silently widening access.
    """
    items = list(evidence)
    if not items:
        return AccessDecision("restricted", None, "no-evidence")
    channel_ids: set[int] = set()
    for item in items:
        if item.access_scope == "guild":
            continue
        if item.access_scope == "channel" and item.channel_id and item.channel_id > 0:
            channel_ids.add(item.channel_id)
            continue
        return AccessDecision("restricted", None, f"unsupported-scope:{item.document_key}")
    if len(channel_ids) > 1:
        return AccessDecision("restricted", None, "multiple-private-channels")
    if channel_ids:
        return AccessDecision("channel", next(iter(channel_ids)), "single-private-channel")
    return AccessDecision("guild", None, "guild-evidence-only")


@dataclass(frozen=True)
class TopicRecord:
    topic_id: str
    festival: int
    bureau: str
    title: str
    normalized_topic: str
    aliases: tuple[str, ...]
    access_scope: str
    channel_id: int | None
    observed_fingerprint: str
    delivered_fingerprint: str | None
    delivery_status: str
    bundle_id: str | None
    bundle_fingerprint: str | None
    attempts: int
    next_attempt_at: str | None
    blocked: bool

    @property
    def dirty(self) -> bool:
        return self.observed_fingerprint != (self.delivered_fingerprint or "")

    @property
    def has_open_bundle(self) -> bool:
        return self.delivery_status in OPEN_DELIVERY_STATUSES and bool(self.bundle_id)

    def retry_due(self, now: datetime | None = None) -> bool:
        if self.delivery_status not in RETRYABLE_DELIVERY_STATUSES or not self.bundle_id:
            return False
        if not self.next_attempt_at:
            return True
        try:
            due = datetime.fromisoformat(self.next_attempt_at)
        except ValueError:
            return True
        return due <= _as_utc(now)


class TopicStateStore:
    """SQLite ledger for source discovery, topic dirtiness, and delivery retry."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        legacy = self._extract_legacy_json()
        if legacy is None:
            self._initialize()
        else:
            self._migrate_legacy(legacy)

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def _extract_legacy_json(self) -> dict | None:
        if not self.path.is_file():
            return None
        try:
            prefix = self.path.read_bytes()[:64].lstrip()
        except OSError:
            return None
        if not prefix.startswith(b"{"):
            return None
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def _migrate_legacy(self, payload: dict) -> None:
        """Build the replacement database before atomically replacing JSON.

        The old implementation renamed the only state file before SQLite was
        initialized/imported.  A crash in between left the configured path
        missing.  Here the JSON remains authoritative until a complete SQLite
        file is ready, while a separate backup preserves the human-readable
        source after the atomic swap.
        """
        original_path = self.path
        backup = original_path.with_suffix(original_path.suffix + ".legacy.json")
        if backup.exists():
            backup = original_path.with_name(
                f"{original_path.name}.{int(_utc_now().timestamp())}.{uuid.uuid4().hex[:8]}.legacy.json"
            )
        backup_temporary = backup.with_suffix(backup.suffix + ".tmp")
        shutil.copyfile(original_path, backup_temporary)
        with backup_temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        os.replace(backup_temporary, backup)

        temporary = original_path.with_name(f".{original_path.name}.{uuid.uuid4().hex}.migrating")
        try:
            self.path = temporary
            self._initialize()
            self._import_legacy(payload)
            with self._connect() as connection:
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self.path = original_path
            os.replace(temporary, original_path)
        except BaseException:
            self.path = original_path
            for suffix in ("", "-wal", "-shm"):
                try:
                    Path(f"{temporary}{suffix}").unlink()
                except FileNotFoundError:
                    pass
            raise
        finally:
            try:
                backup_temporary.unlink()
            except FileNotFoundError:
                pass

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS topics (
                    topic_id TEXT PRIMARY KEY,
                    festival INTEGER NOT NULL,
                    bureau TEXT NOT NULL,
                    title TEXT NOT NULL,
                    normalized_topic TEXT NOT NULL,
                    aliases_json TEXT NOT NULL,
                    access_scope TEXT NOT NULL,
                    channel_id INTEGER,
                    observed_fingerprint TEXT NOT NULL,
                    delivered_fingerprint TEXT,
                    delivery_status TEXT NOT NULL DEFAULT 'dirty',
                    bundle_id TEXT,
                    bundle_fingerprint TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TEXT,
                    last_error TEXT,
                    last_success_at TEXT,
                    blocked INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS topics_delivery_status
                    ON topics(delivery_status, next_attempt_at);
                CREATE TABLE IF NOT EXISTS topic_aliases (
                    festival INTEGER NOT NULL,
                    bureau TEXT NOT NULL,
                    alias TEXT NOT NULL,
                    topic_id TEXT NOT NULL REFERENCES topics(topic_id),
                    PRIMARY KEY(festival, bureau, alias)
                );
                CREATE TABLE IF NOT EXISTS source_observations (
                    document_key TEXT PRIMARY KEY,
                    source_sha256 TEXT NOT NULL,
                    topic_id TEXT,
                    status TEXT NOT NULL,
                    plan_json TEXT,
                    reason TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS source_observations_topic
                    ON source_observations(topic_id, status);
                CREATE TABLE IF NOT EXISTS bundle_sources (
                    bundle_id TEXT NOT NULL,
                    topic_id TEXT NOT NULL,
                    document_key TEXT NOT NULL,
                    source_sha256 TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(bundle_id, document_key)
                );
                CREATE INDEX IF NOT EXISTS bundle_sources_topic
                    ON bundle_sources(topic_id, bundle_id);
                """
            )

    def _import_legacy(self, payload: dict) -> None:
        documents = payload.get("documents")
        if not isinstance(documents, dict):
            return
        now = _utc_now().isoformat()
        rows = []
        for key, value in documents.items():
            if not isinstance(value, dict) or not value.get("sha256"):
                continue
            old_status = str(value.get("status") or "legacy-complete")
            status = "no-knowledge" if old_status == "no-decision" else "delivered"
            rows.append((str(key), str(value["sha256"]), None, status, None, old_status, now))
        with self._connect() as connection:
            connection.executemany(
                """INSERT OR IGNORE INTO source_observations
                   (document_key, source_sha256, topic_id, status, plan_json, reason, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                rows,
            )

    @staticmethod
    def _topic_from_row(row: sqlite3.Row | None) -> TopicRecord | None:
        if row is None:
            return None
        try:
            aliases = tuple(json.loads(row["aliases_json"]))
        except (TypeError, json.JSONDecodeError):
            aliases = ()
        return TopicRecord(
            topic_id=row["topic_id"],
            festival=row["festival"],
            bureau=row["bureau"],
            title=row["title"],
            normalized_topic=row["normalized_topic"],
            aliases=aliases,
            access_scope=row["access_scope"],
            channel_id=row["channel_id"],
            observed_fingerprint=row["observed_fingerprint"],
            delivered_fingerprint=row["delivered_fingerprint"],
            delivery_status=row["delivery_status"],
            bundle_id=row["bundle_id"],
            bundle_fingerprint=row["bundle_fingerprint"],
            attempts=row["attempts"],
            next_attempt_at=row["next_attempt_at"],
            blocked=bool(row["blocked"]),
        )

    def get_topic(self, topic_id: str) -> TopicRecord | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM topics WHERE topic_id=?", (topic_id,)).fetchone()
        return self._topic_from_row(row)

    def resolve_identity(
        self,
        *,
        festival: int,
        bureau: str,
        topic: str,
        aliases: Iterable[str] = (),
    ) -> TopicIdentity:
        candidate = deterministic_topic_identity(festival, bureau, topic)
        normalized_aliases = tuple(
            dict.fromkeys(
                value for value in (normalize_topic(topic), *(normalize_topic(item) for item in aliases)) if value
            )
        )
        with self._connect() as connection:
            for alias in normalized_aliases:
                row = connection.execute(
                    "SELECT topic_id FROM topic_aliases WHERE festival=? AND bureau=? AND alias=?",
                    (festival, candidate.bureau, alias),
                ).fetchone()
                if row:
                    existing = connection.execute(
                        "SELECT normalized_topic FROM topics WHERE topic_id=?", (row["topic_id"],)
                    ).fetchone()
                    if existing:
                        stable = deterministic_topic_identity(festival, candidate.bureau, existing["normalized_topic"])
                        return TopicIdentity(
                            festival=festival,
                            bureau=candidate.bureau,
                            normalized_topic=existing["normalized_topic"],
                            slug=stable.slug,
                            topic_id=row["topic_id"],
                        )
        return candidate

    def observe_topic(
        self,
        identity: TopicIdentity,
        *,
        title: str,
        aliases: Iterable[str],
        fingerprint: str,
        access: AccessDecision,
    ) -> TopicRecord:
        now = _utc_now().isoformat()
        normalized_aliases = tuple(
            dict.fromkeys(
                value
                for value in (
                    identity.normalized_topic,
                    *(normalize_topic(item) for item in aliases),
                )
                if value
            )
        )
        with self._connect() as connection:
            row = connection.execute(
                "SELECT observed_fingerprint, aliases_json FROM topics WHERE topic_id=?",
                (identity.topic_id,),
            ).fetchone()
            if row:
                previous_aliases = json.loads(row["aliases_json"] or "[]")
                merged_aliases = tuple(dict.fromkeys([*previous_aliases, *normalized_aliases]))
                changed = row["observed_fingerprint"] != fingerprint
                connection.execute(
                    """UPDATE topics SET title=?, aliases_json=?, access_scope=?, channel_id=?,
                       observed_fingerprint=?, delivery_status=CASE
                           WHEN ? AND delivery_status NOT IN ('pending','failed','queued','published') THEN 'dirty'
                           ELSE delivery_status END,
                       attempts=CASE WHEN ? AND delivery_status NOT IN ('pending','failed','queued','published') THEN 0 ELSE attempts END,
                       next_attempt_at=CASE WHEN ? AND delivery_status NOT IN ('pending','failed','queued','published') THEN NULL ELSE next_attempt_at END,
                       bundle_id=CASE WHEN ? AND delivery_status NOT IN ('pending','failed','queued','published') THEN NULL ELSE bundle_id END,
                       bundle_fingerprint=CASE WHEN ? AND delivery_status NOT IN ('pending','failed','queued','published') THEN NULL ELSE bundle_fingerprint END,
                       updated_at=? WHERE topic_id=?""",
                    (
                        title,
                        json.dumps(merged_aliases, ensure_ascii=False),
                        access.scope,
                        access.channel_id,
                        fingerprint,
                        int(changed),
                        int(changed),
                        int(changed),
                        int(changed),
                        int(changed),
                        now,
                        identity.topic_id,
                    ),
                )
                normalized_aliases = merged_aliases
            else:
                connection.execute(
                    """INSERT INTO topics (
                       topic_id, festival, bureau, title, normalized_topic, aliases_json,
                       access_scope, channel_id, observed_fingerprint, delivery_status,
                       created_at, updated_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'dirty', ?, ?)""",
                    (
                        identity.topic_id,
                        identity.festival,
                        identity.bureau,
                        title,
                        identity.normalized_topic,
                        json.dumps(normalized_aliases, ensure_ascii=False),
                        access.scope,
                        access.channel_id,
                        fingerprint,
                        now,
                        now,
                    ),
                )
            for alias in normalized_aliases:
                connection.execute(
                    """INSERT OR IGNORE INTO topic_aliases (festival, bureau, alias, topic_id)
                       VALUES (?, ?, ?, ?)""",
                    (identity.festival, identity.bureau, alias, identity.topic_id),
                )
            result = connection.execute("SELECT * FROM topics WHERE topic_id=?", (identity.topic_id,)).fetchone()
        topic_record = self._topic_from_row(result)
        assert topic_record is not None
        return topic_record

    def source_observation(self, document_key: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM source_observations WHERE document_key=?", (document_key,)
            ).fetchone()
        return dict(row) if row else None

    def source_actionable(self, document_key: str, source_sha256: str) -> bool:
        observation = self.source_observation(document_key)
        if not observation or observation["source_sha256"] != source_sha256:
            return True
        return observation["status"] not in TERMINAL_SOURCE_STATUSES

    def record_source(
        self,
        document_key: str,
        source_sha256: str,
        *,
        status: str,
        topic_id: str | None = None,
        plan: dict | None = None,
        reason: str = "",
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO source_observations
                   (document_key, source_sha256, topic_id, status, plan_json, reason, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(document_key) DO UPDATE SET
                       source_sha256=excluded.source_sha256,
                       topic_id=excluded.topic_id,
                       status=excluded.status,
                       plan_json=excluded.plan_json,
                       reason=excluded.reason,
                       updated_at=excluded.updated_at""",
                (
                    document_key,
                    source_sha256,
                    topic_id,
                    status,
                    json.dumps(plan, ensure_ascii=False) if plan is not None else None,
                    reason[:2000],
                    _utc_now().isoformat(),
                ),
            )

    def mark_topic_sources(self, topic_id: str, status: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE source_observations SET status=?, updated_at=? WHERE topic_id=?",
                (status, _utc_now().isoformat(), topic_id),
            )

    def mark_bundle_sources(self, bundle_id: str, status: str) -> int:
        """Complete only the exact source versions captured by one bundle.

        A topic can receive more evidence while its outbox delivery is waiting.
        Matching both document key and digest prevents a later version of the
        same source, or a newly attached source, from being marked delivered by
        an older bundle retry.
        """
        if not bundle_id:
            return 0
        with self._connect() as connection:
            cursor = connection.execute(
                """UPDATE source_observations AS source SET status=?, updated_at=?
                   WHERE EXISTS (
                       SELECT 1 FROM bundle_sources AS bundled
                       WHERE bundled.bundle_id=?
                         AND bundled.topic_id=source.topic_id
                         AND bundled.document_key=source.document_key
                         AND bundled.source_sha256=source.source_sha256
                   )""",
                (status, _utc_now().isoformat(), bundle_id),
            )
        return cursor.rowcount

    def bundle_source_versions(self, bundle_id: str) -> list[tuple[str, str]]:
        """Expose immutable bundle lineage for diagnostics and tests."""
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT document_key, source_sha256 FROM bundle_sources
                   WHERE bundle_id=? ORDER BY document_key""",
                (bundle_id,),
            ).fetchall()
        return [(str(row["document_key"]), str(row["source_sha256"])) for row in rows]

    @staticmethod
    def _retry_at(topic_id: str, attempts: int, now: datetime) -> datetime:
        base = max(60, int(os.environ.get("YAGAPON_KNOWLEDGE_CURATION_RETRY_BASE_SECONDS", "900")))
        cap = max(base, int(os.environ.get("YAGAPON_KNOWLEDGE_CURATION_RETRY_MAX_SECONDS", "21600")))
        delay = min(cap, base * (2 ** max(0, attempts - 1)))
        # Stable jitter prevents every worker from retrying at the same instant,
        # while keeping unit tests and operational reasoning deterministic.
        jitter = int(hashlib.sha256(topic_id.encode()).hexdigest()[:4], 16) % max(1, delay // 5)
        return now + timedelta(seconds=delay + jitter)

    def record_delivery(
        self,
        topic_id: str,
        *,
        fingerprint: str,
        bundle_id: str,
        status: str,
        error: str = "",
        now: datetime | None = None,
        source_versions: Iterable[tuple[str, str]] | None = None,
    ) -> TopicRecord:
        current = _as_utc(now)
        topic = self.get_topic(topic_id)
        if topic is None:
            raise KeyError(topic_id)
        attempts = topic.attempts + 1 if status in RETRYABLE_DELIVERY_STATUSES else 0
        next_attempt = (
            self._retry_at(topic_id, attempts, current).isoformat()
            if status in RETRYABLE_DELIVERY_STATUSES
            else None
        )
        delivered = fingerprint if status in {"queued", "published"} else topic.delivered_fingerprint
        blocked = int(status == "blocked-sensitive-content")
        captured_sources = tuple(
            dict.fromkeys(
                (str(document_key), str(source_sha256))
                for document_key, source_sha256 in (source_versions or ())
                if str(document_key) and str(source_sha256)
            )
        )
        with self._connect() as connection:
            if topic.bundle_id and topic.bundle_id != bundle_id and source_versions is None:
                connection.execute(
                    """INSERT OR IGNORE INTO bundle_sources
                       (bundle_id, topic_id, document_key, source_sha256, created_at)
                       SELECT ?, topic_id, document_key, source_sha256, ?
                       FROM bundle_sources WHERE bundle_id=?""",
                    (bundle_id, current.isoformat(), topic.bundle_id),
                )
            connection.execute(
                """UPDATE topics SET delivered_fingerprint=?, delivery_status=?, bundle_id=?,
                   bundle_fingerprint=?, attempts=?, next_attempt_at=?, last_error=?,
                   last_success_at=CASE WHEN ? THEN ? ELSE last_success_at END,
                   blocked=?, updated_at=? WHERE topic_id=?""",
                (
                    delivered,
                    status,
                    bundle_id,
                    fingerprint,
                    attempts,
                    next_attempt,
                    error[:1000] or None,
                    int(status in {"queued", "published"}),
                    current.isoformat(),
                    blocked,
                    current.isoformat(),
                    topic_id,
                ),
            )
            connection.executemany(
                """INSERT OR IGNORE INTO bundle_sources
                   (bundle_id, topic_id, document_key, source_sha256, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    (bundle_id, topic_id, document_key, source_sha256, current.isoformat())
                    for document_key, source_sha256 in captured_sources
                ),
            )
        result = self.get_topic(topic_id)
        assert result is not None
        return result

    def record_no_knowledge(self, topic_id: str) -> None:
        topic = self.get_topic(topic_id)
        if topic is None:
            raise KeyError(topic_id)
        now = _utc_now().isoformat()
        with self._connect() as connection:
            connection.execute(
                """UPDATE topics SET delivered_fingerprint=observed_fingerprint,
                   delivery_status='no-knowledge', attempts=0, next_attempt_at=NULL,
                   last_success_at=?, updated_at=? WHERE topic_id=?""",
                (now, now, topic_id),
            )

    def mark_accepted(self, topic_id: str) -> None:
        now = _utc_now().isoformat()
        with self._connect() as connection:
            connection.execute(
                """UPDATE topics SET delivery_status='accepted', bundle_id=NULL,
                   bundle_fingerprint=NULL, attempts=0, next_attempt_at=NULL,
                   updated_at=? WHERE topic_id=?""",
                (now, topic_id),
            )

    def due_retries(self, *, now: datetime | None = None, limit: int = 10) -> list[TopicRecord]:
        current = _as_utc(now).isoformat()
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM topics
                   WHERE delivery_status IN ('pending','failed') AND bundle_id IS NOT NULL
                     AND blocked=0 AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                   ORDER BY coalesce(next_attempt_at, updated_at), topic_id LIMIT ?""",
                (current, max(1, min(limit, 100))),
            ).fetchall()
        return [record for row in rows if (record := self._topic_from_row(row)) is not None]

    def retryable_schedule(self) -> tuple[int, str | None]:
        """Return retryable bundle count and its earliest durable retry time."""
        with self._connect() as connection:
            row = connection.execute(
                """SELECT count(*) AS count,
                          min(coalesce(next_attempt_at, '')) AS next_attempt_at
                   FROM topics
                   WHERE delivery_status IN ('pending','failed')
                     AND bundle_id IS NOT NULL AND blocked=0"""
            ).fetchone()
        count = int(row["count"])
        next_attempt_at = str(row["next_attempt_at"] or "") or None
        return count, next_attempt_at

    def count_open_bundles(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT count(*) AS count FROM topics
                   WHERE delivery_status IN ('pending','failed','queued','published') AND bundle_id IS NOT NULL"""
            ).fetchone()
        return int(row["count"])

    def open_topics(self, *, limit: int = 100) -> list[TopicRecord]:
        """Return submitted Draft topics that may now have an approved mirror."""
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM topics
                   WHERE delivery_status IN ('queued','published') AND bundle_id IS NOT NULL
                   ORDER BY updated_at, topic_id LIMIT ?""",
                (max(1, min(limit, 1000)),),
            ).fetchall()
        return [record for row in rows if (record := self._topic_from_row(row)) is not None]
