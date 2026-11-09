"""Import an operator-supplied Google Drive recording into the durable archive."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from bot.gdrive import _extract_folder_id
from bot.knowledge_intake import parse_drive_file_url
from bot.recording import PersistentPCMSink, RecordingLimitExceeded, recording_root
from bot.recording_replay import RecordingArtifact, find_recording_artifact
from knowledge_sync.drive import READONLY_SCOPE, DriveReader

DEFAULT_MAX_SOURCE_BYTES = 1024 * 1024 * 1024
DEFAULT_MAX_DURATION_SECONDS = 4 * 60 * 60
PCM_CHUNK_BYTES = 1024 * 1024
ALLOWED_MIME_PREFIXES = ("audio/", "video/")
ALLOWED_GENERIC_MIME_TYPES = {
    "application/octet-stream",
    "application/ogg",
}


@dataclass(frozen=True)
class ImportedRecording:
    artifact: RecordingArtifact
    source_name: str
    source_url: str


def _positive_env_int(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _probe_duration(path: Path) -> float | None:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(path),
        ],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=60,
    )
    if result.returncode != 0:
        return None
    try:
        duration = float(json.loads(result.stdout).get("format", {}).get("duration"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return duration if duration > 0 else None


def _safe_title(value: str) -> str:
    title = re.sub(r"[\x00-\x1f\x7f]+", " ", value).strip()
    title = re.sub(r"\s+", " ", title)
    if not title:
        raise ValueError("会議名を入力してください")
    return title[:100]


def import_drive_recording(
    config,
    *,
    guild_id: int,
    channel_id: int,
    category_id: int | None,
    category_name: str,
    actor_id: int,
    drive_url: str,
    title: str,
) -> ImportedRecording:
    """Download, normalize, and durably finalize one Drive audio file.

    The source must live below the guild's configured ``drive_set`` folder.
    FFmpeg output is streamed straight into ``PersistentPCMSink`` so a long
    recording never creates a second full-size temporary PCM copy.
    """

    title = _safe_title(title)
    file_id = parse_drive_file_url(drive_url)
    folder_url = config.get_drive_folder(guild_id)
    folder_id = _extract_folder_id(folder_url or "")
    if not folder_id:
        raise ValueError("先に /drive_set で録音を置くDriveフォルダを設定してください")
    reader = DriveReader(
        credentials_path=None,
        drive_id="",
        folder_id=folder_id,
        scopes=[READONLY_SCOPE],
    )
    document = reader.get_document(file_id)
    if not reader.is_in_scope(document):
        raise PermissionError("録音ファイルはdrive_set配下に置いてください")
    if not (
        document.mime_type.startswith(ALLOWED_MIME_PREFIXES)
        or document.mime_type in ALLOWED_GENERIC_MIME_TYPES
    ):
        raise ValueError(f"音声・動画ファイルではありません: {document.mime_type}")

    import_root = recording_root().parent / "imports"
    import_root.mkdir(parents=True, exist_ok=True)
    import_root.chmod(0o700)
    max_source_bytes = _positive_env_int(
        "YAGAPON_MAX_IMPORTED_SOURCE_BYTES",
        DEFAULT_MAX_SOURCE_BYTES,
    )
    max_duration = _positive_env_int(
        "YAGAPON_MAX_IMPORTED_DURATION_SECONDS",
        DEFAULT_MAX_DURATION_SECONDS,
    )
    sink: PersistentPCMSink | None = None
    with tempfile.TemporaryDirectory(prefix="drive-recording-", dir=import_root) as temporary:
        source = Path(temporary) / "source-media"
        reader.download_binary(document, source, max_bytes=max_source_bytes)
        duration = _probe_duration(source)
        if duration is not None and duration > max_duration:
            raise ValueError(
                f"録音が長すぎます（上限{max_duration // 3600}時間）"
            )

        sink = PersistentPCMSink(
            guild_id,
            f"外部録音 - {title}",
            "listen",
            channel_id=channel_id,
            category_id=category_id,
            category_name=category_name,
        )
        error_log = Path(temporary) / "ffmpeg-error.log"
        process = None
        try:
            with error_log.open("wb") as errors:
                process = subprocess.Popen(
                    [
                        "ffmpeg",
                        "-nostdin",
                        "-v",
                        "error",
                        "-i",
                        str(source),
                        "-vn",
                        "-ac",
                        "2",
                        "-ar",
                        "48000",
                        "-f",
                        "s16le",
                        "pipe:1",
                    ],
                    stdout=subprocess.PIPE,
                    stderr=errors,
                )
                assert process.stdout is not None
                while True:
                    chunk = process.stdout.read(PCM_CHUNK_BYTES)
                    if not chunk:
                        break
                    sink.write(chunk, 0)
                    if sink.limit_reached:
                        process.terminate()
                        raise RecordingLimitExceeded(
                            "変換後PCMが録音保存上限または空き容量を超えました"
                        )
                return_code = process.wait(timeout=60)
            if return_code != 0:
                detail = error_log.read_text(encoding="utf-8", errors="replace")[-1000:]
                raise ValueError(f"録音ファイルを読み取れません: {detail.strip()}")
            if sink.total_bytes <= 0:
                raise ValueError("録音ファイルに音声トラックがありません")
            sink.cleanup()
            sink.mark(
                "delivered_recording_retained",
                capture_suspicious=False,
                transcription_failed=False,
                raw_recording_retained=True,
                external_import=True,
                imported_by_discord_id=str(actor_id),
                imported_source_file_id=document.file_id,
                imported_source_name=document.name,
                imported_source_mime=document.mime_type,
                imported_source_url=document.web_view_link,
                external_export_expected=True,
                external_export_reason="operator-explicit-import",
            )
        except BaseException:
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=10)
            try:
                sink.cleanup()
                sink.mark(
                    "capture_interrupted",
                    capture_integrity_reason="external-import-failed",
                    external_import=True,
                )
            except Exception:
                pass
            raise

    artifact = find_recording_artifact(sink.session_dir.name)
    return ImportedRecording(
        artifact=artifact,
        source_name=document.name,
        source_url=document.web_view_link,
    )
