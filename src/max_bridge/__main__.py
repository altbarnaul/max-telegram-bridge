"""Entry point: python -m max_bridge"""
import asyncio
import logging
import os

from aiohttp import web

from .config import Config
from .db import Database
from .crypto import TokenCrypto
from .bot import BotService
from .session_manager import SessionManager
from .router import MessageRouter
from .api import create_api_app
from . import analytics

log = logging.getLogger("max_bridge")


async def create_app(tg_bot_token: str, encryption_key: str,
                     db_path: str, api_host: str, api_port: int,
                     admin_tg_id: int = 0) -> dict:
    """Create and wire all application components."""
    db = Database(db_path)
    await db.init()

    crypto = TokenCrypto(encryption_key)

    router_ref = [None]  # Forward reference

    async def on_max_message(user_id: int, msg: dict):
        if router_ref[0]:
            await router_ref[0].route_message(user_id, msg)

    sm = SessionManager(db=db, crypto=crypto, on_message=on_max_message)

    bot_service = BotService(
        bot_token=tg_bot_token, db=db, crypto=crypto,
        session_manager=sm, admin_tg_id=admin_tg_id,
    )
    sm.on_auth_failed = bot_service.notify_auth_failed

    # Analytics
    analytics.init("max-bridge")
    bot_service.dp.message.middleware(analytics.AnalyticsMiddleware())
    bot_service.dp.callback_query.middleware(analytics.AnalyticsMiddleware())

    router = MessageRouter(db=db, bot=bot_service.bot, session_manager=sm)
    router_ref[0] = router

    api_app = create_api_app(db=db, crypto=crypto, session_manager=sm)

    return {
        "db": db,
        "crypto": crypto,
        "bot": bot_service,
        "session_manager": sm,
        "router": router,
        "api_app": api_app,
        "api_host": api_host,
        "api_port": api_port,
    }


async def run_app(components: dict):
    """Run all services concurrently."""
    sm = components["session_manager"]
    bot = components["bot"]
    api_app = components["api_app"]

    # Start all active user sessions
    await sm.start_all()

    # Run API server
    runner = web.AppRunner(api_app)
    await runner.setup()
    site = web.TCPSite(runner, components["api_host"], components["api_port"])
    await site.start()
    log.info(f"API server started on {components['api_host']}:{components['api_port']}")

    try:
        # Run bot polling (blocks until stopped)
        await bot.start()
    finally:
        analytics.shutdown()
        await sm.stop_all()
        await runner.cleanup()
        await components["db"].close()


def main():
    cfg = Config()  # Create instance to access attributes

    logging.basicConfig(
        level=getattr(logging, cfg.LOG_LEVEL),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    os.makedirs(os.path.dirname(cfg.DB_PATH) or ".", exist_ok=True)

    async def _run():
        components = await create_app(
            tg_bot_token=cfg.TG_BOT_TOKEN,
            encryption_key=cfg.ENCRYPTION_KEY,
            db_path=cfg.DB_PATH,
            api_host=cfg.API_HOST,
            api_port=cfg.API_PORT,
            admin_tg_id=cfg.ADMIN_TG_ID,
        )
        await run_app(components)

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        log.info("MAX Bridge stopped.")


if __name__ == "__main__":
    main()
