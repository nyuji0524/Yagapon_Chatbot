"""
/minutes - 音声ファイルから議事録を作成（声紋による話者識別付き）
対面会議の録音ファイルをアップロードして議事録を生成する
"""

import io
import logging
import os

import discord
from google import genai

from bot.ai_models import fast_model, generation_config, log_usage, response_model
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

        try:
            # 音声ファイルをダウンロード
            audio_bytes = await file.read()
            mime_type = _get_mime_type(ext)

            glossary_text = bot.config.get_glossary_text(ctx.guild_id)

            # 専用STTで話者分離し、回答モデルで議事録を整形
            minutes = await _generate_minutes_from_file(
                audio_bytes, mime_type, file.filename,
                glossary_text, title,
            )

            if not minutes:
                await ctx.followup.send("議事録の生成に失敗したぽん...", silent=True)
                return

            # Google Docsに保存
            from bot.gdrive import upload_minutes
            doc_title = title or f"議事録_{file.filename}"
            drive_url = await upload_minutes(bot.config, ctx.guild_id, minutes, doc_title)

            # 要約を生成
            summary = await _summarize(minutes)

            # Discordに送信
            embed = discord.Embed(
                title=f"📝 {doc_title}",
                description=summary[:4096],
                color=discord.Color.blue(),
            )

            embed.add_field(
                name="🎙️ AI文字起こし",
                value="話者ラベルはAIの推定だぽん。実名との対応は人が確認してねぽん",
                inline=True,
            )

            if drive_url:
                embed.add_field(name="📄 全文", value=f"[Google Docsで見る]({drive_url})", inline=False)
                await ctx.followup.send(embed=embed, silent=True)
            else:
                # Drive保存失敗時はファイル添付
                md_file = discord.File(
                    io.BytesIO(minutes.encode("utf-8")),
                    filename=f"{doc_title}.md",
                )
                await ctx.followup.send(embed=embed, file=md_file, silent=True)

        except Exception as e:
            log.error(f"Minutes generation error: {e}")
            await ctx.followup.send(f"エラーが出ちゃったぽん...: {e}", silent=True)


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
) -> str | None:
    """専用STTで文字起こしし、別モデルで構造化議事録を生成。"""
    client = genai.Client(api_key=os.environ.get("GOOGLE_API_KEY", ""))

    try:
        try:
            transcript = await transcribe_audio(
                client,
                audio_bytes,
                mime_type=mime_type,
                display_name=filename,
                diarization=True,
            )
        except Exception as exc:
            # 30分超など話者分離の制限に当たった場合も通常文字起こしで救済する。
            message = str(exc).lower()
            retryable = getattr(exc, "code", None) == 400 or any(
                hint in message
                for hint in ("diarization", "duration", "30 minute", "invalid argument")
            )
            if not retryable:
                raise
            log.warning("Diarized transcription failed; retrying without diarization: %s", exc)
            transcript = await transcribe_audio(
                client,
                audio_bytes,
                mime_type=mime_type,
                display_name=filename,
            )

        if not transcript:
            return None

        instruction = (
            "以下の文字起こしから、事実を補わず構造化された議事録を作成してください。\n\n"
            "## 出力フォーマット\n"
            f"# {title or '議事録'}\n\n"
            "## 基本情報\n"
            "- 日時: （推定できれば）\n"
            "- 参加者: （文字起こしの話者ラベルを使用）\n\n"
            "## 議題\n"
            "- （議論されたトピックを箇条書き）\n\n"
            "## 議論内容\n"
            "（話者名付きで議論の流れを記載。重要な発言は引用形式で）\n\n"
            "## 決定事項\n"
            "- （決まったこと）\n\n"
            "## アクションアイテム\n"
            "- 【担当者】内容（期限）\n\n"
            "## 注意\n"
            "- spk_1等の話者ラベルを実名だと推測しない\n"
            "- 聞き取れない箇所や不明な担当者は不明と記載する\n\n"
        )

        if glossary_text:
            instruction += f"## 用語辞書（表記修正の参考）\n{glossary_text}\n\n"

        model = response_model()
        response = await client.aio.models.generate_content(
            model=model,
            contents=f"{instruction}\n## 文字起こし\n{transcript}",
            config=generation_config(model, thinking_level="medium", max_output_tokens=4096),
        )
        log_usage(log, "minutes_format", model, response)
        return response.text

    except Exception as e:
        log.error(f"Gemini minutes generation error: {e}")
        return None


async def _summarize(minutes: str) -> str:
    """議事録を要約"""
    client = genai.Client(api_key=os.environ.get("GOOGLE_API_KEY", ""))
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
