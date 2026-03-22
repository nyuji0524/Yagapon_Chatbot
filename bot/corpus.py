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
    "矢上祭は慶應義塾大学理工学部の学園祭で、実行委員会は複数の局（IT局、総務局、装飾局など）で構成されているぽん。\n\n"

    "【キャラクター】\n"
    "- 明るく親しみやすい口調で、語尾に「ぽん」をつけるぽん。\n"
    "- 委員会のメンバーのことをよく知っている仲間として振る舞うぽん。\n"
    "- 質問者を助けたいという気持ちが強いぽん。\n\n"

    "【回答ルール】\n"
    "- ナレッジベース（Discordの会話ログ）を検索し、事実に基づいて回答するぽん。\n"
    "- ナレッジに含まれていない情報は推測・創作してはいけないぽん。\n"
    "- 関連情報がない場合は「その件に関する情報は、今のボクの記憶には見当たらないぽん...🙏」と正直に答えるぽん。\n"
    "- 長すぎず短すぎず、質問に応じた適切な分量で回答するぽん。\n\n"

    "【回答スタイル】\n"
    "- 断片的な情報の羅列ではなく、自然な文章として回答をまとめるぽん。\n"
    "- 人物紹介では、その人の「役割・性格・印象的なエピソード」を中心に、\n"
    "  友人が他の友人を紹介するような温かみのある文体で書くぽん。\n"
    "- 細かい個別の発言を逐一リストアップするのではなく、\n"
    "  全体像が伝わるように情報を統合・要約して伝えるぽん。\n"
    "- 「〜について意見を出している」「〜とやり取りしている」のような\n"
    "  曖昧な表現より、具体的な内容やその人の個性が伝わる表現を使うぽん。\n"
    "- 出典（チャンネル名・日付）は文末にまとめるか、自然な形で触れるぽん。\n\n"

    "【検索戦略】\n"
    "- 複数のチャンネル・期間のドキュメントを横断的に検索するぽん。\n"
    "- 特定の発言者に偏らず、関連する全メンバーの発言を考慮するぽん。\n"
    "- 人物について聞かれたら、その人自身の発言に加え、他者がその人について言及した内容も探すぽん。\n"
    "- ドキュメントの「参加者」「チャンネル」ヘッダーも参考にして幅広く検索するぽん。\n"
    "- 会話の文脈（前後の発言の流れ）を考慮して、発言の意図を正しく読み取るぽん。\n"
    "- 検索結果から得た情報を統合し、全体像を把握してから回答するぽん。"
)


