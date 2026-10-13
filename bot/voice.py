"""VC 3モード - listen / meeting / chat + 音声録音・文字起こし・リアルタイム応答"""

import asyncio
import io
import logging
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from types import SimpleNamespace

import discord
from google import genai
from google.genai import types

from bot.ai_models import generation_config, log_usage, response_model
from bot.ai_retry import RateLimitCircuitBreaker, RateLimitExceeded, new_genai_client
from bot.meeting_time import display_meeting_date
from bot.minutes_generation import generate_minutes
from bot.recording import PersistentPCMSink, find_recoverable_recordings, load_transcript_content
from bot.transcription import transcribe_audio

log = logging.getLogger("yagapon.voice")

# リアルタイム処理の間隔（秒）
REALTIME_INTERVAL_CHAT = 5  # chatモード: 短い間隔で即応答
REALTIME_INTERVAL_MEETING = 15  # meetingモード: 会議の邪魔にならないよう長め
REALTIME_INTERVAL_LISTEN = 300  # listenモード: 専用STTの日次リクエスト枠を温存
TRANSCRIPTION_CHUNK_BYTES_REALTIME = 5 * 1024 * 1024
TRANSCRIPTION_CHUNK_BYTES_LISTEN = 50 * 1024 * 1024
FINAL_REPROCESS_CHUNK_BYTES = 50 * 1024 * 1024
TRANSCRIPTION_TIMEOUT_SECONDS = 180
_transcription_rate_limiter = RateLimitCircuitBreaker()


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
        self.started_at = datetime.now(timezone.utc)
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
        self._transcript_path: Path | None = None
        self._knowledge_pull_request_url: str | None = None
        self._knowledge_outbox_path: Path | None = None
        self._speaker_labels: dict[int, str] = {}
        self._diarized_speaker_labels: dict[str, str] = {}
        self._diarization_chunk_sequence = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._conversation_history: list[dict] = []  # chat/meetingの会話履歴
        self._last_audio_len: dict[int, int] = {}  # user_id -> 文字起こし済みPCMバイト数
        self._reference_docs: list[str] = []  # VCテキストチャットに投稿された参考資料
        self._voiceprint_paths: dict[str, str] | None = None

    def _registered_voiceprints(self) -> dict[str, str]:
        """Load enrolled samples once per session for real speaker attribution."""
        if self._voiceprint_paths is None:
            try:
                from bot.commands.voiceprint import get_voiceprints_with_names

                self._voiceprint_paths = get_voiceprints_with_names(self.bot.config, self.guild_id)
            except (AttributeError, OSError):
                self._voiceprint_paths = {}
        return self._voiceprint_paths

    async def add_reference(self, content: str, source: str = "テキスト"):
        """VCテキストチャットに投稿された資料を参考資料として追加"""
        self._reference_docs.append(f"[{source}]\n{content}")
        log.info(f"Reference doc added: {source} ({len(content)} chars)")

    def _speaker_label(self, user_id: int) -> str:
        """Return a stable label backed by Discord's per-user audio stream."""
        if user_id not in self._speaker_labels:
            name = ""
            guild = getattr(self.channel, "guild", None)
            if guild is None and hasattr(self.bot, "get_guild"):
                guild = self.bot.get_guild(self.guild_id)
            member = guild.get_member(user_id) if guild else None
            name = str(
                getattr(member, "display_name", "")
                or getattr(member, "name", "")
            ).strip()
            if not name:
                config = getattr(self.bot, "config", None)
                if config and hasattr(config, "get_member"):
                    registered = config.get_member(self.guild_id, user_id) or {}
                    name = str(registered.get("name") or "").strip()
            name = re.sub(r"[\[\]\r\n:：]", " ", name).strip() or f"User {user_id}"
            label = f"Discord確定: {name}"
            if label in self._speaker_labels.values():
                label = f"{label} ({str(user_id)[-4:]})"
            self._speaker_labels[user_id] = label
        return self._speaker_labels[user_id]

    def _next_speaker_label(self) -> str:
        used = set(self._speaker_labels.values()) | set(self._diarized_speaker_labels.values())
        number = 1
        while f"Speaker {number}" in used:
            number += 1
        return f"Speaker {number}"

    def _format_transcribed_text(
        self,
        user_id: int,
        text: str,
        *,
        diarized: bool,
        chunk_id: int | None = None,
    ) -> str:
        """Map chunk-local diarization labels to non-colliding session labels.

        STT providers restart ``Speaker 1`` numbering for every request.  The
        chunk identifier therefore has to be part of the mapping key; reusing
        only the local number would silently merge different people who spoke
        in different five-minute chunks.
        """
        if not diarized:
            return f"[{self._speaker_label(user_id)}]: {text}"
        pattern = re.compile(
            r"(?im)^\s*\[?(?:speaker|spk)[ _-]?(\d+)\]?\s*[:：]\s*"
        )
        matches = list(pattern.finditer(text))
        registered = set(self._registered_voiceprints())
        named_pattern = None
        if registered:
            alternatives = "|".join(re.escape(name) for name in sorted(registered, key=len, reverse=True))
            named_pattern = re.compile(rf"(?im)^\s*\[?({alternatives})\]?\s*[:：]\s*")
        named_matches = list(named_pattern.finditer(text)) if named_pattern else []
        if not matches and not named_matches:
            return f"[{self._speaker_label(user_id)}]: {text}"

        if chunk_id is None:
            self._diarization_chunk_sequence += 1
            chunk_id = self._diarization_chunk_sequence

        def replace(match: re.Match) -> str:
            key = f"{user_id}:{chunk_id}:{int(match.group(1))}"
            if key not in self._diarized_speaker_labels:
                self._diarized_speaker_labels[key] = self._next_speaker_label()
            return f"[{self._diarized_speaker_labels[key]}]: "

        formatted = pattern.sub(replace, text)
        if named_pattern:
            formatted = named_pattern.sub(lambda match: f"[{match.group(1)}]: ", formatted)
        return formatted.strip()

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
                    resume=True,
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
                chunk_bytes=self._transcription_chunk_bytes(),
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

        # listenモード: 文字起こしのみ。受信トラック自体がDiscord
        # user_idで分離済みなので、確定ラベルを維持する。
        if self.mode == VoiceMode.LISTEN:
            for user_id, (name, new_audio) in audio_chunks.items():
                log.info(f"Transcribing {len(new_audio)} bytes from {name}")
                text = await self._transcribe_audio(new_audio, name, diarization=False)
                if text is None:
                    continue
                self._last_audio_len[user_id] = next_offsets[user_id]
                if text.strip():
                    formatted = self._format_transcribed_text(user_id, text, diarized=False)
                    await self._append_transcript(formatted)
                    log.info("Transcribed stream=%s: %s...", user_id, formatted[:50])
                await self._checkpoint_state()
            return

        # chat/meetingモード: 専用STTで文字起こし後、回答モデルへ渡す
        result = await self._generate_response_from_audio(audio_chunks, next_offsets)
        if result:
            transcript_text, response = result
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
                await self._checkpoint_state()
                return None

            transcript_text = "\n".join(f"[{item['speaker']}]: {item['text']}" for item in chunk_texts)
            # Persist transcription before optional response generation. A response
            # model outage must never discard already successful STT work.
            await self._append_transcript(transcript_text)
            await self._checkpoint_state()
            response_text = await self._generate_realtime_response(chunk_texts)
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

    @property
    def transcript_text(self) -> str:
        return "\n".join(self.transcript)

    @property
    def transcript_path(self) -> Path | None:
        return self._transcript_path

    @property
    def knowledge_pull_request_url(self) -> str | None:
        return self._knowledge_pull_request_url

    async def publish_knowledge(
        self,
        *,
        summary: str | None = None,
        minutes: str | None = None,
    ) -> str | None:
        """Queue the transcript and optional derived documents for Knowledge review."""
        if not self.recording_sink or not self.transcript_text.strip():
            return None
        from bot.knowledge_publish import publish_meeting_knowledge

        result = await publish_meeting_knowledge(
            session_id=self.recording_sink.session_dir.name,
            guild_id=self.guild_id,
            channel_name=self.channel.name,
            mode=self.mode.value,
            transcript=self.transcript_text,
            summary=summary,
            minutes=minutes,
        )
        if result:
            self._knowledge_pull_request_url = result.pull_request_url
            self._knowledge_outbox_path = result.outbox_dir
            await self._checkpoint_state(
                knowledge_outbox=str(result.outbox_dir),
                knowledge_pull_request_url=result.pull_request_url or "",
            )
        return self._knowledge_pull_request_url

    async def _append_transcript(self, text: str) -> None:
        self.transcript.append(text)
        await self._persist_transcript()

    async def _persist_transcript(self) -> Path | None:
        if not self.recording_sink or not self.transcript:
            return self._transcript_path
        self._transcript_path = await asyncio.to_thread(
            self.recording_sink.save_transcript,
            self.transcript_text,
        )
        return self._transcript_path

    async def _checkpoint_state(self, *, status: str = "processing", **extra) -> None:
        if not self.recording_sink or not hasattr(self.recording_sink, "checkpoint"):
            return
        await asyncio.to_thread(
            self.recording_sink.checkpoint,
            offsets=self._last_audio_len,
            speaker_labels=self._speaker_labels,
            transcript_path=self._transcript_path,
            status=status,
            diarized_speaker_labels=self._diarized_speaker_labels,
            diarization_chunk_sequence=self._diarization_chunk_sequence,
            **extra,
        )

    def mark_delivered(
        self,
        *,
        drive_url: str | None = None,
        transcript_url: str | None = None,
    ) -> None:
        if self.recording_sink:
            transcript_saved = bool(self._transcript_path and self._transcript_path.exists())
            has_recording = self.recording_sink.total_bytes > 0
            if (
                self._transcription_failed
                or self.recording_sink.limit_reached
                or (has_recording and not transcript_saved)
            ):
                self.recording_sink.mark(
                    "delivered_recording_retained",
                    drive_url=drive_url or "",
                    transcript_url=transcript_url or "",
                    transcript_path=str(self._transcript_path or ""),
                    transcription_failed=self._transcription_failed,
                    storage_limit_reached=self.recording_sink.limit_reached,
                    transcript_saved=transcript_saved,
                )
            else:
                self.recording_sink.mark(
                    "delivered_audio_deleted",
                    drive_url=drive_url or "",
                    transcript_url=transcript_url or "",
                    transcript_path=str(self._transcript_path or ""),
                )
                deleted = self.recording_sink.delete_audio()
                log.info(
                    "Deleted raw recording after transcript preservation session=%s files=%s",
                    self.recording_sink.session_dir.name,
                    deleted,
                )

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
                await self._reprocess_complete_recording()
            await self._persist_transcript()

        if self.mode in (VoiceMode.LISTEN, VoiceMode.MEETING):
            try:
                minutes = await self._generate_minutes()
            except Exception as exc:
                log.exception("Minutes generation failed; preserving transcript for delivery")
                if self.recording_sink:
                    self.recording_sink.mark(
                        "minutes_generation_failed",
                        error=type(exc).__name__,
                        transcript_path=str(self._transcript_path or ""),
                    )
                return None
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
                chunk_bytes=self._transcription_chunk_bytes(),
            )
            if not chunks:
                return
            progressed = False
            for user_id, (audio_bytes, next_offset) in chunks.items():
                # Pycord already gives us one durable track per Discord user.
                # Re-diarizing that track discards this certain identity and can
                # invent a fresh Speaker N on every chunk.
                diarized = False
                speaker = self._speaker_label(user_id)
                text = await self._transcribe_audio(
                    audio_bytes, speaker, diarization=diarized
                )
                if text is None:
                    continue
                self._last_audio_len[user_id] = next_offset
                progressed = True
                if text.strip():
                    await self._append_transcript(
                        self._format_transcribed_text(user_id, text, diarized=diarized)
                    )
                await self._checkpoint_state()
            if not progressed:
                self._transcription_failed = True
                self.recording_sink.mark("transcription_failed")
                return

    async def _reprocess_complete_recording(self) -> bool:
        """Re-transcribe the complete recording after capture has stopped.

        Realtime transcripts are crash-safe drafts.  The final pass starts at
        byte zero, uses larger bounded chunks and the Discord stream identity,
        then atomically replaces the draft only when every chunk succeeds.
        Long recordings therefore remain bounded and a 429 never destroys the
        usable incremental transcript.
        """
        if not self.recording_sink or self.recording_sink.total_bytes <= 0:
            return False

        offsets: dict[int, int] = {}
        final_transcript: list[str] = []
        self.recording_sink.mark("final_reprocess_processing")
        while True:
            chunks = await asyncio.to_thread(
                self.recording_sink.read_chunks,
                offsets,
                chunk_bytes=FINAL_REPROCESS_CHUNK_BYTES,
            )
            if not chunks:
                break
            progressed = False
            for user_id, (audio_bytes, next_offset) in chunks.items():
                speaker = self._speaker_label(user_id)
                text = await self._transcribe_audio(
                    audio_bytes,
                    speaker,
                    diarization=False,
                    prefer_dedicated=True,
                )
                if text is None:
                    self.recording_sink.mark(
                        "final_reprocess_deferred",
                        final_reprocess_offsets={str(key): value for key, value in offsets.items()},
                    )
                    return False
                offsets[user_id] = next_offset
                progressed = True
                if text.strip():
                    final_transcript.append(
                        self._format_transcribed_text(user_id, text, diarized=False)
                    )
            if not progressed:
                self.recording_sink.mark("final_reprocess_deferred")
                return False

        if not final_transcript:
            self.recording_sink.mark("final_reprocess_empty")
            return False
        self.transcript = final_transcript
        self._last_audio_len = offsets
        self._transcription_failed = False
        await self._persist_transcript()
        await self._checkpoint_state(status="final_reprocess_completed")
        return True

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

    async def _transcribe_audio(
        self,
        audio_bytes: bytes,
        speaker_name: str,
        *,
        diarization: bool = False,
        prefer_dedicated: bool | None = None,
    ) -> str | None:
        """Gemini Audio APIで音声を文字起こし"""
        if len(audio_bytes) < 1000:  # ほぼ無音
            return ""

        cooldown_remaining = _transcription_rate_limiter.remaining()
        if cooldown_remaining > 0:
            log.debug(
                "Skipping transcription during rate-limit cooldown speaker=%s remaining=%.1fs",
                speaker_name,
                cooldown_remaining,
            )
            return None

        # WAVヘッダーがなければ付与（sinkからの生PCMデータ対応）
        if not audio_bytes[:4] == b"RIFF":
            audio_bytes = self._pcm_to_wav(audio_bytes)

        client = new_genai_client(os.environ.get("GOOGLE_API_KEY", ""))

        try:
            vocabulary = []
            if self.bot and hasattr(self.bot, "config"):
                get_vocabulary = getattr(self.bot.config, "get_glossary_vocabulary", None)
                if get_vocabulary:
                    vocabulary = get_vocabulary(self.guild_id)
                else:
                    vocabulary = list(self.bot.config.get_glossary(self.guild_id).keys())

            text = await asyncio.wait_for(
                transcribe_audio(
                    client,
                    audio_bytes,
                    mime_type="audio/wav",
                    display_name=f"vc-{speaker_name}",
                    custom_vocabulary=vocabulary,
                    diarization=diarization,
                    # Short interactive modes can create many requests. Reserve
                    # the dedicated STT quota for long-form listen/minutes mode.
                    prefer_dedicated=(
                        self.mode == VoiceMode.LISTEN
                        if prefer_dedicated is None
                        else prefer_dedicated
                    ),
                    voiceprint_paths=(self._registered_voiceprints() if diarization else None),
                ),
                timeout=TRANSCRIPTION_TIMEOUT_SECONDS,
            )
            _transcription_rate_limiter.reset()
            return text
        except RateLimitExceeded as exc:
            cooldown = _transcription_rate_limiter.trip(retry_after=exc.retry_after)
            if self.bot and hasattr(self.bot, "operations"):
                self.bot.operations.record_event(
                    "transcription",
                    "rate_limit",
                    "deferred",
                    guild_id=self.guild_id,
                    item_key=(self.recording_sink.session_dir.name if self.recording_sink else None),
                    details={
                        "cooldown_seconds": round(cooldown),
                        "failure_count": _transcription_rate_limiter.failure_count,
                    },
                )
            log.warning(
                "Transcription rate limit exhausted speaker=%s cooldown=%.0fs failures=%s diagnostic=%s",
                speaker_name,
                cooldown,
                _transcription_rate_limiter.failure_count,
                exc.diagnostic,
            )
            return None
        except Exception as e:
            log.error("Transcription error for %s: %r", speaker_name, e)
            return None

    def _transcription_chunk_bytes(self) -> int:
        if self.mode == VoiceMode.LISTEN:
            return TRANSCRIPTION_CHUNK_BYTES_LISTEN
        return TRANSCRIPTION_CHUNK_BYTES_REALTIME

    async def _generate_minutes(self) -> str:
        """蓄積済みの文字起こしから議事録を生成（長時間でも対応可能）"""
        # リアルタイムループで段階的に蓄積されたtranscriptを使用
        all_content = list(self.transcript) if self.transcript else []

        if not all_content:
            return "文字起こしを生成できませんでした。復旧用の録音データはサーバーに保管されています。"

        transcript_text = "\n".join(all_content)

        # 語録を含める
        glossary_text = ""
        speaker_roster = ""
        if self.bot and hasattr(self.bot, "config"):
            glossary_text = self.bot.config.get_glossary_text(self.guild_id)
        if self.bot and hasattr(self.bot, "_build_members_info"):
            speaker_roster = self.bot._build_members_info(self.guild_id)

        client = new_genai_client(os.environ.get("GOOGLE_API_KEY", ""))
        return await generate_minutes(
            client,
            transcript_text,
            glossary_text=glossary_text,
            speaker_roster=speaker_roster,
            title=self.channel.name,
            meeting_date=display_meeting_date(self.started_at),
            checkpoint_dir=(
                self.recording_sink.session_dir / "minutes-checkpoints"
                if self.recording_sink
                else None
            ),
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


async def checkpoint_voice_sessions_for_shutdown() -> None:
    """Persist active offsets before the process is stopped or replaced."""
    for session in list(_sessions.values()):
        try:
            await session._persist_transcript()
            await session._checkpoint_state(status="shutdown_pending")
        except Exception:
            log.exception("Failed to checkpoint voice session guild=%s", session.guild_id)


async def _recover_recording(bot, candidate) -> None:
    metadata = candidate.metadata
    guild_id = int(metadata["guild_id"])
    try:
        mode = VoiceMode(str(metadata.get("mode") or VoiceMode.LISTEN.value))
    except ValueError:
        mode = VoiceMode.LISTEN
    channel = SimpleNamespace(name=str(metadata.get("channel_name") or "recovered-session"))
    session = VoiceSession(bot, guild_id, channel, mode)
    started_at = metadata.get("started_at")
    if started_at:
        try:
            session.started_at = datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
        except ValueError:
            pass
    session.recording_sink = PersistentPCMSink(
        guild_id, channel.name, mode.value, session_dir=candidate.session_dir, resume=True
    )
    session._last_audio_len = {
        int(key): int(value) for key, value in (metadata.get("offsets") or {}).items()
    }
    session._speaker_labels = {
        int(key): str(value) for key, value in (metadata.get("speaker_labels") or {}).items()
    }
    session._diarized_speaker_labels = {
        str(key): str(value)
        for key, value in (metadata.get("diarized_speaker_labels") or {}).items()
    }
    session._diarization_chunk_sequence = int(
        metadata.get("diarization_chunk_sequence") or 0
    )
    transcript_path_value = str(metadata.get("transcript_path") or "")
    session._transcript_path = Path(transcript_path_value) if transcript_path_value else None
    existing = load_transcript_content(session._transcript_path)
    session.transcript = existing.splitlines() if existing else []

    attempts = int(metadata.get("recovery_attempts") or 0) + 1
    started = datetime.now(timezone.utc)
    session.recording_sink.mark("recovery_processing", recovery_attempts=attempts)
    try:
        await session._drain_recording()
        if mode in (VoiceMode.LISTEN, VoiceMode.MEETING):
            await session._reprocess_complete_recording()
        await session._persist_transcript()
        if session._transcription_failed:
            raise RuntimeError("transcription remains rate limited or unavailable")
        minutes = None
        if mode in (VoiceMode.LISTEN, VoiceMode.MEETING):
            minutes = await session._generate_minutes()
        await session.publish_knowledge(minutes=minutes)
        session.recording_sink.mark(
            "recovered", recovery_attempts=attempts,
            recovered_at=datetime.now(timezone.utc).isoformat(),
            transcript_path=str(session._transcript_path or ""),
            knowledge_outbox=str(session._knowledge_outbox_path or ""),
        )
        session.mark_delivered()
        bot.operations.resolve_dead_letter("voice_recovery", candidate.session_dir.name)
        bot.operations.record_event(
            "voice", "recovery", "success", guild_id=guild_id,
            item_key=candidate.session_dir.name,
            duration_ms=int((datetime.now(timezone.utc) - started).total_seconds() * 1000),
            details={"attempts": attempts},
        )
    except Exception as exc:
        maximum = int(os.environ.get("YAGAPON_RECORDING_RECOVERY_MAX_ATTEMPTS", "8"))
        if attempts >= maximum:
            session.recording_sink.mark(
                "recovery_dead_letter", recovery_attempts=attempts, error=type(exc).__name__
            )
            bot.operations.dead_letter(
                "voice_recovery", candidate.session_dir.name,
                attempts=attempts, error_type=type(exc).__name__,
                details={"guild_id": guild_id, "session_dir": str(candidate.session_dir)},
            )
        else:
            delay = min(6 * 3600, 60 * (2 ** (attempts - 1)))
            retry_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
            session.recording_sink.mark(
                "recovery_retry", recovery_attempts=attempts,
                next_retry_at=retry_at.isoformat(), error=type(exc).__name__,
            )
        bot.operations.record_event(
            "voice", "recovery", "failed", guild_id=guild_id,
            item_key=candidate.session_dir.name,
            details={"attempts": attempts, "error_type": type(exc).__name__},
        )
        log.exception("Recording recovery failed session=%s", candidate.session_dir.name)


async def recover_incomplete_recordings(bot) -> None:
    """Resume interrupted transcription jobs without advancing failed offsets."""
    batch_size = int(os.environ.get("YAGAPON_RECORDING_RECOVERY_BATCH_SIZE", "3"))
    candidates = await asyncio.to_thread(find_recoverable_recordings)
    active_directories = {
        session.recording_sink.session_dir.resolve()
        for session in _sessions.values()
        if session.recording_sink is not None
    }
    due = [
        candidate for candidate in candidates
        if candidate.session_dir.resolve() not in active_directories
    ]
    for candidate in due[: max(1, batch_size)]:
        await _recover_recording(bot, candidate)
        # A single exhausted STT quota applies to every session. Do not burn
        # recovery attempts for the remainder of this batch during cooldown.
        if _transcription_rate_limiter.remaining() > 0:
            break


async def recording_recovery_loop(bot) -> None:
    """Continuously retry due recordings; a 429 does not require a bot restart."""
    interval = max(60, int(os.environ.get("YAGAPON_RECORDING_RECOVERY_INTERVAL_SECONDS", "300")))
    while True:
        try:
            await recover_incomplete_recordings(bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Recording recovery scan failed")
        await asyncio.sleep(interval)
