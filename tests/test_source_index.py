import hashlib
import json
from types import SimpleNamespace

import pytest

import source_index.__main__ as source_index_module
from bot.rag_store import RagStore
from source_index.__main__ import (
    SOURCE_POLICY_VERSION,
    Repository,
    chunks_for,
    contains_secret,
    deployed_refs,
    document_policy,
    gcs_snapshot,
    inferred_festival,
    load_repositories,
    metadata_digest_for,
    metadata_for,
    repository_manifest_digest,
    repository_state_is_current,
    safe_source_path,
    source_upload_concurrency,
    stale_managed_document_keys,
    state_entry_matches_chunk,
    sync_once,
)


def curated_repository() -> Repository:
    return Repository(
        name="YagamiFes-IT/YagamiFes-IT-Knowledge",
        ref="main",
        include=("knowledge/",),
        source_type="knowledge_repository",
        authority="curated",
        status="current",
        front_matter_statuses=("active", "historical"),
    )


def test_source_path_allowlist_excludes_generated_credentials_and_lockfiles():
    includes = ("lib/", "functions/", "Dockerfile")

    assert safe_source_path("lib/auth.dart", includes, 100)
    assert safe_source_path("functions/auth.js", includes, 100)
    assert safe_source_path("Dockerfile", includes, 100)
    assert not safe_source_path("node_modules/pkg/index.js", includes, 100)
    assert not safe_source_path("lib/firebase_options.dart", includes, 100)
    assert not safe_source_path("functions/package-lock.json", includes, 100)
    assert not safe_source_path("bot/trusted_knowledge.json", ("bot/",), 100)
    assert not safe_source_path(".env", includes, 100)


def test_source_path_allowlist_respects_file_and_directory_boundaries():
    assert safe_source_path("bot/corpus.py", ("bot/",), 100)
    assert not safe_source_path("bottle/corpus.py", ("bot/",), 100)
    assert safe_source_path("main.py", ("main.py",), 100)
    assert not safe_source_path("main.py.backup", ("main.py",), 100)


def test_source_secret_scanner_allows_environment_lookup_but_blocks_literal():
    assert not contains_secret('token = os.environ.get("GITHUB_TOKEN", "")')
    assert contains_secret('api_key = "1234567890abcdefghijklmnop"')


def test_curated_document_policy_allows_only_approved_front_matter():
    source = "---\nstatus: active\nauthority: reviewed_record\nfestival: 27\n---\n本文"

    assert document_policy(curated_repository(), "knowledge/example.md", source) == (
        "reviewed_record",
        "active",
        27,
        "restricted",
        None,
        None,
    )


def test_curated_document_policy_keeps_only_explicitly_reviewed_legacy_pages_visible():
    source = "---\nstatus: active\nfestival: 27\n---\n本文"

    reviewed = document_policy(
        curated_repository(),
        "knowledge/operations/yagapon-intake-retry.md",
        source,
    )
    unknown = document_policy(
        curated_repository(),
        "knowledge/operations/unknown-legacy.md",
        source,
    )

    assert reviewed[3] == "guild"
    assert unknown[3] == "restricted"


def test_repository_policy_uses_resolved_deployment_authority():
    repository = Repository(
        name="nyuji0524/Yagapon_Chatbot",
        ref="main",
        include=("bot/",),
    )

    assert document_policy(
        repository,
        "bot/client.py",
        "print('running')",
        default_authority="deployed_implementation",
        default_status="deployed",
    ) == ("deployed_implementation", "deployed", None, "guild", None, None)


def test_curated_document_policy_rejects_draft_and_missing_front_matter():
    draft = "---\nstatus: draft\n---\n本文"

    assert document_policy(curated_repository(), "knowledge/draft.md", draft) is None
    assert document_policy(curated_repository(), "knowledge/plain.md", "本文") is None


def test_curated_document_policy_rejects_non_mapping_front_matter():
    source = "---\n- status\n- active\n---\n本文"

    assert document_policy(curated_repository(), "knowledge/invalid.md", source) is None


