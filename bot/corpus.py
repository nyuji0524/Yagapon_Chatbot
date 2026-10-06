"""コーパス管理 - バッチ学習バッファ、flush、RAGクエリ、コーパスCRUD"""

import asyncio
import contextlib
import hashlib
import io
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from google import genai
from google.genai import types

from bot.ai_models import generation_config, log_usage, rag_model
from bot.festival import festival_from_query, festival_metadata_filter, festival_number
from bot.rag_store import LexicalHit, RagStore

log = logging.getLogger("yagapon.corpus")

# バッチ設定
FLUSH_MESSAGE_THRESHOLD = 100  # メッセージ数でflush
FLUSH_TIME_SECONDS = 7200      # 2時間でflush
FLUSH_CHECK_INTERVAL = 120     # 2分ごとにチェック
DOCUMENT_SCHEMA_VERSION = "discord-v2"
DOCUMENT_MAX_MESSAGES = 80
DOCUMENT_MAX_CHARS = 16_000
DOCUMENT_MIN_MESSAGES = 8
DOCUMENT_SESSION_GAP = timedelta(hours=6)
DOCUMENT_MAX_MERGE_GAP = timedelta(hours=24)
UPLOAD_POLL_SECONDS = 2
UPLOAD_TIMEOUT_SECONDS = 180
JST = ZoneInfo("Asia/Tokyo")

# レート制限
DAILY_QUERY_LIMIT = int(os.environ.get("YAGAPON_DAILY_QUERY_LIMIT", "400"))


def festival_filter_enabled() -> bool:
    """Gate metadata filters until legacy File Search documents are rebuilt."""
    return os.environ.get("YAGAPON_RAG_FESTIVAL_FILTER_ENABLED", "false").lower() == "true"


def calculate_incremental_after(cursor: dict | None, now: datetime | None = None) -> datetime:
    """日単位の安定した再構築境界を返す。カーソル日は前日から重ねる。"""
    current = now or datetime.now(timezone.utc)
    cursor_at = None
    if cursor and cursor.get("message_at"):
        try:
            cursor_at = datetime.fromisoformat(cursor["message_at"])
        except (TypeError, ValueError):
            pass
    base = cursor_at or (current - timedelta(days=30))
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    local = base.astimezone(JST).replace(hour=0, minute=0, second=0, microsecond=0)
    if cursor_at is not None:
        local -= timedelta(days=1)
    return local.astimezone(timezone.utc)

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
    "- 検索結果から得た情報を統合し、全体像を把握してから回答するぽん。\n"
    "- 現在の状態を聞かれた場合は、日付が新しい根拠を優先し、古い記録と矛盾する場合は更新時期も説明するぽん。\n"
    "- 出典にない日付・担当者・決定事項を補完してはいけないぽん。"
)


@dataclass
class MessageBuffer:
    channel_name: str
    guild_id: int
    channel_id: int
    corpus_store_name: str
    first_message_at: datetime
    last_message_at: datetime
    messages: list[str] = field(default_factory=list)
    authors: dict[str, int] = field(default_factory=dict)
    first_message_id: int | None = None
    last_message_id: int | None = None
    source_url: str = ""


@dataclass(frozen=True)
class KnowledgeMessage:
    message_id: int
    timestamp: datetime
    author: str
    content: str
    jump_url: str = ""


@dataclass
class KnowledgeDocument:
    display_name: str
    text: str
    metadata: list[dict]
    message_count: int
    start_at: datetime
    end_at: datetime


@dataclass
class BackfillResult:
    messages_seen: int = 0
    messages_indexed: int = 0
    messages_skipped: int = 0
    documents_uploaded: int = 0
    documents_replaced: int = 0
    latest_message_id: int | None = None
    latest_message_at: datetime | None = None

    @property
    def ok(self) -> bool:
        return self.messages_indexed == 0 or self.documents_uploaded > 0


@dataclass(frozen=True)
class RagAnswer:
    text: str
    query_id: str | None = None
    citations: tuple[str, ...] = ()
    festival: int | None = None
    no_answer: bool = False


