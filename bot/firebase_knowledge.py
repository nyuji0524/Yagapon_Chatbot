"""Read a small allowlist of non-personal Firestore data for current-state RAG answers."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from bot.festival import festival_number
from bot.knowledge_authority import QuestionKind
from bot.support_directory import SupportDirectory, _document_fields

log = logging.getLogger("yagapon.firebase_knowledge")


@dataclass(frozen=True)
class FirebaseEvidence:
    document_key: str
    title: str
    body: str
    source_url: str
    authority: str = "live_data"
    status: str = "current"


# Never add users, staffInfos, pass requests, usage logs, receipts, or other
# personal/financial collections here. Values are also projected field-by-field.
COLLECTIONS = {
    "events": {
        "keywords": r"企画|イベント|開催場所|会場|中止|開催状況|isAllHeld",
        "path": "festival/{festival}/events",
        "fields": (
            "eventName", "name", "title", "organization", "organizationId",
            "place", "placeRoom", "status", "isAllHeld", "isDay1Held", "isDay2Held",
            "day1StartTime", "day1EndTime", "day2StartTime", "day2EndTime",
            "festivalId", "category",
        ),
    },
    "standbyPassEvents": {
        "keywords": r"整理券|standby\s*pass|受付枠|時間枠|openBeforeMinutes",
        "path": "yagamy/{festival}/standbyPassEvents",
        "public_statuses": ("open", "paused", "closed"),
        "fields": (
            "eventId", "name", "title", "status", "place", "description",
            "openBeforeMinutes", "maxPartySize", "maxGroupSize", "displayOrder",
            "startAt", "endAt",
        ),
    },
    "notifications": {
        "keywords": r"通知|お知らせ|プッシュ|push",
        "path": "festival/{festival}/notifications",
        "public_statuses": ("published",),
        "fields": (
            "title", "body", "status", "category", "publishedAt", "createdAt",
            "sendPush", "action",
        ),
    },
    "config": {
        "keywords": r"開催時間|終了時間|会期|当日設定|公開設定|config|設定値",
        "path": "yagamy/{festival}/config",
        "fields": (
            "name", "status", "startAt", "endAt", "openingAt", "closingAt",
            "enabled", "isEnabled", "isActive", "festival", "day1StartOfDayAt",
            "day2StartOfDayAt", "lowStockThreshold", "reminderMinutesBefore", "updatedAt",
        ),
    },
}


class FirebaseKnowledgeReader:
    """Ephemeral, fail-open reader for approved public operational collections."""

    def __init__(self) -> None:
        self.enabled = os.environ.get("YAGAPON_FIREBASE_KNOWLEDGE_ENABLED", "false").lower() == "true"
        self.limit = max(1, min(int(os.environ.get("YAGAPON_FIREBASE_KNOWLEDGE_LIMIT", "100")), 200))
        self.cache_seconds = max(10, int(os.environ.get("YAGAPON_FIREBASE_KNOWLEDGE_CACHE_SECONDS", "60")))
        self.api_key = os.environ.get("YAGAPON_FIREBASE_KNOWLEDGE_API_KEY", "").strip()
        self._directory = SupportDirectory()
        knowledge_project = os.environ.get("YAGAPON_FIREBASE_KNOWLEDGE_PROJECT", "").strip()
        if knowledge_project:
            self._directory.project_id = knowledge_project
        knowledge_backend = os.environ.get("YAGAPON_FIREBASE_KNOWLEDGE_BACKEND", "").strip().lower()
        if knowledge_backend in {"emulator", "firestore", "public"}:
            self._directory.backend = knowledge_backend
        self._cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
        self._cache_lock = asyncio.Lock()

    @property
    def available(self) -> bool:
        return (
            self.enabled
            and self._directory.backend in {"emulator", "firestore", "public"}
            and bool(self._directory.project_id)
        )

    async def evidence_for(
        self,
        question: str,
        kind: QuestionKind,
        *,
        festival: int | None,
    ) -> list[FirebaseEvidence]:
        if not self.available or kind not in {
            QuestionKind.RUNTIME,
            QuestionKind.USAGE,
            QuestionKind.DATA_CONTRACT,
            QuestionKind.GENERAL,
        }:
            return []
        selected = [
            (name, config)
            for name, config in COLLECTIONS.items()
            if re.search(config["keywords"], question, re.IGNORECASE)
        ]
        if not selected:
            return []
        year = festival or festival_number()
        evidence: list[FirebaseEvidence] = []
        try:
            results = await asyncio.gather(
                *(
                    self._collection(
                        name,
                        config["path"].format(festival=year),
                        tuple(config.get("public_statuses", ())),
                    )
                    for name, config in selected
                ),
                return_exceptions=True,
            )
            for (name, config), documents in zip(selected, results, strict=True):
                if isinstance(documents, BaseException):
                    log.warning("Firebase collection lookup failed for %s: %s", name, documents)
                    continue
                evidence.extend(self._rank(question, name, config["fields"], documents, year))
        except Exception:
            # Firestore availability must not take Discord answering offline.
            log.exception("Firebase knowledge lookup failed")
            return []
        return evidence[:5]

    async def _collection(
        self,
        name: str,
        path: str,
        public_statuses: tuple[str, ...],
    ) -> list[dict[str, Any]]:
        cache_key = f"{name}:{path}"
        now = time.monotonic()
        async with self._cache_lock:
            cached = self._cache.get(cache_key)
            if cached and now - cached[0] < self.cache_seconds:
                return cached[1]
        if public_statuses:
            parent, collection_id = path.rsplit("/", 1)
            if len(public_statuses) == 1:
                value = {"stringValue": public_statuses[0]}
                operator = "EQUAL"
            else:
                value = {
                    "arrayValue": {
                        "values": [{"stringValue": status} for status in public_statuses]
                    }
                }
                operator = "IN"
            response = await self._directory._request(
                "POST",
                self._directory._documents_url(f"/{parent}:runQuery"),
                headers=self._public_headers(),
                payload={
                    "structuredQuery": {
                        "from": [{"collectionId": collection_id}],
                        "where": {
                            "fieldFilter": {
                                "field": {"fieldPath": "status"},
                                "op": operator,
                                "value": value,
                            }
                        },
                        "limit": self.limit,
                    }
                },
            )
            documents = [row["document"] for row in response or () if "document" in row]
        else:
            payload = await self._directory._request(
                "GET",
                self._directory._documents_url(f"/{path}?pageSize={self.limit}"),
                headers=self._public_headers(),
            )
            documents = list((payload or {}).get("documents", ()))
        async with self._cache_lock:
            self._cache[cache_key] = (now, documents)
        return documents

    def _public_headers(self) -> dict[str, str] | None:
        if self._directory.backend == "public" and self.api_key:
            return {"X-Goog-Api-Key": self.api_key}
        return None

    def _rank(
        self,
        question: str,
        source_type: str,
        allowed_fields: tuple[str, ...],
        documents: list[dict[str, Any]],
        festival: int,
    ) -> list[FirebaseEvidence]:
        terms = self._terms(question)
        ranked: list[tuple[int, FirebaseEvidence]] = []
        for document in documents:
            fields = _document_fields(document)
            projected = {
                key: self._bounded(fields[key])
                for key in allowed_fields
                if key in fields and fields[key] not in (None, "", (), [], {})
            }
            doc_id = document.get("name", "").rsplit("/", 1)[-1]
            if not doc_id or not projected:
                continue
            searchable = f"{doc_id} {json.dumps(projected, ensure_ascii=False, sort_keys=True)}".casefold()
            score = sum(1 for term in terms if term.casefold() in searchable)
            if terms and score == 0:
                continue
            path = document["name"].split("/documents/", 1)[-1]
            console_path = quote(path, safe="").replace("%2F", "~2F")
            url = (
                f"https://console.firebase.google.com/project/{self._directory.project_id}/"
                f"firestore/databases/-default-/data/{console_path}"
            )
            title = str(projected.get("eventName") or projected.get("title") or projected.get("name") or doc_id)
            body = (
                f"Firestore現在値（{festival}th / {source_type}）\n"
                f"document: {path}\n"
                f"fields: {json.dumps(projected, ensure_ascii=False, sort_keys=True)}"
            )
            ranked.append((score, FirebaseEvidence(f"firebase:{path}", title, body, url)))
        ranked.sort(key=lambda item: (item[0], item[1].document_key), reverse=True)
        return [item[1] for item in ranked[:5]]

    @staticmethod
    def _terms(question: str) -> list[str]:
        question = re.sub(r"(?:第\s*)?\d+(?:th|回)", " ", question, flags=re.IGNORECASE)
        stripped = re.sub(
            r"整理券|企画|イベント|通知|お知らせ|現在|今|公開中?|開催状況|状況|一覧|最新|確認|すべて|全部|設定|どうなって(?:いる|る)|教えて|[？?。、\s]",
            " ",
            question,
        )
        terms = []
        for part in stripped.split():
            part = re.sub(r"(?:の|は|を|が|に|で|と)+$", "", part)
            if len(part) >= 2:
                terms.append(part)
        return terms[:8]

    @classmethod
    def _bounded(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value[:500]
        if isinstance(value, tuple | list):
            return [cls._bounded(item) for item in value[:20]]
        if isinstance(value, dict):
            return {str(key)[:100]: cls._bounded(item) for key, item in list(value.items())[:20]}
        return value
