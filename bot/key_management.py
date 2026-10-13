"""Restart-safe room-key workflow and Discord controls."""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import discord

log = logging.getLogger("yagapon.key_management")

KEY_NAMES = (
    "日吉部室",
    "矢上部室（212）",
    "矢上部室（209）",
    "地下倉庫",
    "屋外倉庫",
)
RESTRICTED_KEY = "矢上部室（209）"
KEY_CATEGORY = "06｜鍵管理"
KEY_ACTION_CHANNEL = "鍵を操作"
KEY_REPORT_CHANNEL = "鍵管理報告"
KEY_REQUEST_CHANNEL = "209利用申請"
KEY_PANEL_MARKER = "yagapon-key-panel-v1"
KEY_STATUS_MARKER = "yagapon-key-status-v1"

ACTION_LABELS = {
    "borrow": "あけ（借りる）",
    "return": "しめ（返す）",
    "transfer": "さしかえ（渡す）",
}
ACTION_COLORS = {
    "borrow": 0x2ECC71,
    "return": 0x3498DB,
    "transfer": 0xF1C40F,
    "request": 0x9B59B6,
    "reject": 0xE74C3C,
    "reminder": 0xE67E22,
}


def default_key_db_path() -> Path:
    configured = os.environ.get("YAGAPON_KEY_DB_PATH")
    if configured:
        return Path(configured)
    config_path = Path(
        os.environ.get(
            "YAGAPON_CONFIG_PATH",
            Path(__file__).parent.parent / "data" / "server_config.json",
        )
    )
    return config_path.parent / "key-management.sqlite3"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None = None) -> str:
    return (value or _now()).isoformat(timespec="seconds")


@dataclass(frozen=True)
class KeyHolding:
    guild_id: int
    key_name: str
    holder_id: int
    borrowed_at: str
    updated_at: str
    last_reminded_at: str | None = None


@dataclass(frozen=True)
class KeyEvent:
    id: str
    guild_id: int
    key_name: str
    action: str
    actor_id: int
    previous_holder_id: int | None
    new_holder_id: int | None
    reason: str | None
    approved_by: int | None
    created_at: str
    reported_at: str | None = None


@dataclass(frozen=True)
class KeyRequest:
    id: str
    guild_id: int
    key_name: str
    action: str
    requester_id: int
    target_id: int | None
    reason: str
    status: str
    created_at: str
    decided_by: int | None = None
    decided_at: str | None = None
    message_id: int | None = None


class KeyStateConflict(RuntimeError):
    """The requested transition no longer matches the current key state."""


