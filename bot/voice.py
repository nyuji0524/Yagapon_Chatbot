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
REALTIME_INTERVAL_CHAT = 5      # chatモード: 短い間隔で即応答
REALTIME_INTERVAL_MEETING = 15  # meetingモード: 会議の邪魔にならないよう長め
REALTIME_INTERVAL_LISTEN = 30   # listenモード: 文字起こし蓄積用
# 音声データのメモリ上限（バイト）- 超えたら古いデータを破棄
MAX_AUDIO_BYTES_PER_USER = 10 * 1024 * 1024  # 10MB（約1分半のWAV）
MAX_TOTAL_AUDIO_BYTES = 50 * 1024 * 1024  # 50MB合計


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
        self._last_audio_len: dict[int, int] = {}  # user_id -> 前回処理済みの音声バイト数
        self._reference_docs: list[str] = []  # VCテキストチャットに投稿された参考資料

    async def add_reference(self, content: str, source: str = "テキスト"):
        """VCテキストチャットに投稿された資料を参考資料として追加"""
        self._reference_docs.append(f"[{source}]\n{content}")
        log.info(f"Reference doc added: {source} ({len(content)} chars)")

    async def add_reference_from_message(self, message: discord.Message):
        """Discordメッセージから参考資料を収集（テキスト+添付ファイル）"""
        added = False

        # テキスト本文
        if message.content and not message.content.startswith("/"):
            self._reference_docs.append(
                f"[{message.author.display_name}の投稿]\n{message.content}"
            )
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
                    self._reference_docs.append(
                        f"[添付ファイル: {attachment.filename}]\n{text}"
                    )
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

            # 全モードでリアルタイム文字起こしループ開始（listenも段階的蓄積）
            self._realtime_task = asyncio.create_task(self._realtime_loop())
        else:
            log.error("Voice client not connected after join")

    def _recording_finished(self, error):
        """録音完了コールバック（同期）"""
        if error:
            log.warning(f"Recording finished with error: {error}, will restart")
            # エラーで録音が止まった場合、再開をスケジュール
            if self.is_active:
                asyncio.get_event_loop().call_soon_threadsafe(
                    lambda: asyncio.create_task(self._restart_recording())
                )
        else:
            log.info("Recording finished callback called")
        self._recording_done.set()

    async def _restart_recording(self):
        """録音を再開"""
        await asyncio.sleep(1)
        if not self.is_active or not self.voice_client or not self.voice_client.is_connected():
            return
        try:
            if not self.voice_client.is_recording():
                sink = discord.sinks.WaveSink()
                self.voice_client.start_recording(sink, self._recording_finished)
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
        """録音中のsinkから直接データを読み取り → 文字起こし → 応答判定"""
        if not self.voice_client or not self.voice_client.is_connected():
            return

        # 録音中のsinkから直接音声データを読み取る（録音は止めない）
        current_audio = {}
        try:
            if hasattr(self.voice_client, '_reader') and self.voice_client._reader:
                sink = self.voice_client._reader.sink
                audio_data = getattr(sink, 'audio_data', {})
                for user_id, audio in audio_data.items():
                    if hasattr(audio, 'file'):
                        pos = audio.file.tell()  # 現在位置を保存
                        audio.file.seek(0)
                        audio_bytes = audio.file.read()
                        audio.file.seek(pos)  # 元に戻す
                        if len(audio_bytes) > 1000:
                            current_audio[user_id] = audio_bytes
        except Exception as e:
            log.error(f"Sink read error: {e}")
            return

        if not current_audio:
            return

        # 差分の音声チャンクを収集
        audio_chunks = {}  # user_id -> (name, new_audio_bytes)
        for user_id, audio_bytes in current_audio.items():
            prev_len = self._last_audio_len.get(user_id, 0)
            if len(audio_bytes) <= prev_len + 1000:
                continue  # 新しい音声がほぼない
            self._last_audio_len[user_id] = len(audio_bytes)
            new_audio = audio_bytes[prev_len:]
            member = self.channel.guild.get_member(user_id)
            name = member.display_name if member else f"User {user_id}"
            audio_chunks[user_id] = (name, new_audio)

        if not audio_chunks:
            return

        # listenモード: 文字起こしのみ（従来方式）
        if self.mode == VoiceMode.LISTEN:
            for user_id, (name, new_audio) in audio_chunks.items():
                log.info(f"Transcribing {len(new_audio)} bytes from {name}")
                text = await self._transcribe_audio(new_audio, name)
                if text and text.strip():
                    self.transcript.append(f"[{name}]: {text}")
                    log.info(f"Transcribed: [{name}]: {text[:50]}...")
            return

        # chat/meetingモード: 音声を直接Geminiに渡して応答生成（文字起こし不要）
        result = await self._generate_response_from_audio(audio_chunks)
        if result:
            transcript_text, response = result
            # 議事録用に文字起こしを蓄積
            if transcript_text:
                self.transcript.append(transcript_text)
            if response:
                await self.speak(response)
                self._conversation_history.append({"role": "assistant", "text": response})

    async def _generate_response_from_audio(self, audio_chunks: dict) -> tuple[str, str] | None:
        """音声を直接Geminiに渡して、文字起こし+応答を1回のAPI呼び出しで生成"""
        import time
        t0 = time.monotonic()

        client = genai.Client(api_key=os.environ.get("GOOGLE_API_KEY", ""))

        # 会話履歴
        history = ""
        if self._conversation_history:
            recent = self._conversation_history[-10:]
            history = "\n".join(
                f"[{'やがぽん' if h['role'] == 'assistant' else h.get('speaker', '?')}]: {h['text']}"
                for h in recent
            )

        # メンバー情報と語録
        members_info = self.bot._build_members_info(self.guild_id) if hasattr(self.bot, '_build_members_info') else ""
        glossary_text = self.bot.config.get_glossary_text(self.guild_id)

        # 話者情報
        speakers = ", ".join(name for name, _ in audio_chunks.values())

        if self.mode == VoiceMode.CHAT:
            role_prompt = (
                "あなたは慶應義塾大学 矢上祭実行委員会のマスコット「やがぽん」です。\n"
                "ボイスチャンネルで友達とおしゃべりしています。\n\n"
                "【行動ルール】\n"
                "- 誰かが話したら必ず返事をする\n"
                "- 質問されたら参考資料やナレッジベースを元に具体的に回答する\n"
                "- 雑談には楽しくノリよく返す\n"
                "- 返答は短く自然に（1-3文）。語尾に「ぽん」をつける\n"
                "- 明るく元気なキャラクターで会話を盛り上げる\n"
                "- 相手の発言をオウム返しせず、内容に対するリアクションや回答を返す\n\n"
            )
        else:
            role_prompt = (
                "あなたは会議に参加している「やがぽん」です。\n\n"
                "【行動ルール】\n"
                "- 名前（やがぽん）を呼ばれたら必ず返答する\n"
                "- 質問されたら回答する。重要な補足ができるときは発言する\n"
                "- それ以外は応答を空にする\n"
                "- 返答は簡潔に。語尾に「ぽん」をつける\n\n"
            )

        # 参考資料
        ref_section = ""
        if self._reference_docs:
            ref_text = "\n---\n".join(self._reference_docs)
            ref_section = (
                "【★参考資料（最優先）】\n"
                "質問にはまずこの資料の内容を元に回答してください。\n\n"
                f"{ref_text}\n\n"
            )

        prompt = (
            f"{role_prompt}"
            f"{ref_section}"
            f"{'【メンバー情報】' + chr(10) + members_info + chr(10)*2 if members_info else ''}"
            f"{'【用語辞書】' + chr(10) + glossary_text + chr(10)*2 if glossary_text else ''}"
            f"{'=== これまでの会話 ===' + chr(10) + history + chr(10)*2 if history else ''}"
            f"添付の音声は {speakers} の発言です。\n\n"
            "【出力形式】以下の形式で出力してください。\n"
            "TRANSCRIPT: （音声の文字起こし。話者名を含めて）\n"
            "RESPONSE: （あなたの返答。不要なら空）"
        )

        try:
            # 音声ファイルをアップロード
            loop = asyncio.get_event_loop()
            audio_parts = []

            for user_id, (name, audio_bytes) in audio_chunks.items():
                if not audio_bytes[:4] == b'RIFF':
                    audio_bytes = self._pcm_to_wav(audio_bytes)

                uploaded = await loop.run_in_executor(
                    None,
                    lambda ab=audio_bytes, n=name: client.files.upload(
                        file=io.BytesIO(ab),
                        config={"mime_type": "audio/wav", "display_name": f"vc-{n}"},
                    ),
                )
                audio_parts.append((uploaded, name))

            # 音声+プロンプトを一括でGeminiに送信
            contents = []
            for uploaded, name in audio_parts:
                contents.append(types.Part.from_uri(file_uri=uploaded.uri, mime_type="audio/wav"))
            contents.append(prompt)

            response = await client.aio.models.generate_content(
                model="gemini-2.5-flash",
                contents=contents,
            )

            t1 = time.monotonic()
            raw = (response.text or "").strip()
            log.info(f"Audio response generated in {t1-t0:.1f}s: {raw[:80]}...")

            # アップロードしたファイルを削除（バックグラウンド）
            for uploaded, _ in audio_parts:
                try:
                    await loop.run_in_executor(None, lambda u=uploaded: client.files.delete(name=u.name))
                except Exception:
                    pass

            # パース: TRANSCRIPT: ... RESPONSE: ...
            transcript_text = ""
            response_text = ""

            if "TRANSCRIPT:" in raw and "RESPONSE:" in raw:
                parts = raw.split("RESPONSE:")
                transcript_part = parts[0]
                response_text = parts[1].strip() if len(parts) > 1 else ""
                transcript_text = transcript_part.replace("TRANSCRIPT:", "").strip()
            else:
                # パース失敗時はすべてを応答として扱う
                response_text = raw

            # 会話履歴に追加
            if transcript_text:
                self._conversation_history.append({"role": "user", "speaker": speakers, "text": transcript_text})

            # chatモードではSKIPしない
            if self.mode == VoiceMode.CHAT:
                if not response_text or response_text.upper() == "SKIP":
                    import random
                    fallbacks = [
                        "うんうん、なるほどぽん！", "へぇ〜、そうなんだぽん！",
                        "おもしろいぽん！", "わかるわかるぽん〜！",
                        "それいいねぽん！", "すごいぽん！",
                        "たしかに〜ぽん！", "えー！まじぽん？",
                    ]
                    response_text = random.choice(fallbacks)
            else:
                if not response_text or response_text.upper() == "SKIP":
                    return (transcript_text, "") if transcript_text else None

            return (transcript_text, response_text)

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

            model = "gemini-2.5-flash"

            # 参考資料がある場合はRAGなしで十分（資料がプロンプトに含まれている）
            corpus = self.bot.config.get_corpus(self.guild_id)
            if corpus and not self._reference_docs:
                response = await client.aio.models.generate_content(
                    model=model,
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
                    model=model,
                    contents=prompt,
                )

            t1 = time.monotonic()
            text = (response.text or "").strip()
            log.info(f"Response generated in {t1-t0:.1f}s: {text[:60]}...")
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
        """sinkからオーディオデータを読み取る（メモリ上限付き）"""
        try:
            audio_data = getattr(sink, 'audio_data', {})
            log.info(f"Sink has {len(audio_data)} user(s) audio data")
            total = sum(len(v) for v in self.full_audio.values())

            for user_id, audio in audio_data.items():
                if hasattr(audio, 'file'):
                    audio.file.seek(0)
                    audio_bytes = audio.file.read()
                elif isinstance(audio, bytes):
                    audio_bytes = audio
                else:
                    audio_bytes = bytes(audio)

                # メモリ上限チェック
                if total + len(audio_bytes) > MAX_TOTAL_AUDIO_BYTES:
                    log.warning(f"Total audio limit reached ({total} bytes), truncating")
                    # 古いデータの先頭を切り詰め
                    audio_bytes = audio_bytes[-(MAX_AUDIO_BYTES_PER_USER):]

                log.info(f"User {user_id}: {len(audio_bytes)} bytes of audio")
                if user_id not in self.full_audio:
                    self.full_audio[user_id] = bytearray()
                self.full_audio[user_id].extend(audio_bytes)

                # ユーザーごとの上限
                if len(self.full_audio[user_id]) > MAX_AUDIO_BYTES_PER_USER:
                    self.full_audio[user_id] = self.full_audio[user_id][-MAX_AUDIO_BYTES_PER_USER:]

                total = sum(len(v) for v in self.full_audio.values())

            log.info(f"Total audio: {total} bytes from {len(self.full_audio)} users")
        except Exception as e:
            log.error(f"Sink read error: {e}")

    @staticmethod
    def _pcm_to_wav(pcm_data: bytes, channels: int = 2, sample_rate: int = 48000, sample_width: int = 2) -> bytes:
        """生のPCMデータにWAVヘッダーを付与"""
        import wave
        buf = io.BytesIO()
        with wave.open(buf, 'wb') as wf:
            wf.setnchannels(channels)
            wf.setsampwidth(sample_width)
            wf.setframerate(sample_rate)
            wf.writeframes(pcm_data)
        return buf.getvalue()

    async def _transcribe_audio(self, audio_bytes: bytes, speaker_name: str) -> str:
        """Gemini Audio APIで音声を文字起こし"""
        if len(audio_bytes) < 1000:  # ほぼ無音
            return ""

        # WAVヘッダーがなければ付与（sinkからの生PCMデータ対応）
        if not audio_bytes[:4] == b'RIFF':
            audio_bytes = self._pcm_to_wav(audio_bytes)

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
        """蓄積済みの文字起こしから議事録を生成（長時間でも対応可能）"""
        # リアルタイムループで段階的に蓄積されたtranscriptを使用
        all_content = list(self.transcript) if self.transcript else []

        # transcriptが空なら未処理の音声をまとめて文字起こし（フォールバック）
        if not all_content and self.full_audio:
            for user_id, audio_bytes in self.full_audio.items():
                if len(audio_bytes) < 1000:
                    continue
                member = self.channel.guild.get_member(user_id)
                name = member.display_name if member else f"User {user_id}"
                # 長時間の場合はチャンクに分割して文字起こし
                chunk_size = 5 * 1024 * 1024  # 5MBずつ
                for i in range(0, len(audio_bytes), chunk_size):
                    chunk = bytes(audio_bytes[i:i + chunk_size])
                    text = await self._transcribe_audio(chunk, name)
                    if text:
                        all_content.append(f"[{name}]: {text}")

        if not all_content:
            return "会議の内容が記録されていないぽん..."

        transcript_text = "\n".join(all_content)

        # 語録を含める
        glossary_text = ""
        if self.bot and hasattr(self.bot, 'config'):
            glossary_text = self.bot.config.get_glossary_text(self.guild_id)

        glossary_hint = ""
        if glossary_text:
            glossary_hint = f"\n\n【用語辞書】（正しい表記に修正して使用してください）\n{glossary_text}"

        client = genai.Client(api_key=os.environ.get("GOOGLE_API_KEY", ""))
        response = await client.aio.models.generate_content(
            model="gemini-2.5-flash",
            contents=(
                "以下の会議の記録から、構造化された議事録を作成してください。\n"
                "形式:\n"
                "## 議事録\n"
                "- **日時**: \n"
                "- **参加者**: \n"
                "### 議題\n"
                "### 議論内容\n"
                "### 決定事項\n"
                "### アクションアイテム\n"
                "### 名言・印象的な発言\n\n"
                "話者名は記録通りに使用してください。"
                f"{glossary_hint}\n\n"
                f"=== 会議記録 ===\n{transcript_text}"
            ),
        )
        return response.text

    async def speak(self, text: str):
        """TTSで発話"""
        if not self.voice_client or not self.voice_client.is_connected():
            return

        from bot.tts import VOICE, PITCH, RATE
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
            log.info(f"TTS generated in {t1-t0:.1f}s")

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
