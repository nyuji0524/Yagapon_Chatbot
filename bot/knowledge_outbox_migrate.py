"""Dry-run-first migration for legacy Knowledge outbox bundles.

The migration never edits or removes the source bundle and never sends data to
GCS or GitHub.  ``--apply`` creates a new, retry-compatible local bundle by an
atomic directory rename; an operator can inspect it before explicitly using the
existing Knowledge retry command.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath

from bot.knowledge_publish import _delivery_payload, _sensitive_labels

BUNDLE_ID_PATTERN = re.compile(r"[0-9A-Za-z._-]{1,160}")
CURRENT_DELIVERY_KEYS = ("repositoryRoot", "branch", "title", "body", "manifest")


@dataclass(frozen=True)
class LegacyMigrationPlan:
    source_bundle_id: str
    target_bundle_id: str
    repository_root: str
    file_count: int
    total_bytes: int
    transcript_hash_verified: bool
    sensitive_pattern_count: int
    eligible: bool
    applied: bool = False

    def public_dict(self) -> dict:
        return asdict(self)


def _validate_bundle_id(value: str) -> str:
    if not BUNDLE_ID_PATTERN.fullmatch(value):
        raise ValueError("Invalid Knowledge bundle ID")
    return value


def _default_target_id(source_bundle_id: str) -> str:
    digest = hashlib.sha256(source_bundle_id.encode("utf-8")).hexdigest()[:8]
    suffix = f"-migrated-{digest}"
    return f"{source_bundle_id[: 160 - len(suffix)]}{suffix}"


def _safe_repository_root(source_dir: Path, manifest_path: Path) -> str:
    try:
        relative = manifest_path.parent.resolve().relative_to(source_dir.resolve())
    except ValueError as exc:
        raise ValueError("Legacy manifest is outside its bundle") from exc
    root = PurePosixPath(relative.as_posix())
    if root.is_absolute() or not root.parts or ".." in root.parts:
        raise ValueError("Legacy repository root is invalid")
    return root.as_posix()


def _read_repository_files(repository_dir: Path, source_dir: Path) -> dict[str, str]:
    files: dict[str, str] = {}
    for path in repository_dir.rglob("*"):
        if not path.is_file() or path.name.startswith(".") or path.name.endswith((".tmp", ".bak", "~")):
            continue
        try:
            relative = path.resolve().relative_to(source_dir.resolve()).as_posix()
        except ValueError as exc:
            raise ValueError("Legacy bundle contains an out-of-tree file") from exc
        files[relative] = path.read_text(encoding="utf-8")
    if not files:
        raise ValueError("Legacy bundle has no repository files")
    return files


def _artifact_path(repository_dir: Path, artifact: object) -> Path | None:
    if not isinstance(artifact, dict) or not artifact.get("path"):
        return None
    candidate = (repository_dir / str(artifact["path"])).resolve()
    try:
        candidate.relative_to(repository_dir.resolve())
    except ValueError as exc:
        raise ValueError("Legacy artifact path escapes repository root") from exc
    return candidate


def _verify_manifest_artifacts(repository_dir: Path, manifest: dict) -> bool:
    transcript = manifest.get("transcript")
    transcript_path = _artifact_path(repository_dir, transcript)
    if transcript_path is None or not transcript_path.is_file() or transcript_path.stat().st_size <= 0:
        raise ValueError("Legacy transcript is missing")
    expected_hash = str((transcript or {}).get("sha256") or "")
    actual_hash = hashlib.sha256(transcript_path.read_bytes()).hexdigest()
    if not expected_hash or actual_hash != expected_hash:
        raise ValueError("Legacy transcript hash does not match its manifest")

    optional_artifacts = [manifest.get("summary"), manifest.get("minutes")]
    optional_artifacts.extend(manifest.get("materials") or [])
    for artifact in optional_artifacts:
        path = _artifact_path(repository_dir, artifact)
        if path is not None and (not path.is_file() or path.stat().st_size <= 0):
            raise ValueError("A declared legacy artifact is missing")
    return True


def _delivery_metadata(manifest: dict, repository_root: str, target_bundle_id: str) -> dict:
    session_id = str(manifest.get("sessionId") or target_bundle_id)
    branch_suffix = session_id if BUNDLE_ID_PATTERN.fullmatch(session_id) else target_bundle_id
    channel_name = str(manifest.get("channelName") or "meeting")
    started_at = str(manifest.get("startedAt") or "")
    title = f"source: {started_at[:10] or 'unknown-date'} {channel_name}の文字起こし"
    body = (
        "おしゃべりやがぽんが自動作成した会議資料です。\n\n"
        f"- 保存先: `{repository_root}`\n"
        "- 生文字起こし: 必須\n"
        f"- AI要約: {'あり' if manifest.get('summary') else 'なし'}\n"
        f"- AI議事録: {'あり' if manifest.get('minutes') else 'なし'}\n\n"
        f"- 会議資料: {len(manifest.get('materials') or [])}件\n\n"
        "話者、固有名詞、決定事項、担当、期限、公開範囲を確認してからReadyにしてください。"
    )
    return _delivery_payload(
        status="pending",
        bundle_id=target_bundle_id,
        manifest=manifest,
        repository_root=repository_root,
        branch=f"yagapon/meeting-{branch_suffix.lower()}",
        title=title,
        body=body,
        migratedFromLegacy=True,
    )


def _inspect(
    outbox_root: Path,
    source_bundle_id: str,
    target_bundle_id: str | None,
) -> tuple[LegacyMigrationPlan, dict[str, str], dict]:
    source_bundle_id = _validate_bundle_id(source_bundle_id)
    target_bundle_id = _validate_bundle_id(
        target_bundle_id or _default_target_id(source_bundle_id)
    )
    if source_bundle_id == target_bundle_id:
        raise ValueError("Migration target must differ from source")
    source_dir = outbox_root / source_bundle_id
    delivery_path = source_dir / "delivery.json"
    if not delivery_path.is_file():
        raise FileNotFoundError(source_bundle_id)
    delivery = json.loads(delivery_path.read_text(encoding="utf-8"))
    if all(key in delivery for key in CURRENT_DELIVERY_KEYS):
        raise ValueError("Bundle already uses the current delivery schema")
    if str(delivery.get("status") or "") not in {"pending", "failed"}:
        raise ValueError("Only pending or failed legacy bundles can be migrated")

    manifest_paths = list(source_dir.rglob("manifest.json"))
    if len(manifest_paths) != 1:
        raise ValueError("Legacy bundle must contain exactly one manifest")
    manifest_path = manifest_paths[0]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    embedded_manifest = delivery.get("manifest")
    if embedded_manifest and embedded_manifest != manifest:
        raise ValueError("Legacy delivery and repository manifests differ")
    repository_root = _safe_repository_root(source_dir, manifest_path)
    repository_dir = manifest_path.parent
    transcript_hash_verified = _verify_manifest_artifacts(repository_dir, manifest)
    files = _read_repository_files(repository_dir, source_dir)
    sensitive_labels = _sensitive_labels(files)
    target_exists = (outbox_root / target_bundle_id).exists()
    plan = LegacyMigrationPlan(
        source_bundle_id=source_bundle_id,
        target_bundle_id=target_bundle_id,
        repository_root=repository_root,
        file_count=len(files),
        total_bytes=sum(len(content.encode("utf-8")) for content in files.values()),
        transcript_hash_verified=transcript_hash_verified,
        sensitive_pattern_count=len(sensitive_labels),
        eligible=not sensitive_labels and not target_exists,
    )
    return plan, files, manifest


def migrate_legacy_bundle(
    *,
    outbox_root: Path,
    source_bundle_id: str,
    target_bundle_id: str | None = None,
    apply: bool = False,
) -> LegacyMigrationPlan:
    """Inspect a legacy bundle and optionally create a retry-compatible copy."""
    plan, files, manifest = _inspect(
        outbox_root,
        source_bundle_id,
        target_bundle_id,
    )
    if not apply:
        return plan
    if not plan.eligible:
        raise ValueError("Legacy bundle did not pass migration safety checks")

    root_already_existed = outbox_root.exists()
    outbox_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not root_already_existed:
        outbox_root.chmod(0o700)
    destination = outbox_root / plan.target_bundle_id
    if destination.exists():
        raise FileExistsError(plan.target_bundle_id)
    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{plan.target_bundle_id}.",
            suffix=".tmp",
            dir=outbox_root,
        )
    )
    try:
        staging.chmod(0o700)
        source_dir = outbox_root / plan.source_bundle_id
        for relative in files:
            path = staging / relative
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copyfile(source_dir / relative, path)
            path.chmod(0o600)
        for directory in (path for path in staging.rglob("*") if path.is_dir()):
            directory.chmod(0o700)
        delivery = _delivery_metadata(
            manifest,
            plan.repository_root,
            plan.target_bundle_id,
        )
        delivery_path = staging / "delivery.json"
        delivery_path.write_text(
            json.dumps(delivery, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        delivery_path.chmod(0o600)
        os.replace(staging, destination)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return LegacyMigrationPlan(**{**plan.public_dict(), "applied": True})


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Inspect or copy-migrate one legacy Knowledge outbox bundle",
    )
    parser.add_argument("--bundle-id", required=True)
    parser.add_argument("--target-id")
    parser.add_argument(
        "--outbox-root",
        type=Path,
        default=Path(os.environ.get("YAGAPON_KNOWLEDGE_OUTBOX", "/data/knowledge-outbox")),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="create a new local bundle; never modifies or sends the source",
    )
    args = parser.parse_args()
    result = migrate_legacy_bundle(
        outbox_root=args.outbox_root,
        source_bundle_id=args.bundle_id,
        target_bundle_id=args.target_id,
        apply=args.apply,
    )
    print(json.dumps(result.public_dict(), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
