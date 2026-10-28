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
    full_sqlite_check = (
        os.environ.get("YAGAPON_PREFLIGHT_FULL_SQLITE_CHECK", "false").lower()
        == "true"
    )
    for variable in ("YAGAPON_RAG_DB_PATH", "YAGAPON_OPERATIONS_DB_PATH", "YAGAPON_KEY_DB_PATH"):
        path = Path(os.environ.get(variable, "")) if os.environ.get(variable) else None
        if path and path.exists():
            with sqlite3.connect(path) as connection:
                if full_sqlite_check:
                    result = connection.execute("PRAGMA quick_check").fetchone()[0]
                    if result != "ok":
                        raise RuntimeError(f"SQLite integrity check failed: {path}: {result}")
                else:
                    # A full quick_check scans every page.  The production RAG
                    # database is large enough that doing so before every
                    # restart adds minutes of downtime risk.  Deployment only
                    # needs to prove that SQLite can open and read the schema;
                    # the opt-in full check remains available for maintenance.
                    result = connection.execute("PRAGMA schema_version").fetchone()
                    if result is None:
                        raise RuntimeError(f"SQLite schema is unreadable: {path}")
    print("preflight ok")


if __name__ == "__main__":
    main()
