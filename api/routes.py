"""API endpoints - health, status, ask, backfill"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from api.security import require_api_token
from bot.corpus import (
    backfill_channel_concurrency,
    calculate_resumable_incremental_after,
    run_backfill_channels,
)
from bot.rag_judge import QualityJudgment

router = APIRouter()
log = logging.getLogger("yagapon.api")


class AskRequest(BaseModel):
    guild_id: int
    query: str = Field(min_length=1, max_length=4000)


class AskResponse(BaseModel):
    query: str
    response: str
    query_id: str | None = None
    sources: list[str] = Field(default_factory=list)
    festival: int | None = None
    no_answer: bool = False
    timestamp: str


class BackfillRequest(BaseModel):
    guild_id: int
    channel_id: int | None = None
    mode: Literal["incremental", "rebuild"] = "incremental"
    days: int | None = Field(default=30, ge=1, le=3650)
    dry_run: bool = False


class EvaluateRagRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    # Completeness-first RAG answers may span several Discord messages.  Keep
    # the operator-only evaluator able to judge the same full answer rather
    # than forcing callers to truncate it before quality checks.
    answer: str = Field(min_length=1, max_length=64000)
    sources: list[str] = Field(default_factory=list, max_length=50)
    reference_facts: list[str] = Field(default_factory=list, max_length=30)
    expected_no_answer: bool = False
    category: str = Field(default="general", max_length=100)


@router.get("/health")
async def health():
    return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}


@router.get("/status", dependencies=[Depends(require_api_token)])
async def status(request: Request):
    bot = request.app.state.bot
    return {
        "bot_connected": not bot.is_closed(),
        "bot_user": str(bot.user) if bot.user else None,
        "guilds": len(bot.guilds),
    }


@router.post("/ask", response_model=AskResponse, dependencies=[Depends(require_api_token)])
async def ask(request: Request, body: AskRequest):
    bot = request.app.state.bot
    corpus = bot.config.get_corpus(body.guild_id)
    if not corpus:
        raise HTTPException(404, "Guild not configured")

    result = await bot.corpus.query_with_trace(
        body.query,
        corpus,
        guild_id=body.guild_id,
        members_info=bot._build_members_info(body.guild_id),
        glossary_text=bot.config.get_glossary_text(body.guild_id),
        glossary=bot.config.get_glossary(body.guild_id),
        # This route is protected by the operator API token. Callers cannot
        # self-declare this bypass in the request body.
        allow_unrestricted_conversations=True,
    )
    return AskResponse(
        query=body.query,
        response=result.text,
        query_id=result.query_id,
        sources=list(result.citations),
        festival=result.festival,
        no_answer=result.no_answer,
        timestamp=datetime.now(timezone.utc).isoformat(),
    )


@router.post(
    "/rag/evaluate",
    response_model=QualityJudgment,
    dependencies=[Depends(require_api_token)],
)
async def evaluate_rag(request: Request, body: EvaluateRagRequest):
    """Evaluate an answer against explicit gold facts without exposing the AI key to CI."""
    return await request.app.state.bot.corpus.evaluate_answer_quality(body.model_dump())


@router.post("/backfill", dependencies=[Depends(require_api_token)])
async def backfill(request: Request, body: BackfillRequest):
    bot = request.app.state.bot
    if not request.app.state.accepting_backfills:
        raise HTTPException(503, "API is shutting down")
    corpus = bot.config.get_corpus(body.guild_id)
    if not corpus:
        raise HTTPException(404, "Guild not configured")

    guild = bot.get_guild(body.guild_id)
    if not guild:
        raise HTTPException(404, "Guild not found")

    if body.channel_id:
        channels = [guild.get_channel(body.channel_id)]
        if not channels[0]:
            raise HTTPException(404, "Channel not found")
    else:
        channels = [
            ch for ch in guild.text_channels
            if ch.permissions_for(guild.me).read_message_history
            and not bot.config.is_ignored(body.guild_id, ch.id)
        ]

    if not bot.corpus.start_backfill(body.guild_id):
        raise HTTPException(409, "Backfill already running for this guild")

    concurrency = min(len(channels), backfill_channel_concurrency())
    job_id = uuid4().hex
    job = {
        "job_id": job_id,
        "status": "running",
        "guild_id": body.guild_id,
        "channels_total": len(channels),
        "channels_completed": 0,
        "channel_concurrency": concurrency,
        "messages_indexed": 0,
        "documents_uploaded": 0,
        "documents_replaced": 0,
        "failures": [],
        "started_at": datetime.now(timezone.utc).isoformat(),
        "finished_at": None,
    }
    request.app.state.backfill_jobs[job_id] = job
    if hasattr(bot.operations, "start_job"):
        bot.operations.start_job("backfill", guild_id=body.guild_id, job_id=job_id, progress=job)

    async def run():
        job_lock = asyncio.Lock()

        async def process_channel(ch):
            result = None
            failure = None
            try:
                if body.mode == "incremental":
                    after = calculate_resumable_incremental_after(
                        bot.config,
                        body.guild_id,
                        ch.id,
                    )
                else:
                    after = datetime.now(timezone.utc) - timedelta(days=body.days) if body.days else None
                result = await bot.corpus.backfill_channel(
                    ch,
                    corpus,
                    after=after,
                    replace_existing=True,
                    dry_run=body.dry_run,
                )
                if result.latest_message_id and result.latest_message_at and not body.dry_run:
                    await bot.config.set_backfill_cursor(
                        body.guild_id,
                        ch.id,
                        result.latest_message_id,
                        result.latest_message_at,
                    )
            except Exception as exc:
                failure = {"channel_id": ch.id, "channel_name": ch.name, "error": str(exc)}
                log.exception("API backfill failed for channel %s", ch.id)
            finally:
                async with job_lock:
                    if result is not None:
                        job["messages_indexed"] += result.messages_indexed
                        job["documents_uploaded"] += result.documents_uploaded
                        job["documents_replaced"] += result.documents_replaced
                    if failure is not None:
                        job["failures"].append(failure)
                    job["channels_completed"] += 1
                    progress = {**job, "failures": list(job["failures"])}
                if hasattr(bot.operations, "update_job"):
                    try:
                        bot.operations.update_job(job_id, "running", progress)
                    except Exception:
                        # Progress telemetry must not orphan channel workers or
                        # turn a valid backfill into detached background work.
                        log.exception(
                            "Could not persist API backfill progress job=%s channel=%s",
                            job_id,
                            ch.id,
                        )

        try:
            await run_backfill_channels(channels, process_channel, concurrency=concurrency)
            job["status"] = "failed" if job["failures"] else "completed"
        except asyncio.CancelledError:
            job["status"] = "cancelled"
            raise
        except Exception as exc:
            job["status"] = "failed"
            job["failures"].append({"channel_id": None, "channel_name": None, "error": str(exc)})
            log.exception("API backfill job failed: %s", job_id)
        finally:
            job["finished_at"] = datetime.now(timezone.utc).isoformat()
            bot.corpus.finish_backfill(body.guild_id)
            if hasattr(bot.operations, "update_job"):
                try:
                    bot.operations.update_job(job_id, job["status"], job)
                except Exception:
                    log.exception("Could not persist terminal API backfill job %s", job_id)

    task = asyncio.create_task(run(), name=f"api-backfill-{job_id}")
    request.app.state.backfill_tasks.add(task)
    task.add_done_callback(request.app.state.backfill_tasks.discard)

    return {
        "status": "backfill_started",
        "job_id": job_id,
        "guild_id": body.guild_id,
        "channels": len(channels),
    }


@router.get("/backfill/{job_id}", dependencies=[Depends(require_api_token)])
async def backfill_status(request: Request, job_id: str):
    job = request.app.state.backfill_jobs.get(job_id)
    if not job and hasattr(request.app.state.bot.operations, "get_job"):
        job = request.app.state.bot.operations.get_job(job_id)
    if not job:
        raise HTTPException(404, "Backfill job not found")
    return job
