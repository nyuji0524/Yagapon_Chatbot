"""
/join listen|meeting|chat - VCに参加
/leave - VCから退出 (議事録生成 → Google Docs + Discord要約)
pycord版
"""

import asyncio
import logging
import os

import discord

from bot.ai_completion import require_complete_model_output
from bot.ai_models import fast_model, generation_config
from bot.authorization import require_registered_member
from bot.discord_visibility import (
    has_guildwide_rag_access,
    resolve_minutes_notification_channel,
)
from bot.gdrive import load_meeting_material
from bot.knowledge_intake import parse_drive_file_url
from bot.minutes_generation import format_notification_summary, notification_summary_failure
from bot.recording import RecordingLimitExceeded
from bot.voice import (
    FinalReprocessDeferred,
    VoiceMode,
    VoiceSessionAlreadyActive,
    get_session,
    join_voice,
    leave_voice,
)

log = logging.getLogger("yagapon.commands.voice")


def register(bot):
    @bot.slash_command(name="join", description="ボイスチャンネルに参加するぽん！")
    @discord.option(
        "mode",
        description="モードを選ぶぽん",
        choices=[
            discord.OptionChoice("聞き専 (議事録のみ)", "listen"),
            discord.OptionChoice("参加者 (議事録 + 質問対応)", "meeting"),
            discord.OptionChoice("おしゃべり (雑談モード)", "chat"),
        ],
    )
    @discord.option(
        "document_url",
        description="会議資料のGoogle Docs URL（drive_set配下・省略可）",
        required=False,
        default="",
    )
    async def join_cmd(ctx: discord.ApplicationContext, mode: str, document_url: str = ""):
        if not await require_registered_member(ctx, bot.config):
            return
        if get_session(ctx.guild_id):
            await ctx.respond(
                "すでに録音中だぽん。先に `/leave` で議事録の保存を完了してねぽん！",
                ephemeral=True,
            )
            return
        member = ctx.guild.get_member(ctx.author.id)
        if not member or not member.voice or not member.voice.channel:
            await ctx.respond("先にボイスチャンネルに入ってほしいぽん！", ephemeral=True)
            return

        await ctx.defer(ephemeral=True)

        material = None
        if document_url.strip():
            try:
                file_id = parse_drive_file_url(document_url)
                material = await load_meeting_material(bot.config, ctx.guild_id, file_id)
            except Exception as exc:
                log.warning("Meeting material could not be linked: %s", exc)
                await ctx.followup.send(
                    f"❌ 会議資料を紐付けられなかったぽん。{exc}",
                    ephemeral=True,
                )
                return

        voice_mode = VoiceMode(mode)
        vc = member.voice.channel

        # Capture incidents are private to the originating VC. Refuse to start
        # if the bot cannot post there; silently recording without any possible
        # loss/stall warning is operationally unsafe.
        destination = resolve_minutes_notification_channel(None, ctx.guild, vc)
        if destination is None:
            await ctx.followup.send(
                "❌ このボイスチャンネルのチャットへ録音状態を通知できないため、"
                "録音を開始しなかったぽん。ボットにチャンネルの閲覧・送信権限を付けてほしいぽん。",
                ephemeral=True,
            )
            return

        retention_notice = (
            "🎙️ これからこのボイスチャンネルの音声録音を開始するぽん。"
            "録音PCMは文字起こし・議事録の再試行に備え、管理者が明示的に削除するまで"
            "サーバーへ保持するぽん。終了時は `/leave` を実行してほしいぽん。"
        )
        try:
            await asyncio.wait_for(
                destination.send(
                    retention_notice,
                    silent=False,
                    allowed_mentions=discord.AllowedMentions.none(),
                ),
                timeout=5,
            )
        except Exception:
            log.exception(
                "Could not post pre-capture retention notice guild=%s",
                ctx.guild_id,
            )
            await ctx.followup.send(
                "❌ 録音と保存方針をボイスチャンネルへ案内できなかったため、"
                "録音を開始しなかったぽん。ボットの送信権限を確認してほしいぽん。",
                ephemeral=True,
            )
            return

        try:
            session = await join_voice(bot, ctx.guild_id, vc, voice_mode)
        except VoiceSessionAlreadyActive:
            await ctx.followup.send(
                "すでに録音の開始・実行・終了処理中だぽん。完了後にもう一度試してねぽん！",
                ephemeral=True,
            )
            return
        except RecordingLimitExceeded:
            await ctx.followup.send(
                "❌ 録音を安全に残せる空き容量がないため、接続前に開始を中止したぽん。"
                "管理者が録音ディスクの容量を確認してほしいぽん。",
                ephemeral=True,
            )
            return
        except Exception as exc:
            # `/join` is already deferred.  Storage probes, metadata fsync, or
            # Discord connect can still fail before a receiver exists; always
            # close the interaction with an explicit "not recording" result.
            log.exception("Voice recording did not start guild=%s", ctx.guild_id)
            operations = getattr(bot, "operations", None)
            if operations is not None:
                try:
                    operations.record_event(
                        "voice",
                        "capture_start",
                        "failed",
                        guild_id=ctx.guild_id,
                        details={"error_type": type(exc).__name__},
                    )
                except Exception:
                    log.exception("Could not record voice start failure operation")
            await ctx.followup.send(
                "❌ 録音の初期化に失敗したため、録音は開始されていないぽん。"
                "管理者が保存容量とボットの接続状態を確認してほしいぽん。",
                ephemeral=True,
            )
            return
        if material:
            await session.add_meeting_material(material)

        mode_desc = {
            VoiceMode.LISTEN: "聞き専モード 🎧\n録音から議事録を作成するぽん。終わったら `/leave` で退出してねぽん！",
            VoiceMode.MEETING: "参加者モード 🎤\n録音から議事録を作りつつ、質問にも答えるぽん！",
            VoiceMode.CHAT: "おしゃべりモード 💬\n会話を録音しながら、いっぱいおしゃべりするぽん！",
        }

        material_line = f"\n📎 会議資料: [{material.name}]({material.url})" if material else ""
        joined_message = (
            f"🔊 {vc.name} に参加し、録音を開始したぽん！\n"
            f"{mode_desc[voice_mode]}\n"
            "💾 元の録音PCMは再試行用に、管理者が明示削除するまで保持するぽん。"
            f"{material_line}"
        )
        # Announce only in the originating VC chat. Never echo a private VC
        # name or linked Drive URL into the arbitrary slash-command channel.
        try:
            await asyncio.wait_for(
                destination.send(
                    joined_message,
                    silent=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                ),
                timeout=5,
            )
        except Exception:
            log.exception("Could not post voice join notice guild=%s", ctx.guild_id)
            session._mark_capture_interrupted("join-notice-failed")
            try:
                await leave_voice(ctx.guild_id)
            except Exception:
                log.exception(
                    "Could not stop recording after join notice failure guild=%s",
                    ctx.guild_id,
                )
            await ctx.followup.send(
                "❌ 録音開始の確定案内を送れなかったため、録音を停止したぽん。"
                "保存できた断片は復旧用に保持しているぽん。",
                ephemeral=True,
            )
            return
        await ctx.followup.send(joined_message, ephemeral=True)

        if voice_mode == VoiceMode.CHAT:
            await session.speak("こんにちはぽん！おしゃべりやがぽんだぽん！何でも話しかけてねぽん！")

    @bot.slash_command(
        name="recording_status",
        description="録音時間・受信音声量・音声トラック数を確認するぽん",
    )
    async def recording_status_cmd(ctx: discord.ApplicationContext):
        if not await require_registered_member(ctx, bot.config):
            return
        session = get_session(ctx.guild_id)
        if not session or not session.recording_sink:
            await ctx.respond("現在このサーバーでは録音していないぽん。", ephemeral=True)
            return
        member = ctx.guild.get_member(ctx.author.id) or ctx.author
        member_voice = getattr(getattr(member, "voice", None), "channel", None)
        same_voice_channel = getattr(member_voice, "id", None) == getattr(
            session.channel,
            "id",
            None,
        )
        if not same_voice_channel and not has_guildwide_rag_access(ctx.guild, member):
            await ctx.respond(
                "録音中のボイスチャンネルの参加者かサーバー管理者だけが確認できるぽん。",
                ephemeral=True,
            )
            return

        health = session.capture_health()
        if not health:
            await ctx.respond("録音状態を取得できなかったぽん。", ephemeral=True)
            return
        wall_minutes = health.wall_seconds / 60
        audio_minutes = health.audio_seconds / 60
        if health.audio_seconds <= 0:
            state = "❌ Discordから音声をまだ受信していません"
        elif session.capture_warning_text():
            state = "⚠️ 受信音声が少なく、欠落の可能性があります"
        else:
            state = "✅ Discordから音声を受信しています"
        alert_phase, alert_reason = getattr(
            session,
            "capture_alert_status",
            ("healthy", ""),
        )
        alert_line = (
            f"監視通知: {alert_phase}（{alert_reason}）"
            if alert_phase != "healthy"
            else "監視通知: healthy"
        )
        await ctx.respond(
            "\n".join(
                [
                    f"**録音状態** — {state}",
                    f"経過時間: 約{wall_minutes:.0f}分",
                    f"受信音声: 約{audio_minutes:.1f}分（音声パケットの合計）",
                    f"音声トラック: {health.source_tracks}件",
                    alert_line,
                    "元PCM・受信timeline・metadata: 明示削除まで保持",
                    "※ Discordが送信した発話部分だけを数えるため、受信音声は経過時間より短くなります。",
                ]
            ),
            ephemeral=True,
        )

    @bot.slash_command(name="leave", description="ボイスチャンネルから退出するぽん！")
    async def leave_cmd(ctx: discord.ApplicationContext):
        if not await require_registered_member(ctx, bot.config):
            return
        await ctx.defer(ephemeral=True)

        # セッション情報を退出前に取得
        session = get_session(ctx.guild_id)
        if not session:
            await ctx.followup.send(
                "ボイスチャンネルには参加していないぽん。",
                ephemeral=True,
            )
            return
        member = ctx.guild.get_member(ctx.author.id) or ctx.author
        member_voice = getattr(getattr(member, "voice", None), "channel", None)
        same_voice_channel = getattr(member_voice, "id", None) == getattr(session.channel, "id", None)
        if not same_voice_channel and not has_guildwide_rag_access(ctx.guild, member):
            await ctx.followup.send(
                "この録音を終了できるのは、録音中のボイスチャンネルの参加者かサーバー管理者だけだぽん。",
                ephemeral=True,
            )
            return
        if not await session.claim_delivery():
            await ctx.followup.send(
                "退出と議事録作成はすでに進行中だぽん。完了通知を待ってねぽん！",
                ephemeral=True,
            )
            return
        channel_name = session.channel.name if session else "unknown"
        category_name = getattr(getattr(session.channel, "category", None), "name", "") if session else ""
        has_audio = bool(session and session.mode in (VoiceMode.LISTEN, VoiceMode.MEETING))

        if has_audio:
            await ctx.followup.send(
                "🎙️ 音声を文字起こし中だぽん...少し待ってねぽん ⏳",
                ephemeral=True,
            )

        try:
            minutes = await leave_voice(ctx.guild_id)
        except Exception as exc:
            if session.recording_sink:
                capture_status = (
                    "capture_interrupted"
                    if getattr(session, "_capture_interrupted", False)
                    else (
                        "capture_suspicious"
                        if getattr(session, "_capture_suspicious", False)
                        else (
                            "recording_truncated"
                            if isinstance(exc, RecordingLimitExceeded)
                            else (
                                "final_reprocess_deferred"
                                if isinstance(exc, FinalReprocessDeferred)
                                else "leave_failed"
                            )
                        )
                    )
                )
                await session._mark_recording_sink(
                    capture_status,
                    error=type(exc).__name__,
                )
            log.exception("Voice leave failed for guild %s", ctx.guild_id)
            if isinstance(exc, RecordingLimitExceeded):
                message = (
                    "録音容量の上限に達したため、不完全な議事録は公開しなかったぽん。"
                    "録音データを保管したので管理者に確認してもらってねぽん。"
                )
            elif isinstance(exc, FinalReprocessDeferred):
                message = (
                    "録音の停止と保存は完了したぽん。長時間の文字起こしはバックグラウンド復旧へ"
                    "引き継いだので、完了通知を待ってほしいぽん。元の録音は削除せず保持しているぽん。"
                )
            else:
                message = (
                    "退出処理中にエラーが発生したぽん。録音データはサーバーに保管してあるので、"
                    "管理者が復旧できるぽん。"
                )
            await ctx.followup.send(
                message,
                ephemeral=True,
            )
            return

        # Delivery now spans Drive, Knowledge, File Search and Discord. Keep a
        # recoverable marker until every durable destination has succeeded so
        # an exception in any later step cannot strand a minutes_generated job.
        if session.recording_sink:
            await session._mark_recording_sink(
                "manual_delivery_pending",
                transcript_path=str(session.transcript_path or ""),
            )

        # 逐語文字起こしは議事録とは別のGoogle Docとして保存する。
        from bot.gdrive import upload_minutes, upload_transcript

        export_decision = session.external_export_decision()
        await session.checkpoint_delivery_progress(
            external_export_expected=export_decision.allowed,
            external_export_reason=export_decision.reason,
        )
        if not export_decision.allowed:
            log.info(
                "External meeting export skipped guild=%s channel=%s reason=%s",
                ctx.guild_id,
                session.channel.id,
                export_decision.reason,
            )
        protected_source = export_decision.reason == "protected-channel"
        transcript_url = (
            await upload_transcript(
                bot.config,
                ctx.guild_id,
                session.transcript_text,
                channel_name,
                meeting_at=session.started_at,
                session_id=session.session_id,
            )
            if export_decision.allowed
            else None
        )
        await session.checkpoint_delivery_progress(
            transcript_url=transcript_url or "",
            transcript_path=str(session.transcript_path or ""),
        )

        if not minutes and has_audio:
            if session.recording_sink:
                await session._mark_recording_sink(
                    "minutes_generation_failed",
                    error="EmptyMinutes",
                    transcript_path=str(session.transcript_path or ""),
                )
            await ctx.followup.send(
                "議事録を生成できなかったぽん。文字起こしと録音は復旧用に保管してあるので、管理者が再試行できるぽん。",
                ephemeral=True,
            )
            return

        if not minutes:
            knowledge_url = (
                None if protected_source else await session.publish_knowledge()
            )
            from bot.meeting_index import index_bot_meeting

            if not protected_source:
                await index_bot_meeting(
                    bot,
                    guild_id=ctx.guild_id,
                    channel_id=session.channel.id,
                    parent_channel_id=getattr(session.channel, "parent_id", None),
                    category_id=getattr(session.channel, "category_id", None),
                    session_id=session.session_id,
                    channel_name=channel_name,
                    category_name=category_name,
                    started_at=session.started_at,
                    transcript=session.transcript_text,
                    transcript_url=transcript_url,
                )
            if not await session.mark_delivered(transcript_url=transcript_url):
                await ctx.followup.send(
                    "⚠️ 出力は保存できたものの、完了状態を記録できなかったぽん。"
                    "二重配信を防ぐため完了通知を止めたので、管理者に確認してほしいぽん。",
                    ephemeral=True,
                )
                return
            message = "退出したぽん！👋"
            if transcript_url:
                message += f"\n📜 [文字起こしをGoogle Docsで見る]({transcript_url})"
            if knowledge_url:
                message += f"\n🗂️ [KnowledgeのDraft PR]({knowledge_url})"
            capture_warning_reader = getattr(session, "capture_warning_text", None)
            capture_warning = (
                capture_warning_reader() if callable(capture_warning_reader) else ""
            )
            if capture_warning:
                message += f"\n⚠️ {capture_warning}"
            await ctx.followup.send(message, ephemeral=True)
            return

        # 議事録と逐語文字起こしをそれぞれ保存
        drive_url = (
            await upload_minutes(
                bot.config,
                ctx.guild_id,
                minutes,
                channel_name,
                meeting_at=session.started_at,
                session_id=session.session_id,
            )
            if export_decision.allowed
            else None
        )
        await session.checkpoint_delivery_progress(
            transcript_url=transcript_url or "",
            minutes_url=drive_url or "",
            transcript_path=str(session.transcript_path or ""),
        )

        # Geminiで要約を生成
        summary = await _summarize_minutes(minutes)
        knowledge_url = (
            None
            if protected_source
            else await session.publish_knowledge(summary=summary, minutes=minutes)
        )
        from bot.meeting_index import index_bot_meeting

        if not protected_source:
            await index_bot_meeting(
                bot,
                guild_id=ctx.guild_id,
                channel_id=session.channel.id,
                parent_channel_id=getattr(session.channel, "parent_id", None),
                category_id=getattr(session.channel, "category_id", None),
                session_id=session.session_id,
                channel_name=channel_name,
                category_name=category_name,
                started_at=session.started_at,
                transcript=session.transcript_text,
                minutes=minutes,
                transcript_url=transcript_url,
                minutes_url=drive_url,
            )

        # Discordには要約 + Google Docsリンクを送信
        embed = discord.Embed(
            title=f"📝 議事録 - {channel_name}",
            description=summary[:4096],
            color=discord.Color.blue(),
        )

        if drive_url:
            embed.add_field(name="📄 全文", value=f"[Google Docsで見る]({drive_url})", inline=False)
        else:
            if export_decision.allowed:
                embed.set_footer(text="⚠️ Google Driveへの保存に失敗したため、ファイルを添付します")
            else:
                embed.set_footer(text="🔒 限定チャンネルのため、外部保存を行わず元チャンネルに添付します")
        if transcript_url:
            embed.add_field(
                name="📜 逐語文字起こし",
                value=f"[Google Docsで見る]({transcript_url})",
                inline=False,
            )
        if knowledge_url:
            embed.add_field(
                name="🗂️ Knowledge",
                value=f"[Draft PRを確認する]({knowledge_url})",
                inline=False,
            )
        capture_warning_reader = getattr(session, "capture_warning_text", None)
        capture_warning = (
            capture_warning_reader() if callable(capture_warning_reader) else ""
        )
        if capture_warning:
            embed.add_field(
                name="⚠️ 録音の完全性",
                value=capture_warning,
                inline=False,
            )

        import io

        files = []
        if not drive_url:
            files.append(
                discord.File(
                    io.BytesIO(minutes.encode("utf-8")),
                    filename=f"議事録_{channel_name}.md",
                )
            )
        if session.transcript_text and not transcript_url:
            files.append(
                discord.File(
                    io.BytesIO(session.transcript_text.encode("utf-8")),
                    filename=f"文字起こし_{channel_name}.md",
                )
            )
        send_kwargs = {"embed": embed, "silent": True}
        if files:
            send_kwargs["files"] = files
        # Durable outputs have completed. Mark first so an optional Discord
        # notification outage cannot trigger duplicate Drive/Knowledge output.
        if not await session.mark_delivered(
            drive_url=drive_url,
            transcript_url=transcript_url,
        ):
            await ctx.followup.send(
                "⚠️ 出力は保存できたものの、完了状態を記録できなかったぽん。"
                "二重配信を防ぐため議事録通知を止めたので、管理者に確認してほしいぽん。",
                ephemeral=True,
            )
            return
        destination = resolve_minutes_notification_channel(
            bot.config,
            ctx.guild,
            session.channel,
        )
        if destination:
            try:
                await destination.send(**send_kwargs)
            except Exception:
                log.exception(
                    "Minutes notification failed after durable delivery guild=%s",
                    ctx.guild_id,
                )
                await ctx.followup.send(
                    "保存は完了したけれど、通知チャンネルへの投稿に失敗したぽん。",
                    ephemeral=True,
                )
        else:
            await ctx.followup.send(**{**send_kwargs, "ephemeral": True, "silent": False})


async def _summarize_minutes(minutes: str) -> str:
    """Create a labelled notification overview separate from full minutes."""
    from google import genai

    try:
        client = genai.Client(api_key=os.environ.get("GOOGLE_API_KEY", ""))
        model = fast_model()
        response = await asyncio.wait_for(
            client.aio.models.generate_content(
                model=model,
                contents=(
                    "以下の議事録からDiscord通知用の概要を作成してください。\n"
                    "これは議事録全文の代替ではありません。参加者、主な議題、決定事項、未決事項を含め、"
                    "内容が多い場合は無理に3〜5行へ押し込まず、必要な箇条書きを増やしてください。\n"
                    "語尾は「ぽん」をつけてください。\n\n"
                    f"{minutes}"
                ),
                config=generation_config(model, thinking_level="low", max_output_tokens=2048),
            ),
            timeout=90,
        )
        require_complete_model_output(response, operation="minutes_notification_summary")
        return format_notification_summary(response.text or "")
    except Exception:
        log.exception("Minutes notification summary generation failed")
        return notification_summary_failure()
