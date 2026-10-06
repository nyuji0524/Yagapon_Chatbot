from unittest.mock import MagicMock

import pytest

from bot.gdrive import DriveWriter, _extract_folder_id
from bot.google_credentials import load_google_credentials


def execute_result(value=None, error: Exception | None = None):
    request = MagicMock()
    if error:
        request.execute.side_effect = error
    else:
        request.execute.return_value = value
    return request


def test_extract_folder_id():
    assert _extract_folder_id("https://drive.google.com/drive/folders/folder_123") == "folder_123"
    assert _extract_folder_id("https://example.com/no-folder") is None


def test_drive_writer_creates_document_and_inserts_content():
    writer = DriveWriter.__new__(DriveWriter)
    writer.drive = MagicMock()
    writer.docs = MagicMock()
    writer.drive.files().create.return_value = execute_result(
        {"id": "doc-1", "webViewLink": "https://docs.example/doc-1"}
    )
    writer.docs.documents().batchUpdate.return_value = execute_result({})

    url = writer.create_document("folder-1", "議事録", "本文")

    assert url == "https://docs.example/doc-1"
    create = writer.drive.files().create.call_args.kwargs
    assert create["body"]["parents"] == ["folder-1"]
    update = writer.docs.documents().batchUpdate.call_args.kwargs
    assert update["body"]["requests"][0]["insertText"]["text"] == "本文"


def test_drive_writer_removes_empty_document_when_insert_fails():
    writer = DriveWriter.__new__(DriveWriter)
    writer.drive = MagicMock()
    writer.docs = MagicMock()
    writer.drive.files().create.return_value = execute_result({"id": "doc-1"})
    writer.docs.documents().batchUpdate.return_value = execute_result(error=RuntimeError("failed"))
    writer.drive.files().delete.return_value = execute_result({})

    with pytest.raises(RuntimeError):
        writer.create_document("folder-1", "議事録", "本文")

    writer.drive.files().delete.assert_called_once_with(
        fileId="doc-1",
        supportsAllDrives=True,
    )


def test_missing_legacy_credential_path_has_actionable_error(tmp_path):
    missing = tmp_path / "missing.json"

    with pytest.raises(RuntimeError, match="inside the container"):
        load_google_credentials(["scope"], str(missing))
