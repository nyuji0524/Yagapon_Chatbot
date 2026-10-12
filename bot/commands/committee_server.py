"""28th committee Discord server bootstrap and persistent role controls."""

from __future__ import annotations

import logging
from collections.abc import Iterable

import discord

from bot.authorization import require_guild_admin

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
GRADE_ROLES = ("1年", "2年", "3年")
POSITION_ROLES = ("一般委員", "企画長", "役員", "副局長", "局長", "執行部", "副委員長", "委員長")
ROLE_REQUEST_CHANNEL = "ロール申請"
ROLE_PANEL_CHANNEL = "ロール選択"
ROLE_PANEL_MARKER = "yagapon-role-panel-v1"


def _role_names(entries: Iterable[tuple[str, int, str, str]]) -> tuple[str, ...]:
    return tuple(entry[0] for entry in entries)


def _find_named(items, name: str):
    return discord.utils.get(items, name=name)


async def _replace_role_group(member: discord.Member, role: discord.Role, role_names: Iterable[str]) -> None:
    removable = [current for current in member.roles if current.name in role_names and current.id != role.id]
    if removable:
        await member.remove_roles(*removable, reason="本人によるロール選択")
    if role not in member.roles:
        await member.add_roles(role, reason="本人によるロール選択")


async def _respond_ephemeral(interaction: discord.Interaction, message: str) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


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


class SelfRolePanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(BureauSelect())
        self.add_item(GradeSelect())
        self.add_item(PositionSelect())


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
            await _replace_role_group(member, role, POSITION_ROLES)
        except discord.Forbidden:
            await _respond_ephemeral(interaction, "ロールを付ける権限が足りないぽん。やがぽんのロール順を確認してほしいぽん。")
            return
        embed.color = discord.Color.green()
        embed.add_field(name="結果", value=f"✅ {interaction.user.mention} が承認", inline=False)
    else:
        embed.color = discord.Color.red()
        embed.add_field(name="結果", value=f"❌ {interaction.user.mention} が却下", inline=False)
    await interaction.response.edit_message(embed=embed, view=None)


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
    return await guild.create_category(name, overwrites=overwrites, reason="28thサーバー初期構築"), True


async def _ensure_text(guild, name: str, category, *, topic: str, overwrites=None, read_only=False):
    channel = _find_named(guild.text_channels, name)
    if channel:
        return channel, False
    if read_only:
        overwrites = dict(overwrites or {})
        overwrites[guild.default_role] = discord.PermissionOverwrite(view_channel=True, send_messages=False)
        overwrites[guild.me] = discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_messages=True)
    channel = await guild.create_text_channel(
        name,
        category=category,
        topic=topic,
        overwrites=overwrites,
        default_auto_archive_duration=10080,
        reason="28thサーバー初期構築",
    )
    return channel, True


async def _ensure_forum(guild, name: str, category, *, topic: str, tags, overwrites=None):
    channel = _find_named(guild.forum_channels, name)
    if channel:
        return channel, False
    channel = await guild.create_forum_channel(
        name,
        category=category,
        topic=topic,
        available_tags=[discord.ForumTag(name=tag) for tag in tags],
        overwrites=overwrites,
        default_auto_archive_duration=10080,
        reason="28thサーバー初期構築",
    )
    return channel, True


async def _ensure_voice(guild, name: str, category, *, overwrites=None):
    channel = _find_named(guild.voice_channels, name)
    if channel:
        return channel, False
    return await guild.create_voice_channel(
        name,
        category=category,
        overwrites=overwrites,
        reason="28thサーバー初期構築",
    ), True


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
            if view:
                await message.edit(content=content, embed=embed, view=view)
            return message, False
    message = await channel.send(content=content, embed=embed, view=view, silent=True)
    try:
        await message.pin(reason="28thサーバー案内")
    except discord.HTTPException:
        pass
    return message, True