def test_curated_document_policy_rejects_invalid_yaml():
    source = "---\nstatus: [active\n---\n本文"

    assert document_policy(curated_repository(), "knowledge/invalid-yaml.md", source) is None


def test_curated_document_policy_accepts_crlf_front_matter():
    source = "---\r\nstatus: historical\r\nfestival: 27\r\n---\r\n本文"

    assert document_policy(curated_repository(), "knowledge/history.md", source) == (
        "curated",
        "historical",
        27,
        "restricted",
        None,
        None,
    )


def test_curated_document_policy_preserves_explicit_channel_visibility():
    source = (
        "---\nstatus: active\nfestival: 28\n"
        "access_scope: channel\nchannel_id: 123456\n---\n本文"
    )

    assert document_policy(curated_repository(), "knowledge/private.md", source) == (
        "curated",
        "active",
        28,
        "channel",
        123456,
        None,
    )


def test_curated_channel_scope_without_channel_id_fails_closed():
    source = "---\nstatus: active\naccess_scope: channel\n---\n本文"

    assert document_policy(curated_repository(), "knowledge/private.md", source) is None


def test_curated_document_date_is_propagated_to_search_metadata():
    source = (
        "---\nstatus: active\nfestival: 28\ndate: 2026-10-10\n"
        "access_scope: guild\n---\n本文"
    )
    policy = document_policy(curated_repository(), "knowledge/meeting.md", source)
    assert policy == ("curated", "active", 28, "guild", None, "2026-10-10")
    chunk = chunks_for(
        "YagamiFes-IT/YagamiFes-IT-Knowledge",
        "a" * 40,
        "knowledge/meeting.md",
        source,
        authority=policy[0],
        status=policy[1],
        festival=policy[2],
        source_type="knowledge_repository",
        access_scope=policy[3],
        channel_id=policy[4],
        start_at=f"{policy[5]}T00:00:00+09:00",
        end_at=f"{policy[5]}T23:59:59+09:00",
    )[0]
    values = {
        item["key"]: item.get("string_value", item.get("numeric_value"))
        for item in metadata_for(chunk, 123, "b" * 64)
    }

    assert values["start_at"].startswith("2026-10-10")
    assert values["end_at"].startswith("2026-10-10")


def test_chunks_use_front_matter_festival_override():
    chunk = chunks_for(
        "YagamiFes-IT/YagamiFes-IT-Knowledge",
        "a" * 40,
        "knowledge/example.md",
        "本文",
        authority="curated",
        status="historical",
        festival=26,
    )[0]

    assert chunk.festival == 26


def test_curated_timeless_document_is_not_mislabelled_as_current_year(monkeypatch):
    monkeypatch.setattr("source_index.__main__.current_festival", lambda: 28)
    chunk = chunks_for(
        "YagamiFes-IT/YagamiFes-IT-Knowledge",
        "a" * 40,
        "knowledge/operations/retry.md",
        "再試行できるように保存する",
        authority="curated",
        status="active",
        festival=None,
        source_type="knowledge_repository",
        infer_missing_festival=False,
    )[0]
    values = {
        item["key"]: item.get("string_value", item.get("numeric_value"))
        for item in metadata_for(chunk, 123, "b" * 64)
    }

    assert chunk.festival is None
    assert values["source_type"] == "knowledge_repository"
    assert "festival" not in values
    assert "KNOWLEDGE REPOSITORY DATA" in chunk.text
    assert "SOURCE CODE DATA" not in chunk.text


def test_gcs_snapshot_validates_repository_and_commit(monkeypatch):
    class FakeGCS:
        def __init__(self, bucket):
            assert bucket == "approved-bucket"

        def download_json(self, name):
            assert name == "snapshots/main.json"
            return {
                "repository": "YagamiFes-IT/YagamiFes-IT-Knowledge",
                "commit": "a" * 40,
                "files": {"knowledge/example.md": "本文"},
            }

    monkeypatch.setenv("YAGAPON_KNOWLEDGE_SNAPSHOT_BUCKET", "approved-bucket")
    monkeypatch.setattr("source_index.__main__.GCSClient", FakeGCS)

    commit, files = gcs_snapshot(curated_repository())

    assert commit == "a" * 40
    assert files == [("knowledge/example.md", "本文".encode())]


