"""Read reviewed non-personal Firestore data for current-state RAG answers.

The runtime identity may have broad read access, but this module deliberately
keeps disclosure narrower: paths, statuses and projected fields come only from
the registry below.  Firestore Rules are not used as the answer boundary.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qsl, quote, unquote, urlsplit

from bot.festival import current_festival
from bot.firestore_readonly import FirestoreReadonlyClient
from bot.knowledge_authority import QuestionKind

log = logging.getLogger("yagapon.firebase_knowledge")


_ENTITY_OVERVIEW_RE = re.compile(
    r"^\s*(?P<subject>.{2,48}?)\s*(?:について(?:詳しく)?(?:教えて(?:ください|下さい)?|知りたい)|"
    r"とは(?:何|なん)?(?:ですか)?|の概要(?:を)?(?:教えて(?:ください|下さい)?)?)\s*[？?。\s]*$",
    re.IGNORECASE,
)
_ENTITY_OVERVIEW_STOP_SUBJECTS = {
    "あなた",
    "これ",
    "それ",
    "あれ",
    "この件",
    "その件",
    "機能",
    "企画",
    "イベント",
}


def entity_overview_subject(question: str) -> str | None:
    """Return a concrete subject from a broad entity-overview question."""
    match = _ENTITY_OVERVIEW_RE.fullmatch(question.strip())
    if not match:
        return None
    subject = match.group("subject").strip(" \t\r\n「」『』\"'")
    if subject.casefold() in {item.casefold() for item in _ENTITY_OVERVIEW_STOP_SUBJECTS}:
        return None
    return subject


def _field_value(value: dict[str, Any] | None) -> Any:
    """Decode only the Firestore value types used by reviewed projections."""
    if not value:
        return None
    if "stringValue" in value:
        return value["stringValue"]
    if "integerValue" in value:
        return int(value["integerValue"])
    if "doubleValue" in value:
        return float(value["doubleValue"])
    if "booleanValue" in value:
        return bool(value["booleanValue"])
    if "timestampValue" in value:
        return value["timestampValue"]
    if "nullValue" in value:
        return None
    if "arrayValue" in value:
        return tuple(_field_value(item) for item in value["arrayValue"].get("values", ()))
    if "mapValue" in value:
        return {
            key: _field_value(item)
            for key, item in value["mapValue"].get("fields", {}).items()
        }
    return None


def _document_fields(document: dict[str, Any]) -> dict[str, Any]:
    return {
        name: _field_value(value)
        for name, value in document.get("fields", {}).items()
    }


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
            "festivalId", "category", "shortDescription", "longDescription",
            "organizationDescription", "section", "keyword", "whenRainy", "urlPath",
            "placeId", "venueId", "venueIds", "hasStandbyPass", "hasStampRally",
            "day1IsAllDay", "day2IsAllDay", "priority", "coupon",
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
            "schemaVersion", "title", "body", "status", "category", "publishedAt",
            "createdAt", "displayTargets", "sendPush", "action",
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
            "status", "votingPhase", "updatedAt",
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
        "document": True,
        "fields": (
            "name", "status", "startAt", "endAt", "openingAt", "closingAt",
            "enabled", "isEnabled", "isActive", "festival", "day1StartOfDayAt",
            "day2StartOfDayAt", "lowStockThreshold", "reminderMinutesBefore", "updatedAt",
        ),
    },
}

# Only these reviewed public projections currently have enough information for
# a deterministic data-contract answer.  Keep this list deliberately narrower
# than COLLECTIONS: adding a runtime reader must not silently make its storage
# contract answerable without a matching review.
FIREBASE_SCHEMA_CONTRACT_SOURCES = frozenset({"ranking", "rankingConfig", "maps"})
FIREBASE_SCHEMA_CONTRACT_VERSION = "reviewed-public-projection-v1"

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
    "events": {
        "venueIds": [_SCALAR],
        "coupon": {
            "isEnabled": _SCALAR,
            "type": _SCALAR,
            "contents": _SCALAR,
            "itemName": _SCALAR,
            "remark": _SCALAR,
            "discount": _SCALAR,
            "itemIds": [_SCALAR],
        },
    },
    "notifications": {
        "displayTargets": [_SCALAR],
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
_CONTACT_EMAIL = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_CONTACT_PHONE = re.compile(
    r"(?<!\d)(?:\+?81[-\s]?(?:0)?|0)(?:\d[-\s]?){8,10}\d(?!\d)"
)
_LABELED_PRIVATE_DATA = re.compile(
    r"(?:学籍番号|学生番号|氏名|フルネーム|個人住所|住所|担当者?)\s*[:：=]\s*\S+",
    re.IGNORECASE,
)
_OPAQUE_SECRET_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
        r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b",
        r"\bmfa\.[A-Za-z0-9_-]{20,}\b",
        r"\b[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{20,}\b",
        r"\bsk-[A-Za-z0-9_-]{12,}\b",
        r"\bgh[pousr]_[A-Za-z0-9]{20,}\b",
        r"\bAIza[0-9A-Za-z_-]{20,}\b",
        r"\bAKIA[0-9A-Z]{16}\b",
        r"(?:api[_ -]?key|access[_ -]?token|client[_ -]?secret|password|token|secret)"
        r"\s*[:=]\s*[`\"']?[^\s`\"']{8,}",
    )
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
        self.project_id = os.environ.get("YAGAPON_FIREBASE_KNOWLEDGE_PROJECT", "").strip()
        self.backend = os.environ.get(
            "YAGAPON_FIREBASE_KNOWLEDGE_BACKEND",
            "public",
        ).strip().lower()
        self._client: FirestoreReadonlyClient | None = None
        if self.project_id and self.backend in {"emulator", "firestore", "public"}:
            emulator_host = os.environ.get(
                "YAGAPON_FIREBASE_KNOWLEDGE_EMULATOR_HOST",
                os.environ.get(
                    "YAGAPON_SUPPORT_FIRESTORE_EMULATOR_HOST",
                    os.environ.get("FIRESTORE_EMULATOR_HOST", "127.0.0.1:8080"),
                ),
            ).strip()
            timeout = float(
                os.environ.get("YAGAPON_FIREBASE_KNOWLEDGE_TIMEOUT_SECONDS", "8")
            )
            self._client = FirestoreReadonlyClient(
                project_id=self.project_id,
                backend=self.backend,
                emulator_host=emulator_host,
                api_key=os.environ.get(
                    "YAGAPON_FIREBASE_KNOWLEDGE_API_KEY",
                    "",
                ).strip(),
                timeout=timeout,
            )
        self._cache: dict[str, tuple[float, FirebaseDocumentBatch]] = {}
        self._cache_lock = asyncio.Lock()

    @property
    def available(self) -> bool:
        return (
            self.enabled
            and self._client is not None
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
        # An overview question can name a concrete entity without containing
        # the generic word "企画". Treat it as a focused event lookup too, so
        # the safe Firestore projection can complement documents, Discord and
        # source-code evidence for any named project.
        if entity_overview_subject(question) and not any(
            name == "events" for name, _config in selected
        ):
            selected.append(("events", COLLECTIONS["events"]))
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
            result_by_name = {
                name: documents
                for (name, _config), documents in zip(selected, results, strict=True)
            }
            ranking_public = self._ranking_results_are_public(
                result_by_name.get("rankingConfig")
            )
            for (name, config), documents in zip(selected, results, strict=True):
                if kind is QuestionKind.DATA_CONTRACT and name in FIREBASE_SCHEMA_CONTRACT_SOURCES:
                    if isinstance(documents, BaseException):
                        schema_documents: list[dict[str, Any]] = []
                        observation = "lookup-unavailable"
                    elif name == "ranking" and not ranking_public:
                        # Field names from unpublished aggregate documents are
                        # not required to describe the reviewed projection. Do
                        # not inspect them until the existing result gate opens.
                        schema_documents = []
                        observation = "withheld-until-public-result"
                    else:
                        schema_documents = documents
                        observation = (
                            "public-documents-observed"
                            if self._observed_allowlisted_fields(name, documents)
                            else "no-observed-public-fields"
                        )
                    evidence.append(
                        self._schema_contract_evidence(
                            name,
                            year,
                            schema_documents,
                            observation=observation,
                        )
                    )
                if isinstance(documents, BaseException):
                    log.warning("Firebase collection lookup failed for %s: %s", name, documents)
                    continue
                if name == "ranking" and not ranking_public:
                    log.info("Withholding ranking aggregate until public result state")
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

    @staticmethod
    def _ranking_results_are_public(documents: object) -> bool:
        if not isinstance(documents, list | tuple) or len(documents) != 1:
            return False
        fields = _document_fields(documents[0])
        status = str(fields.get("status") or "").strip().casefold()
        voting_phase = str(fields.get("votingPhase") or "").strip()
        return status in {"result", "published", "final", "結果発表"} or voting_phase == "最終結果発表"

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
        if self._client is None:
            return FirebaseDocumentBatch([], complete=False, reader_limit=self.limit)
        field_paths = self._wire_field_paths(name)
        if name == "config" or COLLECTIONS.get(name, {}).get("document") is True:
            payload = await self._client.get_document(path, field_paths)
            documents = FirebaseDocumentBatch(
                [payload] if payload else [],
                complete=True,
                reader_limit=1,
            )
        elif public_statuses:
            parent, collection_id = path.rsplit("/", 1)
            documents_list: list[dict[str, Any]] = []
            complete = False
            fetch_limit = self.max_documents + 1 if exhaustive else self.limit
            while len(documents_list) < fetch_limit:
                page_size = min(self.limit, fetch_limit - len(documents_list))
                page = list(
                    await self._client.run_status_query(
                        parent,
                        collection_id,
                        field_paths,
                        public_statuses,
                        page_size,
                        len(documents_list),
                    )
                )
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
            complete = False
            fetch_limit = self.max_documents + 1 if exhaustive else self.limit
            page_token: str | None = None
            while len(documents_list) < fetch_limit:
                page_size = min(self.limit, fetch_limit - len(documents_list))
                page = await self._client.list_collection(
                    path,
                    field_paths,
                    page_size,
                    page_token,
                )
                documents_list.extend(page.documents)
                page_token = page.next_page_token
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
            if not projected:
                continue
            searchable = json.dumps(projected, ensure_ascii=False, sort_keys=True).casefold()
            score = sum(1 for term in terms if term.casefold() in searchable)
            if terms and score == 0:
                continue
            path = document["name"].split("/documents/", 1)[-1]
            ranked.append(
                {
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
                        item["path"],
                    )
                )
                order = "publishedAt/createdAt ascending (oldest first)"
            elif source_type == "ranking":
                ranked.sort(key=self._ranking_sort_key)
                order = "category ascending, score descending, document path ascending"
            else:
                ranked.sort(key=lambda item: item["path"])
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
                        item["path"],
                    )
                )
            elif source_type == "ranking":
                ranked.sort(key=self._ranking_sort_key)
                selected = self._ranking_podium(ranked)
                order = "category ascending, score descending; top three ranks including ties"
            else:
                ranked.sort(key=lambda item: (-item["score"], item["path"]))
            if source_type != "ranking":
                selected = ranked[:5]
                order = "relevance descending"

        if not selected:
            return []

        registry_path = self._registry_path(source_type, festival)
        # Firebase Console encodes a Firestore path with ``~2F`` and expects
        # the root slash too.  Use only the reviewed registry path here; a
        # document ``name`` is fetched data and must never redefine the path we
        # present as the public data contract.
        url = self._console_url(registry_path)
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
            reference = f"{source_type}:{position}"
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
            path_key = self._registry_path_kind(source_type)
            body_lines = [
                header,
                f"{path_key}: {registry_path}",
                f"reference: {reference}",
            ]
            if item.get("computed_rank"):
                body_lines.append(f"computed_rank: {item['computed_rank']}")
            body_lines.append(
                f"fields: {json.dumps(item['projected'], ensure_ascii=False, sort_keys=True)}"
            )
            body = "\n".join(body_lines)
            evidence.append(
                FirebaseEvidence(
                    f"firebase:{reference}",
                    citation_title,
                    body,
                    url,
                )
            )
        return evidence

    @staticmethod
    def _registry_path(source_type: str, festival: int) -> str:
        """Resolve a Firestore path exclusively from the reviewed registry."""
        return str(COLLECTIONS[source_type]["path"]).format(festival=festival)

    @staticmethod
    def _registry_path_kind(source_type: str) -> str:
        return (
            "document_path"
            if COLLECTIONS[source_type].get("document") is True
            else "collection_path"
        )

    def _console_url(self, registry_path: str) -> str:
        console_path = "~2F" + quote(registry_path, safe="").replace("%2F", "~2F")
        return (
            f"https://console.firebase.google.com/project/{self.project_id}/"
            f"firestore/databases/-default-/data/{console_path}"
        )

    @classmethod
    def _observed_allowlisted_fields(
        cls,
        source_type: str,
        documents: list[dict[str, Any]],
    ) -> tuple[str, ...]:
        """Return safe top-level field names observed in fetched public data.

        Values still pass through the same projection used for ordinary
        evidence.  A field containing private data or an unreviewed nested
        shape is therefore not reported merely because its key existed.
        """
        observed: set[str] = set()
        for document in documents:
            fields = _document_fields(document)
            for key in COLLECTIONS[source_type]["fields"]:
                if key not in fields or fields[key] in (None, "", (), [], {}):
                    continue
                if cls._project_field(source_type, key, fields[key]) is not _DROP:
                    observed.add(str(key))
        return tuple(
            str(key)
            for key in COLLECTIONS[source_type]["fields"]
            if key in observed
        )

    def _schema_contract_evidence(
        self,
        source_type: str,
        festival: int,
        documents: list[dict[str, Any]],
        *,
        observation: str,
    ) -> FirebaseEvidence:
        """Build a value-free, deterministic public schema projection.

        The path and allowlist originate only in COLLECTIONS.  Firestore data
        contributes field *names* after safe projection, never a path or an
        unpublished value.
        """
        registry_path = self._registry_path(source_type, festival)
        path_kind = self._registry_path_kind(source_type)
        reference = f"schema:{source_type}"
        allowlisted_fields = tuple(str(item) for item in COLLECTIONS[source_type]["fields"])
        observed_fields = self._observed_allowlisted_fields(source_type, documents)
        body = "\n".join(
            (
                f"Firestore公開データ契約（{festival}th / {source_type}）",
                f"schema_contract: {FIREBASE_SCHEMA_CONTRACT_VERSION}",
                f"reference: {reference}",
                f"{path_kind}: {registry_path}",
                "allowlisted_fields: "
                + json.dumps(allowlisted_fields, ensure_ascii=False),
                "observed_fields: "
                + json.dumps(observed_fields, ensure_ascii=False),
                f"observation: {observation}",
                "scope: public-readonly-projection; not-a-complete-firestore-schema",
            )
        )
        return FirebaseEvidence(
            document_key=f"firebase:{reference}",
            title=(
                f"{festival}th {_SOURCE_LABELS.get(source_type, source_type)}"
                "の公開データ契約"
            ),
            body=body,
            source_url=self._console_url(registry_path),
            # The contract path and allowlist come from the deployed reader
            # registry, not from a Firestore document.  Current observations
            # are supplementary and never widen that reviewed projection.
            authority="deployed_implementation",
            status="deployed",
        )

    @classmethod
    def _wire_field_paths(cls, source_type: str) -> tuple[str, ...]:
        """Return the exact reviewed Firestore field mask for one source."""
        config = COLLECTIONS[source_type]
        result: list[str] = []
        for key in config["fields"]:
            schema = _FIELD_SCHEMAS.get(source_type, {}).get(key, _SCALAR)
            result.extend(cls._schema_field_paths(str(key), schema))
        return tuple(dict.fromkeys(result))

    @classmethod
    def _schema_field_paths(cls, prefix: str, schema: object) -> list[str]:
        if isinstance(schema, dict):
            result: list[str] = []
            for child_key, child_schema in schema.items():
                result.extend(
                    cls._schema_field_paths(f"{prefix}.{child_key}", child_schema)
                )
            return result
        return [prefix]

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
                r"(?:firebase|firestore)(?![A-Za-z0-9_])(?:を)?(?:読んで|見て|確認して)?|"
                r"大賞企画|Y-?1(?:グランプリ)?|ランキング|"
                r"ranking(?![A-Za-z0-9_])|"
                r"(?:公開)?状態|結果|得票(?:数)?|票数|順位|構造|"
                r"フィールド|fields?(?![A-Za-z0-9_])|スキーマ|"
                r"schema(?![A-Za-z0-9_])|"
                r"(?:票数)?(?:つき|付き)|どうなって(?:いた|いる|る|た)?",
                " ",
                question,
                flags=re.IGNORECASE,
            )
        elif source_type == "maps":
            question = re.sub(
                r"(?:firebase|firestore)(?![A-Za-z0-9_])(?:を)?(?:読んで|見て|確認して)?|"
                r"map(?![A-Za-z0-9_])|地図|マップ|構造|フィールド|"
                r"fields?(?![A-Za-z0-9_])|スキーマ|schema(?![A-Za-z0-9_])|現在|状態|"
                r"どうなって(?:いた|いる|る|た)?",
                " ",
                question,
                flags=re.IGNORECASE,
            )
        stripped = re.sub(
            r"整理券|企画|イベント|通知|お知らせ|現在|今|公開中?|"
            r"発行(?:済み|された|した)?|配信(?:済み|された|した)?|"
            r"開催状況|状況|一覧|最新|確認|すべて|全て|全部|全件|設定|"
            r"どうなって(?:いる|る|た)|教えて(?:ほしい|欲しい)?|"
            r"出して(?:ほしい|欲しい)?|表示(?:して)?|"
            r"ください|下さい|について|に関して|に関する|って|"
            r"知りたい|見たい|したい|[？?。、\s]",
            " ",
            question,
        )
        terms = []
        for part in stripped.split():
            # Request modifiers can remain after the entity/intent terms above
            # are removed (for example ``票数も含めて`` -> ``も含めて``).
            # They are not value filters.  Strip only a trailing modifier so a
            # real selector such as ``ITラボも含めて`` remains ``ITラボ``.
            part = re.sub(
                r"(?:も|を)?(?:含めて|あわせて|合わせて|まとめて)$",
                "",
                part,
            )
            if part in {"詳しく", "具体的に", "詳細に"}:
                continue
            # Removing an explicit festival marker can leave a Japanese
            # particle at the beginning (``27thのITラボ`` -> ``のITラボ``).
            # A leading particle is grammar, not part of the entity name.
            part = re.sub(r"^(?:の|は|を|が|に|で|と)+", "", part)
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
        return category, -numeric_score, str(item.get("path") or "")

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
    def _project_field(cls, source_type: str, key: str, value: Any) -> Any:
        schema = _FIELD_SCHEMAS.get(source_type, {}).get(key, _SCALAR)
        return cls._project_value(value, schema)

    @classmethod
    def _project_value(cls, value: Any, schema: object) -> Any:
        if schema == _SCALAR:
            if isinstance(value, str):
                return cls._safe_public_text(value)
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
    def _safe_public_text(value: str) -> str | object:
        """Drop high-confidence credentials and private contacts before the LLM."""
        # Remove formatting controls such as zero-width spaces before scanning
        # so ordinary copied text survives while obfuscated secrets become
        # detectable. Preserve ZWJ because it is part of emoji sequences.
        inspected_characters: list[str] = []
        for character in value:
            category = unicodedata.category(character)
            if category == "Cf" and character != "\u200d":
                continue
            if category[0] == "C" and character not in {"\n", "\r", "\t", "\u200d"}:
                return _DROP
            inspected_characters.append(character)
            if len(inspected_characters) >= 8_192:
                break
        inspected = "".join(inspected_characters)
        normalized = unicodedata.normalize("NFKC", inspected)
        variants = (
            normalized,
            "".join(
                character
                for character in normalized
                if unicodedata.category(character)[0] != "M" and character != "\u200d"
            ),
        )
        for candidate in dict.fromkeys(variants):
            if (
                _CONTACT_EMAIL.search(candidate)
                or _CONTACT_PHONE.search(candidate)
                or _LABELED_PRIVATE_DATA.search(candidate)
                or any(pattern.search(candidate) for pattern in _OPAQUE_SECRET_PATTERNS)
            ):
                return _DROP
        return inspected[:500]

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
