import asyncio
import json
import os
import random
import string
from datetime import datetime
from typing import List, Dict, Any

from aiogram import types, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.filters import Command, StateFilter
from aiogram.types import (
    ReplyKeyboardRemove,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

from openai import AsyncOpenAI
from dotenv import load_dotenv

from defect_categories import DefectOrigin, ORIGIN_TITLES, TITLE_TO_ORIGIN, get_origin_titles
from s3_storage import s3_storage


# ==== Инициализация OpenAI ====

load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
MODEL = os.getenv("MODEL")

openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None


# ==== Вспомогательные функции ====


def generate_defect_id(length: int = 6) -> str:
    """
    Сгенерировать случайный ID дефекта длиной `length`.

    Используем только заглавные буквы и цифры, чтобы ID было удобно диктовать/записывать.
    """

    alphabet = string.ascii_uppercase + string.digits
    return "".join(random.choice(alphabet) for _ in range(length))


def get_origin_keyboard() -> InlineKeyboardMarkup:
    """
    Инлайн‑клавиатура для выбора образования дефекта.

    Каждый вариант — отдельная кнопка с callback_data вида: origin_<value>.
    Внизу — кнопка "Назад", которая возвращает на предыдущий шаг
    (на первом шаге — отменяет заявку).
    """

    rows = []
    for origin, title in ORIGIN_TITLES.items():
        rows.append(
            [
                InlineKeyboardButton(
                    text=title,
                    callback_data=f"origin_{origin.value}",
                )
            ]
        )

    # Кнопка "Назад" (на первом шаге отменяет заявку)
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="reg_back")])

    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_back_inline_keyboard() -> InlineKeyboardMarkup:
    """Инлайн‑клавиатура только с кнопкой 'Назад'."""

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="reg_back")]
        ]
    )


def get_photos_inline_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура для этапа фото: назад / продолжить."""

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="reg_back")],
            [InlineKeyboardButton(text="✅ Продолжить", callback_data="photos_next")],
        ]
    )


def get_videos_inline_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура для этапа видео: назад / завершить заявку."""

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="reg_back")],
            [InlineKeyboardButton(text="✅ Завершить", callback_data="videos_finish")],
        ]
    )


async def summarize_defect_text(raw_text: str) -> str:
    """
    Краткое описание (резюме) дефекта по свободному тексту пользователя.

    Требований к строгому формату нет — просто короткий, понятный человеку текст.
    """

    if not openai_client or not MODEL:
        # Если OpenAI не настроен — возвращаем урезанный оригинальный текст
        return raw_text.strip()[:300]

    system_content = (
        "Ты помощник по обработке заявок о дефектах товаров.\n"
        "На вход ты получаешь длинное текстовое описание от человека.\n"
        "Сделай КРАТКОЕ, понятное человеку резюме основных проблем одним-двумя предложениями на русском языке.\n"
        "Не используй маркированные списки, просто связный текст.\n"
    )

    try:
        response = await openai_client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": system_content},
                {"role": "user", "content": raw_text},
            ],
            max_tokens=200,
        )
        summary = response.choices[0].message.content.strip()
        # На всякий случай ограничим длину
        return summary[:500]
    except Exception as e:
        print(f"Error while summarizing defect text: {e}")
        return raw_text.strip()[:300]


async def analyze_image_quality_simple(photo_data: bytes) -> tuple[bool, str]:
    """
    Простая проверка качества фото через OpenAI Vision.

    Использует такой же system_content, как в существующем боте.
    """

    if not openai_client or not MODEL:
        # Если OpenAI не настроен — принимаем фото по умолчанию
        return True, "AI‑проверка выключена, фото принято по умолчанию."

    import base64

    base64_image = base64.b64encode(photo_data).decode("utf-8")

    system_content = (
        "Вы - система анализа качества фотографий для отчетов о дефектах товаров. "
        "Ваш ответ должен быть СТРОГО в формате JSON с полями 'is_acceptable' (true/false) и 'analysis' (текст). "
        "НЕ используйте markdown разметку. НЕ пишите ничего кроме JSON."
    )

    try:
        response = await openai_client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": system_content},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Оцени качество фотографии дефекта товара."},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}},
                    ],
                },
            ],
            max_tokens=500,
            response_format={"type": "json_object"},
        )
        result = json.loads(response.choices[0].message.content)
        return bool(result.get("is_acceptable", False)), result.get("analysis", "Анализ не предоставлен")
    except Exception as e:
        print(f"Error while analyzing image quality: {e}")
        return False, f"Произошла ошибка при анализе изображения: {e}"


