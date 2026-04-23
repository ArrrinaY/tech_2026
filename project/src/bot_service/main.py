"""
Bot Service - Telegram бот для дейтинг-приложения
"""
import asyncio
import sys

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
import httpx

from common.logging_config import setup_logging, get_logger
from common.metrics import messages_processed, message_processing_duration
from common.config import get_settings

# Настройка логирования
setup_logging("bot_service")
logger = get_logger("bot_service")

settings = get_settings()

# Роутеры
router = Router()

# FSM состояния для регистрации анкеты
class ProfileStates(StatesGroup):
    waiting_for_name = State()
    waiting_for_bio = State()
    waiting_for_age = State()
    waiting_for_gender = State()
    waiting_for_city = State()
    waiting_for_photo = State()


# Вспомогательные функции

async def register_user_in_profile_service(
    telegram_id: int,
    username: str | None,
    first_name: str | None
) -> dict | None:
    """Регистрация пользователя в Profile Service"""
    url = f"http://{settings.profile_service_host}:{settings.profile_service_port}/api/v1/users/register"
    
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                url,
                json={
                    "telegram_id": telegram_id,
                    "username": username,
                    "first_name": first_name,
                }
            )
            
            if response.status_code == 201:
                logger.info("user_registered_in_profile_service", telegram_id=telegram_id)
                return response.json()
            elif response.status_code == 409:
                logger.info("user_already_exists", telegram_id=telegram_id)
                return response.json()
            else:
                logger.error("profile_service_error", status=response.status_code, body=response.text)
                return None
                
    except httpx.RequestError as e:
        logger.error("profile_service_unavailable", error=str(e))
        return None


async def update_user_name_in_profile_service(telegram_id: int, first_name: str) -> bool:
    """Обновление имени пользователя в Profile Service"""
    url = f"http://{settings.profile_service_host}:{settings.profile_service_port}/api/v1/users/{telegram_id}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.put(url, json={"first_name": first_name})
            if response.status_code == 200:
                return True
            logger.error("update_user_name_failed", status=response.status_code, body=response.text)
            return False
    except httpx.RequestError as exc:
        logger.error("update_user_name_error", error=str(exc))
        return False


async def get_user_from_profile_service(telegram_id: int) -> dict | None:
    """Получение данных пользователя из Profile Service"""
    url = f"http://{settings.profile_service_host}:{settings.profile_service_port}/api/v1/users/{telegram_id}"
    logger.info("get_user_from_profile_service", url=url, telegram_id=telegram_id)

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url)
            logger.info("get_user_response", status=response.status_code, text=response.text[:200])
            if response.status_code == 200:
                return response.json()
            return None
    except httpx.RequestError as e:
        logger.error("get_user_error", error=str(e))
        return None


async def get_profile_from_profile_service(user_id: int) -> dict | None:
    """Получение анкеты из Profile Service"""
    url = f"http://{settings.profile_service_host}:{settings.profile_service_port}/api/v1/profiles/{user_id}"
    
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url)
            if response.status_code == 200:
                return response.json()
            return None
    except httpx.RequestError:
        return None


async def get_next_discovery_profile(user_id: int) -> dict | None:
    """Получение следующей анкеты для показа пользователю"""
    url = f"http://{settings.profile_service_host}:{settings.profile_service_port}/api/v1/discovery/{user_id}/next"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url)
            if response.status_code == 200:
                return response.json()
            if response.status_code == 404:
                return None
            logger.error("get_next_discovery_profile_failed", status=response.status_code, body=response.text)
            return None
    except httpx.RequestError as exc:
        logger.error("get_next_discovery_profile_error", error=str(exc))
        return None


