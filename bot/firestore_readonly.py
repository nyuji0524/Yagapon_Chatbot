"""Constrained, read-only transport for the Firestore REST API.

The transport deliberately exposes only the three read operations used by the
Firebase knowledge reader.  Callers provide relative Firestore paths and field
masks; they cannot select an HTTP method, database, or arbitrary URL.
"""

from __future__ import annotations

import asyncio
import math
import re
from dataclasses import dataclass
from typing import Any, Final, Literal
from urllib.parse import quote

import aiohttp
import google.auth
from google.auth.transport.requests import Request

FIRESTORE_SCOPE: Final = "https://www.googleapis.com/auth/datastore"
_BACKENDS: Final = frozenset({"emulator", "firestore", "public"})
_FIELD_PATH = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*\Z")
_PROJECT_ID = re.compile(r"[a-z][a-z0-9.-]{2,62}\Z")
_MAX_PATH_SEGMENTS: Final = 100
_MAX_SEGMENT_LENGTH: Final = 1_500
_MAX_FIELD_PATHS: Final = 100
_MAX_PAGE_SIZE: Final = 1_000
_MAX_QUERY_LIMIT: Final = 1_000
_MAX_QUERY_OFFSET: Final = 10_000
_MAX_STATUSES: Final = 30
_MAX_STATUS_LENGTH: Final = 128

JsonObject = dict[str, Any]
Backend = Literal["emulator", "firestore", "public"]


@dataclass(frozen=True)
class FirestoreListPage:
    """One validated page returned by Firestore's list-documents endpoint."""

    documents: tuple[JsonObject, ...]
    next_page_token: str | None


