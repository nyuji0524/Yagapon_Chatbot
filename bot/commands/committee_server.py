"""28th committee Discord server bootstrap and persistent role controls."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable, Iterable
from itertools import combinations

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
from bot.support_directory import SupportIdentity

log = logging.getLogger("yagapon.committee_server")

MEMBER_ROLE = "28th委員"
SERVER_ADMIN_ROLE = "サーバー管理者"
BUREAU_ROLES = (
    ("IT局", 0x58A6FF, "🩵", "it"),
    ("総務局", 0x95A5A6, "🩶", "総務"),
    ("装飾局", 0xF1C40F, "💛", "装飾"),
    ("ステージ局", 0xE74C3C, "❤️", "ステージ"),
    ("屋外局", 0xE67E22, "🧡", "屋外"),
    ("室内局", 0x3498DB, "🩵", "室内"),
    ("広報局", 0xFF69B4, "🩷", "広報"),
    ("渉外局", 0x9B59B6, "💜", "渉外"),
)
LIAISON_CHANNELS = tuple(
    (first[0], second[0], f"{first[3]}{second[3]}連絡チャット")
    for first, second in combinations(BUREAU_ROLES, 2)
)
GRADE_ROLES = ("1年", "2年", "3年")
POSITION_ROLES = ("一般委員", "企画長", "役員", "副局長", "局長", "執行部")
DECLARABLE_POSITIONS = ("企画長", "副局長", "局長")
OFFICER_POSITIONS = ("副局長", "局長")
POSITION_ROLE_ORDER = ("執行部", "局長", "副局長", "企画長", "役員", "一般委員")
ROLE_REQUEST_CHANNEL = "ロール申請🔑"
ROLE_PANEL_CHANNEL = "ロール選択"
ROLE_PANEL_MARKER = "yagapon-role-panel-v1"
COMMITTEE_COLOR = 0xF2A900
YAGA_SUPPORT_URL = "https://yagasupport.yagamifes.com"
ProgressReporter = Callable[[str], Awaitable[None]]


def _role_names(entries: Iterable[tuple[str, int, str, str]]) -> tuple[str, ...]:
    return tuple(entry[0] for entry in entries)


def _find_named(items, name: str):
    return discord.utils.get(items, name=name)


def _find_in_category(items, name: str, category):
    """Find a channel by both name and category so duplicate names stay valid."""
    return next(
        (
            item
            for item in items
            if item.name == name and getattr(item, "category_id", None) == category.id
        ),
        None,
    )


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


def _position_role_names(roles: Iterable[discord.Role]) -> list[str]:
    names = {role.name for role in roles}
    return [name for name in POSITION_ROLE_ORDER if name in names]


def _position_role_summary(roles: Iterable[discord.Role]) -> str:
    return ", ".join(_position_role_names(roles)) or "一般委員"


def _has_completed_registration(config, guild_id: int, member, member_role) -> bool:
    registration = config.get_member(guild_id, member.id)
    return bool(
        member_role
        and member_role in member.roles
        and registration
        and registration.get("email")
        and registration.get("bureau")
        and registration.get("grade")
    )


def _roles_after_position_approval(
    roles: Iterable[discord.Role],
    approved_role: discord.Role,
    officer_role: discord.Role | None = None,
) -> list[discord.Role]:
    """Preserve project-leader and officer roles independently.

    副局長と局長だけは同時に成立しないため、役員申告の承認時に相互置換する。
    企画長は役員と兼任できる。
    """
    removable_names = {"一般委員"}
    if approved_role.name in OFFICER_POSITIONS:
        removable_names.update(OFFICER_POSITIONS)
    desired = [
        role
        for role in roles
        if not getattr(role, "is_default", lambda: False)()
        and role.name not in removable_names
        and role.id != approved_role.id
    ]
    desired.append(approved_role)
    if approved_role.name in OFFICER_POSITIONS and officer_role is not None:
        if all(role.id != officer_role.id for role in desired):
            desired.append(officer_role)
    return desired


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
            "次の順番で登録してください。\n"
            "**1. Discordのサーバープロフィールから表示名を自分で変更**\n"
            "**2. 所属局と学年を選択**\n"
            "**3. 「登録を完了」から慶應メールを入力**\n\n"
            "表示名はやがぽんから変更しません。選び直した局・学年は自動で置き換わります。"
        ),
        color=discord.Color(COMMITTEE_COLOR),
    )
    embed.add_field(name="1️⃣ 登録時", value="**所属局・学年 → 慶應メール**", inline=True)
    embed.add_field(
        name="2️⃣ 登録後（該当者のみ）",
        value="**企画長申告／役員申告**\n※両方を兼任できます",
        inline=True,
    )
    embed.add_field(
        name="困ったとき",
        value="選択肢が足りない、変更できない場合はIT局へ連絡してください。",
        inline=False,
    )
    embed.set_footer(text="登録完了DMには、現在のサーバー表示名も記載します")
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
        value=(
            "まずサーバープロフィールから表示名を自分で変更し、"
            f"{_channel_mention(guild, ROLE_PANEL_CHANNEL)} で **所属局 → 学年 → 慶應メール** を登録します。"
        ),
        inline=False,
    )
    embed.add_field(
        name="2️⃣ 連絡と相談を使い分ける",
        value=(
            f"確定事項は {_channel_mention(guild, 'おしらせ（全体）')}、日常連絡は "
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
        value=f"{_channel_mention(guild, 'おしゃべりやがぽん')} でやがぽんをメンションして質問",
        inline=False,
    )
    embed.add_field(
        name="🎙️ 会議を記録",
        value="`/join` → 会議 → `/leave`\n話者精度を上げる場合は `/voiceprint register`",
        inline=True,
    )
    embed.add_field(
        name="🧭 自分の設定",
        value=f"{_channel_mention(guild, ROLE_PANEL_CHANNEL)} で局・学年・慶應メールを登録",
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


class CommitteeGuideView(discord.ui.View):
    def __init__(self, guild: discord.Guild):
        # Link buttons are resolved by Discord and do not need a running timer.
        super().__init__(timeout=None)
        for label, emoji, channel_name in (
            ("プロフィール設定", "🎫", ROLE_PANEL_CHANNEL),
            ("やがぽんに質問", "💬", "おしゃべりやがぽん"),
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


async def _submit_position_request(
    interaction: discord.Interaction,
    role_name: str,
    *,
    application_kind: str,
) -> None:
    guild = interaction.guild
    member = interaction.user
    role = _find_named(guild.roles, role_name) if guild else None
    request_channel = _find_named(guild.text_channels, ROLE_REQUEST_CHANNEL) if guild else None
    if not guild or not isinstance(member, discord.Member) or role is None or request_channel is None:
        await _respond_ephemeral(interaction, "役職申請の設定が見つからないぽん。管理者に再セットアップを依頼してほしいぽん。")
        return
    member_role = _find_named(guild.roles, MEMBER_ROLE)
    if not _has_completed_registration(
        interaction.client.config,
        guild.id,
        member,
        member_role,
    ):
        await _respond_ephemeral(interaction, "先に所属局・学年・慶應メールの登録を完了してほしいぽん。")
        return
    if role in member.roles:
        await _respond_ephemeral(interaction, f"**{role.name}** は既に付いているぽん！")
        return
    embed = discord.Embed(
        title=f"{application_kind}申請",
        description=f"{member.mention} が **{role.name}** を申請しています。",
        color=discord.Color.gold(),
    )
    embed.add_field(name="申請者ID", value=str(member.id), inline=True)
    embed.add_field(name="役職ロールID", value=str(role.id), inline=True)
    embed.add_field(name="申請種別", value=application_kind, inline=True)
    embed.set_footer(text="企画長と役員は兼任できます。副局長と局長は選択した方へ置き換わります")
    await request_channel.send(embed=embed, view=RoleApprovalView(), silent=True)
    await _respond_ephemeral(interaction, f"📨 **{role.name}** を申請したぽん。管理者の承認を待ってねぽん！")


class ProjectLeaderApplicationButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="企画長を申告",
            emoji="🎪",
            style=discord.ButtonStyle.primary,
            row=0,
            custom_id="yagapon:committee:project-leader:v1",
        )

    async def callback(self, interaction: discord.Interaction):
        await _submit_position_request(interaction, "企画長", application_kind="企画長")


class OfficerSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(
            placeholder="役員申告：副局長または局長を選択",
            min_values=1,
            max_values=1,
            row=1,
            custom_id="yagapon:committee:officer:v1",
            options=[
                discord.SelectOption(label=name, value=name, emoji="🛡️")
                for name in OFFICER_POSITIONS
            ],
        )

    async def callback(self, interaction: discord.Interaction):
        await _submit_position_request(interaction, self.values[0], application_kind="役員")


class PostRegistrationRoleView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(ProjectLeaderApplicationButton())
        self.add_item(OfficerSelect())


def _is_keio_email(value: str) -> bool:
    value = value.strip().lower()
    return bool(re.fullmatch(r"[^@\s]+@(?:[^@\s]+\.)*keio\.jp", value))


def _support_status_message(identity: SupportIdentity) -> str:
    if identity.status == "disabled":
        return "現在はテスト設定のため、やがサポ照合を無効にしています。Discord登録だけ完了しました。"
    if identity.status == "directory_match":
        return "やがサポの登録候補を確認しました。UIDの確定とロール同期は管理者承認待ちです。"
    if identity.status == "verified":
        role_status = (
            "「矢上祭実行委員」ロールも確認済みです。"
            if identity.role_synced
            else "UIDを紐付けました。サーバー設定により、やがサポ側のロールは変更していません。"
        )
        return f"やがサポの登録を確認しました（UID: `{identity.uid}`）。{role_status}"
    if identity.status == "not_found":
        return f"やがサポの利用者がまだ見つかりません。{YAGA_SUPPORT_URL} からサインインしてください。"
    if identity.status == "profile_mismatch":
        return "やがサポの登録は見つかりましたが、局または学年が一致しません。ロール申請チャンネルで確認を依頼してください。"
    return "やがサポとの照合は保留です。Discord登録には影響しません。後から自動または管理者操作で再確認します。"


class RegistrationModal(discord.ui.Modal):
    def __init__(self):
        super().__init__(
            title="委員登録を完了",
            custom_id="yagapon:committee:registration:modal:v1",
        )
        self.add_item(
            discord.ui.InputText(
                label="慶應メールアドレス",
                placeholder="例：example@keio.jp",
                min_length=6,
                max_length=254,
                custom_id="keio_email",
            )
        )

    async def callback(self, interaction: discord.Interaction):
        guild = interaction.guild
        member = interaction.user
        email = self.children[0].value.strip().lower()
        if not guild or not isinstance(member, discord.Member):
            await _respond_ephemeral(interaction, "サーバー内で実行してほしいぽん。")
            return
        if not _is_keio_email(email):
            await _respond_ephemeral(interaction, "慶應のメールアドレス（`@keio.jp`）を入力してほしいぽん。")
            return

        bureau = next((role.name for role in member.roles if role.name in _role_names(BUREAU_ROLES)), None)
        grade = next((role.name for role in member.roles if role.name in GRADE_ROLES), None)
        if not bureau or not grade:
            await _respond_ephemeral(interaction, "先に所属局と学年の両方を選んでほしいぽん。")
            return

        member_role = _find_named(guild.roles, MEMBER_ROLE)
        general_role = _find_named(guild.roles, "一般委員")
        if member_role is None or general_role is None:
            await _respond_ephemeral(interaction, "委員ロールが見つからないぽん。管理者に再セットアップを依頼してほしいぽん。")
            return
        await interaction.response.defer(ephemeral=True)
        identity = await interaction.client.support_directory.verify(email, bureau, grade)
        try:
            roles_to_add = [member_role]
            member_role_names = {role.name for role in member.roles}
            if not member_role_names.intersection(POSITION_ROLES):
                roles_to_add.append(general_role)
            await member.add_roles(*roles_to_add, reason="本人による28th委員登録完了")
            effective_roles = [*member.roles, *roles_to_add]
            position_roles = _position_role_names(effective_roles)
            position_summary = _position_role_summary(effective_roles)
        except discord.Forbidden:
            await _respond_ephemeral(interaction, "委員ロールを付ける権限が足りないぽん。管理者に連絡してほしいぽん。")
            return
        except discord.HTTPException:
            log.exception("Committee registration role update failed guild=%s member=%s", guild.id, member.id)
            await _respond_ephemeral(interaction, "Discord APIとの通信に失敗したぽん。少し待ってからもう一度試してほしいぽん。")
            return

        config = interaction.client.config
        members = config.get_members(guild.id)
        existing_record = members.get(str(member.id), {})
        preserve_verified_link = bool(
            existing_record.get("support_status") == "verified"
            and existing_record.get("support_uid")
            and existing_record.get("email") == email
            and existing_record.get("bureau") == bureau
            and existing_record.get("grade") == grade
        )
        effective_identity = identity
        if preserve_verified_link:
            effective_identity = SupportIdentity(
                status="verified",
                uid=existing_record["support_uid"],
                departments=tuple(existing_record.get("support_departments", ())),
                grade=existing_record.get("support_grade"),
                role_synced=bool(existing_record.get("support_role_synced")),
            )
        members[str(member.id)] = {
            **existing_record,
            "name": member.display_name,
            "email": email,
            "bureau": bureau,
            "grade": grade,
            "role": position_summary,
            "roles": position_roles,
            "support_uid": effective_identity.uid if preserve_verified_link else None,
            "support_candidate_uid": None if preserve_verified_link else identity.uid,
            "support_status": effective_identity.status,
            "support_departments": list(effective_identity.departments),
            "support_grade": effective_identity.grade,
            "support_role_synced": effective_identity.role_synced,
        }
        await config.set_members(guild.id, members)

        if not preserve_verified_link and identity.status == "directory_match" and identity.uid:
            request_channel = _find_named(guild.text_channels, ROLE_REQUEST_CHANNEL)
            if request_channel:
                request_embed = discord.Embed(
                    title="やがサポUID紐付け申請",
                    description=(
                        f"{member.mention} の自己申告と、やがサポの保存値が一致しました。"
                        "本人確認後に承認してください。"
                    ),
                    color=discord.Color.gold(),
                )
                request_embed.add_field(name="申請者ID", value=str(member.id), inline=True)
                request_embed.add_field(name="候補UID", value=identity.uid, inline=True)
                request_embed.add_field(name="慶應メール", value=email, inline=False)
                request_embed.add_field(name="所属局", value=bureau, inline=True)
                request_embed.add_field(name="学年", value=grade, inline=True)
                request_embed.set_footer(text="入力メールだけでは確定せず、承認時にFirestoreを再照合します")
                await request_channel.send(
                    embed=request_embed,
                    view=SupportLinkApprovalView(),
                    silent=True,
                )

        embed = discord.Embed(
            title="✅ Discordの委員登録が完了しました",
            description=f"現在のサーバー表示名は **{member.display_name}** です。",
            color=discord.Color.green(),
        )
        embed.add_field(name="所属", value=f"{bureau} / {grade}", inline=True)
        embed.add_field(name="慶應メール", value=email, inline=True)
        embed.add_field(name="やがサポ", value=_support_status_message(effective_identity), inline=False)
        embed.set_footer(text="表示名の変更はDiscordのサーバープロフィールから行ってください")
        embed.add_field(
            name="該当する人だけ次へ",
            value=(
                "下の操作から **企画長申告** または **役員申告（副局長・局長）** を行えます。"
                "企画長と役員は両方申請できます。"
            ),
            inline=False,
        )
        await interaction.followup.send(
            embed=embed,
            view=PostRegistrationRoleView(),
            ephemeral=True,
        )
        try:
            await member.send(
                "✅ 28th矢上祭実行委員会のDiscord登録が完了しました。\n"
                f"**サーバー表示名:** {member.display_name}\n"
                f"**所属:** {bureau} / {grade}\n"
                f"**慶應メール:** {email}\n\n"
                "表示名を変える場合は、Discordのサーバープロフィールから自分で変更してください。\n"
                f"**やがサポ:** {_support_status_message(effective_identity)}\n\n"
                "企画長・副局長・局長に該当する場合は、登録完了画面または "
                "`/committee_roles` から申請してください。企画長と役員は兼任できます。"
            )
        except discord.HTTPException:
            log.info("Could not send committee registration DM guild=%s member=%s", guild.id, member.id)


class SelfRolePanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(BureauSelect())
        self.add_item(GradeSelect())

    @discord.ui.button(
        label="登録を完了",
        emoji="✅",
        style=discord.ButtonStyle.success,
        row=2,
        custom_id="yagapon:committee:registration:v1",
    )
    async def registration(self, _button: discord.ui.Button, interaction: discord.Interaction):
        await interaction.response.send_modal(RegistrationModal())


def _embed_field_id(embed: discord.Embed, field_name: str) -> int | None:
    for field in embed.fields:
        if field.name == field_name:
            try:
                return int(field.value)
            except (TypeError, ValueError):
                return None
    return None


def _embed_field_value(embed: discord.Embed, field_name: str) -> str | None:
    for field in embed.fields:
        if field.name == field_name:
            return str(field.value)
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
    if not embed or member is None or role is None or role.name not in DECLARABLE_POSITIONS:
        await _respond_ephemeral(interaction, "申請データを確認できなかったぽん。")
        return
    if approve:
        try:
            officer_role = None
            if role.name in ("副局長", "局長"):
                officer_role = _find_named(guild.roles, "役員")
                if officer_role is None:
                    await _respond_ephemeral(interaction, "役員ロールが見つからないぽん。再セットアップしてほしいぽん。")
                    return
            desired_roles = _roles_after_position_approval(
                member.roles,
                role,
                officer_role,
            )
            await member.edit(roles=desired_roles, reason=f"{interaction.user} による役職承認")
            members = interaction.client.config.get_members(guild.id)
            record = members.get(str(member.id), {})
            position_roles = _position_role_names(desired_roles)
            record["role"] = _position_role_summary(desired_roles)
            record["roles"] = position_roles
            members[str(member.id)] = record
            await interaction.client.config.set_members(guild.id, members)
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


async def _handle_support_link_request(interaction: discord.Interaction, *, approve: bool) -> None:
    permissions = getattr(interaction.user, "guild_permissions", None)
    if not permissions or not permissions.manage_roles:
        await _respond_ephemeral(interaction, "やがサポ連携を処理できるのはロール管理者だけだぽん。")
        return
    message = interaction.message
    embed = message.embeds[0] if message and message.embeds else None
    guild = interaction.guild
    member_id = _embed_field_id(embed, "申請者ID") if embed else None
    expected_uid = _embed_field_value(embed, "候補UID") if embed else None
    email = _embed_field_value(embed, "慶應メール") if embed else None
    bureau = _embed_field_value(embed, "所属局") if embed else None
    grade = _embed_field_value(embed, "学年") if embed else None
    if not guild or not embed or not all((member_id, expected_uid, email, bureau, grade)):
        await _respond_ephemeral(interaction, "連携申請データを確認できなかったぽん。")
        return
    member = guild.get_member(member_id)
    if member is None:
        try:
            member = await guild.fetch_member(member_id)
        except (discord.NotFound, discord.HTTPException):
            member = None
    if member is None:
        await _respond_ephemeral(interaction, "申請者がサーバーに見つからないぽん。")
        return
    if not approve:
        embed.color = discord.Color.red()
        embed.add_field(name="結果", value=f"❌ {interaction.user.mention} が却下", inline=False)
        await interaction.response.edit_message(embed=embed, view=None)
        try:
            await member.send("やがサポとのUID紐付け申請は却下されました。必要な場合は管理者へ確認してください。")
        except discord.HTTPException:
            pass
        return

    await interaction.response.defer(ephemeral=True)
    identity = await interaction.client.support_directory.approve_link(
        email,
        bureau,
        grade,
        expected_uid,
    )
    if identity.status != "verified" or identity.uid is None:
        await interaction.followup.send(
            "再照合で一致を確認できなかったぽん。申請は変更せず、保存値を確認してほしいぽん。",
            ephemeral=True,
        )
        return
    members = interaction.client.config.get_members(guild.id)
    record = members.get(str(member.id), {})
    record.update(
        {
            "support_uid": identity.uid,
            "support_candidate_uid": None,
            "support_status": "verified",
            "support_departments": list(identity.departments),
            "support_grade": identity.grade,
            "support_role_synced": identity.role_synced,
        }
    )
    members[str(member.id)] = record
    await interaction.client.config.set_members(guild.id, members)
    embed.color = discord.Color.green()
    embed.add_field(name="結果", value=f"✅ {interaction.user.mention} が承認", inline=False)
    embed.add_field(name="ロール同期", value="完了" if identity.role_synced else "無効（UIDのみ確定）", inline=True)
    await message.edit(embed=embed, view=None)
    await interaction.followup.send("✅ やがサポとのUID紐付けを確定したぽん。", ephemeral=True)
    try:
        await member.send(
            "✅ やがサポとのUID紐付けが承認されました。\n"
            f"**サーバー表示名:** {member.display_name}\n"
            f"**UID:** `{identity.uid}`\n"
            f"**ロール同期:** {'完了' if identity.role_synced else '未実施'}"
        )
    except discord.HTTPException:
        pass


class SupportLinkApprovalView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="本人確認して承認",
        style=discord.ButtonStyle.success,
        custom_id="yagapon:committee:support-link:approve:v1",
    )
    async def approve(self, _button: discord.ui.Button, interaction: discord.Interaction):
        await _handle_support_link_request(interaction, approve=True)

    @discord.ui.button(
        label="却下",
        style=discord.ButtonStyle.danger,
        custom_id="yagapon:committee:support-link:reject:v1",
    )
    async def reject(self, _button: discord.ui.Button, interaction: discord.Interaction):
        await _handle_support_link_request(interaction, approve=False)


async def _ensure_role(
    guild: discord.Guild,
    name: str,
    *,
    color: int = 0,
    permissions: discord.Permissions | None = None,
) -> tuple[discord.Role, bool]:
    role = _find_named(guild.roles, name)
    if role:
        return role, False
    role = await guild.create_role(
        name=name,
        color=discord.Color(color),
        permissions=permissions or discord.Permissions.none(),
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
    channel = _find_in_category(guild.text_channels, name, category)
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
    channel = _find_in_category(guild.forum_channels, name, category) or _find_in_category(
        guild.text_channels, name, category
    )
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


async def _ensure_thread_only(guild, name: str, category, *, topic: str, overwrites=None):
    channel = _find_in_category(guild.text_channels, name, category)
    thread_overwrites = dict(overwrites or {})
    existing_default = thread_overwrites.get(guild.default_role, discord.PermissionOverwrite())
    default_view = existing_default.view_channel
    if default_view is None:
        default_view = True
    thread_overwrites[guild.default_role] = discord.PermissionOverwrite(
        view_channel=default_view,
        send_messages=False,
        create_public_threads=default_view,
        send_messages_in_threads=default_view,
        read_message_history=default_view,
    )
    for target, existing in tuple(thread_overwrites.items()):
        if target in (guild.default_role, guild.me) or existing.view_channel is not True:
            continue
        thread_overwrites[target] = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=False,
            create_public_threads=True,
            send_messages_in_threads=True,
            read_message_history=True,
        )
    thread_overwrites[guild.me] = discord.PermissionOverwrite(
        view_channel=True,
        send_messages=True,
        create_public_threads=True,
        send_messages_in_threads=True,
        read_message_history=True,
        manage_messages=True,
        manage_threads=True,
    )
    full_topic = f"【スレッド専用】{topic}"
    if channel:
        if (
            channel.topic != full_topic
            or _overwrite_signature(channel.overwrites) != _overwrite_signature(thread_overwrites)
        ):
            await channel.edit(
                category=category,
                topic=full_topic,
                overwrites=thread_overwrites,
                reason="スレッド専用チャンネル設定を同期",
            )
        return channel, False
    return await guild.create_text_channel(
        name,
        category=category,
        topic=full_topic,
        overwrites=thread_overwrites,
        default_auto_archive_duration=10080,
        reason="28thサーバー初期構築",
    ), True


async def _ensure_voice(guild, name: str, category, *, overwrites=None):
    channel = _find_in_category(guild.voice_channels, name, category)
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


def _overwrite_signature(overwrites) -> dict[int, tuple[int, int]]:
    """Compare Discord overwrites by target and permission bits, not object identity."""
    signature = {}
    for target, overwrite in overwrites.items():
        allow, deny = overwrite.pair()
        signature[target.id] = (allow.value, deny.value)
    return signature


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
    setup_actor: discord.Member | None = None,
    report_progress: ProgressReporter | None = None,
) -> dict[str, int]:
    """Create or reconcile the approved 28th server layout without deleting data."""
    counts = {"roles": 0, "categories": 0, "channels": 0, "messages": 0}

    async def report(stage: str) -> None:
        log.info("Committee setup v2 progress guild=%s stage=%s", guild.id, stage)
        if report_progress is not None:
            await report_progress(stage)

    async def ensure_category(name: str, *, overwrites=None):
        category, created = await _ensure_category(guild, name, overwrites=overwrites)
        counts["categories"] += int(created)
        if not created and overwrites is not None and _overwrite_signature(category.overwrites) != _overwrite_signature(overwrites):
            await category.edit(overwrites=overwrites, reason="28thカテゴリ権限を同期")
        return category

    async def ensure_text(name: str, category, *, topic: str, overwrites=None, read_only=False):
        if read_only:
            overwrites = dict(overwrites or {})
            overwrites[guild.default_role] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=False,
            )
            overwrites[guild.me] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                manage_messages=True,
            )
        channel, created = await _ensure_text(
            guild,
            name,
            category,
            topic=topic,
            overwrites=overwrites,
            read_only=read_only,
        )
        counts["channels"] += int(created)
        if not created:
            edit_kwargs = {}
            if channel.topic != topic:
                edit_kwargs["topic"] = topic
            if overwrites is not None and _overwrite_signature(channel.overwrites) != _overwrite_signature(overwrites):
                edit_kwargs["overwrites"] = overwrites
            if edit_kwargs:
                await channel.edit(**edit_kwargs, reason="28thチャンネル設定を同期")
        return channel

    async def ensure_forum(name: str, category, *, topic: str, tags, overwrites=None):
        channel, created = await _ensure_forum(
            guild,
            name,
            category,
            topic=topic,
            tags=tags,
            overwrites=overwrites,
        )
        counts["channels"] += int(created)
        return channel

    async def ensure_voice(name: str, category, *, overwrites=None):
        channel, created = await _ensure_voice(guild, name, category, overwrites=overwrites)
        counts["channels"] += int(created)
        return channel

    await report("ロールを準備中")
    _member_role, created = await _ensure_role(guild, MEMBER_ROLE, color=0x2ECC71)
    counts["roles"] += int(created)
    administrator_permissions = discord.Permissions(administrator=True)
    server_admin_role, created = await _ensure_role(
        guild,
        SERVER_ADMIN_ROLE,
        color=0xE74C3C,
        permissions=administrator_permissions,
    )
    counts["roles"] += int(created)
    if not server_admin_role.permissions.administrator:
        await server_admin_role.edit(
            permissions=administrator_permissions,
            reason="サーバー管理者ロールを最上位権限へ同期",
        )
    if setup_actor is not None and server_admin_role not in setup_actor.roles:
        await setup_actor.add_roles(
            server_admin_role,
            reason="/committee_setup 実行者をサーバー管理者に登録",
        )
    bureau_roles = {}
    for name, color, _emoji, _slug in BUREAU_ROLES:
        bureau_roles[name], created = await _ensure_role(guild, name, color=color)
        counts["roles"] += int(created)
    for name in GRADE_ROLES:
        _role, created = await _ensure_role(guild, name, color=0x7289DA)
        counts["roles"] += int(created)
    position_roles = {}
    for name in POSITION_ROLES:
        position_roles[name], created = await _ensure_role(guild, name, color=0xF39C12)
        counts["roles"] += int(created)

    await report("00｜はじめに を準備中")
    start = await ensure_category("00｜はじめに")
    intro = await ensure_text(
        "はじめに",
        start,
        topic="最初に読む案内。サーバープロフィールの表示名を自分で設定してから登録を進めます。",
        read_only=True,
    )
    panel = await ensure_text(
        ROLE_PANEL_CHANNEL,
        start,
        topic="所属局・学年・慶應メールを登録し、該当者だけ役職を申告します。",
        read_only=True,
    )
    request_overwrites = _private_overwrites(
        guild,
        [server_admin_role, position_roles["執行部"]],
    )
    await ensure_text(
        ROLE_REQUEST_CHANNEL,
        start,
        topic="企画長・副局長・局長の申告と例外対応。サーバー管理者と執行部のみ閲覧できます。",
        overwrites=request_overwrites,
    )

    await report("01｜全体 を準備中")
    general = await ensure_category("01｜全体")
    await ensure_text(
        "全体チャット",
        general,
        topic="全委員が利用する通常の連絡・相談。長い話題はスレッドへまとめます。",
    )
    announcement_overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=True, send_messages=False),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_messages=True),
        position_roles["執行部"]: discord.PermissionOverwrite(view_channel=True, send_messages=True),
    }
    await ensure_text(
        "おしらせ（全体）",
        general,
        topic="全委員向けの確定事項・締切・緊急連絡。",
        overwrites=announcement_overwrites,
    )
    await ensure_forum(
        "財務に相談",
        general,
        topic="財務に関する相談を1件1投稿で管理します。",
        tags=("相談", "確認待ち", "回答済み"),
    )
    external_overwrites = dict(announcement_overwrites)
    external_overwrites[bureau_roles["渉外局"]] = discord.PermissionOverwrite(view_channel=True, send_messages=True)
    await ensure_text(
        "渉外案件ex",
        general,
        topic="協賛条件に基づく企業からの告知・お知らせ・勧誘などを一斉配信します。",
        overwrites=external_overwrites,
    )

    await report("02｜鍵管理 を準備中")
    key_category = await ensure_category(KEY_CATEGORY)
    key_action = await ensure_text(
        KEY_ACTION_CHANNEL,
        key_category,
        topic="鍵を選び、あけ・しめ・さしかえをボタンで報告します。",
        read_only=True,
    )
    key_report = await ensure_text(
        KEY_REPORT_CHANNEL,
        key_category,
        topic="現在の貸出状況だけを固定表示し、操作のたびに内容を更新します。",
        read_only=True,
    )
    key_request_overwrites = _private_overwrites(
        guild,
        [server_admin_role, bureau_roles["渉外局"], position_roles["執行部"]],
    )
    await ensure_text(
        KEY_REQUEST_CHANNEL,
        key_category,
        topic="矢上部室（209）の利用理由を確認し、承認・却下します。",
        overwrites=key_request_overwrites,
    )

    await report("03｜会議 を準備中")
    meetings = await ensure_category("03｜会議")
    await ensure_text("会議案内", meetings, topic="全体会議の日時・場所・資料を掲載します。", read_only=True)
    await ensure_forum(
        "タスク・議題",
        meetings,
        topic="全体会議の議題・決定事項・担当・期限を1件1投稿で管理します。",
        tags=("議題", "タスク", "決定", "完了"),
    )
    minutes_channel = await ensure_text(
        "議事録",
        meetings,
        topic="全体会議の生文字起こし・要約・Google Docsリンクを集約します。",
    )
    await ensure_voice("会議用🎙️", meetings)

    await report("04｜やがぽんBOT を準備中")
    bot_category = await ensure_category("04｜やがぽんBOT")
    bot_guide = await ensure_text(
        "やがぽん使い方",
        bot_category,
        topic="おしゃべりやがぽんのコマンドと運用ルール。",
        read_only=True,
    )
    await ensure_forum(
        "おしゃべりやがぽん",
        bot_category,
        topic="質問・検索・Bot活用の話題を1件1投稿で管理します。",
        tags=("質問", "検索", "使い方", "解決済み"),
    )

    leadership_overwrites = _private_overwrites(
        guild,
        [server_admin_role, position_roles["役員"], position_roles["企画長"], position_roles["執行部"]],
    )
    await report("05｜役員・企画長 を準備中")
    leadership = await ensure_category("05｜役員・企画長", overwrites=leadership_overwrites)
    officers_only = _private_overwrites(
        guild,
        [server_admin_role, position_roles["役員"], position_roles["執行部"]],
    )
    await ensure_text("役員", leadership, topic="役員向けの連絡・相談。", overwrites=officers_only)
    await ensure_text("企画長", leadership, topic="企画長向けの連絡・相談。", overwrites=leadership_overwrites)
    await ensure_text("新歓", leadership, topic="新歓に関する調整と共有。", overwrites=leadership_overwrites)
    await ensure_text("教室予約報告", leadership, topic="教室予約の申請・結果・変更を報告します。", overwrites=leadership_overwrites)

    await report("06｜役員会 を準備中")
    officers = await ensure_category("06｜役員会", overwrites=officers_only)
    await ensure_text("役員会", officers, topic="役員会の連絡・会話。", overwrites=officers_only)
    channel, created = await _ensure_thread_only(
        guild,
        "欠席連絡",
        officers,
        topic="会議ごとにスレッドを作り、欠席・遅刻をその中へ投稿します。",
        overwrites=officers_only,
    )
    counts["channels"] += int(created)
    await ensure_forum(
        "タスク・議題",
        officers,
        topic="役員会の議題・決定・タスクを1件1投稿で管理します。",
        tags=("議題", "タスク", "決定", "完了"),
        overwrites=officers_only,
    )
    await ensure_text("議事録", officers, topic="役員会の議事録を集約します。", overwrites=officers_only)
    await ensure_voice("会議用🎙️", officers, overwrites=officers_only)

    await report("07｜執行部 を準備中")
    executive_overwrites = _private_overwrites(guild, [server_admin_role, position_roles["執行部"]])
    executive = await ensure_category("07｜執行部", overwrites=executive_overwrites)
    for bureau_name, _color, _emoji, slug in BUREAU_ROLES:
        bureau_exec_overwrites = _private_overwrites(
            guild,
            [server_admin_role, position_roles["執行部"], bureau_roles[bureau_name]],
        )
        await ensure_text(
            f"{slug}＋執行部",
            executive,
            topic=f"{bureau_name}と執行部だけが閲覧できる連絡チャットです。",
            overwrites=bureau_exec_overwrites,
        )

    await report("08｜局間連携 を準備中")
    coordination = await ensure_category("08｜局間連携")
    await ensure_voice("会議用🎙️", coordination)

    await report("09｜連絡チャット（28通り）を準備中")
    liaison_category = await ensure_category("09｜連絡チャット")
    liaison_channels = []
    for first_bureau, second_bureau, channel_name in LIAISON_CHANNELS:
        liaison_overwrites = _private_overwrites(
            guild,
            [server_admin_role, position_roles["執行部"], bureau_roles[first_bureau], bureau_roles[second_bureau]],
        )
        liaison_channel = await ensure_text(
            channel_name,
            liaison_category,
            topic=f"{first_bureau}・{second_bureau}・執行部だけが閲覧できる連絡チャットです。",
            overwrites=liaison_overwrites,
        )
        liaison_channels.append((liaison_channel, first_bureau, second_bureau))

    await report("その他・アーカイブを準備中")
    await ensure_category("10｜その他")
    await ensure_category("11｜アーカイブ", overwrites=executive_overwrites)

    await report("固定案内を掲示中")
    for liaison_channel, first_bureau, second_bureau in liaison_channels:
        marker = f"yagapon-liaison-guide-v2:{first_bureau}-{second_bureau}"
        _message, created = await _post_once(
            liaison_channel,
            marker,
            content=f"`{marker}`",
            embed=_liaison_guide_embed(first_bureau, second_bureau),
        )
        counts["messages"] += int(created)
    for channel, marker, embed, view in (
        (intro, "yagapon-committee-intro-v2", _intro_embed(guild), CommitteeGuideView(guild)),
        (panel, ROLE_PANEL_MARKER, _role_panel_embed(), SelfRolePanelView()),
        (bot_guide, "yagapon-bot-guide-v2", _bot_guide_embed(guild), CommitteeGuideView(guild)),
        (key_action, KEY_PANEL_MARKER, key_panel_embed(), KeyPanelView()),
    ):
        _message, created = await _post_once(
            channel,
            marker,
            content=f"`{marker}`",
            embed=embed,
            view=view,
        )
        counts["messages"] += int(created)
    status_message, created = await _post_once(
        key_report,
        KEY_STATUS_MARKER,
        content=f"`{KEY_STATUS_MARKER}`",
        embed=key_status_embed(guild, key_store),
    )
    await asyncio.to_thread(key_store.set_status_message_id, guild.id, status_message.id)
    counts["messages"] += int(created)
    counts["minutes_channel_id"] = minutes_channel.id
    return counts


def register_persistent_views(bot) -> None:
    bot.add_view(SelfRolePanelView())
    bot.add_view(PostRegistrationRoleView())
    bot.add_view(RoleApprovalView())
    bot.add_view(SupportLinkApprovalView())
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
                ("管理者", permissions.administrator),
                ("ロール管理", permissions.manage_roles),
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
        managed_role_names = {
            MEMBER_ROLE,
            SERVER_ADMIN_ROLE,
            *GRADE_ROLES,
            *POSITION_ROLES,
            *_role_names(BUREAU_ROLES),
        }
        hierarchy_conflicts = list(
            dict.fromkeys(
                role.name
                for role in ctx.guild.roles
                if role.name in managed_role_names and not me.top_role > role
            )
        )
        if hierarchy_conflicts:
            omitted = len(hierarchy_conflicts) - 8
            omitted_label = f"（他 {omitted} 件）" if omitted > 0 else ""
            await ctx.respond(
                "やがぽんのロールを、委員長・執行部・各局などすべての管理対象ロールより上に移動してほしいぽん。"
                f" 現在管理できないロール: {', '.join(hierarchy_conflicts[:8])}{omitted_label}",
                ephemeral=True,
            )
            return
        await ctx.defer(ephemeral=True)
        progress_message = await ctx.followup.send(
            "⏳ 委員会サーバーの整備を開始したぽん…",
            ephemeral=True,
            wait=True,
        )
        current_stage = "開始準備"

        async def report_progress(stage: str) -> None:
            nonlocal current_stage
            current_stage = stage
            try:
                await progress_message.edit(content=f"⏳ {stage}だぽん…")
            except discord.HTTPException:
                log.warning("Could not update committee setup progress guild=%s", ctx.guild_id)

        try:
            counts = await bootstrap_committee_server(
                ctx.guild,
                key_store=bot.key_management.store,
                setup_actor=ctx.author,
                report_progress=report_progress,
            )
        except discord.Forbidden as exc:
            log.exception("Committee setup permission error guild=%s", ctx.guild_id)
            await progress_message.edit(
                content=(
                    f"❌ **{current_stage}**で権限エラーになったぽん。"
                    "やがぽんに管理者権限を付け、ロールをすべての管理対象ロールより上にしてほしいぽん。"
                    f" status={exc.status} / code={exc.code}"
                )
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
        minutes_channel = ctx.guild.get_channel(counts["minutes_channel_id"])
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

    @bot.slash_command(name="committee_roles", description="登録後に企画長・役員を申告するぽん")
    async def committee_roles(ctx: discord.ApplicationContext):
        guild = ctx.guild
        member = ctx.author
        member_role = _find_named(guild.roles, MEMBER_ROLE) if guild else None
        if not guild or not isinstance(member, discord.Member):
            await ctx.respond("サーバー内で実行してほしいぽん。", ephemeral=True)
            return
        if not _has_completed_registration(bot.config, guild.id, member, member_role):
            await ctx.respond(
                "先にロール選択パネルから所属局・学年・慶應メールの登録を完了してほしいぽん。",
                ephemeral=True,
            )
            return
        embed = discord.Embed(
            title="🛡️ 登録後の役職申告",
            description=(
                "該当するものだけ申告してください。\n"
                "**企画長**と**役員（副局長・局長）**は兼任できます。"
            ),
            color=discord.Color.gold(),
        )
        await ctx.respond(embed=embed, view=PostRegistrationRoleView(), ephemeral=True)

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
