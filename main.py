import asyncio
import logging
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from aiogram import Bot, Dispatcher
from aiogram.types import BotCommand, BotCommandScopeDefault
from config import settings
from models import init_db
from tasks.scheduler import setup_scheduler
from handlers import base, settings as settings_handlers, actions, admin, subscription, profile

# Настраиваем вывод времени в логах строго по Москве (UTC+3)
MSK_TZ = timezone(timedelta(hours=3))
logging.Formatter.converter = lambda *args: datetime.now(MSK_TZ).timetuple()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [MSK] - %(name)s - %(levelname)s - %(message)s"
)


def ensure_playwright_browser():
    """
    Гарантирует наличие бинарника Chromium на хостингах вроде Bothost,
    которые запускают проект по requirements.txt в обход Dockerfile.
    """
    logging.info("Проверка наличия браузера Chromium для Playwright...")
    try:
        subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=180
        )
        logging.info("Браузер Chromium для Playwright готов к работе.")
    except Exception as e:
        logging.warning("Не удалось автоматически установить Chromium при старте: %s", e)


async def set_bot_commands(bot: Bot):
    """
    Устанавливает синюю кнопку 'Меню' слева от поля ввода.
    Нажатие на /start или /menu мгновенно возвращает постоянные кнопки клавиатуры.
    """
    commands = [
        BotCommand(command="start", description="🔄 Запустить бота / Показать кнопки меню"),
        BotCommand(command="menu", description="🏠 Главное меню (Сводка)"),
    ]
    await bot.set_my_commands(commands, scope=BotCommandScopeDefault())


async def main():
    await asyncio.to_thread(ensure_playwright_browser)
    await init_db()

    bot = Bot(token=settings.BOT_TOKEN)
    dp = Dispatcher()

    dp.include_router(admin.router)
    dp.include_router(base.router)
    dp.include_router(settings_handlers.router)
    dp.include_router(actions.router)
    dp.include_router(subscription.router)
    dp.include_router(profile.router)

    scheduler = setup_scheduler(bot)
    scheduler.start()

    await set_bot_commands(bot)
    logging.info("Бот запущен и готов к обработке задач.")

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown(wait=False)
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Бот остановлен.")