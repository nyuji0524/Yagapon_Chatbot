#!/usr/bin/env python3
"""Create a Drive authorized-user credential without printing secret values."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import secrets
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
SCOPES = (
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/documents",
)


def _client(source: dict) -> tuple[str, str]:
    nested = source.get("installed") or source.get("web") or source
    client_id = str(nested.get("client_id") or "")
    client_secret = str(nested.get("client_secret") or "")
    if not client_id or not client_secret:
        raise ValueError("client_id and client_secret are required")
    return client_id, client_secret


def _challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _authorization_url(client_id: str, redirect_uri: str, state: str, verifier: str) -> str:
    query = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(SCOPES),
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
            "state": state,
            "code_challenge": _challenge(verifier),
            "code_challenge_method": "S256",
        }
    )
    return f"{AUTH_ENDPOINT}?{query}"


def _exchange(
    *, client_id: str, client_secret: str, code: str, redirect_uri: str, verifier: str
) -> dict:
    body = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "code_verifier": verifier,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        }
    ).encode("ascii")
    request = urllib.request.Request(
        TOKEN_ENDPOINT,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--client", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    source = json.loads(args.client.read_text(encoding="utf-8"))
    client_id, client_secret = _client(source)
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    result: dict[str, str] = {}

    class Callback(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            values = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if values.get("state", [""])[0] != state:
                self.send_error(400, "Invalid OAuth state")
                return
            if values.get("error"):
                result["error"] = values["error"][0]
            elif values.get("code"):
                result["code"] = values["code"][0]
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write("認証を受け付けました。このタブを閉じてください。".encode())

        def log_message(self, _format, *_args):
            return

    server = HTTPServer(("127.0.0.1", 0), Callback)
    redirect_uri = f"http://127.0.0.1:{server.server_port}/"
    url = _authorization_url(client_id, redirect_uri, state, verifier)
    print("Google認証画面を開きます。Driveの読み取り権限を許可してください。")
    if args.no_browser or not webbrowser.open(url):
        print(url)
    server.handle_request()
    server.server_close()
    if result.get("error") or not result.get("code"):
        raise SystemExit(f"OAuth authorization failed: {result.get('error', 'missing code')}")

    token = _exchange(
        client_id=client_id,
        client_secret=client_secret,
        code=result["code"],
        redirect_uri=redirect_uri,
        verifier=verifier,
    )
    refresh_token = str(token.get("refresh_token") or "")
    granted = set(str(token.get("scope") or "").split())
    missing = set(SCOPES) - granted
    if not refresh_token or missing:
        raise SystemExit("OAuth response did not include the required offline scopes")
    payload = {
        "type": "authorized_user",
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "token_uri": TOKEN_ENDPOINT,
        "scopes": list(SCOPES),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.chmod(args.output, 0o600)
    print(f"認証情報を安全に保存しました: {args.output}")


if __name__ == "__main__":
    main()
