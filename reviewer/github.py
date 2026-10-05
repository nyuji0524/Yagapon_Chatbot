"""Minimal GitHub REST client used by the Actions reviewer."""

import base64
import json
import urllib.error
import urllib.parse
import urllib.request


class GitHubApiError(RuntimeError):
    pass


class GitHubClient:
    def __init__(self, token: str, api_url: str = "https://api.github.com"):
        if not token:
            raise ValueError("GITHUB_TOKEN is required")
        self._token = token
        self._api_url = api_url.rstrip("/")

    def request(self, method: str, path: str, payload: dict | None = None):
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            f"{self._api_url}{path}",
            data=body,
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self._token}",
                "User-Agent": "YagaPon-AI-Reviewer",
                "X-GitHub-Api-Version": "2022-11-28",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            details = exc.read().decode("utf-8", errors="replace")
            raise GitHubApiError(f"GitHub API {method} {path} failed: {exc.code} {details}") from exc

    def get(self, path: str):
        return self.request("GET", path)

    def post(self, path: str, payload: dict):
        return self.request("POST", path, payload)

    def list_pull_files(self, repository: str, pull_number: int) -> list[dict]:
        files = []
        for page in range(1, 31):
            batch = self.get(
                f"/repos/{repository}/pulls/{pull_number}/files?per_page=100&page={page}"
            )
            files.extend(batch)
            if len(batch) < 100:
                break
        return files

    def get_file_text(self, repository: str, path: str, ref: str) -> str | None:
        encoded_path = urllib.parse.quote(path, safe="/")
        encoded_ref = urllib.parse.quote(ref, safe="")
        try:
            data = self.get(f"/repos/{repository}/contents/{encoded_path}?ref={encoded_ref}")
        except GitHubApiError as exc:
            if " 404 " in str(exc):
                return None
            raise
        if not isinstance(data, dict) or data.get("encoding") != "base64":
            return None
        return base64.b64decode(data["content"]).decode("utf-8", errors="replace")

    def review_already_exists(self, repository: str, pull_number: int, marker: str) -> bool:
        reviews = self.get(f"/repos/{repository}/pulls/{pull_number}/reviews?per_page=100")
        return any(marker in (review.get("body") or "") for review in reviews)

    def create_review(self, repository: str, pull_number: int, payload: dict):
        return self.post(f"/repos/{repository}/pulls/{pull_number}/reviews", payload)
