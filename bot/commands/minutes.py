"""
/minutes - 音声ファイルから議事録を作成（声紋による話者識別付き）
対面会議の録音ファイルをアップロードして議事録を生成する
"""

import asyncio
import io
import logging
import os
import shutil
from datetime import datetime, timezone
from uuid import uuid4

import discord

from bot.ai_models import fast_model, generation_config, log_usage
from bot.ai_retry import new_genai_client
from bot.authorization import require_registered_member
from bot.meeting_time import display_meeting_date
from bot.minutes_generation import generate_minutes
from bot.recording import save_external_transcript
from bot.transcription import transcribe_audio

log = logging.getLogger("yagapon.minutes")

# 対応する音声形式
AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".ogg", ".webm", ".mp4", ".flac", ".aac"}
MAX_FILE_SIZE = 100 * 1024 * 1024  # 100MB


def register(bot):
    @bot.slash_command(name="minutes", description="音声ファイルから議事録を作成するぽん！")
    @discord.option("file", description="会議の録音ファイル", type=discord.Attachment)
    @discord.option("title", description="議事録のタイトル（省略可）", required=False, default="")
    async def minutes_cmd(ctx: discord.ApplicationContext, file: discord.Attachment, title: str = ""):
        if not await require_registered_member(ctx, bot.config):
            return
        # ファイル形式チェック
        ext = os.path.splitext(file.filename)[1].lower()
        if ext not in AUDIO_EXTENSIONS:
            await ctx.respond(
                f"対応していないファイル形式だぽん...\n対応形式: {', '.join(AUDIO_EXTENSIONS)}",
                ephemeral=True,
            )
            return

        if file.size > MAX_FILE_SIZE:
            await ctx.respond("ファイルが大きすぎるぽん...（上限100MB）", ephemeral=True)
            return

        await ctx.defer()
        await ctx.followup.send("🎙️ 音声ファイルを処理中だぽん...しばらく待ってねぽん ⏳", silent=True)

        transcript = None
        transcript_path = None
        transcript_url = None
        session_id = ""
        doc_title = title or os.path.splitext(file.filename)[0]
        checkpoint_dir = None
        meeting_at = datetime.now(timezone.utc)
        try:
            # 音声ファイルをダウンロード
            audio_bytes = await file.read()
            mime_type = _get_mime_type(ext)

            glossary_text = bot.config.get_glossary_text(ctx.guild_id)

            # 文字起こしを先に恒久保存し、議事録生成失敗時にも失わない。
            transcript = await _transcribe_file(
                audio_bytes,
                mime_type,
                file.filename,
                voiceprint_paths=_voiceprint_paths(bot, ctx.guild_id),
            )
            if not transcript:
                if hasattr(bot, "operations"):
                    bot.operations.record_event(
                        "minutes", "uploaded_audio", "failed",
                        guild_id=ctx.guild_id,
                        details={"error_type": "empty_transcript"},
                    )
                await ctx.followup.send("文字起こしの生成に失敗したぽん...", silent=True)
                return
            session_id = (
                datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                + f"-upload-{uuid4().hex[:8]}"
            )
            transcript_path = await asyncio.to_thread(
                save_external_transcript,
                ctx.guild_id,
                session_id,
                title or file.filename,
                transcript,
            )

            # 生文字起こしは議事録生成より先に外部へ退避する。
            # 後続の要約APIが失敗しても、再度STTする必要はない。
            from bot.gdrive import upload_minutes, upload_transcript

            transcript_url = await upload_transcript(
                bot.config,
                ctx.guild_id,
                transcript,
                doc_title,
                meeting_at=meeting_at,
            )
            checkpoint_dir = (
                transcript_path.parent / f"{session_id}-checkpoints"
                if transcript_path else None
            )
            minutes = await generate_minutes(
                new_genai_client(os.environ.get("GOOGLE_API_KEY", "")),
                transcript,
                title=doc_title,
                glossary_text=glossary_text,
                speaker_roster=bot._build_members_info(ctx.guild_id),
                meeting_date=display_meeting_date(meeting_at),
                checkpoint_dir=checkpoint_dir,
            )
            if not minutes or not minutes.strip():
                raise RuntimeError("minutes generation returned empty content")

            # Google Docsに保存
            drive_url = await upload_minutes(
                bot.config,
                ctx.guild_id,
                minutes,
                doc_title,
                meeting_at=meeting_at,
            )

            # 要約を生成
            summary = await _summarize(minutes)

            from bot.knowledge_publish import publish_meeting_knowledge

            knowledge = await publish_meeting_knowledge(
                session_id=session_id,
                guild_id=ctx.guild_id,
                channel_name=doc_title,
                mode="uploaded",
                transcript=transcript,
                summary=summary,
                minutes=minutes,
            )

            # Discordに送信
            embed = discord.Embed(
                title=f"📝 {doc_title}",
                description=summary[:4096],
                color=discord.Color.blue(),
            )

            embed.add_field(
                name="🎙️ AI文字起こし",
                value="登録声紋と十分一致した話者だけ実名表示し、それ以外は匿名ラベルだぽん。最終確認してねぽん",
                inline=True,
            )

            if drive_url:
                embed.add_field(name="📄 全文", value=f"[Google Docsで見る]({drive_url})", inline=False)
            if transcript_url:
                embed.add_field(
                    name="📜 生文字起こし",
                    value=f"[Google Docsで見る]({transcript_url})",
                    inline=False,
                )
            if knowledge and knowledge.pull_request_url:
                embed.add_field(
                    name="🗂️ Knowledge",
                    value=f"[Draft PRを確認する]({knowledge.pull_request_url})",
                    inline=False,
                )
            if hasattr(bot, "operations"):
                bot.operations.record_event(
                    "minutes", "uploaded_audio", "success",
                    guild_id=ctx.guild_id, item_key=session_id,
                    details={
                        "transcript_saved": bool(transcript_path),
                        "drive_minutes": bool(drive_url),
                        "drive_transcript": bool(transcript_url),
                        "knowledge_outbox": bool(knowledge),
                    },
                )
            files = []
            if not drive_url:
                files.append(discord.File(
                    io.BytesIO(minutes.encode("utf-8")),
                    filename=f"{doc_title}.md",
                ))
            if not transcript_url:
                files.append(discord.File(
                    io.BytesIO(transcript.encode("utf-8")),
                    filename=f"文字起こし_{doc_title}.md",
                ))
            kwargs = {"embed": embed, "silent": True}
            if files:
                kwargs["files"] = files
            await ctx.followup.send(**kwargs)
            if checkpoint_dir:
                await asyncio.to_thread(shutil.rmtree, checkpoint_dir, True)

        except Exception as e:
            log.exception("Minutes generation error")
            if hasattr(bot, "operations"):
                bot.operations.record_event(
                    "minutes", "uploaded_audio", "failed",
                    guild_id=ctx.guild_id, item_key=session_id or None,
                    details={"error_type": type(e).__name__},
                )
            message = "議事録の生成中にエラーが発生したぽん。"
            files = []
            if transcript:
                message += f"\n生文字起こしは保存済みだぽん。セッション: `{session_id}`"
                if transcript_url:
                    message += f"\nGoogle Docs: {transcript_url}"
                else:
                    files.append(discord.File(
                        io.BytesIO(transcript.encode("utf-8")),
                        filename=f"文字起こし_{doc_title}.md",
                    ))
            else:
                message += "\n文字起こしは完了していないぽん。"
            kwargs = {"content": message, "silent": True}
            if files:
                kwargs["files"] = files
            await ctx.followup.send(**kwargs)