class KeyManagementStore:
    """SQLite state machine for physical keys and 209 approval requests."""

    def __init__(self, path: Path | None = None):
        self.path = path or default_key_db_path()
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._lock, self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS key_holdings (
                    guild_id INTEGER NOT NULL,
                    key_name TEXT NOT NULL,
                    holder_id INTEGER NOT NULL,
                    borrowed_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_reminded_at TEXT,
                    PRIMARY KEY (guild_id, key_name)
                );
                CREATE TABLE IF NOT EXISTS key_events (
                    id TEXT PRIMARY KEY,
                    guild_id INTEGER NOT NULL,
                    key_name TEXT NOT NULL,
                    action TEXT NOT NULL,
                    actor_id INTEGER NOT NULL,
                    previous_holder_id INTEGER,
                    new_holder_id INTEGER,
                    reason TEXT,
                    approved_by INTEGER,
                    created_at TEXT NOT NULL,
                    reported_at TEXT
                );
                CREATE INDEX IF NOT EXISTS key_events_guild_created
                    ON key_events(guild_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS key_requests (
                    id TEXT PRIMARY KEY,
                    guild_id INTEGER NOT NULL,
                    key_name TEXT NOT NULL,
                    action TEXT NOT NULL,
                    requester_id INTEGER NOT NULL,
                    target_id INTEGER,
                    reason TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    decided_by INTEGER,
                    decided_at TEXT,
                    message_id INTEGER
                );
                CREATE INDEX IF NOT EXISTS key_requests_pending
                    ON key_requests(guild_id, status, created_at DESC);
                """
            )
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(key_events)")}
            if "reported_at" not in columns:
                connection.execute("ALTER TABLE key_events ADD COLUMN reported_at TEXT")

    @staticmethod
    def _holding(row: sqlite3.Row | None) -> KeyHolding | None:
        return KeyHolding(**dict(row)) if row else None

    @staticmethod
    def _request(row: sqlite3.Row | None) -> KeyRequest | None:
        return KeyRequest(**dict(row)) if row else None

    def get_holding(self, guild_id: int, key_name: str) -> KeyHolding | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM key_holdings WHERE guild_id=? AND key_name=?",
                (guild_id, key_name),
            ).fetchone()
        return self._holding(row)

    def list_holdings(self, guild_id: int) -> dict[str, KeyHolding]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM key_holdings WHERE guild_id=?",
                (guild_id,),
            ).fetchall()
        return {row["key_name"]: self._holding(row) for row in rows}

    def _apply_action(
        self,
        connection: sqlite3.Connection,
        *,
        guild_id: int,
        key_name: str,
        action: str,
        actor_id: int,
        target_id: int | None,
        reason: str | None,
        approved_by: int | None,
        force: bool,
        now: str,
    ) -> KeyEvent:
        if key_name not in KEY_NAMES:
            raise KeyStateConflict("不明な鍵です。")
        row = connection.execute(
            "SELECT * FROM key_holdings WHERE guild_id=? AND key_name=?",
            (guild_id, key_name),
        ).fetchone()
        holding = self._holding(row)
        previous_holder_id = holding.holder_id if holding else None

        if action == "borrow":
            if holding:
                if holding.holder_id == actor_id:
                    raise KeyStateConflict("この鍵はすでにあなたが所持しています。")
                raise KeyStateConflict("この鍵は現在ほかの人が所持しています。")
            new_holder_id = actor_id
            connection.execute(
                """
                INSERT INTO key_holdings (
                    guild_id, key_name, holder_id, borrowed_at, updated_at, last_reminded_at
                ) VALUES (?, ?, ?, ?, ?, NULL)
                """,
                (guild_id, key_name, new_holder_id, now, now),
            )
        elif action == "return":
            if not holding:
                raise KeyStateConflict("この鍵はすでに返却済みです。")
            if holding.holder_id != actor_id and not force:
                raise KeyStateConflict("返却を記録できるのは、現在の所持者だけです。")
            new_holder_id = None
            connection.execute(
                "DELETE FROM key_holdings WHERE guild_id=? AND key_name=?",
                (guild_id, key_name),
            )
        elif action == "transfer":
            if not holding:
                raise KeyStateConflict("この鍵は貸出中ではありません。先に「あけ」を記録してください。")
            if holding.holder_id != actor_id:
                raise KeyStateConflict("さしかえを記録できるのは、現在の所持者だけです。")
            if not target_id or target_id == actor_id:
                raise KeyStateConflict("自分以外の受取人を選んでください。")
            new_holder_id = target_id
            connection.execute(
                """
                UPDATE key_holdings
                SET holder_id=?, borrowed_at=?, updated_at=?, last_reminded_at=NULL
                WHERE guild_id=? AND key_name=?
                """,
                (new_holder_id, now, now, guild_id, key_name),
            )
        else:
            raise KeyStateConflict("不明な操作です。")

        event = KeyEvent(
            id=uuid.uuid4().hex,
            guild_id=guild_id,
            key_name=key_name,
            action=action,
            actor_id=actor_id,
            previous_holder_id=previous_holder_id,
            new_holder_id=new_holder_id,
            reason=reason,
            approved_by=approved_by,
            created_at=now,
        )
        connection.execute(
            """
            INSERT INTO key_events (
                id, guild_id, key_name, action, actor_id, previous_holder_id,
                new_holder_id, reason, approved_by, created_at, reported_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                event.id,
                event.guild_id,
                event.key_name,
                event.action,
                event.actor_id,
                event.previous_holder_id,
                event.new_holder_id,
                event.reason,
                event.approved_by,
                event.created_at,
            ),
        )
        return event

    def apply_action(
        self,
        guild_id: int,
        key_name: str,
        action: str,
        actor_id: int,
        *,
        target_id: int | None = None,
        reason: str | None = None,
        approved_by: int | None = None,
        force: bool = False,
    ) -> KeyEvent:
        now = _iso()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            return self._apply_action(
                connection,
                guild_id=guild_id,
                key_name=key_name,
                action=action,
                actor_id=actor_id,
                target_id=target_id,
                reason=reason,
                approved_by=approved_by,
                force=force,
                now=now,
            )

    def create_request(
        self,
        guild_id: int,
        key_name: str,
        action: str,
        requester_id: int,
        *,
        target_id: int | None,
        reason: str,
    ) -> tuple[KeyRequest, bool]:
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM key_requests
                WHERE guild_id=? AND key_name=? AND action=? AND requester_id=?
                  AND COALESCE(target_id, 0)=COALESCE(?, 0) AND status='pending'
                ORDER BY created_at DESC LIMIT 1
                """,
                (guild_id, key_name, action, requester_id, target_id),
            ).fetchone()
            if row:
                return self._request(row), False
            request = KeyRequest(
                id=uuid.uuid4().hex,
                guild_id=guild_id,
                key_name=key_name,
                action=action,
                requester_id=requester_id,
                target_id=target_id,
                reason=reason.strip(),
                status="pending",
                created_at=_iso(),
            )
            connection.execute(
                """
                INSERT INTO key_requests (
                    id, guild_id, key_name, action, requester_id, target_id,
                    reason, status, created_at, decided_by, decided_at, message_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL)
                """,
                (
                    request.id,
                    request.guild_id,
                    request.key_name,
                    request.action,
                    request.requester_id,
                    request.target_id,
                    request.reason,
                    request.status,
                    request.created_at,
                ),
            )
        return request, True

    def set_request_message(self, request_id: str, message_id: int) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE key_requests SET message_id=? WHERE id=?",
                (message_id, request_id),
            )

    def get_request(self, request_id: str) -> KeyRequest | None:
        with self._lock, self._connect() as connection:
            row = connection.execute("SELECT * FROM key_requests WHERE id=?", (request_id,)).fetchone()
        return self._request(row)

    def decide_request(
        self,
        request_id: str,
        approver_id: int,
        *,
        approve: bool,
    ) -> tuple[KeyRequest, KeyEvent | None]:
        now = _iso()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM key_requests WHERE id=?", (request_id,)).fetchone()
            request = self._request(row)
            if not request:
                raise KeyStateConflict("申請データが見つかりません。")
            if request.status != "pending":
                raise KeyStateConflict("この申請はすでに処理済みです。")
            event = None
            status = "rejected"
            if approve:
                event = self._apply_action(
                    connection,
                    guild_id=request.guild_id,
                    key_name=request.key_name,
                    action=request.action,
                    actor_id=request.requester_id,
                    target_id=request.target_id,
                    reason=request.reason,
                    approved_by=approver_id,
                    force=False,
                    now=now,
                )
                status = "approved"
            connection.execute(
                """
                UPDATE key_requests
                SET status=?, decided_by=?, decided_at=?
                WHERE id=? AND status='pending'
                """,
                (status, approver_id, now, request_id),
            )
            updated = self._request(
                connection.execute("SELECT * FROM key_requests WHERE id=?", (request_id,)).fetchone()
            )
        return updated, event

    def mark_request_delivery_failed(self, request_id: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE key_requests SET status='delivery_failed' WHERE id=? AND status='pending'",
                (request_id,),
            )

    def list_unreported_events(self, limit: int = 100) -> list[KeyEvent]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM key_events
                WHERE reported_at IS NULL
                ORDER BY created_at ASC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [KeyEvent(**dict(row)) for row in rows]

    def mark_event_reported(self, event_id: str, *, at: datetime | None = None) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE key_events SET reported_at=? WHERE id=?",
                (_iso(at), event_id),
            )

    def due_reminders(
        self,
        *,
        now: datetime | None = None,
        after_hours: int = 8,
        repeat_hours: int = 12,
    ) -> list[KeyHolding]:
        now = now or _now()
        borrowed_cutoff = _iso(now - timedelta(hours=after_hours))
        reminder_cutoff = _iso(now - timedelta(hours=repeat_hours))
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM key_holdings
                WHERE borrowed_at <= ?
                  AND (last_reminded_at IS NULL OR last_reminded_at <= ?)
                ORDER BY borrowed_at
                """,
                (borrowed_cutoff, reminder_cutoff),
            ).fetchall()
        return [self._holding(row) for row in rows]

    def mark_reminded(self, guild_id: int, key_name: str, *, at: datetime | None = None) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE key_holdings SET last_reminded_at=? WHERE guild_id=? AND key_name=?",
                (_iso(at), guild_id, key_name),
            )


