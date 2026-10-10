"""VC 3モード - listen / meeting / chat + 音声録音・文字起こし・リアルタイム応答"""

import asyncio
import io
import logging
import os
import tempfile
import time
from enum import Enum

import discord
from google import genai
from google.genai import types

from bot.ai_models import generation_config, log_usage, response_model
from bot.ai_retry import RateLimitExceeded, new_genai_client
from bot.minutes_generation import generate_minutes
from bot.recording import PersistentPCMSink
from bot.transcription import transcribe_audio

log = logging.getLogger("yagapon.voice")

# リアルタイム処理の間隔（秒）
REALTIME_INTERVAL_CHAT = 5  # chatモード: 短い間隔で即応答
REALTIME_INTERVAL_MEETING = 15  # meetingモード: 会議の邪魔にならないよう長め
REALTIME_INTERVAL_LISTEN = 30  # listenモード: 文字起こし蓄積用
TRANSCRIPTION_CHUNK_BYTES = 5 * 1024 * 1024
TRANSCRIPTION_TIMEOUT_SECONDS = 180


class VoiceMode(Enum):
    LISTEN = "listen"  # 聞き専: 議事録作成のみ
    MEETING = "meeting"  # 参加者: 議事録 + リアルタイム応答
    CHAT = "chat"  # おしゃべり: リアルタイム応答（議事録なし）


