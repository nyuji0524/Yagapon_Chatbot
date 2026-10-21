"""Publish meeting transcripts to the private Knowledge repository via Draft PRs."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from zoneinfo import ZoneInfo

log = logging.getLogger("yagapon.knowledge_publish")

DEFAULT_REPOSITORY = "YagamiFes-IT/YagamiFes-IT-Knowledge"
JST = ZoneInfo("Asia/Tokyo")
SENSITIVE_PATTERNS = {
    "private-key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "github-token": re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b|\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    "google-api-key": re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),
    "credential-assignment": re.compile(
        r"(?i)(?:password|passwd|secret|api[_ -]?key|token|パスワード)\s*[:=]\s*[^\s]{8,}"
    ),
}


@dataclass(frozen=True)
class KnowledgePublishResult:
    session_id: str
    outbox_dir: Path
    pull_request_url: str | None = None
    status: str = "pending"


class GitHubKnowledgePublisher:
    """Small synchronous GitHub Git Data API client."""

    def __init__(self, token: str, repository: str, *, base_branch: str = "main"):
        self.token = token
        self.repository = repository
        self.base_branch = base_branch
        self.api_root = f"https://api.github.com/repos/{repository}"

    def _request(
        self,
        method: str,
        path: str,
        payload: dict | None = None,
        *,
        allow_not_found: bool = False,
    ):
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            f"{self.api_root}{path}",
            data=data,
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "User-Agent": "yagapon-knowledge-publisher",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if allow_not_found and exc.code == 404:
                return None
            details = exc.read().decode("utf-8", errors="replace")[:1000]
            raise RuntimeError(f"GitHub API {method} {path} failed: {exc.code} {details}") from exc

    def publish(self, *, branch: str, title: str, body: str, files: dict[str, str]) -> str:
        owner = self.repository.split("/", 1)[0]
        query = urllib.parse.urlencode({"state": "all", "head": f"{owner}:{branch}"})
        existing = self._request("GET", f"/pulls?{query}")
        if existing:
            return existing[0]["html_url"]

        base_ref = self._request("GET", f"/git/ref/heads/{self.base_branch}")
        base_sha = base_ref["object"]["sha"]
        base_commit = self._request("GET", f"/git/commits/{base_sha}")

        tree_items = []
        for path, content in sorted(files.items()):
            blob = self._request(
                "POST",
                "/git/blobs",
                {
                    "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
                    "encoding": "base64",
                },
            )
            tree_items.append(
                {"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]}
            )

        tree = self._request(
            "POST",
            "/git/trees",
            {"base_tree": base_commit["tree"]["sha"], "tree": tree_items},
        )
        commit = self._request(
            "POST",
            "/git/commits",
            {
                "message": title,
                "tree": tree["sha"],
                "parents": [base_sha],
            },
        )
        branch_path = urllib.parse.quote(branch, safe="/")
        existing_ref = self._request(
            "GET",
            f"/git/ref/heads/{branch_path}",
            allow_not_found=True,
        )
        if existing_ref:
            self._request(
                "PATCH",
                f"/git/refs/heads/{branch_path}",
                {"sha": commit["sha"], "force": True},
            )
        else:
            self._request(
                "POST",
                "/git/refs",
                {"ref": f"refs/heads/{branch}", "sha": commit["sha"]},
            )
        pull_request = self._request(
            "POST",
            "/pulls",
            {
                "title": title,
                "body": body,
                "head": branch,
                "base": self.base_branch,
                "draft": True,
            },
        )
        return pull_request["html_url"]


def festival_number(when: datetime) -> int:
    local = when.astimezone(JST)
    return local.year + (2 if local.month >= 10 else 1) - 2000


def _session_time(session_id: str) -> datetime:
    match = re.match(r"(\d{8}T\d{6}Z)", session_id)
    if not match:
        return datetime.now(timezone.utc)
    return datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)


def _yaml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _sensitive_labels(files: dict[str, str]) -> list[str]:
    labels = set()
    for content in files.values():
        for label, pattern in SENSITIVE_PATTERNS.items():
            if pattern.search(content):
                labels.add(label)
    return sorted(labels)


def build_meeting_bundle(
    *,
    session_id: str,
    guild_id: int,
    channel_name: str,
    mode: str,
    transcript: str,
    summary: str | None = None,
    minutes: str | None = None,
    include_minutes: bool = False,
) -> tuple[str, dict[str, str], dict]:
    """Build review-required Knowledge source files and their repository root."""
    started_at = _session_time(session_id)
    local_time = started_at.astimezone(JST)
    festival = festival_number(started_at)
    date = local_time.date().isoformat()
    root = f"sources/meetings/yagapon/{festival}th/{date}/{session_id}"
    title = f"{date} {channel_name} 生文字起こし"

    front_matter = (
        "---\n"
        f"title: {_yaml_string(title)}\n"
        "type: source\n"
        f"festival: {festival}\n"
        f"date: {_yaml_string(date)}\n"
        f"last_updated: {_yaml_string(datetime.now(JST).date().isoformat())}\n"
        "status: draft\n"
        "contributors: []\n"
        "tags: [meeting, transcript, yagapon, needs-human-review]\n"
        "sources:\n"
        "  - kind: discord-voice\n"
        f"    ref: {_yaml_string(f'discord-guild:{guild_id}/session:{session_id}')}\n"
        f"    locator: {_yaml_string(channel_name)}\n"
        f"    accessed_on: {_yaml_string(date)}\n"
        "---\n"
    )
    transcript_document = (
        f"{front_matter}\n# {title}\n\n"
        "> AIによる自動文字起こしです。誤認識、話者ラベル、固有名詞を人が確認するまで、"
        "現在の決定事項や運用根拠として扱わないでください。\n\n"
        "## セッション情報\n\n"
        f"- 開始日時: {local_time.isoformat(timespec='seconds')}\n"
        f"- チャンネル: {channel_name}\n"
        f"- モード: {mode}\n"
        f"- セッションID: `{session_id}`\n\n"
        "## 生文字起こし\n\n"
        f"{transcript.rstrip()}\n"
    )
    files = {f"{root}/transcript.md": transcript_document}

    if summary and summary.strip():
        files[f"{root}/summary.md"] = (
            f"# AI要約（要確認）: {date} {channel_name}\n\n"
            "> 生文字起こしから自動生成した参考資料です。事実・決定・担当・期限は"
            "transcript.mdと人間の確認を優先してください。\n\n"
            f"{summary.strip()}\n"
        )
    if include_minutes and minutes and minutes.strip():
        files[f"{root}/minutes.md"] = (
            f"# AI議事録（要確認）: {date} {channel_name}\n\n"
            "> 生文字起こしから自動生成した詳細議事録です。未確認のdraftです。\n\n"
            f"{minutes.strip()}\n"
        )

    manifest = {
        "schemaVersion": 1,
        "source": "yagapon-discord-voice",
        "status": "draft",
        "festival": festival,
        "sessionId": session_id,
        "guildId": str(guild_id),
        "channelName": channel_name,
        "mode": mode,
        "startedAt": started_at.isoformat(),
        "transcript": {
            "path": "transcript.md",
            "sha256": _sha256(transcript_document),
            "speakerIdentity": "anonymous-labels",
            "humanReviewed": False,
        },
        "summary": {"path": "summary.md", "humanReviewed": False} if summary else None,
        "minutes": (
            {"path": "minutes.md", "humanReviewed": False}
            if include_minutes and minutes
            else None
        ),
        "rawAudio": {"published": False, "retention": "deleted-after-durable-transcript"},
    }
    files[f"{root}/manifest.json"] = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    return root, files, manifest


def _delivery_payload(
    *,
    status: str,
    bundle_id: str,
    manifest: dict,
    repository_root: str,
    branch: str,
    title: str,
    body: str,
    **extra,
) -> dict:
    return {
        "status": status,
        "bundleId": bundle_id,
        # Keep the legacy key so already deployed operational tooling can read
        # meeting deliveries while generic knowledge sources are introduced.
        "sessionId": bundle_id,
        "manifest": manifest,
        "repositoryRoot": repository_root,
        "branch": branch,
        "title": title,
        "body": body,
        "updatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **extra,
    }


def _write_outbox(
    bundle_id: str,
    files: dict[str, str],
    manifest: dict,
    *,
    repository_root: str,
    branch: str,
    title: str,
    body: str,
) -> Path:
    bundle_path = PurePosixPath(bundle_id)
    if bundle_path.is_absolute() or len(bundle_path.parts) != 1 or bundle_id in {".", ".."}:
        raise ValueError("Invalid Knowledge bundle ID")
    for repository_path in files:
        path = PurePosixPath(repository_path)
        if path.is_absolute() or ".." in path.parts or not path.parts:
            raise ValueError(f"Invalid Knowledge repository path: {repository_path}")
    root = Path(os.environ.get("YAGAPON_KNOWLEDGE_OUTBOX", "/data/knowledge-outbox")) / bundle_id
    root.mkdir(parents=True, exist_ok=True)
    for repository_path, content in files.items():
        destination = root / repository_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        temporary.write_text(content, encoding="utf-8")
        os.replace(temporary, destination)
    delivery = root / "delivery.json"
    delivery.write_text(
        json.dumps(
            _delivery_payload(
                status="pending",
                bundle_id=bundle_id,
                manifest=manifest,
                repository_root=repository_root,
                branch=branch,
                title=title,
                body=body,
            ),
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return root


def _write_delivery(outbox_dir: Path, payload: dict) -> None:
    destination = outbox_dir / "delivery.json"
    temporary = destination.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, destination)


def _outbox_files(outbox_dir: Path, repository_root: str) -> dict[str, str]:
    path = PurePosixPath(repository_root)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError("Invalid Knowledge repository root")
    root = outbox_dir / repository_root
    if not root.is_dir():
        raise RuntimeError(f"Knowledge bundle files are missing: {repository_root}")
    return {
        path.relative_to(outbox_dir).as_posix(): path.read_text(encoding="utf-8")
        for path in root.rglob("*")
        if path.is_file()
        and not path.name.startswith(".")
        and not path.name.endswith((".tmp", ".bak", "~"))
    }


async def publish_knowledge_bundle(
    *,
    bundle_id: str,
    repository_root: str,
    files: dict[str, str],
    manifest: dict,
    branch: str,
    title: str,
    body: str,
) -> KnowledgePublishResult:
    """Persist a review bundle and submit a Draft PR when configured.

    The durable outbox is authoritative for delivery. A GitHub outage or a
    missing service credential must not lose a submitted source.
    """
    outbox_dir = await asyncio.to_thread(
        _write_outbox,
        bundle_id,
        files,
        manifest,
        repository_root=repository_root,
        branch=branch,
        title=title,
        body=body,
    )
    sensitive_labels = _sensitive_labels(files)
    if sensitive_labels:
        payload = _delivery_payload(
            status="blocked-sensitive-content",
            bundle_id=bundle_id,
            manifest=manifest,
            repository_root=repository_root,
            branch=branch,
            title=title,
            body=body,
            detectedPatterns=sensitive_labels,
        )
        await asyncio.to_thread(_write_delivery, outbox_dir, payload)
        log.error(
            "Knowledge publication blocked by sensitive-content scan bundle=%s patterns=%s",
            bundle_id,
            ",".join(sensitive_labels),
        )
        return KnowledgePublishResult(
            session_id=bundle_id,
            outbox_dir=outbox_dir,
            status="blocked-sensitive-content",
        )

    token = os.environ.get("YAGAPON_KNOWLEDGE_GITHUB_TOKEN", "").strip()
    if not token:
        log.warning("Knowledge GitHub token is not configured; bundle retained at %s", outbox_dir)
        return KnowledgePublishResult(session_id=bundle_id, outbox_dir=outbox_dir)

    repository = os.environ.get("YAGAPON_KNOWLEDGE_REPOSITORY", DEFAULT_REPOSITORY)
    base_branch = os.environ.get("YAGAPON_KNOWLEDGE_BASE_BRANCH", "main")
    publisher = GitHubKnowledgePublisher(token, repository, base_branch=base_branch)
    try:
        url = await asyncio.to_thread(
            publisher.publish,
            branch=branch,
            title=title,
            body=body,
            files=files,
        )
    except Exception as exc:
        payload = _delivery_payload(
            status="failed",
            bundle_id=bundle_id,
            manifest=manifest,
            repository_root=repository_root,
            branch=branch,
            title=title,
            body=body,
            errorType=type(exc).__name__,
        )
        await asyncio.to_thread(_write_delivery, outbox_dir, payload)
        log.exception("Knowledge Draft PR publication failed; bundle remains at %s", outbox_dir)
        return KnowledgePublishResult(
            session_id=bundle_id,
            outbox_dir=outbox_dir,
            status="failed",
        )

    payload = _delivery_payload(
        status="published",
        bundle_id=bundle_id,
        manifest=manifest,
        repository_root=repository_root,
        branch=branch,
        title=title,
        body=body,
        repository=repository,
        pullRequestUrl=url,
    )
    await asyncio.to_thread(_write_delivery, outbox_dir, payload)
    return KnowledgePublishResult(
        session_id=bundle_id,
        outbox_dir=outbox_dir,
        pull_request_url=url,
        status="published",
    )


def list_knowledge_deliveries(*, guild_id: int | None = None, limit: int = 10) -> list[dict]:
    """Return recent privacy-safe delivery metadata for operator commands."""
    root = Path(os.environ.get("YAGAPON_KNOWLEDGE_OUTBOX", "/data/knowledge-outbox"))
    if not root.is_dir():
        return []
    deliveries = []
    for path in root.glob("*/delivery.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        manifest = payload.get("manifest") or {}
        manifest_guild = manifest.get("guildId")
        if guild_id is not None and str(manifest_guild or "") != str(guild_id):
            continue
        deliveries.append(payload)
    deliveries.sort(key=lambda item: str(item.get("updatedAt") or ""), reverse=True)
    return deliveries[: max(1, min(limit, 50))]


def get_knowledge_delivery(bundle_id: str) -> dict | None:
    """Read one delivery by its safe ID for authorization and retry."""
    if not re.fullmatch(r"[0-9A-Za-z._-]{1,160}", bundle_id):
        return None
    root = Path(os.environ.get("YAGAPON_KNOWLEDGE_OUTBOX", "/data/knowledge-outbox"))
    path = root / bundle_id / "delivery.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


async def retry_knowledge_delivery(bundle_id: str) -> KnowledgePublishResult:
    """Retry a durable pending/failed bundle without regenerating its content."""
    if not re.fullmatch(r"[0-9A-Za-z._-]{1,160}", bundle_id):
        raise ValueError("Invalid Knowledge bundle ID")
    root = Path(os.environ.get("YAGAPON_KNOWLEDGE_OUTBOX", "/data/knowledge-outbox"))
    outbox_dir = root / bundle_id
    payload = await asyncio.to_thread(get_knowledge_delivery, bundle_id)
    if payload is None:
        raise FileNotFoundError(bundle_id)
    if payload.get("status") == "blocked-sensitive-content":
        raise RuntimeError("Sensitive content must be reviewed before retrying")
    required = ("repositoryRoot", "branch", "title", "body", "manifest")
    if any(key not in payload for key in required):
        raise RuntimeError("This legacy bundle does not contain retry metadata")
    files = await asyncio.to_thread(_outbox_files, outbox_dir, payload["repositoryRoot"])
    return await publish_knowledge_bundle(
        bundle_id=bundle_id,
        repository_root=payload["repositoryRoot"],
        files=files,
        manifest=payload["manifest"],
        branch=payload["branch"],
        title=payload["title"],
        body=payload["body"],
    )


async def publish_meeting_knowledge(
    *,
    session_id: str,
    guild_id: int,
    channel_name: str,
    mode: str,
    transcript: str,
    summary: str | None = None,
    minutes: str | None = None,
) -> KnowledgePublishResult | None:
    """Persist a bundle locally, then publish it to a Draft PR when configured."""
    if not transcript.strip():
        return None
    include_minutes = os.environ.get("YAGAPON_KNOWLEDGE_INCLUDE_MINUTES", "false").lower() == "true"
    repository_root, files, manifest = build_meeting_bundle(
        session_id=session_id,
        guild_id=guild_id,
        channel_name=channel_name,
        mode=mode,
        transcript=transcript,
        summary=summary,
        minutes=minutes,
        include_minutes=include_minutes,
    )
    branch = f"yagapon/meeting-{session_id.lower()}"
    title = f"source: {manifest['startedAt'][:10]} {manifest['channelName']}の文字起こし"
    body = (
        "おしゃべりやがぽんが自動作成した会議資料です。\n\n"
        f"- 保存先: `{repository_root}`\n"
        "- 生文字起こし: 必須\n"
        f"- AI要約: {'あり' if summary else 'なし'}\n"
        f"- AI議事録: {'あり' if include_minutes and minutes else 'なし'}\n\n"
        "話者、固有名詞、決定事項、担当、期限、公開範囲を確認してからReadyにしてください。"
    )
    return await publish_knowledge_bundle(
        bundle_id=session_id,
        repository_root=repository_root,
        files=files,
        manifest=manifest,
        branch=branch,
        title=title,
        body=body,
    )