def _has_role(member: discord.Member | discord.User, role_name: str) -> bool:
    return any(role.name == role_name for role in (getattr(member, "roles", None) or ()))


def can_use_restricted_key(member: discord.Member) -> bool:
    permissions = getattr(member, "guild_permissions", None)
    return bool(
        (permissions and permissions.administrator)
        or _has_role(member, "渉外局")
        or _has_role(member, "執行部")
    )


def can_approve_restricted_key(member: discord.Member) -> bool:
    permissions = getattr(member, "guild_permissions", None)
    return bool(
        (permissions and permissions.administrator)
        or _has_role(member, "渉外局")
        or _has_role(member, "執行部")
    )


async def _respond_ephemeral(
    interaction: discord.Interaction,
    *,
    content: str | None = None,
    embed: discord.Embed | None = None,
    view: discord.ui.View | None = None,
) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(content=content, embed=embed, view=view, ephemeral=True)
    else:
        await interaction.response.send_message(content=content, embed=embed, view=view, ephemeral=True)


def key_panel_embed() -> discord.Embed:
    embed = discord.Embed(
        title="🔑 部室・倉庫の鍵管理",
        description=(
            "鍵を選ぶと、現在の所持状況と操作ボタンが表示されます。\n"
            "チャットへ「あけ」「しめ」と入力する必要はありません。"
        ),
        color=discord.Color(0x5865F2),
    )
    embed.add_field(name="🔓 あけ", value="鍵を借りたとき", inline=True)
    embed.add_field(name="🔒 しめ", value="鍵を返却したとき", inline=True)
    embed.add_field(name="🔁 さしかえ", value="別の委員へ直接渡したとき", inline=True)
    embed.add_field(
        name="⚠️ 報告忘れを防ぐために",
        value=(
            "返却したらすぐ **しめ**、手渡したらその場で **さしかえ** を押してください。\n"
            "長時間所持したままの場合は、現在の所持者へ確認を送ります。"
        ),
        inline=False,
    )
    embed.add_field(
        name="🔐 矢上部室（209）",
        value="渉外局・執行部のみ直接利用できます。その他の委員は理由を入力して承認を待ちます。",
        inline=False,
    )
    embed.set_footer(text="物理的に受け渡した後、記録も必ず更新してください")
    return embed


