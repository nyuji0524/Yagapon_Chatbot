from pathlib import Path

import yaml

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
