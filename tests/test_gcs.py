import json
from io import BytesIO
from urllib.error import HTTPError

from bot.gcs import GCSClient


class Credentials:
    valid = True
    token = "access-token"


class Response:
    def __init__(self, payload=b"{}"):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.payload


def test_gcs_upload_uses_create_only_precondition(monkeypatch):
    requests = []

    def open_request(request, timeout):
        requests.append((request, timeout))
        return Response()

    monkeypatch.setattr("bot.gcs.urllib.request.urlopen", open_request)
    client = GCSClient("bucket", credentials=Credentials())

    assert client.upload_json("pending/id.json", {"value": "日本語"}, if_absent=True)
    request, timeout = requests[0]
    assert timeout == 30
    assert "ifGenerationMatch=0" in request.full_url
    assert request.get_header("Authorization") == "Bearer access-token"
    assert json.loads(request.data) == {"value": "日本語"}


def test_gcs_create_only_conflict_is_idempotent(monkeypatch):
    def conflict(_request, timeout):
        assert timeout == 30
        raise HTTPError("url", 412, "exists", {}, BytesIO(b"exists"))

    monkeypatch.setattr("bot.gcs.urllib.request.urlopen", conflict)
    client = GCSClient("bucket", credentials=Credentials())

    assert client.upload_json("pending/id.json", {}, if_absent=True) is False