class FirestoreReadonlyClient:
    """Minimal Firestore REST client whose public surface cannot write data."""

    def __init__(
        self,
        *,
        project_id: str,
        backend: Backend | str,
        emulator_host: str | None = None,
        api_key: str | None = None,
        timeout: float = 8.0,
    ) -> None:
        normalized_backend = str(backend).strip().lower()
        if normalized_backend not in _BACKENDS:
            raise ValueError(f"unsupported Firestore backend: {backend!r}")
        normalized_project = project_id.strip()
        if not _PROJECT_ID.fullmatch(normalized_project):
            raise ValueError("project_id is not a valid Google Cloud project identifier")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise TypeError("timeout must be a number")
        normalized_timeout = float(timeout)
        if not math.isfinite(normalized_timeout) or not 0.1 <= normalized_timeout <= 60.0:
            raise ValueError("timeout must be between 0.1 and 60 seconds")

        normalized_host = (emulator_host or "127.0.0.1:8080").strip().rstrip("/")
        if normalized_backend == "emulator":
            if not normalized_host or "://" in normalized_host or any(
                character in normalized_host for character in "/?#"
            ):
                raise ValueError("emulator_host must be a host[:port], without a URL scheme")
            root = f"http://{normalized_host}"
        else:
            root = "https://firestore.googleapis.com"

        self._backend: Backend = normalized_backend  # type: ignore[assignment]
        self._project_id = normalized_project
        self._api_key = (api_key or "").strip() if normalized_backend == "public" else ""
        self._timeout_seconds = normalized_timeout
        quoted_project = quote(normalized_project, safe="")
        self._documents_url = (
            f"{root}/v1/projects/{quoted_project}/databases/(default)/documents"
        )
        self._resource_prefix = (
            f"projects/{normalized_project}/databases/(default)/documents/"
        )
        self._credentials: Any | None = None
        self._credentials_lock = asyncio.Lock()

    async def get_document(
        self,
        path: str,
        field_paths: tuple[str, ...] | list[str],
    ) -> JsonObject | None:
        """Get exactly one reviewed document, projected through a field mask."""
        segments = _validate_relative_path(path, document=True)
        fields = _validate_field_paths(field_paths)
        relative_path = "/".join(segments)
        response = await self._get(
            self._url_for_segments(segments),
            params=[("mask.fieldPaths", field) for field in fields],
            allow_not_found=True,
        )
        if response is None:
            return None
        document = _require_object(response, "get-document response")
        self._validate_document_resource(document, expected_path=relative_path)
        _validate_document_fields(document, fields)
        return document

    async def list_collection(
        self,
        path: str,
        field_paths: tuple[str, ...] | list[str],
        page_size: int,
        page_token: str | None = None,
    ) -> FirestoreListPage:
        """List direct documents in one reviewed collection with a field mask."""
        segments = _validate_relative_path(path, document=False)
        fields = _validate_field_paths(field_paths)
        size = _bounded_int("page_size", page_size, minimum=1, maximum=_MAX_PAGE_SIZE)
        token = _validate_page_token(page_token)
        params = [("pageSize", str(size))]
        params.extend(("mask.fieldPaths", field) for field in fields)
        if token is not None:
            params.append(("pageToken", token))

        response = await self._get(
            self._url_for_segments(segments),
            params=params,
            allow_not_found=False,
        )
        payload = _require_object(response, "list-collection response")
        raw_documents = payload.get("documents", [])
        if not isinstance(raw_documents, list):
            raise RuntimeError("Firestore list-collection response has invalid documents")
        if len(raw_documents) > size:
            raise RuntimeError("Firestore list-collection response exceeded the requested page size")
        collection_path = "/".join(segments)
        documents: list[JsonObject] = []
        for raw_document in raw_documents:
            document = _require_object(raw_document, "listed document")
            self._validate_document_resource(document, collection_path=collection_path)
            _validate_document_fields(document, fields)
            documents.append(document)
        next_token = payload.get("nextPageToken")
        if next_token is not None and not isinstance(next_token, str):
            raise RuntimeError("Firestore list-collection response has invalid nextPageToken")
        return FirestoreListPage(
            documents=tuple(documents),
            next_page_token=next_token or None,
        )

    async def run_status_query(
        self,
        parent: str,
        collection_id: str,
        field_paths: tuple[str, ...] | list[str],
        statuses: tuple[str, ...] | list[str],
        limit: int,
        offset: int = 0,
    ) -> tuple[JsonObject, ...]:
        """Query one child collection by reviewed public statuses."""
        parent_segments = _validate_relative_path(parent, document=True)
        collection_segment = _validate_single_segment("collection_id", collection_id)
        fields = _validate_field_paths(field_paths)
        selected_statuses = _validate_statuses(statuses)
        query_limit = _bounded_int("limit", limit, minimum=1, maximum=_MAX_QUERY_LIMIT)
        query_offset = _bounded_int("offset", offset, minimum=0, maximum=_MAX_QUERY_OFFSET)

        if len(selected_statuses) == 1:
            operator = "EQUAL"
            value: JsonObject = {"stringValue": selected_statuses[0]}
        else:
            operator = "IN"
            value = {
                "arrayValue": {
                    "values": [
                        {"stringValue": status} for status in selected_statuses
                    ]
                }
            }
        structured_query: JsonObject = {
            "select": {"fields": [{"fieldPath": field} for field in fields]},
            "from": [{"collectionId": collection_segment}],
            "where": {
                "fieldFilter": {
                    "field": {"fieldPath": "status"},
                    "op": operator,
                    "value": value,
                }
            },
            "orderBy": [
                {
                    "field": {"fieldPath": "__name__"},
                    "direction": "ASCENDING",
                }
            ],
            "limit": query_limit,
        }
        if query_offset:
            structured_query["offset"] = query_offset

        url = f"{self._url_for_segments(parent_segments)}:runQuery"
        response = await self._post_run_query(
            url,
            payload={"structuredQuery": structured_query},
        )
        if not isinstance(response, list):
            raise RuntimeError("Firestore runQuery response is not a list")
        collection_path = "/".join((*parent_segments, collection_segment))
        documents: list[JsonObject] = []
        for raw_row in response:
            row = _require_object(raw_row, "runQuery row")
            if "document" not in row:
                continue
            document = _require_object(row["document"], "queried document")
            self._validate_document_resource(document, collection_path=collection_path)
            _validate_document_fields(document, fields)
            documents.append(document)
            if len(documents) > query_limit:
                raise RuntimeError("Firestore runQuery response exceeded the requested limit")
        return tuple(documents)

    async def _get(
        self,
        url: str,
        *,
        params: list[tuple[str, str]],
        allow_not_found: bool,
    ) -> Any:
        headers = await self._headers()
        timeout = aiohttp.ClientTimeout(total=self._timeout_seconds)
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.get(url, params=params) as response:
                if allow_not_found and response.status == 404:
                    return None
                return await _decode_response(response)

    async def _post_run_query(self, url: str, *, payload: JsonObject) -> Any:
        headers = await self._headers()
        timeout = aiohttp.ClientTimeout(total=self._timeout_seconds)
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.post(url, json=payload) as response:
                return await _decode_response(response)

    async def _headers(self) -> dict[str, str]:
        if self._backend == "public":
            return {"X-Goog-Api-Key": self._api_key} if self._api_key else {}
        if self._backend == "firestore":
            return {"Authorization": f"Bearer {await self._access_token()}"}
        return {}

    async def _access_token(self) -> str:
        async with self._credentials_lock:
            if self._credentials is None:
                credentials, _ = await asyncio.to_thread(
                    google.auth.default,
                    scopes=[FIRESTORE_SCOPE],
                )
                self._credentials = credentials
            if not self._credentials.valid or not self._credentials.token:
                await asyncio.to_thread(self._credentials.refresh, Request())
            token = self._credentials.token
            if not isinstance(token, str) or not token:
                raise RuntimeError("Firestore ADC did not provide an access token")
            return token

    def _url_for_segments(self, segments: tuple[str, ...]) -> str:
        encoded_path = "/".join(quote(segment, safe="-._~") for segment in segments)
        return f"{self._documents_url}/{encoded_path}"

    def _validate_document_resource(
        self,
        document: JsonObject,
        *,
        expected_path: str | None = None,
        collection_path: str | None = None,
    ) -> None:
        name = document.get("name")
        if not isinstance(name, str) or not name.startswith(self._resource_prefix):
            raise RuntimeError("Firestore returned a document outside the configured database")
        relative_path = name[len(self._resource_prefix) :]
        try:
            resource_segments = _validate_relative_path(relative_path, document=True)
        except (TypeError, ValueError) as error:
            raise RuntimeError("Firestore returned an invalid document resource name") from error
        normalized_path = "/".join(resource_segments)
        if expected_path is not None and normalized_path != expected_path:
            raise RuntimeError("Firestore returned a document outside the requested path")
        if collection_path is not None:
            prefix = f"{collection_path}/"
            child_id = normalized_path[len(prefix) :] if normalized_path.startswith(prefix) else ""
            if not child_id or "/" in child_id:
                raise RuntimeError("Firestore returned a document outside the requested collection")


