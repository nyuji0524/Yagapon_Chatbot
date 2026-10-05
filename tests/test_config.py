import json

import pytest

from bot.config import ConfigManager


@pytest.mark.asyncio
async def test_config_is_saved_and_reloaded(tmp_path):
    path = tmp_path / "server_config.json"
    config = ConfigManager(path)

    await config.set_drive_folder(1, "https://drive.google.com/drive/folders/test")

    assert ConfigManager(path).get_drive_folder(1).endswith("/test")
    assert not path.with_suffix(".json.tmp").exists()


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