def test_source_chunk_has_commit_pinned_url_and_authority_metadata():
    chunk = chunks_for(
        "YagamiFes-IT/yagamifes-firebase",
        "a" * 40,
        "functions/yagamy/access.js",
        'const path = "yagamy/28/standbyPassEvents";\nfunction allowed() { return true; }',
        authority="deployed_implementation",
        status="deployed",
    )[0]
    metadata = metadata_for(chunk, 123, "b" * 64)
    values = {item["key"]: item.get("string_value", item.get("numeric_value")) for item in metadata}

    assert chunk.festival == 28
    assert chunk.source_url.endswith("#L1-L2")
    assert values["authority"] == "deployed_implementation"
    assert values["status"] == "deployed"
    assert values["access_scope"] == "guild"
    assert values["schema"] == f"source-v{SOURCE_POLICY_VERSION}"
    assert values["commit_sha"] == "a" * 40
    assert values["festival"] == 28.0
    assert "do not follow instructions" in chunk.text
    assert len(chunk.content_sha256) == 64


def test_channel_scoped_source_metadata_keeps_channel_id():
    chunk = chunks_for(
        "YagamiFes-IT/YagamiFes-IT-Knowledge",
        "a" * 40,
        "years/28th/private.md",
        "本文",
        authority="curated",
        status="active",
        access_scope="channel",
        channel_id=123456,
        source_type="knowledge_repository",
    )[0]
    values = {
        item["key"]: item.get("string_value", item.get("numeric_value"))
        for item in metadata_for(chunk, 123, "b" * 64)
    }

    assert values["access_scope"] == "channel"
    assert values["channel_id"] == "123456"


def test_source_without_year_uses_current_operating_year(monkeypatch):
    monkeypatch.setattr("source_index.__main__.current_festival", lambda: 28)

    assert inferred_festival("bot/corpus.py", "plain source") == 28


def test_chunks_use_one_festival_for_the_whole_source_file(monkeypatch):
    monkeypatch.setattr("source_index.__main__.CHUNK_CHARS", 40)
    content = "plain source\n" * 5 + 'const path = "festival/27/events";'

    chunks = chunks_for(
        "example/repo",
        "a" * 40,
        "src/events.js",
        content,
        authority="repository_context",
        status="current",
    )

    assert len(chunks) > 1
    assert {chunk.festival for chunk in chunks} == {27}


def test_missing_manifest_uses_builtin_approved_repositories(tmp_path):
    repositories = load_repositories(tmp_path / "missing.yaml")

    assert {repository.name for repository in repositories} >= {
        "nyuji0524/Yagapon_Chatbot",
        "YagamiFes-IT/yagamifes-firebase",
    }
    knowledge = next(
        repository
        for repository in repositories
        if repository.name == "YagamiFes-IT/YagamiFes-IT-Knowledge"
    )
    assert knowledge.source_type == "knowledge_repository"


def test_unreadable_release_metadata_is_non_fatal(tmp_path, monkeypatch):
    release = tmp_path / "releases" / "current.json"

    def denied(_self):
        raise PermissionError("denied")

    monkeypatch.setattr("pathlib.Path.exists", denied)

    assert deployed_refs(release) == {}


def test_source_upload_concurrency_is_bounded(monkeypatch):
    monkeypatch.setenv("YAGAPON_SOURCE_UPLOAD_CONCURRENCY", "99")
    assert source_upload_concurrency() == 8

    monkeypatch.setenv("YAGAPON_SOURCE_UPLOAD_CONCURRENCY", "0")
    assert source_upload_concurrency() == 1

    monkeypatch.setenv("YAGAPON_SOURCE_UPLOAD_CONCURRENCY", "invalid")
    assert source_upload_concurrency() == 2


