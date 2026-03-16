"""コーパス管理 - バッチ学習バッファ、flush、RAGクエリ、コーパスCRUD"""

import asyncio
import io
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from google import genai
from google.genai import types

log = logging.getLogger("yagapon.corpus")

# バッチ設定
FLUSH_MESSAGE_THRESHOLD = 100  # メッセージ数でflush
FLUSH_TIME_SECONDS = 7200      # 2時間でflush
FLUSH_CHECK_INTERVAL = 120     # 2分ごとにチェック

# レート制限
DAILY_QUERY_LIMIT = 400  # 1ギルドあたり1日の質問上限

SYSTEM_INSTRUCTION = (
    "あなたは慶應義塾大学 矢上祭実行委員会の専属AI「おしゃべりやがぽん」だぽん。\n"
    "ユーザーの質問に対し、ナレッジベース（コード、議事録、Discordログ）を検索し、"
    "事実に基づいて回答するぽん。\n"
    "- ナレッジに含まれていない情報は、絶対に回答に含めてはいけないぽん。\n"
    "- もしナレッジに関連情報がない場合は「その件に関する情報は、"
    "今のボクの記憶には見当たらないぽん...🙏」と正直に答えるぽん。\n"
    "- 語尾には「ぽん」をつけるぽん。\n"
    "- 回答は簡潔にするぽん。"
)


@dataclass
class MessageBuffer:
    channel_name: str
    guild_id: int
    channel_id: int
    first_message_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    messages: list[str] = field(default_factory=list)


