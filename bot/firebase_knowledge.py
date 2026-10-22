"""Read a small allowlist of non-personal Firestore data for current-state RAG answers."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qsl, quote, unquote, urlsplit

from bot.festival import current_festival
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


class FirebaseDocumentBatch(list[dict[str, Any]]):
    """Fetched Firestore documents with an explicit completeness contract."""

    def __init__(
        self,
        documents: list[dict[str, Any]],
        *,
        complete: bool,
        reader_limit: int,
    ) -> None:
        super().__init__(documents)
        self.complete = complete
        self.reader_limit = reader_limit


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
    "ranking": {
        "keywords": r"大賞企画|Y-?1(?:グランプリ)?|ランキング|ranking|得票|票数|順位",
        "path": "yagamy/{festival}/ranking",
        # This collection contains only the public aggregate for each event.
        # Never join it with per-user vote state or authentication records.
        "fields": (
            "eventId", "eventName", "category", "score",
        ),
    },
    "rankingConfig": {
        "keywords": r"大賞企画|Y-?1(?:グランプリ)?|ランキング|ranking|得票|票数|順位",
        "path": "yagamy/{festival}/config/ranking",
        "document": True,
        "fields": (
            "status", "updatedAt",
        ),
    },
    "maps": {
        "keywords": r"(?:Firestore|firestore).{0,20}(?:map|地図|マップ)|"
        r"(?:map|地図|マップ).{0,20}(?:Firestore|firestore|構造|状態)|"
        r"通行規制|避難経路",
        "path": "yagamy/{festival}/maps",
        "public_statuses": ("published",),
        # Deliberately omit areaImagePaths because Firebase Storage download
        # URLs can carry long-lived token query parameters. Map features live
        # in nested collections and are explained by the indexed source code.
        "fields": (
            "mapId", "name", "kind", "status", "schemaVersion", "mapVersion",
            "coordinateSystem", "sourceSize", "assetKey", "floorId",
            "parentFeatureIds", "featureGeometryStatus", "campusGeometry",
            "routeState",
        ),
    },
    "config": {
        "keywords": r"開催時間|終了時間|会期|当日設定|公開設定|config|設定値",
        # Rules expose this known public document, not a list of the config collection.
        "path": "yagamy/{festival}/config/standbyPass",
        "fields": (
            "name", "status", "startAt", "endAt", "openingAt", "closingAt",
            "enabled", "isEnabled", "isActive", "festival", "day1StartOfDayAt",
            "day2StartOfDayAt", "lowStockThreshold", "reminderMinutesBefore", "updatedAt",
        ),
    },
}

_FIREBASE_LIST_NOUN = (
    r"(?:公開)?通知|お知らせ|企画|イベント|整理券(?:対象|企画|イベント)?|"
    r"ランキング|大賞企画|Y-?1(?:グランプリ)?|(?:公開)?マップ|地図"
)
_FIREBASE_LIST_EXCLUDED_INTENT = re.compile(
    r"FAQ|よくある質問|機能|持ち物|使い方|手順|方法|説明|仕組み|"
    r"経緯|理由|背景|問題点|課題|改善|評価|比較|影響|実装|ソース\s*コード|"
    r"コード|コミット|変更履歴|firestore\s*構造|データ構造|権限|なぜ|どうして|について",
    re.IGNORECASE,
)
_SOURCE_LABELS = {
    "events": "企画",
    "standbyPassEvents": "整理券",
    "notifications": "公開通知",
    "ranking": "大賞企画の公開集計",
    "rankingConfig": "大賞企画の公開状態",
    "maps": "公開マップ",
    "config": "整理券設定",
}

_DROP = object()
_SCALAR = "scalar"
_PUBLIC_URL = "public_url"
_FIELD_SCHEMAS: dict[str, dict[str, object]] = {
    "notifications": {
        "action": {
            "type": _SCALAR,
            "url": _PUBLIC_URL,
            "label": _SCALAR,
            "targetId": _SCALAR,
        },
    },
    "maps": {
        "coordinateSystem": {
            "type": _SCALAR,
            "origin": _SCALAR,
        },
        "sourceSize": {
            "width": _SCALAR,
            "height": _SCALAR,
        },
        "parentFeatureIds": [_SCALAR],
        "campusGeometry": {
            "type": _SCALAR,
            "minX": _SCALAR,
            "minY": _SCALAR,
            "maxX": _SCALAR,
            "maxY": _SCALAR,
        },
        "routeState": {
            "schemaVersion": _SCALAR,
            "mapVersion": _SCALAR,
            "mode": _SCALAR,
            "label": _SCALAR,
            "instruction": _SCALAR,
            "evacuationDestinationIds": [_SCALAR],
            "enabled": _SCALAR,
        },
    },
}
_SENSITIVE_URL_QUERY_TERMS = (
    "token",
    "secret",
    "password",
    "credential",
    "signature",
    "api_key",
    "apikey",
    "access_key",
    "x-goog-",
    "x-amz-",
)
_EMAIL_IN_URL = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE)
_SECRET_IN_URL = re.compile(
    r"(?:^|[=/?#&;])(?:access[_-]?token|api[_-]?key|client[_-]?secret|"
    r"password|token|secret|credential|signature|sig|auth|authorization|"
    r"x-goog-[a-z0-9_-]*|x-amz-[a-z0-9_-]*)(?:=|:|/)[^/?#&;\s]+",
    re.IGNORECASE,
)


def is_explicit_firebase_list_query(question: str) -> bool:
    """Recognize only direct requests to enumerate a reviewed Firebase entity."""
    if _FIREBASE_LIST_EXCLUDED_INTENT.search(question):
        return False
    noun_then_list = re.search(
        rf"(?:{_FIREBASE_LIST_NOUN}).{{0,12}}(?:一覧|全件|列挙|すべて|全て|全部)",
        question,
        re.IGNORECASE,
    )
    list_then_noun = re.search(
        rf"(?:一覧|全件|列挙|すべて|全て|全部)(?:の|を)?(?:{_FIREBASE_LIST_NOUN})",
        question,
        re.IGNORECASE,
    )
    return bool(noun_then_list or list_then_noun)


def firebase_document_path_is_safe(source_type: str, festival: int, path: str) -> bool:
    """Validate that evidence came from the exact reviewed document scope."""
    config = COLLECTIONS.get(source_type)
    if config is None:
        return False
    base_path = str(config["path"]).format(festival=festival)
    if source_type == "config" or config.get("document") is True:
        return path == base_path
    prefix = base_path + "/"
    document_id = path[len(prefix):] if path.startswith(prefix) else ""
    return bool(document_id) and "/" not in document_id


def firebase_projected_fields_are_safe(source_type: str, fields: dict[str, Any]) -> bool:
    """Revalidate projected evidence at the rendering trust boundary."""
    config = COLLECTIONS.get(source_type)
    if config is None or not fields or not set(fields).issubset(config["fields"]):
        return False
    projected = {
        key: FirebaseKnowledgeReader._project_field(source_type, key, value)
        for key, value in fields.items()
    }
    return all(value is not _DROP for value in projected.values()) and projected == fields


class FirebaseKnowledgeReader:
    """Ephemeral, fail-open reader for approved public operational collections."""

    def __init__(self) -> None:
        self.enabled = os.environ.get("YAGAPON_FIREBASE_KNOWLEDGE_ENABLED", "false").lower() == "true"
        self.limit = max(1, min(int(os.environ.get("YAGAPON_FIREBASE_KNOWLEDGE_LIMIT", "100")), 200))
        self.max_documents = max(
            self.limit,
            min(
                int(os.environ.get("YAGAPON_FIREBASE_KNOWLEDGE_MAX_DOCUMENTS", "2000")),
                10_000,
            ),
        )
        self.cache_seconds = max(10, int(os.environ.get("YAGAPON_FIREBASE_KNOWLEDGE_CACHE_SECONDS", "60")))
        self.api_key = os.environ.get("YAGAPON_FIREBASE_KNOWLEDGE_API_KEY", "").strip()
        self._directory = SupportDirectory()
        knowledge_project = os.environ.get("YAGAPON_FIREBASE_KNOWLEDGE_PROJECT", "").strip()
        if knowledge_project:
            self._directory.project_id = knowledge_project
        knowledge_backend = os.environ.get("YAGAPON_FIREBASE_KNOWLEDGE_BACKEND", "").strip().lower()
        if knowledge_backend in {"emulator", "firestore", "public"}:
            self._directory.backend = knowledge_backend
        self._cache: dict[str, tuple[float, FirebaseDocumentBatch]] = {}
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
        ranking_intent = bool(
            re.search(COLLECTIONS["ranking"]["keywords"], question, re.IGNORECASE)
        )
        ranking_needs_event_details = bool(
            re.search(r"開催場所|会場|中止|開催状況|isAllHeld", question, re.IGNORECASE)
        )
        selected = [
            (name, config)
            for name, config in COLLECTIONS.items()
            if re.search(config["keywords"], question, re.IGNORECASE)
            and not (
                name == "events"
                and ranking_intent
                and not ranking_needs_event_details
            )
        ]
        if not selected:
            return []
        year = festival or current_festival()
        exhaustive = self._is_exhaustive_query(question)
        evidence: list[FirebaseEvidence] = []
        try:
            results = await asyncio.gather(
                *(
                    (
                        self._collection(
                            name,
                            config["path"].format(festival=year),
                            tuple(config.get("public_statuses", ())),
                            True,
                        )
                        if exhaustive
                        else self._collection(
                            name,
                            config["path"].format(festival=year),
                            tuple(config.get("public_statuses", ())),
                        )
                    )
                    for name, config in selected
                ),
                return_exceptions=True,
            )
            for (name, config), documents in zip(selected, results, strict=True):
                if isinstance(documents, BaseException):
                    log.warning("Firebase collection lookup failed for %s: %s", name, documents)
                    continue
                evidence.extend(
                    self._rank(
                        question,
                        name,
                        config["fields"],
                        documents,
                        year,
                        exhaustive=exhaustive,
                    )
                )
        except Exception:
            # Firestore availability must not take Discord answering offline.
            log.exception("Firebase knowledge lookup failed")
            return []
        # Each collection applies its own ranking. Do not truncate the
        # merged evidence again: that used to drop the third-place item from a
        # second ranking category and could also hide the accompanying config
        # document. Explicit lists page through the collection up to the
        # separately reported safety cap; focused collections keep top-five/
        # podium behavior.
        return evidence

    async def _collection(
        self,
        name: str,
        path: str,
        public_statuses: tuple[str, ...],
        exhaustive: bool = False,
    ) -> FirebaseDocumentBatch:
        cache_key = f"{name}:{path}:{'all' if exhaustive else 'focused'}"
        now = time.monotonic()
        async with self._cache_lock:
            cached = self._cache.get(cache_key)
            if cached and now - cached[0] < self.cache_seconds:
                return cached[1]
        if name == "config" or COLLECTIONS.get(name, {}).get("document") is True:
            payload = await self._directory._request(
                "GET",
                self._directory._documents_url(f"/{path}"),
                headers=self._public_headers(),
            )
            documents = FirebaseDocumentBatch(
                [payload] if payload else [],
                complete=True,
                reader_limit=1,
            )
        elif public_statuses:
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
            documents_list: list[dict[str, Any]] = []
            complete = False
            fetch_limit = self.max_documents + 1 if exhaustive else self.limit
            while len(documents_list) < fetch_limit:
                page_size = min(self.limit, fetch_limit - len(documents_list))
                structured_query: dict[str, Any] = {
                    "from": [{"collectionId": collection_id}],
                    "where": {
                        "fieldFilter": {
                            "field": {"fieldPath": "status"},
                            "op": operator,
                            "value": value,
                        }
                    },
                    "orderBy": [
                        {"field": {"fieldPath": "__name__"}, "direction": "ASCENDING"}
                    ],
                    "limit": page_size,
                }
                if documents_list:
                    structured_query["offset"] = len(documents_list)
                response = await self._directory._request(
                    "POST",
                    self._directory._documents_url(f"/{parent}:runQuery"),
                    headers=self._public_headers(),
                    payload={"structuredQuery": structured_query},
                )
                page = [row["document"] for row in response or () if "document" in row]
                documents_list.extend(page)
                if len(page) < page_size:
                    complete = True
                    break
                if not exhaustive:
                    break
            if len(documents_list) > self.max_documents:
                documents_list = documents_list[: self.max_documents]
                complete = False
            documents = FirebaseDocumentBatch(
                documents_list,
                complete=complete,
                reader_limit=self.max_documents if exhaustive else self.limit,
            )
        else:
            documents_list = []
            page_token = ""
            complete = False
            fetch_limit = self.max_documents + 1 if exhaustive else self.limit
            while len(documents_list) < fetch_limit:
                page_size = min(self.limit, fetch_limit - len(documents_list))
                suffix = f"?pageSize={page_size}"
                if page_token:
                    suffix += f"&pageToken={quote(page_token, safe='')}"
                payload = await self._directory._request(
                    "GET",
                    self._directory._documents_url(f"/{path}{suffix}"),
                    headers=self._public_headers(),
                )
                documents_list.extend(list((payload or {}).get("documents", ())))
                page_token = str((payload or {}).get("nextPageToken") or "")
                if not page_token:
                    complete = True
                    break
                if not exhaustive:
                    break
            if len(documents_list) > self.max_documents:
                documents_list = documents_list[: self.max_documents]
                complete = False
            documents = FirebaseDocumentBatch(
                documents_list,
                complete=complete,
                reader_limit=self.max_documents if exhaustive else self.limit,
            )
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
        *,
        exhaustive: bool = False,
    ) -> list[FirebaseEvidence]:
        terms = self._terms(question, source_type=source_type)
        ranked: list[dict[str, Any]] = []
        for document in documents:
            fields = _document_fields(document)
            projected: dict[str, Any] = {}
            for key in allowed_fields:
                if key not in fields or fields[key] in (None, "", (), [], {}):
                    continue
                value = self._project_field(source_type, key, fields[key])
                if value is not _DROP:
                    projected[key] = value
            doc_id = document.get("name", "").rsplit("/", 1)[-1]
            if not doc_id or not projected:
                continue
            searchable = f"{doc_id} {json.dumps(projected, ensure_ascii=False, sort_keys=True)}".casefold()
            score = sum(1 for term in terms if term.casefold() in searchable)
            if terms and score == 0:
                continue
            path = document["name"].split("/documents/", 1)[-1]
            ranked.append(
                {
                    "document_key": f"firebase:{path}",
                    "path": path,
                    "projected": projected,
                    "score": score,
                    "timestamp": self._notification_timestamp(projected)
                    if source_type == "notifications"
                    else None,
                }
            )

        if exhaustive:
            if source_type == "notifications":
                # Notification IDs are not a reliable chronology. Prefer the
                # persisted publication time, falling back to creation time,
                # and place undated legacy records last.
                ranked.sort(
                    key=lambda item: (
                        item["timestamp"] is None,
                        item["timestamp"] if item["timestamp"] is not None else 0.0,
                        item["document_key"],
                    )
                )
                order = "publishedAt/createdAt ascending (oldest first)"
            elif source_type == "ranking":
                ranked.sort(key=self._ranking_sort_key)
                order = "category ascending, score descending, document path ascending"
            else:
                ranked.sort(key=lambda item: item["document_key"])
                order = "document path ascending"
            selected = ranked
        else:
            if source_type == "notifications":
                # For a focused question, equally relevant notifications are
                # more useful newest-first. Missing timestamps remain last.
                ranked.sort(
                    key=lambda item: (
                        -item["score"],
                        item["timestamp"] is None,
                        -(item["timestamp"] or 0.0),
                        item["document_key"],
                    )
                )
            elif source_type == "ranking":
                ranked.sort(key=self._ranking_sort_key)
                selected = self._ranking_podium(ranked)
                order = "category ascending, score descending; top three ranks including ties"
            else:
                ranked.sort(key=lambda item: (-item["score"], item["document_key"]))
            if source_type != "ranking":
                selected = ranked[:5]
                order = "relevance descending"

        if not selected:
            return []

        collection_path = selected[0]["path"].rsplit("/", 1)[0]
        # Firebase Console encodes a Firestore path with ``~2F`` and expects
        # the root slash too. Every item in this collection deliberately shares
        # one URL so Discord emits at most one login preview for the citation.
        console_path = "~2F" + quote(collection_path, safe="").replace("%2F", "~2F")
        url = (
            f"https://console.firebase.google.com/project/{self._directory.project_id}/"
            f"firestore/databases/-default-/data/{console_path}"
        )
        citation_title = f"{festival}th {_SOURCE_LABELS.get(source_type, source_type)}"
        returned_count = len(selected)
        matched_count = len(ranked)
        fetched_count = len(documents)
        reader_limit = max(
            returned_count,
            int(getattr(documents, "reader_limit", self.limit)),
        )
        collection_complete = bool(
            getattr(documents, "complete", fetched_count < self.limit)
        )
        evidence = []
        for position, item in enumerate(selected, start=1):
            header = f"Firestore現在値（{festival}th / {source_type}）"
            if exhaustive:
                completeness = "complete" if collection_complete else "unknown-limit-reached"
                if position == 1:
                    header += (
                        f"\nlist_result: returned={returned_count}; matched={matched_count}; "
                        f"firestore_fetched={fetched_count}; reader_limit={reader_limit}; "
                        f"completeness={completeness}; order={order}"
                    )
                header += f"\nitem: {position}/{returned_count}"
            body_lines = [header, f"document: {item['path']}"]
            if item.get("computed_rank"):
                body_lines.append(f"computed_rank: {item['computed_rank']}")
            body_lines.append(
                f"fields: {json.dumps(item['projected'], ensure_ascii=False, sort_keys=True)}"
            )
            body = "\n".join(body_lines)
            evidence.append(
                FirebaseEvidence(
                    item["document_key"],
                    citation_title,
                    body,
                    url,
                )
            )
        return evidence

    @staticmethod
    def _is_exhaustive_query(question: str) -> bool:
        return is_explicit_firebase_list_query(question)

    @staticmethod
    def _notification_timestamp(projected: dict[str, Any]) -> float | None:
        value = projected.get("publishedAt") or projected.get("createdAt")
        if not isinstance(value, str) or not value.strip():
            return None
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()

    @staticmethod
    def _terms(question: str, *, source_type: str = "") -> list[str]:
        question = re.sub(r"(?:第\s*)?\d+(?:th|回)", " ", question, flags=re.IGNORECASE)
        if source_type in {"ranking", "rankingConfig"}:
            question = re.sub(
                r"(?:firebase|firestore)(?:を)?(?:読んで|見て|確認して)?|"
                r"大賞企画|Y-?1(?:グランプリ)?|ランキング|ranking|"
                r"(?:公開)?状態|結果|得票(?:数)?|票数|順位|構造|スキーマ|schema|"
                r"あわせて|合わせて|(?:票数)?(?:つき|付き)|どうなって(?:いた|いる|る|た)?",
                " ",
                question,
                flags=re.IGNORECASE,
            )
        elif source_type == "maps":
            question = re.sub(
                r"(?:firebase|firestore)(?:を)?(?:読んで|見て|確認して)?|"
                r"map|地図|マップ|構造|スキーマ|schema|現在|状態|"
                r"どうなって(?:いた|いる|る|た)?",
                " ",
                question,
                flags=re.IGNORECASE,
            )
        stripped = re.sub(
            r"整理券|企画|イベント|通知|お知らせ|現在|今|公開中?|開催状況|状況|一覧|最新|確認|すべて|全て|全部|全件|設定|どうなって(?:いる|る|た)|教えて|ください|下さい|について|って|知りたい|見たい|したい|[？?。、\s]",
            " ",
            question,
        )
        terms = []
        for part in stripped.split():
            part = re.sub(r"(?:の|は|を|が|に|で|と)+$", "", part)
            if len(part) >= 2:
                terms.append(part)
        return terms[:8]

    @staticmethod
    def _ranking_sort_key(item: dict[str, Any]) -> tuple[str, float, str]:
        projected = item.get("projected") or {}
        category = str(projected.get("category") or "")
        score = projected.get("score")
        numeric_score = float(score) if isinstance(score, int | float) else 0.0
        return category, -numeric_score, str(item.get("document_key") or "")

    @classmethod
    def _ranking_podium(cls, ranked: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Return the public top-three ranks per category, preserving ties."""
        selected: list[dict[str, Any]] = []
        current_category = None
        previous_score: float | None = None
        previous_rank = 0
        position = 0
        for original in ranked:
            item = dict(original)
            projected = item.get("projected") or {}
            category = str(projected.get("category") or "")
            score_value = projected.get("score")
            score = float(score_value) if isinstance(score_value, int | float) else 0.0
            if category != current_category:
                current_category = category
                previous_score = None
                previous_rank = 0
                position = 0
            position += 1
            rank = previous_rank if previous_score is not None and score == previous_score else position
            previous_score = score
            previous_rank = rank
            if rank <= 3:
                item["computed_rank"] = rank
                selected.append(item)
        return selected

    @classmethod
    def _bounded(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value[:500]
        if isinstance(value, tuple | list):
            return [cls._bounded(item) for item in value[:20]]
        if isinstance(value, dict):
            return {str(key)[:100]: cls._bounded(item) for key, item in list(value.items())[:20]}
        return value

    @classmethod
    def _project_field(cls, source_type: str, key: str, value: Any) -> Any:
        schema = _FIELD_SCHEMAS.get(source_type, {}).get(key, _SCALAR)
        return cls._project_value(value, schema)

    @classmethod
    def _project_value(cls, value: Any, schema: object) -> Any:
        if schema == _SCALAR:
            if isinstance(value, str):
                return value[:500]
            if isinstance(value, bool | int | float):
                return value
            return _DROP
        if schema == _PUBLIC_URL:
            return cls._safe_public_url(value)
        if isinstance(schema, list) and len(schema) == 1:
            if not isinstance(value, list | tuple):
                return _DROP
            projected = []
            for item in value[:20]:
                safe_item = cls._project_value(item, schema[0])
                if safe_item is _DROP:
                    return _DROP
                projected.append(safe_item)
            return projected
        if isinstance(schema, dict):
            if not isinstance(value, dict):
                return _DROP
            projected = {}
            for child_key, child_value in value.items():
                child_schema = schema.get(str(child_key))
                if child_schema is None:
                    continue
                safe_child = cls._project_value(child_value, child_schema)
                if safe_child is not _DROP:
                    projected[str(child_key)] = safe_child
            return projected if projected else _DROP
        return _DROP

    @staticmethod
    def _safe_public_url(value: Any) -> str | object:
        if not isinstance(value, str) or len(value) > 2_000:
            return _DROP
        try:
            parsed = urlsplit(value)
        except ValueError:
            return _DROP
        if (
            parsed.scheme.lower() != "https"
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
        ):
            return _DROP
        decoded = value
        # Each percent-encoding layer must add input characters, so input
        # length is a strict convergence bound. Fixed-depth decoding is
        # bypassable by adding one more layer around a token or email address.
        for _ in range(len(value) + 1):
            if _EMAIL_IN_URL.search(decoded) or _SECRET_IN_URL.search(decoded):
                return _DROP
            next_decoded = unquote(decoded)
            if next_decoded == decoded:
                break
            decoded = next_decoded
        else:
            return _DROP
        if _EMAIL_IN_URL.search(decoded) or _SECRET_IN_URL.search(decoded):
            return _DROP
        for key, _ in parse_qsl(parsed.query, keep_blank_values=True):
            normalized = key.casefold()
            if normalized in {"auth", "authorization", "key", "sig"} or any(
                term in normalized for term in _SENSITIVE_URL_QUERY_TERMS
            ):
                return _DROP
        return value
