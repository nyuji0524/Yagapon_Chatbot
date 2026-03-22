"""
/voiceprint register - 声紋登録（VCで10秒間録音）
/voiceprint list - 登録済み声紋一覧
/voiceprint delete - 自分の声紋を削除
"""

import asyncio
import os
import logging

import discord

log = logging.getLogger("yagapon.voiceprint")

import io
from google import genai
from google.genai import types

VOICEPRINT_DIR = "voiceprints"
RECORD_DURATION = 10  # 秒


async def _validate_voice(audio_bytes: bytes, speaker_name: str) -> dict:
    """Geminiで音声を検証。人の声が含まれているか確認"""
    client = genai.Client(api_key=os.environ.get("GOOGLE_API_KEY", ""))
    try:
        loop = asyncio.get_event_loop()
        uploaded = await loop.run_in_executor(
            None,
            lambda: client.files.upload(
                file=io.BytesIO(audio_bytes),
                config={"mime_type": "audio/wav", "display_name": f"voiceprint-{speaker_name}"},
            ),
        )

        response = await client.aio.models.generate_content(
            model="gemini-2.5-flash",
            contents=[
                types.Part.from_uri(file_uri=uploaded.uri, mime_type="audio/wav"),
                "この音声ファイルを分析して、以下のJSON形式で回答してください。\n"
                '{"has_voice": true/false, "quality": "good"/"poor"/"silent", "description": "声の特徴を褒めるコメント"}\n\n'
                "判定基準:\n"
                "- has_voice: 人の声が含まれているか\n"
                "- quality: good=声紋登録に十分な音質, poor=ノイズが多い/声が小さい, silent=無音\n"
                "- description: 声の特徴を1行で褒めてください！語尾は「ぽん」で。\n"
                "  例: 「落ち着いたダンディーな声だぽん！」「明るくてハキハキした素敵な声だぽん！」「優しくて聞き心地の良い声だぽん！」\n"
                "JSONのみ返してください。"
            ],
        )

        # アップロードファイル削除
        try:
            await loop.run_in_executor(None, lambda: client.files.delete(name=uploaded.name))
        except Exception:
            pass

        import json
        text = response.text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        result = json.loads(text)

        if not result.get("has_voice"):
            return {"ok": False, "reason": "音声に人の声が検出されなかったぽん。マイクがONか確認してねぽん。"}
        if result.get("quality") == "silent":
            return {"ok": False, "reason": "無音だったぽん。マイクがミュートになってないか確認してねぽん。"}
        if result.get("quality") == "poor":
            return {"ok": False, "reason": "音質が悪いぽん。ノイズが少ない環境でもう一度試してねぽん。"}

        return {"ok": True, "summary": result.get("description", "声紋OK")}

    except Exception as e:
        log.error(f"Voice validation error: {e}")
        # 検証失敗時は通す（録音データ自体はある）
        return {"ok": True, "summary": "（検証スキップ）"}


def _get_voiceprint_path(guild_id: int, user_id: int) -> str:
    guild_dir = os.path.join(VOICEPRINT_DIR, str(guild_id))
    os.makedirs(guild_dir, exist_ok=True)
    return os.path.join(guild_dir, f"{user_id}.wav")


def get_voiceprints_with_names(config, guild_id: int) -> dict[str, str]:
    """ギルドの声紋ファイルをメンバー名つきで取得。{表示名: file_path}"""
    guild_dir = os.path.join(VOICEPRINT_DIR, str(guild_id))
    if not os.path.exists(guild_dir):
        return {}

    members = config.get_members(guild_id)
    result = {}
    for f in os.listdir(guild_dir):
        if f.endswith(".wav"):
            uid = f.replace(".wav", "")
            member_info = members.get(uid, {})
            # nickname > name > User ID の優先順
            name = member_info.get("nickname") or member_info.get("name") or f"User {uid}"
            result[name] = os.path.join(guild_dir, f)
    return result


