import inspect
from dataclasses import dataclass
from typing import Any

import pytest

from bot.firestore_readonly import (
    FIRESTORE_SCOPE,
    FirestoreListPage,
    FirestoreReadonlyClient,
)


def _resource(project: str, path: str) -> str:
    return f"projects/{project}/databases/(default)/documents/{path}"


class _FakeResponse:
    def __init__(self, status: int, payload: Any) -> None:
        self.status = status
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def json(self):
        return self._payload

    async def text(self):
        return str(self._payload)


class _FakeSession:
    def __init__(self, recorder, *, timeout, headers) -> None:
        self.recorder = recorder
        self.recorder.session_options.append({"timeout": timeout, "headers": headers})

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    def get(self, url, *, params):
        self.recorder.requests.append({"method": "GET", "url": url, "params": params})
        return self.recorder.next_response()

    def post(self, url, *, json):
        self.recorder.requests.append({"method": "POST", "url": url, "json": json})
        return self.recorder.next_response()


class _Recorder:
    def __init__(self, *responses: _FakeResponse) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []
        self.session_options: list[dict[str, Any]] = []

    def session(self, *, timeout, headers):
        return _FakeSession(self, timeout=timeout, headers=headers)

    def next_response(self):
        assert self.responses, "unexpected HTTP request"
        return self.responses.pop(0)


@pytest.mark.asyncio
async def test_public_get_uses_api_key_and_repeated_field_mask(monkeypatch):
    document = {
        "name": _resource("yagamy26th", "yagamy/27/config/standbyPass"),
        "fields": {"status": {"stringValue": "open"}},
    }
    recorder = _Recorder(_FakeResponse(200, document))
    monkeypatch.setattr("bot.firestore_readonly.aiohttp.ClientSession", recorder.session)
    monkeypatch.setattr(
        "bot.firestore_readonly.google.auth.default",
        lambda **kwargs: pytest.fail("public backend must not load ADC"),
    )
    client = FirestoreReadonlyClient(
        project_id="yagamy26th",
        backend="public",
        api_key="public-browser-key",
        timeout=7,
    )

    result = await client.get_document(
        "yagamy/27/config/standbyPass",
        ("status", "action.url"),
    )

    assert result == document
    assert recorder.requests == [
        {
            "method": "GET",
            "url": (
                "https://firestore.googleapis.com/v1/projects/yagamy26th/"
                "databases/(default)/documents/yagamy/27/config/standbyPass"
            ),
            "params": [
                ("mask.fieldPaths", "status"),
                ("mask.fieldPaths", "action.url"),
            ],
        }
    ]
    assert recorder.session_options[0]["headers"] == {
        "X-Goog-Api-Key": "public-browser-key"
    }
    assert recorder.session_options[0]["timeout"].total == 7


@pytest.mark.asyncio
async def test_list_collection_masks_fields_and_preserves_page_token(monkeypatch):
    first = {
        "name": _resource("demo-project", "festival/27/notifications/notice-1"),
        "fields": {"title": {"stringValue": "公開通知"}},
    }
    recorder = _Recorder(
        _FakeResponse(
            200,
            {"documents": [first], "nextPageToken": "opaque+/token=="},
        )
    )
    monkeypatch.setattr("bot.firestore_readonly.aiohttp.ClientSession", recorder.session)
    client = FirestoreReadonlyClient(project_id="demo-project", backend="public")

    page = await client.list_collection(
        "festival/27/notifications",
        ["title", "publishedAt"],
        page_size=50,
        page_token="previous+/token==",
    )

    assert page == FirestoreListPage((first,), "opaque+/token==")
    assert recorder.requests[0]["method"] == "GET"
    assert recorder.requests[0]["params"] == [
        ("pageSize", "50"),
        ("mask.fieldPaths", "title"),
        ("mask.fieldPaths", "publishedAt"),
        ("pageToken", "previous+/token=="),
    ]


