"""Atomic JSON storage for admin-editable catalog entries."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import threading
import unicodedata
from pathlib import Path

from knowledge_catalog.models import CatalogEntry, CatalogPatch, utc_now


class CatalogConflictError(RuntimeError):
    pass


class CatalogNotFoundError(KeyError):
    pass


def normalize_label(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).strip().casefold()
    return "".join(normalized.split())


def catalog_entry_id(guild_id: int, kind: str, label: str) -> str:
    digest = hashlib.sha256(f"{guild_id}:{kind}:{normalize_label(label)}".encode()).hexdigest()[:24]
    return f"{kind}-{digest}"


def default_catalog_path() -> Path:
    configured = os.environ.get("YAGAPON_CATALOG_PATH")
    if configured:
        return Path(configured)
    config_path = Path(os.environ.get("YAGAPON_CONFIG_PATH", Path(__file__).parent.parent / "server_config.json"))
    return config_path.parent / "knowledge_catalog.json"


class CatalogStore:
    def __init__(self, path: Path | None = None):
        self.path = path or default_catalog_path()
        self._lock = threading.RLock()

    def _read(self) -> dict:
        if not self.path.exists():
            return {"schema_version": 1, "entries": []}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise RuntimeError(f"Failed to load catalog: {self.path}") from exc
        if payload.get("schema_version") != 1 or not isinstance(payload.get("entries"), list):
            raise RuntimeError(f"Unsupported catalog schema: {self.path}")
        return payload

    def _write(self, payload: dict):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, self.path)

    def list(
        self,
        guild_id: int,
        *,
        kind: str | None = None,
        status: str | None = None,
        query: str = "",
    ) -> list[CatalogEntry]:
        with self._lock:
            entries = [CatalogEntry.model_validate(item) for item in self._read()["entries"]]
        needle = normalize_label(query)
        return sorted(
            (
                entry for entry in entries
                if entry.guild_id == guild_id
                and (kind is None or entry.kind == kind)
                and (status is None or entry.status == status)
                and (
                    not needle
                    or needle in normalize_label(entry.label)
                    or any(needle in normalize_label(alias) for alias in entry.aliases)
                    or needle in normalize_label(entry.description)
                )
            ),
            key=lambda entry: (entry.kind, entry.status, entry.label),
        )

    def get(self, guild_id: int, entry_id: str) -> CatalogEntry:
        for entry in self.list(guild_id):
            if entry.id == entry_id:
                return entry
        raise CatalogNotFoundError(entry_id)

    def upsert_candidates(self, candidates: list[CatalogEntry]) -> tuple[int, int]:
        """Insert candidates and refresh only untouched, still-pending entries."""
        with self._lock:
            payload = self._read()
            by_id = {item["id"]: item for item in payload["entries"]}
            created = 0
            refreshed = 0
            for candidate in candidates:
                current_data = by_id.get(candidate.id)
                if current_data is None:
                    by_id[candidate.id] = candidate.model_dump()
                    created += 1
                    continue
                current = CatalogEntry.model_validate(current_data)
                if current.status != "candidate" or current.manually_edited:
                    continue
                candidate.created_at = current.created_at
                candidate.revision = current.revision + 1
                candidate.updated_at = utc_now()
                by_id[candidate.id] = candidate.model_dump()
                refreshed += 1
            payload["entries"] = list(by_id.values())
            self._write(payload)
            return created, refreshed

    def create(self, entry: CatalogEntry) -> CatalogEntry:
        with self._lock:
            payload = self._read()
            if any(item["id"] == entry.id for item in payload["entries"]):
                raise CatalogConflictError(f"Catalog entry already exists: {entry.id}")
            entry.manually_edited = True
            payload["entries"].append(entry.model_dump())
            self._write(payload)
            return entry

    def patch(self, guild_id: int, entry_id: str, patch: CatalogPatch) -> CatalogEntry:
        with self._lock:
            payload = self._read()
            for index, item in enumerate(payload["entries"]):
                entry = CatalogEntry.model_validate(item)
                if entry.guild_id != guild_id or entry.id != entry_id:
                    continue
                if entry.revision != patch.expected_revision:
                    raise CatalogConflictError(
                        f"Expected revision {patch.expected_revision}, current revision is {entry.revision}"
                    )
                changes = patch.model_dump(exclude={"expected_revision"}, exclude_none=True)
                updated = entry.model_copy(update={
                    **changes,
                    "manually_edited": True,
                    "revision": entry.revision + 1,
                    "updated_at": utc_now(),
                })
                payload["entries"][index] = updated.model_dump()
                self._write(payload)
                return updated
        raise CatalogNotFoundError(entry_id)

    def export_csv(self, guild_id: int, output: Path) -> int:
        entries = self.list(guild_id)
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow([
                "id", "kind", "status", "label", "reading", "aliases", "description",
                "roles", "reason", "confidence", "source_count", "revision",
            ])
            for entry in entries:
                writer.writerow([
                    entry.id,
                    entry.kind,
                    entry.status,
                    entry.label,
                    entry.reading,
                    " / ".join(entry.aliases),
                    entry.description,
                    " / ".join(entry.roles),
                    entry.reason,
                    entry.confidence,
                    entry.source_count,
                    entry.revision,
                ])
        return len(entries)
