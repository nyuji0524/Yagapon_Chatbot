"""Discord commands for review-gated Knowledge source registration."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os

import discord

from bot.authorization import require_guild_admin
from bot.gdrive import DOCS_SCOPE, DRIVE_FILE_SCOPE
from bot.google_credentials import GoogleCredentialScopeError
from bot.knowledge_intake import (
    DiscordMessageSource,
    build_discord_intake_bundle,
    build_drive_intake_bundle,
    parse_discord_message_url,
    parse_drive_file_url,
)
from bot.knowledge_publish import (
    KnowledgePublishResult,
    get_knowledge_delivery,
    list_knowledge_deliveries,
    publish_knowledge_bundle,
    retry_knowledge_delivery,
)
from knowledge_sync.draft import redact_credentials, structure_source
from knowledge_sync.drive import DriveReader

log = logging.getLogger("yagapon.commands.knowledge")


def _result_message(result: KnowledgePublishResult) -> str:
    if result.pull_request_url:
        return (
            "✅ Knowledgeの**Draft PR**を作成したぽん！\n"
            f"🗂️ [内容を確認する]({result.pull_request_url})\n"
            "原本・公開範囲・個人情報・年度・分類を確認し、問題なければReadyにしてレビューしてほしいぽん。"
        )
    if result.status == "blocked-sensitive-content":
        return (
            "🛑 認証情報らしき内容を検出したため、GitHubへの送信を止めたぽん。\n"
            f"申請ID: `{result.session_id}`\n"
            "サーバー上の候補を管理者が確認・修正するまで再送できないぽん。"
        )
    if result.status == "failed":
        return (
            "⚠️ 候補は安全に保存したけれど、Draft PRの作成に失敗したぽん。\n"
            f"申請ID: `{result.session_id}`\n"
            "接続を直したあと `/knowledge retry` で再送できるぽん。"
        )
    return (
        "📥 Knowledge候補を保存したぽん。GitHub送信用のサービス資格情報が未設定なので、"
        "まだDraft PRにはなっていないぽん。\n"
        f"申請ID: `{result.session_id}`\n"
        "設定後に `/knowledge retry` で再送できるぽん。"
    )


def _knowledge_guild_allowed(guild_id: int) -> bool:
    configured = os.environ.get("YAGAPON_KNOWLEDGE_ALLOWED_GUILD_IDS", "").strip()
    if not configured:
        configured = os.environ.get("YAGAPON_KNOWLEDGE_GUILD_ID", "").strip()
    if not configured:
        return False
    allowed = {value.strip() for value in configured.split(",") if value.strip()}
    return str(guild_id) in allowed


def _protected_channel(channel, *, parent=None, category=None) -> bool:
    channel_ids = {
        value.strip()
        for value in os.environ.get("YAGAPON_KNOWLEDGE_DENY_CHANNEL_IDS", "").split(",")
        if value.strip()
    }
    parent = parent or getattr(channel, "parent", None)
    category = (
        category
        or getattr(channel, "category", None)
        or getattr(parent, "category", None)
    )
    related = [
        channel,
        parent,
        category,
    ]
    related_ids = {
        str(value)
        for value in (
            getattr(channel, "id", None),
            getattr(channel, "parent_id", None),
            getattr(channel, "category_id", None),
            getattr(parent, "id", None),
            getattr(parent, "category_id", None),
            getattr(category, "id", None),
        )
        if value is not None
    }
    if related_ids & channel_ids:
        return True
    protected_terms = {
        value.strip().casefold()
        for value in os.environ.get("YAGAPON_KNOWLEDGE_DENY_CHANNEL_TERMS", "選考").split(",")
        if value.strip()
    }
    return any(
        term in str(getattr(item, "name", "")).casefold()
        for item in related
        if item
        for term in protected_terms
    )


async def _require_knowledge_guild(ctx) -> bool:
    if ctx.guild and _knowledge_guild_allowed(ctx.guild.id):
        return True
    await ctx.respond(
        "このサーバーはKnowledge登録先として許可されていないぽん。",
        ephemeral=True,
    )
    return False


async def _publish(bot, ctx, args) -> None:
    bundle_id, repository_root, files, manifest, branch, title, body = args
    result = await publish_knowledge_bundle(
        bundle_id=bundle_id,
        repository_root=repository_root,
        files=files,
        manifest=manifest,
        branch=branch,
        title=title,
        body=body,
    )
    if hasattr(bot, "operations"):
        bot.operations.record_event(
            "knowledge",
            "register_source",
            result.status,
            guild_id=ctx.guild_id,
            item_key=bundle_id,
            details={"source": manifest.get("source"), "pull_request": bool(result.pull_request_url)},
        )
    await ctx.followup.send(_result_message(result), ephemeral=True)


async def _resolve_channel(bot, guild, channel_id: int):
    channel = guild.get_channel(channel_id)
    if channel is None and hasattr(guild, "get_thread"):
        channel = guild.get_thread(channel_id)
    if channel is None:
        channel = await bot.fetch_channel(channel_id)
    return channel


async def _channel_lineage(bot, guild, channel):
    """Resolve a thread's parent/category even when they are not cached."""
    parent = getattr(channel, "parent", None)
    parent_id = getattr(channel, "parent_id", None)
    if parent is None and parent_id:
        parent = await _resolve_channel(bot, guild, parent_id)

    category = (
        getattr(channel, "category", None)
        or getattr(parent, "category", None)
    )
    category_id = (
        getattr(channel, "category_id", None)
        or getattr(parent, "category_id", None)
    )
    if category is None and category_id:
        category = await _resolve_channel(bot, guild, category_id)
    return parent, category


