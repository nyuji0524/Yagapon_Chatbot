from bot.secret_value import read_secret


def test_read_secret_prefers_direct_environment(monkeypatch, tmp_path):
    path = tmp_path / "token"
    path.write_text("file-token\n")
    monkeypatch.setenv("TOKEN", "direct-token")
    monkeypatch.setenv("TOKEN_FILE", str(path))

    assert read_secret("TOKEN") == "direct-token"


def test_read_secret_reads_mounted_file(monkeypatch, tmp_path):
    path = tmp_path / "token"
    path.write_text("file-token\n")
    monkeypatch.delenv("TOKEN", raising=False)
    monkeypatch.setenv("TOKEN_FILE", str(path))

    assert read_secret("TOKEN") == "file-token"
