"""Google Drive integration using Drive and Docs APIs directly."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from googleapiclient.discovery import build

from bot.google_credentials import load_google_credentials
from bot.meeting_time import document_name
from knowledge_sync.drive import READONLY_SCOPE, DriveReader

log = logging.getLogger("yagapon.gdrive")

DRIVE_FILE_SCOPE = "https://www.googleapis.com/auth/drive.file"
DOCS_SCOPE = "https://www.googleapis.com/auth/documents"
GOOGLE_DOC_MIME_TYPE = "application/vnd.google-apps.document"
GOOGLE_FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"
TRANSCRIPT_FOLDER_NAME = "文字起こし"
MINUTES_ACCENT_COLOR = {"red": 19 / 255, "green": 79 / 255, "blue": 92 / 255}
MINUTES_SUBHEADING_COLOR = {"red": 67 / 255, "green": 67 / 255, "blue": 67 / 255}
DEFAULT_MINUTES_IMAGE_URL = (
    # This repository is public. If its visibility changes, operators must set
    # YAGAPON_MINUTES_IMAGE_URL to another publicly fetchable image URL.
    "https://raw.githubusercontent.com/nyuji0524/Yagapon_Chatbot/main/assets/yagapon_minutes.png"
)
MAX_MEETING_MATERIAL_CHARS = 120_000
DRIVE_ARTIFACT_KEY_PROPERTY = "yagaponArtifactKey"
DRIVE_SESSION_ID_PROPERTY = "yagaponSessionId"
DRIVE_ARTIFACT_TYPE_PROPERTY = "yagaponArtifactType"


@dataclass(frozen=True)
class MeetingMaterial:
    file_id: str
    name: str
    url: str
    modified_time: str
    text: str


def _artifact_key(session_id: str, artifact_type: str) -> str:
    """Return a Drive-safe stable key for one session artifact."""
    canonical = f"{session_id.strip()}\0{artifact_type.strip()}"
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _escape_drive_query_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _extract_folder_id(url: str) -> str | None:
    """Extract a Drive folder ID from a configured URL."""
    match = re.search(r"/folders/([a-zA-Z0-9_-]+)", url)
    return match.group(1) if match else None


def read_meeting_material(folder_url: str, file_id: str) -> MeetingMaterial:
    """Read one meeting document only when it belongs to the configured Drive tree."""
    folder_id = _extract_folder_id(folder_url)
    if not folder_id:
        raise ValueError("drive_setのフォルダURLが正しくありません")
    reader = DriveReader(
        # Shared loader resolves user OAuth first, then service account/ADC.
        credentials_path=None,
        drive_id="",
        folder_id=folder_id,
        scopes=[READONLY_SCOPE],
    )
    document = reader.get_document(file_id)
    if not reader.is_in_scope(document):
        raise PermissionError("指定資料はdrive_set配下にありません")
    text = reader.read_text(document)
    if text is None:
        raise ValueError("このファイル形式は会議資料として読み取れません")
    text = text.strip()
    if not text:
        raise ValueError("指定資料の本文が空です")
    if len(text) > MAX_MEETING_MATERIAL_CHARS:
        raise ValueError("指定資料が大きすぎます（12万文字まで）")
    return MeetingMaterial(
        file_id=document.file_id,
        name=document.name,
        url=document.web_view_link,
        modified_time=document.modified_time,
        text=text,
    )


async def load_meeting_material(config, guild_id: int, file_id: str) -> MeetingMaterial:
    folder_url = config.get_drive_folder(guild_id)
    if not folder_url:
        raise ValueError("先に /drive_set で会議資料フォルダを設定してください")
    return await asyncio.to_thread(read_meeting_material, folder_url, file_id)


def _utf16_length(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def _strip_inline_markdown(value: str, start_index: int) -> tuple[str, list[tuple[int, int]]]:
    """Remove bold markers and return Docs API ranges for their contents."""
    output: list[str] = []
    ranges: list[tuple[int, int]] = []
    cursor = 0
    output_units = 0
    for match in re.finditer(r"\*\*(.+?)\*\*", value):
        prefix = value[cursor : match.start()]
        output.append(prefix)
        output_units += _utf16_length(prefix)
        bold = match.group(1)
        bold_start = start_index + output_units
        output.append(bold)
        output_units += _utf16_length(bold)
        ranges.append((bold_start, start_index + output_units))
        cursor = match.end()
    output.append(value[cursor:])
    return "".join(output), ranges


def _render_markdown(
    content: str,
    *,
    minutes_document: bool = False,
) -> tuple[str, list[dict], int | None]:
    """Convert the limited minutes Markdown into a styled Google Doc."""
    rendered: list[str] = []
    headings: list[tuple[int, int, str, int]] = []
    bullets: list[tuple[int, int]] = []
    bold_ranges: list[tuple[int, int]] = []
    author_range: tuple[int, int] | None = None
    image_index: int | None = None
    index = 1
    title_seen = False

    for raw_line in content.strip().splitlines():
        line = raw_line.rstrip()
        heading = re.match(r"^(#{1,4})\s+(.+)$", line)
        bullet = re.match(r"^\s*[-*]\s+(.+)$", line)
        named_style = "NORMAL_TEXT"
        if heading:
            level = len(heading.group(1))
            line = heading.group(2)
            if minutes_document:
                named_style = "TITLE" if level == 1 else ("HEADING_1" if level == 2 else "HEADING_3")
            else:
                named_style = "TITLE" if level == 1 else f"HEADING_{min(level - 1, 3)}"
        elif bullet:
            line = bullet.group(1)

        plain_line, inline_bold = _strip_inline_markdown(line, index)
        end = index + _utf16_length(plain_line)
        rendered.append(plain_line + "\n")
        bold_ranges.extend(inline_bold)
        if heading and end > index:
            headings.append((index, end, named_style, level))
            if minutes_document and level == 1 and not title_seen:
                image_index = end
                title_seen = True
        if bullet:
            bullets.append((index, end + 1))
        index = end + 1

        if minutes_document and heading and level == 1 and author_range is None:
            author = "作成：おしゃべりやがぽん"
            author_start = index
            rendered.append(author + "\n")
            index += _utf16_length(author) + 1
            author_range = (author_start, index - 1)

    text = "".join(rendered) or "\n"
    end_index = 1 + _utf16_length(text)
    requests: list[dict] = [
        {
            "updateTextStyle": {
                "range": {"startIndex": 1, "endIndex": end_index},
                "textStyle": {"weightedFontFamily": {"fontFamily": "Noto Sans JP"}},
                "fields": "weightedFontFamily",
            }
        }
    ]
    if minutes_document:
        requests[0]["updateTextStyle"]["textStyle"]["fontSize"] = {
            "magnitude": 11,
            "unit": "PT",
        }
        requests[0]["updateTextStyle"]["fields"] = "weightedFontFamily,fontSize"

    for start, end, style, level in headings:
        paragraph_style: dict = {"namedStyleType": style}
        paragraph_fields = "namedStyleType"
        if minutes_document and level == 1:
            paragraph_style["alignment"] = "CENTER"
            paragraph_fields += ",alignment"
        requests.append(
            {
                "updateParagraphStyle": {
                    "range": {"startIndex": start, "endIndex": end},
                    "paragraphStyle": paragraph_style,
                    "fields": paragraph_fields,
                }
            }
        )
        if minutes_document:
            if level == 1:
                text_style = {
                    "bold": True,
                    "fontSize": {"magnitude": 24, "unit": "PT"},
                    "foregroundColor": {"color": {"rgbColor": MINUTES_ACCENT_COLOR}},
                }
            elif level == 2:
                text_style = {
                    "bold": True,
                    "fontSize": {"magnitude": 20, "unit": "PT"},
                    "foregroundColor": {"color": {"rgbColor": MINUTES_ACCENT_COLOR}},
                }
            else:
                text_style = {
                    "bold": True,
                    "fontSize": {"magnitude": 14, "unit": "PT"},
                    "foregroundColor": {"color": {"rgbColor": MINUTES_SUBHEADING_COLOR}},
                }
            requests.append(
                {
                    "updateTextStyle": {
                        "range": {"startIndex": start, "endIndex": end},
                        "textStyle": text_style,
                        "fields": "bold,fontSize,foregroundColor",
                    }
                }
            )

    if author_range:
        requests.append(
            {
                "updateParagraphStyle": {
                    "range": {"startIndex": author_range[0], "endIndex": author_range[1]},
                    "paragraphStyle": {"alignment": "END"},
                    "fields": "alignment",
                }
            }
        )
    for start, end in bullets:
        requests.append(
            {
                "createParagraphBullets": {
                    "range": {"startIndex": start, "endIndex": end},
                    "bulletPreset": "BULLET_DISC_CIRCLE_SQUARE",
                }
            }
        )
    for start, end in bold_ranges:
        requests.append(
            {
                "updateTextStyle": {
                    "range": {"startIndex": start, "endIndex": end},
                    "textStyle": {"bold": True},
                    "fields": "bold",
                }
            }
        )
    return text, requests, image_index


class DriveWriter:
    def __init__(self):
        # ``drive.file`` alone cannot discover a pre-existing configured parent.
        credentials, self.credential_source = load_google_credentials(
            [READONLY_SCOPE, DRIVE_FILE_SCOPE, DOCS_SCOPE]
        )
        self.drive = build("drive", "v3", credentials=credentials, cache_discovery=False)
        self.docs = build("docs", "v1", credentials=credentials, cache_discovery=False)

    def check_folder(self, folder_id: str) -> dict:
        folder = (
            self.drive.files()
            .get(
                fileId=folder_id,
                fields="id,name,mimeType,trashed,capabilities(canAddChildren)",
                supportsAllDrives=True,
            )
            .execute(num_retries=3)
        )
        return {
            "credential_source": self.credential_source,
            "folder": folder,
            "can_add_children": bool(folder.get("capabilities", {}).get("canAddChildren")),
        }

    def get_or_create_folder(self, parent_id: str, name: str) -> str:
        escaped_name = name.replace("'", "\\'")
        query = (
            f"'{parent_id}' in parents and trashed = false and "
            f"mimeType = '{GOOGLE_FOLDER_MIME_TYPE}' and name = '{escaped_name}'"
        )
        response = (
            self.drive.files()
            .list(
                q=query,
                spaces="drive",
                corpora="allDrives",
                fields="files(id,name)",
                pageSize=10,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute(num_retries=3)
        )
        matches = response.get("files", [])
        if matches:
            return matches[0]["id"]
        created = (
            self.drive.files()
            .create(
                body={"name": name, "mimeType": GOOGLE_FOLDER_MIME_TYPE, "parents": [parent_id]},
                fields="id",
                supportsAllDrives=True,
            )
            .execute(num_retries=3)
        )
        return created["id"]

    def _find_artifact_document(
        self,
        folder_id: str,
        idempotency_key: str,
    ) -> dict | None:
        escaped_folder = _escape_drive_query_value(folder_id)
        escaped_key = _escape_drive_query_value(idempotency_key)
        query = (
            f"'{escaped_folder}' in parents and trashed = false and "
            f"mimeType = '{GOOGLE_DOC_MIME_TYPE}' and "
            "appProperties has { "
            f"key='{DRIVE_ARTIFACT_KEY_PROPERTY}' and value='{escaped_key}'"
            " }"
        )
        response = (
            self.drive.files()
            .list(
                q=query,
                spaces="drive",
                corpora="allDrives",
                fields="files(id,name,webViewLink,modifiedTime,appProperties)",
                orderBy="modifiedTime desc",
                pageSize=10,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute(num_retries=3)
        )
        matches = response.get("files", [])
        if len(matches) > 1:
            log.warning(
                "Multiple Drive artifacts share idempotency key=%s folder=%s; reusing newest",
                idempotency_key,
                folder_id,
            )
        return matches[0] if matches else None

    def _document_end_index(self, document_id: str) -> int:
        document = (
            self.docs.documents()
            .get(documentId=document_id, fields="body.content.endIndex")
            .execute(num_retries=3)
        )
        return max(
            (
                int(item.get("endIndex") or 1)
                for item in document.get("body", {}).get("content", [])
            ),
            default=1,
        )

    def create_document(
        self,
        folder_id: str,
        filename: str,
        content: str,
        *,
        minutes_document: bool = False,
        idempotency_key: str | None = None,
        session_id: str | None = None,
        artifact_type: str | None = None,
    ) -> str:
        app_properties = None
        existing = None
        if idempotency_key:
            app_properties = {
                DRIVE_ARTIFACT_KEY_PROPERTY: idempotency_key,
                DRIVE_SESSION_ID_PROPERTY: str(session_id or "")[:120],
                DRIVE_ARTIFACT_TYPE_PROPERTY: str(artifact_type or "")[:120],
            }
            existing = self._find_artifact_document(folder_id, idempotency_key)

        created_new = existing is None
        if existing:
            document_id = existing["id"]
            updated = (
                self.drive.files()
                .update(
                    fileId=document_id,
                    body={"name": filename, "appProperties": app_properties},
                    fields="id,webViewLink",
                    supportsAllDrives=True,
                )
                .execute(num_retries=3)
            )
            file_info = {**existing, **(updated or {})}
        else:
            body = {
                "name": filename,
                "mimeType": GOOGLE_DOC_MIME_TYPE,
                "parents": [folder_id],
            }
            if app_properties:
                body["appProperties"] = app_properties
            file_info = (
                self.drive.files()
                .create(
                    body=body,
                    fields="id,webViewLink",
                    supportsAllDrives=True,
                )
                .execute(num_retries=3)
            )
            document_id = file_info["id"]

        text, style_requests, image_index = _render_markdown(
            content,
            minutes_document=minutes_document,
        )
        replace_requests = []
        if not created_new:
            end_index = self._document_end_index(document_id)
            if end_index > 2:
                replace_requests.append(
                    {
                        "deleteContentRange": {
                            "range": {"startIndex": 1, "endIndex": end_index - 1}
                        }
                    }
                )
        try:
            self.docs.documents().batchUpdate(
                documentId=document_id,
                body={
                    "requests": [
                        *replace_requests,
                        {"insertText": {"location": {"index": 1}, "text": text}},
                        *style_requests,
                    ]
                },
            ).execute(num_retries=3)
        except Exception:
            if created_new:
                try:
                    self.drive.files().delete(
                        fileId=document_id, supportsAllDrives=True
                    ).execute(num_retries=3)
                except Exception:
                    log.exception(
                        "Failed to remove an empty Google Doc after content insertion failed"
                    )
            raise
        if minutes_document and image_index is not None:
            image_url = os.environ.get("YAGAPON_MINUTES_IMAGE_URL", DEFAULT_MINUTES_IMAGE_URL).strip()
            if image_url:
                try:
                    self.docs.documents().batchUpdate(
                        documentId=document_id,
                        body={
                            "requests": [
                                {
                                    "insertText": {
                                        "location": {"index": image_index},
                                        "text": " ",
                                    }
                                },
                                {
                                    "insertInlineImage": {
                                        "location": {"index": image_index + 1},
                                        "uri": image_url,
                                        "objectSize": {
                                            "height": {"magnitude": 52, "unit": "PT"},
                                            "width": {"magnitude": 64, "unit": "PT"},
                                        },
                                    }
                                }
                            ]
                        },
                    ).execute(num_retries=3)
                except Exception:
                    log.exception("Failed to insert the YagaPon image; keeping the styled minutes document")
        return file_info.get("webViewLink") or f"https://docs.google.com/document/d/{document_id}/edit"


async def diagnose_drive(folder_url: str) -> dict:
    folder_id = _extract_folder_id(folder_url)
    if not folder_id:
        raise ValueError("Invalid Google Drive folder URL")
    writer = await asyncio.to_thread(DriveWriter)
    return await asyncio.to_thread(writer.check_folder, folder_id)


async def upload_to_drive(
    folder_url: str,
    filename: str,
    content: str,
    *,
    subfolder_name: str | None = None,
    minutes_document: bool = False,
    session_id: str | None = None,
    artifact_type: str | None = None,
) -> str | None:
    """Create a styled Google Doc with ADC/service-account credentials."""
    folder_id = _extract_folder_id(folder_url)
    if not folder_id:
        log.error("Invalid Google Drive folder URL: %s", folder_url)
        return None
    try:
        writer = await asyncio.to_thread(DriveWriter)
        if subfolder_name:
            folder_id = await asyncio.to_thread(writer.get_or_create_folder, folder_id, subfolder_name)
        idempotency_key = (
            _artifact_key(session_id, artifact_type)
            if session_id and artifact_type
            else None
        )
        return await asyncio.to_thread(
            writer.create_document,
            folder_id,
            filename,
            content,
            minutes_document=minutes_document,
            idempotency_key=idempotency_key,
            session_id=session_id,
            artifact_type=artifact_type,
        )
    except Exception:
        log.exception("Direct Google Drive upload failed")
        return None


async def upload_minutes(
    config,
    guild_id: int,
    minutes: str,
    channel_name: str,
    *,
    meeting_at: datetime | str | None = None,
    session_id: str | None = None,
) -> str | None:
    folder_url = config.get_drive_folder(guild_id)
    if not folder_url:
        return None
    url = await upload_to_drive(
        folder_url,
        document_name(channel_name, meeting_at),
        minutes,
        minutes_document=True,
        session_id=session_id,
        artifact_type="minutes" if session_id else None,
    )
    if not url:
        raise RuntimeError("Configured Google Drive minutes upload failed")
    return url


async def upload_transcript(
    config,
    guild_id: int,
    transcript: str,
    channel_name: str,
    *,
    meeting_at: datetime | str | None = None,
    session_id: str | None = None,
) -> str | None:
    """Store the verbatim transcript in the Drive ``文字起こし`` subfolder."""
    if not transcript.strip():
        return None
    folder_url = config.get_drive_folder(guild_id)
    if not folder_url:
        return None
    url = await upload_to_drive(
        folder_url,
        document_name(channel_name, meeting_at),
        transcript,
        subfolder_name=TRANSCRIPT_FOLDER_NAME,
        session_id=session_id,
        artifact_type="transcript" if session_id else None,
    )
    if not url:
        raise RuntimeError("Configured Google Drive transcript upload failed")
    return url


async def upload_report(config, guild_id: int, report: str, report_type: str) -> str | None:
    folder_url = config.get_drive_folder(guild_id)
    if not folder_url:
        return None
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return await upload_to_drive(folder_url, f"{report_type}_{now}", report)
