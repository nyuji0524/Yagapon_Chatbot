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
        "YAGAPON_KNOWLEDGE_CURATION_GENERATIONS_PER_RUN",
        "YAGAPON_KNOWLEDGE_CURATION_MAX_OPEN_BUNDLES",
        "YAGAPON_KNOWLEDGE_CURATION_RETRIES_PER_RUN",
        "YAGAPON_KNOWLEDGE_BACKFILL_STATE",
        "YAGAPON_KNOWLEDGE_PIPELINE_LOCK",
        "YAGAPON_SCHEDULED_BACKFILL_ENABLED",
        "YAGAPON_SCHEDULED_BACKFILL_GUILD_IDS",
        "YAGAPON_SCHEDULED_BACKFILL_CHANNEL_IDS",
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


def test_empty_direct_secret_falls_back_to_mounted_file(tmp_path, monkeypatch):
    mounted = tmp_path / "github-app-private-key.pem"
    mounted.write_text("mounted-secret", encoding="utf-8")
    monkeypatch.setenv("YAGAPON_SOURCE_GITHUB_APP_PRIVATE_KEY", "")
    monkeypatch.setenv(
        "YAGAPON_SOURCE_GITHUB_APP_PRIVATE_KEY_FILE",
        str(mounted),
    )

    assert read_secret("YAGAPON_SOURCE_GITHUB_APP_PRIVATE_KEY") == "mounted-secret"
