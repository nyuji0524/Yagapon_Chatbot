import importlib.util
from pathlib import Path
from urllib.parse import parse_qs, urlparse

MODULE_PATH = Path(__file__).parents[1] / "deploy" / "reauthorize_google_drive.py"
SPEC = importlib.util.spec_from_file_location("reauthorize_google_drive", MODULE_PATH)
module = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(module)


def test_client_accepts_authorized_user_and_desktop_shapes():
    assert module._client({"client_id": "id", "client_secret": "secret"}) == (
        "id",
        "secret",
    )
    assert module._client({"installed": {"client_id": "id", "client_secret": "secret"}}) == (
        "id",
        "secret",
    )


def test_authorization_url_requires_offline_readonly_pkce():
    url = module._authorization_url("client", "http://127.0.0.1:1/", "state", "verifier")
    query = parse_qs(urlparse(url).query)

    assert query["access_type"] == ["offline"]
    assert query["prompt"] == ["consent"]
    assert query["code_challenge_method"] == ["S256"]
    assert "https://www.googleapis.com/auth/drive.readonly" in query["scope"][0]
    assert "https://www.googleapis.com/auth/drive.file" in query["scope"][0]
    assert query["redirect_uri"] == ["http://127.0.0.1:1/"]
