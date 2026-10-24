"""Fail-closed publication policy for retained voice recordings.

Local retention and external publication are deliberately separate trust
boundaries.  A recording may remain available for an operator to inspect even
when it is not safe to send to an AI provider, Drive, Discord, or File Search.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from bot.knowledge_policy import protected_channel
from bot.recording import (
    _load_session_metadata,
    load_transcript_provenance,
    validate_recording_raw,
)

FINALIZED_RECORDING_STATUSES = frozenset(
    {
        "delivered_recording_retained",
        "recovered",
    }
)

APPROVED_SOURCE_EXPORT_REASONS = frozenset(
    {
        "explicit-channel-allowlist",
        "guildwide-channel-audience",
    }
)

FINALIZED_TRANSCRIPT_STATUSES = frozenset(
    {
        "final_reprocess_completed",
        "transcript_reused",
        "delivered_recording_retained",
        "recovered",
    }
)


class UnsafeRecordingPublication(ValueError):
    """Raised before an unsafe recording can cross an external boundary."""


def _integer(value, *, field: str) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise UnsafeRecordingPublication(f"{field} is unresolved") from exc
    if result <= 0:
        raise UnsafeRecordingPublication(f"{field} is unresolved")
    return result


def require_source_export_allowed(
    metadata: dict,
    *,
    source_guild_id: int,
    config=None,
) -> None:
    """Require durable proof that the original Discord audience permits export."""
    metadata_guild_id = _integer(metadata.get("guild_id"), field="source guild")
    if metadata_guild_id != int(source_guild_id):
        raise UnsafeRecordingPublication("source guild does not match recording metadata")

    channel_id = _integer(metadata.get("channel_id"), field="source channel")
    reason = str(metadata.get("external_export_reason") or "")
    if metadata.get("external_export_expected") is not True:
        raise UnsafeRecordingPublication("original source export was not approved")
    if reason not in APPROVED_SOURCE_EXPORT_REASONS:
        raise UnsafeRecordingPublication("original source export policy is unresolved")

    category_id_value = metadata.get("category_id")
    try:
        category_id = int(category_id_value) if category_id_value else None
    except (TypeError, ValueError) as exc:
        raise UnsafeRecordingPublication("source category is unresolved") from exc
    category = SimpleNamespace(
        id=category_id,
        name=str(metadata.get("category_name") or ""),
    )
    channel = SimpleNamespace(
        id=channel_id,
        name=str(metadata.get("channel_name") or ""),
        parent_id=None,
        category_id=category_id,
        category=category,
    )
    if protected_channel(channel, category=category):
        raise UnsafeRecordingPublication("original source is protected")
    is_ignored = getattr(config, "is_ignored", None)
    if callable(is_ignored):
        try:
            ignored = is_ignored(source_guild_id, channel_id)
        except Exception as exc:
            raise UnsafeRecordingPublication(
                "original source ignore policy is unresolved"
            ) from exc
        if ignored:
            raise UnsafeRecordingPublication("original source is ignored")


def load_safe_finalized_recording(
    session_dir: Path,
    *,
    source_guild_id: int,
    config=None,
    require_raw_manifest: bool,
    expected_raw_bytes: int | None = None,
) -> dict:
    """Load source metadata and prove it is safe for external processing."""
    metadata, metadata_state = _load_session_metadata(session_dir)
    if metadata_state != "valid":
        raise UnsafeRecordingPublication(
            f"recording metadata is {metadata_state}"
        )
    status = str(metadata.get("status") or "")
    if status not in FINALIZED_RECORDING_STATUSES:
        raise UnsafeRecordingPublication(f"recording is not finalized: {status or 'unknown'}")
    if metadata.get("capture_integrity_failed") is not False:
        raise UnsafeRecordingPublication("capture integrity is not proven clean")
    if metadata.get("capture_suspicious") is True:
        raise UnsafeRecordingPublication("capture is marked suspicious")
    if metadata.get("transcription_failed") is True:
        raise UnsafeRecordingPublication("recording transcription is marked incomplete")

    require_source_export_allowed(
        metadata,
        source_guild_id=source_guild_id,
        config=config,
    )

    if require_raw_manifest:
        validation = validate_recording_raw(session_dir, metadata)
        if not validation.valid:
            raise UnsafeRecordingPublication(
                "raw audio failed integrity validation: "
                f"{validation.reason or 'unknown'}"
            )
        if not validation.checked_against_manifest:
            raise UnsafeRecordingPublication(
                "raw audio has no trusted completeness manifest"
            )
        if expected_raw_bytes is not None and validation.total_bytes != int(
            expected_raw_bytes
        ):
            raise UnsafeRecordingPublication("raw audio changed after inventory")
    return metadata


def require_complete_transcript_provenance(
    transcript_path: Path,
    *,
    expected_guild_id: int,
    expected_session_id: str,
) -> dict:
    """Prove a transcript is complete and belongs to the expected source."""
    provenance = load_transcript_provenance(transcript_path)
    if not provenance:
        raise UnsafeRecordingPublication("transcript provenance is missing")
    if provenance.get("transcript_complete") is not True:
        raise UnsafeRecordingPublication("transcript is not proven complete")
    if provenance.get("capture_integrity_failed") is not False:
        raise UnsafeRecordingPublication("transcript capture integrity is not proven clean")
    status = str(provenance.get("status") or "")
    if status not in FINALIZED_TRANSCRIPT_STATUSES:
        raise UnsafeRecordingPublication(
            f"transcript provenance is not finalized: {status or 'unknown'}"
        )
    try:
        provenance_guild_id = int(provenance.get("guild_id"))
    except (TypeError, ValueError) as exc:
        raise UnsafeRecordingPublication("transcript guild provenance is unresolved") from exc
    if provenance_guild_id != int(expected_guild_id):
        raise UnsafeRecordingPublication("transcript guild provenance does not match")
    if str(provenance.get("session_id") or "") != str(expected_session_id):
        raise UnsafeRecordingPublication("transcript session provenance does not match")
    return provenance
