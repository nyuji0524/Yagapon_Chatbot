"""VC 3モード - listen / meeting / chat + 音声録音・文字起こし・リアルタイム応答"""

import asyncio
import io
import logging
import os
import tempfile
from enum import Enum

import discord
from google import genai
from google.genai import types

log = logging.getLogger("yagapon.voice")

# リアルタイム処理の間隔（秒）
REALTIME_INTERVAL = 15


class VoiceMode(Enum):
    LISTEN = "listen"    # 聞き専: 議事録作成のみ
    MEETING = "meeting"  # 参加者: 議事録 + リアルタイム応答
    CHAT = "chat"        # おしゃべり: リアルタイム応答（議事録なし）


class VoiceSession:
    """1つのVCセッションを管理"""

    def __init__(self, bot, guild_id: int, channel: discord.VoiceChannel, mode: VoiceMode):
        self.bot = bot
        self.guild_id = guild_id
        self.channel = channel
        self.mode = mode
        self.voice_client: discord.VoiceClient | None = None
        self.transcript: list[str] = []  # テキストチャットの記録
        self.full_audio: dict[int, bytearray] = {}  # user_id -> 全音声（議事録用）
        self.is_active = False
        self._recording_done = asyncio.Event()
        self._realtime_task: asyncio.Task | None = None
        self._conversation_history: list[dict] = []  # chat/meetingの会話履歴

    async def start(self):
        self.voice_client = await self.channel.connect()
        self.is_active = True
        log.info(f"VC joined: {self.channel.name} (mode={self.mode.value})")

        # 接続が安定するまで待つ
        await asyncio.sleep(2)

        # 録音開始
        if self.voice_client and self.voice_client.is_connected():
            try:
                sink = discord.sinks.WaveSink()
                self.voice_client.start_recording(
                    sink,
                    self._recording_finished,
                )
                log.info(f"Recording started in {self.channel.name}")
            except Exception as e:
                log.error(f"Failed to start recording: {e}")

            # meeting/chatモードならリアルタイム処理ループ開始
            if self.mode in (VoiceMode.MEETING, VoiceMode.CHAT):
                self._realtime_task = asyncio.create_task(self._realtime_loop())
        else:
            log.error("Voice client not connected after join")

    def _recording_finished(self, error):
        """録音完了コールバック（同期）"""
        if error:
            log.error(f"Recording finished with error: {error}")
        else:
            log.info("Recording finished callback called")
        self._recording_done.set()

    async def _realtime_loop(self):
        """定期的に音声を取得 → 文字起こし → 応答"""
        log.info(f"Realtime loop started (interval={REALTIME_INTERVAL}s)")
        await asyncio.sleep(REALTIME_INTERVAL)  # 最初の間隔を待つ

        while self.is_active:
            try:
                log.info("Processing realtime chunk...")
                await self._process_realtime_chunk()
            except Exception as e:
                log.error(f"Realtime processing error: {e}")
            await asyncio.sleep(REALTIME_INTERVAL)

    async def _process_realtime_chunk(self):
        """現在の録音チャンクを取得 → 文字起こし → 応答判定"""
        if not self.voice_client or not self.voice_client.is_connected():
            return

        # 録音を一時停止して音声を取得
        self._recording_done.clear()
        try:
            if hasattr(self.voice_client, 'recording') and self.voice_client.recording:
                self.voice_client.stop_recording()
                await asyncio.wait_for(self._recording_done.wait(), timeout=10)
        except (asyncio.TimeoutError, Exception) as e:
            log.warning(f"Chunk recording stop error: {e}")

        # すぐに録音を再開
        if self.is_active and self.voice_client and self.voice_client.is_connected():
            try:
                sink = discord.sinks.WaveSink()
                self.voice_client.start_recording(
                    sink,
                    self._recording_finished,
                )
            except Exception as e:
                log.error(f"Failed to restart recording: {e}")
                return

        # 直前のチャンクの音声を文字起こし
        chunk_texts = []
        for user_id, audio_data in list(self.full_audio.items()):
            # 最新のチャンク分だけ処理（簡易: full_audioの末尾）
            if len(audio_data) < 1000:
                continue

            member = self.channel.guild.get_member(user_id)
            name = member.display_name if member else f"User {user_id}"

            # 直近の音声チャンクのみ文字起こし
            text = await self._transcribe_audio(bytes(audio_data[-500000:]), name)
            if text and text.strip():
                chunk_texts.append({"speaker": name, "text": text})
                self.transcript.append(f"[{name}]: {text}")

        if not chunk_texts:
            return

        # 応答が必要か判定 → 応答生成
        response = await self._generate_realtime_response(chunk_texts)
        if response:
            await self.speak(response)
            self._conversation_history.append({"role": "assistant", "text": response})

    async def _generate_realtime_response(self, chunk_texts: list[dict]) -> str | None:
        """文字起こし結果から応答が必要か判定し、必要なら応答を生成"""
        client = genai.Client(api_key=os.environ.get("GOOGLE_API_KEY", ""))

        # 会話履歴を含めたコンテキスト
        history = ""
        if self._conversation_history:
            recent = self._conversation_history[-10:]  # 直近10件
            history = "\n".join(
                f"[{'やがぽん' if h['role'] == 'assistant' else h.get('speaker', '?')}]: {h['text']}"
                for h in recent
            )

        current = "\n".join(f"[{t['speaker']}]: {t['text']}" for t in chunk_texts)

        # 会話履歴に追加
        for t in chunk_texts:
            self._conversation_history.append({"role": "user", "speaker": t["speaker"], "text": t["text"]})

        # メンバー情報と語録を取得
        members_info = self.bot._build_members_info(self.guild_id) if hasattr(self.bot, '_build_members_info') else ""
        glossary_text = self.bot.config.get_glossary_text(self.guild_id)

        if self.mode == VoiceMode.CHAT:
            prompt = (
                "あなたは慶應義塾大学 矢上祭実行委員会のマスコット「やがぽん」です。\n"
                "ボイスチャンネルで友達とおしゃべりしています。\n\n"
                "【行動ルール】\n"
                "- 誰かが話したら必ず返事をする。「SKIP」は絶対に使わない\n"
                "- 質問されたら、ナレッジベースを検索して具体的に回答する\n"
                "- 雑談には楽しくノリよく返す。相槌・感想・質問返しなど自然に\n"
                "- 返答は短く自然に（1-3文）。語尾に「ぽん」をつける\n"
                "- 質問への回答はしっかり答えつつも簡潔に\n"
                "- 明るく元気なキャラクターで、会話を盛り上げる\n\n"
            )
        else:  # MEETING
            prompt = (
                "あなたは会議に参加している「やがぽん」です。\n\n"
                "【行動ルール】\n"
                "- 名前（やがぽん）を呼ばれたら必ず返答する\n"
                "- 質問されたらナレッジベースを検索して回答する\n"
                "- 重要な情報を補足できるときは発言する\n"
                "- それ以外は「SKIP」とだけ返す\n"
                "- 返答は簡潔に。語尾に「ぽん」をつける\n\n"
            )

        if members_info:
            prompt += f"【メンバー情報】\n{members_info}\n\n"
        if glossary_text:
            prompt += f"【用語辞書】\n{glossary_text}\n\n"
        if history:
            prompt += f"=== これまでの会話 ===\n{history}\n\n"
        prompt += f"=== 今の発言 ===\n{current}"

        try:
            # chat/meetingどちらもRAG検索を使う
            corpus = self.bot.config.get_corpus(self.guild_id)
            if corpus:
                response = await client.aio.models.generate_content(
                    model="gemini-2.5-flash",
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        tools=[
                            types.Tool(
                                file_search=types.FileSearch(
                                    file_search_store_names=[corpus]
                                )
                            )
                        ],
                    ),
                )
            else:
                response = await client.aio.models.generate_content(
                    model="gemini-2.5-flash",
                    contents=prompt,
                )

            text = (response.text or "").strip()
            # chatモードではSKIPしない（必ず返事する）
            if self.mode == VoiceMode.CHAT:
                if not text or text.upper() == "SKIP":
                    import random
                    fallbacks = [
                        "うんうん、なるほどぽん！",
                        "へぇ〜、そうなんだぽん！",
                        "おもしろいぽん！",
                        "わかるわかるぽん〜！",
                        "それいいねぽん！",
                        "ふむふむ、続きが気になるぽん！",
                        "すごいぽん！",
                        "たしかに〜ぽん！",
                        "えー！まじぽん？",
                        "なるほどなるほどぽん〜",
                        "いいと思うぽん！",
                        "ほほ〜、勉強になるぽん！",
                    ]
                    return random.choice(fallbacks)
                return text
            else:
                if text.upper() == "SKIP" or not text:
                    return None
                return text
        except Exception as e:
            log.error(f"Realtime response error: {e}")
            return None

    async def stop(self) -> str | None:
        self.is_active = False

        # リアルタイム処理を停止
        if self._realtime_task:
            self._realtime_task.cancel()
            try:
                await self._realtime_task
            except asyncio.CancelledError:
                pass

        if self.voice_client and self.voice_client.is_connected():
            # 録音を停止してデータを取得
            try:
                if self.voice_client.is_recording():
                    # stop前にsinkへの参照を保持
                    sink = None
                    if hasattr(self.voice_client, '_reader') and self.voice_client._reader:
                        sink = self.voice_client._reader.sink

                    self.voice_client.stop_recording()
                    await asyncio.sleep(1)  # データ書き込み完了を待つ

                    # sinkからデータを読み取り
                    if sink:
                        self._read_from_sink(sink)
                    else:
                        log.warning("No sink reference found before stop")
            except Exception as e:
                log.warning(f"Stop recording error: {e}")

            await self.voice_client.disconnect()

        if self.mode in (VoiceMode.LISTEN, VoiceMode.MEETING):
            return await self._generate_minutes()
        return None

    def _read_from_sink(self, sink):
        """sinkからオーディオデータを読み取る"""
        try:
            audio_data = getattr(sink, 'audio_data', {})
            log.info(f"Sink has {len(audio_data)} user(s) audio data")
            for user_id, audio in audio_data.items():
                if hasattr(audio, 'file'):
                    audio.file.seek(0)
                    audio_bytes = audio.file.read()
                elif isinstance(audio, bytes):
                    audio_bytes = audio
                else:
                    audio_bytes = bytes(audio)

                log.info(f"User {user_id}: {len(audio_bytes)} bytes of audio")
                if user_id not in self.full_audio:
                    self.full_audio[user_id] = bytearray()
                self.full_audio[user_id].extend(audio_bytes)
            log.info(f"Total audio: {sum(len(v) for v in self.full_audio.values())} bytes from {len(self.full_audio)} users")
        except Exception as e:
            log.error(f"Sink read error: {e}")

    async def _transcribe_audio(self, audio_bytes: bytes, speaker_name: str) -> str:
        """Gemini Audio APIで音声を文字起こし"""
        if len(audio_bytes) < 1000:  # ほぼ無音
            return ""

        client = genai.Client(api_key=os.environ.get("GOOGLE_API_KEY", ""))

        try:
            loop = asyncio.get_event_loop()
            uploaded = await loop.run_in_executor(
                None,
                lambda: client.files.upload(
                    file=io.BytesIO(audio_bytes),
                    config={"mime_type": "audio/wav", "display_name": f"vc-{speaker_name}"},
                ),
            )

            # 語録があれば文字起こしに活用
            glossary_hint = ""
            if self.bot and hasattr(self.bot, 'config'):
                glossary_text = self.bot.config.get_glossary_text(self.guild_id)
                if glossary_text:
                    glossary_hint = f"\n\n以下は組織特有の用語です。音声に出てきた場合は正しく表記してください:\n{glossary_text}"

            response = await client.aio.models.generate_content(
                model="gemini-2.5-flash",
                contents=[
                    types.Part.from_uri(file_uri=uploaded.uri, mime_type="audio/wav"),
                    f"この音声を日本語で文字起こししてください。話者は「{speaker_name}」です。\n"
                    "発言内容をそのまま書き起こしてください。無音部分は省略してください。\n"
                    f"無音のみの場合は空文字を返してください。{glossary_hint}"
                ],
            )

            # アップロードしたファイルを削除
            try:
                await loop.run_in_executor(None, lambda: client.files.delete(name=uploaded.name))
            except Exception:
                pass

            return response.text or ""
        except Exception as e:
            log.error(f"Transcription error for {speaker_name}: {e}")
            return ""

    async def _generate_minutes(self) -> str:
        """全体の音声文字起こし + テキストから議事録を生成"""
        # リアルタイム処理で既に文字起こし済みならtranscriptを使う
        if self.transcript:
            all_content = self.transcript
        else:
            # 未処理の音声を文字起こし
            all_content = []
            for user_id, audio_bytes in self.full_audio.items():
                if len(audio_bytes) < 1000:
                    continue
                member = self.channel.guild.get_member(user_id)
                name = member.display_name if member else f"User {user_id}"
                text = await self._transcribe_audio(bytes(audio_bytes), name)
                if text:
                    all_content.append(f"【{name}】\n{text}")

        if not all_content:
            return "会議の内容が記録されていないぽん..."

        transcript_text = "\n".join(all_content)

        client = genai.Client(api_key=os.environ.get("GOOGLE_API_KEY", ""))
        response = await client.aio.models.generate_content(
            model="gemini-2.5-flash",
            contents=(
                "以下の会議の記録から、構造化された議事録を作成してください。\n"
                "形式: 日時、参加者、議題、議論内容、決定事項、アクションアイテム\n\n"
                f"{transcript_text}"
            ),
        )
        return response.text

    async def speak(self, text: str):
        """TTSで発話"""
        if not self.voice_client or not self.voice_client.is_connected():
            return

        from bot.tts import VOICE, PITCH, RATE
        try:
            import edge_tts

            communicate = edge_tts.Communicate(text[:500], VOICE, pitch=PITCH, rate=RATE)
            tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
            tmp_path = tmp.name
            tmp.close()
            await communicate.save(tmp_path)

            if self.voice_client.is_playing():
                self.voice_client.stop()

            self.voice_client.play(discord.FFmpegPCMAudio(tmp_path))
            while self.voice_client.is_playing():
                await asyncio.sleep(0.5)

            os.remove(tmp_path)
        except Exception as e:
            log.error(f"Voice speak error: {e}")


# グローバルセッション管理
_sessions: dict[int, VoiceSession] = {}  # guild_id -> session


async def join_voice(bot, guild_id: int, channel: discord.VoiceChannel, mode: VoiceMode) -> VoiceSession:
    # 既存セッションがあれば切断
    if guild_id in _sessions:
        await _sessions[guild_id].stop()

    session = VoiceSession(bot, guild_id, channel, mode)
    await session.start()
    _sessions[guild_id] = session
    return session


async def leave_voice(guild_id: int) -> str | None:
    session = _sessions.pop(guild_id, None)
    if session:
        return await session.stop()
    return None


def get_session(guild_id: int) -> VoiceSession | None:
    return _sessions.get(guild_id)