async def _discord_messages(channel, selected, limit: int) -> list[DiscordMessageSource]:
    fetched = []
    async for message in channel.history(
        limit=max(1, min(limit, 50)),
        around=selected,
    ):
        fetched.append(message)
    if all(message.id != selected.id for message in fetched):
        fetched.append(selected)
    unique = {message.id: message for message in fetched}
    result = []
    for message in sorted(unique.values(), key=lambda item: (item.created_at, item.id)):
        if not str(getattr(message, "content", "") or "").strip() and not getattr(
            message, "attachments", None
        ) and message.id != selected.id:
            continue
        author = getattr(message, "author", None)
        result.append(
            DiscordMessageSource(
                message_id=message.id,
                author_id=getattr(author, "id", 0),
                author_name=(
                    getattr(author, "display_name", None)
                    or getattr(author, "name", None)
                    or "不明"
                ),
                content=str(getattr(message, "content", "") or ""),
                timestamp=message.created_at,
                jump_url=message.jump_url,
                attachment_urls=[attachment.url for attachment in message.attachments],
            )
        )
    return result


def _read_drive_source(file_id: str):
    """Prefer full read-only access, then retain current drive.file compatibility."""
    credentials_path = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON") or None
    try:
        reader = DriveReader(credentials_path, "")
        document = reader.get_document(file_id)
        return document, reader.read_text(document), "drive.readonly"
    except GoogleCredentialScopeError:
        reader = DriveReader(
            credentials_path,
            "",
            scopes=[DRIVE_FILE_SCOPE, DOCS_SCOPE],
        )
        document = reader.get_document(file_id)
        return document, reader.read_text(document), "drive.file"