class CorpusManager:
    def __init__(self):
        api_key = os.environ.get("GOOGLE_API_KEY", "")
        self._client = genai.Client(api_key=api_key)
        self.rag_store = RagStore()
        self._buffers: dict[tuple[int, int], MessageBuffer] = {}
        self._upload_semaphore = asyncio.Semaphore(5)
        self._flush_task: asyncio.Task | None = None
        self._active_backfills: set[int] = set()
        # レート制限: {guild_id: {"date": "2026-03-16", "count": 42}}
        self._query_counts: dict[int, dict] = {}

    # ------ lifecycle ------

    def start_flush_loop(self):
        if self._flush_task is None:
            self._flush_task = asyncio.create_task(self._flush_loop())

    async def shutdown(self):
        if self._flush_task:
            self._flush_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._flush_task
            self._flush_task = None
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
                    corpus_store_name: str, message_id: int | None = None,
                    source_url: str = ""):
        """メッセージをバッファに追加。閾値超えたらflushをスケジュール。"""
        key = (guild_id, channel_id)
        if key not in self._buffers:
            self._buffers[key] = MessageBuffer(
                channel_name=channel_name,
                guild_id=guild_id,
                channel_id=channel_id,
                corpus_store_name=corpus_store_name,
                first_message_at=timestamp,
                last_message_at=timestamp,
                first_message_id=message_id,
                last_message_id=message_id,
                source_url=source_url,
            )

        buf = self._buffers[key]
        # セットアップ変更後は、以後のメッセージを現在のストアへ保存する。
        buf.corpus_store_name = corpus_store_name
        buf.last_message_at = max(buf.last_message_at, timestamp)
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        ts = timestamp.astimezone(JST).strftime("%Y-%m-%d %H:%M")
        buf.messages.append(f"[{ts}] [{author}]: {content}")
        buf.authors[author] = buf.authors.get(author, 0) + 1
        if message_id is not None:
            buf.first_message_id = buf.first_message_id or message_id
            buf.last_message_id = message_id
        buf.source_url = buf.source_url or source_url

        # メッセージ数閾値
        if len(buf.messages) >= FLUSH_MESSAGE_THRESHOLD:
            asyncio.create_task(self._flush_buffer(key, corpus_store_name))

    async def _flush_buffer(self, key: tuple[int, int], corpus_store_name: str | None = None) -> bool:
        buf = self._buffers.pop(key, None)
        if not buf or not buf.messages:
            return True

        start_at = buf.first_message_at.astimezone(JST)
        end_at = buf.last_message_at.astimezone(JST)
        start = start_at.strftime("%Y-%m-%d %H:%M")
        end = end_at.strftime("%Y-%m-%d %H:%M")
        display_name = f"#{buf.channel_name} | {start}-{end}"
        text = self._build_document_text(buf.channel_name, f"{start}-{end}", buf.messages, buf.authors)
        store_name = corpus_store_name or buf.corpus_store_name
        key_source = (
            f"{buf.guild_id}:{buf.channel_id}:{buf.first_message_id or start}:"
            f"{buf.last_message_id or end}:{DOCUMENT_SCHEMA_VERSION}"
        )
        metadata = [
            {"key": "source", "string_value": "discord_live"},
            {"key": "source_type", "string_value": "discord_conversation"},
            {"key": "status", "string_value": "raw"},
            {"key": "authority", "string_value": "conversation"},
            {"key": "festival", "numeric_value": float(festival_number(start_at))},
            {"key": "schema", "string_value": DOCUMENT_SCHEMA_VERSION},
            {"key": "guild_id", "string_value": str(buf.guild_id)},
            {"key": "channel_id", "string_value": str(buf.channel_id)},
            {"key": "channel_name", "string_value": buf.channel_name[:500]},
            {"key": "start_at", "string_value": start_at.isoformat()},
            {"key": "end_at", "string_value": end_at.isoformat()},
            {"key": "start_epoch", "numeric_value": start_at.timestamp()},
            {"key": "end_epoch", "numeric_value": end_at.timestamp()},
            {"key": "message_count", "numeric_value": float(len(buf.messages))},
            {
                "key": "document_key",
                "string_value": hashlib.sha256(key_source.encode()).hexdigest()[:24],
            },
            {"key": "source_url", "string_value": buf.source_url[:500]},
        ]

        if not store_name:
            log.error("Corpus store is missing for buffered messages: guild=%s channel=%s", *key)
            self._restore_buffer(key, buf)
            return False

        if not await self._upload_document(store_name, display_name, text, metadata):
            self._restore_buffer(key, buf)
            return False
        return True

    def _restore_buffer(self, key: tuple[int, int], failed: MessageBuffer):
        """Upload失敗時に、flush中に到着した新規メッセージと結合して戻す。"""
        current = self._buffers.get(key)
        if current is None:
            self._buffers[key] = failed
            return

        current.messages = failed.messages + current.messages
        for author, count in failed.authors.items():
            current.authors[author] = current.authors.get(author, 0) + count
        current.first_message_at = min(failed.first_message_at, current.first_message_at)
        current.last_message_at = max(failed.last_message_at, current.last_message_at)
        current.first_message_id = failed.first_message_id or current.first_message_id
        current.source_url = failed.source_url or current.source_url

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

    async def _upload_document(
        self,
        store_name: str,
        display_name: str,
        text: str,
        metadata: list[dict] | None = None,
    ) -> str | None:
        """文書をアップロードし、索引作成完了まで待つ。成功時は文書名を返す。"""
        async with self._upload_semaphore:
            try:
                loop = asyncio.get_running_loop()
                file_bytes = text.encode("utf-8")

                operation = await loop.run_in_executor(
                    None,
                    lambda: self._client.file_search_stores.upload_to_file_search_store(
                        file=io.BytesIO(file_bytes),
                        file_search_store_name=store_name,
                        config={
                            "display_name": display_name,
                            "mime_type": "text/plain",
                            "custom_metadata": metadata or [],
                            "chunking_config": {
                                "white_space_config": {
                                    "max_tokens_per_chunk": 500,
                                    "max_overlap_tokens": 50,
                                }
                            },
                        },
                    ),
                )

                waited = 0
                while getattr(operation, "done", True) is not True:
                    if waited >= UPLOAD_TIMEOUT_SECONDS:
                        raise TimeoutError(f"Indexing timed out after {UPLOAD_TIMEOUT_SECONDS}s")
                    await asyncio.sleep(UPLOAD_POLL_SECONDS)
                    waited += UPLOAD_POLL_SECONDS
                    operation = await loop.run_in_executor(
                        None,
                        lambda op=operation: self._client.operations.get(op),
                    )

                if getattr(operation, "error", None):
                    raise RuntimeError(f"Indexing failed: {operation.error}")
                response = getattr(operation, "response", None)
                document_name = getattr(response, "document_name", None)
                if not document_name:
                    raise RuntimeError("Indexing completed without a document name")
                try:
                    self.rag_store.upsert_document(document_name, text, metadata)
                except Exception as exc:
                    log.warning("Local exact-match mirror update failed: %s", exc)
                log.info(f"Uploaded: {display_name} -> {store_name}")
                return document_name
            except Exception as e:
                log.error(f"Upload error ({display_name}): {e}")
                return None

    async def _delete_documents(self, document_names: list[str]) -> int:
        """指定文書をforce削除し、成功件数を返す。"""
        loop = asyncio.get_running_loop()
        deleted = 0
        for name in document_names:
            try:
                await loop.run_in_executor(
                    None,
                    lambda n=name: self._client.file_search_stores.documents.delete(
                        name=n,
                        config={"force": True},
                    ),
                )
                deleted += 1
            except Exception as exc:
                log.warning("Failed to delete document %s: %s", name, exc)
        try:
            self.rag_store.delete_remote_documents(document_names)
        except Exception as exc:
            log.warning("Local exact-match mirror deletion failed: %s", exc)
        return deleted

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
                    guild_id: int = 0, members_info: str = "", glossary_text: str = "",
                    glossary: dict | None = None) -> str:
        result = await self.query_with_trace(
            question,
            corpus_store_name,
            guild_id=guild_id,
            members_info=members_info,
            glossary_text=glossary_text,
            glossary=glossary,
        )
        return result.text

    async def query_with_trace(
        self,
        question: str,
        corpus_store_name: str,
        *,
        guild_id: int = 0,
        actor_id: int | None = None,
        channel_id: int | None = None,
        members_info: str = "",
        glossary_text: str = "",
        glossary: dict | None = None,
    ) -> RagAnswer:
        if guild_id and not self._check_rate_limit(guild_id):
            return RagAnswer(text=(
                f"今日の質問上限（{DAILY_QUERY_LIMIT}回）に達しちゃったぽん...\n"
                "明日またたくさん聞いてねぽん！🙏"
            ), no_answer=True)
        started = time.monotonic()
        festival = festival_from_query(question)
        metadata_filter = festival_metadata_filter(festival) if festival_filter_enabled() else None
        terms = self.rag_store.query_terms(question, glossary)
        lexical_hits = self.rag_store.search_exact(
            guild_id, terms, festival=festival, limit=4
        ) if guild_id else []
        try:
            system = SYSTEM_INSTRUCTION
            if festival is not None:
                system += f"\n\n【対象年度】{festival}thの記録だけを根拠に回答するぽん。"
            if members_info:
                system += f"\n\n【メンバー情報】\n{members_info}"
            if glossary_text:
                system += f"\n\n【用語辞書】以下の用語は矢上祭実行委員会特有の用語だぽん。回答時に参考にするぽん。\n{glossary_text}"
            if lexical_hits:
                system += (
                    "\n\n【完全一致検索の候補】意味検索とは別に取得した原文候補だぽん。"
                    "質問との関係を確認し、関係がないものは使わないぽん。\n"
                    + self._lexical_context(lexical_hits)
                )

            model = rag_model()
            search = types.FileSearch(
                file_search_store_names=[corpus_store_name],
                top_k=12,
                metadata_filter=metadata_filter,
            )
            response = await self._client.aio.models.generate_content(
                model=model,
                contents=question,
                config=generation_config(
                    model,
                    thinking_level="low",
                    max_output_tokens=1024,
                    system_instruction=system,
                    tools=[
                        types.Tool(
                            file_search=search
                        )
                    ],
                ),
            )
            log_usage(log, "rag_query", model, response)
            answer = response.text or "回答を生成できなかったぽん..."
            sources = self._response_sources(response)
            sources.extend(self._lexical_sources(lexical_hits, sources))
            no_answer = not sources
            if no_answer:
                answer = (
                    "その件を裏付ける記録を、現在検索できるナレッジから見つけられなかったぽん。"
                    "年度や用語を変えて質問するか、資料を追加してほしいぽん。"
                )
            if sources:
                answer += "\n\n**参照**\n" + "\n".join(f"- {source}" for source in sources)
            query_id = self.rag_store.record_query(
                guild_id=guild_id,
                actor_id=actor_id,
                channel_id=channel_id,
                question=question,
                answer=answer,
                festival=festival,
                metadata_filter=metadata_filter,
                citations=sources,
                lexical_keys=[hit.document_key for hit in lexical_hits],
                latency_ms=round((time.monotonic() - started) * 1000),
                no_answer=no_answer,
            ) if guild_id else None
            return RagAnswer(
                text=answer,
                query_id=query_id,
                citations=tuple(sources),
                festival=festival,
                no_answer=no_answer,
            )
        except Exception as e:
            log.error(f"RAG query error: {e}")
            return RagAnswer(text=f"エラーが出ちゃったぽん...: {e}", festival=festival, no_answer=True)

    @staticmethod
    def _lexical_context(hits: list[LexicalHit]) -> str:
        return "\n\n".join(
            f"[exact:{hit.document_key} #{hit.channel_name} "
            f"{f'{hit.festival}th' if hit.festival is not None else '年度不明'}]\n{hit.excerpt}"
            for hit in hits
        )

    @staticmethod
    def _lexical_sources(hits: list[LexicalHit], existing: list[str]) -> list[str]:
        rendered = []
        for hit in hits:
            label = f"完全一致: #{hit.channel_name or '記録'}"
            value = f"[{label}]({hit.source_url})" if hit.source_url else label
            if value not in existing and value not in rendered:
                rendered.append(value)
        return rendered

    @classmethod
    def _response_sources(cls, response, limit: int = 5) -> list[str]:
        """File Search grounding metadataをDiscord上で読める参照一覧へ変換する。"""
        candidates = getattr(response, "candidates", None) or []
        if not candidates:
            return []
        grounding = getattr(candidates[0], "grounding_metadata", None)
        chunks = getattr(grounding, "grounding_chunks", None) or []
        sources = []
        seen = set()
        for chunk in chunks:
            context = getattr(chunk, "retrieved_context", None)
            if context is None:
                continue
            metadata = cls._metadata_map(context)
            channel = metadata.get("channel_name")
            start = str(metadata.get("start_at") or "")[:10]
            festival = metadata.get("festival")
            status = metadata.get("status")
            url = metadata.get("source_url") or getattr(context, "uri", None)
            title = getattr(context, "title", None)
            label = f"#{channel}" if channel else (title or "Discord会話記録")
            if start:
                label += f"（{start}）"
            details = []
            if festival is not None:
                details.append(f"{int(float(festival))}th")
            if status and status != "approved":
                details.append(str(status))
            if details:
                label += f" [{' / '.join(details)}]"
            rendered = f"[{label}]({url})" if url else label
            if rendered not in seen:
                seen.add(rendered)
                sources.append(rendered)
            if len(sources) >= limit:
                break
        return sources

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

    @staticmethod
    def _message_content(message) -> str:
        """検索価値のある本文と添付情報を正規化する。URLはDiscord本文内のものだけ保持。"""
        content = (getattr(message, "content", "") or "").strip()
        attachments = []
        for attachment in getattr(message, "attachments", []) or []:
            name = getattr(attachment, "filename", "添付ファイル")
            content_type = getattr(attachment, "content_type", None)
            attachments.append(f"[添付: {name}{f' ({content_type})' if content_type else ''}]")
        if attachments:
            content = "\n".join(part for part in (content, *attachments) if part)
        return content.replace("\u200b", "").strip()

    @classmethod
    def _knowledge_message(cls, message) -> KnowledgeMessage | None:
        if getattr(getattr(message, "author", None), "bot", False):
            return None
        content = cls._message_content(message)
        if not content or content.startswith("/"):
            return None
        if len(content) < 4 and not getattr(message, "attachments", None):
            return None
        timestamp = message.created_at
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        return KnowledgeMessage(
            message_id=int(message.id),
            timestamp=timestamp,
            author=message.author.display_name,
            content=content,
            jump_url=getattr(message, "jump_url", "") or "",
        )

    @staticmethod
    def _split_knowledge_messages(messages: list[KnowledgeMessage]) -> list[list[KnowledgeMessage]]:
        """会話セッションと文書サイズを考慮して検索しやすい単位へ分割する。"""
        if not messages:
            return []
        groups: list[list[KnowledgeMessage]] = []
        current: list[KnowledgeMessage] = []
        current_chars = 0
        for message in messages:
            gap = message.timestamp - current[-1].timestamp if current else timedelta(0)
            exceeds_size = (
                len(current) >= DOCUMENT_MAX_MESSAGES
                or current_chars + len(message.content) > DOCUMENT_MAX_CHARS
            )
            closes_session = bool(current) and (
                message.timestamp.astimezone(JST).date() != current[-1].timestamp.astimezone(JST).date()
                or
                gap > DOCUMENT_MAX_MERGE_GAP
                or (gap > DOCUMENT_SESSION_GAP and len(current) >= DOCUMENT_MIN_MESSAGES)
            )
            if current and (exceeds_size or closes_session):
                groups.append(current)
                current = []
                current_chars = 0
            current.append(message)
            current_chars += len(message.content)
        if current:
            groups.append(current)

        # 最後だけ極端に小さい場合は、上限内で直前の会話へ統合する。
        if len(groups) >= 2 and len(groups[-1]) < DOCUMENT_MIN_MESSAGES:
            previous, trailing = groups[-2], groups[-1]
            combined_chars = sum(len(item.content) for item in previous + trailing)
            gap = trailing[0].timestamp - previous[-1].timestamp
            if (
                len(previous) + len(trailing) <= DOCUMENT_MAX_MESSAGES
                and combined_chars <= DOCUMENT_MAX_CHARS
                and gap <= DOCUMENT_MAX_MERGE_GAP
                and previous[-1].timestamp.astimezone(JST).date()
                == trailing[0].timestamp.astimezone(JST).date()
            ):
                groups[-2] = previous + trailing
                groups.pop()
        return groups

    @classmethod
    def _build_knowledge_documents(cls, channel, messages: list[KnowledgeMessage]) -> list[KnowledgeDocument]:
        guild_id = int(channel.guild.id)
        channel_id = int(channel.id)
        channel_name = str(channel.name)
        category = getattr(getattr(channel, "category", None), "name", "") or ""
        topic = (getattr(channel, "topic", None) or "").strip()
        documents = []
        for group in cls._split_knowledge_messages(messages):
            start_at = group[0].timestamp.astimezone(JST)
            end_at = group[-1].timestamp.astimezone(JST)
            key_source = f"{guild_id}:{channel_id}:{group[0].message_id}:{group[-1].message_id}:{DOCUMENT_SCHEMA_VERSION}"
            document_key = hashlib.sha256(key_source.encode()).hexdigest()[:24]
            authors: dict[str, int] = {}
            lines = []
            for item in group:
                authors[item.author] = authors.get(item.author, 0) + 1
                local = item.timestamp.astimezone(JST)
                lines.append(f"[{local.strftime('%Y-%m-%d %H:%M')}] [{item.author}]: {item.content}")
            participants = ", ".join(
                f"{name}({count}件)"
                for name, count in sorted(authors.items(), key=lambda value: value[1], reverse=True)
            )
            source_url = group[0].jump_url
            header = [
                "文書種別: Discord会話記録",
                f"チャンネル: #{channel_name}",
                f"カテゴリ: {category or 'なし'}",
                f"期間: {start_at.isoformat(timespec='minutes')} ～ {end_at.isoformat(timespec='minutes')}",
                f"参加者: {participants}",
                f"発言数: {len(group)}",
                f"ソース: {source_url or 'Discord履歴'}",
            ]
            if topic:
                header.append(f"チャンネル説明: {topic}")
            text = "\n".join(header) + "\n\n" + "\n".join(lines)
            display_name = (
                f"#{channel_name} | {start_at.strftime('%Y-%m-%d %H:%M')}"
                f"-{end_at.strftime('%Y-%m-%d %H:%M')} | {document_key[:8]}"
            )
            metadata = [
                {"key": "source", "string_value": "discord"},
                {"key": "source_type", "string_value": "discord_conversation"},
                {"key": "status", "string_value": "raw"},
                {"key": "authority", "string_value": "conversation"},
                {"key": "festival", "numeric_value": float(festival_number(start_at))},
                {"key": "schema", "string_value": DOCUMENT_SCHEMA_VERSION},
                {"key": "guild_id", "string_value": str(guild_id)},
                {"key": "channel_id", "string_value": str(channel_id)},
                {"key": "channel_name", "string_value": channel_name[:500]},
                {"key": "category", "string_value": category[:500]},
                {"key": "start_at", "string_value": start_at.isoformat()},
                {"key": "end_at", "string_value": end_at.isoformat()},
                {"key": "start_epoch", "numeric_value": start_at.timestamp()},
                {"key": "end_epoch", "numeric_value": end_at.timestamp()},
                {"key": "message_count", "numeric_value": float(len(group))},
                {"key": "document_key", "string_value": document_key},
                {"key": "source_url", "string_value": source_url[:500]},
            ]
            documents.append(KnowledgeDocument(
                display_name=display_name,
                text=text,
                metadata=metadata,
                message_count=len(group),
                start_at=start_at,
                end_at=end_at,
            ))
        return documents

    @staticmethod
    def _metadata_map(document) -> dict[str, object]:
        result = {}
        for item in getattr(document, "custom_metadata", None) or []:
            if isinstance(item, dict):
                value = item.get("string_value")
                if value is None:
                    value = item.get("numeric_value")
                result[item.get("key", "")] = value
                continue
            value = getattr(item, "string_value", None)
            if value is None:
                value = getattr(item, "numeric_value", None)
            result[getattr(item, "key", "")] = value
        return result

    async def _existing_channel_documents(self, store_name: str, channel, after=None) -> list[str]:
        """置換対象を事前取得する。metadataなしの旧形式も表示名で対象化する。"""
        loop = asyncio.get_running_loop()
        documents = await loop.run_in_executor(
            None,
            lambda: list(self._client.file_search_stores.documents.list(
                parent=store_name,
                config={"page_size": 20},
            )),
        )
        prefix = f"#{channel.name} | "
        after_timestamp = after.timestamp() if after else None
        matches = []
        for document in documents:
            metadata = self._metadata_map(document)
            channel_matches = metadata.get("channel_id") == str(channel.id)
            legacy_matches = not metadata and (document.display_name or "").startswith(prefix)
            if not (channel_matches or legacy_matches):
                continue
            if after_timestamp is not None:
                start_epoch = metadata.get("start_epoch")
                if start_epoch is not None and float(start_epoch) < after_timestamp:
                    continue
                if start_epoch is None:
                    match = re.search(r"\| (\d{4}-\d{2}-\d{2} \d{2}:\d{2})", document.display_name or "")
                    if match:
                        document_at = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
                        if document_at < after:
                            continue
            matches.append(document.name)
        return matches

    async def backfill_channel(
        self,
        channel,
        corpus_store_name: str,
        after=None,
        progress_callback=None,
        replace_existing: bool = True,
    ) -> BackfillResult:
        """履歴を再現可能な文書へ変換し、成功後に同範囲の旧文書を置換する。"""
        result = BackfillResult()
        existing = []
        if replace_existing:
            existing = await self._existing_channel_documents(corpus_store_name, channel, after)

        messages = []
        async for message in channel.history(limit=None, after=after, oldest_first=True):
            result.messages_seen += 1
            knowledge_message = self._knowledge_message(message)
            if knowledge_message is None:
                result.messages_skipped += 1
                continue
            messages.append(knowledge_message)
            result.messages_indexed += 1
            result.latest_message_id = knowledge_message.message_id
            result.latest_message_at = knowledge_message.timestamp
            if progress_callback and result.messages_seen % 500 == 0:
                await progress_callback(result.messages_seen)

        documents = self._build_knowledge_documents(channel, messages)
        uploaded_names: list[str] = []
        for document in documents:
            uploaded = await self._upload_document(
                corpus_store_name,
                document.display_name,
                document.text,
                document.metadata,
            )
            if not uploaded:
                if uploaded_names:
                    await self._delete_documents(uploaded_names)
                raise RuntimeError(f"Failed to index {document.display_name}")
            if isinstance(uploaded, str):
                uploaded_names.append(uploaded)
            result.documents_uploaded += 1

        # 新文書が全てACTIVEになった後だけ旧文書を削除する。
        if replace_existing and documents:
            result.documents_replaced = await self._delete_documents(existing)
        return result

    def start_backfill(self, guild_id: int) -> bool:
        """同じサーバーへの重複実行を防ぐ。"""
        if guild_id in self._active_backfills:
            return False
        self._active_backfills.add(guild_id)
        return True

    def finish_backfill(self, guild_id: int):
        self._active_backfills.discard(guild_id)