async def bootstrap_committee_server(guild: discord.Guild) -> dict[str, int]:
    """Create the approved server template without deleting existing resources."""
    counts = {"roles": 0, "categories": 0, "channels": 0, "messages": 0}

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

    for member in guild.members:
        if not member.bot and member_role not in member.roles:
            await member.add_roles(member_role, reason="28thサーバー初期構築")

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
    request_overwrites = _private_overwrites(guild, [])
    requests, created = await _ensure_text(
        guild,
        ROLE_REQUEST_CHANNEL,
        start,
        topic="役職ロール申請。ロール管理者のみ閲覧できます。",
        overwrites=request_overwrites,
    )
    counts["channels"] += int(created)

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
    _channel, created = await _ensure_forum(
        guild,
        "申請・依頼",
        coordination,
        topic="局間の申請・制作依頼・確認依頼を1件1投稿で管理します。",
        tags=("申請", "制作依頼", "確認依頼", "対応中", "完了"),
    )
    counts["channels"] += int(created)

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

    intro_text = (
        f"`{ROLE_PANEL_MARKER}`\n"
        "# 28th矢上祭実行委員会 Discord\n"
        "1. #ロール選択 で所属局と学年を設定\n"
        "2. 確定連絡はテキストチャンネル、案件・質問はフォーラムへ\n"
        "3. 長くなる話題はスレッドに分け、結論を先頭メッセージへ追記\n"
        "4. 会議ではVC参加後に `/join`、終了時に `/leave` で議事録を作成\n"
        "5. 機密情報・個人情報は必要な権限のチャンネルだけに投稿してください"
    )
    _message, created = await _post_once(intro, ROLE_PANEL_MARKER, content=intro_text)
    counts["messages"] += int(created)
    panel_embed = discord.Embed(
        title="所属ロールを設定",
        description=(
            "所属局と学年はすぐ反映されます。役職はチャンネル権限に関わるため管理者承認制です。\n"
            "変更したい場合は、もう一度選び直してください。"
        ),
        color=discord.Color.blurple(),
    )
    _message, created = await _post_once(
        panel,
        ROLE_PANEL_MARKER,
        content=f"`{ROLE_PANEL_MARKER}`",
        embed=panel_embed,
        view=SelfRolePanelView(),
    )
    counts["messages"] += int(created)
    bot_text = (
        f"`{ROLE_PANEL_MARKER}`\n"
        "# おしゃべりやがぽん\n"
        "- 質問: #やがぽん でやがぽんをメンション\n"
        "- 議事録: VCで `/join` → 会議終了時に `/leave`\n"
        "- 声紋登録: `/voiceprint`（話者名の精度向上）\n"
        "- 困ったとき: `/status`\n"
        "- 会議の成果物: #議事録 と連携Google Driveへ保存\n\n"
        "会議はなるべく #やがぽん会議 または各局VCで行い、関連資料はVCチャットへ貼ってください。"
    )
    _message, created = await _post_once(bot_guide, ROLE_PANEL_MARKER, content=bot_text)
    counts["messages"] += int(created)

    try:
        await guild.edit(system_channel=minutes_channel, reason="議事録通知先を設定")
    except discord.HTTPException:
        log.warning("Could not set system channel guild=%s", guild.id)
    return counts


def register_persistent_views(bot) -> None:
    bot.add_view(SelfRolePanelView())
    bot.add_view(RoleApprovalView())


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
                ("チャンネル管理", permissions.manage_channels),
                ("メッセージ管理", permissions.manage_messages),
            )
            if not allowed
        ]
        if missing:
            await ctx.respond(f"権限が足りないぽん: {', '.join(missing)}", ephemeral=True)
            return
        await ctx.defer(ephemeral=True)
        try:
            counts = await bootstrap_committee_server(ctx.guild)
        except discord.Forbidden:
            log.exception("Committee setup permission error guild=%s", ctx.guild_id)
            await ctx.followup.send(
                "セットアップ中に権限エラーになったぽん。やがぽんのロールを他の管理対象ロールより上にしてほしいぽん。",
                ephemeral=True,
            )
            return
        await ctx.followup.send(
            "✅ 委員会サーバーを整備したぽん！\n"
            f"新規: ロール {counts['roles']} / カテゴリ {counts['categories']} / "
            f"チャンネル {counts['channels']} / 案内 {counts['messages']}\n"
            "既存のものは削除・重複作成していないぽん。",
            ephemeral=True,
        )

    @bot.slash_command(name="role_panel", description="ロール選択パネルを再掲するぽん")
    @discord.default_permissions(administrator=True)
    async def role_panel(ctx: discord.ApplicationContext):
        if not await require_guild_admin(ctx):
            return
        await ctx.respond(
            "所属局・学年を選択し、必要な役職を申請してください。",
            view=SelfRolePanelView(),
        )
