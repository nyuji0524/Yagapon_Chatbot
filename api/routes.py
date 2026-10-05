"""API endpoints - health, status, ask, backfill"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from api.security import require_api_token
from bot.corpus import calculate_incremental_after

router = APIRouter()
log = logging.getLogger("yagapon.api")


class AskRequest(BaseModel):
    guild_id: int
    query: str = Field(min_length=1, max_length=4000)


class AskResponse(BaseModel):
    query: str
    response: str
    timestamp: str


class BackfillRequest(BaseModel):
    guild_id: int
    channel_id: int | None = None
    mode: Literal["incremental", "rebuild"] = "incremental"
    days: int | None = Field(default=30, ge=1, le=3650)


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

    answer = await bot.corpus.query(
        body.query,
        corpus,
        guild_id=body.guild_id,
        members_info=bot._build_members_info(body.guild_id),
        glossary_text=bot.config.get_glossary_text(body.guild_id),
    )
    return AskResponse(
        query=body.query,
        response=answer,
        timestamp=datetime.now(timezone.utc).isoformat(),
    )


@router.post("/backfill", dependencies=[Depends(require_api_token)])
async def backfill(request: Request, body: BackfillRequest):
    bot = request.app.state.bot
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

    job_id = uuid4().hex
    job = {
        "job_id": job_id,
        "status": "running",
        "guild_id": body.guild_id,
        "channels_total": len(channels),
        "channels_completed": 0,
        "messages_indexed": 0,
        "documents_uploaded": 0,
        "documents_replaced": 0,
        "failures": [],
        "started_at": datetime.now(timezone.utc).isoformat(),
        "finished_at": None,
    }
    request.app.state.backfill_jobs[job_id] = job

    async def run():
        try:
            for ch in channels:
                cursor = bot.config.get_backfill_cursor(body.guild_id, ch.id)
                if body.mode == "incremental":
                    after = calculate_incremental_after(cursor)
                else:
                    after = datetime.now(timezone.utc) - timedelta(days=body.days) if body.days else None
                try:
                    result = await bot.corpus.backfill_channel(
                        ch,
                        corpus,
                        after=after,
                        replace_existing=True,
                    )
                    job["messages_indexed"] += result.messages_indexed
                    job["documents_uploaded"] += result.documents_uploaded
                    job["documents_replaced"] += result.documents_replaced
                    if result.latest_message_id and result.latest_message_at:
                        await bot.config.set_backfill_cursor(
                            body.guild_id,
                            ch.id,
                            result.latest_message_id,
                            result.latest_message_at,
                        )
                except Exception as exc:
                    job["failures"].append({"channel_id": ch.id, "channel_name": ch.name, "error": str(exc)})
                    log.exception("API backfill failed for channel %s", ch.id)
                finally:
                    job["channels_completed"] += 1
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

    task = asyncio.create_task(run())
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
    if not job:
        raise HTTPException(404, "Backfill job not found")
    return job