def key_status_embed(guild: discord.Guild, store: KeyManagementStore) -> discord.Embed:
    holdings = store.list_holdings(guild.id)
    embed = discord.Embed(
        title="🗝️ 鍵の現在状況",
        description="操作が行われるたびに自動更新されます。",
        color=discord.Color(0x2B2D31),
        timestamp=_now(),
    )
    for key_name in KEY_NAMES:
        holding = holdings.get(key_name)
        if holding:
            value = f"🔴 貸出中：<@{holding.holder_id}>\n<t:{int(datetime.fromisoformat(holding.borrowed_at).timestamp())}:R>から"
        else:
            value = "🟢 返却済み"
        embed.add_field(name=key_name, value=value, inline=False)
    embed.set_footer(text="表示と実物が違う場合は、現在の所持者またはIT局へ連絡してください")
    return embed


def _selected_key_embed(guild: discord.Guild, key_name: str, store: KeyManagementStore) -> discord.Embed:
    holding = store.get_holding(guild.id, key_name)
    embed = discord.Embed(title=f"🔑 {key_name}", color=discord.Color(0x5865F2))
    if holding:
        embed.description = f"現在の所持者：<@{holding.holder_id}>\n貸出開始：<t:{int(datetime.fromisoformat(holding.borrowed_at).timestamp())}:F>"
    else:
        embed.description = "現在は返却済みです。"
    if key_name == RESTRICTED_KEY:
        embed.add_field(
            name="209の利用制限",
            value="権限がない場合は、**あけ** または **さしかえ** の際に利用理由を申請します。",
            inline=False,
        )
    return embed


class KeySelect(discord.ui.Select):
    def __init__(self):
        super().__init__(
            placeholder="操作する鍵を選択",
            min_values=1,
            max_values=1,
            custom_id="yagapon:key:select:v1",
            options=[discord.SelectOption(label=name, value=name, emoji="🔑") for name in KEY_NAMES],
        )

    async def callback(self, interaction: discord.Interaction):
        manager = getattr(interaction.client, "key_management", None)
        if not manager or not interaction.guild:
            await _respond_ephemeral(interaction, content="鍵管理を利用できないぽん。管理者へ連絡してほしいぽん。")
            return
        key_name = self.values[0]
        await _respond_ephemeral(
            interaction,
            embed=_selected_key_embed(interaction.guild, key_name, manager.store),
            view=KeyActionView(key_name),
        )


class KeyPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(KeySelect())

    @discord.ui.button(
        label="現在の所持状況",
        emoji="🗝️",
        style=discord.ButtonStyle.secondary,
        row=1,
        custom_id="yagapon:key:status:v1",
    )
    async def status(self, _button: discord.ui.Button, interaction: discord.Interaction):
        manager = getattr(interaction.client, "key_management", None)
        if not manager or not interaction.guild:
            await _respond_ephemeral(interaction, content="鍵管理を利用できないぽん。")
            return
        await _respond_ephemeral(interaction, embed=key_status_embed(interaction.guild, manager.store))


