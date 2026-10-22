"""FastAPI app factory and background-task lifecycle helpers."""

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI

from knowledge_catalog.store import CatalogStore

log = logging.getLogger("yagapon.api")


async def shutdown_backfill_tasks(app: FastAPI) -> None:
    """Cancel and await every API backfill before bot resources are closed.

    A task can be cancelled before its coroutine gets its first execution turn,
    in which case the ``finally`` block in the route never runs.  The second
    pass over in-memory jobs therefore also releases the per-guild backfill
    guard and persists a terminal state.
    """
    app.state.accepting_backfills = False
    tasks = tuple(getattr(app.state, "backfill_tasks", ()))
    for task in tasks:
        if not task.done():
            task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)

    task_set = getattr(app.state, "backfill_tasks", None)
    if task_set is not None:
        task_set.clear()

    bot = app.state.bot
    finished_at = datetime.now(timezone.utc).isoformat()
    for job_id, job in getattr(app.state, "backfill_jobs", {}).items():
        if job.get("status") != "running":
            continue
        job["status"] = "cancelled"
        job["finished_at"] = finished_at
        operations = getattr(bot, "operations", None)
        corpus = getattr(bot, "corpus", None)
        try:
            if operations is not None and hasattr(operations, "update_job"):
                operations.update_job(job_id, "cancelled", job)
        except Exception:
            log.exception("Could not persist cancelled API backfill job %s", job_id)
        finally:
            if corpus is not None and hasattr(corpus, "finish_backfill"):
                corpus.finish_backfill(job["guild_id"])


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Reconcile stale jobs at startup and drain API work at shutdown."""
    app.state.accepting_backfills = True
    bot = app.state.bot
    operations = getattr(bot, "operations", None)
    interrupted: list[str] = []
    if operations is not None and hasattr(operations, "interrupt_running_jobs"):
        interrupted = operations.interrupt_running_jobs(
            component="backfill",
            reason="process_restart",
        )
    if interrupted:
        log.warning("Reconciled %s stale API backfill job(s)", len(interrupted))
        if hasattr(operations, "record_event"):
            operations.record_event(
                "backfill",
                "startup_reconcile",
                "interrupted",
                details={"jobs_reconciled": len(interrupted)},
            )
    replay_manager = getattr(bot, "recording_replays", None)
    replay_reconciliation = None
    if replay_manager is not None and hasattr(
        replay_manager,
        "reconcile_completed_checkpoints",
    ):
        try:
            replay_reconciliation = await asyncio.to_thread(
                replay_manager.reconcile_completed_checkpoints,
            )
        except Exception:
            # A maintenance reconciliation must never make Discord/API
            # unavailable.  It is fail-closed and can be retried next startup.
            log.exception("Completed replay checkpoint reconciliation failed")
    if replay_reconciliation and replay_reconciliation.get("reconciled"):
        reconciled = int(replay_reconciliation["reconciled"])
        log.warning("Reconciled %s completed replay checkpoint(s)", reconciled)
        if operations is not None and hasattr(operations, "record_event"):
            try:
                operations.record_event(
                    "recording_replay",
                    "startup_reconcile",
                    "success",
                    details={
                        "checkpoints_reconciled": reconciled,
                        "checkpoints_inspected": int(
                            replay_reconciliation.get("inspected") or 0
                        ),
                    },
                )
            except Exception:
                # Reconciliation telemetry is best-effort. A locked or damaged
                # operations database must not prevent the API from starting.
                log.exception("Could not record replay reconciliation event")
    try:
        yield
    finally:
        await shutdown_backfill_tasks(app)


def create_app(bot) -> FastAPI:
    app = FastAPI(title="おしゃべりやがぽん API", lifespan=_lifespan)
    app.state.bot = bot
    app.state.backfill_jobs = {}
    app.state.backfill_tasks = set()
    app.state.accepting_backfills = True
    app.state.catalog = getattr(bot, "catalog", None) or CatalogStore()

    from api.catalog_routes import router as catalog_router
    from api.operations import router as operations_router
    from api.rag_admin import router as rag_admin_router
    from api.recordings import router as recordings_router
    from api.routes import router

    app.include_router(router)
    app.include_router(catalog_router)
    app.include_router(rag_admin_router)
    app.include_router(operations_router)
    app.include_router(recordings_router)

    return app
