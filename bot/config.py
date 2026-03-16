"""設定管理 - サーバーごとの設定 + メンバー情報を server_config.json で管理"""

import json
import asyncio
from pathlib import Path
from typing import Optional

CONFIG_PATH = Path(__file__).parent.parent / "server_config.json"


class ConfigManager:
    """
    Per-guild config schema:
    {
        "guild_id": {
            "bureau": "IT局",
            "corpus_store_name": "fileSearchStores/xxx",
            "ignore_channels": [channel_id, ...],
            "github_webhook_channel": channel_id or null,
            "reactions": {
                "enabled": true,
                "interesting": "💡",
                "surprised": "😲",
                "funny": "😂"
            },
            "sheets_url": "https://docs.google.com/spreadsheets/d/...",
            "members": {
                "discord_user_id": {
                    "name": "中山裕二",
                    "role": "局長",
                    "tasks": ["bot開発", "サーバー管理"],
                    "grade": "M1"
                }
            }
        }
    }
    """

    def __init__(self):
        self._lock = asyncio.Lock()
        self._config: dict = {}
        self._load()

    # ------ persistence ------

    def _load(self):
        if CONFIG_PATH.exists():
            try:
                self._config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self._config = {}

    async def _save(self):
        async with self._lock:
            CONFIG_PATH.write_text(
                json.dumps(self._config, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

    # ------ guild helpers ------

    def _guild(self, guild_id: int) -> dict:
        key = str(guild_id)
        if key not in self._config:
            self._config[key] = {}
        return self._config[key]

    # ------ bureau / corpus ------

    def get_bureau(self, guild_id: int) -> Optional[str]:
        return self._guild(guild_id).get("bureau")

    def get_corpus(self, guild_id: int) -> Optional[str]:
        return self._guild(guild_id).get("corpus_store_name")

    async def set_bureau(self, guild_id: int, bureau: str, corpus_store_name: str):
        g = self._guild(guild_id)
        g["bureau"] = bureau
        g["corpus_store_name"] = corpus_store_name
        await self._save()

    # ------ ignore channels ------

    def is_ignored(self, guild_id: int, channel_id: int) -> bool:
        return channel_id in self._guild(guild_id).get("ignore_channels", [])

    async def add_ignore_channel(self, guild_id: int, channel_id: int) -> bool:
        g = self._guild(guild_id)
        channels = g.setdefault("ignore_channels", [])
        if channel_id in channels:
            return False
        channels.append(channel_id)
        await self._save()
        return True

    # ------ github ------

    def get_github_channel(self, guild_id: int) -> Optional[int]:
        return self._guild(guild_id).get("github_webhook_channel")

    async def set_github_channel(self, guild_id: int, channel_id: int):
        self._guild(guild_id)["github_webhook_channel"] = channel_id
        await self._save()

    # ------ reactions ------

    def get_reactions(self, guild_id: int) -> dict:
        return self._guild(guild_id).get("reactions", {
            "enabled": False,
            "interesting": "💡",
            "surprised": "😲",
            "funny": "😂",
        })

    async def set_reactions(self, guild_id: int, enabled: bool,
                            interesting: str, surprised: str, funny: str):
        self._guild(guild_id)["reactions"] = {
            "enabled": enabled,
            "interesting": interesting,
            "surprised": surprised,
            "funny": funny,
        }
        await self._save()

    # ------ role mapping ------

    def get_role_mapping(self, guild_id: int) -> dict:
        """{"position": [role_id, ...], "task": [role_id, ...], "grade": [role_id, ...]}"""
        return self._guild(guild_id).get("role_mapping", {})

    async def set_role_mapping(self, guild_id: int, mapping: dict):
        self._guild(guild_id)["role_mapping"] = mapping
        await self._save()

    # ------ members ------

    def get_members(self, guild_id: int) -> dict:
        return self._guild(guild_id).get("members", {})

    def get_member(self, guild_id: int, user_id: int) -> Optional[dict]:
        return self.get_members(guild_id).get(str(user_id))

    def find_guild_for_user(self, user_id: int) -> Optional[int]:
        """DM用: ユーザーが登録されているギルドIDを返す"""
        for gid, gdata in self._config.items():
            if str(user_id) in gdata.get("members", {}):
                return int(gid)
        return None

    async def set_members(self, guild_id: int, members: dict):
        self._guild(guild_id)["members"] = members
        await self._save()

    # ------ google drive ------

    def get_drive_folder(self, guild_id: int) -> Optional[str]:
        return self._guild(guild_id).get("drive_folder_url")

    async def set_drive_folder(self, guild_id: int, url: str):
        self._guild(guild_id)["drive_folder_url"] = url
        await self._save()

    # ------ sheets ------

    def get_sheets_url(self, guild_id: int) -> Optional[str]:
        return self._guild(guild_id).get("sheets_url")

    async def set_sheets_url(self, guild_id: int, url: str):
        self._guild(guild_id)["sheets_url"] = url
        await self._save()