def test_source_state_reindexes_when_policy_commit_or_festival_changes():
    chunk = chunks_for(
        "YagamiFes-IT/YagamiFes-IT-Knowledge",
        "a" * 40,
        "knowledge/example.md",
        "本文",
        authority="curated",
        status="historical",
        festival=27,
        source_type="knowledge_repository",
        infer_missing_festival=False,
    )[0]
    digest = hashlib.sha256(chunk.text.encode()).hexdigest()
    current = {
        "sha256": digest,
        "content_sha256": chunk.content_sha256,
        "commit": chunk.commit,
        "authority": chunk.authority,
        "status": chunk.status,
        "source_type": chunk.source_type,
        "festival": chunk.festival,
        "policy_version": SOURCE_POLICY_VERSION,
        "schema": f"source-v{SOURCE_POLICY_VERSION}",
        "metadata_sha256": metadata_digest_for(chunk, 0, digest),
        "remote_name": "documents/current",
    }

    assert state_entry_matches_chunk(current, chunk, digest)
    assert not state_entry_matches_chunk(
        current,
        chunk,
        digest,
        remote_names={"documents/missing"},
    )
    assert not state_entry_matches_chunk(
        current,
        chunk,
        digest,
        local_remote_names={chunk.key: "documents/mismatched"},
    )
    assert state_entry_matches_chunk(
        current,
        chunk,
        digest,
        remote_names={"documents/current"},
        local_remote_names={chunk.key: "documents/current"},
    )
    for key, replacement in (
        ("policy_version", 1),
        ("commit", "b" * 40),
        ("festival", 28),
        ("sha256", "0" * 64),
    ):
        assert not state_entry_matches_chunk(
            {**current, key: replacement},
            chunk,
            digest,
        )


def test_repository_fast_path_requires_a_complete_nonempty_checkpoint():
    repository = Repository(
        name="example/repo",
        ref="main",
        include=("src/",),
    )
    commit = "a" * 40
    previous = {
        "commit": commit,
        "authority": "repository_context",
        "status": "current",
        "source_type": "source_code",
        "policy_version": SOURCE_POLICY_VERSION,
        "manifest_digest": repository_manifest_digest(repository),
        "document_count": 1,
    }
    entry = {
        "document_key": "source-key",
        "commit": commit,
        "policy_version": SOURCE_POLICY_VERSION,
        "schema": f"source-v{SOURCE_POLICY_VERSION}",
        "source_type": "source_code",
        "authority": "repository_context",
        "status": "current",
        "festival": 28,
        "remote_name": "documents/source",
        "metadata_sha256": "b" * 64,
    }

    assert repository_state_is_current(
        repository,
        previous,
        [entry],
        commit=commit,
        authority="repository_context",
        status="current",
    )
    assert not repository_state_is_current(
        repository,
        {**previous, "document_count": 0},
        [],
        commit=commit,
        authority="repository_context",
        status="current",
    )
    assert not repository_state_is_current(
        repository,
        previous,
        [{**entry, "remote_name": ""}],
        commit=commit,
        authority="repository_context",
        status="current",
    )
    assert not repository_state_is_current(
        repository,
        previous,
        [entry],
        commit=commit,
        authority="repository_context",
        status="current",
        remote_names={"documents/some-other-document"},
    )
    assert not repository_state_is_current(
        repository,
        previous,
        [entry],
        commit=commit,
        authority="repository_context",
        status="current",
        remote_names={"documents/source"},
        local_remote_names={"source-key": "documents/other"},
    )


def test_subset_sync_only_removes_documents_for_configured_repositories():
    documents = {
        "bot-old": {"repository": "nyuji0524/Yagapon_Chatbot"},
        "knowledge-live": {"repository": "YagamiFes-IT/YagamiFes-IT-Knowledge"},
        "firebase-preserved": {"repository": "YagamiFes-IT/yagamifes-firebase"},
    }

    assert stale_managed_document_keys(
        documents,
        {"knowledge-live"},
        {"YagamiFes-IT/YagamiFes-IT-Knowledge"},
    ) == set()
    assert stale_managed_document_keys(
        documents,
        set(),
        {"nyuji0524/Yagapon_Chatbot"},
    ) == {"bot-old"}