async def save_defect_to_s3(
    defect_id: str,
    user_id: int,
    origin: DefectOrigin,
    manufacturer: str,
    model: str,
    raw_description: str,
    summary_description: str,
    photo_file_ids: List[str],
    video_file_ids: List[str],
    message: types.Message,
) -> None:
    """
    Сохранить все данные дефекта (json + сами файлы) в S3.

    - data_<id>.json — структура с основными полями;
    - photo_X.jpg, video_X.mp4 — файлы из Telegram.
    """

    # Создаем папку под дефект
    s3_storage.create_defect_folder(defect_id)

    bot = message.bot
    photo_filenames: List[Dict[str, Any]] = []
    video_filenames: List[Dict[str, Any]] = []

    # Сохраняем фото
    for idx, file_id in enumerate(photo_file_ids, start=1):
        file = await bot.get_file(file_id)
        downloaded = await bot.download_file(file.file_path)
        # Простое имя файла: photo_1.jpg, photo_2.jpg и т.д.
        filename = f"photo_{idx}.jpg"
        key = s3_storage.save_defect_file(
            defect_id=defect_id,
            filename=filename,
            data=downloaded.read(),
            content_type="image/jpeg",
        )
        if key:
            photo_filenames.append({"filename": filename, "file_id": file_id})

    # Сохраняем видео
    for idx, file_id in enumerate(video_file_ids, start=1):
        file = await bot.get_file(file_id)
        downloaded = await bot.download_file(file.file_path)
        filename = f"video_{idx}.mp4"
        key = s3_storage.save_defect_file(
            defect_id=defect_id,
            filename=filename,
            data=downloaded.read(),
            content_type="video/mp4",
        )
        if key:
            video_filenames.append({"filename": filename, "file_id": file_id})

    # Формируем JSON‑структуру
    now_iso = datetime.now().isoformat()
    defect_data = {
        "id": defect_id,
        "created_at": now_iso,
        "updated_at": now_iso,
        "user_id": user_id,
        "origin": origin.value,
        "manufacturer": manufacturer,
        "model": model,
        "raw_description": raw_description,
        # Краткое описание из нейросети — не показываем пользователю, только храним
        "summary_description": summary_description,
        "photos": photo_filenames,
        "videos": video_filenames,
    }

    json_str = json.dumps(defect_data, ensure_ascii=False, indent=2)
    s3_storage.save_defect_json(defect_id, json_str)


def format_defect_for_view(defect_data: Dict[str, Any], hide_summary: bool = True) -> str:
    """
    Сформировать читаемый текст для просмотра/редактирования дефекта.

    Если hide_summary=True, поле summary_description не включается в текст.
    """

    lines = [
        f"ID дефекта: {defect_data.get('id')}",
        "",
        f"Образование дефекта: {defect_data.get('origin')}",
        f"1. Производитель: {defect_data.get('manufacturer')}",
        f"2. Модель: {defect_data.get('model')}",
        f"3. Что случилось: {defect_data.get('raw_description')}",
    ]

    if not hide_summary:
        lines.append(f"4. Краткое описание (AI): {defect_data.get('summary_description')}")

    photos = defect_data.get("photos") or []
    videos = defect_data.get("videos") or []

    lines.append(f"4. Фото дефекта: {len(photos)} шт.")
    lines.append(f"5. Видео дефекта: {len(videos)} шт.")

    return "\n".join(lines)


# ==== Состояния FSM ====


class RegisterDefectStates(StatesGroup):
    """Состояния регистрации нового дефекта."""

    origin = State()
    manufacturer = State()
    model = State()
    description = State()
    photos = State()
    videos = State()


class EditDefectStates(StatesGroup):
    """Состояния редактирования существующего дефекта."""

    waiting_for_id = State()
    choose_field = State()
    edit_manufacturer = State()
    edit_model = State()
    edit_description = State()
    edit_photos = State()
    edit_videos = State()


class ViewDefectStates(StatesGroup):
    """Состояния просмотра дефекта по ID."""

    waiting_for_id = State()


# ==== Обработчики команд ====


