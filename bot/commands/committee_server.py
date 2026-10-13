"""28th committee Discord server bootstrap and persistent role controls."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable

import discord

from bot.authorization import require_guild_admin
from bot.key_management import (
    KEY_ACTION_CHANNEL,
    KEY_CATEGORY,
    KEY_NAMES,
    KEY_PANEL_MARKER,
    KEY_REPORT_CHANNEL,
    KEY_REQUEST_CHANNEL,
    KEY_STATUS_MARKER,
    KeyPanelView,
    RestrictedKeyApprovalView,
    key_panel_embed,
    key_status_embed,
)

log = logging.getLogger("yagapon.committee_server")

MEMBER_ROLE = "28th委員"
BUREAU_ROLES = (
    ("IT局", 0x58A6FF, "💻", "it"),
    ("総務局", 0x95A5A6, "📋", "soumu"),
    ("装飾局", 0xF1C40F, "🎨", "soushoku"),
    ("ステージ局", 0xE74C3C, "🎤", "stage"),
    ("屋外局", 0xE67E22, "⛺", "okugai"),
    ("室内局", 0x3498DB, "🏫", "shitsunai"),
    ("広報局", 0xFF69B4, "📣", "kouhou"),
    ("渉外局", 0x9B59B6, "🤝", "shougai"),
)
LIAISON_CHANNELS = (
    ("IT局", "総務局", "it総務連絡チャット"),
    ("IT局", "装飾局", "it装飾連絡チャット"),
    ("IT局", "ステージ局", "itステージ連絡チャット"),
    ("IT局", "屋外局", "it屋外連絡チャット"),
    ("IT局", "室内局", "it室内連絡チャット"),
    ("IT局", "広報局", "it広報連絡チャット"),
    ("IT局", "渉外局", "it渉外連絡チャット"),
    ("ステージ局", "広報局", "ステージ広報連絡チャット"),
    ("室内局", "広報局", "室内広報連絡チャット"),
)
GRADE_ROLES = ("1年", "2年", "3年")
POSITION_ROLES = ("一般委員", "企画長", "役員", "副局長", "局長", "執行部", "副委員長", "委員長")
ROLE_REQUEST_CHANNEL = "ロール申請"
ROLE_PANEL_CHANNEL = "ロール選択"
ROLE_PANEL_MARKER = "yagapon-role-panel-v1"
COMMITTEE_COLOR = 0xF2A900
ProgressReporter = Callable[[str], Awaitable[None]]


def _role_names(entries: Iterable[tuple[str, int, str, str]]) -> tuple[str, ...]:
    return tuple(entry[0] for entry in entries)


def _find_named(items, name: str):
    return discord.utils.get(items, name=name)


async def _replace_role_group(
    member: discord.Member,
    role: discord.Role,
    role_names: Iterable[str],
    *,
    reason: str = "本人によるロール選択",
) -> None:
    role_names = set(role_names)
    desired_roles = [
        current
        for current in member.roles
        if not getattr(current, "is_default", lambda: False)() and current.name not in role_names
    ]
    desired_roles.append(role)
    if {current.id for current in desired_roles} == {
        current.id for current in member.roles if not getattr(current, "is_default", lambda: False)()
    }:
        return
    await member.edit(roles=desired_roles, reason=reason)


async def _respond_ephemeral(interaction: discord.Interaction, message: str) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


def _named_channel(guild: discord.Guild, name: str):
    return _find_named(guild.channels, name)


def _channel_mention(guild: discord.Guild, name: str) -> str:
    channel = _named_channel(guild, name)
    return channel.mention if channel else f"#{name}"


def _role_panel_embed() -> discord.Embed:
    embed = discord.Embed(
        title="🎫 委員プロフィールを設定",
        description=(
            "下のメニューを上から順に設定してください。\n"
            "選び直すと、同じ種類の古いロールは自動で置き換わります。"
        ),
        color=discord.Color(COMMITTEE_COLOR),
    )
    embed.add_field(name="⚡ すぐ反映", value="**所属局・学年・サーバー表示名**", inline=True)
    embed.add_field(name="🛡️ 管理者が確認", value="**役職ロール**（申請後に承認）", inline=True)
    embed.add_field(
        name="困ったとき",
        value="選択肢が足りない、変更できない場合はIT局へ連絡してください。",
        inline=False,
    )
    embed.set_footer(text="個人情報ではなく、委員会で呼ばれたい名前を設定してください")
    return embed


def _liaison_guide_embed(first_bureau: str, second_bureau: str) -> discord.Embed:
    embed = discord.Embed(
        title=f"🔒 {first_bureau} × {second_bureau} 連絡チャット",
        description=(
            f"**{first_bureau}・{second_bureau}・執行部**だけが閲覧できる連絡場所です。\n"
            "相談、途中経過、確定事項を同じ場所に残してください。"
        ),
        color=discord.Color(COMMITTEE_COLOR),
    )
    embed.add_field(
        name="使い方",
        value="話題が長くなる場合はスレッドを作り、最初の投稿に目的・担当・期限を書きます。",
        inline=False,
    )
    embed.add_field(
        name="共有範囲",
        value="ほかの局からは表示されません。全体共有が必要になった内容は、適切な全体チャンネルにも転記してください。",
        inline=False,
    )
    embed.set_footer(text="この案内はピン留めされています。チャンネル上部のトピックからも用途を確認できます")
    return embed


def _intro_embed(guild: discord.Guild) -> discord.Embed:
    embed = discord.Embed(
        title="🏮 28th矢上祭実行委員会へようこそ",
        description="このサーバーは、連絡・相談・会議記録を迷わず探せるように整理されています。",
        color=discord.Color(COMMITTEE_COLOR),
    )
    embed.add_field(
        name="1️⃣ 最初にプロフィールを設定",
        value=f"{_channel_mention(guild, ROLE_PANEL_CHANNEL)} で **表示名 → 所属局 → 学年 → 役職** を設定します。",
        inline=False,
    )
    embed.add_field(
        name="2️⃣ 連絡と相談を使い分ける",
        value=(
            f"確定事項は {_channel_mention(guild, 'お知らせ')}、日常連絡は "
            f"{_channel_mention(guild, '全体チャット')}、案件ごとの相談はフォーラムへ。"
        ),
        inline=False,
    )
    embed.add_field(
        name="3️⃣ 長い話題はスレッドへ",
        value="1つの話題につき1スレッド。決まったことは先頭メッセージへ追記してください。",
        inline=False,
    )
    embed.add_field(
        name="4️⃣ 会議はやがぽんで記録",
        value=(
            f"VC参加後に `/join`、終了時に `/leave`。成果物は "
            f"{_channel_mention(guild, '議事録')} にまとまります。"
        ),
        inline=False,
    )
    embed.add_field(
        name="5️⃣ 部室・倉庫の鍵を記録",
        value=(
            f"{_channel_mention(guild, KEY_ACTION_CHANNEL)} で鍵を選び、"
            "**あけ・しめ・さしかえ** をボタンで記録します。"
        ),
        inline=False,
    )
    embed.set_footer(text="機密情報・個人情報は、必要なメンバーだけが見られるチャンネルへ")
    return embed


def _bot_guide_embed(guild: discord.Guild) -> discord.Embed:
    embed = discord.Embed(
        title="🤖 おしゃべりやがぽん ガイド",
        description="検索・会議記録・委員プロフィールの設定をDiscord内で完結できます。",
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name="💬 質問・検索",
        value=f"{_channel_mention(guild, 'やがぽん')} でやがぽんをメンションして質問",
        inline=False,
    )
    embed.add_field(
        name="🎙️ 会議を記録",
        value="`/join` → 会議 → `/leave`\n話者精度を上げる場合は `/voiceprint register`",
        inline=True,
    )
    embed.add_field(
        name="🧭 自分の設定",
        value=f"{_channel_mention(guild, ROLE_PANEL_CHANNEL)} で表示名・局・学年・役職を設定",
        inline=True,
    )
    embed.add_field(
        name="🔑 鍵管理",
        value=f"{_channel_mention(guild, KEY_ACTION_CHANNEL)} で鍵と操作を選択",
        inline=True,
    )
    embed.add_field(
        name="🛠️ 管理・診断",
        value="`/guide` 使い方　`/status` 設定確認　`/backfill` 過去ログ取込み",
        inline=False,
    )
    embed.set_footer(text="会議資料はVCチャットへ貼ると、議事録と一緒に追いやすくなります")
    return embed


def _nickname_block_reason(guild: discord.Guild, member: discord.Member) -> str | None:
    if member.id == guild.owner_id:
        return "サーバー所有者の表示名はBotから変更できないぽん。プロフィールから手動で変更してほしいぽん。"
    me = guild.me
    if not me or not me.guild_permissions.manage_nicknames:
        return "やがぽんに「ニックネームの管理」権限が足りないぽん。管理者に連絡してほしいぽん。"
    if member.top_role >= me.top_role:
        return "ロール順位の都合で表示名を変更できないぽん。管理者に連絡してほしいぽん。"
    return None


class CommitteeGuideView(discord.ui.View):
    def __init__(self, guild: discord.Guild):
        # Link buttons are resolved by Discord and do not need a running timer.
        super().__init__(timeout=None)
        for label, emoji, channel_name in (
            ("プロフィール設定", "🎫", ROLE_PANEL_CHANNEL),
            ("やがぽんに質問", "💬", "やがぽん"),
            ("議事録を見る", "📝", "議事録"),
        ):
            channel = _named_channel(guild, channel_name)
            if channel:
                self.add_item(
                    discord.ui.Button(
                        label=label,
                        emoji=emoji,
                        style=discord.ButtonStyle.link,
                        url=f"https://discord.com/channels/{guild.id}/{channel.id}",
                    )
                )


class BureauSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(
            placeholder="所属局を選択",
            min_values=1,
            max_values=1,
            row=0,
            custom_id="yagapon:committee:bureau:v1",
            options=[
                discord.SelectOption(label=name, value=name, emoji=emoji)
                for name, _color, emoji, _slug in BUREAU_ROLES
            ],
        )

    async def callback(self, interaction: discord.Interaction):
        guild = interaction.guild
        member = interaction.user
        role = _find_named(guild.roles, self.values[0]) if guild else None
        if not guild or not isinstance(member, discord.Member) or role is None:
            await _respond_ephemeral(interaction, "対象ロールが見つからないぽん。管理者に再セットアップを依頼してほしいぽん。")
            return
        try:
            await _replace_role_group(member, role, _role_names(BUREAU_ROLES))
        except discord.Forbidden:
            await _respond_ephemeral(interaction, "ロールを付ける権限が足りないぽん。やがぽんのロールを上に移動してほしいぽん。")
            return
        await _respond_ephemeral(interaction, f"✅ 所属局を **{role.name}** に設定したぽん！")


class GradeSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(
            placeholder="学年を選択",
            min_values=1,
            max_values=1,
            row=1,
            custom_id="yagapon:committee:grade:v1",
            options=[discord.SelectOption(label=name, value=name, emoji=f"{index}️⃣") for index, name in enumerate(GRADE_ROLES, 1)],
        )

    async def callback(self, interaction: discord.Interaction):
        guild = interaction.guild
        member = interaction.user
        role = _find_named(guild.roles, self.values[0]) if guild else None
        if not guild or not isinstance(member, discord.Member) or role is None:
            await _respond_ephemeral(interaction, "対象ロールが見つからないぽん。管理者に再セットアップを依頼してほしいぽん。")
            return
        try:
            await _replace_role_group(member, role, GRADE_ROLES)
        except discord.Forbidden:
            await _respond_ephemeral(interaction, "ロールを付ける権限が足りないぽん。やがぽんのロールを上に移動してほしいぽん。")
            return
        await _respond_ephemeral(interaction, f"✅ 学年を **{role.name}** に設定したぽん！")


class PositionSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(
            placeholder="役職を申請（管理者承認制）",
            min_values=1,
            max_values=1,
            row=2,
            custom_id="yagapon:committee:position:v1",
            options=[discord.SelectOption(label=name, value=name) for name in POSITION_ROLES],
        )

    async def callback(self, interaction: discord.Interaction):
        guild = interaction.guild
        member = interaction.user
        role = _find_named(guild.roles, self.values[0]) if guild else None
        request_channel = _find_named(guild.text_channels, ROLE_REQUEST_CHANNEL) if guild else None
        if not guild or not isinstance(member, discord.Member) or role is None or request_channel is None:
            await _respond_ephemeral(interaction, "役職申請の設定が見つからないぽん。管理者に再セットアップを依頼してほしいぽん。")
            return
        if role in member.roles:
            await _respond_ephemeral(interaction, f"**{role.name}** は既に付いているぽん！")
            return
        embed = discord.Embed(
            title="役職ロール申請",
            description=f"{member.mention} が **{role.name}** を申請しています。",
            color=discord.Color.gold(),
        )
        embed.add_field(name="申請者ID", value=str(member.id), inline=True)
        embed.add_field(name="役職ロールID", value=str(role.id), inline=True)
        embed.set_footer(text="局・学年は本人選択、役職だけ管理者承認です")
        await request_channel.send(embed=embed, view=RoleApprovalView(), silent=True)
        await _respond_ephemeral(interaction, f"📨 **{role.name}** を申請したぽん。管理者の承認を待ってねぽん！")


class NicknameModal(discord.ui.Modal):
    def __init__(self):
        super().__init__(
            title="サーバー表示名を設定",
            custom_id="yagapon:committee:nickname:modal:v1",
        )
        self.add_item(
            discord.ui.InputText(
                label="委員会で呼ばれたい名前",
                placeholder="例：山田 太郎 / やまだ",
                min_length=1,
                max_length=32,
                custom_id="display_name",
            )
        )

    async def callback(self, interaction: discord.Interaction):
        guild = interaction.guild
        member = interaction.user
        nickname = self.children[0].value.strip()
        if not guild or not isinstance(member, discord.Member):
            await _respond_ephemeral(interaction, "サーバー内で実行してほしいぽん。")
            return
        if not nickname:
            await _respond_ephemeral(interaction, "表示名を入力してほしいぽん。")
            return
        block_reason = _nickname_block_reason(guild, member)
        if block_reason:
            await _respond_ephemeral(interaction, block_reason)
            return
        try:
            await member.edit(nick=nickname, reason="本人によるサーバー表示名設定")
        except discord.Forbidden:
            await _respond_ephemeral(interaction, "表示名を変更する権限が足りないぽん。管理者に連絡してほしいぽん。")
            return
        except discord.HTTPException:
            log.exception("Nickname update failed guild=%s member=%s", guild.id, member.id)
            await _respond_ephemeral(interaction, "Discord APIとの通信に失敗したぽん。少し待ってからもう一度試してほしいぽん。")
            return
        embed = discord.Embed(
            title="✅ 表示名を更新しました",
            description=f"このサーバーでは **{nickname}** と表示されます。",
            color=discord.Color.green(),
        )
        embed.set_footer(text="Discordアカウント本体の名前は変わりません")
        await interaction.response.send_message(embed=embed, ephemeral=True)


class SelfRolePanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(BureauSelect())
        self.add_item(GradeSelect())
        self.add_item(PositionSelect())

    @discord.ui.button(
        label="サーバー表示名を設定",
        emoji="✏️",
        style=discord.ButtonStyle.primary,
        row=3,
        custom_id="yagapon:committee:nickname:v1",
    )
    async def nickname(self, _button: discord.ui.Button, interaction: discord.Interaction):
        await interaction.response.send_modal(NicknameModal())


def _embed_field_id(embed: discord.Embed, field_name: str) -> int | None:
    for field in embed.fields:
        if field.name == field_name:
            try:
                return int(field.value)
            except (TypeError, ValueError):
                return None
    return None


async def _handle_position_request(interaction: discord.Interaction, *, approve: bool) -> None:
    permissions = getattr(interaction.user, "guild_permissions", None)
    if not permissions or not permissions.manage_roles:
        await _respond_ephemeral(interaction, "役職申請を処理できるのはロール管理者だけだぽん。")
        return
    message = interaction.message
    embed = message.embeds[0] if message and message.embeds else None
    guild = interaction.guild
    member_id = _embed_field_id(embed, "申請者ID") if embed else None
    role_id = _embed_field_id(embed, "役職ロールID") if embed else None
    member = guild.get_member(member_id) if guild and member_id else None
    if member is None and guild and member_id:
        try:
            member = await guild.fetch_member(member_id)
        except (discord.NotFound, discord.HTTPException):
            member = None
    role = guild.get_role(role_id) if guild and role_id else None
    if not embed or member is None or role is None or role.name not in POSITION_ROLES:
        await _respond_ephemeral(interaction, "申請データを確認できなかったぽん。")
        return
    if approve:
        try:
            await _replace_role_group(
                member,
                role,
                POSITION_ROLES,
                reason=f"{interaction.user} による役職承認",
            )
        except discord.Forbidden:
            await _respond_ephemeral(interaction, "ロールを付ける権限が足りないぽん。やがぽんのロール順を確認してほしいぽん。")
            return
        embed.color = discord.Color.green()
        embed.add_field(name="結果", value=f"✅ {interaction.user.mention} が承認", inline=False)
    else:
        embed.color = discord.Color.red()
        embed.add_field(name="結果", value=f"❌ {interaction.user.mention} が却下", inline=False)
    await interaction.response.edit_message(embed=embed, view=None)
    try:
        result = "承認" if approve else "却下"
        await member.send(f"28th矢上祭実行委員会で申請した **{role.name}** ロールは{result}されました。")
    except discord.HTTPException:
        pass


class RoleApprovalView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="承認",
        style=discord.ButtonStyle.success,
        custom_id="yagapon:committee:position:approve:v1",
    )
    async def approve(self, _button: discord.ui.Button, interaction: discord.Interaction):
        await _handle_position_request(interaction, approve=True)

    @discord.ui.button(
        label="却下",
        style=discord.ButtonStyle.danger,
        custom_id="yagapon:committee:position:reject:v1",
    )
    async def reject(self, _button: discord.ui.Button, interaction: discord.Interaction):
        await _handle_position_request(interaction, approve=False)


async def _ensure_role(guild: discord.Guild, name: str, *, color: int = 0) -> tuple[discord.Role, bool]:
    role = _find_named(guild.roles, name)
    if role:
        return role, False
    role = await guild.create_role(
        name=name,
        color=discord.Color(color),
        mentionable=True,
        reason="28thサーバー初期構築",
    )
    return role, True


async def _ensure_category(guild: discord.Guild, name: str, *, overwrites=None):
    category = _find_named(guild.categories, name)
    if category:
        return category, False
    kwargs = {"reason": "28thサーバー初期構築"}
    if overwrites is not None:
        kwargs["overwrites"] = overwrites
    return await guild.create_category(name, **kwargs), True


async def _ensure_text(guild, name: str, category, *, topic: str, overwrites=None, read_only=False):
    channel = _find_named(guild.text_channels, name)
    if channel:
        return channel, False
    if read_only:
        overwrites = dict(overwrites or {})
        overwrites[guild.default_role] = discord.PermissionOverwrite(view_channel=True, send_messages=False)
        overwrites[guild.me] = discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_messages=True)
    kwargs = {
        "category": category,
        "topic": topic,
        "default_auto_archive_duration": 10080,
        "reason": "28thサーバー初期構築",
    }
    if overwrites is not None:
        kwargs["overwrites"] = overwrites
    channel = await guild.create_text_channel(name, **kwargs)
    return channel, True


async def _ensure_forum(guild, name: str, category, *, topic: str, tags, overwrites=None):
    channel = _find_named(guild.forum_channels, name) or _find_named(guild.text_channels, name)
    if channel:
        return channel, False
    try:
        kwargs = {
            "category": category,
            "topic": topic,
            "available_tags": [discord.ForumTag(name=tag) for tag in tags],
            "default_auto_archive_duration": 10080,
            "reason": "28thサーバー初期構築",
        }
        if overwrites is not None:
            kwargs["overwrites"] = overwrites
        channel = await guild.create_forum_channel(name, **kwargs)
    except discord.HTTPException as exc:
        if getattr(exc, "code", None) != 50024:
            raise
        log.warning("Forum unavailable; using thread-first text channel guild=%s name=%s", guild.id, name)
        kwargs = {
            "category": category,
            "topic": f"【スレッド専用】{topic}",
            "default_auto_archive_duration": 10080,
            "reason": "コミュニティ機能未有効のためスレッド専用チャンネルとして作成",
        }
        if overwrites is not None:
            kwargs["overwrites"] = overwrites
        channel = await guild.create_text_channel(name, **kwargs)
    return channel, True


async def _ensure_voice(guild, name: str, category, *, overwrites=None):
    channel = _find_named(guild.voice_channels, name)
    if channel:
        return channel, False
    kwargs = {"category": category, "reason": "28thサーバー初期構築"}
    if overwrites is not None:
        kwargs["overwrites"] = overwrites
    return await guild.create_voice_channel(name, **kwargs), True


def _private_overwrites(guild: discord.Guild, allowed_roles: Iterable[discord.Role]):
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            connect=True,
            speak=True,
        ),
    }
    for role in allowed_roles:
        overwrites[role] = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            connect=True,
            speak=True,
        )
    return overwrites


async def _post_once(channel: discord.TextChannel, marker: str, *, content=None, embed=None, view=None):
    async for message in channel.history(limit=50):
        if message.author.id == channel.guild.me.id and marker in (message.content or ""):
            if view or content or embed:
                await message.edit(content=content, embed=embed, view=view)
            if not message.pinned:
                try:
                    await message.pin(reason="28thサーバー案内")
                except discord.HTTPException as exc:
                    log.warning(
                        "Could not pin existing committee guide guild=%s channel=%s message=%s: %s",
                        channel.guild.id,
                        channel.id,
                        message.id,
                        exc,
                    )
            return message, False
    message = await channel.send(content=content, embed=embed, view=view, silent=True)
    try:
        await message.pin(reason="28thサーバー案内")
    except discord.HTTPException as exc:
        log.warning(
            "Could not pin new committee guide guild=%s channel=%s message=%s: %s",
            channel.guild.id,
            channel.id,
            message.id,
            exc,
        )
    return message, True


async def bootstrap_committee_server(
    guild: discord.Guild,
    *,
    key_store,
    report_progress: ProgressReporter | None = None,
) -> dict[str, int]:
    """Create the approved server template without deleting existing resources."""
    counts = {"roles": 0, "categories": 0, "channels": 0, "messages": 0}

    async def report(stage: str) -> None:
        log.info("Committee setup progress guild=%s stage=%s", guild.id, stage)
        if report_progress is not None:
            await report_progress(stage)

    await report("ロールを準備中")
    member_role, created = await _ensure_role(guild, MEMBER_ROLE, color=0x2ECC71)
    counts["roles"] += int(created)
    bureau_roles = {}
    for name, color, _emoji, _slug in BUREAU_ROLES:
        bureau_roles[name], created = await _ensure_role(guild, name, color=color)
        counts["roles"] += int(created)
    grade_roles = {}
    for name in GRADE_ROLES:
        grade_roles[name], created = await _ensure_role(guild, name, color=0x7289DA)
        counts["roles"] += int(created)
    position_roles = {}
    for name in POSITION_ROLES:
        position_roles[name], created = await _ensure_role(guild, name, color=0xF39C12)
        counts["roles"] += int(created)
    await report("既存メンバーへ基本ロールを設定中")
    if not guild.chunked:
        await guild.chunk()
    for member in guild.members:
        if not member.bot and member_role not in member.roles:
            try:
                await member.add_roles(member_role, reason="28thサーバー初期構築")
            except (discord.Forbidden, discord.HTTPException):
                log.warning("Could not add initial member role guild=%s member=%s", guild.id, member.id)

    await report("案内・ロール選択チャンネルを準備中")
    start, created = await _ensure_category(guild, "00｜はじめに")
    counts["categories"] += int(created)
    intro, created = await _ensure_text(
        guild,
        "はじめに",
        start,
        topic="最初に読むサーバー案内。重要な使い方はピン留めされています。",
        read_only=True,
    )
    counts["channels"] += int(created)
    panel, created = await _ensure_text(
        guild,
        ROLE_PANEL_CHANNEL,
        start,
        topic="所属局と学年を選択し、役職を申請するチャンネルです。",
        read_only=True,
    )
    counts["channels"] += int(created)
    announcements, created = await _ensure_text(
        guild,
        "お知らせ",
        start,
        topic="全委員向けの確定事項・締切・緊急連絡のみ。",
        read_only=True,
    )
    counts["channels"] += int(created)
    # Administrators bypass channel overwrites. Non-admin approvers must already
    # have Discord's Manage Roles permission; leadership labels alone are not an
    # authorization source.
    approver_roles = [role for role in guild.roles if role.permissions.manage_roles]
    request_overwrites = _private_overwrites(guild, approver_roles)
    requests, created = await _ensure_text(
        guild,
        ROLE_REQUEST_CHANNEL,
        start,
        topic="役職ロール申請。ロール管理者のみ閲覧できます。",
        overwrites=request_overwrites,
    )
    counts["channels"] += int(created)
    await requests.edit(overwrites=request_overwrites, reason="役職申請の閲覧権限を同期")

    await report("全体チャンネルを準備中")
    general, created = await _ensure_category(guild, "01｜全体")
    counts["categories"] += int(created)
    _channel, created = await _ensure_text(
        guild,
        "全体チャット",
        general,
        topic="全委員が見る通常の連絡・相談。話題が長くなる場合はスレッドへ。",
    )
    counts["channels"] += int(created)
    _channel, created = await _ensure_forum(
        guild,
        "質問・相談",
        general,
        topic="1件につき1投稿。解決したらタイトル先頭に【解決】を付けてください。",
        tags=("質問", "相談", "解決済み"),
    )
    counts["channels"] += int(created)
    _channel, created = await _ensure_text(
        guild,
        "雑談",
        general,
        topic="委員同士の雑談用。業務上残すべき話は各局またはフォーラムへ。",
    )
    counts["channels"] += int(created)

    await report("会議チャンネルを準備中")
    meetings, created = await _ensure_category(guild, "02｜会議")
    counts["categories"] += int(created)
    _channel, created = await _ensure_text(
        guild,
        "会議案内",
        meetings,
        topic="日時・場所・議題・資料リンクを集約します。",
        read_only=True,
    )
    counts["channels"] += int(created)
    _channel, created = await _ensure_forum(
        guild,
        "欠席連絡",
        meetings,
        topic="会議ごとに投稿を作り、欠席・遅刻連絡はその投稿へまとめます。",
        tags=("全体会議", "役員会", "局会議"),
    )
    counts["channels"] += int(created)
    _channel, created = await _ensure_forum(
        guild,
        "議題・タスク",
        meetings,
        topic="議題・決定事項・担当・期限を1件1投稿で管理します。",
        tags=("議題", "決定", "タスク", "完了"),
    )
    counts["channels"] += int(created)
    _channel, created = await _ensure_voice(guild, "全体会議", meetings)
    counts["channels"] += int(created)

    await report("Bot・ナレッジチャンネルを準備中")
    bot_category, created = await _ensure_category(guild, "03｜BOT・ナレッジ")
    counts["categories"] += int(created)
    bot_guide, created = await _ensure_text(
        guild,
        "やがぽん使い方",
        bot_category,
        topic="おしゃべりやがぽんのコマンドと運用ルール。",
        read_only=True,
    )
    counts["channels"] += int(created)
    _channel, created = await _ensure_text(
        guild,
        "やがぽん",
        bot_category,
        topic="質問、検索、管理コマンドを試す専用チャンネル。質問はやがぽんをメンション。",
    )
    counts["channels"] += int(created)
    minutes_channel, created = await _ensure_text(
        guild,
        "議事録",
        bot_category,
        topic="会議の生文字起こし・要約・Google Docsリンクを集約します。",
    )
    counts["channels"] += int(created)
    _channel, created = await _ensure_voice(guild, "やがぽん会議", bot_category)
    counts["channels"] += int(created)

    await report("役員・局間連携チャンネルを準備中")
    leaders = [position_roles[name] for name in POSITION_ROLES if name != "一般委員"]
    leader_overwrites = _private_overwrites(guild, leaders)
    leadership, created = await _ensure_category(guild, "04｜役員・企画長", overwrites=leader_overwrites)
    counts["categories"] += int(created)
    _channel, created = await _ensure_text(
        guild,
        "役員会",
        leadership,
        topic="役員会の確定連絡。案件ごとにスレッドを作成します。",
        overwrites=leader_overwrites,
    )
    counts["channels"] += int(created)
    _channel, created = await _ensure_forum(
        guild,
        "企画長連絡",
        leadership,
        topic="企画長向けの依頼・確認を案件単位で管理します。",
        tags=("連絡", "確認依頼", "要対応", "完了"),
        overwrites=leader_overwrites,
    )
    counts["channels"] += int(created)
    _channel, created = await _ensure_voice(guild, "役員会", leadership, overwrites=leader_overwrites)
    counts["channels"] += int(created)

    coordination, created = await _ensure_category(guild, "05｜局間連携")
    counts["categories"] += int(created)
    _channel, created = await _ensure_text(
        guild,
        "局間連携",
        coordination,
        topic="複数局にまたがる連絡。継続する案件は申請・依頼フォーラムへ。",
    )
    counts["channels"] += int(created)

    await report("局間専用連絡チャンネルを準備中")
    liaison_channels = []
    for first_bureau, second_bureau, channel_name in LIAISON_CHANNELS:
        liaison_overwrites = _private_overwrites(
            guild,
            [
                bureau_roles[first_bureau],
                bureau_roles[second_bureau],
                position_roles["執行部"],
            ],
        )
        liaison_topic = (
            f"{first_bureau}・{second_bureau}・執行部のみ閲覧可能。"
            "局間の相談、途中経過、確定事項を共有します。"
        )
        liaison_channel, created = await _ensure_text(
            guild,
            channel_name,
            coordination,
            topic=liaison_topic,
            overwrites=liaison_overwrites,
        )
        counts["channels"] += int(created)
        await liaison_channel.edit(
            category=coordination,
            topic=liaison_topic,
            overwrites=liaison_overwrites,
            reason="局間専用連絡チャンネルの閲覧権限を同期",
        )
        liaison_channels.append((liaison_channel, first_bureau, second_bureau))

    await report("鍵管理チャンネルを準備中")
    key_category, created = await _ensure_category(guild, KEY_CATEGORY)
    counts["categories"] += int(created)
    key_action, created = await _ensure_text(
        guild,
        KEY_ACTION_CHANNEL,
        key_category,
        topic="鍵を選び、あけ・しめ・さしかえをボタンで記録します。",
        read_only=True,
    )
    counts["channels"] += int(created)
    key_report, created = await _ensure_text(
        guild,
        KEY_REPORT_CHANNEL,
        key_category,
        topic="現在の貸出状況を固定表示します。操作通知は一定時間後に自動削除されます。",
        read_only=True,
    )
    counts["channels"] += int(created)
    await key_report.edit(
        topic="現在の貸出状況を固定表示します。操作通知は一定時間後に自動削除されます。",
        reason="鍵管理報告チャンネルの案内を更新",
    )
    _channel, created = await _ensure_forum(
        guild,
        "申請・依頼",
        coordination,
        topic="局間の申請・制作依頼・確認依頼を1件1投稿で管理します。",
        tags=("申請", "制作依頼", "確認依頼", "対応中", "完了"),
    )
    counts["channels"] += int(created)

    await report("局別チャンネルを準備中")
    for index, (bureau_name, _color, _emoji, slug) in enumerate(BUREAU_ROLES, 10):
        overwrites = _private_overwrites(guild, [bureau_roles[bureau_name]])
        category, created = await _ensure_category(guild, f"{index}｜{bureau_name}", overwrites=overwrites)
        counts["categories"] += int(created)
        _channel, created = await _ensure_text(
            guild,
            f"{slug}-連絡",
            category,
            topic=f"{bureau_name}内の確定連絡。話題が続く場合はスレッドへ。",
            overwrites=overwrites,
        )
        counts["channels"] += int(created)
        _channel, created = await _ensure_forum(
            guild,
            f"{slug}-話題",
            category,
            topic=f"{bureau_name}の案件・質問・タスクを1件1投稿で管理します。",
            tags=("相談", "タスク", "決定", "完了"),
            overwrites=overwrites,
        )
        counts["channels"] += int(created)
        _channel, created = await _ensure_voice(guild, f"{bureau_name}会議", category, overwrites=overwrites)
        counts["channels"] += int(created)
        if bureau_name == "渉外局":
            request_overwrites = _private_overwrites(
                guild,
                [bureau_roles["渉外局"], position_roles["執行部"]],
            )
            request_channel, created = await _ensure_text(
                guild,
                KEY_REQUEST_CHANNEL,
                category,
                topic="矢上部室（209）の利用理由を確認し、承認・却下します。",
                overwrites=request_overwrites,
            )
            counts["channels"] += int(created)
            await request_channel.edit(
                overwrites=request_overwrites,
                reason="209利用申請の閲覧・承認権限を同期",
            )

    await report("案内とロール選択パネルを掲示中")
    for liaison_channel, first_bureau, second_bureau in liaison_channels:
        marker = f"yagapon-liaison-guide-v1:{liaison_channel.name}"
        _message, created = await _post_once(
            liaison_channel,
            marker,
            content=f"`{marker}`",
            embed=_liaison_guide_embed(first_bureau, second_bureau),
        )
        counts["messages"] += int(created)
    _message, created = await _post_once(
        intro,
        ROLE_PANEL_MARKER,
        content=f"`{ROLE_PANEL_MARKER}`",
        embed=_intro_embed(guild),
        view=CommitteeGuideView(guild),
    )
    counts["messages"] += int(created)
    _message, created = await _post_once(
        panel,
        ROLE_PANEL_MARKER,
        content=f"`{ROLE_PANEL_MARKER}`",
        embed=_role_panel_embed(),
        view=SelfRolePanelView(),
    )
    counts["messages"] += int(created)
    _message, created = await _post_once(
        bot_guide,
        ROLE_PANEL_MARKER,
        content=f"`{ROLE_PANEL_MARKER}`",
        embed=_bot_guide_embed(guild),
        view=CommitteeGuideView(guild),
    )
    counts["messages"] += int(created)
    _message, created = await _post_once(
        key_action,
        KEY_PANEL_MARKER,
        content=f"`{KEY_PANEL_MARKER}`",
        embed=key_panel_embed(),
        view=KeyPanelView(),
    )
    counts["messages"] += int(created)
    _message, created = await _post_once(
        key_report,
        KEY_STATUS_MARKER,
        content=f"`{KEY_STATUS_MARKER}`",
        embed=key_status_embed(guild, key_store),
    )
    await asyncio.to_thread(key_store.set_status_message_id, guild.id, _message.id)
    counts["messages"] += int(created)

    return counts


def register_persistent_views(bot) -> None:
    bot.add_view(SelfRolePanelView())
    bot.add_view(RoleApprovalView())
    bot.add_view(KeyPanelView())
    bot.add_view(RestrictedKeyApprovalView())


def register(bot):
    @bot.slash_command(name="committee_setup", description="委員会サーバーを一括構築するぽん")
    @discord.default_permissions(administrator=True)
    async def committee_setup(ctx: discord.ApplicationContext):
        if not await require_guild_admin(ctx):
            return
        me = ctx.guild.me
        permissions = me.guild_permissions
        missing = [
            name
            for name, allowed in (
                ("ロール管理", permissions.manage_roles),
                ("ニックネーム管理", permissions.manage_nicknames),
                ("チャンネル管理", permissions.manage_channels),
                ("メッセージ管理", permissions.manage_messages),
                (
                    "メッセージのピン留め",
                    getattr(permissions, "pin_messages", permissions.manage_messages),
                ),
            )
            if not allowed
        ]
        if missing:
            await ctx.respond(f"権限が足りないぽん: {', '.join(missing)}", ephemeral=True)
            return
        await ctx.defer(ephemeral=True)
        progress_message = await ctx.followup.send(
            "⏳ 委員会サーバーの整備を開始したぽん…",
            ephemeral=True,
            wait=True,
        )

        async def report_progress(stage: str) -> None:
            try:
                await progress_message.edit(content=f"⏳ {stage}だぽん…")
            except discord.HTTPException:
                log.warning("Could not update committee setup progress guild=%s", ctx.guild_id)

        try:
            counts = await bootstrap_committee_server(
                ctx.guild,
                key_store=bot.key_management.store,
                report_progress=report_progress,
            )
        except discord.Forbidden:
            log.exception("Committee setup permission error guild=%s", ctx.guild_id)
            await progress_message.edit(
                content="❌ セットアップ中に権限エラーになったぽん。やがぽんのロールを他の管理対象ロールより上にしてほしいぽん。"
            )
            return
        except discord.HTTPException as exc:
            log.exception("Committee setup Discord API error guild=%s", ctx.guild_id)
            await progress_message.edit(
                content=(
                    "❌ Discord APIエラーで停止したぽん。"
                    f" status={exc.status} / code={exc.code}。再実行しても既存設定は重複しないぽん。"
                )
            )
            return
        except Exception as exc:
            log.exception("Unexpected committee setup error guild=%s", ctx.guild_id)
            detail = " ".join(str(exc).split())[:240] or "詳細なし"
            await progress_message.edit(
                content=(
                    "❌ 予期しないエラーで停止したぽん。"
                    f" `{type(exc).__name__}: {detail}`\n"
                    "既存設定は保持されているぽん。"
                )
            )
            return
        minutes_channel = _find_named(ctx.guild.text_channels, "議事録")
        if minutes_channel:
            await bot.config.set_minutes_channel(ctx.guild.id, minutes_channel.id)
        await progress_message.edit(
            content=(
                "✅ 委員会サーバーを整備したぽん！\n"
                f"新規: ロール {counts['roles']} / カテゴリ {counts['categories']} / "
                f"チャンネル {counts['channels']} / 案内 {counts['messages']}\n"
                "既存のものは削除・重複作成していないぽん。"
            )
        )

    @bot.slash_command(name="role_panel", description="ロール選択パネルを再掲するぽん")
    @discord.default_permissions(administrator=True)
    async def role_panel(ctx: discord.ApplicationContext):
        if not await require_guild_admin(ctx):
            return
        await ctx.respond(embed=_role_panel_embed(), view=SelfRolePanelView())

    @bot.slash_command(name="key_status", description="部室・倉庫の鍵の現在状況を表示するぽん")
    async def key_status(ctx: discord.ApplicationContext):
        if not ctx.guild:
            await ctx.respond("サーバー内で実行してほしいぽん。", ephemeral=True)
            return
        await ctx.respond(embed=key_status_embed(ctx.guild, bot.key_management.store), ephemeral=True)

    @bot.slash_command(name="key_force_return", description="鍵の記録を管理者が返却済みに修正するぽん")
    @discord.default_permissions(administrator=True)
    @discord.option(
        "key_name",
        description="返却済みに修正する鍵",
        choices=[discord.OptionChoice(name, name) for name in KEY_NAMES],
    )
    async def key_force_return(ctx: discord.ApplicationContext, key_name: str):
        if not await require_guild_admin(ctx):
            return
        await bot.key_management.force_return(ctx, key_name)

    @bot.slash_command(name="guide", description="やがぽんと委員会サーバーの使い方を表示するぽん")
    async def guide(ctx: discord.ApplicationContext):
        if not ctx.guild:
            await ctx.respond("サーバー内で実行してほしいぽん。", ephemeral=True)
            return
        guide_view = CommitteeGuideView(ctx.guild)
        await ctx.respond(
            embed=_bot_guide_embed(ctx.guild),
            view=guide_view if guide_view.children else None,
            ephemeral=True,
        )
