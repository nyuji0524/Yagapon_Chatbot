"""
/minutes - 音声ファイルから議事録を作成（声紋による話者識別付き）
対面会議の録音ファイルをアップロードして議事録を生成する
"""

import asyncio
import io
import logging
import os

import discord
from google import genai
from google.genai import types

from bot.commands.voiceprint import get_voiceprints_with_names

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

            # 声紋ファイルを取得
            voiceprints = get_voiceprints_with_names(bot.config, ctx.guild_id)
            glossary_text = bot.config.get_glossary_text(ctx.guild_id)

            # Geminiで文字起こし+議事録生成
            minutes = await _generate_minutes_from_file(
                audio_bytes, mime_type, file.filename,
                voiceprints, glossary_text, title,
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

            if voiceprints:
                embed.add_field(
                    name="🎙️ 声紋照合",
                    value=f"{len(voiceprints)}人の声紋で話者を識別したぽん",
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
    voiceprints: dict[str, str],
    glossary_text: str,
    title: str,
) -> str | None:
    """音声ファイル + 声紋からGeminiで議事録を生成"""
    client = genai.Client(api_key=os.environ.get("GOOGLE_API_KEY", ""))
    loop = asyncio.get_event_loop()

    try:
        # メイン音声ファイルをアップロード
        uploaded_main = await loop.run_in_executor(
            None,
            lambda: client.files.upload(
                file=io.BytesIO(audio_bytes),
                config={"mime_type": mime_type, "display_name": filename},
            ),
        )

        # 声紋ファイルをアップロード
        uploaded_voiceprints = []
        for name, path in voiceprints.items():
            try:
                with open(path, 'rb') as f:
                    vp_bytes = f.read()
                uploaded_vp = await loop.run_in_executor(
                    None,
                    lambda b=vp_bytes, n=name: client.files.upload(
                        file=io.BytesIO(b),
                        config={"mime_type": "audio/wav", "display_name": f"voiceprint-{n}"},
                    ),
                )
                uploaded_voiceprints.append({"name": name, "file": uploaded_vp})
            except Exception as e:
                log.warning(f"Failed to upload voiceprint for {name}: {e}")

        # プロンプト構築
        contents = []

        # 声紋サンプルを先に提供
        if uploaded_voiceprints:
            vp_intro = "以下は参加者の声紋サンプルです。会議音声の中でこれらの声を識別してください。\n\n"
            for vp in uploaded_voiceprints:
                vp_intro += f"【{vp['name']}の声】\n"
                contents.append(vp_intro)
                contents.append(types.Part.from_uri(file_uri=vp['file'].uri, mime_type="audio/wav"))
                vp_intro = ""

        # メイン音声
        contents.append("\n\n【会議の録音】\n")
        contents.append(types.Part.from_uri(file_uri=uploaded_main.uri, mime_type=mime_type))

        # 指示
        instruction = (
            "\n\n上記の会議音声から、構造化された議事録を作成してください。\n\n"
            "## 出力フォーマット\n"
            f"# {title or '議事録'}\n\n"
            "## 基本情報\n"
            "- 日時: （推定できれば）\n"
            "- 参加者: （声紋から識別した名前を使用。識別できない場合は「話者A」「話者B」等）\n\n"
            "## 議題\n"
            "- （議論されたトピックを箇条書き）\n\n"
            "## 議論内容\n"
            "（話者名付きで議論の流れを記載。重要な発言は引用形式で）\n\n"
            "## 決定事項\n"
            "- （決まったこと）\n\n"
            "## アクションアイテム\n"
            "- 【担当者】内容（期限）\n\n"
        )

        if uploaded_voiceprints:
            instruction += (
                "## 話者識別のルール\n"
                "- 提供された声紋サンプルと会議音声の声を照合し、可能な限り実名で記載\n"
                "- 声紋と一致しない話者は「話者A」「話者B」等で区別\n"
                "- 識別に自信がない場合は「（推定）」と付記\n\n"
            )

        if glossary_text:
            instruction += f"## 用語辞書（音声認識の参考）\n{glossary_text}\n\n"

        contents.append(instruction)

        # Geminiで生成
        response = await client.aio.models.generate_content(
            model="gemini-2.5-flash",
            contents=contents,
        )

        # アップロードファイルを削除
        try:
            await loop.run_in_executor(None, lambda: client.files.delete(name=uploaded_main.name))
            for vp in uploaded_voiceprints:
                await loop.run_in_executor(None, lambda f=vp['file']: client.files.delete(name=f.name))
        except Exception:
            pass

        return response.text

    except Exception as e:
        log.error(f"Gemini minutes generation error: {e}")
        return None


async def _summarize(minutes: str) -> str:
    """議事録を要約"""
    client = genai.Client(api_key=os.environ.get("GOOGLE_API_KEY", ""))
    try:
        response = await client.aio.models.generate_content(
            model="gemini-2.5-flash",
            contents=(
                "以下の議事録を3〜5行で簡潔に要約してください。\n"
                "要約には: 参加者、主な議題、決定事項を含めてください。\n"
                "語尾は「ぽん」をつけてください。\n\n"
                f"{minutes}"
            ),
        )
        return response.text or "要約を生成できなかったぽん..."
    except Exception:
        return minutes[:500] + ("\n\n..." if len(minutes) > 500 else "")
