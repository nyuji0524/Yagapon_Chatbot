"""Minimal authenticated Google Cloud Storage JSON transport."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

import google.auth
from google.auth.transport.requests import Request

CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"


class GCSClient:
    def __init__(self, bucket: str, credentials=None):
        if not bucket or "/" in bucket:
            raise ValueError("A plain GCS bucket name is required")
        self.bucket = bucket
        if credentials is None:
            credentials, _ = google.auth.default(scopes=[CLOUD_PLATFORM_SCOPE])
        self.credentials = credentials

    def _authorization(self) -> str:
        if not getattr(self.credentials, "valid", False):
            self.credentials.refresh(Request())
        return f"Bearer {self.credentials.token}"

    def upload_json(self, name: str, payload: dict, *, if_absent: bool = False) -> bool:
        query = {
            "uploadType": "media",
            "name": name,
        }
        if if_absent:
            query["ifGenerationMatch"] = "0"
        url = (
            "https://storage.googleapis.com/upload/storage/v1/b/"
            f"{urllib.parse.quote(self.bucket, safe='')}/o?{urllib.parse.urlencode(query)}"
        )
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": self._authorization(),
                "Content-Type": "application/json; charset=utf-8",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30):
                return True
        except urllib.error.HTTPError as exc:
            if if_absent and exc.code in {409, 412}:
                return False
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise RuntimeError(f"GCS upload failed: {exc.code} {detail}") from exc

    def download_json(self, name: str) -> dict:
        object_name = urllib.parse.quote(name, safe="")
        url = (
            "https://storage.googleapis.com/storage/v1/b/"
            f"{urllib.parse.quote(self.bucket, safe='')}/o/{object_name}?alt=media"
        )
        request = urllib.request.Request(
            url,
            headers={"Authorization": self._authorization()},
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise RuntimeError(f"GCS download failed: {exc.code} {detail}") from exc
