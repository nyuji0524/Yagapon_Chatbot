"""28th committee registration and persistent role controls."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import unicodedata
import uuid
from collections.abc import Iterable
from itertools import combinations

import discord

from bot.authorization import require_guild_admin
from bot.committee_backend import CommitteeBackendError
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
GRADE_ROLES = ("１年生", "２年生", "３年生")
LEGACY_GRADE_ROLES = {
    "1年": "１年生",
    "2年": "２年生",
    "3年": "３年生",
}
POSITION_ROLES = ("企画長", "役員", "副局長", "局長", "執行部")
DECLARABLE_POSITIONS = ("企画長", "副局長", "局長")
OFFICER_POSITIONS = ("副局長", "局長")
POSITION_ROLE_ORDER = ("執行部", "局長", "副局長", "企画長", "役員")
SERVER_ADMIN_ROLE = "サーバー管理者"
ROLE_REQUEST_CHANNEL = "ロール申請🔑"
ROLE_PANEL_CHANNEL = "ロール選択"
ROLE_PANEL_MARKER = "yagapon-role-panel-v1"
COMMITTEE_COLOR = 0xF2A900
YAGA_SUPPORT_URL = "https://yagasupport.yagamifes.com"


def _role_names(entries: Iterable[tuple[str, int, str, str]]) -> tuple[str, ...]:
    return tuple(entry[0] for entry in entries)


def _find_named(items, name: str):
    return discord.utils.get(items, name=name)


def _find_in_category(items, name: str, category):
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


async def _update_saved_registration_selection(
    config,
    guild_id: int,
    member_id: int,
    *,
    field: str,
    value: str,
) -> None:
    """Keep the local registration record aligned with a changed Discord role."""
    members = config.get_members(guild_id)
    existing = members.get(str(member_id))
    if not isinstance(existing, dict) or existing.get(field) == value:
        return
    members[str(member_id)] = {**existing, field: value}
    await config.set_members(guild_id, members)


def _position_role_names(roles: Iterable[discord.Role]) -> list[str]:
    names = {role.name for role in roles}
    return [name for name in POSITION_ROLE_ORDER if name in names]


def _position_role_summary(roles: Iterable[discord.Role]) -> str:
    return ", ".join(_position_role_names(roles)) or "なし"


def _committee_registration_mode() -> str:
    """Return otp, firebase_email_only, or local with legacy compatibility."""
    configured = os.getenv("YAGAPON_COMMITTEE_REGISTRATION_MODE", "").strip().lower()
    if configured in {"otp", "firebase_email_only", "local"}:
        return configured
    value = os.getenv("YAGAPON_COMMITTEE_EMAIL_VERIFICATION_ENABLED", "true")
    return "otp" if value.strip().lower() not in {"0", "false", "no", "off"} else "local"


def _committee_email_verification_enabled() -> bool:
    """Return whether OTP verification is active."""
    return _committee_registration_mode() == "otp"


def _committee_firebase_registration_enabled() -> bool:
    """Return whether committee registration and applications use Firebase."""
    return _committee_registration_mode() in {"otp", "firebase_email_only"}


def _has_completed_registration(config, guild_id: int, member) -> bool:
    registration = config.get_member(guild_id, member.id)
    if not registration or not registration.get("bureau") or not registration.get("grade"):
        return False
    if registration.get("registration_completed") is True:
        return bool(registration.get("email"))
    return bool(registration.get("email_verified") is True and registration.get("email"))


def _bot_can_run_committee_setup(member) -> bool:
    """Whether the bot can create and grant an Administrator role."""
    permissions = getattr(member, "guild_permissions", None)
    return bool(permissions and permissions.administrator)


def _roles_after_position_approval(
    roles: Iterable[discord.Role],
    approved_role: discord.Role,
    officer_role: discord.Role | None = None,
) -> list[discord.Role]:
    """Preserve project-leader and officer roles independently.

    副局長と局長だけは同時に成立しないため、役員申告の承認時に相互置換する。
    企画長は役員と兼任できる。
    """
    # Remove the two retired common roles when an existing member is approved.
    removable_names = {"一般委員", "28th委員"}
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
    email_verification_enabled = _committee_email_verification_enabled()
    firebase_registration_enabled = _committee_firebase_registration_enabled()
    completion_step = (
        "**3. 「登録を完了」から慶應メールを入力**"
        if email_verification_enabled
        else "**3. 「登録を完了」から慶應メールを入力（確認コードなし）**"
    )
    registration_summary = "**所属局・学年 → 慶應メール**"
    embed = discord.Embed(
        title="🎫 委員プロフィールを設定",
        description=(
            "次の順番で登録してください。\n"
            "**1. Discordのサーバープロフィールから表示名を自分で変更**\n"
            "**2. 所属局と学年を選択**\n"
            f"{completion_step}\n\n"
            "ご自身でサーバーの表示名を本名に変更して下さい。"
        ),
        color=discord.Color(COMMITTEE_COLOR),
    )
    embed.add_field(name="1️⃣ 登録時", value=registration_summary, inline=True)
    if firebase_registration_enabled and not email_verification_enabled:
        embed.add_field(
            name="Firebase連携",
            value="確認コードは使用せず、申告内容とやがサポ照合結果を保存します。",
            inline=False,
        )
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
        await interaction.response.defer(ephemeral=True)
        try:
            await _replace_role_group(member, role, _role_names(BUREAU_ROLES))
        except discord.Forbidden:
            await _respond_ephemeral(interaction, "ロールを付ける権限が足りないぽん。やがぽんのロールを上に移動してほしいぽん。")
            return
        except discord.HTTPException:
            log.exception("Failed to replace bureau role guild=%s member=%s", guild.id, member.id)
            await _respond_ephemeral(interaction, "所属局を変更できなかったぽん。少し待ってからもう一度試してほしいぽん。")
            return
        await _update_saved_registration_selection(
            interaction.client.config,
            guild.id,
            member.id,
            field="bureau",
            value=role.name,
        )
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
        await interaction.response.defer(ephemeral=True)
        try:
            await _replace_role_group(member, role, GRADE_ROLES)
        except discord.Forbidden:
            await _respond_ephemeral(interaction, "ロールを付ける権限が足りないぽん。やがぽんのロールを上に移動してほしいぽん。")
            return
        except discord.HTTPException:
            log.exception("Failed to replace grade role guild=%s member=%s", guild.id, member.id)
            await _respond_ephemeral(interaction, "学年を変更できなかったぽん。少し待ってからもう一度試してほしいぽん。")
            return
        await _update_saved_registration_selection(
            interaction.client.config,
            guild.id,
            member.id,
            field="grade",
            value=role.name,
        )
        await _respond_ephemeral(interaction, f"✅ 学年を **{role.name}** に設定したぽん！")


async def _submit_position_request(
    interaction: discord.Interaction,
    role_name: str,
    *,
    application_kind: str,
    organization_id: str | None = None,
    organization_name: str | None = None,
    organization_not_listed: bool = False,
) -> None:
    guild = interaction.guild
    member = interaction.user
    role = _find_named(guild.roles, role_name) if guild else None
    request_channel = _find_named(guild.text_channels, ROLE_REQUEST_CHANNEL) if guild else None
    if not guild or not isinstance(member, discord.Member) or role is None or request_channel is None:
        await _respond_ephemeral(interaction, "役職申請の設定が見つからないぽん。管理者に再セットアップを依頼してほしいぽん。")
        return
    firebase_registration_enabled = _committee_firebase_registration_enabled()
    if not _has_completed_registration(interaction.client.config, guild.id, member):
        requirement = "所属局・学年・慶應メール"
        await _respond_ephemeral(interaction, f"先に{requirement}の登録を完了してほしいぽん。")
        return
    if role in member.roles:
        await _respond_ephemeral(interaction, f"**{role.name}** は既に付いているぽん！")
        return
    await interaction.response.defer(ephemeral=True)
    if firebase_registration_enabled:
        kind = "project_leader" if role_name == "企画長" else "officer"
        try:
            result = await interaction.client.committee_backend.create_application(
                guildId=str(guild.id),
                discordUserId=str(member.id),
                applicantDisplayName=member.display_name,
                kind=kind,
                role=role_name,
                organizationId=organization_id,
                organizationNotListed=organization_not_listed,
                manualReason="選択肢に企画がないため手動確認" if organization_not_listed else None,
            )
        except CommitteeBackendError as exc:
            log.warning(
                "Committee role application failed guild=%s member=%s status=%s",
                guild.id,
                member.id,
                exc.status,
            )
            await interaction.followup.send(
                "役職申請を保存できなかったぽん。メール認証が完了しているか確認し、"
                "少し待ってからもう一度試してほしいぽん。",
                ephemeral=True,
            )
            return
        application_id = str(result["applicationId"])
    else:
        application_id = f"local-{uuid.uuid4()}"
    embed = discord.Embed(
        title=f"{application_kind}申請",
        description=f"{member.mention} が **{role.name}** を申請しています。",
        color=discord.Color.gold(),
    )
    embed.add_field(name="申請者ID", value=str(member.id), inline=True)
    embed.add_field(name="役職ロールID", value=str(role.id), inline=True)
    embed.add_field(name="申請種別", value=application_kind, inline=True)
    embed.add_field(name="申請ID", value=application_id, inline=False)
    if not firebase_registration_enabled:
        embed.add_field(name="処理方式", value="Discord内の暫定申請", inline=False)
    if role_name == "企画長":
        embed.add_field(
            name="担当企画",
            value=organization_name or "選択肢にない（手動確認）",
            inline=False,
        )
    embed.set_footer(text="企画長と役員は兼任できます。副局長と局長は選択した方へ置き換わります")
    await request_channel.send(embed=embed, view=RoleApprovalView(), silent=True)
    await interaction.followup.send(
        f"📨 **{role.name}** を申請したぽん。管理者の承認を待ってねぽん！",
        ephemeral=True,
    )


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
        guild = interaction.guild
        member = interaction.user
        if not guild or not isinstance(member, discord.Member):
            await _respond_ephemeral(interaction, "サーバー内で実行してほしいぽん。")
            return
        if not _has_completed_registration(interaction.client.config, guild.id, member):
            await _respond_ephemeral(interaction, "先に委員プロフィールの登録を完了してほしいぽん。")
            return
        if not _committee_firebase_registration_enabled():
            await interaction.response.send_modal(ManualProjectLeaderModal())
            return
        await interaction.response.defer(ephemeral=True)
        try:
            organizations = await interaction.client.committee_backend.list_organizations(
                guild_id=guild.id,
                discord_user_id=member.id,
            )
        except CommitteeBackendError:
            await interaction.followup.send(
                "企画一覧を取得できなかったぽん。少し待ってからもう一度試してほしいぽん。",
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            "担当する企画を選んでほしいぽん。見つからない場合は最後の選択肢を選んでねぽん。",
            view=ProjectOrganizationView(organizations),
            ephemeral=True,
        )


class ManualProjectLeaderModal(discord.ui.Modal):
    def __init__(self):
        super().__init__(title="企画長を申告")
        self.add_item(
            discord.ui.InputText(
                label="担当企画名",
                placeholder="例：ITラボ",
                min_length=1,
                max_length=100,
            )
        )

    async def callback(self, interaction: discord.Interaction):
        await _submit_position_request(
            interaction,
            "企画長",
            application_kind="企画長",
            organization_name=self.children[0].value.strip(),
            organization_not_listed=True,
        )


class ProjectOrganizationSelect(discord.ui.Select):
    def __init__(self, organizations):
        organizations = sorted(
            organizations,
            key=lambda item: str(item.get("name") or ""),
        )
        self._organizations = {
            str(item["id"]): str(item["name"])
            for item in organizations[:24]
            if item.get("id") and item.get("name")
        }
        options = [
            discord.SelectOption(label=name[:100], value=organization_id)
            for organization_id, name in self._organizations.items()
        ]
        options.append(
            discord.SelectOption(
                label="この中にない",
                value="__not_listed__",
                description="新企画や登録漏れなど",
                emoji="🔑",
            )
        )
        super().__init__(
            placeholder="担当企画を選択",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        selected = self.values[0]
        not_listed = selected == "__not_listed__"
        await _submit_position_request(
            interaction,
            "企画長",
            application_kind="企画長",
            organization_id=None if not_listed else selected,
            organization_name=None if not_listed else self._organizations[selected],
            organization_not_listed=not_listed,
        )


class ProjectOrganizationView(discord.ui.View):
    def __init__(self, organizations):
        super().__init__(timeout=10 * 60)
        self.add_item(ProjectOrganizationSelect(organizations))


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

        registration_mode = _committee_registration_mode()
        if registration_mode == "local":
            await _complete_registration_without_verification(
                interaction,
                email=email,
                bureau=bureau,
                grade=grade,
            )
            return

        if registration_mode == "firebase_email_only":
            await _complete_firebase_email_only_registration(
                interaction,
                email=email,
                bureau=bureau,
                grade=grade,
            )
            return

        await interaction.response.defer(ephemeral=True)
        try:
            await interaction.client.committee_backend.request_code(
                guild_id=guild.id,
                discord_user_id=member.id,
                email=email,
                bureau=bureau,
                grade=grade,
                display_name=member.display_name,
            )
        except CommitteeBackendError as exc:
            log.warning(
                "Committee email code request failed guild=%s member=%s status=%s",
                guild.id,
                member.id,
                exc.status,
            )
            message = (
                "確認コードは直前にも送信されているぽん。1分ほど待ってメールを確認してほしいぽん。"
                if exc.status == 429
                else "確認コードを送信できなかったぽん。少し待ってからもう一度試してほしいぽん。"
            )
            await interaction.followup.send(message, ephemeral=True)
            return

        config = interaction.client.config
        members = config.get_members(guild.id)
        existing_record = members.get(str(member.id), {})
        members[str(member.id)] = {
            **existing_record,
            "name": member.display_name,
            "email": email,
            "bureau": bureau,
            "grade": grade,
            "email_verified": False,
            "registration_completed": False,
            "registration_mode": "email_verification_pending",
            "role": _position_role_summary(member.roles),
            "roles": _position_role_names(member.roles),
        }
        await config.set_members(guild.id, members)
        verify_view = EmailVerificationView(
            guild_id=guild.id,
            member_id=member.id,
            email=email,
        )
        try:
            await member.send(
                "📨 慶應メールへ6桁の確認コードを送信したぽん。"
                "10分以内に下のボタンから入力してほしいぽん。",
                view=verify_view,
            )
            await interaction.followup.send(
                "📨 確認コードを送信したぽん。DMを確認してほしいぽん。",
                ephemeral=True,
            )
        except discord.HTTPException:
            await interaction.followup.send(
                "DMを送れなかったため、ここから確認コードを入力してほしいぽん。",
                view=verify_view,
                ephemeral=True,
            )


class EmailCodeModal(discord.ui.Modal):
    def __init__(self, *, guild_id: int, member_id: int, email: str):
        super().__init__(title="慶應メールを確認", timeout=10 * 60)
        self.guild_id = guild_id
        self.member_id = member_id
        self.email = email
        self.add_item(
            discord.ui.InputText(
                label="6桁の確認コード",
                placeholder="123456",
                min_length=6,
                max_length=6,
            )
        )

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.member_id:
            await _respond_ephemeral(interaction, "この確認コードは登録した本人だけが入力できるぽん。")
            return
        code = unicodedata.normalize("NFKC", self.children[0].value).strip()
        await interaction.response.defer(ephemeral=True)
        try:
            result = await interaction.client.committee_backend.verify_code(
                guild_id=self.guild_id,
                discord_user_id=self.member_id,
                code=code,
                display_name=getattr(interaction.user, "display_name", None),
            )
        except CommitteeBackendError as exc:
            message = {
                400: "確認コードが違うぽん。メールを確認してもう一度入力してほしいぽん。",
                410: "確認コードの有効期限が切れたぽん。サーバーで登録をやり直してほしいぽん。",
                429: "入力回数が上限に達したぽん。登録をやり直してほしいぽん。",
            }.get(exc.status, "メール認証に失敗したぽん。少し待ってからもう一度試してほしいぽん。")
            await interaction.followup.send(message, ephemeral=True)
            return
        guild = interaction.client.get_guild(self.guild_id)
        member = guild.get_member(self.member_id) if guild else None
        if member is None and guild is not None:
            try:
                member = await guild.fetch_member(self.member_id)
            except (discord.NotFound, discord.HTTPException):
                member = None
        if member is None:
            await interaction.followup.send("登録したサーバーに利用者が見つからないぽん。", ephemeral=True)
            return
        members = interaction.client.config.get_members(self.guild_id)
        existing = members.get(str(self.member_id), {})
        existing.update(
            {
                "name": member.display_name,
                "email": result["email"],
                "bureau": result["bureau"],
                "grade": result["grade"],
                "email_verified": True,
                "registration_completed": True,
                "registration_mode": "email_verified",
                "support_user_exists": result.get("userExists") is True,
                "support_staff_info_exists": result.get("staffInfoExists") is True,
                "support_uid": result.get("userUid"),
                "support_departments": result.get("userDepartments") or [],
                "support_status": "verified" if result.get("userExists") else "not_found",
                "role": _position_role_summary(member.roles),
                "roles": _position_role_names(member.roles),
            }
        )
        members[str(self.member_id)] = existing
        await interaction.client.config.set_members(self.guild_id, members)
        synced_roles = await _sync_pending_local_roles(interaction.client, guild, member)
        support_line = (
            f"✅ users登録あり（UID: `{result.get('userUid')}`）"
            if result.get("userExists")
            else f"⚠️ users登録なし。先に {YAGA_SUPPORT_URL} へサインインしてほしいぽん。"
        )
        await interaction.followup.send(
            "\n".join(
                [
                    "✅ 委員プロフィールの登録が完了したぽん！",
                    f"表示名: **{member.display_name}**",
                    f"所属: **{result['bureau']} / {result['grade']}**",
                    f"やがサポ: {support_line}",
                    (
                        f"暫定期間の承認済み役職 {synced_roles}件をFirebaseへ移行したぽん。"
                        if synced_roles
                        else "暫定期間の役職同期が残っている場合は、Botが自動で再試行するぽん。"
                    ),
                    "表示名はDiscordのサーバープロフィールから本人が変更してねぽん。",
                    "企画長・副局長・局長に該当する場合は、サーバーで `/committee_roles` を実行してほしいぽん。",
                ]
            ),
            ephemeral=True,
        )


class EmailVerificationView(discord.ui.View):
    def __init__(self, *, guild_id: int, member_id: int, email: str):
        super().__init__(timeout=10 * 60)
        self.guild_id = guild_id
        self.member_id = member_id
        self.email = email

    @discord.ui.button(label="確認コードを入力", emoji="✉️", style=discord.ButtonStyle.primary)
    async def verify(self, _button: discord.ui.Button, interaction: discord.Interaction):
        if interaction.user.id != self.member_id:
            await _respond_ephemeral(interaction, "登録した本人だけが入力できるぽん。")
            return
        await interaction.response.send_modal(
            EmailCodeModal(
                guild_id=self.guild_id,
                member_id=self.member_id,
                email=self.email,
            )
        )


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


async def _complete_registration_without_verification(
    interaction: discord.Interaction,
    *,
    email: str,
    bureau: str,
    grade: str,
) -> None:
    guild = interaction.guild
    member = interaction.user
    if not guild or not isinstance(member, discord.Member):
        await _respond_ephemeral(interaction, "サーバー内で実行してほしいぽん。")
        return
    await interaction.response.defer(ephemeral=True)
    config = interaction.client.config
    members = config.get_members(guild.id)
    existing = members.get(str(member.id), {})
    members[str(member.id)] = {
        **existing,
        "name": member.display_name,
        "email": email,
        "bureau": bureau,
        "grade": grade,
        "email_verified": False,
        "registration_completed": True,
        "registration_mode": "email_collected_verification_disabled",
        "support_status": "not_checked",
        "role": _position_role_summary(member.roles),
        "roles": _position_role_names(member.roles),
    }
    await config.set_members(guild.id, members)
    await interaction.followup.send(
        "\n".join(
            [
                "✅ 委員プロフィールの登録が完了したぽん！",
                f"表示名: **{member.display_name}**",
                f"所属: **{bureau} / {grade}**",
                f"慶應メール: **{email}**",
                "現在は確認コードを使用していないため、メール確認とやがサポ照合は未実施だぽん。",
                "企画長・副局長・局長に該当する場合は、下から申告できるぽん。",
            ]
        ),
        view=PostRegistrationRoleView(),
        ephemeral=True,
    )


async def _complete_firebase_email_only_registration(
    interaction: discord.Interaction,
    *,
    email: str,
    bureau: str,
    grade: str,
) -> None:
    guild = interaction.guild
    member = interaction.user
    if not guild or not isinstance(member, discord.Member):
        await _respond_ephemeral(interaction, "サーバー内で実行してほしいぽん。")
        return
    await interaction.response.defer(ephemeral=True)
    try:
        result = await interaction.client.committee_backend.register_email_only(
            guild_id=guild.id,
            discord_user_id=member.id,
            email=email,
            bureau=bureau,
            grade=grade,
            display_name=member.display_name,
        )
    except CommitteeBackendError as exc:
        log.warning(
            "Committee email-only registration failed guild=%s member=%s status=%s",
            guild.id,
            member.id,
            exc.status,
        )
        await interaction.followup.send(
            "Firebaseへ委員登録を保存できなかったぽん。少し待ってからもう一度試してほしいぽん。",
            ephemeral=True,
        )
        return

    config = interaction.client.config
    members = config.get_members(guild.id)
    existing = members.get(str(member.id), {})
    members[str(member.id)] = {
        **existing,
        "name": member.display_name,
        "email": result["email"],
        "bureau": result["bureau"],
        "grade": result["grade"],
        "email_verified": False,
        "email_ownership_verified": False,
        "registration_completed": True,
        "registration_mode": "firebase_email_only",
        "firebase_registration_synced": True,
        "support_user_exists": result.get("userExists") is True,
        "support_staff_info_exists": result.get("staffInfoExists") is True,
        "support_uid": result.get("userUid"),
        "support_departments": result.get("userDepartments") or [],
        "support_status": "matched" if result.get("userExists") else "not_found",
        "role": _position_role_summary(member.roles),
        "roles": _position_role_names(member.roles),
    }
    await config.set_members(guild.id, members)
    synced_roles = await _sync_pending_local_roles(interaction.client, guild, member)
    support_line = (
        "users登録あり"
        if result.get("userExists")
        else f"users登録なし。後から {YAGA_SUPPORT_URL} へサインインできるぽん。"
    )
    await interaction.followup.send(
        "\n".join(
            [
                "✅ 委員プロフィールをFirebaseへ保存したぽん！",
                f"表示名: **{member.display_name}**",
                f"所属: **{result['bureau']} / {result['grade']}**",
                f"やがサポ: {support_line}",
                (
                    f"暫定期間の承認済み役職 {synced_roles}件をFirebaseへ移行したぽん。"
                    if synced_roles
                    else "企画長・副局長・局長に該当する場合は、下から申告できるぽん。"
                ),
            ]
        ),
        view=PostRegistrationRoleView(),
        ephemeral=True,
    )


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


def _queue_local_role_for_firebase(
    record: dict,
    *,
    application_id: str,
    role_name: str,
    organization_name: str | None,
    approved_by: int,
) -> None:
    queue = record.setdefault("firebase_role_sync_queue", [])
    if any(item.get("local_application_id") == application_id for item in queue):
        return
    queue.append(
        {
            "local_application_id": application_id,
            "role": role_name,
            "kind": "project_leader" if role_name == "企画長" else "officer",
            "organization_name": organization_name,
            "organization_not_listed": role_name == "企画長",
            "approved_by_discord_id": str(approved_by),
            "status": "pending_registration",
        }
    )


async def _sync_pending_local_roles(bot, guild: discord.Guild, member: discord.Member) -> int:
    """Replay locally approved roles after Firebase registration completes."""
    members = bot.config.get_members(guild.id)
    record = members.get(str(member.id), {})
    if record.get("email_verified") is not True and record.get("registration_completed") is not True:
        return 0
    queue = record.get("firebase_role_sync_queue")
    if not isinstance(queue, list):
        return 0
    synced = 0
    for item in queue:
        if not isinstance(item, dict) or item.get("status") == "synced":
            continue
        try:
            backend_application_id = item.get("backend_application_id")
            if not backend_application_id:
                organization_id = None
                organization_not_listed = item.get("organization_not_listed") is True
                if item.get("kind") == "project_leader" and item.get("organization_name"):
                    organizations = await bot.committee_backend.list_organizations(
                        guild_id=guild.id,
                        discord_user_id=member.id,
                    )
                    matches = [
                        organization
                        for organization in organizations
                        if str(organization.get("name") or "").strip()
                        == str(item["organization_name"]).strip()
                    ]
                    if len(matches) == 1:
                        organization_id = str(matches[0]["id"])
                        organization_not_listed = False
                result = await bot.committee_backend.create_application(
                    guildId=str(guild.id),
                    discordUserId=str(member.id),
                    applicantDisplayName=member.display_name,
                    kind=item["kind"],
                    role=item["role"],
                    organizationId=organization_id,
                    organizationNotListed=organization_not_listed,
                    manualReason=(
                        f"メール認証前のDiscord承認を移行: {item.get('organization_name') or '企画名未登録'}"
                        if organization_not_listed
                        else None
                    ),
                )
                backend_application_id = str(result["applicationId"])
                item["backend_application_id"] = backend_application_id
                item["status"] = "created"
                members[str(member.id)] = record
                await bot.config.set_members(guild.id, members)
            if item.get("status") != "decided":
                await bot.committee_backend.decide_application(
                    application_id=backend_application_id,
                    approved=True,
                    actor_discord_id=int(item["approved_by_discord_id"]),
                )
                item["status"] = "decided"
                members[str(member.id)] = record
                await bot.config.set_members(guild.id, members)
            await bot.committee_backend.acknowledge_decision(
                guild_id=guild.id,
                application_id=backend_application_id,
            )
            item["status"] = "synced"
            item.pop("last_error_status", None)
            synced += 1
        except (CommitteeBackendError, KeyError, TypeError, ValueError) as exc:
            item["last_error_status"] = getattr(exc, "status", "invalid_local_record")
            log.warning(
                "Deferred committee role sync failed guild=%s member=%s local_application=%s status=%s",
                guild.id,
                member.id,
                item.get("local_application_id"),
                item["last_error_status"],
            )
        members[str(member.id)] = record
        await bot.config.set_members(guild.id, members)
    return synced


async def _sync_email_only_registration(
    bot,
    guild: discord.Guild,
    member: discord.Member,
    record: dict,
) -> bool:
    """Backfill a registration collected during the previous local-only period."""
    if record.get("firebase_registration_synced") is True or record.get("email_verified") is True:
        return False
    email = str(record.get("email") or "").strip().lower()
    bureau = str(record.get("bureau") or "").strip()
    grade = str(record.get("grade") or "").strip()
    if not _is_keio_email(email) or bureau not in _role_names(BUREAU_ROLES) or grade not in GRADE_ROLES:
        return False
    result = await bot.committee_backend.register_email_only(
        guild_id=guild.id,
        discord_user_id=member.id,
        email=email,
        bureau=bureau,
        grade=grade,
        display_name=member.display_name,
    )
    record.update(
        {
            "name": member.display_name,
            "email": result["email"],
            "bureau": result["bureau"],
            "grade": result["grade"],
            "email_verified": False,
            "email_ownership_verified": False,
            "registration_completed": True,
            "registration_mode": "firebase_email_only",
            "firebase_registration_synced": True,
            "support_user_exists": result.get("userExists") is True,
            "support_staff_info_exists": result.get("staffInfoExists") is True,
            "support_uid": result.get("userUid"),
            "support_departments": result.get("userDepartments") or [],
            "support_status": "matched" if result.get("userExists") else "not_found",
        }
    )
    members = bot.config.get_members(guild.id)
    members[str(member.id)] = record
    await bot.config.set_members(guild.id, members)
    return True


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
    application_id = _embed_field_value(embed, "申請ID") if embed else None
    application_mode = _embed_field_value(embed, "処理方式") if embed else None
    is_local_application = bool(
        application_id
        and application_id.startswith("local-")
        and application_mode == "Discord内の暫定申請"
    )
    member = guild.get_member(member_id) if guild and member_id else None
    if member is None and guild and member_id:
        try:
            member = await guild.fetch_member(member_id)
        except (discord.NotFound, discord.HTTPException):
            member = None
    role = guild.get_role(role_id) if guild and role_id else None
    if (
        not embed
        or member is None
        or role is None
        or role.name not in DECLARABLE_POSITIONS
        or not application_id
    ):
        await _respond_ephemeral(interaction, "申請データを確認できなかったぽん。")
        return
    officer_role = None
    if approve and role.name in OFFICER_POSITIONS:
        officer_role = _find_named(guild.roles, "役員")
        if officer_role is None:
            await _respond_ephemeral(
                interaction,
                "役員ロールが見つからないぽん。管理者がロールを作成してから再度承認してほしいぽん。",
            )
            return
    await interaction.response.defer(ephemeral=True)
    if application_id.startswith("local-") and not is_local_application:
        await interaction.followup.send("暫定申請の確認情報が一致しないぽん。", ephemeral=True)
        return
    if not is_local_application:
        try:
            await interaction.client.committee_backend.decide_application(
                application_id=application_id,
                approved=approve,
                actor_discord_id=interaction.user.id,
            )
        except CommitteeBackendError as exc:
            await interaction.followup.send(
                "申請状態を更新できなかったぽん。すでに処理済みか、通信に失敗した可能性があるぽん。",
                ephemeral=True,
            )
            log.warning("Committee decision failed id=%s status=%s", application_id, exc.status)
            return
    if approve:
        try:
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
            if is_local_application:
                _queue_local_role_for_firebase(
                    record,
                    application_id=application_id,
                    role_name=role.name,
                    organization_name=_embed_field_value(embed, "担当企画"),
                    approved_by=interaction.user.id,
                )
            members[str(member.id)] = record
            await interaction.client.config.set_members(guild.id, members)
        except discord.Forbidden:
            await interaction.followup.send(
                "申請は承認済みだけれどDiscordロールを付けられなかったぽん。"
                "やがぽんのロール順を直すと自動同期で再試行するぽん。",
                ephemeral=True,
            )
            return
        embed.color = discord.Color.green()
        embed.add_field(name="結果", value=f"✅ {interaction.user.mention} が承認", inline=False)
    else:
        embed.color = discord.Color.red()
        embed.add_field(name="結果", value=f"❌ {interaction.user.mention} が却下", inline=False)
    if interaction.message is not None:
        await interaction.message.edit(embed=embed, view=None)
    if not is_local_application:
        try:
            await interaction.client.committee_backend.acknowledge_decision(
                guild_id=guild.id,
                application_id=application_id,
            )
        except CommitteeBackendError:
            log.warning("Could not acknowledge Discord role decision id=%s", application_id)
    await interaction.followup.send("申請を処理したぽん。", ephemeral=True)
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


async def _sync_role_decision(bot, guild: discord.Guild, application: dict) -> None:
    application_id = str(application.get("id") or "")
    try:
        member_id = int(application.get("discordUserId") or 0)
    except (TypeError, ValueError):
        member_id = 0
    status = application.get("status")
    role_name = str(application.get("role") or "")
    if not application_id or member_id <= 0 or status not in {"approved", "rejected"}:
        log.error("Ignoring malformed committee role decision id=%s", application_id)
        if application_id:
            await bot.committee_backend.acknowledge_decision(
                guild_id=guild.id,
                application_id=application_id,
            )
        return
    member = guild.get_member(member_id)
    if member is None:
        try:
            member = await guild.fetch_member(member_id)
        except discord.NotFound:
            log.warning(
                "Committee applicant left guild=%s member=%s application=%s",
                guild.id,
                member_id,
                application_id,
            )
            await bot.committee_backend.acknowledge_decision(
                guild_id=guild.id,
                application_id=application_id,
            )
            return
    if status == "approved":
        role = _find_named(guild.roles, role_name)
        if role is None or role.name not in DECLARABLE_POSITIONS:
            raise RuntimeError(f"Discord role is missing: {role_name}")
        officer_role = _find_named(guild.roles, "役員") if role_name in OFFICER_POSITIONS else None
        if role_name in OFFICER_POSITIONS and officer_role is None:
            raise RuntimeError("Discord role is missing: 役員")
        desired_roles = _roles_after_position_approval(member.roles, role, officer_role)
        await member.edit(roles=desired_roles, reason="やがサポで承認された役職を同期")
        members = bot.config.get_members(guild.id)
        record = members.get(str(member.id), {})
        record["role"] = _position_role_summary(desired_roles)
        record["roles"] = _position_role_names(desired_roles)
        members[str(member.id)] = record
        await bot.config.set_members(guild.id, members)
    await bot.committee_backend.acknowledge_decision(
        guild_id=guild.id,
        application_id=application_id,
    )
    try:
        result = "承認" if status == "approved" else "却下"
        await member.send(f"申請した **{role_name}** ロールは{result}されました。")
    except discord.HTTPException:
        pass


async def committee_role_sync_loop(bot) -> None:
    """Apply Yaga Support decisions to Discord with idempotent acknowledgement."""
    while not bot.is_closed():
        if not _committee_firebase_registration_enabled():
            await asyncio.sleep(45)
            continue
        for guild in bot.guilds:
            # The bot also serves IT and test guilds.  Only a server prepared
            # for committee registration has both the role panel and managed
            # committee roles, so avoid a paid Functions poll everywhere else.
            if (
                _find_named(guild.text_channels, ROLE_PANEL_CHANNEL) is None
                or not any(_find_named(guild.roles, name) for name in GRADE_ROLES)
            ):
                continue
            try:
                registration_records = bot.config.get_members(guild.id)
                for member_id, record in list(registration_records.items()):
                    if not isinstance(record, dict):
                        continue
                    try:
                        member = guild.get_member(int(member_id))
                    except (TypeError, ValueError):
                        member = None
                    if member is None:
                        continue
                    if _committee_registration_mode() == "firebase_email_only":
                        try:
                            await _sync_email_only_registration(bot, guild, member, record)
                        except CommitteeBackendError as exc:
                            log.warning(
                                "Committee registration backfill failed guild=%s member=%s status=%s",
                                guild.id,
                                member.id,
                                exc.status,
                            )
                            continue
                    queue = record.get("firebase_role_sync_queue")
                    if (
                        record.get("email_verified") is not True
                        and record.get("registration_completed") is not True
                    ) or not isinstance(queue, list):
                        continue
                    if any(
                        isinstance(item, dict) and item.get("status") != "synced"
                        for item in queue
                    ):
                        await _sync_pending_local_roles(bot, guild, member)
                decisions = await bot.committee_backend.list_decisions(guild.id)
                for application in decisions:
                    try:
                        await _sync_role_decision(bot, guild, application)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        log.exception(
                            "Committee decision sync failed guild=%s application=%s",
                            guild.id,
                            application.get("id"),
                        )
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Committee role sync failed guild=%s", guild.id)
        await asyncio.sleep(45)


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
        overwrites[guild.me] = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            manage_messages=True,
        )
    kwargs = {
        "category": category,
        "topic": topic,
        "default_auto_archive_duration": 10080,
        "reason": "28thサーバー初期構築",
    }
    if overwrites is not None:
        kwargs["overwrites"] = overwrites
    return await guild.create_text_channel(name, **kwargs), True


async def _ensure_forum(guild, name: str, category, *, topic: str, tags, overwrites=None):
    channel = _find_in_category(guild.forum_channels, name, category) or _find_in_category(
        guild.text_channels,
        name,
        category,
    )
    if channel:
        return channel, False
    try:
        kwargs = {
            "category": category,
            "topic": topic,
            "available_tags": [discord.ForumTag(name=tag, emoji="🏷️") for tag in tags],
            "default_auto_archive_duration": 10080,
            "reason": "28thサーバー初期構築",
        }
        if overwrites is not None:
            kwargs["overwrites"] = overwrites
        return await guild.create_forum_channel(name, **kwargs), True
    except discord.HTTPException as exc:
        if getattr(exc, "code", None) != 50024:
            raise
        kwargs = {
            "category": category,
            "topic": f"【スレッド専用】{topic}",
            "default_auto_archive_duration": 10080,
            "reason": "コミュニティ機能未有効のためスレッド専用チャンネルとして作成",
        }
        if overwrites is not None:
            kwargs["overwrites"] = overwrites
        return await guild.create_text_channel(name, **kwargs), True


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
    signature = {}
    for target, overwrite in overwrites.items():
        allow, deny = overwrite.pair()
        signature[target.id] = (allow.value, deny.value)
    return signature


async def _ensure_setup_actor_is_server_admin(
    guild: discord.Guild,
    member: discord.Member,
) -> tuple[discord.Role, bool]:
    """Give the already-authorized setup operator an explicit admin role.

    ``/committee_setup`` itself is administrator-only. This therefore does not
    create a privilege-escalation path; it makes the operator selected for the
    initial setup visible and keeps their access stable when account-specific
    Discord permissions are later cleaned up.
    """
    permissions = discord.Permissions.none()
    permissions.administrator = True
    role, created = await _ensure_role(
        guild,
        SERVER_ADMIN_ROLE,
        color=0x2B2D31,
        permissions=permissions,
    )
    if not role.permissions.administrator:
        await role.edit(
            permissions=permissions,
            reason="28thサーバー管理者権限を整備",
        )
    if all(current.id != role.id for current in member.roles):
        await member.add_roles(
            role,
            reason="/committee_setup 実行者をサーバー管理者として登録",
        )
    return role, created


async def _migrate_legacy_grade_roles(guild: discord.Guild) -> tuple[int, int]:
    """Move members to the new grade names, then remove empty legacy roles."""
    migrated_members = 0
    deleted_roles = 0
    for legacy_name, desired_name in LEGACY_GRADE_ROLES.items():
        legacy_role = _find_named(guild.roles, legacy_name)
        desired_role = _find_named(guild.roles, desired_name)
        if legacy_role is None or desired_role is None:
            continue
        for member in list(legacy_role.members):
            roles = [role for role in member.roles if role.id != legacy_role.id]
            if all(role.id != desired_role.id for role in roles):
                roles.append(desired_role)
            await member.edit(roles=roles, reason=f"学年ロールを {desired_name} へ移行")
            migrated_members += 1
        await legacy_role.delete(reason=f"{desired_name} への移行完了")
        deleted_roles += 1
    return migrated_members, deleted_roles


async def _bootstrap_committee_channels(guild: discord.Guild, *, key_store) -> dict[str, int]:
    """Create the approved server layout without posting into ordinary chats."""
    counts = {"categories": 0, "channels": 0, "messages": 0}
    bureau_roles = {name: _find_named(guild.roles, name) for name, *_rest in BUREAU_ROLES}
    position_roles = {name: _find_named(guild.roles, name) for name in POSITION_ROLES}
    server_admin_role = _find_named(guild.roles, SERVER_ADMIN_ROLE)

    async def ensure_category(name: str, *, overwrites=None):
        category, created = await _ensure_category(guild, name, overwrites=overwrites)
        counts["categories"] += int(created)
        if (
            not created
            and overwrites is not None
            and _overwrite_signature(category.overwrites) != _overwrite_signature(overwrites)
        ):
            await category.edit(overwrites=overwrites, reason="28thカテゴリ権限を同期")
        return category

    async def ensure_text(name: str, category, *, topic: str, overwrites=None, read_only=False):
        effective_overwrites = overwrites
        if read_only:
            effective_overwrites = dict(overwrites or {})
            effective_overwrites[guild.default_role] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=False,
            )
            effective_overwrites[guild.me] = discord.PermissionOverwrite(
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
            if (
                effective_overwrites is not None
                and _overwrite_signature(channel.overwrites)
                != _overwrite_signature(effective_overwrites)
            ):
                edit_kwargs["overwrites"] = effective_overwrites
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
        if not created and overwrites is not None:
            edit_kwargs = {}
            if getattr(channel, "topic", None) != topic:
                edit_kwargs["topic"] = topic
            if _overwrite_signature(channel.overwrites) != _overwrite_signature(overwrites):
                edit_kwargs["overwrites"] = overwrites
            if edit_kwargs:
                await channel.edit(**edit_kwargs, reason="28thフォーラム設定を同期")
        return channel

    async def ensure_voice(name: str, category, *, overwrites=None):
        channel, created = await _ensure_voice(guild, name, category, overwrites=overwrites)
        counts["channels"] += int(created)
        if (
            not created
            and overwrites is not None
            and _overwrite_signature(channel.overwrites) != _overwrite_signature(overwrites)
        ):
            await channel.edit(overwrites=overwrites, reason="28thVC権限を同期")
        return channel

    start = await ensure_category("00｜はじめに")
    await ensure_text(
        "はじめに",
        start,
        topic="最初に読む案内。サーバープロフィールの表示名を自分で設定してから登録を進めます。",
        read_only=True,
    )
    role_panel = await ensure_text(
        ROLE_PANEL_CHANNEL,
        start,
        topic="所属局・学年を登録し、該当者だけ役職を申告します。",
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
    external_overwrites[bureau_roles["渉外局"]] = discord.PermissionOverwrite(
        view_channel=True,
        send_messages=True,
    )
    await ensure_text(
        "渉外案件ex",
        general,
        topic="協賛条件に基づく企業からの告知・お知らせ・勧誘などを一斉配信します。",
        overwrites=external_overwrites,
    )

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
    await ensure_text(
        KEY_REQUEST_CHANNEL,
        key_category,
        topic="矢上部室（209）の利用理由を確認し、承認・却下します。",
        overwrites=_private_overwrites(
            guild,
            [server_admin_role, bureau_roles["渉外局"], position_roles["執行部"]],
        ),
    )

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

    bot_category = await ensure_category("04｜やがぽんBOT")
    await ensure_text(
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
    leadership = await ensure_category("05｜役員・企画長", overwrites=leadership_overwrites)
    officers_only = _private_overwrites(
        guild,
        [server_admin_role, position_roles["役員"], position_roles["執行部"]],
    )
    await ensure_text("役員", leadership, topic="役員向けの連絡・相談。", overwrites=officers_only)
    await ensure_text("企画長", leadership, topic="企画長向けの連絡・相談。", overwrites=leadership_overwrites)
    await ensure_text("新歓", leadership, topic="新歓に関する調整と共有。", overwrites=leadership_overwrites)
    await ensure_text(
        "教室予約報告",
        leadership,
        topic="教室予約の申請・結果・変更を報告します。",
        overwrites=leadership_overwrites,
    )

    officers = await ensure_category("06｜役員会", overwrites=officers_only)
    await ensure_text("役員会", officers, topic="役員会の連絡・会話。", overwrites=officers_only)
    _channel, created = await _ensure_thread_only(
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

    executive_overwrites = _private_overwrites(
        guild,
        [server_admin_role, position_roles["執行部"]],
    )
    executive = await ensure_category("07｜執行部", overwrites=executive_overwrites)
    for bureau_name, _color, _emoji, slug in BUREAU_ROLES:
        await ensure_text(
            f"{slug}＋執行部",
            executive,
            topic=f"{bureau_name}と執行部だけが閲覧できる連絡チャットです。",
            overwrites=_private_overwrites(
                guild,
                [server_admin_role, position_roles["執行部"], bureau_roles[bureau_name]],
            ),
        )

    coordination = await ensure_category("08｜局間連携")
    await ensure_voice("会議用🎙️", coordination)

    liaison_category = await ensure_category("09｜連絡チャット")
    for first_bureau, second_bureau, channel_name in LIAISON_CHANNELS:
        await ensure_text(
            channel_name,
            liaison_category,
            topic=f"{first_bureau}・{second_bureau}・執行部だけが閲覧できる連絡チャットです。",
            overwrites=_private_overwrites(
                guild,
                [
                    server_admin_role,
                    position_roles["執行部"],
                    bureau_roles[first_bureau],
                    bureau_roles[second_bureau],
                ],
            ),
        )

    await ensure_category("10｜その他")
    await ensure_category("11｜アーカイブ", overwrites=executive_overwrites)

    # Ordinary chats, including all 28 liaison chats, intentionally receive no
    # setup message. Only interactive control surfaces are posted and pinned.
    for channel, marker, embed, view in (
        (role_panel, ROLE_PANEL_MARKER, _role_panel_embed(), SelfRolePanelView()),
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


def register_persistent_views(bot) -> None:
    bot.add_view(SelfRolePanelView())
    bot.add_view(PostRegistrationRoleView())
    bot.add_view(RoleApprovalView())
    bot.add_view(SupportLinkApprovalView())
    bot.add_view(KeyPanelView())
    bot.add_view(RestrictedKeyApprovalView())


def register(bot):
    @bot.slash_command(name="committee_setup", description="委員会サーバーのロールとチャンネルを一括整備するぽん")
    @discord.default_permissions(administrator=True)
    async def committee_setup(ctx: discord.ApplicationContext):
        if not await require_guild_admin(ctx):
            return
        me = ctx.guild.me
        if not _bot_can_run_committee_setup(me):
            await ctx.respond(
                "セットアップには、やがぽん自身のAdministrator権限が必要だぽん。"
                "権限を付けてからもう一度実行してほしいぽん。",
                ephemeral=True,
            )
            return
        managed_role_names = {
            *GRADE_ROLES,
            *LEGACY_GRADE_ROLES,
            *POSITION_ROLES,
            *_role_names(BUREAU_ROLES),
            SERVER_ADMIN_ROLE,
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
        created_count = 0
        migrated_members = 0
        deleted_legacy_roles = 0
        try:
            for name, color, _emoji, _slug in BUREAU_ROLES:
                _role, created = await _ensure_role(ctx.guild, name, color=color)
                created_count += int(created)
            for name in GRADE_ROLES:
                _role, created = await _ensure_role(ctx.guild, name, color=0x7289DA)
                created_count += int(created)
            for name in POSITION_ROLES:
                _role, created = await _ensure_role(ctx.guild, name, color=0xF39C12)
                created_count += int(created)
            _admin_role, created = await _ensure_setup_actor_is_server_admin(
                ctx.guild,
                ctx.author,
            )
            created_count += int(created)
            migrated_members, deleted_legacy_roles = await _migrate_legacy_grade_roles(ctx.guild)
            channel_counts = await _bootstrap_committee_channels(
                ctx.guild,
                key_store=bot.key_management.store,
            )
        except discord.Forbidden:
            await ctx.followup.send(
                "ロールまたはチャンネルを整備できなかったぽん。"
                "やがぽんの管理者権限とロール順を確認してほしいぽん。",
                ephemeral=True,
            )
            return
        except discord.HTTPException as exc:
            log.exception("Committee setup Discord API error guild=%s", ctx.guild_id)
            await ctx.followup.send(
                f"Discord APIエラーで整備を中断したぽん（status={exc.status} / code={exc.code}）。"
                "再実行しても既存のロールやチャンネルは重複しないぽん。",
                ephemeral=True,
            )
            return
        minutes_channel = ctx.guild.get_channel(channel_counts["minutes_channel_id"])
        if minutes_channel is not None:
            await bot.config.set_minutes_channel(ctx.guild.id, minutes_channel.id)
        await ctx.followup.send(
            "\n".join(
                [
                    f"✅ 委員登録基盤を整備したぽん（新規ロール {created_count}件）。",
                    f"学年ロール移行: {migrated_members}人 / 旧ロール削除 {deleted_legacy_roles}件。",
                    (
                        f"新規カテゴリ {channel_counts['categories']}件 / "
                        f"新規チャンネル {channel_counts['channels']}件 / "
                        f"操作パネル {channel_counts['messages']}件。"
                    ),
                    "通常チャットや局間連絡チャットには案内メッセージを投稿していないぽん。",
                    "このコマンドの実行者には「サーバー管理者」ロールを付与したぽん。",
                ]
            ),
            ephemeral=True,
        )

    @bot.slash_command(name="committee_roles", description="登録後に企画長・役員を申告するぽん")
    async def committee_roles(ctx: discord.ApplicationContext):
        guild = ctx.guild
        member = ctx.author
        if not guild or not isinstance(member, discord.Member):
            await ctx.respond("サーバー内で実行してほしいぽん。", ephemeral=True)
            return
        if not _has_completed_registration(bot.config, guild.id, member):
            requirement = "所属局・学年・慶應メール"
            await ctx.respond(
                f"先にロール選択パネルから{requirement}の登録を完了してほしいぽん。",
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
