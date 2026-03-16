"""Google Drive連携 - 議事録・レポートをGoogleドキュメントとして作成"""

import io
import logging
import os
import re
from datetime import datetime, timezone

log = logging.getLogger("yagapon.gdrive")


def _extract_folder_id(url: str) -> str | None:
    """Google DriveフォルダURLからIDを抽出"""
    match = re.search(r"/folders/([a-zA-Z0-9_-]+)", url)
    return match.group(1) if match else None


def _get_credentials():
    """サービスアカウント認証情報を取得"""
    from google.oauth2 import service_account

    creds_path = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "service_account.json")
    if not os.path.exists(creds_path):
        return None

    return service_account.Credentials.from_service_account_file(
        creds_path,
        scopes=["https://www.googleapis.com/auth/drive"],
    )


async def upload_to_drive(
    folder_url: str,
    filename: str,
    content: str,
    as_google_doc: bool = True,
) -> str | None:
    """
    Google Driveにアップロード。
    as_google_doc=True: Googleドキュメントとして作成 (ブラウザで編集可能)
    as_google_doc=False: .mdファイルとしてアップロード
    """
    import asyncio

    folder_id = _extract_folder_id(folder_url)
    if not folder_id:
        log.error(f"Invalid folder URL: {folder_url}")
        return None

    creds = _get_credentials()
    if not creds:
        log.warning("No service account credentials, skipping Drive upload")
        return None

    def _upload():
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaIoBaseUpload

        service = build("drive", "v3", credentials=creds)

        file_metadata = {
            "name": filename,
            "parents": [folder_id],
        }

        if as_google_doc:
            # text/html → Google Docs に自動変換
            # MarkdownをシンプルなHTMLに変換してからアップロード
            html = _markdown_to_html(content)
            media = MediaIoBaseUpload(
                io.BytesIO(html.encode("utf-8")),
                mimetype="text/html",
                resumable=False,
            )
            file_metadata["mimeType"] = "application/vnd.google-apps.document"
        else:
            media = MediaIoBaseUpload(
                io.BytesIO(content.encode("utf-8")),
                mimetype="text/markdown",
                resumable=False,
            )

        file = service.files().create(
            body=file_metadata,
            media_body=media,
            fields="id, webViewLink",
            supportsAllDrives=True,
        ).execute()

        return file.get("webViewLink")

    try:
        loop = asyncio.get_event_loop()
        url = await loop.run_in_executor(None, _upload)
        log.info(f"Uploaded to Drive: {filename} -> {url}")
        return url
    except Exception as e:
        log.error(f"Drive upload error: {e}")
        return None


def _markdown_to_html(md: str) -> str:
    """Markdownを簡易HTMLに変換 (Google Docsインポート用)"""
    import re as re_mod

    lines = md.split("\n")
    html_lines = []

    for line in lines:
        # 見出し
        if line.startswith("### "):
            html_lines.append(f"<h3>{line[4:]}</h3>")
        elif line.startswith("## "):
            html_lines.append(f"<h2>{line[3:]}</h2>")
        elif line.startswith("# "):
            html_lines.append(f"<h1>{line[2:]}</h1>")
        # リスト
        elif line.startswith("- "):
            html_lines.append(f"<li>{line[2:]}</li>")
        # 引用
        elif line.startswith("> "):
            html_lines.append(f"<blockquote>{line[2:]}</blockquote>")
        # 太字
        elif "**" in line:
            line = re_mod.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", line)
            html_lines.append(f"<p>{line}</p>")
        # 空行
        elif not line.strip():
            html_lines.append("<br>")
        else:
            html_lines.append(f"<p>{line}</p>")

    return f"<html><body>{''.join(html_lines)}</body></html>"


async def upload_minutes(config, guild_id: int, minutes: str, channel_name: str) -> str | None:
    """議事録をGoogleドキュメントとしてDriveに作成。URLを返す。"""
    folder_url = config.get_drive_folder(guild_id)
    if not folder_url:
        return None

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M")
    filename = f"議事録_{channel_name}_{now}"

    return await upload_to_drive(folder_url, filename, minutes, as_google_doc=True)


async def upload_report(config, guild_id: int, report: str, report_type: str) -> str | None:
    """レポートをGoogleドキュメントとしてDriveに作成。URLを返す。"""
    folder_url = config.get_drive_folder(guild_id)
    if not folder_url:
        return None

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    filename = f"{report_type}_{now}"

    return await upload_to_drive(folder_url, filename, report, as_google_doc=True)
