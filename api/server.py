"""FastAPI app factory"""

from fastapi import FastAPI

from knowledge_catalog.store import CatalogStore


def create_app(bot) -> FastAPI:
    app = FastAPI(title="おしゃべりやがぽん API")
    app.state.bot = bot
    app.state.backfill_jobs = {}
    app.state.backfill_tasks = set()
    app.state.catalog = CatalogStore()

    from api.catalog_routes import router as catalog_router
    from api.github_webhook import router as gh_router
    from api.routes import router

    app.include_router(router)
    app.include_router(gh_router)
    app.include_router(catalog_router)

    return app
