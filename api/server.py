"""FastAPI app factory"""

from fastapi import FastAPI

from knowledge_catalog.store import CatalogStore


def create_app(bot) -> FastAPI:
    app = FastAPI(title="おしゃべりやがぽん API")
    app.state.bot = bot
    app.state.backfill_jobs = {}
    app.state.backfill_tasks = set()
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
