from source_index.__main__ import (
    chunks_for,
    contains_secret,
    inferred_festival,
    load_repositories,
    metadata_for,
    safe_source_path,
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