@dataclass
class MessageBuffer:
    channel_name: str
    guild_id: int
    channel_id: int
    first_message_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    messages: list[str] = field(default_factory=list)
    authors: dict[str, int] = field(default_factory=dict)


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

        # まずストア内のドキュメントを全削除
        while True:
            docs = await loop.run_in_executor(
                None,
                lambda: list(self._client.file_search_stores.documents.list(
                    parent=store_name,
                    config={"page_size": 20},
                )),
            )
            if not docs:
                break
            for doc in docs:
                try:
                    await loop.run_in_executor(
                        None,
                        lambda d=doc: self._client.file_search_stores.documents.delete(
                            name=d.name,
                        ),
                    )
                except Exception as e:
                    log.warning(f"Failed to delete doc {doc.name}: {e}")
            log.info(f"Deleted {len(docs)} docs from {store_name}")

        # ストアを削除
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
        buf.authors[author] = buf.authors.get(author, 0) + 1

        # メッセージ数閾値
        if len(buf.messages) >= FLUSH_MESSAGE_THRESHOLD:
            asyncio.create_task(self._flush_buffer(key, corpus_store_name))

    async def _flush_buffer(self, key: tuple[int, int], corpus_store_name: str | None = None):
        buf = self._buffers.pop(key, None)
        if not buf or not buf.messages:
            return

        start = buf.first_message_at.strftime("%Y-%m-%d %H:%M")
        end = datetime.now(timezone.utc).strftime("%H:%M")
        display_name = f"#{buf.channel_name} | {start}-{end}"
        text = self._build_document_text(buf.channel_name, f"{start}-{end}", buf.messages, buf.authors)

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

    async def query(self, question: str, corpus_store_name: str,
                    guild_id: int = 0, members_info: str = "", glossary_text: str = "") -> str:
        if guild_id and not self._check_rate_limit(guild_id):
            return (
                f"今日の質問上限（{DAILY_QUERY_LIMIT}回）に達しちゃったぽん...\n"
                "明日またたくさん聞いてねぽん！🙏"
            )
        try:
            system = SYSTEM_INSTRUCTION
            if members_info:
                system += f"\n\n【メンバー情報】\n{members_info}"
            if glossary_text:
                system += f"\n\n【用語辞書】以下の用語は矢上祭実行委員会特有の用語だぽん。回答時に参考にするぽん。\n{glossary_text}"

            response = await self._client.aio.models.generate_content(
                model="gemini-2.5-flash",
                contents=question,
                config=types.GenerateContentConfig(
                    system_instruction=system,
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

    @staticmethod
    def _build_document_text(channel_name: str, bucket_key: str, lines: list[str],
                              authors: dict[str, int]) -> str:
        """ドキュメントテキストを構築。参加者サマリー付き。"""
        # 参加者サマリー（発言数順）
        sorted_authors = sorted(authors.items(), key=lambda x: x[1], reverse=True)
        participants = ", ".join(f"{name}({count}件)" for name, count in sorted_authors)

        return (
            f"チャンネル: #{channel_name}\n"
            f"期間: {bucket_key}\n"
            f"参加者: {participants}\n"
            f"発言数: {len(lines)}\n\n"
            + "\n".join(lines)
        )

    async def backfill_channel(self, channel, corpus_store_name: str,
                               after=None, progress_callback=None) -> int:
        """チャンネルの履歴を取得してバッチアップロード。after=Noneで全量。件数を返す。"""
        count = 0
        # バケット: key -> {"lines": [...], "authors": {name: count}}
        buckets: dict[str, dict] = {}

        async for message in channel.history(limit=None, after=after, oldest_first=True):
            if message.author.bot or len(message.content) < 4:
                continue
            if message.content.startswith("/"):
                continue

            ts = message.created_at
            bucket_key = ts.strftime("%Y-%m-%d %H:00")
            author_name = message.author.display_name
            line = f"[{ts.strftime('%H:%M')}] [{author_name}]: {message.content}"

            if bucket_key not in buckets:
                buckets[bucket_key] = {"lines": [], "authors": {}}
            buckets[bucket_key]["lines"].append(line)
            buckets[bucket_key]["authors"][author_name] = buckets[bucket_key]["authors"].get(author_name, 0) + 1
            count += 1

            if progress_callback and count % 500 == 0:
                await progress_callback(count)

        # 小さすぎるバケットを隣接バケットと統合（最低10件）
        merged = self._merge_small_buckets(buckets, min_lines=10)

        # バケットごとにアップロード
        for bucket_key, data in merged.items():
            if not data["lines"]:
                continue
            display_name = f"#{channel.name} | {bucket_key}"
            text = self._build_document_text(
                channel.name, bucket_key, data["lines"], data["authors"]
            )
            await self._upload_document(corpus_store_name, display_name, text)

        return count

    @staticmethod
    def _merge_small_buckets(buckets: dict, min_lines: int = 10) -> dict:
        """小さいバケットを次のバケットに統合してドキュメント品質を上げる"""
        sorted_keys = sorted(buckets.keys())
        if not sorted_keys:
            return {}

        merged = {}
        current_key = sorted_keys[0]
        current = {"lines": list(buckets[sorted_keys[0]]["lines"]),
                    "authors": dict(buckets[sorted_keys[0]]["authors"])}

        for key in sorted_keys[1:]:
            if len(current["lines"]) < min_lines:
                # 小さいので次と統合
                current["lines"].extend(buckets[key]["lines"])
                for author, cnt in buckets[key]["authors"].items():
                    current["authors"][author] = current["authors"].get(author, 0) + cnt
            else:
                merged[current_key] = current
                current_key = key
                current = {"lines": list(buckets[key]["lines"]),
                            "authors": dict(buckets[key]["authors"])}

        merged[current_key] = current
        return merged