@pytest.mark.asyncio
async def test_status_query_uses_select_fixed_filter_order_limit_and_offset(monkeypatch):
    document = {
        "name": _resource("demo-project", "festival/27/notifications/notice-1"),
        "fields": {"status": {"stringValue": "published"}},
    }
    recorder = _Recorder(_FakeResponse(200, [{"document": document}, {"readTime": "now"}]))
    monkeypatch.setattr("bot.firestore_readonly.aiohttp.ClientSession", recorder.session)
    client = FirestoreReadonlyClient(project_id="demo-project", backend="public")

    result = await client.run_status_query(
        "festival/27",
        "notifications",
        ("title", "status", "action.url"),
        ("published", "archived"),
        limit=200,
        offset=40,
    )

    assert result == (document,)
    request = recorder.requests[0]
    assert request["method"] == "POST"
    assert request["url"].endswith(
        "/databases/(default)/documents/festival/27:runQuery"
    )
    query = request["json"]["structuredQuery"]
    assert query == {
        "select": {
            "fields": [
                {"fieldPath": "title"},
                {"fieldPath": "status"},
                {"fieldPath": "action.url"},
            ]
        },
        "from": [{"collectionId": "notifications"}],
        "where": {
            "fieldFilter": {
                "field": {"fieldPath": "status"},
                "op": "IN",
                "value": {
                    "arrayValue": {
                        "values": [
                            {"stringValue": "published"},
                            {"stringValue": "archived"},
                        ]
                    }
                },
            }
        },
        "orderBy": [
            {
                "field": {"fieldPath": "__name__"},
                "direction": "ASCENDING",
            }
        ],
        "limit": 200,
        "offset": 40,
    }


@pytest.mark.asyncio
async def test_single_status_query_uses_equal_and_omits_zero_offset(monkeypatch):
    recorder = _Recorder(_FakeResponse(200, []))
    monkeypatch.setattr("bot.firestore_readonly.aiohttp.ClientSession", recorder.session)
    client = FirestoreReadonlyClient(project_id="demo-project", backend="public")

    await client.run_status_query(
        "festival/27",
        "notifications",
        ("title",),
        ("published",),
        limit=20,
    )

    query = recorder.requests[0]["json"]["structuredQuery"]
    assert query["where"]["fieldFilter"]["op"] == "EQUAL"
    assert query["where"]["fieldFilter"]["value"] == {"stringValue": "published"}
    assert "offset" not in query


@dataclass
class _Credentials:
    token: str = "adc-token"
    valid: bool = True
    refresh_count: int = 0

    def refresh(self, request):
        self.refresh_count += 1
        self.valid = True
        self.token = "refreshed-token"


@pytest.mark.asyncio
async def test_firestore_backend_uses_scoped_adc_and_never_api_key(monkeypatch):
    document = {
        "name": _resource("demo-project", "festival/27/config/runtime"),
        "fields": {},
    }
    recorder = _Recorder(_FakeResponse(200, document))
    credentials = _Credentials()
    adc_calls = []

    def fake_default(*, scopes):
        adc_calls.append(scopes)
        return credentials, "demo-project"

    monkeypatch.setattr("bot.firestore_readonly.aiohttp.ClientSession", recorder.session)
    monkeypatch.setattr("bot.firestore_readonly.google.auth.default", fake_default)
    client = FirestoreReadonlyClient(
        project_id="demo-project",
        backend="firestore",
        api_key="must-not-leak",
    )

    await client.get_document("festival/27/config/runtime", ["status"])

    assert adc_calls == [[FIRESTORE_SCOPE]]
    assert recorder.session_options[0]["headers"] == {
        "Authorization": "Bearer adc-token"
    }
    assert "must-not-leak" not in repr(recorder.requests + recorder.session_options)


@pytest.mark.asyncio
async def test_firestore_backend_refreshes_invalid_adc(monkeypatch):
    document = {
        "name": _resource("demo-project", "festival/27/config/runtime"),
        "fields": {},
    }
    recorder = _Recorder(_FakeResponse(200, document))
    credentials = _Credentials(token="", valid=False)
    monkeypatch.setattr("bot.firestore_readonly.aiohttp.ClientSession", recorder.session)
    monkeypatch.setattr(
        "bot.firestore_readonly.google.auth.default",
        lambda *, scopes: (credentials, "demo-project"),
    )
    client = FirestoreReadonlyClient(project_id="demo-project", backend="firestore")

    await client.get_document("festival/27/config/runtime", ["status"])

    assert credentials.refresh_count == 1
    assert recorder.session_options[0]["headers"] == {
        "Authorization": "Bearer refreshed-token"
    }


