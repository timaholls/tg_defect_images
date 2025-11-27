"""
Главная точка входа для Telegram‑бота работы с дефектами.

В этом файле только:
- инициализация aiogram‑бота,
- команда /start с кратким описанием,
- подключение обработчиков из `defect_bot.setup_defect_handlers`.
"""

import asyncio
import logging
import os

from aiogram import Bot, Dispatcher, types
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.filters import Command
from aiogram.types import ReplyKeyboardRemove
from dotenv import load_dotenv

from defect_bot import setup_defect_handlers


load_dotenv()


BOT_TOKEN = os.getenv("BOT_TOKEN")


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def create_dispatcher() -> Dispatcher:
    """
    Создать Dispatcher, зарегистрировать /start и подключить новый бот дефектов.
    """

    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)

    @dp.message(Command("start"))
    async def cmd_start(message: types.Message) -> None:
        """
        Стартовая команда: кратко объясняет, что умеет бот.
        """

        text = (
            "Привет! Я бот для регистрации и ведения дефектов товаров.\n\n"
            "Доступные команды:\n"
            "/register_defect — зарегистрировать новый дефект\n"
            "/view_defect — посмотреть информацию по дефекту по ID\n"
            "/edit_defect — изменить данные существующего дефекта по ID\n"
        )
        await message.answer(text, reply_markup=ReplyKeyboardRemove())

    # Подключаем обработчики нового бота
    setup_defect_handlers(dp)

    return dp


async def main() -> None:
    """
    Запуск long‑polling aiogram‑бота.
    """

    if not BOT_TOKEN:
        raise RuntimeError("Переменная окружения BOT_TOKEN не задана")

    bot = Bot(token=BOT_TOKEN)
    dp = create_dispatcher()

    logger.info("Starting defect bot...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())


