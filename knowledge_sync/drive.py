"""Read-only Google Shared Drive change reader."""

import io
from dataclasses import dataclass

import google.auth
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

READONLY_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
EXPORT_TYPES = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
    "application/vnd.google-apps.presentation": "text/plain",
}
TEXT_TYPES = {"text/plain", "text/markdown", "text/csv", "application/json"}


@dataclass
class DriveDocument:
    file_id: str
    name: str
    mime_type: str
    modified_time: str
    web_view_link: str
    parents: list[str]
    removed: bool = False


class DriveReader:
    def __init__(self, credentials_path: str | None, drive_id: str, folder_id: str | None = None):
        if credentials_path:
            credentials = service_account.Credentials.from_service_account_file(
                credentials_path,
                scopes=[READONLY_SCOPE],
            )
        else:
            credentials, _ = google.auth.default(scopes=[READONLY_SCOPE])
        self._service = build("drive", "v3", credentials=credentials, cache_discovery=False)
        self.drive_id = drive_id
        self.folder_id = folder_id

    def get_start_page_token(self) -> str:
        response = self._service.changes().getStartPageToken(driveId=self.drive_id).execute()
        return response["startPageToken"]

    def list_all(self) -> list[DriveDocument]:
        documents = []
        page_token = None
        query = "trashed = false"
        if self.folder_id:
            query += f" and '{self.folder_id}' in parents"
        while True:
            response = self._service.files().list(
                corpora="drive",
                driveId=self.drive_id,
                includeItemsFromAllDrives=True,
                supportsAllDrives=True,
                q=query,
                pageSize=1000,
                pageToken=page_token,
                fields="nextPageToken,files(id,name,mimeType,modifiedTime,webViewLink,parents,trashed)",
            ).execute()
            documents.extend(self._to_document(item) for item in response.get("files", []))
            page_token = response.get("nextPageToken")
            if not page_token:
                return documents

    def list_changes(self, page_token: str) -> tuple[list[DriveDocument], str]:
        documents = []
        current_token = page_token
        new_start_token = page_token
        while current_token:
            response = self._service.changes().list(
                pageToken=current_token,
                driveId=self.drive_id,
                includeItemsFromAllDrives=True,
                supportsAllDrives=True,
                includeRemoved=True,
                pageSize=1000,
                fields=(
                    "nextPageToken,newStartPageToken,changes(removed,fileId,"
                    "file(id,name,mimeType,modifiedTime,webViewLink,parents,trashed))"
                ),
            ).execute()
            for change in response.get("changes", []):
                file_data = change.get("file")
                if change.get("removed") or not file_data or file_data.get("trashed"):
                    documents.append(
                        DriveDocument(
                            file_id=change["fileId"],
                            name="",
                            mime_type="",
                            modified_time="",
                            web_view_link="",
                            parents=[],
                            removed=True,
                        )
                    )
                    continue
                document = self._to_document(file_data)
                if not self.folder_id or self.folder_id in document.parents:
                    documents.append(document)
            current_token = response.get("nextPageToken")
            new_start_token = response.get("newStartPageToken") or new_start_token
        return documents, new_start_token

    def read_text(self, document: DriveDocument) -> str | None:
        if document.mime_type in EXPORT_TYPES:
            request = self._service.files().export_media(
                fileId=document.file_id,
                mimeType=EXPORT_TYPES[document.mime_type],
            )
        elif document.mime_type in TEXT_TYPES:
            request = self._service.files().get_media(fileId=document.file_id)
        else:
            return None

        output = io.BytesIO()
        downloader = MediaIoBaseDownload(output, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        return output.getvalue().decode("utf-8", errors="replace")

    @staticmethod
    def _to_document(item: dict) -> DriveDocument:
        return DriveDocument(
            file_id=item["id"],
            name=item.get("name") or "名称未設定",
            mime_type=item.get("mimeType") or "",
            modified_time=item.get("modifiedTime") or "",
            web_view_link=item.get("webViewLink") or "",
            parents=item.get("parents") or [],
        )