def _get_mime_type(ext: str) -> str:
    mime_map = {
        ".mp3": "audio/mpeg",
        ".wav": "audio/wav",
        ".m4a": "audio/mp4",
        ".ogg": "audio/ogg",
        ".webm": "audio/webm",
        ".mp4": "audio/mp4",
        ".flac": "audio/flac",
        ".aac": "audio/aac",
    }
    return mime_map.get(ext, "audio/mpeg")


async def _generate_minutes_from_file(
    audio_bytes: bytes,
    mime_type: str,
    filename: str,
    glossary_text: str,
    title: str,
    speaker_roster: str = "",
    voiceprint_paths: dict[str, str] | None = None,
) -> str | None:
    """専用STTで文字起こしし、別モデルで構造化議事録を生成。"""
    try:
        transcript = await _transcribe_file(
            audio_bytes,
            mime_type,
            filename,
            voiceprint_paths=voiceprint_paths,
        )

        if not transcript:
            return None

        return await generate_minutes(
            new_genai_client(os.environ.get("GOOGLE_API_KEY", "")),
            transcript,
            title=title,
            glossary_text=glossary_text,
            speaker_roster=speaker_roster,
        )

    except Exception as e:
        log.error(f"Gemini minutes generation error: {e}")
        return None


def _voiceprint_paths(bot, guild_id: int) -> dict[str, str]:
    from bot.commands.voiceprint import get_voiceprints_with_names

    return get_voiceprints_with_names(bot.config, guild_id)


async def _transcribe_file(
    audio_bytes: bytes,
    mime_type: str,
    filename: str,
    *,
    voiceprint_paths: dict[str, str] | None = None,
) -> str | None:
    """Diarize a whole in-person recording, with a non-diarized fallback."""
    client = new_genai_client(os.environ.get("GOOGLE_API_KEY", ""))
    try:
        try:
            return await transcribe_audio(
                client, audio_bytes, mime_type=mime_type,
                display_name=filename,
                diarization=True,
                voiceprint_paths=voiceprint_paths,
            )
        except Exception as exc:
            message = str(exc).lower()
            retryable = getattr(exc, "code", None) == 400 or any(
                hint in message
                for hint in ("diarization", "duration", "30 minute", "invalid argument")
            )
            if not retryable:
                raise
            log.warning("Diarized transcription failed; retrying without diarization: %s", exc)
            return await transcribe_audio(
                client, audio_bytes, mime_type=mime_type, display_name=filename,
            )
    except Exception:
        log.exception("Uploaded audio transcription failed")
        return None


async def _summarize(minutes: str) -> str:
    """議事録を要約"""
    client = new_genai_client(os.environ.get("GOOGLE_API_KEY", ""))
    try:
        model = fast_model()
        response = await client.aio.models.generate_content(
            model=model,
            contents=(
                "以下の議事録を3〜5行で簡潔に要約してください。\n"
                "要約には: 参加者、主な議題、決定事項を含めてください。\n"
                "語尾は「ぽん」をつけてください。\n\n"
                f"{minutes}"
            ),
            config=generation_config(model, thinking_level="low", max_output_tokens=512),
        )
        log_usage(log, "minutes_summary", model, response)
        return response.text or "要約を生成できなかったぽん..."
    except Exception:
        return minutes[:500] + ("\n\n..." if len(minutes) > 500 else "")