class CorpusManager:
    def __init__(self):
        api_key = os.environ.get("GOOGLE_API_KEY", "")
        self._client = genai.Client(api_key=api_key)
        self._buffers: dict[tuple[int, int], MessageBuffer] = {}
        self._upload_semaphore = asyncio.Semaphore(5)
        self._flush_task: asyncio.Task | None = None
        # レート制限: {guild_id: {"date": "2026-03-16", "count": 42}}
        self._query_counts: dict[int, dict] = {}

    # ------ lifecycle ------

    def start_flush_loop(self):
        if self._flush_task is None:
            self._flush_task = asyncio.create_task(self._flush_loop())

    async def shutdown(self):
        if self._flush_task:
            self._flush_task.cancel()
        await self.flush_all()

    async def _flush_loop(self):
        while True:
            await asyncio.sleep(FLUSH_CHECK_INTERVAL)
            try:
                await self._check_time_flushes()
            except Exception as e:
                log.error(f"Flush loop error: {e}")

    async def _check_time_flushes(self):
        now = datetime.now(timezone.utc)
        keys_to_flush = [
            key for key, buf in self._buffers.items()
            if buf.messages and (now - buf.first_message_at).total_seconds() >= FLUSH_TIME_SECONDS
        ]
        for key in keys_to_flush:
            await self._flush_buffer(key)

    # ------ corpus CRUD ------

    async def create_corpus(self, guild_id: int, bureau_name: str) -> str:
        display_name = f"yagapon-{bureau_name}-{guild_id}"
        loop = asyncio.get_event_loop()
        store = await loop.run_in_executor(
            None,
            lambda: self._client.file_search_stores.create(
                config={"display_name": display_name}
            ),
        )
        log.info(f"Created corpus: {store.name} ({display_name})")
        return store.name

    async def delete_corpus(self, store_name: str):
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            lambda: self._client.file_search_stores.delete(
                name=store_name,
            ),
        )
        log.info(f"Deleted corpus: {store_name}")

    # ------ batch learning ------

    def add_message(self, guild_id: int, channel_id: int, channel_name: str,
                    author: str, content: str, timestamp: datetime,
                    corpus_store_name: str):
        """メッセージをバッファに追加。閾値超えたらflushをスケジュール。"""
        key = (guild_id, channel_id)
        if key not in self._buffers:
            self._buffers[key] = MessageBuffer(
                channel_name=channel_name,
                guild_id=guild_id,
                channel_id=channel_id,
            )

        buf = self._buffers[key]
        ts = timestamp.strftime("%H:%M")
        buf.messages.append(f"[{ts}] [{author}]: {content}")

        # メッセージ数閾値
        if len(buf.messages) >= FLUSH_MESSAGE_THRESHOLD:
            asyncio.create_task(self._flush_buffer(key, corpus_store_name))

    async def _flush_buffer(self, key: tuple[int, int], corpus_store_name: str | None = None):
        buf = self._buffers.pop(key, None)
        if not buf or not buf.messages:
            return

        text = "\n".join(buf.messages)
        start = buf.first_message_at.strftime("%Y-%m-%d %H:%M")
        end = datetime.now(timezone.utc).strftime("%H:%M")
        display_name = f"#{buf.channel_name} | {start}-{end}"

        if corpus_store_name:
            await self._upload_document(corpus_store_name, display_name, text)

    async def flush_all(self, corpus_store_name_lookup=None):
        """全バッファをflush。corpus_store_name_lookup: guild_id -> store_name"""
        keys = list(self._buffers.keys())
        for key in keys:
            guild_id = key[0]
            store_name = corpus_store_name_lookup(guild_id) if corpus_store_name_lookup else None
            await self._flush_buffer(key, store_name)

    async def flush_guild_channel(self, guild_id: int, channel_id: int, corpus_store_name: str):
        key = (guild_id, channel_id)
        await self._flush_buffer(key, corpus_store_name)

    # ------ upload ------

    async def _upload_document(self, store_name: str, display_name: str, text: str):
        async with self._upload_semaphore:
            try:
                loop = asyncio.get_event_loop()
                file_bytes = text.encode("utf-8")

                await loop.run_in_executor(
                    None,
                    lambda: self._client.file_search_stores.upload_to_file_search_store(
                        file=io.BytesIO(file_bytes),
                        file_search_store_name=store_name,
                        config={
                            "display_name": display_name,
                            "mime_type": "text/plain",
                        },
                    ),
                )
                log.info(f"Uploaded: {display_name} -> {store_name}")
            except Exception as e:
                log.error(f"Upload error ({display_name}): {e}")

    # ------ rate limit ------

    def _check_rate_limit(self, guild_id: int) -> bool:
        """True = OK, False = 上限到達"""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        entry = self._query_counts.get(guild_id)
        if not entry or entry["date"] != today:
            self._query_counts[guild_id] = {"date": today, "count": 0}
            entry = self._query_counts[guild_id]
        if entry["count"] >= DAILY_QUERY_LIMIT:
            return False
        entry["count"] += 1
        return True

    def get_remaining_queries(self, guild_id: int) -> int:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        entry = self._query_counts.get(guild_id)
        if not entry or entry["date"] != today:
            return DAILY_QUERY_LIMIT
        return max(0, DAILY_QUERY_LIMIT - entry["count"])

    # ------ RAG query ------

    async def query(self, question: str, corpus_store_name: str, guild_id: int = 0) -> str:
        if guild_id and not self._check_rate_limit(guild_id):
            return (
                f"今日の質問上限（{DAILY_QUERY_LIMIT}回）に達しちゃったぽん...\n"
                "明日またたくさん聞いてねぽん！🙏"
            )
        try:
            response = await self._client.aio.models.generate_content(
                model="gemini-2.5-flash",
                contents=question,
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_INSTRUCTION,
                    tools=[
                        types.Tool(
                            file_search=types.FileSearch(
                                file_search_store_names=[corpus_store_name]
                            )
                        )
                    ],
                ),
            )
            return response.text or "回答を生成できなかったぽん..."
        except Exception as e:
            log.error(f"RAG query error: {e}")
            return f"エラーが出ちゃったぽん...: {e}"

    # ------ backfill ------

    async def backfill_channel(self, channel, corpus_store_name: str,
                               after=None, progress_callback=None) -> int:
        """チャンネルの履歴を取得してバッチアップロード。after=Noneで全量。件数を返す。"""
        count = 0
        hour_bucket: dict[str, list[str]] = {}  # "YYYY-MM-DD HH:00" -> lines

        async for message in channel.history(limit=None, after=after, oldest_first=True):
            if message.author.bot or len(message.content) < 4:
                continue
            if message.content.startswith("/"):
                continue

            ts = message.created_at
            bucket_key = ts.strftime("%Y-%m-%d %H:00")
            line = f"[{ts.strftime('%H:%M')}] [{message.author.display_name}]: {message.content}"

            if bucket_key not in hour_bucket:
                hour_bucket[bucket_key] = []
            hour_bucket[bucket_key].append(line)
            count += 1

            if progress_callback and count % 500 == 0:
                await progress_callback(count)

        # バケットごとにアップロード
        for bucket_key, lines in hour_bucket.items():
            if not lines:
                continue
            display_name = f"#{channel.name} | {bucket_key}"
            text = "\n".join(lines)
            await self._upload_document(corpus_store_name, display_name, text)

        return count
