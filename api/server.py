"""FastAPI app factory"""

from fastapi import FastAPI


def create_app(bot) -> FastAPI:
    app = FastAPI(title="おしゃべりやがぽん API")
    app.state.bot = bot

    from api.routes import router
    from api.github_webhook import router as gh_router

    app.include_router(router)
    app.include_router(gh_router)

    return app