class KeyActionView(discord.ui.View):
    def __init__(self, key_name: str):
        super().__init__(timeout=300)
        self.key_name = key_name

    @discord.ui.button(label="あけ（借りる）", emoji="🔓", style=discord.ButtonStyle.success)
    async def borrow(self, _button: discord.ui.Button, interaction: discord.Interaction):
        manager = getattr(interaction.client, "key_management", None)
        member = interaction.user
        if not manager or not interaction.guild or not isinstance(member, discord.Member):
            await _respond_ephemeral(interaction, content="サーバー内で操作してほしいぽん。")
            return
        if self.key_name == RESTRICTED_KEY and not can_use_restricted_key(member):
            await interaction.response.send_modal(RestrictedKeyReasonModal("borrow"))
            return
        await manager.perform_action(interaction, self.key_name, "borrow")

    @discord.ui.button(label="しめ（返す）", emoji="🔒", style=discord.ButtonStyle.primary)
    async def return_key(self, _button: discord.ui.Button, interaction: discord.Interaction):
        manager = getattr(interaction.client, "key_management", None)
        if not manager:
            await _respond_ephemeral(interaction, content="鍵管理を利用できないぽん。")
            return
        await manager.perform_action(interaction, self.key_name, "return")

    @discord.ui.button(label="さしかえ（渡す）", emoji="🔁", style=discord.ButtonStyle.secondary)
    async def transfer(self, _button: discord.ui.Button, interaction: discord.Interaction):
        manager = getattr(interaction.client, "key_management", None)
        if not manager or not interaction.guild:
            await _respond_ephemeral(interaction, content="鍵管理を利用できないぽん。")
            return
        holding = manager.store.get_holding(interaction.guild.id, self.key_name)
        if not holding or holding.holder_id != interaction.user.id:
            await _respond_ephemeral(interaction, content="さしかえを記録できるのは、現在の所持者だけだぽん。")
            return
        await _respond_ephemeral(
            interaction,
            content=f"**{self.key_name}** を渡した相手を選んでほしいぽん。",
            view=KeyTransferView(self.key_name),
        )


class KeyRecipientSelect(discord.ui.Select):
    def __init__(self, key_name: str):
        super().__init__(
            select_type=discord.ComponentType.user_select,
            placeholder="鍵を渡した相手を選択",
            min_values=1,
            max_values=1,
        )
        self.key_name = key_name

    async def callback(self, interaction: discord.Interaction):
        target = self.values[0]
        member = interaction.user
        manager = getattr(interaction.client, "key_management", None)
        if not manager or not interaction.guild or not isinstance(member, discord.Member):
            await _respond_ephemeral(interaction, content="サーバー内で操作してほしいぽん。")
            return
        if target.bot:
            await _respond_ephemeral(interaction, content="Botへ鍵を渡すことはできないぽん。")
            return
        if target.id == member.id:
            await _respond_ephemeral(interaction, content="自分以外の受取人を選んでほしいぽん。")
            return
        target_member = target if isinstance(target, discord.Member) else interaction.guild.get_member(target.id)
        if target_member is None:
            try:
                target_member = await interaction.guild.fetch_member(target.id)
            except (discord.NotFound, discord.HTTPException):
                target_member = None
        if self.key_name == RESTRICTED_KEY and (
            target_member is None or not can_use_restricted_key(target_member)
        ):
            await interaction.response.send_modal(RestrictedKeyReasonModal("transfer", target_id=target.id))
            return
        await manager.perform_action(interaction, self.key_name, "transfer", target_id=target.id)


class KeyTransferView(discord.ui.View):
    def __init__(self, key_name: str):
        super().__init__(timeout=300)
        self.add_item(KeyRecipientSelect(key_name))


class RestrictedKeyReasonModal(discord.ui.Modal):
    def __init__(self, action: str, *, target_id: int | None = None):
        title = "209利用申請" if action == "borrow" else "209さしかえ申請"
        super().__init__(title=title)
        self.action = action
        self.target_id = target_id
        self.add_item(
            discord.ui.InputText(
                label="利用理由・用途",
                placeholder="例：協賛物品を取り出すため（○時ごろ返却予定）",
                style=discord.InputTextStyle.long,
                min_length=5,
                max_length=400,
                required=True,
            )
        )

    async def callback(self, interaction: discord.Interaction):
        manager = getattr(interaction.client, "key_management", None)
        if not manager:
            await _respond_ephemeral(interaction, content="鍵管理を利用できないぽん。")
            return
        await manager.submit_restricted_request(
            interaction,
            action=self.action,
            target_id=self.target_id,
            reason=self.children[0].value.strip(),
        )


def _request_id_from_message(message: discord.Message | None) -> str | None:
    if not message or not message.embeds:
        return None
    for field in message.embeds[0].fields:
        if field.name == "申請ID":
            return str(field.value).strip(" `")
    return None


class RestrictedKeyApprovalView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="承認",
        emoji="✅",
        style=discord.ButtonStyle.success,
        custom_id="yagapon:key:209:approve:v1",
    )
    async def approve(self, _button: discord.ui.Button, interaction: discord.Interaction):
        manager = getattr(interaction.client, "key_management", None)
        if not manager:
            await _respond_ephemeral(interaction, content="鍵管理を利用できないぽん。")
            return
        await manager.decide_restricted_request(interaction, approve=True)

    @discord.ui.button(
        label="却下",
        emoji="✖️",
        style=discord.ButtonStyle.danger,
        custom_id="yagapon:key:209:reject:v1",
    )
    async def reject(self, _button: discord.ui.Button, interaction: discord.Interaction):
        manager = getattr(interaction.client, "key_management", None)
        if not manager:
            await _respond_ephemeral(interaction, content="鍵管理を利用できないぽん。")
            return
        await manager.decide_restricted_request(interaction, approve=False)