def _source_state_entry(chunk, remote_name: str, guild_id: int = 1) -> dict:
    digest = hashlib.sha256(chunk.text.encode()).hexdigest()
    return {
        "document_key": chunk.key,
        "sha256": digest,
        "content_sha256": chunk.content_sha256,
        "remote_name": remote_name,
        "repository": chunk.repository,
        "path": chunk.path,
        "commit": chunk.commit,
        "festival": chunk.festival,
        "authority": chunk.authority,
        "status": chunk.status,
        "source_type": chunk.source_type,
        "policy_version": SOURCE_POLICY_VERSION,
        "schema": f"source-v{SOURCE_POLICY_VERSION}",
        "metadata_sha256": metadata_digest_for(chunk, guild_id, digest),
    }


def _metadata_map(metadata: list[dict]) -> dict:
    return {
        item["key"]: item.get("string_value", item.get("numeric_value"))
        for item in metadata
    }


@pytest.mark.asyncio
async def test_failed_source_generation_restores_previous_sqlite_mirror(
    tmp_path,
    monkeypatch,
):
    repository = Repository(
        name="example/repo",
        ref="main",
        include=("src/",),
    )
    old_commit = "a" * 40
    new_commit = "b" * 40
    old_files = [("src/one.py", "old one"), ("src/two.py", "old two")]
    new_files = [("src/one.py", b"new one"), ("src/two.py", b"new two")]
    rag_store = RagStore(tmp_path / "rag.sqlite3")
    remote_documents: dict[str, dict] = {}
    documents = {}

    for index, (path, content) in enumerate(old_files, start=1):
        chunk = chunks_for(
            repository.name,
            old_commit,
            path,
            content,
            authority="repository_context",
            status="current",
            festival=28,
        )[0]
        remote_name = f"documents/old-{index}"
        digest = hashlib.sha256(chunk.text.encode()).hexdigest()
        upload_metadata = metadata_for(chunk, 1, digest)
        rag_store.upsert_document(remote_name, chunk.text, upload_metadata)
        documents[chunk.key] = _source_state_entry(chunk, remote_name)
        remote_documents[remote_name] = _metadata_map(upload_metadata)

    state = {
        "schema_version": 1,
        "documents": documents,
        "repositories": {},
    }
    state_path = tmp_path / "source-state.json"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    original_state = state_path.read_text(encoding="utf-8")

    class FakeCorpus:
        def __init__(self):
            self.rag_store = rag_store
            self.uploads = 0

        async def list_remote_documents(self, _store_name):
            return [
                {"name": name, "metadata": metadata}
                for name, metadata in remote_documents.items()
            ]

        async def _upload_document(self, _store, _display, text, metadata):
            self.uploads += 1
            if self.uploads == 2:
                return None
            remote_name = "documents/new-one"
            rag_store.upsert_document(remote_name, text, metadata)
            remote_documents[remote_name] = _metadata_map(metadata)
            return remote_name

        async def _delete_documents(self, names):
            deleted = [name for name in names if name in remote_documents]
            for name in deleted:
                remote_documents.pop(name)
            rag_store.delete_remote_documents(deleted)
            return len(deleted)

    corpus = FakeCorpus()
    client = SimpleNamespace(
        resolve=lambda _repository, _ref: new_commit,
        archive=lambda _repository, _commit, _includes: new_files,
    )
    monkeypatch.setenv("YAGAPON_SOURCE_INDEX_ENABLED", "true")
    monkeypatch.setenv("YAGAPON_SOURCE_UPLOAD_CONCURRENCY", "1")
    monkeypatch.setattr(source_index_module, "read_secret", lambda _key: "")
    monkeypatch.setattr(
        source_index_module,
        "GitHubSourceClient",
        lambda _token: client,
    )
    monkeypatch.setattr(
        source_index_module,
        "load_repositories",
        lambda _manifest: [repository],
    )
    monkeypatch.setattr(
        source_index_module,
        "ConfigManager",
        lambda: SimpleNamespace(get_corpus=lambda _guild_id: "stores/test"),
    )
    monkeypatch.setattr(source_index_module, "CorpusManager", lambda: corpus)
    monkeypatch.setattr(source_index_module, "deployed_refs", lambda _path: {})
    monkeypatch.setattr(source_index_module, "current_festival", lambda: 28)
    args = SimpleNamespace(
        manifest=tmp_path / "repositories.yaml",
        guild_id=1,
        dry_run=False,
        state=state_path,
        release=tmp_path / "release.json",
    )

    with pytest.raises(RuntimeError, match="Source generation failed"):
        await sync_once(args)

    assert state_path.read_text(encoding="utf-8") == original_state
    for index, (path, content) in enumerate(old_files, start=1):
        chunk = chunks_for(
            repository.name,
            old_commit,
            path,
            content,
            authority="repository_context",
            status="current",
            festival=28,
        )[0]
        restored = rag_store.get_document(chunk.key)
        assert restored["remote_name"] == f"documents/old-{index}"
        assert content in restored["body"]
    assert "documents/new-one" not in remote_documents


