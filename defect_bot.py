import asyncio
import io
import json
import logging
import os
import re
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


def generate_defect_id() -> str:
    """
    Сгенерировать следующий последовательный ID дефекта: D1, D2, D3, ...

    Номер берётся из файла last_id.txt в S3, увеличивается на 1 и сохраняется обратно.
    """

    last_number = s3_storage.get_last_defect_number()
    new_number = last_number + 1
    s3_storage.save_last_defect_number(new_number)
    return f"D{new_number}"


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


def get_description_choice_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура для выбора варианта описания: исходный/резюмированный/заново/назад."""

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="1 Вариант", callback_data="desc_original"),
                InlineKeyboardButton(text="2 Вариант", callback_data="desc_summary"),
            ],
            [
                InlineKeyboardButton(text="🔄 Наговорить заново", callback_data="desc_rerecord"),
            ],
            [
                InlineKeyboardButton(text="⬅️ Назад", callback_data="reg_back"),
            ],
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


def get_photos_after_accept_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура после принятия фото: отправить ещё / продолжить."""

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📸 Отправить ещё", callback_data="photos_add_more"),
                InlineKeyboardButton(text="✅ Продолжить", callback_data="photos_next"),
            ],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="reg_back")],
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


def get_edit_media_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура для шага редактирования медиа."""

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="⬅️ Назад", callback_data="edit_back"),
                InlineKeyboardButton(text="✖️ Отмена", callback_data="edit_cancel"),
            ],
            [InlineKeyboardButton(text="💾 Сохранить изменения", callback_data="edit_save_media")],
        ]
    )


def get_edit_control_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура управления шагами редактирования."""

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="⬅️ Назад", callback_data="edit_back"),
                InlineKeyboardButton(text="✖️ Отмена", callback_data="edit_cancel"),
            ]
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
        "Вы - система анализа ТЕХНИЧЕСКОГО качества фотографий. "
        "Ваш ответ должен быть СТРОГО в формате JSON с полями 'is_acceptable' (true/false) и 'analysis' (текст). "
        "НЕ используйте markdown разметку. НЕ пишите ничего кроме JSON."
    )

    user_prompt = (
        "Оцени ТЕХНИЧЕСКОЕ качество этой фотографии. "
        "Проверь ТОЛЬКО следующие параметры:\n"
        "1. Видимость объекта (объект должен быть виден на фото)\n"
        "2. Разборчивость (можно ли понять, что изображено)\n\n"
        "ВАЖНО: НЕ оценивай наличие или отсутствие дефекта товара. "
        "НЕ оценивай качество самого товара. "
        "Проверяй ТОЛЬКО техническое качество фотографии.\n\n"
        "Фото должно быть принято (is_acceptable: true), если объект виден и фото в целом разборчиво. "
        "Небольшая размытость, неидеальное освещение или неполный фокус - это нормально, принимай такие фото.\n\n"
        "Фото должно быть отклонено (is_acceptable: false) ТОЛЬКО в крайних случаях:\n"
        "- Объект полностью не виден или невозможно понять, что изображено\n"
        "- Фото настолько темное, что ничего не разобрать\n"
        "- Фото настолько размыто, что невозможно понять, что на нем изображено\n"
        "- Фото полностью белое или черное (переэкспонировано/недоэкспонировано)"
    )

    try:
        response = await openai_client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": system_content},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": user_prompt},
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


async def transcribe_voice(voice_bytes: bytes) -> str:
    """
    Распознавание голосового сообщения через OpenAI Whisper.
    
    Возвращает распознанный текст или пустую строку при ошибке.
    """
    
    if not openai_client:
        logging.error("OpenAI client не инициализирован")
        return ""
    
    try:
        result = await openai_client.audio.transcriptions.create(
            model="whisper-1",
            file=("audio.ogg", voice_bytes, "audio/ogg")
        )
        return getattr(result, "text", "").strip()
    except Exception as e:
        logging.error(f"Ошибка распознавания голоса: {e}")
        return ""


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
    choosing_description = State()  # Выбор между исходным и резюмированным текстом
    photos = State()
    videos = State()