@pytest.mark.asyncio
async def test_emulator_uses_fixed_local_database_without_auth(monkeypatch):
    document = {
        "name": _resource("demo-project", "festival/27/config/runtime"),
        "fields": {},
    }
    recorder = _Recorder(_FakeResponse(200, document))
    monkeypatch.setattr("bot.firestore_readonly.aiohttp.ClientSession", recorder.session)
    monkeypatch.setattr(
        "bot.firestore_readonly.google.auth.default",
        lambda **kwargs: pytest.fail("emulator backend must not load ADC"),
    )
    client = FirestoreReadonlyClient(
        project_id="demo-project",
        backend="emulator",
        emulator_host="firestore:8080",
        api_key="must-not-leak",
    )

    await client.get_document("festival/27/config/runtime", ["status"])

    assert recorder.requests[0]["url"].startswith(
        "http://firestore:8080/v1/projects/demo-project/databases/(default)/documents/"
    )
    assert recorder.session_options[0]["headers"] == {}
    assert "must-not-leak" not in repr(recorder.requests + recorder.session_options)


@pytest.mark.asyncio
async def test_get_returns_none_only_for_document_not_found(monkeypatch):
    recorder = _Recorder(_FakeResponse(404, {"error": "missing"}))
    monkeypatch.setattr("bot.firestore_readonly.aiohttp.ClientSession", recorder.session)
    client = FirestoreReadonlyClient(project_id="demo-project", backend="public")

    assert await client.get_document("festival/27/config/runtime", ["status"]) is None


@pytest.mark.asyncio
async def test_response_resource_must_match_project_database_and_collection(monkeypatch):
    cross_project = {
        "name": _resource("other-project", "festival/27/notifications/notice-1"),
        "fields": {},
    }
    nested_descendant = {
        "name": _resource(
            "demo-project",
            "festival/27/notifications/notice-1/private/secret",
        ),
        "fields": {},
    }
    recorder = _Recorder(
        _FakeResponse(200, {"documents": [cross_project]}),
        _FakeResponse(200, {"documents": [nested_descendant]}),
    )
    monkeypatch.setattr("bot.firestore_readonly.aiohttp.ClientSession", recorder.session)
    client = FirestoreReadonlyClient(project_id="demo-project", backend="public")

    with pytest.raises(RuntimeError, match="configured database"):
        await client.list_collection("festival/27/notifications", ["title"], 10)
    with pytest.raises(RuntimeError, match="requested collection"):
        await client.list_collection("festival/27/notifications", ["title"], 10)


@pytest.mark.asyncio
async def test_response_fields_must_be_a_subset_of_exact_mask(monkeypatch):
    unrequested_top_level = {
        "name": _resource("demo-project", "festival/27/notifications/notice-1"),
        "fields": {
            "title": {"stringValue": "公開通知"},
            "ownerEmail": {"stringValue": "private@example.com"},
        },
    }
    unrequested_nested = {
        "name": _resource("demo-project", "festival/27/notifications/notice-2"),
        "fields": {
            "action": {
                "mapValue": {
                    "fields": {
                        "url": {"stringValue": "https://example.com"},
                        "token": {"stringValue": "private-token"},
                    }
                }
            },
        },
    }
    recorder = _Recorder(
        _FakeResponse(200, unrequested_top_level),
        _FakeResponse(200, unrequested_nested),
    )
    monkeypatch.setattr("bot.firestore_readonly.aiohttp.ClientSession", recorder.session)
    client = FirestoreReadonlyClient(project_id="demo-project", backend="public")

    with pytest.raises(RuntimeError, match="outside the requested mask"):
        await client.get_document("festival/27/notifications/notice-1", ["title"])
    with pytest.raises(RuntimeError, match="outside the requested mask"):
        await client.get_document(
            "festival/27/notifications/notice-2",
            ["action.url"],
        )


@pytest.mark.asyncio
async def test_nested_field_mask_accepts_canonical_null_but_rejects_mixed_value(monkeypatch):
    canonical_null = {
        "name": _resource("demo-project", "festival/27/notifications/notice-1"),
        "fields": {"action": {"nullValue": None}},
    }
    mixed_null = {
        "name": _resource("demo-project", "festival/27/notifications/notice-2"),
        "fields": {
            "action": {
                "nullValue": None,
                "mapValue": {
                    "fields": {"token": {"stringValue": "private-token"}}
                },
            }
        },
    }
    recorder = _Recorder(
        _FakeResponse(200, canonical_null),
        _FakeResponse(200, mixed_null),
    )
    monkeypatch.setattr("bot.firestore_readonly.aiohttp.ClientSession", recorder.session)
    client = FirestoreReadonlyClient(project_id="demo-project", backend="public")

    assert await client.get_document(
        "festival/27/notifications/notice-1",
        ["action.url"],
    ) == canonical_null
    with pytest.raises(RuntimeError, match="outside the requested mask"):
        await client.get_document(
            "festival/27/notifications/notice-2",
            ["action.url"],
        )