async def send_interaction_to_profile_service(actor_user_id: int, target_profile_id: int, action: str) -> dict | None:
    """Отправка лайка/пропуска в Profile Service"""
    url = f"http://{settings.profile_service_host}:{settings.profile_service_port}/api/v1/interactions"
    payload = {
        "actor_user_id": actor_user_id,
        "target_profile_id": target_profile_id,
        "action": action,
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(url, json=payload)
            if response.status_code == 200:
                return response.json()
            logger.error("interaction_failed", status=response.status_code, body=response.text)
            return None
    except httpx.RequestError as exc:
        logger.error("interaction_request_error", error=str(exc))
        return None


async def get_matches_from_profile_service(user_id: int) -> list[dict] | None:
    """Получение списка мэтчей пользователя"""
    url = f"http://{settings.profile_service_host}:{settings.profile_service_port}/api/v1/matches/{user_id}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url)
            if response.status_code == 200:
                return response.json()
            if response.status_code == 404:
                return []
            logger.error("get_matches_failed", status=response.status_code, body=response.text)
            return None
    except httpx.RequestError as exc:
        logger.error("get_matches_error", error=str(exc))
        return None


def format_gender(gender: str | None) -> str:
    """Преобразует значение пола в русскоязычный формат."""
    if not gender:
        return "Не указан"

    normalized = gender.strip().lower()
    gender_map = {
        "male": "Мужской",
        "female": "Женский",
        "other": "Другой",
        "мужской": "Мужской",
        "женский": "Женский",
        "другой": "Другой",
    }
    return gender_map.get(normalized, gender)


def get_search_keyboard(profile_id: int) -> InlineKeyboardMarkup:
    """Клавиатура для реакции на анкету"""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="❤️ Лайк", callback_data=f"rate:like:{profile_id}"),
                InlineKeyboardButton(text="⏭ Пропустить", callback_data=f"rate:pass:{profile_id}"),
            ],
            [InlineKeyboardButton(text="🏠 Меню", callback_data="main_menu")],
        ]
    )


async def send_next_candidate(message: Message, telegram_id: int) -> bool:
    """Показ следующей анкеты в поиске"""
    user_data = await get_user_from_profile_service(telegram_id)
    if not user_data:
        await message.answer("Сначала нажмите /start для регистрации.")
        return False

    candidate = await get_next_discovery_profile(user_data["id"])
    if not candidate:
        await message.answer(
            "Пока подходящие анкеты закончились. Попробуйте позже, когда появятся новые взаимодействия."
        )
        return False

    rating = candidate.get("rating", {})
    await message.answer(
        "💘 <b>Найдена анкета</b>\n\n"
        f"Имя: {candidate.get('first_name') or 'Не указано'}\n"
        f"Возраст: {candidate.get('age', 'Не указан')}\n"
        f"Пол: {format_gender(candidate.get('gender'))}\n"
        f"Город: {candidate.get('city', 'Не указан')}\n"
        f"О себе: {candidate.get('bio') or 'Не указано'}\n\n"
        f"Рейтинг анкеты: {rating.get('combined_score', 0):.1f}\n"
        f"(первичный: {rating.get('primary_score', 0):.1f}, поведенческий: {rating.get('behavioral_score', 0):.1f})",
        parse_mode=ParseMode.HTML,
        reply_markup=get_search_keyboard(candidate["profile_id"]),
    )
    return True


def get_main_keyboard() -> InlineKeyboardMarkup:
    """Главное меню бота"""
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 Заполнить анкету", callback_data="fill_profile")],
        [InlineKeyboardButton(text="👤 Моя анкета", callback_data="my_profile")],
        [InlineKeyboardButton(text="💞 Мои мэтчи", callback_data="matches")],
        [InlineKeyboardButton(text="🔍 Поиск пары", callback_data="search")],
    ])
    return keyboard


async def setup_bot_commands(bot: Bot) -> None:
    """Настройка команд для кнопки меню Telegram."""
    commands = [
        BotCommand(command="start", description="Запустить бота"),
        BotCommand(command="help", description="Справка по командам"),
        BotCommand(command="fill", description="Заполнить анкету"),
        BotCommand(command="profile", description="Моя анкета"),
        BotCommand(command="matches", description="Показать мои мэтчи"),
        BotCommand(command="search", description="Поиск пары"),
    ]
    await bot.set_my_commands(commands)

@router.message(CommandStart())
async def cmd_start(message: Message):
    """
    Обработка команды /start
    Регистрация пользователя и показ главного меню
    """
    start_time = asyncio.get_event_loop().time()
    logger.info("command_start", user_id=message.from_user.id, username=message.from_user.username)

    user_data = await register_user_in_profile_service(
        telegram_id=message.from_user.id,
        username=message.from_user.username,
        first_name=None,
    )
    
    if user_data:
        await message.answer(
            "👋 Привет!\n\n"
            f"Добро пожаловать в Dating Bot! 💕\n\n"
            f"Я помогу тебе найти идеальную пару.\n"
            f"Заполни анкету, чтобы начать поиск!\n\n"
            f"Твой ID: <code>{message.from_user.id}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=get_main_keyboard(),
        )
    else:
        await message.answer(
            "👋 Привет!\n\n"
            f"Добро пожаловать! К сожалению, сейчас возникли технические неполадки.\n"
            f"Попробуй позже! 🔧",
        )

    messages_processed.labels(service="bot_service", message_type="command_start").inc()
    duration = asyncio.get_event_loop().time() - start_time
    message_processing_duration.labels(service="bot_service", message_type="command_start").observe(duration)