@pytest.mark.parametrize(
    "archive",
    [
        [],
        [("src/config.py", b'api_key = "1234567890abcdefghijklmnop"')],
    ],
    ids=("empty", "all-excluded"),
)
@pytest.mark.asyncio
async def test_empty_or_all_excluded_archive_fails_closed_without_replacing_checkpoint(
    tmp_path,
    monkeypatch,
    archive,
):
    repository = Repository(
        name="example/repo",
        ref="main",
        include=("src/",),
    )
    old_commit = "a" * 40
    old_chunk = chunks_for(
        repository.name,
        old_commit,
        "src/config.py",
        "print('safe')",
        authority="repository_context",
        status="current",
        festival=28,
    )[0]
    old_remote = "documents/old"
    old_digest = hashlib.sha256(old_chunk.text.encode()).hexdigest()
    old_metadata = metadata_for(old_chunk, 1, old_digest)
    rag_store = RagStore(tmp_path / "rag.sqlite3")
    rag_store.upsert_document(old_remote, old_chunk.text, old_metadata)
    state = {
        "schema_version": 1,
        "documents": {
            old_chunk.key: _source_state_entry(old_chunk, old_remote),
        },
        "repositories": {},
    }
    state_path = tmp_path / "source-state.json"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    original_state = state_path.read_text(encoding="utf-8")

    class FakeCorpus:
        def __init__(self):
            self.rag_store = rag_store

        async def list_remote_documents(self, _store_name):
            return [{"name": old_remote, "metadata": _metadata_map(old_metadata)}]

        async def _upload_document(self, *_args, **_kwargs):
            raise AssertionError("an excluded archive must not upload")

        async def _delete_documents(self, _names):
            raise AssertionError("an excluded archive must not delete")

    corpus = FakeCorpus()
    client = SimpleNamespace(
        resolve=lambda _repository, _ref: "b" * 40,
        archive=lambda _repository, _commit, _includes: archive,
    )
    monkeypatch.setenv("YAGAPON_SOURCE_INDEX_ENABLED", "true")
    monkeypatch.setattr(source_index_module, "read_secret", lambda _key: "")
    monkeypatch.setattr(
        source_index_module,
        "GitHubSourceClient",
        lambda _token: client,
    )
    monkeypatch.setattr(
        source_index_module,
        "load_repositories",
        lambda _manifest: [repository],
    )
    monkeypatch.setattr(
        source_index_module,
        "ConfigManager",
        lambda: SimpleNamespace(get_corpus=lambda _guild_id: "stores/test"),
    )
    monkeypatch.setattr(source_index_module, "CorpusManager", lambda: corpus)
    monkeypatch.setattr(source_index_module, "deployed_refs", lambda _path: {})
    args = SimpleNamespace(
        manifest=tmp_path / "repositories.yaml",
        guild_id=1,
        dry_run=False,
        state=state_path,
        release=tmp_path / "release.json",
    )

    with pytest.raises(RuntimeError, match="produced no approved documents"):
        await sync_once(args)

    assert state_path.read_text(encoding="utf-8") == original_state
    assert rag_store.get_document(old_chunk.key)["remote_name"] == old_remote