class EditDefectStates(StatesGroup):
    """Состояния редактирования существующего дефекта."""

    waiting_for_id = State()
    choose_field = State()
    edit_manufacturer = State()
    edit_model = State()
    edit_description = State()
    choosing_edit_description = State()  # Выбор между исходным и резюмированным текстом при редактировании
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
            "Постарайтесь указать максимальное количество деталей.\n\n"
            "💡 Вы можете написать текст или отправить голосовое сообщение.",
            reply_markup=get_back_inline_keyboard(),
        )

    @dp.message(RegisterDefectStates.description, F.text)
    async def process_description_text(message: types.Message, state: FSMContext):
        """Обработка текстового описания дефекта."""
        
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
            "Можно отправить несколько фотографий пачкой или по одной.\n"
            "Каждое фото будет проверено на качество автоматически.\n"
            "Когда закончите — нажмите кнопку «Продолжить».",
            reply_markup=get_photos_inline_keyboard(),
        )

    @dp.message(RegisterDefectStates.description, F.voice)
    async def process_description_voice(message: types.Message, state: FSMContext):
        """Обработка голосового описания дефекта."""
        
        if not message.voice:
            return
        
        # Показываем, что обрабатываем голосовое сообщение
        status_msg = await message.answer("🎤 Обрабатываю голосовое сообщение...")
        
        try:
            bot = message.bot
            # Получаем file_id голосового сообщения
            voice_file_id = message.voice.file_id
            # Получаем информацию о файле
            file = await bot.get_file(voice_file_id)
            # Скачиваем файл
            downloaded = await bot.download_file(file.file_path)
            voice_bytes = downloaded.read()
            
            # Распознаём голос
            description = await transcribe_voice(voice_bytes)
            
            if not description or len(description.strip()) < 10:
                await bot.edit_message_text(
                    chat_id=message.chat.id,
                    message_id=status_msg.message_id,
                    text="❌ Не удалось распознать голосовое сообщение или оно слишком короткое.\n\nПопробуйте отправить текст или записать голосовое сообщение ещё раз.",
                )
                return
            
            # Генерируем резюме синхронно, чтобы показать оба варианта
            await bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=status_msg.message_id,
                text="⏳ Генерирую резюме...",
            )
            
            summary = await summarize_defect_text(description)
            
            # Сохраняем оба варианта во временное хранилище
            await state.update_data(
                original_description=description,
                summary_description=summary
            )
            
            # Показываем оба варианта и предлагаем выбрать
            choice_text = (
                f"Ваш исходный текст:\n{description}\n\n"
                f"Резюмированная версия:\n{summary}\n\n"
                f"Выберите вариант для сохранения:"
            )
            
            await bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=status_msg.message_id,
                text=choice_text,
                reply_markup=get_description_choice_keyboard(),
            )
            
            # Переходим в состояние выбора
            await state.set_state(RegisterDefectStates.choosing_description)
            
        except Exception as e:
            logging.error(f"Ошибка при обработке голосового сообщения: {e}")
            await message.bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=status_msg.message_id,
                text=f"❌ Ошибка при обработке голосового сообщения: {e}\n\nПопробуйте отправить текст или записать голосовое сообщение ещё раз.",
            )

    async def _handle_desc_original_registration(callback_query: types.CallbackQuery, state: FSMContext):
        """Вспомогательная функция: обработка выбора исходного текста при регистрации."""
        
        await callback_query.answer()
        data = await state.get_data()
        original_description = data.get("original_description", "")
        
        if not original_description:
            await callback_query.message.answer("❌ Ошибка: исходный текст не найден.")
            return
        
        # Сохраняем исходный текст как raw_description
        await state.update_data(raw_description=original_description)
        
        # Генерируем резюме в фоне для хранения
        async def generate_summary_and_store():
            summary = await summarize_defect_text(original_description)
            await state.update_data(summary_description=summary)
        
        asyncio.create_task(generate_summary_and_store())
        
        # Переходим к следующему шагу
        await state.set_state(RegisterDefectStates.photos)
        await state.update_data(photo_file_ids=[])
        await callback_query.message.answer(
            "✅ Сохранён исходный текст.\n\n"
            "Теперь отправьте фото дефекта.\n\n"
            "Можно отправить несколько фотографий пачкой или по одной.\n"
            "Каждое фото будет проверено на качество автоматически.\n"
            "Когда закончите — нажмите кнопку «Продолжить».",
            reply_markup=get_photos_inline_keyboard(),
        )

    async def _handle_desc_summary_registration(callback_query: types.CallbackQuery, state: FSMContext):
        """Вспомогательная функция: обработка выбора резюмированного текста при регистрации."""
        
        await callback_query.answer()
        data = await state.get_data()
        summary_description = data.get("summary_description", "")
        
        if not summary_description:
            await callback_query.message.answer("❌ Ошибка: резюмированный текст не найден.")
            return
        
        # Сохраняем резюмированный текст как raw_description
        await state.update_data(raw_description=summary_description)
        # Также сохраняем summary_description (может быть полезно для истории)
        await state.update_data(summary_description=summary_description)
        
        # Переходим к следующему шагу
        await state.set_state(RegisterDefectStates.photos)
        await state.update_data(photo_file_ids=[])
        await callback_query.message.answer(
            "✅ Сохранён резюмированный текст.\n\n"
            "Теперь отправьте фото дефекта.\n\n"
            "Можно отправить несколько фотографий пачкой или по одной.\n"
            "Каждое фото будет проверено на качество автоматически.\n"
            "Когда закончите — нажмите кнопку «Продолжить».",
            reply_markup=get_photos_inline_keyboard(),
        )

    async def _handle_desc_rerecord_registration(callback_query: types.CallbackQuery, state: FSMContext):
        """Вспомогательная функция: обработка запроса на повторную запись голосового сообщения при регистрации."""
        
        await callback_query.answer()
        
        # Возвращаемся к состоянию описания, чтобы пользователь мог записать заново
        await state.set_state(RegisterDefectStates.description)
        # Очищаем временные данные
        await state.update_data(original_description=None, summary_description=None)
        
        await callback_query.message.answer(
            "🎤 Запишите голосовое сообщение заново.\n\n"
            "Опишите подробно, что случилось с товаром и в чем заключается дефект.\n\n"
            "Постарайтесь указать максимальное количество деталей.\n\n"
            "💡 Вы можете написать текст или отправить голосовое сообщение.",
            reply_markup=get_back_inline_keyboard(),
        )

    @dp.message(RegisterDefectStates.photos, F.photo)
    async def process_photo(message: types.Message, state: FSMContext):
        """
        Обработка фото дефекта в режиме регистрации.
        Проверяем качество каждого фото сразу при получении.
        Если фото не прошло проверку - показываем ошибку и не добавляем в список.
        """

        data = await state.get_data()
        photo_ids: List[str] = data.get("photo_file_ids", [])

        file_id = message.photo[-1].file_id
        bot = message.bot

        # Показываем, что анализируем фото
        status_msg = await message.answer("⏳ Анализирую качество фото...")

        try:
            # Скачиваем фото для проверки качества
            file = await bot.get_file(file_id)
            downloaded = await bot.download_file(file.file_path)
            photo_data = downloaded.read()

            # Проверяем качество
            is_acceptable, analysis = await analyze_image_quality_simple(photo_data)

            if is_acceptable:
                # Фото прошло проверку - добавляем в список
                photo_ids.append(file_id)
                await state.update_data(photo_file_ids=photo_ids)

                await bot.edit_message_text(
                    chat_id=message.chat.id,
                    message_id=status_msg.message_id,
                    text=f"✅ Фото принято. Всего сейчас: {len(photo_ids)}.\n\n{analysis}",
                    reply_markup=get_photos_after_accept_keyboard(),
                )
            else:
                # Фото не прошло проверку - не добавляем в список
                await bot.edit_message_text(
                    chat_id=message.chat.id,
                    message_id=status_msg.message_id,
                    text=f"❌ Фото не прошло проверку качества.\n\n{analysis}\n\nПопробуйте отправить другое фото.",
                    reply_markup=get_photos_after_accept_keyboard(),
                )

        except Exception as e:
            # Ошибка при обработке фото
            await bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=status_msg.message_id,
                text=f"❌ Ошибка при обработке фото: {e}\n\nПопробуйте отправить фото ещё раз.",
                reply_markup=get_photos_after_accept_keyboard(),
            )

    @dp.callback_query(F.data == "photos_add_more")
    async def handle_photos_add_more(callback_query: types.CallbackQuery, state: FSMContext):
        """
        Подсказка пользователю, что можно отправить ещё фото.
        """

        current_state = await state.get_state()
        if current_state != RegisterDefectStates.photos.state:
            await callback_query.answer()
            return

        await callback_query.answer()
        data = await state.get_data()
        photo_count = len(data.get("photo_file_ids", []))
        await callback_query.message.answer(
            f"📸 Отправьте следующее фото.\n\n"
            f"Сейчас у вас {photo_count} фото. Можно добавить ещё.",
            reply_markup=get_photos_after_accept_keyboard(),
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
            "Введите регистрационный номер дефекта:",
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
                filename = photo_info.get("filename")
                if not file_id:
                    continue
                try:
                    # Создаем кнопку для копирования ссылки
                    copy_keyboard = None
                    if filename:
                        copy_keyboard = InlineKeyboardMarkup(
                            inline_keyboard=[
                                [
                                    InlineKeyboardButton(
                                        text="📋 Скопировать ссылку",
                                        callback_data=f"copy_url_{defect_id}_{filename}",
                                    )
                                ]
                            ]
                        )
                    
                    await message.answer_photo(
                        photo=file_id,
                        caption=f"Фото {idx} из {len(photos)}",
                        reply_markup=copy_keyboard,
                    )
                except Exception as e:
                    # Не падаем, просто логируем в stdout
                    print(f"Failed to send photo #{idx} for defect {defect_id}: {e}")

        # Дополнительно подгружаем видео (если в json сохранены file_id)
        videos = defect_data.get("videos") or []
        if videos:
            await message.answer("🎥 Видео дефекта:")
            for idx, video_info in enumerate(videos, start=1):
                file_id = video_info.get("file_id")
                filename = video_info.get("filename")
                if not file_id:
                    continue
                try:
                    # Создаем кнопку для копирования ссылки
                    copy_keyboard = None
                    if filename:
                        copy_keyboard = InlineKeyboardMarkup(
                            inline_keyboard=[
                                [
                                    InlineKeyboardButton(
                                        text="📋 Скопировать ссылку",
                                        callback_data=f"copy_url_{defect_id}_{filename}",
                                    )
                                ]
                            ]
                        )
                    
                    await message.answer_video(
                        video=file_id,
                        caption=f"Видео {idx} из {len(videos)}",
                        reply_markup=copy_keyboard,
                    )
                except Exception as e:
                    # Не падаем, просто логируем в stdout
                    print(f"Failed to send video #{idx} for defect {defect_id}: {e}")

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

    @dp.callback_query(F.data.startswith("copy_url_"))
    async def handle_copy_url(callback_query: types.CallbackQuery, state: FSMContext):
        """Обработка запроса на копирование ссылки на файл из S3."""
        
        await callback_query.answer()
        
        # Парсим callback_data: copy_url_<defect_id>_<filename>
        # Формат: copy_url_D1_photo_1.jpg или copy_url_D1_video_1.mp4
        data = callback_query.data.replace("copy_url_", "")
        # Ищем первое вхождение паттерна ID дефекта (D + число)
        match = re.match(r"^(D\d+)_(.+)$", data)
        if not match:
            await callback_query.message.answer("❌ Ошибка: неверный формат запроса.")
            return
        
        defect_id = match.group(1)
        filename = match.group(2)
        
        # Получаем URL из S3
        url = s3_storage.get_file_url(defect_id, filename)
        
        if not url:
            await callback_query.message.answer("❌ Не удалось получить ссылку на файл.")
            return
        
        # Отправляем ссылку пользователю
        await callback_query.message.answer(
            f"🔗 Ссылка на файл:\n\n{url}\n\n"
        )

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
        data = await state.get_data()
        defect_data = data.get("defect_data", {})

        if choice == "1":
            current_manufacturer = defect_data.get("manufacturer", "")
            await state.set_state(EditDefectStates.edit_manufacturer)
            await message.answer(
                f"Текущее название производителя: {current_manufacturer}\n\n"
                "Введите новое название производителя:",
                reply_markup=get_edit_control_keyboard(),
            )
        elif choice == "2":
            current_model = defect_data.get("model", "")
            await state.set_state(EditDefectStates.edit_model)
            await message.answer(
                f"Текущее название модели: {current_model}\n\n"
                "Введите новое название модели:",
                reply_markup=get_edit_control_keyboard(),
            )
        elif choice == "3":
            current_description = defect_data.get("raw_description", "")
            await state.set_state(EditDefectStates.edit_description)
            await message.answer(
                f"Текущее описание: {current_description}\n\n"
                "Введите новое подробное описание того, что случилось.\n\n"
                "💡 Вы можете написать текст или отправить голосовое сообщение.",
                reply_markup=get_edit_control_keyboard(),
            )
        elif choice == "4":
            await state.set_state(EditDefectStates.edit_photos)
            await state.update_data(photo_file_ids=[])
            
            # Отправляем текущие фото
            photos = defect_data.get("photos") or []
            defect_id = data.get("defect_id", "")
            if photos:
                await message.answer("📸 Текущие фото дефекта:")
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
            
            await message.answer(
                "Отправьте новые фото дефекта.\n"
                "Старые фото будут удалены и заменены новыми.\n"
                "Когда закончите — нажмите «💾 Сохранить изменения».",
                reply_markup=get_edit_media_keyboard(),
            )
        elif choice == "5":
            await state.set_state(EditDefectStates.edit_videos)
            await state.update_data(video_file_ids=[])
            
            # Отправляем текущие видео
            videos = defect_data.get("videos") or []
            defect_id = data.get("defect_id", "")
            if videos:
                await message.answer("🎥 Текущие видео дефекта:")
                for idx, video_info in enumerate(videos, start=1):
                    file_id = video_info.get("file_id")
                    if not file_id:
                        continue
                    try:
                        await message.answer_video(
                            video=file_id,
                            caption=f"Видео {idx} из {len(videos)}",
                        )
                    except Exception as e:
                        # Не падаем, просто логируем в stdout
                        print(f"Failed to send video #{idx} for defect {defect_id}: {e}")
            
            await message.answer(
                "Отправьте новые видео дефекта.\n"
                "Старые видео будут удалены и заменены новыми.\n"
                "Когда закончите — нажмите «💾 Сохранить изменения».",
                reply_markup=get_edit_media_keyboard(),
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

    @dp.message(EditDefectStates.edit_description, F.text)
    async def process_edit_description_text(message: types.Message, state: FSMContext):
        """Обработка текстового описания дефекта при редактировании."""
        
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

    @dp.message(EditDefectStates.edit_description, F.voice)
    async def process_edit_description_voice(message: types.Message, state: FSMContext):
        """Обработка голосового описания дефекта при редактировании."""
        
        if not message.voice:
            return
        
        # Показываем, что обрабатываем голосовое сообщение
        status_msg = await message.answer("🎤 Обрабатываю голосовое сообщение...")
        
        try:
            bot = message.bot
            # Получаем file_id голосового сообщения
            voice_file_id = message.voice.file_id
            # Получаем информацию о файле
            file = await bot.get_file(voice_file_id)
            # Скачиваем файл
            downloaded = await bot.download_file(file.file_path)
            voice_bytes = downloaded.read()
            
            # Распознаём голос
            description = await transcribe_voice(voice_bytes)
            
            if not description or len(description.strip()) < 10:
                await bot.edit_message_text(
                    chat_id=message.chat.id,
                    message_id=status_msg.message_id,
                    text="❌ Не удалось распознать голосовое сообщение или оно слишком короткое.\n\nПопробуйте отправить текст или записать голосовое сообщение ещё раз.",
                )
                return
            
            # Генерируем резюме синхронно, чтобы показать оба варианта
            await bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=status_msg.message_id,
                text="⏳ Генерирую резюме...",
            )
            
            summary = await summarize_defect_text(description)
            
            # Сохраняем оба варианта во временное хранилище
            await state.update_data(
                original_description=description,
                summary_description=summary
            )
            
            # Показываем оба варианта и предлагаем выбрать
            choice_text = (
                f"Ваш исходный текст:\n{description}\n\n"
                f"Резюмированная версия:\n{summary}\n\n"
                f"Выберите вариант для сохранения:"
            )
            
            await bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=status_msg.message_id,
                text=choice_text,
                reply_markup=get_description_choice_keyboard(),
            )
            
            # Переходим в состояние выбора
            await state.set_state(EditDefectStates.choosing_edit_description)
            
        except Exception as e:
            logging.error(f"Ошибка при обработке голосового сообщения: {e}")
            await message.bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=status_msg.message_id,
                text=f"❌ Ошибка при обработке голосового сообщения: {e}\n\nПопробуйте отправить текст или записать голосовое сообщение ещё раз.",
            )

    async def handle_edit_desc_original(callback_query: types.CallbackQuery, state: FSMContext):
        """Вспомогательная функция: обработка выбора исходного текста при редактировании."""
        
        await callback_query.answer()
        data = await state.get_data()
        original_description = data.get("original_description", "")
        
        if not original_description:
            await callback_query.message.answer("❌ Ошибка: исходный текст не найден.")
            return
        
        defect_data = data.get("defect_data", {})
        defect_data["raw_description"] = original_description
        
        # Генерируем и сохраняем резюме
        summary = await summarize_defect_text(original_description)
        defect_data["summary_description"] = summary
        defect_data["updated_at"] = datetime.now().isoformat()
        defect_id = data["defect_id"]
        s3_storage.save_defect_json(defect_id, json.dumps(defect_data, ensure_ascii=False, indent=2))
        
        await state.clear()
        await callback_query.message.answer("✅ Описание обновлено. Ваша заявка принята.", reply_markup=ReplyKeyboardRemove())

    async def handle_edit_desc_summary(callback_query: types.CallbackQuery, state: FSMContext):
        """Вспомогательная функция: обработка выбора резюмированного текста при редактировании."""
        
        await callback_query.answer()
        data = await state.get_data()
        summary_description = data.get("summary_description", "")
        
        if not summary_description:
            await callback_query.message.answer("❌ Ошибка: резюмированный текст не найден.")
            return
        
        defect_data = data.get("defect_data", {})
        # Сохраняем резюмированный текст как raw_description
        defect_data["raw_description"] = summary_description
        # Также сохраняем summary_description
        defect_data["summary_description"] = summary_description
        defect_data["updated_at"] = datetime.now().isoformat()
        
        defect_id = data["defect_id"]
        s3_storage.save_defect_json(defect_id, json.dumps(defect_data, ensure_ascii=False, indent=2))
        
        await state.clear()
        await callback_query.message.answer("✅ Описание обновлено. Ваша заявка принята.", reply_markup=ReplyKeyboardRemove())

    async def handle_edit_desc_rerecord(callback_query: types.CallbackQuery, state: FSMContext):
        """Вспомогательная функция: обработка запроса на повторную запись голосового сообщения при редактировании."""
        
        await callback_query.answer()
        
        # Возвращаемся к состоянию описания, чтобы пользователь мог записать заново
        await state.set_state(EditDefectStates.edit_description)
        # Очищаем временные данные
        await state.update_data(original_description=None, summary_description=None)
        
        data = await state.get_data()
        defect_data = data.get("defect_data", {})
        current_description = defect_data.get("raw_description", "")
        
        await callback_query.message.answer(
            f"Текущее описание: {current_description}\n\n"
            "🎤 Запишите голосовое сообщение заново.\n\n"
            "Введите новое подробное описание того, что случилось.\n\n"
            "💡 Вы можете написать текст или отправить голосовое сообщение.",
            reply_markup=get_edit_control_keyboard(),
        )

    @dp.callback_query(F.data == "desc_original")
    async def handle_desc_original_universal(callback_query: types.CallbackQuery, state: FSMContext):
        """Универсальный обработчик для выбора исходного текста."""
        current_state = await state.get_state()
        
        if current_state == EditDefectStates.choosing_edit_description.state:
            await handle_edit_desc_original(callback_query, state)
        elif current_state == RegisterDefectStates.choosing_description.state:
            await _handle_desc_original_registration(callback_query, state)
        else:
            await callback_query.answer()

    @dp.callback_query(F.data == "desc_summary")
    async def handle_desc_summary_universal(callback_query: types.CallbackQuery, state: FSMContext):
        """Универсальный обработчик для выбора резюмированного текста."""
        current_state = await state.get_state()
        
        if current_state == EditDefectStates.choosing_edit_description.state:
            await handle_edit_desc_summary(callback_query, state)
        elif current_state == RegisterDefectStates.choosing_description.state:
            await _handle_desc_summary_registration(callback_query, state)
        else:
            await callback_query.answer()

    @dp.callback_query(F.data == "desc_rerecord")
    async def handle_desc_rerecord_universal(callback_query: types.CallbackQuery, state: FSMContext):
        """Универсальный обработчик для повторной записи."""
        current_state = await state.get_state()
        
        if current_state == EditDefectStates.choosing_edit_description.state:
            await handle_edit_desc_rerecord(callback_query, state)
        elif current_state == RegisterDefectStates.choosing_description.state:
            await _handle_desc_rerecord_registration(callback_query, state)
        else:
            await callback_query.answer()

    @dp.callback_query(F.data == "edit_cancel")
    async def handle_edit_cancel(callback_query: types.CallbackQuery, state: FSMContext):
        """Отмена редактирования по инлайн-кнопке."""

        await state.clear()
        await callback_query.answer()
        await callback_query.message.answer("Редактирование отменено.", reply_markup=ReplyKeyboardRemove())

    @dp.callback_query(F.data == "edit_back")
    async def handle_edit_back(callback_query: types.CallbackQuery, state: FSMContext):
        """
        Возврат к выбору поля при редактировании.
        Если данных нет — просто выходим.
        """

        current_state = await state.get_state()
        await callback_query.answer()

        if not current_state:
            return

        data = await state.get_data()
        defect_id = data.get("defect_id")
        defect_data = data.get("defect_data")

        if not defect_id or not defect_data:
            await state.clear()
            await callback_query.message.answer("Редактирование отменено.", reply_markup=ReplyKeyboardRemove())
            return

        if current_state == EditDefectStates.choose_field.state:
            await state.clear()
            await callback_query.message.answer("Редактирование отменено.", reply_markup=ReplyKeyboardRemove())
            return

        # При возврате очищаем временные коллекции
        if current_state == EditDefectStates.edit_photos.state:
            await state.update_data(photo_file_ids=[])
        if current_state == EditDefectStates.edit_videos.state:
            await state.update_data(video_file_ids=[])

        # Если находимся в состоянии выбора описания, возвращаемся к редактированию описания
        if current_state == EditDefectStates.choosing_edit_description.state:
            await state.set_state(EditDefectStates.edit_description)
            # Очищаем временные данные
            await state.update_data(original_description=None, summary_description=None)
            current_description = defect_data.get("raw_description", "")
            await callback_query.message.answer(
                f"Текущее описание: {current_description}\n\n"
                "Введите новое подробное описание того, что случилось.\n\n"
                "💡 Вы можете написать текст или отправить голосовое сообщение.",
                reply_markup=get_edit_control_keyboard(),
            )
            return

        if current_state in {
            EditDefectStates.edit_manufacturer.state,
            EditDefectStates.edit_model.state,
            EditDefectStates.edit_description.state,
            EditDefectStates.edit_photos.state,
            EditDefectStates.edit_videos.state,
        }:
            await _start_edit_flow(callback_query.message, state, defect_id, defect_data)
            return

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
        elif current_state == RegisterDefectStates.choosing_description.state:
            # Возвращаемся к состоянию описания
            await state.set_state(RegisterDefectStates.description)
            # Очищаем временные данные выбора
            await state.update_data(original_description=None, summary_description=None)
            await callback_query.message.answer(
                "Опишите подробно, что случилось с товаром и в чем заключается дефект.\n\n"
                "Постарайтесь указать максимальное количество деталей.\n\n"
                "💡 Вы можете написать текст или отправить голосовое сообщение.",
                reply_markup=get_back_inline_keyboard(),
            )
        elif current_state == RegisterDefectStates.photos.state:
            await state.set_state(RegisterDefectStates.description)
            await callback_query.message.answer(
                "Опишите подробно, что случилось с товаром и в чем заключается дефект.\n\n"
                "Постарайтесь указать максимальное количество деталей.\n\n"
                "💡 Вы можете написать текст или отправить голосовое сообщение.",
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
        """
        Обработка фото при редактировании дефекта.
        Проверяем качество каждого фото сразу при получении.
        """

        data = await state.get_data()
        photo_ids: List[str] = data.get("photo_file_ids", [])

        file_id = message.photo[-1].file_id
        bot = message.bot

        # Показываем, что анализируем фото
        status_msg = await message.answer("⏳ Анализирую качество фото...")

        try:
            # Скачиваем фото для проверки качества
            file = await bot.get_file(file_id)
            downloaded = await bot.download_file(file.file_path)
            photo_data = downloaded.read()

            # Проверяем качество
            is_acceptable, analysis = await analyze_image_quality_simple(photo_data)

            if is_acceptable:
                # Фото прошло проверку - добавляем в список
                photo_ids.append(file_id)
                await state.update_data(photo_file_ids=photo_ids)

                await bot.edit_message_text(
                    chat_id=message.chat.id,
                    message_id=status_msg.message_id,
                    text=f"✅ Фото принято. Всего сейчас: {len(photo_ids)}.\n\n{analysis}",
                    reply_markup=get_edit_media_keyboard(),
                )
            else:
                # Фото не прошло проверку - не добавляем в список
                await bot.edit_message_text(
                    chat_id=message.chat.id,
                    message_id=status_msg.message_id,
                    text=f"❌ Фото не прошло проверку качества.\n\n{analysis}\n\nПопробуйте отправить другое фото.",
                    reply_markup=get_edit_media_keyboard(),
                )

        except Exception as e:
            # Ошибка при обработке фото
            await bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=status_msg.message_id,
                text=f"❌ Ошибка при обработке фото: {e}\n\nПопробуйте отправить фото ещё раз.",
                reply_markup=get_edit_media_keyboard(),
            )

    @dp.message(EditDefectStates.edit_videos, F.video)
    async def process_edit_videos_collect(message: types.Message, state: FSMContext):
        data = await state.get_data()
        video_ids: List[str] = data.get("video_file_ids", [])

        file_id = message.video.file_id
        video_ids.append(file_id)
        await state.update_data(video_file_ids=video_ids)

        await message.answer(
            f"Видео принято. Всего сейчас: {len(video_ids)}.",
            reply_markup=get_edit_media_keyboard(),
        )

    async def _save_media_changes_common(message: types.Message, state: FSMContext):
        data = await state.get_data()
        defect_id: str = data["defect_id"]
        defect_data: Dict[str, Any] = data.get("defect_data", {})
        current_state = await state.get_state()

        if current_state == EditDefectStates.edit_photos.state:
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

        elif current_state == EditDefectStates.edit_videos.state:
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

        else:
            await message.answer("Сначала загрузите новые фото или видео перед сохранением.")
            return

        defect_data["updated_at"] = datetime.now().isoformat()
        s3_storage.save_defect_json(defect_id, json.dumps(defect_data, ensure_ascii=False, indent=2))

        await state.clear()
        await message.answer("✅ Изменения сохранены. Ваша заявка принята.", reply_markup=ReplyKeyboardRemove())

    @dp.message(StateFilter(EditDefectStates.edit_photos, EditDefectStates.edit_videos), Command("save_changes"))
    async def cmd_save_media_changes(message: types.Message, state: FSMContext):
        """
        Сохранение изменений по фото/видео вручную командой.
        """

        await _save_media_changes_common(message, state)

    @dp.callback_query(F.data == "edit_save_media")
    async def handle_edit_save_media(callback_query: types.CallbackQuery, state: FSMContext):
        """Сохранение изменений по кнопке."""

        await callback_query.answer()
        await _save_media_changes_common(callback_query.message, state)


