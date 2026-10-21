from datetime import datetime, timezone

import pytest
import yaml

from bot.knowledge_intake import (
    DiscordMessageSource,
    build_discord_intake_bundle,
    build_drive_intake_bundle,
    parse_discord_message_url,
    parse_drive_file_url,
)
from knowledge_sync.draft import StructuredDraft
from knowledge_sync.drive import DriveDocument


def test_parse_discord_message_url_accepts_supported_hosts():
    assert parse_discord_message_url(
        "https://discord.com/channels/123/456/789"
    ) == (123, 456, 789)
    assert parse_discord_message_url(
        "https://canary.discordapp.com/channels/123/456/789?x=1"
    ) == (123, 456, 789)


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/channels/123/456/789",
        "https://discord.com/channels/@me/456/789",
        "https://discord.com/channels/123/456",
    ],
)
def test_parse_discord_message_url_rejects_untrusted_or_incomplete_urls(url):
    with pytest.raises(ValueError):
        parse_discord_message_url(url)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://docs.google.com/document/d/doc-id/edit", "doc-id"),
        ("https://docs.google.com/spreadsheets/d/sheet_id/edit", "sheet_id"),
        ("https://drive.google.com/file/d/file-id/view", "file-id"),
        ("https://drive.google.com/open?id=open-id", "open-id"),
    ],
)
def test_parse_drive_file_url(url, expected):
    assert parse_drive_file_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/file/d/id/view",
        "https://drive.google.com/drive/folders/folder-id",
        "javascript:alert(1)",
    ],
)
def test_parse_drive_file_url_rejects_non_files(url):
    with pytest.raises(ValueError):
        parse_drive_file_url(url)


def test_discord_bundle_is_draft_with_selected_message_and_traceable_source():
    messages = [
        DiscordMessageSource(
            message_id=10,
            author_id=1,
            author_name="委員A",
            content="前提です",
            timestamp=datetime(2026, 9, 30, 14, 59, tzinfo=timezone.utc),
            jump_url="https://discord.com/channels/1/2/10",
        ),
        DiscordMessageSource(
            message_id=11,
            author_id=2,
            author_name="委員B",
            content="この方針で進めます",
            timestamp=datetime(2026, 9, 30, 15, 0, tzinfo=timezone.utc),
            jump_url="https://discord.com/channels/1/2/11",
            attachment_urls=["https://cdn.discordapp.com/a/file.pdf?ex=signed"],
        ),
    ]

    bundle_id, root, files, manifest, branch, title, body = build_discord_intake_bundle(
        guild_id=1,
        channel_id=2,
        channel_name="仕事に関する雑談",
        selected_message_id=11,
        requested_by=3,
        messages=messages,
        title="次年度方針",
    )

    source = files[f"{root}/source.md"]
    front_matter = yaml.safe_load(source[4:].split("\n---\n", 1)[0])
    assert bundle_id.startswith("discord-11-")
    assert root.startswith("sources/discord/inbox/28th/")
    assert front_matter["status"] == "draft"
    assert front_matter["festival"] == 28
    assert front_matter["sources"][0]["message_id"] == "11"
    assert "**[登録対象]**" in source
    assert "?ex=signed" not in source
    assert manifest["guildId"] == "1"
    assert manifest["messageCount"] == 2
    assert branch.startswith("yagapon/intake-discord-11-")
    assert "次年度方針" in title
    assert "周辺メッセージ: 2件" in body


def test_discord_default_title_uses_jst_date():
    selected = DiscordMessageSource(
        message_id=11,
        author_id=2,
        author_name="委員B",
        content="朝の発言",
        timestamp=datetime(2026, 10, 20, 16, 0, tzinfo=timezone.utc),
        jump_url="https://discord.com/channels/1/2/11",
    )

    _, root, files, _, _, title, _ = build_discord_intake_bundle(
        guild_id=1,
        channel_id=2,
        channel_name="一般",
        selected_message_id=11,
        requested_by=3,
        messages=[selected],
    )

    assert "2026-10-21" in title
    assert "2026-10-21" in files[f"{root}/source.md"]
    assert "2026-10-21T01:00:00+09:00" in files[f"{root}/source.md"]


def test_drive_bundle_keeps_original_as_reference_not_authoritative_content():
    document = DriveDocument(
        file_id="drive-file-id",
        name="運用資料",
        mime_type="application/vnd.google-apps.document",
        modified_time="2026-10-20T00:00:00Z",
        web_view_link="https://docs.google.com/document/d/drive-file-id/edit",
        parents=["parent"],
    )
    structured = StructuredDraft(
        summary="運用の候補をまとめた資料です。",
        facts=["運用案の記載がある。"],
        unknowns=["承認状況は不明。"],
        suggested_festival=28,
        suggested_knowledge_types=["years/28th/sources"],
    )

    bundle_id, root, files, manifest, branch, title, body = build_drive_intake_bundle(
        guild_id=1,
        requested_by=2,
        document=document,
        source_text="[認証情報を除外]",
        source_sha256="raw-source-sha256",
        structured=structured,
        redaction_count=1,
        access_mode="drive.readonly",
    )

    source = files[f"{root}/source.md"]
    front_matter = yaml.safe_load(source[4:].split("\n---\n", 1)[0])
    assert bundle_id.startswith("drive-drive-file-id-")
    assert root.startswith("sources/drive/inbox/")
    assert front_matter["status"] == "draft"
    assert manifest["redactionCount"] == 1
    assert manifest["driveAccessMode"] == "drive.readonly"
    assert manifest["sourceSha256"] == "raw-source-sha256"
    assert "secret-value" not in source
    assert branch.startswith("yagapon/intake-drive-")
    assert "運用資料" in title
    assert "自動除外した認証情報: 1件" in body
