import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.gdrive import DriveWriter, _extract_folder_id, _render_markdown, upload_minutes, upload_transcript
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


@pytest.mark.asyncio
async def test_transcript_is_uploaded_as_a_separate_document():
    config = MagicMock()
    config.get_drive_folder.return_value = "https://drive.google.com/drive/folders/folder_123"
    uploader = AsyncMock(return_value="https://docs.example/transcript")

    with patch("bot.gdrive.upload_to_drive", uploader):
        result = await upload_transcript(
            config,
            1,
            "[Speaker 1]: 発言",
            "全体会議",
            meeting_at="2026-10-12T00:00:00+09:00",
        )

    assert result == "https://docs.example/transcript"
    args = uploader.await_args.args
    assert args[0] == "https://drive.google.com/drive/folders/folder_123"
    assert args[1] == "261012全体会議"
    assert args[2] == "[Speaker 1]: 発言"
    assert uploader.await_args.kwargs["subfolder_name"] == "文字起こし"


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
    assert update["body"]["requests"][0]["insertText"]["text"] == "本文\n"
    assert any("updateTextStyle" in request for request in update["body"]["requests"])


def test_minutes_document_matches_reference_style_and_inserts_yagapon_image():
    writer = DriveWriter.__new__(DriveWriter)
    writer.drive = MagicMock()
    writer.docs = MagicMock()
    writer.drive.files().create.return_value = execute_result(
        {"id": "doc-1", "webViewLink": "https://docs.example/doc-1"}
    )
    writer.docs.documents().batchUpdate.return_value = execute_result({})

    writer.create_document(
        "folder-1",
        "261014全体会議",
        "# 26/10/14 全体会議\n\n## 今日の議題\n- Discord整理\n\n## Discordについて\n### 問題\n権限が複雑",
        minutes_document=True,
    )

    calls = writer.docs.documents().batchUpdate.call_args_list
    assert len(calls) == 2
    first_requests = calls[0].kwargs["body"]["requests"]
    inserted = first_requests[0]["insertText"]["text"]
    assert "作成：おしゃべりやがぽん\n" in inserted
    assert any(
        request.get("updateParagraphStyle", {}).get("paragraphStyle", {}).get("alignment") == "CENTER"
        for request in first_requests
    )
    assert any(
        request.get("updateTextStyle", {}).get("textStyle", {}).get("foregroundColor", {})
        .get("color", {})
        .get("rgbColor", {})
        .get("red")
        == 19 / 255
        for request in first_requests
    )
    image_requests = calls[1].kwargs["body"]["requests"]
    assert image_requests[0]["insertText"]["text"] == " "
    image_request = image_requests[1]["insertInlineImage"]
    assert image_request["uri"].endswith("/assets/yagapon_minutes.png")
    assert image_request["objectSize"]["width"]["magnitude"] == 64


def test_minutes_markdown_uses_heading_3_for_reference_subheadings():
    text, requests, image_index = _render_markdown(
        "# 26/10/14 全体会議\n## 議題\n### 問題\n本文",
        minutes_document=True,
    )

    assert text.startswith("26/10/14 全体会議\n作成：おしゃべりやがぽん\n")
    assert image_index == 1 + len("26/10/14 全体会議")
    styles = [
        request["updateParagraphStyle"]["paragraphStyle"].get("namedStyleType")
        for request in requests
        if "updateParagraphStyle" in request
    ]
    assert "HEADING_1" in styles
    assert "HEADING_3" in styles


@pytest.mark.asyncio
async def test_minutes_upload_selects_minutes_document_style():
    config = MagicMock()
    config.get_drive_folder.return_value = "https://drive.google.com/drive/folders/folder_123"
    uploader = AsyncMock(return_value="https://docs.example/minutes")

    with patch("bot.gdrive.upload_to_drive", uploader):
        result = await upload_minutes(config, 1, "# 議事録", "全体会議")

    assert result == "https://docs.example/minutes"
    assert uploader.await_args.kwargs["minutes_document"] is True


def test_drive_writer_reuses_transcript_folder():
    writer = DriveWriter.__new__(DriveWriter)
    writer.drive = MagicMock()
    writer.drive.files().list.return_value = execute_result(
        {"files": [{"id": "transcripts", "name": "文字起こし"}]}
    )

    assert writer.get_or_create_folder("root", "文字起こし") == "transcripts"
    assert writer.drive.files().list.call_args.kwargs["corpora"] == "allDrives"
    writer.drive.files().create.assert_not_called()


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

    with patch.dict("os.environ", {"GOOGLE_OAUTH_AUTHORIZED_USER_JSON": ""}):
        with pytest.raises(RuntimeError, match="inside the container"):
            load_google_credentials(["scope"], str(missing))


def test_user_oauth_credentials_take_priority_over_service_account():
    info = {
        "type": "authorized_user",
        "client_id": "client-id",
        "client_secret": "client-secret",
        "refresh_token": "refresh-token",
    }

    with (
        patch.dict(
            "os.environ",
            {
                "GOOGLE_OAUTH_AUTHORIZED_USER_JSON": json.dumps(info),
                "GOOGLE_SERVICE_ACCOUNT_JSON": "/missing/service-account.json",
            },
        ),
        patch("bot.google_credentials.Credentials.from_authorized_user_info") as loader,
    ):
        credentials, source = load_google_credentials(["scope"])

    assert credentials is loader.return_value
    assert source == "user-oauth-json"
    loader.assert_called_once_with(info, scopes=["scope"])


def test_user_oauth_credentials_can_be_loaded_from_file(tmp_path):
    info = {
        "client_id": "client-id",
        "client_secret": "client-secret",
        "refresh_token": "refresh-token",
    }
    credentials_path = tmp_path / "authorized-user.json"
    credentials_path.write_text(json.dumps(info))

    with (
        patch.dict(
            "os.environ",
            {"GOOGLE_OAUTH_AUTHORIZED_USER_JSON": str(credentials_path)},
        ),
        patch("bot.google_credentials.Credentials.from_authorized_user_info") as loader,
    ):
        credentials, source = load_google_credentials(["scope"])

    assert credentials is loader.return_value
    assert source == "user-oauth-file"
    loader.assert_called_once_with(info, scopes=["scope"])


def test_user_oauth_rejects_service_account_json():
    with patch.dict(
        "os.environ",
        {
            "GOOGLE_OAUTH_AUTHORIZED_USER_JSON": json.dumps(
                {"type": "service_account"}
            )
        },
    ):
        with pytest.raises(RuntimeError, match="authorized-user credentials"):
            load_google_credentials(["scope"])
