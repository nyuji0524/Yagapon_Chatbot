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


async def main():
    from bot.client import create_bot
    from api.server import create_app

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
    def handle_signal():
        log.info("Shutdown signal received")
        asyncio.create_task(shutdown(bot, server))

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, handle_signal)

    # Bot + API を同時起動
    token = os.environ.get("DISCORD_TOKEN", "")
    if not token:
        log.error("DISCORD_TOKEN is not set!")
        return

    log.info("Starting bot + API server...")
    await asyncio.gather(
        bot.start(token),
        server.serve(),
    )


async def shutdown(bot, server):
    log.info("Flushing corpus buffers...")
    await bot.corpus.shutdown()
    log.info("Closing bot...")
    await bot.close()
    server.should_exit = True


if __name__ == "__main__":
    asyncio.run(main())
