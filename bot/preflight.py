"""Offline production preflight run before replacing the healthy container."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
from pathlib import Path


def main() -> None:
    config_path = Path(os.environ.get("YAGAPON_CONFIG_PATH", "/data/server_config.json"))
    if config_path.exists():
        payload = json.loads(config_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise RuntimeError("server config must be a JSON object")
    data_path = config_path.parent
    probe = data_path / ".preflight-write"
    probe.write_text("ok", encoding="utf-8")
    probe.unlink()
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg is missing")
    for variable in ("YAGAPON_RAG_DB_PATH", "YAGAPON_OPERATIONS_DB_PATH", "YAGAPON_KEY_DB_PATH"):
        path = Path(os.environ.get(variable, "")) if os.environ.get(variable) else None
        if path and path.exists():
            with sqlite3.connect(path) as connection:
                result = connection.execute("PRAGMA quick_check").fetchone()[0]
                if result != "ok":
                    raise RuntimeError(f"SQLite integrity check failed: {path}: {result}")
    # Import the startup graph without connecting to Discord or external APIs.
    from api.server import create_app  # noqa: F401
    from bot.client import YagaPon  # noqa: F401

    print("preflight ok")


if __name__ == "__main__":
    main()
