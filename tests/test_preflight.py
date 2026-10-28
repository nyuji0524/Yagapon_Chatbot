from __future__ import annotations

from types import SimpleNamespace

from bot import preflight


class _Connection:
    def __init__(self, queries: list[str]):
        self.queries = queries

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query: str):
        self.queries.append(query)
        value = "ok" if query == "PRAGMA quick_check" else 1
        return SimpleNamespace(fetchone=lambda: (value,))


def _prepare(tmp_path, monkeypatch):
    config = tmp_path / "server_config.json"
    config.write_text("{}", encoding="utf-8")
    database = tmp_path / "rag.sqlite3"
    database.touch()
    monkeypatch.setenv("YAGAPON_CONFIG_PATH", str(config))
    monkeypatch.setenv("YAGAPON_RAG_DB_PATH", str(database))
    monkeypatch.delenv("YAGAPON_OPERATIONS_DB_PATH", raising=False)
    monkeypatch.delenv("YAGAPON_KEY_DB_PATH", raising=False)


def test_deploy_preflight_reads_only_sqlite_header(tmp_path, monkeypatch):
    _prepare(tmp_path, monkeypatch)
    (tmp_path / "rag.sqlite3").write_bytes(b"SQLite format 3\x00fixture")
    monkeypatch.delenv("YAGAPON_PREFLIGHT_FULL_SQLITE_CHECK", raising=False)
    monkeypatch.setattr(
        preflight.sqlite3,
        "connect",
        lambda _path: (_ for _ in ()).throw(AssertionError("must not open SQLite")),
    )
    monkeypatch.setattr(preflight.shutil, "which", lambda _name: "/usr/bin/ffmpeg")

    preflight.main()


def test_maintenance_preflight_can_request_full_sqlite_check(tmp_path, monkeypatch):
    _prepare(tmp_path, monkeypatch)
    queries = []
    monkeypatch.setenv("YAGAPON_PREFLIGHT_FULL_SQLITE_CHECK", "true")
    monkeypatch.setattr(preflight.sqlite3, "connect", lambda _path: _Connection(queries))
    monkeypatch.setattr(preflight.shutil, "which", lambda _name: "/usr/bin/ffmpeg")

    preflight.main()

    assert queries == ["PRAGMA quick_check"]