class KeyManagementManager:
    def __init__(self, bot, store: KeyManagementStore | None = None):
        self.bot = bot
        self.store = store or KeyManagementStore()
        self._reminder_task: asyncio.Task | None = None
        self.reminder_after_hours = int(os.environ.get("YAGAPON_KEY_REMINDER_HOURS", "8"))
        self.reminder_repeat_hours = int(os.environ.get("YAGAPON_KEY_REMINDER_REPEAT_HOURS", "12"))

    def start(self) -> None:
        if self._reminder_task is None or self._reminder_task.done():
            self._reminder_task = asyncio.create_task(self._reminder_loop(), name="key-management-reminders")

    @staticmethod
    def _named_text_channel(guild: discord.Guild, name: str):
        return discord.utils.get(guild.text_channels, name=name)

    async def perform_action(
        self,
        interaction: discord.Interaction,
        key_name: str,
        action: str,
        *,
        target_id: int | None = None,
    ) -> None:
        if not interaction.guild:
            await _respond_ephemeral(interaction, content="サーバー内で操作してほしいぽん。")
            return
        await interaction.response.defer(ephemeral=True)
        try:
            event = await asyncio.to_thread(
                self.store.apply_action,
                interaction.guild.id,
                key_name,
                action,
                interaction.user.id,
                target_id=target_id,
            )
        except KeyStateConflict as exc:
            await interaction.followup.send(f"⚠️ {exc}ぽん", ephemeral=True)
            return
        report_synced = True
        try:
            await self._publish_event(interaction.guild, event)
            await self.refresh_status(interaction.guild)
        except discord.HTTPException:
            report_synced = False
            log.exception("Key event report failed guild=%s event=%s", interaction.guild.id, event.id)
        result = discord.Embed(
            title=f"✅ {ACTION_LABELS[action]}を記録しました",
            description=f"**{key_name}** の記録を更新しました。",
            color=discord.Color(ACTION_COLORS[action]),
        )
        if action == "transfer" and target_id:
            result.add_field(name="新しい所持者", value=f"<@{target_id}>")
        result.set_footer(
            text=(
                "実物の受け渡しと記録が一致していることを確認してください"
                if report_synced
                else "状態は保存済みです。報告チャンネルへの転送は自動再試行します"
            )
        )
        await interaction.followup.send(embed=result, ephemeral=True)

    async def force_return(self, ctx: discord.ApplicationContext, key_name: str) -> None:
        await ctx.defer(ephemeral=True)
        try:
            event = await asyncio.to_thread(
                self.store.apply_action,
                ctx.guild.id,
                key_name,
                "return",
                ctx.author.id,
                reason="管理者による記録修正",
                approved_by=ctx.author.id,
                force=True,
            )
        except KeyStateConflict as exc:
            await ctx.followup.send(f"⚠️ {exc}ぽん", ephemeral=True)
            return
        report_synced = True
        try:
            await self._publish_event(ctx.guild, event)
            await self.refresh_status(ctx.guild)
        except discord.HTTPException:
            report_synced = False
            log.exception("Forced key return report failed guild=%s event=%s", ctx.guild.id, event.id)
        embed = discord.Embed(
            title="✅ 管理者が返却記録を修正しました",
            description=f"**{key_name}** を返却済みにしました。",
            color=discord.Color(ACTION_COLORS["return"]),
        )
        embed.add_field(name="記録上の前所持者", value=f"<@{event.previous_holder_id}>")
        if not report_synced:
            embed.set_footer(text="報告チャンネルへの転送は自動再試行します")
        await ctx.followup.send(embed=embed, ephemeral=True)

    async def submit_restricted_request(
        self,
        interaction: discord.Interaction,
        *,
        action: str,
        target_id: int | None,
        reason: str,
    ) -> None:
        guild = interaction.guild
        if not guild:
            await _respond_ephemeral(interaction, content="サーバー内で操作してほしいぽん。")
            return
        await interaction.response.defer(ephemeral=True)
        request_channel = self._named_text_channel(guild, KEY_REQUEST_CHANNEL)
        report_channel = self._named_text_channel(guild, KEY_REPORT_CHANNEL)
        if not request_channel:
            await _respond_ephemeral(interaction, content="209利用申請チャンネルがないぽん。管理者に再セットアップを依頼してほしいぽん。")
            return
        try:
            request, created = await asyncio.to_thread(
                self.store.create_request,
                guild.id,
                RESTRICTED_KEY,
                action,
                interaction.user.id,
                target_id=target_id,
                reason=reason,
            )
        except KeyStateConflict as exc:
            await _respond_ephemeral(interaction, content=f"⚠️ {exc}ぽん")
            return
        if not created:
            await _respond_ephemeral(interaction, content="同じ209利用申請がすでに承認待ちだぽん。")
            return
        embed = discord.Embed(
            title="🔐 矢上部室（209）利用申請",
            description=f"{interaction.user.mention} から申請が届きました。",
            color=discord.Color(ACTION_COLORS["request"]),
            timestamp=_now(),
        )
        embed.add_field(name="操作", value=ACTION_LABELS[action], inline=True)
        if target_id:
            embed.add_field(name="受取人", value=f"<@{target_id}>", inline=True)
        embed.add_field(name="利用理由・用途", value=reason, inline=False)
        embed.add_field(name="申請ID", value=f"`{request.id}`", inline=False)
        embed.set_footer(text="承認時にも現在の所持状況を再確認します")
        try:
            message = await request_channel.send(embed=embed, view=RestrictedKeyApprovalView(), silent=True)
        except discord.HTTPException:
            await asyncio.to_thread(self.store.mark_request_delivery_failed, request.id)
            log.exception("209 request delivery failed guild=%s request=%s", guild.id, request.id)
            await _respond_ephemeral(
                interaction,
                content="申請チャンネルへの送信に失敗したぽん。記録は未申請に戻したので、少し待って再度試してほしいぽん。",
            )
            return
        await asyncio.to_thread(self.store.set_request_message, request.id, message.id)
        if report_channel:
            notice = discord.Embed(
                title="🟣 209利用申請を受け付けました",
                description=f"{interaction.user.mention} の申請は渉外担当の確認待ちです。",
                color=discord.Color(ACTION_COLORS["request"]),
                timestamp=_now(),
            )
            notice.add_field(name="操作", value=ACTION_LABELS[action])
            await report_channel.send(embed=notice, silent=True)
        await _respond_ephemeral(
            interaction,
            content=f"📨 209の **{ACTION_LABELS[action]}** を申請したぽん。承認されるまで実物の鍵は受け取らないでほしいぽん。",
        )

    async def decide_restricted_request(self, interaction: discord.Interaction, *, approve: bool) -> None:
        member = interaction.user
        if not isinstance(member, discord.Member) or not can_approve_restricted_key(member):
            await _respond_ephemeral(interaction, content="209利用申請を処理できるのは、渉外局または執行部だけだぽん。")
            return
        request_id = _request_id_from_message(interaction.message)
        if not request_id:
            await _respond_ephemeral(interaction, content="申請IDを確認できなかったぽん。")
            return
        try:
            request, event = await asyncio.to_thread(
                self.store.decide_request,
                request_id,
                member.id,
                approve=approve,
            )
        except KeyStateConflict as exc:
            await _respond_ephemeral(interaction, content=f"⚠️ {exc}ぽん")
            return
        embed = interaction.message.embeds[0]
        embed.color = discord.Color.green() if approve else discord.Color.red()
        embed.add_field(
            name="結果",
            value=f"{'✅ 承認' if approve else '❌ 却下'}：{member.mention}",
            inline=False,
        )
        await interaction.response.edit_message(embed=embed, view=None)
        guild = interaction.guild
        if guild and event:
            try:
                await self._publish_event(guild, event)
                await self.refresh_status(guild)
            except discord.HTTPException:
                log.exception("Approved key event report failed guild=%s event=%s", guild.id, event.id)
        elif guild:
            channel = self._named_text_channel(guild, KEY_REPORT_CHANNEL)
            if channel:
                rejected = discord.Embed(
                    title="🔴 209利用申請が却下されました",
                    description=f"<@{request.requester_id}> の申請を {member.mention} が却下しました。",
                    color=discord.Color(ACTION_COLORS["reject"]),
                    timestamp=_now(),
                )
                await channel.send(embed=rejected, silent=True)
        target = await self._resolve_user(guild, request.requester_id) if guild else None
        if target:
            try:
                await target.send(
                    f"矢上部室（209）の **{ACTION_LABELS[request.action]}** 申請は"
                    f"{'承認' if approve else '却下'}されたぽん。"
                )
            except discord.HTTPException:
                pass

    async def _publish_event(self, guild: discord.Guild, event: KeyEvent) -> None:
        channel = self._named_text_channel(guild, KEY_REPORT_CHANNEL)
        if not channel:
            log.warning("Key report channel is missing guild=%s", guild.id)
            return
        titles = {
            "borrow": "🟢 あけ｜鍵を借りました",
            "return": "🔵 しめ｜鍵を返しました",
            "transfer": "🟡 さしかえ｜鍵を渡しました",
        }
        embed = discord.Embed(
            title=titles[event.action],
            color=discord.Color(ACTION_COLORS[event.action]),
            timestamp=datetime.fromisoformat(event.created_at),
        )
        embed.add_field(name="鍵", value=event.key_name, inline=True)
        if event.action == "borrow":
            embed.add_field(name="所持者", value=f"<@{event.new_holder_id}>", inline=True)
        elif event.action == "return":
            embed.add_field(name="返却者", value=f"<@{event.actor_id}>", inline=True)
            if event.previous_holder_id and event.previous_holder_id != event.actor_id:
                embed.add_field(name="記録上の前所持者", value=f"<@{event.previous_holder_id}>", inline=True)
        else:
            embed.add_field(name="渡した人", value=f"<@{event.actor_id}>", inline=True)
            embed.add_field(name="受け取った人", value=f"<@{event.new_holder_id}>", inline=True)
        if event.approved_by:
            label = "209承認者" if event.key_name == RESTRICTED_KEY and event.reason != "管理者による記録修正" else "記録修正者"
            embed.add_field(name=label, value=f"<@{event.approved_by}>", inline=False)
        embed.set_footer(text=f"記録ID: {event.id[:10]}")
        await channel.send(embed=embed, silent=True)
        await asyncio.to_thread(self.store.mark_event_reported, event.id)

    async def refresh_status(self, guild: discord.Guild) -> None:
        channel = self._named_text_channel(guild, KEY_REPORT_CHANNEL)
        if not channel:
            return
        message = None
        async for candidate in channel.history(limit=100):
            if candidate.author.id == guild.me.id and KEY_STATUS_MARKER in (candidate.content or ""):
                message = candidate
                break
        embed = key_status_embed(guild, self.store)
        if message:
            await message.edit(content=f"`{KEY_STATUS_MARKER}`", embed=embed)
            if not message.pinned:
                try:
                    await message.pin(reason="鍵の現在状況")
                except discord.HTTPException:
                    log.warning("Could not pin key status guild=%s message=%s", guild.id, message.id)
        else:
            message = await channel.send(content=f"`{KEY_STATUS_MARKER}`", embed=embed, silent=True)
            try:
                await message.pin(reason="鍵の現在状況")
            except discord.HTTPException:
                log.warning("Could not pin key status guild=%s message=%s", guild.id, message.id)

    async def _resolve_user(self, guild: discord.Guild, user_id: int):
        member = guild.get_member(user_id)
        if member:
            return member
        try:
            return await guild.fetch_member(user_id)
        except discord.NotFound:
            try:
                return await self.bot.fetch_user(user_id)
            except (discord.NotFound, discord.HTTPException):
                return None
        except discord.HTTPException:
            try:
                return await self.bot.fetch_user(user_id)
            except (discord.NotFound, discord.HTTPException):
                return None

    async def run_reminders(self) -> None:
        await self.retry_unreported_events()
        due = await asyncio.to_thread(
            self.store.due_reminders,
            after_hours=self.reminder_after_hours,
            repeat_hours=self.reminder_repeat_hours,
        )
        for holding in due:
            guild = self.bot.get_guild(holding.guild_id)
            if not guild:
                continue
            member = await self._resolve_user(guild, holding.holder_id)
            report_channel = self._named_text_channel(guild, KEY_REPORT_CHANNEL)
            notified = False
            if member:
                try:
                    await member.send(
                        f"🔑 **{holding.key_name}** をまだ所持している記録になっているぽん。\n"
                        "返却済みなら「しめ」、別の人へ渡したなら「さしかえ」を記録してほしいぽん。"
                    )
                    notified = True
                except discord.HTTPException:
                    pass
            if report_channel:
                embed = discord.Embed(
                    title="🟠 鍵の所持状況を確認してください",
                    description=f"<@{holding.holder_id}>：**{holding.key_name}**",
                    color=discord.Color(ACTION_COLORS["reminder"]),
                    timestamp=_now(),
                )
                embed.add_field(
                    name="確認事項",
                    value="返却済みなら **しめ**、別の委員へ渡したなら **さしかえ** を記録してください。",
                    inline=False,
                )
                try:
                    await report_channel.send(embed=embed, silent=True)
                    notified = True
                except discord.HTTPException:
                    log.warning(
                        "Key reminder report failed guild=%s key=%s",
                        holding.guild_id,
                        holding.key_name,
                    )
            if notified:
                await asyncio.to_thread(self.store.mark_reminded, holding.guild_id, holding.key_name)

    async def retry_unreported_events(self) -> None:
        events = await asyncio.to_thread(self.store.list_unreported_events)
        for event in events:
            guild = self.bot.get_guild(event.guild_id)
            if not guild:
                continue
            try:
                await self._publish_event(guild, event)
                await self.refresh_status(guild)
            except discord.HTTPException:
                log.warning(
                    "Key event report retry failed guild=%s event=%s",
                    event.guild_id,
                    event.id,
                )

    async def _reminder_loop(self) -> None:
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            try:
                await self.run_reminders()
            except Exception:
                log.exception("Key reminder loop failed")
            await asyncio.sleep(15 * 60)


def register_persistent_views(bot) -> None:
    bot.add_view(KeyPanelView())
    bot.add_view(RestrictedKeyApprovalView())