def register(bot):
    group = bot.create_group("voiceprint", "声紋登録だぽん！")

    @group.command(name="register", description="声紋を登録するぽん！（VCで10秒間録音）")
    @discord.option("user", description="他のメンバーの声紋を登録する場合（省略で自分）", type=discord.Member, required=False, default=None)
    async def voiceprint_register(ctx: discord.ApplicationContext, user: discord.Member = None):
        target = user or ctx.author
        member = ctx.guild.get_member(target.id)
        if not member or not member.voice or not member.voice.channel:
            if user:
                await ctx.respond(
                    f"**{target.display_name}** がボイスチャンネルにいないぽん！", ephemeral=True
                )
            else:
                await ctx.respond(
                    "先にボイスチャンネルに入ってほしいぽん！", ephemeral=True
                )
            return

        # メンバー登録されているか確認
        members = bot.config.get_members(ctx.guild_id)
        uid = str(target.id)
        if uid not in members:
            await ctx.respond(
                f"**{target.display_name}** はメンバー登録されてないぽん。\n"
                f"先に `/member sync` でメンバー登録してねぽん。",
                ephemeral=True,
            )
            return

        await ctx.defer()
        vc_channel = member.voice.channel

        # VC接続
        voice_client = None
        try:
            for vc in bot.voice_clients:
                if vc.guild == ctx.guild:
                    voice_client = vc
                    if vc.channel != vc_channel:
                        await vc.move_to(vc_channel)
                    break

            if voice_client is None:
                voice_client = await vc_channel.connect()

            await asyncio.sleep(1)

            # 録音開始
            sink = discord.sinks.WaveSink()
            voice_client.start_recording(sink, lambda err: None)

            display_name = members[uid].get("nickname") or members[uid].get("name") or target.display_name
            await ctx.followup.send(
                f"🎙️ **{display_name}** さん、{RECORD_DURATION}秒間何か話してねぽん！\n"
                f"（自己紹介や好きな食べ物の話など何でもOKだぽん）",
                silent=True,
            )

            await asyncio.sleep(RECORD_DURATION)

            # 録音停止 → sinkからデータ取得
            recording_sink = None
            if hasattr(voice_client, '_reader') and voice_client._reader:
                recording_sink = voice_client._reader.sink

            voice_client.stop_recording()
            await asyncio.sleep(0.5)

            if recording_sink:
                audio_data = getattr(recording_sink, 'audio_data', {})
                user_audio = audio_data.get(target.id)

                if user_audio and hasattr(user_audio, 'file'):
                    user_audio.file.seek(0)
                    audio_bytes = user_audio.file.read()

                    if len(audio_bytes) > 1000:
                        # Geminiで音声を検証
                        await ctx.followup.send("🔍 音声を検証中だぽん...", silent=True)
                        validation = await _validate_voice(audio_bytes, display_name)

                        if not validation["ok"]:
                            await ctx.followup.send(
                                f"❌ 声紋登録に失敗したぽん...\n"
                                f"理由: {validation['reason']}\n"
                                f"もう一度 `/voiceprint register` を試してねぽん。",
                                silent=True,
                            )
                        else:
                            path = _get_voiceprint_path(ctx.guild_id, target.id)
                            with open(path, 'wb') as f:
                                f.write(audio_bytes)

                            # メンバー情報に声紋登録済みフラグを追加
                            members[uid]["voiceprint"] = True
                            await bot.config.set_members(ctx.guild_id, members)

                            size_kb = len(audio_bytes) // 1024
                            await ctx.followup.send(
                                f"✅ **{display_name}** の声紋を登録したぽん！（{size_kb}KB）\n"
                                f"検証結果: {validation['summary']}\n"
                                f"対面会議の録音時に声で話者を識別できるようになるぽん！",
                                silent=True,
                            )
                    else:
                        await ctx.followup.send(
                            "音声が短すぎるぽん...もう一度 `/voiceprint register` を試してねぽん。",
                            silent=True,
                        )
                else:
                    await ctx.followup.send(
                        f"**{display_name}** の音声が取得できなかったぽん...\n"
                        f"マイクがONになっているか確認して、もう一度試してねぽん。",
                        silent=True,
                    )
            else:
                await ctx.followup.send("録音データが取得できなかったぽん...", silent=True)

            # 他のセッションが使っていなければ切断
            from bot.voice import get_session
            if not get_session(ctx.guild_id):
                await voice_client.disconnect()

        except Exception as e:
            log.error(f"Voiceprint registration error: {e}")
            await ctx.followup.send(f"エラーが出ちゃったぽん...: {e}", silent=True)
            if voice_client and voice_client.is_connected():
                from bot.voice import get_session
                if not get_session(ctx.guild_id):
                    await voice_client.disconnect()

    @group.command(name="list", description="登録済みの声紋一覧を表示するぽん！")
    async def voiceprint_list(ctx: discord.ApplicationContext):
        voiceprints = get_voiceprints_with_names(bot.config, ctx.guild_id)
        if not voiceprints:
            await ctx.respond("まだ誰も声紋を登録してないぽん！\n`/voiceprint register` で登録してねぽん。", silent=True)
            return

        embed = discord.Embed(title="🎙️ 声紋登録済みメンバー", color=discord.Color.blue())
        for name, path in voiceprints.items():
            size = os.path.getsize(path) // 1024
            embed.add_field(name=name, value=f"{size}KB", inline=True)

        await ctx.respond(embed=embed, silent=True)

    @group.command(name="delete", description="声紋を削除するぽん")
    @discord.option("user", description="他のメンバーの声紋を削除する場合（省略で自分）", type=discord.Member, required=False, default=None)
    async def voiceprint_delete(ctx: discord.ApplicationContext, user: discord.Member = None):
        target = user or ctx.author
        path = _get_voiceprint_path(ctx.guild_id, target.id)
        display_name = target.display_name

        if os.path.exists(path):
            os.remove(path)
            # メンバー情報からフラグも削除
            members = bot.config.get_members(ctx.guild_id)
            uid = str(target.id)
            if uid in members:
                members[uid].pop("voiceprint", None)
                await bot.config.set_members(ctx.guild_id, members)

            await ctx.respond(f"✅ **{display_name}** の声紋を削除したぽん。", ephemeral=True)
        else:
            await ctx.respond(f"**{display_name}** の声紋は登録されてないぽん。", ephemeral=True)
