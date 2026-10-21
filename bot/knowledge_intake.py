"""Build review-gated Knowledge bundles from Discord and Google Drive sources."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import parse_qs, urlparse, urlunparse

import yaml

from bot.knowledge_publish import JST, festival_number
from knowledge_sync.draft import StructuredDraft, render_source_draft
from knowledge_sync.drive import DriveDocument

DISCORD_MESSAGE_URL = re.compile(
    r"^https?://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/channels/"
    r"(?P<guild_id>\d+)/(?P<channel_id>\d+)/(?P<message_id>\d+)(?:[/?#].*)?$"
)
DRIVE_PATH_PATTERNS = (
    re.compile(r"/(?:document|spreadsheets|presentation)/d/([0-9A-Za-z_-]+)"),
    re.compile(r"/file/d/([0-9A-Za-z_-]+)"),
)


@dataclass(frozen=True)
class DiscordMessageSource:
    message_id: int
    author_id: int
    author_name: str
    content: str
    timestamp: datetime
    jump_url: str
    attachment_urls: list[str] = field(default_factory=list)


def parse_discord_message_url(value: str) -> tuple[int, int, int]:
    match = DISCORD_MESSAGE_URL.fullmatch(value.strip())
    if not match:
        raise ValueError("DiscordメッセージURLの形式が正しくありません")
    return tuple(int(match.group(name)) for name in ("guild_id", "channel_id", "message_id"))


def parse_drive_file_url(value: str) -> str:
    parsed = urlparse(value.strip())
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {
        "drive.google.com",
        "docs.google.com",
    }:
        raise ValueError("Google DriveまたはGoogle DocsのURLを指定してください")
    for pattern in DRIVE_PATH_PATTERNS:
        match = pattern.search(parsed.path)
        if match:
            return match.group(1)
    query_id = parse_qs(parsed.query).get("id", [""])[0]
    if re.fullmatch(r"[0-9A-Za-z_-]+", query_id):
        return query_id
    raise ValueError("URLからGoogle DriveのファイルIDを取得できません")


def _safe_url(value: str) -> str:
    """Remove signed query parameters before a source URL is committed."""
    parsed = urlparse(value)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))


def _source_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _discord_document(
    *,
    title: str,
    guild_id: int,
    channel_id: int,
    channel_name: str,
    selected_message_id: int,
    requested_by: int,
    messages: list[DiscordMessageSource],
) -> tuple[str, int, str]:
    selected = next(
        (message for message in messages if message.message_id == selected_message_id),
        None,
    )
    if selected is None:
        raise ValueError("登録対象のメッセージが取得範囲に含まれていません")
    festival = festival_number(selected.timestamp)
    date = selected.timestamp.astimezone(JST).date().isoformat()
    metadata = {
        "title": title,
        "type": "source",
        "festival": festival,
        "date": date,
        "last_updated": datetime.now(JST).date().isoformat(),
        "status": "draft",
        "contributors": [],
        "related_roles": [],
        "systems": [],
        "areas": [],
        "tags": ["discord", "auto-imported", "needs-human-review"],
        "related": [],
        "sources": [
            {
                "kind": "discord",
                "ref": selected.jump_url,
                "locator": f"message_id:{selected_message_id}",
                "timestamp": selected.timestamp.isoformat(),
                "channel": channel_name,
                "message_id": str(selected_message_id),
                "accessed_on": datetime.now(JST).date().isoformat(),
            }
        ],
    }
    front_matter = yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False).strip()
    lines = [
        f"---\n{front_matter}\n---",
        f"# {title}",
        "",
        "> Discord上の選択メッセージと周辺会話を自動取得した確認待ち資料です。",
        "> 発言はその時点の会話であり、最終決定・現在の運用・実装完了を示すとは限りません。",
        "",
        "## 取得情報",
        "",
        f"- Guild ID: `{guild_id}`",
        f"- Channel: `#{channel_name}` (`{channel_id}`)",
        f"- 選択Message ID: `{selected_message_id}`",
        f"- 登録申請者Discord ID: `{requested_by}`",
        f"- 取得メッセージ数: {len(messages)}",
        "",
        "## 会話抜粋（未確認）",
        "",
    ]
    for message in sorted(messages, key=lambda item: (item.timestamp, item.message_id)):
        marker = " **[登録対象]**" if message.message_id == selected_message_id else ""
        lines.extend(
            [
                f"### {message.timestamp.isoformat()} — {message.author_name}{marker}",
                "",
                f"- Author ID: `{message.author_id}`",
                f"- [Discord原文]({message.jump_url})",
                "",
                message.content.strip() or "（本文なし）",
                "",
            ]
        )
        if message.attachment_urls:
            lines.append("添付:")
            lines.extend(f"- {_safe_url(url)}" for url in message.attachment_urls)
            lines.append("")
    lines.extend(
        [
            "## 人による確認事項",
            "",
            "- 登録対象と前後関係が適切か",
            "- 最終決定と単なる提案・推測が区別されているか",
            "- 個人情報・認証情報・非公開情報をKnowledgeへ保管してよいか",
            "- 正式な記事へ整理する場合、既存の正本と重複しないか",
            "",
        ]
    )
    return "\n".join(lines), festival, date


def build_discord_intake_bundle(
    *,
    guild_id: int,
    channel_id: int,
    channel_name: str,
    selected_message_id: int,
    requested_by: int,
    messages: list[DiscordMessageSource],
    title: str | None = None,
) -> tuple[str, str, dict[str, str], dict, str, str, str]:
    """Return publish arguments for a Discord source registration."""
    selected = next(
        (message for message in messages if message.message_id == selected_message_id),
        None,
    )
    if selected is None:
        raise ValueError("登録対象のメッセージが取得範囲に含まれていません")
    selected_date = selected.timestamp.astimezone(JST).date()
    resolved_title = (title or "").strip() or f"{channel_name} Discord会話 {selected_date}"
    document, festival, date = _discord_document(
        title=resolved_title,
        guild_id=guild_id,
        channel_id=channel_id,
        channel_name=channel_name,
        selected_message_id=selected_message_id,
        requested_by=requested_by,
        messages=messages,
    )
    digest = _source_hash(document)[:12]
    bundle_id = f"discord-{selected_message_id}-{digest}"
    repository_root = f"sources/discord/inbox/{festival}th/{bundle_id}"
    manifest = {
        "schemaVersion": 1,
        "source": "yagapon-discord-message",
        "status": "draft",
        "festival": festival,
        "date": date,
        "guildId": str(guild_id),
        "channelId": str(channel_id),
        "selectedMessageId": str(selected_message_id),
        "requestedBy": str(requested_by),
        "messageCount": len(messages),
        "sourceUrl": selected.jump_url,
        "humanReviewed": False,
        "contentSha256": _source_hash(document),
    }
    files = {
        f"{repository_root}/source.md": document,
        f"{repository_root}/manifest.json": json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
    }
    branch = f"yagapon/intake-{bundle_id}"
    pr_title = f"source: Discord「{resolved_title}」を登録"
    pr_body = (
        "Discordから申請された確認待ち資料です。\n\n"
        f"- 保存先: `{repository_root}`\n"
        f"- 登録対象: {selected.jump_url}\n"
        f"- 周辺メッセージ: {len(messages)}件\n\n"
        "公開範囲、個人情報、発言の前後関係、決定事項の確度を確認してからReadyにしてください。"
    )
    return bundle_id, repository_root, files, manifest, branch, pr_title, pr_body


def build_drive_intake_bundle(
    *,
    guild_id: int,
    requested_by: int,
    document: DriveDocument,
    source_text: str,
    structured: StructuredDraft,
    redaction_count: int,
    access_mode: str | None = None,
) -> tuple[str, str, dict[str, str], dict, str, str, str]:
    """Return publish arguments for a Google Drive source registration."""
    rendered = render_source_draft(document, source_text, structured, redaction_count)
    digest = _source_hash(rendered)[:12]
    safe_id = re.sub(r"[^0-9A-Za-z_-]", "", document.file_id)
    if not safe_id:
        raise ValueError("Drive file ID cannot be converted to a safe identifier")
    bundle_id = f"drive-{safe_id}-{digest}"
    repository_root = f"sources/drive/inbox/{bundle_id}"
    manifest = {
        "schemaVersion": 1,
        "source": "yagapon-google-drive",
        "status": "draft",
        "festival": structured.suggested_festival,
        "guildId": str(guild_id),
        "driveFileId": document.file_id,
        "sourceUrl": document.web_view_link,
        "modifiedTime": document.modified_time,
        "requestedBy": str(requested_by),
        "humanReviewed": False,
        "redactionCount": redaction_count,
        "driveAccessMode": access_mode,
        "sourceSha256": _source_hash(source_text),
    }
    files = {
        f"{repository_root}/source.md": rendered,
        f"{repository_root}/manifest.json": json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
    }
    branch = f"yagapon/intake-{bundle_id}"
    pr_title = f"source: Drive「{document.name}」を登録"
    pr_body = (
        "Google Driveから申請された確認待ち資料です。\n\n"
        f"- 保存先: `{repository_root}`\n"
        f"- 原本: {document.web_view_link}\n"
        f"- 自動除外した認証情報: {redaction_count}件\n\n"
        "原本との差分、公開範囲、個人情報、年度、分類、既存正本との重複を確認してからReadyにしてください。"
    )
    return bundle_id, repository_root, files, manifest, branch, pr_title, pr_body