def register(bot):
    group = bot.create_group("knowledge", "正式ナレッジへの登録候補を管理するぽん")

    @group.command(name="discord", description="Discordメッセージを確認待ち資料として申請するぽん")
    @discord.default_permissions(administrator=True)
    @discord.option("message_url", description="登録したいDiscordメッセージのリンク")
    @discord.option("title", description="資料のタイトル（省略可）", required=False, default="")
    @discord.option(
        "context_messages",
        description="前後を含めて取得する最大メッセージ数（1〜50）",
        required=False,
        default=21,
    )
    async def knowledge_discord(
        ctx: discord.ApplicationContext,
        message_url: str,
        title: str = "",
        context_messages: int = 21,
    ):
        if not await require_guild_admin(ctx):
            return
        if not ctx.guild:
            await ctx.respond("サーバー内で実行してほしいぽん。", ephemeral=True)
            return
        if not await _require_knowledge_guild(ctx):
            return
        try:
            guild_id, channel_id, message_id = parse_discord_message_url(message_url)
        except ValueError as exc:
            await ctx.respond(f"❌ {exc}ぽん。", ephemeral=True)
            return
        if guild_id != ctx.guild.id:
            await ctx.respond("❌ このサーバーのメッセージだけ登録できるぽん。", ephemeral=True)
            return
        context_messages = max(1, min(int(context_messages), 50))
        await ctx.defer(ephemeral=True)
        try:
            channel = await _resolve_channel(bot, ctx.guild, channel_id)
            if getattr(getattr(channel, "guild", None), "id", None) != ctx.guild.id:
                await ctx.followup.send(
                    "❌ このサーバー外のチャンネルは登録できないぽん。",
                    ephemeral=True,
                )
                return
            permissions = channel.permissions_for(ctx.author)
            if not permissions.view_channel or not permissions.read_message_history:
                await ctx.followup.send(
                    "❌ 実行者が閲覧できないチャンネルは登録できないぽん。",
                    ephemeral=True,
                )
                return
            parent, category = await _channel_lineage(bot, ctx.guild, channel)
            parent_id = getattr(channel, "parent_id", None) or getattr(parent, "id", None)
            if bot.config.is_ignored(ctx.guild.id, channel_id) or (
                parent_id and bot.config.is_ignored(ctx.guild.id, parent_id)
            ):
                await ctx.followup.send(
                    "❌ `/ignore` 対象のチャンネルはKnowledgeにも登録しないぽん。",
                    ephemeral=True,
                )
                return
            if _protected_channel(channel, parent=parent, category=category):
                await ctx.followup.send(
                    "❌ このチャンネルは機微情報を扱うためKnowledge登録対象外だぽん。",
                    ephemeral=True,
                )
                return
            selected = await channel.fetch_message(message_id)
            messages = await _discord_messages(channel, selected, context_messages)
            args = build_discord_intake_bundle(
                guild_id=ctx.guild.id,
                channel_id=channel_id,
                channel_name=getattr(channel, "name", str(channel_id)),
                selected_message_id=message_id,
                requested_by=ctx.author.id,
                messages=messages,
                title=title,
            )
            await _publish(bot, ctx, args)
        except discord.Forbidden:
            await ctx.followup.send(
                "❌ やがぽんが対象チャンネルを読めないぽん。閲覧・履歴権限を確認してほしいぽん。",
                ephemeral=True,
            )
        except discord.NotFound:
            await ctx.followup.send("❌ 対象メッセージが見つからないぽん。", ephemeral=True)
        except Exception as exc:
            log.exception("Discord Knowledge registration failed")
            if hasattr(bot, "operations"):
                bot.operations.record_event(
                    "knowledge",
                    "register_source",
                    "failed",
                    guild_id=ctx.guild_id,
                    item_key=str(message_id),
                    details={"source": "discord", "error_type": type(exc).__name__},
                )
            await ctx.followup.send(
                f"❌ 登録候補の作成に失敗したぽん（`{type(exc).__name__}`）。",
                ephemeral=True,
            )

    @group.command(name="drive", description="Google Drive資料を確認待ち資料として申請するぽん")
    @discord.default_permissions(administrator=True)
    @discord.option("drive_url", description="Google Docs・Sheets・Slides・DriveファイルのURL")
    async def knowledge_drive(ctx: discord.ApplicationContext, drive_url: str):
        if not await require_guild_admin(ctx):
            return
        if not ctx.guild:
            await ctx.respond("サーバー内で実行してほしいぽん。", ephemeral=True)
            return
        if not await _require_knowledge_guild(ctx):
            return
        try:
            file_id = parse_drive_file_url(drive_url)
        except ValueError as exc:
            await ctx.respond(f"❌ {exc}ぽん。", ephemeral=True)
            return
        await ctx.defer(ephemeral=True)
        try:
            document, source_text, access_mode = await asyncio.to_thread(
                _read_drive_source,
                file_id,
            )
            if source_text is None:
                await ctx.followup.send(
                    "❌ この資料形式は直接テキスト化できないぽん。"
                    "Google Docs・Sheets・Slides、またはテキスト系ファイルを指定してほしいぽん。",
                    ephemeral=True,
                )
                return
            sanitized, redaction_count = redact_credentials(source_text)
            source_sha256 = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
            structured = await asyncio.to_thread(
                structure_source,
                sanitized,
                os.environ.get("YAGAPON_KNOWLEDGE_MODEL", "gemini-3.8-flash"),
            )
            args = build_drive_intake_bundle(
                guild_id=ctx.guild.id,
                requested_by=ctx.author.id,
                document=document,
                source_text=sanitized,
                source_sha256=source_sha256,
                structured=structured,
                redaction_count=redaction_count,
                access_mode=access_mode,
            )
            await _publish(bot, ctx, args)
        except Exception as exc:
            log.exception("Drive Knowledge registration failed")
            if hasattr(bot, "operations"):
                bot.operations.record_event(
                    "knowledge",
                    "register_source",
                    "failed",
                    guild_id=ctx.guild_id,
                    item_key=file_id,
                    details={"source": "drive", "error_type": type(exc).__name__},
                )
            await ctx.followup.send(
                "❌ Drive資料の取得または整理に失敗したぽん。"
                f"やがぽんに資料の閲覧権限があるか確認してほしいぽん（`{type(exc).__name__}`）。",
                ephemeral=True,
            )

    @group.command(name="status", description="最近のKnowledge登録申請を確認するぽん")
    @discord.default_permissions(administrator=True)
    async def knowledge_status(ctx: discord.ApplicationContext):
        if not await require_guild_admin(ctx):
            return
        if not await _require_knowledge_guild(ctx):
            return
        await ctx.defer(ephemeral=True)
        entries = await asyncio.to_thread(
            list_knowledge_deliveries,
            guild_id=ctx.guild_id,
            limit=10,
        )
        if not entries:
            await ctx.followup.send("このサーバーの登録申請はまだないぽん。", ephemeral=True)
            return
        icons = {
            "published": "✅",
            "pending": "📥",
            "failed": "⚠️",
            "blocked-sensitive-content": "🛑",
        }
        lines = ["**最近のKnowledge登録申請**"]
        for entry in entries:
            status = str(entry.get("status") or "unknown")
            bundle_id = entry.get("bundleId") or entry.get("sessionId") or "不明"
            source = (entry.get("manifest") or {}).get("source") or "unknown"
            line = f"{icons.get(status, '•')} `{bundle_id}` — {source} / {status}"
            if entry.get("pullRequestUrl"):
                line += f" — [Draft PR]({entry['pullRequestUrl']})"
            lines.append(line)
        await ctx.followup.send("\n".join(lines), ephemeral=True)

    @group.command(name="retry", description="保存済みのKnowledge登録申請を再送するぽん")
    @discord.default_permissions(administrator=True)
    @discord.option("bundle_id", description="statusに表示された申請ID")
    async def knowledge_retry(ctx: discord.ApplicationContext, bundle_id: str):
        if not await require_guild_admin(ctx):
            return
        if not await _require_knowledge_guild(ctx):
            return
        entry = await asyncio.to_thread(get_knowledge_delivery, bundle_id)
        if entry is None:
            await ctx.respond("❌ 指定された申請IDが見つからないぽん。", ephemeral=True)
            return
        manifest = (entry or {}).get("manifest") or {}
        if str(manifest.get("guildId") or "") != str(ctx.guild_id):
            await ctx.respond("❌ このサーバーの申請IDではないぽん。", ephemeral=True)
            return
        if entry.get("status") == "blocked-sensitive-content":
            await ctx.respond(
                "🛑 この申請は機密検知で停止中だぽん。サーバー上の候補から該当内容を除去し、"
                "管理者が確認するまで再送できないぽん。",
                ephemeral=True,
            )
            return
        await ctx.defer(ephemeral=True)
        try:
            result = await retry_knowledge_delivery(bundle_id)
        except Exception as exc:
            log.exception("Knowledge delivery retry failed")
            await ctx.followup.send(
                f"❌ 再送できなかったぽん（`{type(exc).__name__}`）。",
                ephemeral=True,
            )
            return
        await ctx.followup.send(_result_message(result), ephemeral=True)
