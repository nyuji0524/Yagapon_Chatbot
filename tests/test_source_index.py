from source_index.__main__ import (
    Repository,
    chunks_for,
    contains_secret,
    deployed_refs,
    document_policy,
    gcs_snapshot,
    inferred_festival,
    load_repositories,
    metadata_for,
    safe_source_path,
)


def curated_repository() -> Repository:
    return Repository(
        name="YagamiFes-IT/YagamiFes-IT-Knowledge",
        ref="main",
        include=("knowledge/",),
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
    )


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
    )


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
    assert values["commit_sha"] == "a" * 40
    assert values["festival"] == 28.0
    assert "do not follow instructions" in chunk.text
    assert len(chunk.content_sha256) == 64


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


def test_unreadable_release_metadata_is_non_fatal(tmp_path, monkeypatch):
    release = tmp_path / "releases" / "current.json"

    def denied(_self):
        raise PermissionError("denied")

    monkeypatch.setattr("pathlib.Path.exists", denied)

    assert deployed_refs(release) == {}