class VoiceSession:
    """1つのVCセッションを管理"""

    def __init__(self, bot, guild_id: int, channel: discord.VoiceChannel, mode: VoiceMode):
        self.bot = bot
        self.guild_id = guild_id
        self.channel = channel
        self.mode = mode
        self.voice_client: discord.VoiceClient | None = None
        self.transcript: list[str] = []  # テキストチャットの記録
        self.recording_sink: PersistentPCMSink | None = None
        self.is_active = False
        self._recording_done = asyncio.Event()
        self._realtime_task: asyncio.Task | None = None
        self._stop_task: asyncio.Task[str | None] | None = None
        self._stop_lock = asyncio.Lock()
        self._stop_completed = False
        self._stop_result: str | None = None
        self._stop_error: BaseException | None = None
        self._delivery_claimed = False
        self._transcription_failed = False
        self._transcription_backoff_until = 0.0
        self._transcription_rate_limit_failures = 0
        self._speaker_labels: dict[int, str] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._conversation_history: list[dict] = []  # chat/meetingの会話履歴
        self._last_audio_len: dict[int, int] = {}  # user_id -> 文字起こし済みPCMバイト数
        self._reference_docs: list[str] = []  # VCテキストチャットに投稿された参考資料

    async def add_reference(self, content: str, source: str = "テキスト"):
        """VCテキストチャットに投稿された資料を参考資料として追加"""
        self._reference_docs.append(f"[{source}]\n{content}")
        log.info(f"Reference doc added: {source} ({len(content)} chars)")

    def _speaker_label(self, user_id: int) -> str:
        """Return a stable anonymous label for this recording session."""
        if user_id not in self._speaker_labels:
            self._speaker_labels[user_id] = f"Speaker {len(self._speaker_labels) + 1}"
        return self._speaker_labels[user_id]

    async def add_reference_from_message(self, message: discord.Message):
        """Discordメッセージから参考資料を収集（テキスト+添付ファイル）"""
        added = False

        # テキスト本文
        if message.content and not message.content.startswith("/"):
            self._reference_docs.append(f"[{message.author.display_name}の投稿]\n{message.content}")
            log.info(f"Reference added from text: {len(message.content)} chars")
            added = True

        # 添付ファイル（テキスト系のみ読み取り）
        for attachment in message.attachments:
            if attachment.size > 1_000_000:  # 1MB超はスキップ
                continue
            ext = os.path.splitext(attachment.filename)[1].lower()
            if ext in (".txt", ".md", ".csv", ".json", ".py", ".js", ".html"):
                try:
                    data = await attachment.read()
                    text = data.decode("utf-8", errors="replace")
                    self._reference_docs.append(f"[添付ファイル: {attachment.filename}]\n{text}")
                    log.info(f"Reference added from file: {attachment.filename} ({len(text)} chars)")
                    added = True
                except Exception as e:
                    log.warning(f"Failed to read attachment {attachment.filename}: {e}")

        # 読み取り完了を通知
        if added:
            try:
                await message.add_reaction("📖")
            except Exception:
                pass

    async def start(self):
        self._loop = asyncio.get_running_loop()
        self.recording_sink = PersistentPCMSink(self.guild_id, self.channel.name, self.mode.value)
        self.voice_client = await self.channel.connect()
        self.is_active = True
        log.info(f"VC joined: {self.channel.name} (mode={self.mode.value})")

        # 接続が安定するまで待つ
        await asyncio.sleep(2)

        # 録音開始
        if self.voice_client and self.voice_client.is_connected():
            try:
                self.voice_client.start_recording(
                    self.recording_sink,
                    self._recording_finished,
                    self.guild_id,
                )
                log.info(
                    "Recording started in %s: %s",
                    self.channel.name,
                    self.recording_sink.session_dir,
                )
            except Exception as e:
                log.error(f"Failed to start recording: {e}")

            # 全モードでリアルタイム文字起こしループ開始（listenも段階的蓄積）
            self._realtime_task = asyncio.create_task(self._realtime_loop())
        else:
            log.error("Voice client not connected after join")

    def _recording_finished(self, sink, _guild_id=None):
        """Pycord recording completion callback (runs outside the event loop)."""
        log.info("Recording finished callback called: %s", sink.session_dir)
        if self._loop and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._recording_done.set)
            if self.is_active:
                log.warning("Recording stopped unexpectedly; scheduling restart")
                self._loop.call_soon_threadsafe(lambda: asyncio.create_task(self._restart_recording()))

    async def _restart_recording(self):
        """録音を再開"""
        await asyncio.sleep(1)
        if not self.is_active or not self.voice_client or not self.voice_client.is_connected():
            return
        try:
            if not self.voice_client.is_recording():
                if not self.recording_sink:
                    return
                self.recording_sink = PersistentPCMSink(
                    self.guild_id,
                    self.channel.name,
                    self.mode.value,
                    session_dir=self.recording_sink.session_dir,
                )
                self.voice_client.start_recording(
                    self.recording_sink,
                    self._recording_finished,
                    self.guild_id,
                )
                log.info("Recording restarted after error")
        except Exception as e:
            log.error(f"Failed to restart recording: {e}")

    async def _realtime_loop(self):
        """定期的に音声を取得 → 文字起こし → 応答"""
        if self.mode == VoiceMode.CHAT:
            interval = REALTIME_INTERVAL_CHAT
        elif self.mode == VoiceMode.MEETING:
            interval = REALTIME_INTERVAL_MEETING
        else:
            interval = REALTIME_INTERVAL_LISTEN
        log.info(f"Realtime loop started (mode={self.mode.value}, interval={interval}s)")
        await asyncio.sleep(interval)  # 最初の間隔を待つ

        while self.is_active:
            try:
                await self._process_realtime_chunk()
            except Exception as e:
                log.error(f"Realtime processing error: {e}")
            await asyncio.sleep(interval)

    async def _process_realtime_chunk(self):
        """Read one bounded chunk per speaker from durable PCM storage."""
        if not self.voice_client or not self.voice_client.is_connected():
            return
        if not self.recording_sink:
            return
        try:
            current_audio = await asyncio.to_thread(
                self.recording_sink.read_chunks,
                self._last_audio_len,
                chunk_bytes=TRANSCRIPTION_CHUNK_BYTES,
            )
        except Exception as e:
            log.error(f"Sink read error: {e}")
            return

        if not current_audio:
            return

        # 差分の音声チャンクを収集
        audio_chunks = {}  # user_id -> (name, new_audio_bytes)
        next_offsets = {}
        for user_id, (audio_bytes, next_offset) in current_audio.items():
            if len(audio_bytes) < 1000:
                self._last_audio_len[user_id] = next_offset
                continue
            speaker = self._speaker_label(user_id)
            audio_chunks[user_id] = (speaker, audio_bytes)
            next_offsets[user_id] = next_offset

        if not audio_chunks:
            return

        # listenモード: 文字起こしのみ（従来方式）
        if self.mode == VoiceMode.LISTEN:
            for user_id, (name, new_audio) in audio_chunks.items():
                log.info(f"Transcribing {len(new_audio)} bytes from {name}")
                text = await self._transcribe_audio(new_audio, name)
                if text is None:
                    continue
                self._last_audio_len[user_id] = next_offsets[user_id]
                if text.strip():
                    self.transcript.append(f"[{name}]: {text}")
                    log.info(f"Transcribed: [{name}]: {text[:50]}...")
            return

        # chat/meetingモード: 専用STTで文字起こし後、回答モデルへ渡す
        result = await self._generate_response_from_audio(audio_chunks, next_offsets)
        if result:
            transcript_text, response = result
            # 議事録用に文字起こしを蓄積
            if transcript_text:
                self.transcript.append(transcript_text)
            if response:
                await self.speak(response)
                self._conversation_history.append({"role": "assistant", "text": response})

    async def _generate_response_from_audio(
        self,
        audio_chunks: dict,
        next_offsets: dict[int, int] | None = None,
    ) -> tuple[str, str] | None:
        """話者ごとに専用STTで文字起こしし、そのテキストから応答を生成。"""
        import time

        t0 = time.monotonic()

        try:
            results = await asyncio.gather(
                *(self._transcribe_audio(audio_bytes, name) for name, audio_bytes in audio_chunks.values())
            )
            chunk_texts = []
            for (user_id, (name, _)), text in zip(audio_chunks.items(), results):
                if text is None:
                    continue
                if next_offsets and user_id in next_offsets:
                    self._last_audio_len[user_id] = next_offsets[user_id]
                if text.strip():
                    chunk_texts.append({"speaker": name, "text": text})
            if not chunk_texts:
                return None

            response_text = await self._generate_realtime_response(chunk_texts)
            transcript_text = "\n".join(f"[{item['speaker']}]: {item['text']}" for item in chunk_texts)
            t1 = time.monotonic()
            log.info("Transcription and response generated in %.1fs", t1 - t0)
            return transcript_text, response_text or ""

        except Exception as e:
            log.error(f"Audio response error: {e}")
            return None

    async def _generate_realtime_response(self, chunk_texts: list[dict]) -> str | None:
        """文字起こし結果から応答が必要か判定し、必要なら応答を生成（フォールバック用）"""
        client = genai.Client(api_key=os.environ.get("GOOGLE_API_KEY", ""))

        # 会話履歴を含めたコンテキスト
        history = ""
        if self._conversation_history:
            recent = self._conversation_history[-10:]  # 直近10件
            history = "\n".join(
                f"[{'やがぽん' if h['role'] == 'assistant' else h.get('speaker', '?')}]: {h['text']}" for h in recent
            )

        current = "\n".join(f"[{t['speaker']}]: {t['text']}" for t in chunk_texts)

        # 会話履歴に追加
        for t in chunk_texts:
            self._conversation_history.append({"role": "user", "speaker": t["speaker"], "text": t["text"]})

        # メンバー情報と語録を取得
        members_info = self.bot._build_members_info(self.guild_id) if hasattr(self.bot, "_build_members_info") else ""
        glossary_text = self.bot.config.get_glossary_text(self.guild_id)

        if self.mode == VoiceMode.CHAT:
            prompt = (
                "あなたは慶應義塾大学 矢上祭実行委員会のマスコット「やがぽん」です。\n"
                "ボイスチャンネルで友達とおしゃべりしています。\n\n"
                "【行動ルール】\n"
                "- 誰かが話したら必ず返事をする。「SKIP」は絶対に使わない\n"
                "- 質問されたら、参考資料やナレッジベースを元に具体的に回答する\n"
                "- 雑談には楽しくノリよく返す。相槌・感想・質問返しなど自然に\n"
                "- 返答は短く自然に（1-3文）。語尾に「ぽん」をつける\n"
                "- 明るく元気なキャラクターで、会話を盛り上げる\n"
                "- 絶対に文字起こしの内容をそのまま繰り返さない。自分の言葉で返答する\n"
                "- 相手の発言を要約したりオウム返しするのではなく、内容に対するリアクションや回答を返す\n\n"
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

        # 参考資料（最優先で参照）
        if self._reference_docs:
            ref_text = "\n---\n".join(self._reference_docs)
            prompt += (
                "【★参考資料（最優先）】\n"
                "以下の資料が提供されています。質問にはまずこの資料の内容を元に回答してください。\n"
                "資料にない情報はナレッジベースを検索してください。\n\n"
                f"{ref_text}\n\n"
            )

        if members_info:
            prompt += f"【メンバー情報】\n{members_info}\n\n"
        if glossary_text:
            prompt += f"【用語辞書】\n{glossary_text}\n\n"
        if history:
            prompt += f"=== これまでの会話 ===\n{history}\n\n"
        prompt += f"=== 今の発言 ===\n{current}"

        try:
            import time

            t0 = time.monotonic()

            model = response_model()
            config_kwargs = {
                "thinking_level": "low",
                "max_output_tokens": 256,
            }

            # 参考資料がある場合はRAGなしで十分（資料がプロンプトに含まれている）
            corpus = self.bot.config.get_corpus(self.guild_id)
            if corpus and not self._reference_docs:
                response = await asyncio.wait_for(
                    client.aio.models.generate_content(
                        model=model,
                        contents=prompt,
                        config=generation_config(
                            model,
                            **config_kwargs,
                            tools=[types.Tool(file_search=types.FileSearch(file_search_store_names=[corpus]))],
                        ),
                    ),
                    timeout=90,
                )
            else:
                response = await asyncio.wait_for(
                    client.aio.models.generate_content(
                        model=model,
                        contents=prompt,
                        config=generation_config(model, **config_kwargs),
                    ),
                    timeout=90,
                )

            log_usage(log, "voice_response", model, response)
            t1 = time.monotonic()
            text = (response.text or "").strip()
            log.info(f"Response generated in {t1 - t0:.1f}s: {text[:60]}...")
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
        async with self._stop_lock:
            if self._stop_completed:
                if self._stop_error:
                    raise self._stop_error
                return self._stop_result
            try:
                self._stop_result = await self._stop_once()
                return self._stop_result
            except BaseException as exc:
                self._stop_error = exc
                raise
            finally:
                self._stop_completed = True

    def claim_delivery(self) -> bool:
        """Ensure concurrent manual/automatic leave paths publish only once."""
        if self._delivery_claimed:
            return False
        self._delivery_claimed = True
        return True

    def mark_delivered(self, *, drive_url: str | None = None) -> None:
        if self.recording_sink:
            if self._transcription_failed or self.recording_sink.limit_reached:
                self.recording_sink.mark(
                    "delivered_recording_retained",
                    drive_url=drive_url or "",
                    transcription_failed=self._transcription_failed,
                    storage_limit_reached=self.recording_sink.limit_reached,
                )
            else:
                self.recording_sink.mark("delivered", drive_url=drive_url or "")

    async def _stop_once(self) -> str | None:
        self.is_active = False

        # リアルタイム処理を停止
        if self._realtime_task:
            self._realtime_task.cancel()
            try:
                await self._realtime_task
            except asyncio.CancelledError:
                pass

        if self.voice_client:
            try:
                if self.voice_client.is_recording():
                    self.voice_client.stop_recording()
            except Exception as e:
                log.warning(f"Stop recording error: {e}")
            try:
                await asyncio.wait_for(self.voice_client.disconnect(force=True), timeout=15)
            except Exception as e:
                log.warning("Voice disconnect error: %s", e)

        if self.recording_sink:
            await self._drain_recording()

        if self.mode in (VoiceMode.LISTEN, VoiceMode.MEETING):
            minutes = await self._generate_minutes()
            if self.recording_sink and not self._transcription_failed:
                self.recording_sink.mark("minutes_generated")
            return minutes
        if self.recording_sink:
            self.recording_sink.mark("completed_without_minutes")
        return None

    async def _drain_recording(self) -> None:
        """Transcribe every durable chunk not yet processed; stop after a failed chunk."""
        if not self.recording_sink:
            return
        while True:
            chunks = await asyncio.to_thread(
                self.recording_sink.read_chunks,
                self._last_audio_len,
                chunk_bytes=TRANSCRIPTION_CHUNK_BYTES,
            )
            if not chunks:
                return
            progressed = False
            for user_id, (audio_bytes, next_offset) in chunks.items():
                speaker = self._speaker_label(user_id)
                text = await self._transcribe_audio(audio_bytes, speaker)
                if text is None:
                    continue
                self._last_audio_len[user_id] = next_offset
                progressed = True
                if text.strip():
                    self.transcript.append(f"[{speaker}]: {text}")
            if not progressed:
                self._transcription_failed = True
                self.recording_sink.mark("transcription_failed")
                return

    @staticmethod
    def _pcm_to_wav(pcm_data: bytes, channels: int = 2, sample_rate: int = 48000, sample_width: int = 2) -> bytes:
        """生のPCMデータにWAVヘッダーを付与"""
        import wave

        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(channels)
            wf.setsampwidth(sample_width)
            wf.setframerate(sample_rate)
            wf.writeframes(pcm_data)
        return buf.getvalue()

    async def _transcribe_audio(self, audio_bytes: bytes, speaker_name: str) -> str | None:
        """Gemini Audio APIで音声を文字起こし"""
        if len(audio_bytes) < 1000:  # ほぼ無音
            return ""

        now = time.monotonic()
        if now < self._transcription_backoff_until:
            log.info(
                "Skipping transcription during rate-limit cooldown speaker=%s remaining=%.1fs",
                speaker_name,
                self._transcription_backoff_until - now,
            )
            return None

        # WAVヘッダーがなければ付与（sinkからの生PCMデータ対応）
        if not audio_bytes[:4] == b"RIFF":
            audio_bytes = self._pcm_to_wav(audio_bytes)

        client = new_genai_client(os.environ.get("GOOGLE_API_KEY", ""))

        try:
            vocabulary = []
            if self.bot and hasattr(self.bot, "config"):
                vocabulary = list(self.bot.config.get_glossary(self.guild_id).keys())

            text = await asyncio.wait_for(
                transcribe_audio(
                    client,
                    audio_bytes,
                    mime_type="audio/wav",
                    display_name=f"vc-{speaker_name}",
                    custom_vocabulary=vocabulary,
                ),
                timeout=TRANSCRIPTION_TIMEOUT_SECONDS,
            )
            self._transcription_rate_limit_failures = 0
            self._transcription_backoff_until = 0.0
            return text
        except RateLimitExceeded:
            self._transcription_rate_limit_failures += 1
            cooldown = min(1800.0, 300.0 * (2 ** (self._transcription_rate_limit_failures - 1)))
            self._transcription_backoff_until = time.monotonic() + cooldown
            log.warning(
                "Transcription rate limit exhausted speaker=%s cooldown=%.0fs failures=%s",
                speaker_name,
                cooldown,
                self._transcription_rate_limit_failures,
            )
            return None
        except Exception as e:
            log.error("Transcription error for %s: %r", speaker_name, e)
            return None

    async def _generate_minutes(self) -> str:
        """蓄積済みの文字起こしから議事録を生成（長時間でも対応可能）"""
        # リアルタイムループで段階的に蓄積されたtranscriptを使用
        all_content = list(self.transcript) if self.transcript else []

        if not all_content:
            return "文字起こしを生成できませんでした。復旧用の録音データはサーバーに保管されています。"

        transcript_text = "\n".join(all_content)

        # 語録を含める
        glossary_text = ""
        if self.bot and hasattr(self.bot, "config"):
            glossary_text = self.bot.config.get_glossary_text(self.guild_id)

        client = new_genai_client(os.environ.get("GOOGLE_API_KEY", ""))
        return await generate_minutes(
            client,
            transcript_text,
            glossary_text=glossary_text,
        )

    async def speak(self, text: str):
        """TTSで発話"""
        if not self.voice_client or not self.voice_client.is_connected():
            return

        from bot.tts import PITCH, RATE, VOICE

        try:
            import time

            import edge_tts

            t0 = time.monotonic()
            communicate = edge_tts.Communicate(text[:500], VOICE, pitch=PITCH, rate=RATE)
            tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
            tmp_path = tmp.name
            tmp.close()
            await communicate.save(tmp_path)
            t1 = time.monotonic()
            log.info(f"TTS generated in {t1 - t0:.1f}s")

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
        await leave_voice(guild_id)

    session = VoiceSession(bot, guild_id, channel, mode)
    await session.start()
    _sessions[guild_id] = session
    return session


async def leave_voice(guild_id: int) -> str | None:
    session = _sessions.get(guild_id)
    if session:
        if session._stop_task is None:
            session._stop_task = asyncio.create_task(session.stop())

            def release_session(task: asyncio.Task) -> None:
                # Retrieve an exception even when the caller itself was cancelled.
                if not task.cancelled():
                    task.exception()
                if _sessions.get(guild_id) is session:
                    _sessions.pop(guild_id, None)
                session._stop_task = None

            session._stop_task.add_done_callback(release_session)
        return await asyncio.shield(session._stop_task)
    return None


def get_session(guild_id: int) -> VoiceSession | None:
    return _sessions.get(guild_id)