@router.message(Command("help"))
async def cmd_help(message: Message):
    """Справка по командам бота"""
    logger.info("command_help", user_id=message.from_user.id)
    
    await message.answer(
        "📚 <b>Справка по командам:</b>\n\n"
        "/start - Запустить бота\n"
        "/help - Показать эту справку\n"
        "/profile - Моя анкета\n"
        "/matches - Мои мэтчи\n"
        "/search - Начать поиск\n\n"
        "Или используй кнопки в меню! 👇",
        parse_mode=ParseMode.HTML,
    )
    
    messages_processed.labels(service="bot_service", message_type="command_help").inc()


@router.message(Command("profile"))
async def cmd_profile(message: Message):
    """Показать профиль пользователя"""
    await cmd_profile_with_telegram_id(message, message.from_user.id)


async def cmd_profile_with_telegram_id(message: Message, telegram_id: int):
    """Показать профиль пользователя по telegram_id"""
    logger.info("cmd_profile_with_telegram_id", telegram_id=telegram_id)
    
    try:
        user_data = await get_user_from_profile_service(telegram_id)
        logger.info("got_user_data", user_data=user_data)

        if not user_data:
            logger.warning("user_not_found", telegram_id=telegram_id)
            await message.answer(" Профиль не найден. Нажмите /start для регистрации.")
            return

        profile_data = await get_profile_from_profile_service(user_data["id"])
        logger.info("got_profile_data", profile_data=profile_data)

        if profile_data:
            completeness = float(profile_data.get("completeness_score", 0)) * 100
            await message.answer(
                f"📝 <b>Ваша анкета:</b>\n\n"
                f"Заполненность: {completeness:.0f}%\n"
                f"Имя: {user_data.get('first_name') or 'Не указано'}\n"
                f"Возраст: {profile_data.get('age', 'Не указан')}\n"
                f"Пол: {format_gender(profile_data.get('gender'))}\n"
                f"Город: {profile_data.get('city', 'Не указан')}\n"
                f"О себе: {profile_data.get('bio', 'Не указано') or 'Не указано'}\n",
                parse_mode=ParseMode.HTML,
            )
        else:
            await message.answer(
                "📝 Ваша анкета ещё не заполнена.\n"
                "Нажмите /fill или кнопку '📝 Заполнить анкету'.",
                parse_mode=ParseMode.HTML,
            )
    except Exception as e:
        logger.error("cmd_profile_error", error=str(e), exc_info=True)
        await message.answer(f" Ошибка загрузки профиля: {str(e)}")

    messages_processed.labels(service="bot_service", message_type="command_profile").inc()


@router.message(Command("fill"))
async def cmd_fill_profile(message: Message, state: FSMContext):
    """Начать заполнение анкеты"""
    logger.info("command_fill_profile", user_id=message.from_user.id)
    
    await state.clear()
    await state.set_state(ProfileStates.waiting_for_name)
    
    await message.answer(
        "📝 <b>Заполнение анкеты</b>\n\n"
        "Как вас зовут?\n"
        "(введите имя, как хотите показываться в анкете)",
        parse_mode=ParseMode.HTML,
    )
    
    messages_processed.labels(service="bot_service", message_type="command_fill").inc()


@router.message(ProfileStates.waiting_for_name)
async def process_name(message: Message, state: FSMContext):
    """Обработка имени"""
    first_name = (message.text or "").strip()
    if not first_name:
        await message.answer("Пожалуйста, введите имя текстом.")
        return

    if not await update_user_name_in_profile_service(message.from_user.id, first_name):
        await message.answer("⚠️ Не удалось сохранить имя, попробуйте еще раз.")
        return

    await state.set_state(ProfileStates.waiting_for_bio)
    await message.answer(
        "Расскажите немного о себе (2-3 предложения):\n"
        "(или отправьте 'пропустить')",
        parse_mode=ParseMode.HTML,
    )


@router.message(Command("search"))
async def cmd_search(message: Message):
    """Запустить поиск анкеты"""
    logger.info("command_search", user_id=message.from_user.id)
    await send_next_candidate(message, message.from_user.id)


@router.message(Command("matches"))
async def cmd_matches(message: Message):
    """Показать список мэтчей пользователя"""
    await cmd_matches_with_telegram_id(message, message.from_user.id)