@pytest.mark.asyncio
async def test_response_cannot_exceed_requested_read_bounds(monkeypatch):
    documents = [
        {
            "name": _resource(
                "demo-project",
                f"festival/27/notifications/notice-{index}",
            ),
            "fields": {},
        }
        for index in range(2)
    ]
    recorder = _Recorder(
        _FakeResponse(200, {"documents": documents}),
        _FakeResponse(200, [{"document": document} for document in documents]),
    )
    monkeypatch.setattr("bot.firestore_readonly.aiohttp.ClientSession", recorder.session)
    client = FirestoreReadonlyClient(project_id="demo-project", backend="public")

    with pytest.raises(RuntimeError, match="page size"):
        await client.list_collection("festival/27/notifications", ["title"], 1)
    with pytest.raises(RuntimeError, match="requested limit"):
        await client.run_status_query(
            "festival/27",
            "notifications",
            ["title", "status"],
            ["published"],
            limit=1,
        )


@pytest.mark.parametrize(
    ("path", "is_document"),
    [
        ("/festival/27", True),
        ("festival/27/", True),
        ("festival/../config/runtime", True),
        ("festival/%2F/config/runtime", True),
        ("festival/%2e%2e/config/runtime", True),
        ("festival/27/notifications", True),
        ("festival/27", False),
    ],
)
@pytest.mark.asyncio
async def test_rejects_traversal_encoded_slash_and_wrong_path_shape(path, is_document):
    client = FirestoreReadonlyClient(project_id="demo-project", backend="public")

    with pytest.raises(ValueError):
        if is_document:
            await client.get_document(path, ["status"])
        else:
            await client.list_collection(path, ["status"], 10)


@pytest.mark.parametrize(
    "field_paths",
    [[], ["action.`url`"], ["title/status"], ["title[0]"], ["title", "title"]],
)
@pytest.mark.asyncio
async def test_rejects_unreviewable_field_paths(field_paths):
    client = FirestoreReadonlyClient(project_id="demo-project", backend="public")

    with pytest.raises(ValueError):
        await client.get_document("festival/27/config/runtime", field_paths)


@pytest.mark.parametrize(
    ("page_size", "limit", "offset"),
    [(0, 1, 0), (1_001, 1, 0), (1, 0, 0), (1, 1_001, 0), (1, 1, -1), (1, 1, 10_001)],
)
@pytest.mark.asyncio
async def test_rejects_out_of_bounds_pagination(page_size, limit, offset):
    client = FirestoreReadonlyClient(project_id="demo-project", backend="public")

    if page_size != 1:
        with pytest.raises(ValueError):
            await client.list_collection("festival/27/notifications", ["title"], page_size)
    else:
        with pytest.raises(ValueError):
            await client.run_status_query(
                "festival/27",
                "notifications",
                ["title"],
                ["published"],
                limit,
                offset,
            )


def test_public_surface_has_no_arbitrary_or_write_capable_operation():
    public_methods = {
        name
        for name, member in inspect.getmembers(FirestoreReadonlyClient, inspect.isfunction)
        if not name.startswith("_")
    }

    assert public_methods == {"get_document", "list_collection", "run_status_query"}
    assert not public_methods & {"request", "post", "put", "patch", "delete", "commit", "write"}


@pytest.mark.parametrize("backend", ["disabled", "rest", "production"])
def test_unknown_backend_is_rejected(backend):
    with pytest.raises(ValueError, match="unsupported Firestore backend"):
        FirestoreReadonlyClient(project_id="demo-project", backend=backend)


def test_emulator_host_cannot_override_url_shape():
    with pytest.raises(ValueError, match="host"):
        FirestoreReadonlyClient(
            project_id="demo-project",
            backend="emulator",
            emulator_host="https://attacker.invalid/path",
        )
