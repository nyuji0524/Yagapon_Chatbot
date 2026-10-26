from pathlib import Path

import yaml

from bot.secret_value import read_secret

ROOT = Path(__file__).resolve().parents[1]


def test_bot_and_worker_receive_the_same_curation_policy():
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    bot_environment = compose["services"]["yagapon"]["environment"]
    worker_environment = compose["services"]["knowledge-curation-watch"][
        "environment"
    ]
    shared_policy = {
        "YAGAPON_KNOWLEDGE_GUILD_ID",
        "YAGAPON_KNOWLEDGE_ALLOWED_GUILD_IDS",
        "YAGAPON_KNOWLEDGE_DENY_CHANNEL_IDS",
        "YAGAPON_KNOWLEDGE_DENY_CHANNEL_TERMS",
        "YAGAPON_KNOWLEDGE_GCS_BUCKET",
        "YAGAPON_KNOWLEDGE_CURATION_ENABLED",
        "YAGAPON_KNOWLEDGE_CURATION_STATE",
        "YAGAPON_KNOWLEDGE_CURATION_CHANNEL_IDS",
        "YAGAPON_KNOWLEDGE_CURATION_BATCH",
        "YAGAPON_KNOWLEDGE_CURATION_MODEL_CALLS_PER_RUN",
        "YAGAPON_KNOWLEDGE_CURATION_MODEL",
        "YAGAPON_KNOWLEDGE_CURATION_USE_BATCH_API",
        "YAGAPON_KNOWLEDGE_CURATION_GENERATIONS_PER_RUN",
        "YAGAPON_KNOWLEDGE_CURATION_MAX_OPEN_BUNDLES",
        "YAGAPON_KNOWLEDGE_CURATION_RETRIES_PER_RUN",
        "YAGAPON_KNOWLEDGE_BACKFILL_STATE",
        "YAGAPON_KNOWLEDGE_PIPELINE_LOCK",
        "YAGAPON_SCHEDULED_BACKFILL_ENABLED",
        "YAGAPON_SCHEDULED_BACKFILL_GUILD_IDS",
        "YAGAPON_SCHEDULED_BACKFILL_CHANNEL_IDS",
        "YAGAPON_SCHEDULED_BACKFILL_STATE",
    }

    assert shared_policy <= set(bot_environment)
    assert shared_policy <= set(worker_environment)
    for key in shared_policy:
        assert bot_environment[key] == worker_environment[key]


def test_shared_env_file_cannot_expose_direct_source_credentials():
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    protected = {
        "YAGAPON_SOURCE_GITHUB_APP_PRIVATE_KEY",
        "YAGAPON_SOURCE_GITHUB_TOKEN",
    }

    services_with_shared_env = {
        name: service
        for name, service in compose["services"].items()
        if service.get("env_file")
    }
    assert services_with_shared_env
    for service in services_with_shared_env.values():
        environment = service.get("environment", {})
        assert protected <= set(environment)
        assert all(environment[name] == "" for name in protected)

    source_environment = services_with_shared_env["source-index-watch"]["environment"]
    assert source_environment["YAGAPON_SOURCE_GITHUB_APP_PRIVATE_KEY_FILE"] == (
        "/run/secrets/github-app-private-key.pem"
    )
    assert source_environment["YAGAPON_SOURCE_GITHUB_TOKEN_FILE"] == ""
    assert source_environment["YAGAPON_SOURCE_REQUIRE_FULL_MANIFEST"] == (
        "${YAGAPON_SOURCE_REQUIRE_FULL_MANIFEST:-true}"
    )


def test_empty_direct_secret_falls_back_to_mounted_file(tmp_path, monkeypatch):
    mounted = tmp_path / "github-app-private-key.pem"
    mounted.write_text("mounted-secret", encoding="utf-8")
    monkeypatch.setenv("YAGAPON_SOURCE_GITHUB_APP_PRIVATE_KEY", "")
    monkeypatch.setenv(
        "YAGAPON_SOURCE_GITHUB_APP_PRIVATE_KEY_FILE",
        str(mounted),
    )

    assert read_secret("YAGAPON_SOURCE_GITHUB_APP_PRIVATE_KEY") == "mounted-secret"


