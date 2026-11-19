from __future__ import annotations

import asyncio
import hashlib
import logging
import os

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

from .ai import SalesAgent
from .catalog import Catalog
from .config import Settings, load_settings
from .db import Database
from .handlers import Services, admin_router, client_router

log = logging.getLogger(__name__)

WEBHOOK_PATH = "/telegram"

SHORT_DESCRIPTION = "Танцевальная студия Just Dance в Алматы для детей и подростков 6–17 лет."
DESCRIPTION = (
    "Just Dance: детские, эстрадные, народные и современные танцы для детей и подростков "
    "6–17 лет в Алматы.\n\n"
    "Здесь можно узнать расписание и цены, записаться на пробное занятие и оформить абонемент.\n\n"
    "Нажмите «Старт»."
)
COMMANDS = [
    BotCommand(command="menu", description="Главное меню"),
    BotCommand(command="cancel", description="Отменить оформление"),
    BotCommand(command="forget", description="Удалить мои данные"),
]


async def apply_profile(bot: Bot) -> None:
    """Обновляет описание и команды, только если они изменились."""
    if (await bot.get_my_short_description()).short_description != SHORT_DESCRIPTION:
        await bot.set_my_short_description(SHORT_DESCRIPTION)
    if (await bot.get_my_description()).description != DESCRIPTION:
        await bot.set_my_description(DESCRIPTION)
    current = [(c.command, c.description) for c in await bot.get_my_commands()]
    if current != [(c.command, c.description) for c in COMMANDS]:
        await bot.set_my_commands(COMMANDS)


def build_dispatcher(services: Services) -> Dispatcher:
    dispatcher = Dispatcher(storage=MemoryStorage(), services=services)
    dispatcher.include_router(admin_router)
    dispatcher.include_router(client_router)
    return dispatcher


def build_services(settings: Settings) -> Services:
    catalog = Catalog.load(settings.catalog_path)
    return Services(
        settings=settings,
        catalog=catalog,
        db=Database(settings.db_path),
        agent=SalesAgent(catalog, settings.ai_model),
    )


def webhook_secret(token: str) -> str:
    """Telegram присылает этот код с каждым запросом, чужие запросы отклоняются."""
    return hashlib.sha256(f"webhook:{token}".encode()).hexdigest()[:48]


def build_web_app(bot: Bot, dispatcher: Dispatcher) -> web.Application:
    async def health(_: web.Request) -> web.Response:
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    SimpleRequestHandler(dispatcher=dispatcher, bot=bot, secret_token=webhook_secret(bot.token)).register(
        app, path=WEBHOOK_PATH
    )
    setup_application(app, dispatcher, bot=bot)
    return app


async def run_webhook(bot: Bot, dispatcher: Dispatcher, base_url: str, port: int) -> None:
    async def on_startup(bot: Bot) -> None:
        await apply_profile(bot)
        await bot.set_webhook(
            base_url.rstrip("/") + WEBHOOK_PATH,
            secret_token=webhook_secret(bot.token),
            allowed_updates=dispatcher.resolve_used_update_types(),
        )
        log.info("Webhook set, listening on port %s", port)

    dispatcher.startup.register(on_startup)
    runner = web.AppRunner(build_web_app(bot, dispatcher))
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


async def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    services = build_services(settings)
    bot = Bot(settings.bot_token)
    dispatcher = build_dispatcher(services)
    # Render сам задаёт RENDER_EXTERNAL_URL; на ноутбуке его нет, и бот работает как раньше.
    base_url = os.getenv("WEBHOOK_BASE_URL") or os.getenv("RENDER_EXTERNAL_URL")
    try:
        if base_url:
            await run_webhook(bot, dispatcher, base_url, int(os.getenv("PORT", "8080")))
        else:
            await apply_profile(bot)
            await bot.delete_webhook()
            await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())
    finally:
        services.db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(run())