def setup_defect_handlers(dp):
    """
    Регистрация обработчиков команд и сообщений.

    Использование:
        from defect_bot import setup_defect_handlers
        setup_defect_handlers(dp)
    """

    # --- Регистрация нового дефекта ---

    @dp.message(Command("register_defect"))
    async def cmd_register_defect(message: types.Message, state: FSMContext):
        """
        Старт регистрации нового дефекта.
        """
        await state.clear()
        await state.set_state(RegisterDefectStates.origin)
        await message.answer(
            "Регистрация нового дефекта.\n\n"
            "Сначала выберите, на каком этапе образовался дефект:",
            reply_markup=get_origin_keyboard(),
        )

    @dp.message(RegisterDefectStates.origin)
    async def process_origin(message: types.Message, state: FSMContext):
        text = message.text.strip()
        origin = TITLE_TO_ORIGIN.get(text)
        if not origin:
            await message.answer(
                "Пожалуйста, выберите один из вариантов на клавиатуре.",
                reply_markup=get_origin_keyboard(),
            )
            return

        await state.update_data(origin=origin.value)
        await state.set_state(RegisterDefectStates.manufacturer)
        await message.answer(
            "Введите производителя товара:",
            reply_markup=get_back_inline_keyboard(),
        )

    @dp.callback_query(F.data.startswith("origin_"))
    async def process_origin_callback(callback_query: types.CallbackQuery, state: FSMContext):
        """
        Обработка выбора образования дефекта по инлайн‑кнопке.
        """

        origin_value = callback_query.data.replace("origin_", "")
        try:
            origin = DefectOrigin(origin_value)
        except ValueError:
            await callback_query.answer("Неизвестный тип дефекта.", show_alert=True)
            return

        await callback_query.answer()
        await state.update_data(origin=origin.value)
        await state.set_state(RegisterDefectStates.manufacturer)
        await callback_query.message.answer(
            "Введите производителя товара:",
            reply_markup=get_back_inline_keyboard(),
        )

    @dp.message(RegisterDefectStates.manufacturer)
    async def process_manufacturer(message: types.Message, state: FSMContext):
        manufacturer = message.text.strip()
        if not manufacturer:
            await message.answer("Производитель не может быть пустым. Введите название производителя.")
            return

        await state.update_data(manufacturer=manufacturer)
        await state.set_state(RegisterDefectStates.model)
        await message.answer("Введите модель товара:", reply_markup=get_back_inline_keyboard())

    @dp.message(RegisterDefectStates.model)
    async def process_model(message: types.Message, state: FSMContext):
        model = message.text.strip()
        if not model:
            await message.answer("Модель не может быть пустой. Введите модель товара.")
            return

        await state.update_data(model=model)
        await state.set_state(RegisterDefectStates.description)
        await message.answer(
            "Опишите подробно, что случилось с товаром и в чем заключается дефект.\n\n"
            "Постарайтесь указать максимальное количество деталей.",
            reply_markup=get_back_inline_keyboard(),
        )

    @dp.message(RegisterDefectStates.description)
    async def process_description(message: types.Message, state: FSMContext):
        description = message.text.strip()
        if len(description) < 10:
            await message.answer("Описание слишком короткое. Пожалуйста, опишите дефект подробнее (минимум 10 символов).")
            return

        await state.update_data(raw_description=description)

        # Генерируем краткое резюме в фоне, чтобы не тормозить пользователя
        async def generate_summary_and_store():
            summary = await summarize_defect_text(description)
            await state.update_data(summary_description=summary)

        asyncio.create_task(generate_summary_and_store())

        await state.set_state(RegisterDefectStates.photos)
        await state.update_data(photo_file_ids=[])
        await message.answer(
            "Теперь отправьте фото дефекта.\n\n"
            "Можно отправить несколько фотографий по очереди.\n"
            "Когда закончите — нажмите кнопку «Продолжить».",
            reply_markup=get_photos_inline_keyboard(),
        )

    @dp.message(RegisterDefectStates.photos, F.photo)
    async def process_photo(message: types.Message, state: FSMContext):
        """
        Обработка фото дефекта в режиме регистрации.
        Сохраняем только file_id, сами файлы загрузим в S3 после завершения анкеты.
        """

        data = await state.get_data()
        photo_ids: List[str] = data.get("photo_file_ids", [])

        file_id = message.photo[-1].file_id
        photo_ids.append(file_id)
        await state.update_data(photo_file_ids=photo_ids)

        await message.answer(
            f"Фото принято. Всего сейчас: {len(photo_ids)}.",
            reply_markup=get_photos_inline_keyboard(),
        )

    @dp.callback_query(F.data == "photos_next")
    async def handle_photos_next(callback_query: types.CallbackQuery, state: FSMContext):
        """
        Переход от фото к видео по инлайн‑кнопке.
        """

        current_state = await state.get_state()
        if current_state != RegisterDefectStates.photos.state:
            await callback_query.answer()
            return

        await callback_query.answer()
        await state.set_state(RegisterDefectStates.videos)
        await state.update_data(video_file_ids=[])
        await callback_query.message.answer(
            "Теперь отправьте видео дефекта (при необходимости).\n"
            "Можно отправить несколько видео.\n"
            "Если видео нет — нажмите кнопку «Завершить».",
            reply_markup=get_videos_inline_keyboard(),
        )

    @dp.message(RegisterDefectStates.videos, F.video)
    async def process_video(message: types.Message, state: FSMContext):
        data = await state.get_data()
        video_ids: List[str] = data.get("video_file_ids", [])

        file_id = message.video.file_id
        video_ids.append(file_id)
        await state.update_data(video_file_ids=video_ids)

        await message.answer(
            f"Видео принято. Всего сейчас: {len(video_ids)}.",
            reply_markup=get_videos_inline_keyboard(),
        )

    async def _finalize_defect(message: types.Message, state: FSMContext):
        """
        Завершение регистрации дефекта: генерируем ID, ждем резюме (если оно ещё не готово),
        сохраняем всё в S3 и выводим пользователю номер заявки.
        """

        data = await state.get_data()

        origin_value = data.get("origin")
        manufacturer = data.get("manufacturer", "")
        model = data.get("model", "")
        raw_description = data.get("raw_description", "")
        summary_description = data.get("summary_description")  # могло ещё не успеть записаться
        photo_ids: List[str] = data.get("photo_file_ids", [])
        video_ids: List[str] = data.get("video_file_ids", [])

        # Если резюме ещё не готово — посчитаем синхронно
        if not summary_description:
            summary_description = await summarize_defect_text(raw_description)

        defect_id = generate_defect_id()

        await message.answer("Сохраняю данные дефекта, подождите пару секунд...")

        await save_defect_to_s3(
            defect_id=defect_id,
            user_id=message.from_user.id,
            origin=DefectOrigin(origin_value),
            manufacturer=manufacturer,
            model=model,
            raw_description=raw_description,
            summary_description=summary_description,
            photo_file_ids=photo_ids,
            video_file_ids=video_ids,
            message=message,
        )

        await state.clear()

        await message.answer(
            "✅ Ваша заявка принята!\n\n"
            f"Регистрационный номер дефекта: {defect_id}\n\n"
            "По этому номеру вы сможете посмотреть или изменить данные через команды:\n"
            "/view_defect — просмотр\n"
            "/edit_defect — изменение",
            reply_markup=ReplyKeyboardRemove(),
        )

    @dp.message(RegisterDefectStates.videos, Command("finish_defect"))
    async def cmd_finish_defect(message: types.Message, state: FSMContext):
        await _finalize_defect(message, state)

    @dp.callback_query(F.data == "videos_finish")
    async def handle_videos_finish(callback_query: types.CallbackQuery, state: FSMContext):
        """
        Завершение регистрации по инлайн‑кнопке.
        """

        current_state = await state.get_state()
        if current_state != RegisterDefectStates.videos.state:
            await callback_query.answer()
            return

        await callback_query.answer()
        await _finalize_defect(callback_query.message, state)

    # --- Просмотр дефекта ---

    @dp.message(Command("view_defect"))
    async def cmd_view_defect(message: types.Message, state: FSMContext):
        await state.clear()
        await state.set_state(ViewDefectStates.waiting_for_id)
        await message.answer(
            "Введите регистрационный номер дефекта (6 символов):",
            reply_markup=ReplyKeyboardRemove(),
        )

    @dp.message(ViewDefectStates.waiting_for_id)
    async def process_view_id(message: types.Message, state: FSMContext):
        defect_id = message.text.strip().upper()

        json_str = s3_storage.load_defect_json(defect_id)
        if not json_str:
            await message.answer("❌ Дефект с таким ID не найден. Проверьте номер и попробуйте ещё раз.")
            return

        defect_data = json.loads(json_str)
        text = format_defect_for_view(defect_data, hide_summary=True)

        await state.clear()
        await message.answer(text, reply_markup=ReplyKeyboardRemove())

        # Дополнительно подгружаем фото (если в json сохранены file_id)
        photos = defect_data.get("photos") or []
        if photos:
            await message.answer("📸 Фото дефекта:")
            for idx, photo_info in enumerate(photos, start=1):
                file_id = photo_info.get("file_id")
                if not file_id:
                    continue
                try:
                    await message.answer_photo(
                        photo=file_id,
                        caption=f"Фото {idx} из {len(photos)}",
                    )
                except Exception as e:
                    # Не падаем, просто логируем в stdout
                    print(f"Failed to send photo #{idx} for defect {defect_id}: {e}")

        # Кнопка для быстрого перехода в режим редактирования текущего дефекта
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✏️ Редактировать этот дефект",
                        callback_data=f"edit_defect_{defect_id}",
                    )
                ]
            ]
        )
        await message.answer("Вы можете отредактировать этот дефект:", reply_markup=keyboard)

    # --- Редактирование дефекта ---

    @dp.message(Command("edit_defect"))
    async def cmd_edit_defect(message: types.Message, state: FSMContext):
        await state.clear()
        await state.set_state(EditDefectStates.waiting_for_id)
        await message.answer(
            "Введите регистрационный номер дефекта, который хотите изменить:",
            reply_markup=ReplyKeyboardRemove(),
        )

    # Общая команда отмены на любом шаге
    @dp.message(Command("cancel"))
    async def cmd_cancel(message: types.Message, state: FSMContext):
        await state.clear()
        await message.answer("Заполнение заявки отменено.", reply_markup=ReplyKeyboardRemove())

    async def _start_edit_flow(message: types.Message, state: FSMContext, defect_id: str, defect_data: Dict[str, Any]):
        """
        Общий хелпер: подготовить состояние и отправить список полей для редактирования.
        Используется и командой /edit_defect, и инлайн‑кнопкой из просмотра.
        """

        await state.update_data(defect_id=defect_id, defect_data=defect_data)

        text = format_defect_for_view(defect_data, hide_summary=True)
        text += (
            "\n\nЧто хотите изменить?\n"
            "1 — Производитель\n"
            "2 — Модель\n"
            "3 — Описание (что случилось)\n"
            "4 — Фото\n"
            "5 — Видео"
        )

        await state.set_state(EditDefectStates.choose_field)
        await message.answer(text)

    @dp.message(EditDefectStates.waiting_for_id)
    async def process_edit_id(message: types.Message, state: FSMContext):
        defect_id = message.text.strip().upper()
        json_str = s3_storage.load_defect_json(defect_id)
        if not json_str:
            await message.answer("❌ Дефект с таким ID не найден. Проверьте номер и попробуйте ещё раз.")
            return

        defect_data = json.loads(json_str)
        await _start_edit_flow(message, state, defect_id, defect_data)

    @dp.callback_query(F.data.startswith("edit_defect_"))
    async def process_edit_from_view(callback_query: types.CallbackQuery, state: FSMContext):
        """
        Инлайн‑кнопка «Редактировать этот дефект» после просмотра.
        Автоматически подставляет ID текущего дефекта.
        """

        defect_id = callback_query.data.replace("edit_defect_", "").strip().upper()
        json_str = s3_storage.load_defect_json(defect_id)
        if not json_str:
            await callback_query.answer("Дефект с таким ID не найден.", show_alert=True)
            return

        await callback_query.answer()
        defect_data = json.loads(json_str)
        await _start_edit_flow(callback_query.message, state, defect_id, defect_data)

    @dp.message(EditDefectStates.choose_field)
    async def process_edit_choice(message: types.Message, state: FSMContext):
        choice = message.text.strip()

        if choice == "1":
            await state.set_state(EditDefectStates.edit_manufacturer)
            await message.answer("Введите нового производителя:", reply_markup=ReplyKeyboardRemove())
        elif choice == "2":
            await state.set_state(EditDefectStates.edit_model)
            await message.answer("Введите новую модель:", reply_markup=ReplyKeyboardRemove())
        elif choice == "3":
            await state.set_state(EditDefectStates.edit_description)
            await message.answer(
                "Введите новое подробное описание того, что случилось:",
                reply_markup=ReplyKeyboardRemove(),
            )
        elif choice == "4":
            await state.set_state(EditDefectStates.edit_photos)
            await state.update_data(photo_file_ids=[])
            await message.answer(
            "Отправьте новые фото дефекта.\n"
            "Старые фото будут удалены и заменены новыми.\n"
            "Когда закончите — отправьте /save_changes.",
            reply_markup=ReplyKeyboardRemove(),
            )
        elif choice == "5":
            await state.set_state(EditDefectStates.edit_videos)
            await state.update_data(video_file_ids=[])
            await message.answer(
            "Отправьте новые видео дефекта.\n"
            "Старые видео будут удалены и заменены новыми.\n"
            "Когда закончите — отправьте /save_changes.",
            reply_markup=ReplyKeyboardRemove(),
            )
        else:
            await message.answer("Пожалуйста, введите число от 1 до 5.")

    @dp.message(EditDefectStates.edit_manufacturer)
    async def process_edit_manufacturer(message: types.Message, state: FSMContext):
        manufacturer = message.text.strip()
        if not manufacturer:
            await message.answer("Производитель не может быть пустым. Введите название производителя.")
            return

        data = await state.get_data()
        defect_data = data.get("defect_data", {})
        defect_data["manufacturer"] = manufacturer
        defect_data["updated_at"] = datetime.now().isoformat()

        defect_id = data["defect_id"]
        s3_storage.save_defect_json(defect_id, json.dumps(defect_data, ensure_ascii=False, indent=2))

        await state.clear()
        await message.answer("✅ Производитель обновлён. Ваша заявка принята.", reply_markup=ReplyKeyboardRemove())

    @dp.message(EditDefectStates.edit_model)
    async def process_edit_model(message: types.Message, state: FSMContext):
        model = message.text.strip()
        if not model:
            await message.answer("Модель не может быть пустой. Введите модель товара.")
            return

        data = await state.get_data()
        defect_data = data.get("defect_data", {})
        defect_data["model"] = model
        defect_data["updated_at"] = datetime.now().isoformat()

        defect_id = data["defect_id"]
        s3_storage.save_defect_json(defect_id, json.dumps(defect_data, ensure_ascii=False, indent=2))

        await state.clear()
        await message.answer("✅ Модель обновлена. Ваша заявка принята.", reply_markup=ReplyKeyboardRemove())

    @dp.message(EditDefectStates.edit_description)
    async def process_edit_description(message: types.Message, state: FSMContext):
        description = message.text.strip()
        if len(description) < 10:
            await message.answer("Описание слишком короткое. Пожалуйста, опишите дефект подробнее (минимум 10 символов).")
            return

        data = await state.get_data()
        defect_data = data.get("defect_data", {})
        defect_data["raw_description"] = description
        # Пересчёт краткого описания
        summary = await summarize_defect_text(description)
        defect_data["summary_description"] = summary
        defect_data["updated_at"] = datetime.now().isoformat()

        defect_id = data["defect_id"]
        s3_storage.save_defect_json(defect_id, json.dumps(defect_data, ensure_ascii=False, indent=2))

        await state.clear()
        await message.answer("✅ Описание обновлено. Ваша заявка принята.", reply_markup=ReplyKeyboardRemove())

    # --- Общая кнопка "Назад" для всех шагов регистрации ---

    @dp.callback_query(F.data == "reg_back")
    async def handle_reg_back(callback_query: types.CallbackQuery, state: FSMContext):
        """
        Реализация кнопки "Назад" для процесса регистрации дефекта.

        - На первом шаге (origin) отменяет заполнение.
        - На остальных шагах возвращает к предыдущему.
        """

        current_state = await state.get_state()
        await callback_query.answer()

        if current_state is None:
            return

        # Первый шаг — отмена
        if current_state == RegisterDefectStates.origin.state:
            await state.clear()
            await callback_query.message.answer("Заполнение заявки отменено.")
            return

        # Назад по цепочке шагов
        if current_state == RegisterDefectStates.manufacturer.state:
            await state.set_state(RegisterDefectStates.origin)
            await callback_query.message.answer(
                "Сначала выберите, на каком этапе образовался дефект:",
                reply_markup=get_origin_keyboard(),
            )
        elif current_state == RegisterDefectStates.model.state:
            await state.set_state(RegisterDefectStates.manufacturer)
            await callback_query.message.answer(
                "Введите производителя товара:",
                reply_markup=get_back_inline_keyboard(),
            )
        elif current_state == RegisterDefectStates.description.state:
            await state.set_state(RegisterDefectStates.model)
            await callback_query.message.answer(
                "Введите модель товара:",
                reply_markup=get_back_inline_keyboard(),
            )
        elif current_state == RegisterDefectStates.photos.state:
            await state.set_state(RegisterDefectStates.description)
            await callback_query.message.answer(
                "Опишите подробно, что случилось с товаром и в чем заключается дефект.\n\n"
                "Постарайтесь указать максимальное количество деталей.",
                reply_markup=get_back_inline_keyboard(),
            )
        elif current_state == RegisterDefectStates.videos.state:
            await state.set_state(RegisterDefectStates.photos)
            await callback_query.message.answer(
                "Теперь отправьте фото дефекта.\n\n"
                "Можно отправить несколько фотографий по очереди.\n"
                "Когда закончите — нажмите кнопку «Продолжить».",
                reply_markup=get_photos_inline_keyboard(),
            )

    @dp.message(EditDefectStates.edit_photos, F.photo)
    async def process_edit_photos_collect(message: types.Message, state: FSMContext):
        data = await state.get_data()
        photo_ids: List[str] = data.get("photo_file_ids", [])

        file_id = message.photo[-1].file_id
        photo_ids.append(file_id)
        await state.update_data(photo_file_ids=photo_ids)

        await message.answer(f"Фото принято. Всего сейчас: {len(photo_ids)}.")

    @dp.message(EditDefectStates.edit_videos, F.video)
    async def process_edit_videos_collect(message: types.Message, state: FSMContext):
        data = await state.get_data()
        video_ids: List[str] = data.get("video_file_ids", [])

        file_id = message.video.file_id
        video_ids.append(file_id)
        await state.update_data(video_file_ids=video_ids)

        await message.answer(f"Видео принято. Всего сейчас: {len(video_ids)}.")

    @dp.message(StateFilter(EditDefectStates.edit_photos, EditDefectStates.edit_videos), Command("save_changes"))
    async def cmd_save_media_changes(message: types.Message, state: FSMContext):
        """
        Сохранение изменений по фото/видео:
        - удаляем старые файлы по префиксу photo_/video_;
        - перезаливаем новые;
        - обновляем json.
        """

        data = await state.get_data()
        defect_id: str = data["defect_id"]
        defect_data: Dict[str, Any] = data.get("defect_data", {})

        if await state.get_state() == EditDefectStates.edit_photos.state:
            new_photo_ids: List[str] = data.get("photo_file_ids", [])
            # Удаляем старые фото
            s3_storage.delete_defect_files_by_prefix(defect_id, "photo_")

            bot = message.bot
            photo_filenames: List[Dict[str, Any]] = []
            for idx, file_id in enumerate(new_photo_ids, start=1):
                file = await bot.get_file(file_id)
                downloaded = await bot.download_file(file.file_path)
                filename = f"photo_{idx}.jpg"
                key = s3_storage.save_defect_file(
                    defect_id=defect_id,
                    filename=filename,
                    data=downloaded.read(),
                    content_type="image/jpeg",
                )
                if key:
                    photo_filenames.append({"filename": filename, "file_id": file_id})

            defect_data["photos"] = photo_filenames

        elif await state.get_state() == EditDefectStates.edit_videos.state:
            new_video_ids: List[str] = data.get("video_file_ids", [])
            s3_storage.delete_defect_files_by_prefix(defect_id, "video_")

            bot = message.bot
            video_filenames: List[Dict[str, Any]] = []
            for idx, file_id in enumerate(new_video_ids, start=1):
                file = await bot.get_file(file_id)
                downloaded = await bot.download_file(file.file_path)
                filename = f"video_{idx}.mp4"
                key = s3_storage.save_defect_file(
                    defect_id=defect_id,
                    filename=filename,
                    data=downloaded.read(),
                    content_type="video/mp4",
                )
                if key:
                    video_filenames.append({"filename": filename, "file_id": file_id})

            defect_data["videos"] = video_filenames

        defect_data["updated_at"] = datetime.now().isoformat()
        s3_storage.save_defect_json(defect_id, json.dumps(defect_data, ensure_ascii=False, indent=2))

        await state.clear()
        await message.answer("✅ Изменения сохранены. Ваша заявка принята.", reply_markup=ReplyKeyboardRemove())