def test_dedicated_knowledge_app_key_is_mounted_only_into_dispatchers():
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    services = compose["services"]
    dispatchers = {"yagapon", "knowledge-curation-watch"}
    protected = {
        "YAGAPON_KNOWLEDGE_GITHUB_APP_ID",
        "YAGAPON_KNOWLEDGE_GITHUB_APP_ID_FILE",
        "YAGAPON_KNOWLEDGE_GITHUB_APP_INSTALLATION_ID",
        "YAGAPON_KNOWLEDGE_GITHUB_APP_INSTALLATION_ID_FILE",
        "YAGAPON_KNOWLEDGE_GITHUB_APP_PRIVATE_KEY",
        "YAGAPON_KNOWLEDGE_GITHUB_APP_PRIVATE_KEY_FILE",
    }
    container_key_path = "/run/secrets/knowledge-github-app-private-key.pem"
    expected_volume = (
        "${YAGAPON_KNOWLEDGE_GITHUB_APP_PRIVATE_KEY_HOST_FILE:-/dev/null}:"
        f"{container_key_path}:ro"
    )

    for name, service in services.items():
        volumes = service.get("volumes") or []
        matching_mounts = [volume for volume in volumes if container_key_path in volume]
        environment = service.get("environment") or {}
        if name in dispatchers:
            assert matching_mounts == [expected_volume]
            assert environment["YAGAPON_KNOWLEDGE_GITHUB_APP_ID"] == (
                "${YAGAPON_KNOWLEDGE_GITHUB_APP_ID:-}"
            )
            assert environment["YAGAPON_KNOWLEDGE_GITHUB_APP_INSTALLATION_ID"] == (
                "${YAGAPON_KNOWLEDGE_GITHUB_APP_INSTALLATION_ID:-}"
            )
            assert environment["YAGAPON_KNOWLEDGE_GITHUB_APP_PRIVATE_KEY"] == ""
            assert environment["YAGAPON_KNOWLEDGE_GITHUB_APP_PRIVATE_KEY_FILE"] == (
                container_key_path
            )
            assert environment["YAGAPON_KNOWLEDGE_GITHUB_APP_ID_FILE"] == ""
            assert environment[
                "YAGAPON_KNOWLEDGE_GITHUB_APP_INSTALLATION_ID_FILE"
            ] == ""
        else:
            assert not matching_mounts
            assert protected <= set(environment)
            assert all(environment[name] == "" for name in protected)


def test_source_and_knowledge_github_apps_are_isolated_by_service():
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    services = compose["services"]
    source_only = services["source-index-watch"]["environment"]

    assert source_only["YAGAPON_SOURCE_GITHUB_APP_PRIVATE_KEY_FILE"] == (
        "/run/secrets/github-app-private-key.pem"
    )
    assert source_only["YAGAPON_KNOWLEDGE_GITHUB_APP_PRIVATE_KEY_FILE"] == ""
    for name in ("yagapon", "knowledge-curation-watch"):
        environment = services[name]["environment"]
        assert environment["YAGAPON_SOURCE_GITHUB_APP_PRIVATE_KEY_FILE"] == ""
        assert environment["YAGAPON_KNOWLEDGE_GITHUB_APP_PRIVATE_KEY_FILE"] == (
            "/run/secrets/knowledge-github-app-private-key.pem"
        )


def test_empty_knowledge_direct_secret_falls_back_to_dedicated_mounted_file(
    tmp_path,
    monkeypatch,
):
    mounted = tmp_path / "knowledge-github-app-private-key.pem"
    mounted.write_text("knowledge-mounted-secret", encoding="utf-8")
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_GITHUB_APP_PRIVATE_KEY", "")
    monkeypatch.setenv(
        "YAGAPON_KNOWLEDGE_GITHUB_APP_PRIVATE_KEY_FILE",
        str(mounted),
    )

    assert read_secret("YAGAPON_KNOWLEDGE_GITHUB_APP_PRIVATE_KEY") == (
        "knowledge-mounted-secret"
    )


def test_bot_requires_file_search_grounding_supports_by_default():
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    environment = compose["services"]["yagapon"]["environment"]

    assert environment["YAGAPON_RAG_REQUIRE_ANSWER_STATUS"] == (
        "${YAGAPON_RAG_REQUIRE_ANSWER_STATUS:-true}"
    )
    assert environment["YAGAPON_RAG_REQUIRE_GROUNDING_SUPPORTS"] == (
        "${YAGAPON_RAG_REQUIRE_GROUNDING_SUPPORTS:-true}"
    )
    assert environment["YAGAPON_RAG_GROUNDING_COVERAGE_THRESHOLD"] == (
        "${YAGAPON_RAG_GROUNDING_COVERAGE_THRESHOLD:-0.8}"
    )
    assert environment["YAGAPON_FILE_SEARCH_INDEX_TIMEOUT_SECONDS"] == (
        "${YAGAPON_FILE_SEARCH_INDEX_TIMEOUT_SECONDS:-1800}"
    )


def test_file_search_uploaders_share_the_index_timeout_default():
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    expected_index = "${YAGAPON_FILE_SEARCH_INDEX_TIMEOUT_SECONDS:-1800}"
    expected_start = "${YAGAPON_FILE_SEARCH_UPLOAD_START_TIMEOUT_SECONDS:-180}"

    assert compose["services"]["yagapon"]["environment"][
        "YAGAPON_FILE_SEARCH_INDEX_TIMEOUT_SECONDS"
    ] == expected_index
    assert compose["services"]["source-index-watch"]["environment"][
        "YAGAPON_FILE_SEARCH_INDEX_TIMEOUT_SECONDS"
    ] == expected_index
    assert compose["services"]["yagapon"]["environment"][
        "YAGAPON_FILE_SEARCH_UPLOAD_START_TIMEOUT_SECONDS"
    ] == expected_start
    assert compose["services"]["source-index-watch"]["environment"][
        "YAGAPON_FILE_SEARCH_UPLOAD_START_TIMEOUT_SECONDS"
    ] == expected_start
