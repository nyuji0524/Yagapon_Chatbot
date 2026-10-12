"""Operator commands for non-destructive archived recording tests."""

import asyncio

import discord

from bot.authorization import require_guild_admin
from bot.recording_replay import find_recording_artifact, list_recording_artifacts


def _duration(seconds: int) -> str:
    hours, remainder = divmod(max(0, seconds), 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


async def _require_operator(ctx, bot) -> bool:
    if not await require_guild_admin(ctx):
        return False
    if not await bot.is_owner(ctx.author):
        await ctx.respond(
            "録音の横断テストはBot所有者のみ実行できます。",
            ephemeral=True,
        )
        return False
    return True


def register(bot):
    group = bot.create_group("recording", "保存済み録音の運用テスト")

    @group.command(name="list", description="再生できる長時間録音を確認する")
    @discord.default_permissions(administrator=True)
    async def recording_list(ctx: discord.ApplicationContext):
        if not await _require_operator(ctx, bot):
            return
        await ctx.defer(ephemeral=True)
        artifacts = await asyncio.to_thread(list_recording_artifacts)
        if not artifacts:
            await ctx.followup.send("再生可能な録音または文字起こしはありません。", ephemeral=True)
            return
        lines = []
        for item in artifacts[:15]:
            sources = []
            if item.raw_bytes:
                sources.append(f"PCM {_duration(item.estimated_seconds)}")
            if item.transcript_bytes:
                sources.append("文字起こし")
            candidate = (
                f"`{item.session_id}`\n"
                f"　{item.channel_name} / guild `{item.source_guild_id}` / "
                f"{', '.join(sources)} / {item.status}"
            )
            if sum(len(line) for line in lines) + len(candidate) > 1700:
                break
            lines.append(candidate)
        await ctx.followup.send(
            "**再生可能な録音（長い順）**\n" + "\n".join(lines),
            ephemeral=True,
        )

    @group.command(name="replay", description="/leave 相当の議事録生成を非破壊で再実行する")
    @discord.default_permissions(administrator=True)
    @discord.option("session_id", description="/recording list に表示されたセッションID")
    @discord.option(
        "retranscribe",
        description="PCMから文字起こしもやり直す（時間とAPI枠を使用）",
        type=bool,
        default=False,
    )
    async def recording_replay(
        ctx: discord.ApplicationContext,
        session_id: str,
        retranscribe: bool = False,
    ):
        if not await _require_operator(ctx, bot):
            return
        await ctx.defer(ephemeral=True)
        try:
            artifact = await asyncio.to_thread(find_recording_artifact, session_id)
            job = bot.recording_replays.start(
                artifact=artifact,
                target_channel=ctx.channel,
                retranscribe=retranscribe,
            )
        except (FileNotFoundError, ValueError, RuntimeError) as exc:
            await ctx.followup.send(f"開始できません: {exc}", ephemeral=True)
            return
        await ctx.followup.send(
            f"テスト議事録ジョブ `{job.id}` を開始しました。"
            "完了後にこのチャンネルへ議事録と生文字起こしを投稿します。",
            ephemeral=True,
        )
