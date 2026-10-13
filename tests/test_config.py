import json
from datetime import datetime, timezone

import pytest

from bot.config import ConfigManager


@pytest.mark.asyncio
async def test_config_is_saved_and_reloaded(tmp_path):
    path = tmp_path / "server_config.json"
    config = ConfigManager(path)

    await config.set_drive_folder(1, "https://drive.google.com/drive/folders/test")

    assert ConfigManager(path).get_drive_folder(1).endswith("/test")
    assert not path.with_suffix(".json.tmp").exists()


@pytest.mark.asyncio
async def test_minutes_channel_is_saved_as_an_integer(tmp_path):
    path = tmp_path / "server_config.json"
    config = ConfigManager(path)

    await config.set_minutes_channel(1, 123456789)

    assert ConfigManager(path).get_minutes_channel(1) == 123456789


def test_corrupt_config_stops_startup_instead_of_being_overwritten(tmp_path):
    path = tmp_path / "server_config.json"
    path.write_text("{broken", encoding="utf-8")

    with pytest.raises(RuntimeError):
        ConfigManager(path)

    assert path.read_text(encoding="utf-8") == "{broken"


def test_saved_config_is_valid_json(tmp_path):
    path = tmp_path / "server_config.json"
    path.write_text('{"1": {"bureau": "IT局"}}', encoding="utf-8")

    loaded = ConfigManager(path)

    assert json.loads(path.read_text(encoding="utf-8")) == loaded._config


def test_glossary_vocabulary_contains_terms_readings_and_aliases(tmp_path):
    config = ConfigManager(tmp_path / "server_config.json")
    config._guild(1)["glossary"] = {
        "矢上祭": {
            "definition": "大学祭",
            "reading": "やがみさい",
            "aliases": ["矢実", "やがみさい"],
        },
        "やがサポ": {
            "definition": "運営アプリ",
            "aliases": ["Yaga Support"],
        },
        "IT局": {
            "definition": "実行委員会のIT担当局",
            "aliases": None,
        },
    }

    assert config.get_glossary_vocabulary(1) == [
        "矢上祭",
        "やがみさい",
        "矢実",
        "やがサポ",
        "Yaga Support",
        "IT局",
    ]


@pytest.mark.asyncio
async def test_backfill_cursor_is_saved_and_reloaded(tmp_path):
    path = tmp_path / "server_config.json"
    config = ConfigManager(path)
    message_at = datetime(2026, 10, 5, 1, 2, tzinfo=timezone.utc)

    assert config.has_backfill_cursors(1) is False

    await config.set_backfill_cursor(1, 20, 999, message_at)

    reloaded = ConfigManager(path)
    assert reloaded.has_backfill_cursors(1) is True
    assert reloaded.get_backfill_cursor(1, 20) == {
        "message_id": "999",
        "message_at": message_at.isoformat(),
    }