async def _decode_response(response: Any) -> Any:
    if response.status >= 400:
        detail = (await response.text())[:300]
        raise RuntimeError(f"Firestore read failed: {response.status} {detail}")
    return await response.json()


def _require_object(value: Any, label: str) -> JsonObject:
    if not isinstance(value, dict):
        raise RuntimeError(f"Firestore {label} is not an object")
    return value


def _validate_document_fields(document: JsonObject, field_paths: tuple[str, ...]) -> None:
    """Fail closed if a backend returns fields outside the requested mask."""
    raw_fields = document.get("fields", {})
    if not isinstance(raw_fields, dict):
        raise RuntimeError("Firestore document has invalid fields")
    mask_tree: dict[str, Any] = {}
    for field_path in field_paths:
        node = mask_tree
        for segment in field_path.split("."):
            node = node.setdefault(segment, {})
    _validate_masked_fields(raw_fields, mask_tree)


def _validate_masked_fields(fields: dict[str, Any], mask_tree: dict[str, Any]) -> None:
    for field_name, encoded_value in fields.items():
        child_mask = mask_tree.get(field_name)
        if child_mask is None:
            raise RuntimeError("Firestore returned a field outside the requested mask")
        if not child_mask:
            continue
        if not isinstance(encoded_value, dict):
            raise RuntimeError("Firestore returned an invalid nested field value")
        map_value = encoded_value.get("mapValue")
        if not isinstance(map_value, dict):
            raise RuntimeError("Firestore returned a non-map value for a nested field mask")
        nested_fields = map_value.get("fields", {})
        if not isinstance(nested_fields, dict):
            raise RuntimeError("Firestore returned invalid nested map fields")
        _validate_masked_fields(nested_fields, child_mask)


