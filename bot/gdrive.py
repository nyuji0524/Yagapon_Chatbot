"""Google Drive integration using Drive and Docs APIs directly."""

import asyncio
import logging
import re
from datetime import datetime, timezone

from googleapiclient.discovery import build

from bot.google_credentials import load_google_credentials

log = logging.getLogger("yagapon.gdrive")

DRIVE_FILE_SCOPE = "https://www.googleapis.com/auth/drive.file"
DOCS_SCOPE = "https://www.googleapis.com/auth/documents"
GOOGLE_DOC_MIME_TYPE = "application/vnd.google-apps.document"


def _extract_folder_id(url: str) -> str | None:
    """Extract a Drive folder ID from a configured URL."""
    match = re.search(r"/folders/([a-zA-Z0-9_-]+)", url)
    return match.group(1) if match else None


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

    def create_document(self, folder_id: str, filename: str, content: str) -> str:
        created = (
            self.drive.files()
            .create(
                body={
                    "name": filename,
                    "mimeType": GOOGLE_DOC_MIME_TYPE,
                    "parents": [folder_id],
                },
                fields="id,webViewLink",
                supportsAllDrives=True,
            )
            .execute(num_retries=3)
        )
        document_id = created["id"]
        try:
            self.docs.documents().batchUpdate(
                documentId=document_id,
                body={"requests": [{"insertText": {"location": {"index": 1}, "text": content}}]},
            ).execute(num_retries=3)
        except Exception:
            try:
                self.drive.files().delete(
                    fileId=document_id,
                    supportsAllDrives=True,
                ).execute(num_retries=3)
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


async def upload_to_drive(folder_url: str, filename: str, content: str) -> str | None:
    """Create a Google Doc with ADC/service-account credentials."""
    folder_id = _extract_folder_id(folder_url)
    if not folder_id:
        log.error("Invalid Google Drive folder URL: %s", folder_url)
        return None
    try:
        writer = await asyncio.to_thread(DriveWriter)
        return await asyncio.to_thread(
            writer.create_document,
            folder_id,
            filename,
            content,
        )
    except Exception:
        log.exception("Direct Google Drive upload failed")
        return None


async def upload_minutes(config, guild_id: int, minutes: str, channel_name: str) -> str | None:
    folder_url = config.get_drive_folder(guild_id)
    if not folder_url:
        return None
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M")
    return await upload_to_drive(folder_url, f"議事録_{channel_name}_{now}", minutes)


async def upload_report(config, guild_id: int, report: str, report_type: str) -> str | None:
    folder_url = config.get_drive_folder(guild_id)
    if not folder_url:
        return None
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return await upload_to_drive(folder_url, f"{report_type}_{now}", report)
