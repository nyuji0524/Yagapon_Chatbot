"""Google Drive integration using Drive and Docs APIs directly."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone

from googleapiclient.discovery import build

from bot.google_credentials import load_google_credentials
from bot.meeting_time import document_name

log = logging.getLogger("yagapon.gdrive")

DRIVE_FILE_SCOPE = "https://www.googleapis.com/auth/drive.file"
DOCS_SCOPE = "https://www.googleapis.com/auth/documents"
GOOGLE_DOC_MIME_TYPE = "application/vnd.google-apps.document"
GOOGLE_FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"
TRANSCRIPT_FOLDER_NAME = "文字起こし"


def _extract_folder_id(url: str) -> str | None:
    """Extract a Drive folder ID from a configured URL."""
    match = re.search(r"/folders/([a-zA-Z0-9_-]+)", url)
    return match.group(1) if match else None


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


def _render_markdown(content: str) -> tuple[str, list[dict]]:
    """Convert the limited minutes Markdown into a styled Google Doc."""
    rendered: list[str] = []
    headings: list[tuple[int, int, str]] = []
    bullets: list[tuple[int, int]] = []
    bold_ranges: list[tuple[int, int]] = []
    index = 1

    for raw_line in content.strip().splitlines():
        line = raw_line.rstrip()
        heading = re.match(r"^(#{1,4})\s+(.+)$", line)
        bullet = re.match(r"^\s*[-*]\s+(.+)$", line)
        named_style = "NORMAL_TEXT"
        if heading:
            level = len(heading.group(1))
            line = heading.group(2)
            named_style = "TITLE" if level == 1 else f"HEADING_{min(level - 1, 3)}"
        elif bullet:
            line = bullet.group(1)

        plain_line, inline_bold = _strip_inline_markdown(line, index)
        end = index + _utf16_length(plain_line)
        rendered.append(plain_line + "\n")
        bold_ranges.extend(inline_bold)
        if heading and end > index:
            headings.append((index, end, named_style))
        if bullet:
            bullets.append((index, end + 1))
        index = end + 1

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
    for start, end, style in headings:
        requests.append(
            {
                "updateParagraphStyle": {
                    "range": {"startIndex": start, "endIndex": end},
                    "paragraphStyle": {"namedStyleType": style},
                    "fields": "namedStyleType",
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
    return text, requests


class DriveWriter:
    def __init__(self):
        credentials, self.credential_source = load_google_credentials([DRIVE_FILE_SCOPE, DOCS_SCOPE])
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

    def create_document(self, folder_id: str, filename: str, content: str) -> str:
        created = (
            self.drive.files()
            .create(
                body={"name": filename, "mimeType": GOOGLE_DOC_MIME_TYPE, "parents": [folder_id]},
                fields="id,webViewLink",
                supportsAllDrives=True,
            )
            .execute(num_retries=3)
        )
        document_id = created["id"]
        text, style_requests = _render_markdown(content)
        try:
            self.docs.documents().batchUpdate(
                documentId=document_id,
                body={
                    "requests": [
                        {"insertText": {"location": {"index": 1}, "text": text}},
                        *style_requests,
                    ]
                },
            ).execute(num_retries=3)
        except Exception:
            try:
                self.drive.files().delete(fileId=document_id, supportsAllDrives=True).execute(num_retries=3)
            except Exception:
                log.exception("Failed to remove an empty Google Doc after content insertion failed")
            raise
        return created.get("webViewLink") or f"https://docs.google.com/document/d/{document_id}/edit"


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
        return await asyncio.to_thread(writer.create_document, folder_id, filename, content)
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
) -> str | None:
    folder_url = config.get_drive_folder(guild_id)
    if not folder_url:
        return None
    return await upload_to_drive(folder_url, document_name(channel_name, meeting_at), minutes)


async def upload_transcript(
    config,
    guild_id: int,
    transcript: str,
    channel_name: str,
    *,
    meeting_at: datetime | str | None = None,
) -> str | None:
    """Store the verbatim transcript in the Drive ``文字起こし`` subfolder."""
    if not transcript.strip():
        return None
    folder_url = config.get_drive_folder(guild_id)
    if not folder_url:
        return None
    return await upload_to_drive(
        folder_url,
        document_name(channel_name, meeting_at),
        transcript,
        subfolder_name=TRANSCRIPT_FOLDER_NAME,
    )


async def upload_report(config, guild_id: int, report: str, report_type: str) -> str | None:
    folder_url = config.get_drive_folder(guild_id)
    if not folder_url:
        return None
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return await upload_to_drive(folder_url, f"{report_type}_{now}", report)