def _validate_relative_path(path: str, *, document: bool) -> tuple[str, ...]:
    if not isinstance(path, str):
        raise TypeError("Firestore path must be a string")
    if not path or path != path.strip() or path.startswith("/") or path.endswith("/"):
        raise ValueError("Firestore path must be a non-empty relative path")
    segments = tuple(path.split("/"))
    if len(segments) > _MAX_PATH_SEGMENTS:
        raise ValueError("Firestore path has too many segments")
    expected_parity = 0 if document else 1
    if len(segments) % 2 != expected_parity:
        kind = "document" if document else "collection"
        raise ValueError(f"Firestore path is not a {kind} path")
    return tuple(_validate_single_segment("path segment", segment) for segment in segments)


def _validate_single_segment(label: str, segment: str) -> str:
    if not isinstance(segment, str):
        raise TypeError(f"{label} must be a string")
    if (
        not segment
        or segment in {".", ".."}
        or len(segment) > _MAX_SEGMENT_LENGTH
        or any(character in segment for character in "/\\?#%")
    ):
        raise ValueError(f"invalid Firestore {label}")
    return segment


def _validate_field_paths(field_paths: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    if isinstance(field_paths, (str, bytes)) or not isinstance(field_paths, (tuple, list)):
        raise TypeError("field_paths must be a tuple or list")
    if not 1 <= len(field_paths) <= _MAX_FIELD_PATHS:
        raise ValueError(f"field_paths must contain between 1 and {_MAX_FIELD_PATHS} entries")
    validated: list[str] = []
    seen: set[str] = set()
    for field_path in field_paths:
        if not isinstance(field_path, str) or not _FIELD_PATH.fullmatch(field_path):
            raise ValueError("field paths must be simple dotted identifiers")
        if field_path in seen:
            raise ValueError("field_paths must not contain duplicates")
        seen.add(field_path)
        validated.append(field_path)
    return tuple(validated)


def _validate_statuses(statuses: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    if isinstance(statuses, (str, bytes)) or not isinstance(statuses, (tuple, list)):
        raise TypeError("statuses must be a tuple or list")
    if not 1 <= len(statuses) <= _MAX_STATUSES:
        raise ValueError(f"statuses must contain between 1 and {_MAX_STATUSES} entries")
    validated: list[str] = []
    seen: set[str] = set()
    for status in statuses:
        if (
            not isinstance(status, str)
            or status != status.strip()
            or not status
            or len(status) > _MAX_STATUS_LENGTH
            or any(ord(character) < 32 for character in status)
        ):
            raise ValueError("invalid Firestore status")
        if status in seen:
            raise ValueError("statuses must not contain duplicates")
        seen.add(status)
        validated.append(status)
    return tuple(validated)


def _validate_page_token(page_token: str | None) -> str | None:
    if page_token in (None, ""):
        return None
    if not isinstance(page_token, str):
        raise TypeError("page_token must be a string or None")
    if len(page_token) > 4_096 or any(ord(character) < 32 for character in page_token):
        raise ValueError("invalid Firestore page_token")
    return page_token


def _bounded_int(name: str, value: int, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value