async def cmd_matches_with_telegram_id(message: Message, telegram_id: int):
    """Показать список мэтчей по telegram_id"""
    user_data = await get_user_from_profile_service(telegram_id)
    if not user_data:
        await message.answer("Сначала нажмите /start для регистрации.")
        return

    matches = await get_matches_from_profile_service(user_data["id"])
    if matches is None:
        await message.answer("⚠️ Не удалось загрузить мэтчи, попробуйте позже.")
        return
    if not matches:
        await message.answer("Пока мэтчей нет. Продолжайте ставить лайки в поиске 💘")
        return

    lines = ["💞 <b>Ваши мэтчи:</b>\n"]
    for idx, match in enumerate(matches, start=1):
        lines.append(
            f"{idx}. {match.get('first_name') or 'Без имени'}, "
            f"{match.get('age', 'возраст не указан')} — {match.get('city') or 'город не указан'}"
        )
    await message.answer("\n".join(lines), parse_mode=ParseMode.HTML)


def is_skip_command(text: str) -> bool:
    """Проверяет, является ли текст командой пропуска"""
    if not text:
        return False
    skip_variants = ["пропустить", "пропуск", "skip", "skipped", "дальше", "далее", "нет", "-", ""]
    return text.strip().lower() in skip_variants


@router.message(ProfileStates.waiting_for_bio)
async def process_bio(message: Message, state: FSMContext):
    """Обработка био"""
    bio = message.text.strip()

    await state.update_data(bio=None if is_skip_command(bio) else bio)
    await state.set_state(ProfileStates.waiting_for_age)
    
    await message.answer(
        "Сколько вам лет? (18+)\n"
        "(отправьте число)"
    )


@router.message(ProfileStates.waiting_for_age)
async def process_age(message: Message, state: FSMContext):
    """Обработка возраста"""
    try:
        age = int(message.text)
        if age < 18:
            await message.answer(" Вам должно быть минимум 18 лет. Попробуйте снова:")
            return
    except ValueError:
        await message.answer(" Пожалуйста, отправьте число:")
        return
    
    await state.update_data(age=age)
    await state.set_state(ProfileStates.waiting_for_gender)
    
    await message.answer(
        "Ваш пол:\n"
        "1. Мужской\n"
        "2. Женский\n"
        "3. Другой\n\n"
        "(отправьте номер варианта)"
    )


@router.message(ProfileStates.waiting_for_gender)
async def process_gender(message: Message, state: FSMContext):
    """Обработка пола"""
    gender_map = {"1": "мужской", "2": "женский", "3": "другой"}
    gender = gender_map.get(message.text.strip())
    
    if not gender:
        await message.answer(" Выберите 1, 2 или 3:")
        return
    
    await state.update_data(gender=gender)
    await state.set_state(ProfileStates.waiting_for_city)
    
    await message.answer(
        "Ваш город:\n"
        "(или 'пропустить')"
    )


@router.message(ProfileStates.waiting_for_city)
async def process_city(message: Message, state: FSMContext):
    """Обработка города"""
    city = message.text.strip()
    city = None if is_skip_command(city) else city

    await state.update_data(city=city)

    data = await state.get_data()
    logger.info("saving_profile", telegram_id=message.from_user.id, data=data)

    user_data = await get_user_from_profile_service(message.from_user.id)
    logger.info("got_user_data", user_data=user_data)

    if user_data:
        url = f"http://{settings.profile_service_host}:{settings.profile_service_port}/api/v1/profiles/{user_data['id']}"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.put(url, json=data)
                logger.info("profile_save_response", status=response.status_code, text=response.text[:200])
                if response.status_code != 200:
                    logger.error("failed_to_save_profile", status=response.status_code, text=response.text)
                    await message.answer(f"⚠️ Ошибка сохранения: {response.status_code}")
        except Exception as e:
            logger.error("failed_to_save_profile", error=str(e))
            await message.answer(f"⚠️ Ошибка сети: {str(e)}")
    else:
        logger.error("user_not_found_for_profile_save", telegram_id=message.from_user.id)
        await message.answer("⚠️ Пользователь не найден. Нажмите /start")

    await state.clear()

    await message.answer(
        "✅ Анкета заполнена!\n\n"
        "Теперь вы можете начать поиск пары! 💕\n"
        "Нажмите /search или используйте кнопку в меню.",
        parse_mode=ParseMode.HTML,
    )

    messages_processed.labels(service="bot_service", message_type="profile_filled").inc()


