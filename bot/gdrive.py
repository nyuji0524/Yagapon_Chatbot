"""Google Drive連携 - Google Apps Script経由でGoogleドキュメントを作成"""

import logging
import os
import re
from datetime import datetime, timezone

import aiohttp

log = logging.getLogger("yagapon.gdrive")

# Google Apps ScriptのデプロイURL
APPS_SCRIPT_URL = os.environ.get("GOOGLE_APPS_SCRIPT_URL", "")


def _extract_folder_id(url: str) -> str | None:
    """Google DriveフォルダURLからIDを抽出"""
    match = re.search(r"/folders/([a-zA-Z0-9_-]+)", url)
    return match.group(1) if match else None


async def upload_to_drive(
    folder_url: str,
    filename: str,
    content: str,
) -> str | None:
    """Google Apps Script経由でGoogleドキュメントを作成"""
    if not APPS_SCRIPT_URL:
        log.warning("GOOGLE_APPS_SCRIPT_URL not set, skipping Drive upload")
        return None

    folder_id = _extract_folder_id(folder_url)
    if not folder_id:
        log.error(f"Invalid folder URL: {folder_url}")
        return None

    payload = {
        "folderId": folder_id,
        "fileName": filename,
        "content": content,
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                APPS_SCRIPT_URL,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    if "error" in data:
                        log.error(f"Apps Script error: {data['error']}")
                        return None
                    url = data.get("url")
                    log.info(f"Uploaded to Drive: {filename} -> {url}")
                    return url
                else:
                    text = await resp.text()
                    log.error(f"Apps Script HTTP {resp.status}: {text}")
                    return None
    except Exception as e:
        log.error(f"Drive upload error: {e}")
        return None


async def upload_minutes(config, guild_id: int, minutes: str, channel_name: str) -> str | None:
    """議事録をGoogleドキュメントとしてDriveに作成。URLを返す。"""
    folder_url = config.get_drive_folder(guild_id)
    if not folder_url:
        return None

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M")
    filename = f"議事録_{channel_name}_{now}"

    return await upload_to_drive(folder_url, filename, minutes)


async def upload_report(config, guild_id: int, report: str, report_type: str) -> str | None:
    """レポートをGoogleドキュメントとしてDriveに作成。URLを返す。"""
    folder_url = config.get_drive_folder(guild_id)
    if not folder_url:
        return None

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    filename = f"{report_type}_{now}"

    return await upload_to_drive(folder_url, filename, report)
