import hashlib
import json
from pathlib import Path

import pytest

from bot.knowledge_outbox_migrate import (
    CURRENT_DELIVERY_KEYS,
    migrate_legacy_bundle,
)


def _legacy_bundle(root: Path, *, transcript: str = "# 文字起こし\n\n発言") -> Path:
    bundle = root / "legacy-session"
    repository = (
        bundle
        / "sources"
        / "meetings"
        / "yagapon"
        / "28th"
        / "2026-10-09"
        / "legacy-session"
    )
    repository.mkdir(parents=True)
    transcript_path = repository / "transcript.md"
    transcript_path.write_text(transcript, encoding="utf-8")
    manifest = {
        "schemaVersion": 1,
        "source": "yagapon-discord-voice",
        "status": "draft",
        "festival": 28,
        "sessionId": "legacy-session",
        "guildId": "123",
        "channelName": "全体会議",
        "mode": "listen",
        "startedAt": "2026-10-09T10:00:00+09:00",
        "transcript": {
            "path": "transcript.md",
            "sha256": hashlib.sha256(transcript.encode("utf-8")).hexdigest(),
            "speakerIdentity": "anonymous-labels",
            "humanReviewed": False,
        },
        "summary": None,
        "minutes": None,
        "rawAudio": {
            "published": False,
            "retention": "deleted-after-durable-transcript",
        },
    }
    (repository / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False),
        encoding="utf-8",
    )
    (bundle / "delivery.json").write_text(
        json.dumps({
            "status": "pending",
            "sessionId": "legacy-session",
            "manifest": manifest,
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    return bundle


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def test_legacy_outbox_migration_defaults_to_read_only_dry_run(tmp_path):
    root = tmp_path / "outbox"
    source = _legacy_bundle(root)
    before = _snapshot(source)

    plan = migrate_legacy_bundle(
        outbox_root=root,
        source_bundle_id="legacy-session",
    )

    assert plan.eligible is True
    assert plan.applied is False
    assert plan.file_count == 2
    assert plan.transcript_hash_verified is True
    assert not (root / plan.target_bundle_id).exists()
    assert _snapshot(source) == before


def test_legacy_outbox_apply_creates_retryable_copy_without_editing_source(tmp_path):
    root = tmp_path / "outbox"
    source = _legacy_bundle(root)
    before = _snapshot(source)

    plan = migrate_legacy_bundle(
        outbox_root=root,
        source_bundle_id="legacy-session",
        target_bundle_id="legacy-session-migrated",
        apply=True,
    )

    destination = root / "legacy-session-migrated"
    delivery = json.loads(
        (destination / "delivery.json").read_text(encoding="utf-8")
    )
    assert plan.applied is True
    assert all(key in delivery for key in CURRENT_DELIVERY_KEYS)
    assert delivery["status"] == "pending"
    assert delivery["migratedFromLegacy"] is True
    assert delivery["bundleId"] == plan.target_bundle_id
    assert delivery["branch"] == f"yagapon/meeting-{plan.target_bundle_id.lower()}"
    assert (destination / delivery["repositoryRoot"] / "transcript.md").read_bytes() == (
        source / delivery["repositoryRoot"] / "transcript.md"
    ).read_bytes()
    assert (destination / delivery["repositoryRoot"] / "manifest.json").read_bytes() == (
        source / delivery["repositoryRoot"] / "manifest.json"
    ).read_bytes()
    assert _snapshot(source) == before


def test_legacy_outbox_migration_rejects_transcript_hash_mismatch(tmp_path):
    root = tmp_path / "outbox"
    bundle = _legacy_bundle(root)
    transcript = next(bundle.rglob("transcript.md"))
    transcript.write_text("tampered", encoding="utf-8")

    with pytest.raises(ValueError, match="hash"):
        migrate_legacy_bundle(
            outbox_root=root,
            source_bundle_id="legacy-session",
        )


def test_legacy_outbox_migration_rejects_root_not_accepted_by_knowledge(tmp_path):
    root = tmp_path / "outbox"
    bundle = _legacy_bundle(root)
    accepted = next(bundle.glob("sources/meetings/yagapon/*/*/*"))
    rejected = bundle / "years" / "28th" / "meetings" / "legacy-session"
    rejected.parent.mkdir(parents=True)
    accepted.rename(rejected)

    with pytest.raises(ValueError, match="outside Knowledge intake areas"):
        migrate_legacy_bundle(
            outbox_root=root,
            source_bundle_id="legacy-session",
        )


def test_legacy_outbox_migration_never_applies_sensitive_bundle(tmp_path):
    root = tmp_path / "outbox"
    _legacy_bundle(root, transcript="api_key=abcdefghijk")

    plan = migrate_legacy_bundle(
        outbox_root=root,
        source_bundle_id="legacy-session",
    )

    assert plan.sensitive_pattern_count == 1
    assert plan.eligible is False
    with pytest.raises(ValueError, match="safety"):
        migrate_legacy_bundle(
            outbox_root=root,
            source_bundle_id="legacy-session",
            apply=True,
        )