@router.callback_query(F.data == "my_profile")
async def cb_my_profile(callback: CallbackQuery):
    """Показать профиль"""
    logger.info("callback_my_profile", callback_user_id=callback.from_user.id)
    
    try:
        await callback.answer("Загружаю анкету...")

        await cmd_profile_with_telegram_id(callback.message, callback.from_user.id)
    except Exception as e:
        logger.error("callback_my_profile_error", error=str(e))
        await callback.message.answer(f" Ошибка: {str(e)}")


@router.callback_query(F.data == "fill_profile")
async def cb_fill_profile(callback: CallbackQuery, state: FSMContext):
    """Заполнить анкету"""
    logger.info("callback_fill_profile", user_id=callback.from_user.id)
    await callback.answer()
    await cmd_fill_profile(callback.message, state)


@router.callback_query(F.data == "search")
async def cb_search(callback: CallbackQuery):
    """Начать поиск"""
    logger.info("callback_search", user_id=callback.from_user.id)

    user_data = await get_user_from_profile_service(callback.from_user.id)
    if not user_data:
        await callback.answer("Сначала нажмите /start!", show_alert=True)
        return

    profile_data = await get_profile_from_profile_service(user_data["id"])

    if not profile_data or not profile_data.get("age"):
        await callback.answer("Сначала заполните анкету! Нажмите '📝 Заполнить анкету'", show_alert=True)
        return

    await callback.message.answer("🔍 Ищу анкету для вас...")
    await send_next_candidate(callback.message, callback.from_user.id)
    await callback.answer()


@router.callback_query(F.data == "matches")
async def cb_matches(callback: CallbackQuery):
    """Показать мэтчи через кнопку меню"""
    await callback.answer("Загружаю мэтчи...")
    await cmd_matches_with_telegram_id(callback.message, callback.from_user.id)


@router.callback_query(F.data.startswith("rate:"))
async def cb_rate_profile(callback: CallbackQuery):
    """Обработка лайка/пропуска и выдача следующей анкеты"""
    parts = (callback.data or "").split(":")
    if len(parts) != 3:
        await callback.answer("Некорректные данные", show_alert=True)
        return

    _, action, profile_id_raw = parts
    if action not in {"like", "pass"}:
        await callback.answer("Неизвестное действие", show_alert=True)
        return

    try:
        profile_id = int(profile_id_raw)
    except ValueError:
        await callback.answer("Некорректный id анкеты", show_alert=True)
        return

    user_data = await get_user_from_profile_service(callback.from_user.id)
    if not user_data:
        await callback.answer("Сначала нажмите /start", show_alert=True)
        return

    interaction_result = await send_interaction_to_profile_service(
        actor_user_id=user_data["id"],
        target_profile_id=profile_id,
        action=action,
    )
    if not interaction_result:
        await callback.answer("Ошибка отправки реакции", show_alert=True)
        return

    if interaction_result.get("is_match"):
        await callback.message.answer("🎉 Взаимный лайк! У вас мэтч!")
    else:
        await callback.answer("Реакция сохранена")

    await send_next_candidate(callback.message, callback.from_user.id)


@router.callback_query(F.data == "settings")
async def cb_settings(callback: CallbackQuery):
    """Настройки"""
    logger.info("callback_settings", user_id=callback.from_user.id)
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back")],
    ])
    
    await callback.message.answer(
        "⚙️ <b>Настройки</b>\n\n"
        "Здесь вы сможете настроить предпочтения для поиска.\n"
        "(Функционал в разработке)",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )
    await callback.answer()


@router.callback_query(F.data == "back")
async def cb_back(callback: CallbackQuery):
    """Назад в главное меню"""
    await callback.message.edit_reply_markup(reply_markup=get_main_keyboard())
    await callback.answer()


@router.callback_query(F.data == "main_menu")
async def cb_main_menu(callback: CallbackQuery):
    """Показать главное меню отдельной кнопкой"""
    await callback.message.answer("🏠 Главное меню:", reply_markup=get_main_keyboard())
    await callback.answer()




async def main():
    """Запуск бота"""
    if not settings.bot_token:
        logger.error("BOT_TOKEN not set in environment!")
        print("❌ Ошибка: BOT_TOKEN не установлен в переменной окружения!")
        print("Создайте .env файл с BOT_TOKEN=ваш_токен_бота")
        sys.exit(1)
    
    # Создание бота и диспетчера
    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    
    # Регистрация роутера
    dp.include_router(router)
    await setup_bot_commands(bot)
    
    logger.info("bot_starting", bot_id=(await bot.get_me()).id)
    
    # Запуск polling
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("bot_stopped")
