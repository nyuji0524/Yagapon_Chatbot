"""おしゃべりやがぽん v2 - エントリポイント"""

import asyncio
import logging
import os
import signal

import uvicorn
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
log = logging.getLogger("yagapon")
# Pycord logs an INFO line for many normal RTCP packets. During long sessions
# that synchronous log volume can starve the Discord interaction event loop.
logging.getLogger("discord.voice.receive.reader").setLevel(logging.WARNING)


async def main():
    from api.server import create_app
    from bot.client import create_bot

    bot = create_bot()
    app = create_app(bot)

    # uvicorn設定 (同一イベントループで実行)
    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("API_PORT", "8000")),
        loop="none",
        log_level="info",
    )
    server = uvicorn.Server(config)

    # graceful shutdown
    shutdown_task: asyncio.Task | None = None

    def handle_signal():
        nonlocal shutdown_task
        if shutdown_task is not None and not shutdown_task.done():
            log.info("Shutdown is already in progress")
            return
        log.info("Shutdown signal received")
        shutdown_task = asyncio.create_task(shutdown(bot, server), name="graceful-shutdown")

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, handle_signal)

    # Bot + API を同時起動
    token = os.environ.get("DISCORD_TOKEN", "")
    if not token:
        log.error("DISCORD_TOKEN is not set!")
        return

    log.info("Starting bot + API server...")
    try:
        await asyncio.gather(
            bot.start(token),
            server.serve(),
        )
    finally:
        # ``bot.close`` can release bot.start before its final durable corpus
        # flush finishes. Unexpected task failures must also checkpoint active
        # sessions and flush buffers before the event loop exits.
        if shutdown_task is None:
            shutdown_task = asyncio.create_task(
                shutdown(bot, server), name="graceful-shutdown"
            )
        await shutdown_task


async def shutdown(bot, server):
    # Stop accepting API work before closing Discord. ``bot.close`` owns all
    # bot resources (including the corpus) so each resource is flushed once.
    server.should_exit = True
    log.info("Closing bot...")
    await bot.close()


if __name__ == "__main__":
    asyncio.run(main())
