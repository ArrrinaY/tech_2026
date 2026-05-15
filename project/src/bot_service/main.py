"""
Bot Service - Telegram бот для дейтинг-приложения
"""
import asyncio
import sys
from html import escape
from typing import Optional
import time

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
    BufferedInputFile,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramNetworkError
import httpx

from common.logging_config import setup_logging, get_logger
from common.metrics import messages_processed, message_processing_duration
from common.config import get_settings

# Настройка логирования
setup_logging("bot_service")
logger = get_logger("bot_service")

settings = get_settings()
PROFILE_SERVICE_BASE_URL = f"http://{settings.profile_service_host}:{settings.profile_service_port}"
HTTP_TIMEOUT = httpx.Timeout(connect=3.0, read=10.0, write=10.0, pool=5.0)
HTTP_LIMITS = httpx.Limits(max_connections=100, max_keepalive_connections=20)
http_client: Optional[httpx.AsyncClient] = None
USER_CACHE_TTL_SECONDS = 60.0
SEARCH_RATE_LIMIT_SECONDS = 1.2
RATE_DEDUP_TTL_SECONDS = 3.0
user_lookup_cache: dict[int, tuple[float, dict]] = {}
last_search_action_ts: dict[int, float] = {}
recent_rate_actions: dict[str, float] = {}
user_action_locks: dict[int, asyncio.Lock] = {}


def get_http_client() -> httpx.AsyncClient:
    global http_client
    if http_client is None or http_client.is_closed:
        http_client = httpx.AsyncClient(timeout=HTTP_TIMEOUT, limits=HTTP_LIMITS)
    return http_client


async def close_http_client() -> None:
    global http_client
    if http_client is not None and not http_client.is_closed:
        await http_client.aclose()
    http_client = None


def _cache_get_user(telegram_id: int) -> dict | None:
    cached = user_lookup_cache.get(telegram_id)
    if not cached:
        return None
    ts, payload = cached
    if time.monotonic() - ts > USER_CACHE_TTL_SECONDS:
        user_lookup_cache.pop(telegram_id, None)
        return None
    return payload


def _cache_put_user(telegram_id: int, payload: dict) -> None:
    user_lookup_cache[telegram_id] = (time.monotonic(), payload)


def should_rate_limit_search_action(telegram_id: int) -> bool:
    now = time.monotonic()
    last_ts = last_search_action_ts.get(telegram_id)
    if last_ts is not None and now - last_ts < SEARCH_RATE_LIMIT_SECONDS:
        return True
    last_search_action_ts[telegram_id] = now
    return False


def is_duplicate_rate_action(telegram_id: int, profile_id: int, action: str) -> bool:
    now = time.monotonic()
    action_key = f"{telegram_id}:{profile_id}:{action}"

    # Opportunistic cleanup of stale dedup entries.
    stale_keys = [key for key, ts in recent_rate_actions.items() if now - ts > RATE_DEDUP_TTL_SECONDS]
    for key in stale_keys:
        recent_rate_actions.pop(key, None)

    existing_ts = recent_rate_actions.get(action_key)
    if existing_ts is not None and now - existing_ts <= RATE_DEDUP_TTL_SECONDS:
        return True

    recent_rate_actions[action_key] = now
    return False


def get_user_action_lock(telegram_id: int) -> asyncio.Lock:
    lock = user_action_locks.get(telegram_id)
    if lock is None:
        lock = asyncio.Lock()
        user_action_locks[telegram_id] = lock
    return lock


def prioritize_photo_urls(photo_urls: list[str]) -> list[str]:
    minio_host = settings.minio_endpoint

    def sort_key(url: str) -> tuple[int, int]:
        # Prefer MinIO URLs first, then everything else.
        return (0 if minio_host in url else 1, len(url))

    return sorted(photo_urls, key=sort_key)


async def send_photo_caption_or_text(
    message: Message,
    photo_urls: list[str],
    caption_text: str,
    telegram_id: int,
    log_prefix: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    prioritized = prioritize_photo_urls(photo_urls)
    for idx, photo_url in enumerate(prioritized):
        try:
            response = await get_http_client().get(photo_url)
            if response.status_code != 200:
                logger.warning(
                    f"{log_prefix}_photo_unavailable",
                    telegram_id=telegram_id,
                    photo_url=photo_url,
                    status=response.status_code,
                )
                continue
            photo = BufferedInputFile(response.content, filename=f"{log_prefix}_{telegram_id}_{idx}.jpg")
            await message.answer_photo(
                photo=photo,
                caption=caption_text,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
            )
            return
        except Exception as exc:
            logger.warning(
                f"{log_prefix}_photo_send_failed",
                telegram_id=telegram_id,
                photo_url=photo_url,
                error=str(exc),
            )
    await message.answer(caption_text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)

# Роутеры
router = Router()

# FSM состояния для регистрации анкеты
class ProfileStates(StatesGroup):
    waiting_for_name = State()
    waiting_for_bio = State()
    waiting_for_age = State()
    waiting_for_gender = State()
    waiting_for_partner_gender = State()
    waiting_for_age_pref = State()
    waiting_for_city = State()
    waiting_for_city_pref = State()
    waiting_for_photo = State()


# Вспомогательные функции

async def register_user_in_profile_service(
    telegram_id: int,
    username: str | None,
    first_name: str | None
) -> dict | None:
    """Регистрация пользователя в Profile Service"""
    url = f"{PROFILE_SERVICE_BASE_URL}/api/v1/users/register"
    
    try:
        response = await get_http_client().post(
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
        if response.status_code == 409:
            logger.info("user_already_exists", telegram_id=telegram_id)
            return response.json()
        logger.error("profile_service_error", status=response.status_code, body=response.text)
        return None
                
    except httpx.RequestError as e:
        logger.error("profile_service_unavailable", error=str(e))
        return None


async def update_user_name_in_profile_service(telegram_id: int, first_name: str) -> bool:
    """Обновление имени пользователя в Profile Service"""
    url = f"{PROFILE_SERVICE_BASE_URL}/api/v1/users/{telegram_id}"
    try:
        response = await get_http_client().put(url, json={"first_name": first_name})
        if response.status_code == 200:
            return True
        logger.error("update_user_name_failed", status=response.status_code, body=response.text)
        return False
    except httpx.RequestError as exc:
        logger.error("update_user_name_error", error=str(exc))
        return False


async def get_user_from_profile_service(telegram_id: int) -> dict | None:
    """Получение данных пользователя из Profile Service"""
    cached = _cache_get_user(telegram_id)
    if cached is not None:
        return cached

    url = f"{PROFILE_SERVICE_BASE_URL}/api/v1/users/{telegram_id}"

    try:
        response = await get_http_client().get(url)
        if response.status_code == 200:
            payload = response.json()
            _cache_put_user(telegram_id, payload)
            return payload
        return None
    except httpx.RequestError as e:
        logger.error("get_user_error", error=str(e))
        return None


async def get_profile_from_profile_service(user_id: int) -> dict | None:
    """Получение анкеты из Profile Service"""
    url = f"{PROFILE_SERVICE_BASE_URL}/api/v1/profiles/{user_id}"
    
    try:
        response = await get_http_client().get(url)
        if response.status_code == 200:
            return response.json()
        return None
    except httpx.RequestError:
        return None


async def get_next_discovery_profile(user_id: int) -> dict | None:
    """Получение следующей анкеты для показа пользователю"""
    url = f"{PROFILE_SERVICE_BASE_URL}/api/v1/discovery/{user_id}/next"

    try:
        response = await get_http_client().get(url)
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
    url = f"{PROFILE_SERVICE_BASE_URL}/api/v1/interactions"
    payload = {
        "actor_user_id": actor_user_id,
        "target_profile_id": target_profile_id,
        "action": action,
    }
    try:
        response = await get_http_client().post(url, json=payload)
        if response.status_code == 200:
            return response.json()
        logger.error("interaction_failed", status=response.status_code, body=response.text)
        return None
    except httpx.RequestError as exc:
        logger.error("interaction_request_error", error=str(exc))
        return None


async def get_matches_from_profile_service(user_id: int) -> list[dict] | None:
    """Получение списка мэтчей пользователя"""
    url = f"{PROFILE_SERVICE_BASE_URL}/api/v1/matches/{user_id}"
    try:
        response = await get_http_client().get(url)
        if response.status_code == 200:
            return response.json()
        if response.status_code == 404:
            return []
        logger.error("get_matches_failed", status=response.status_code, body=response.text)
        return None
    except httpx.RequestError as exc:
        logger.error("get_matches_error", error=str(exc))
        return None


async def save_profile_data(user_id: int, data: dict) -> bool:
    """Сохраняет анкету пользователя в Profile Service."""
    url = f"{PROFILE_SERVICE_BASE_URL}/api/v1/profiles/{user_id}"
    try:
        response = await get_http_client().put(url, json=data)
        if response.status_code == 200:
            return True
        logger.error("failed_to_save_profile", status=response.status_code, text=response.text)
        return False
    except Exception as exc:
        logger.error("failed_to_save_profile", error=str(exc))
        return False


async def save_preferences_in_profile_service(user_id: int, **fields) -> bool:
    """Сохраняет предпочтения поиска (передаются только изменяемые поля)."""
    url = f"{PROFILE_SERVICE_BASE_URL}/api/v1/preferences"
    payload = {"user_id": user_id, **fields}
    try:
        response = await get_http_client().post(url, json=payload)
        if response.status_code == 200:
            return True
        logger.error("failed_to_save_preferences", status=response.status_code, text=response.text)
        return False
    except Exception as exc:
        logger.error("failed_to_save_preferences", error=str(exc))
        return False


def get_own_gender_keyboard() -> InlineKeyboardMarkup:
    """Кнопки выбора своего пола."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="👨 Мужской", callback_data="own_gender:мужской"),
                InlineKeyboardButton(text="👩 Женский", callback_data="own_gender:женский"),
            ],
            [InlineKeyboardButton(text="🧑 Другой", callback_data="own_gender:другой")],
        ]
    )


def get_partner_gender_keyboard() -> InlineKeyboardMarkup:
    """Кнопки выбора, кого искать в поиске."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="👨 Мужчин", callback_data="pref_gender:мужской"),
                InlineKeyboardButton(text="👩 Женщин", callback_data="pref_gender:женский"),
            ],
            [InlineKeyboardButton(text="👥 Всех", callback_data="pref_gender:any")],
        ]
    )


PARTNER_GENDER_LABELS = {
    "мужской": "мужчин",
    "женский": "женщин",
    "any": "всех",
}

AGE_RANGE_PRESETS: dict[str, tuple[int, int]] = {
    "18-25": (18, 25),
    "26-35": (26, 35),
    "36-45": (36, 45),
    "46+": (46, 99),
    "any": (18, 99),
}

AGE_RANGE_LABELS = {
    "18-25": "18–25 лет",
    "26-35": "26–35 лет",
    "36-45": "36–45 лет",
    "46+": "46+ лет",
    "any": "любой возраст (18–99)",
}


def get_age_pref_keyboard(*, callback_prefix: str = "pref_age") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="18–25", callback_data=f"{callback_prefix}:18-25"),
                InlineKeyboardButton(text="26–35", callback_data=f"{callback_prefix}:26-35"),
            ],
            [
                InlineKeyboardButton(text="36–45", callback_data=f"{callback_prefix}:36-45"),
                InlineKeyboardButton(text="46+", callback_data=f"{callback_prefix}:46+"),
            ],
            [InlineKeyboardButton(text="🎂 Любой возраст", callback_data=f"{callback_prefix}:any")],
        ]
    )


def get_city_pref_keyboard(profile_city: str | None, *, callback_prefix: str = "pref_city") -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if profile_city:
        rows.append([
            InlineKeyboardButton(
                text=f"📍 Только {profile_city}",
                callback_data=f"{callback_prefix}:mine",
            )
        ])
    rows.append([InlineKeyboardButton(text="🌍 Любой город", callback_data=f"{callback_prefix}:any")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_settings_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💕 Кого ищу", callback_data="settings:gender")],
            [InlineKeyboardButton(text="🎂 Возраст партнёра", callback_data="settings:age")],
            [InlineKeyboardButton(text="📍 Город в поиске", callback_data="settings:city")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="back")],
        ]
    )


async def prompt_age_pref(message: Message, state: FSMContext) -> None:
    await state.set_state(ProfileStates.waiting_for_age_pref)
    await message.answer(
        "🎂 <b>Возраст в поиске</b>\n\n"
        "Выберите возрастной диапазон анкет:",
        parse_mode=ParseMode.HTML,
        reply_markup=get_age_pref_keyboard(),
    )


async def prompt_city_pref(message: Message, state: FSMContext, profile_city: str | None) -> None:
    await state.set_state(ProfileStates.waiting_for_city_pref)
    hint = (
        f"Можно искать только в городе <b>{escape(profile_city)}</b> или по всем городам."
        if profile_city
        else "Укажите, ограничивать ли поиск вашим городом."
    )
    await message.answer(
        f"📍 <b>Город в поиске</b>\n\n{hint}",
        parse_mode=ParseMode.HTML,
        reply_markup=get_city_pref_keyboard(profile_city),
    )


async def apply_age_pref_choice(
    user_id: int,
    choice: str,
    state: FSMContext | None = None,
) -> tuple[bool, str]:
    if choice not in AGE_RANGE_PRESETS:
        return False, ""
    age_min, age_max = AGE_RANGE_PRESETS[choice]
    if state is not None:
        await state.update_data(age_min=age_min, age_max=age_max)
    ok = await save_preferences_in_profile_service(user_id, age_min=age_min, age_max=age_max)
    return ok, AGE_RANGE_LABELS[choice]


async def apply_city_pref_choice(
    user_id: int,
    choice: str,
    profile_city: str | None,
    state: FSMContext | None = None,
) -> tuple[bool, str]:
    if choice not in {"mine", "any"}:
        return False, ""
    city_pref = profile_city if choice == "mine" and profile_city else None
    if choice == "mine" and not profile_city:
        return False, ""
    if state is not None:
        await state.update_data(city_pref=city_pref)
    ok = await save_preferences_in_profile_service(user_id, city_pref=city_pref)
    label = f"только {profile_city}" if city_pref else "любой город"
    return ok, label


async def prompt_partner_gender(message: Message, state: FSMContext) -> None:
    await state.set_state(ProfileStates.waiting_for_partner_gender)
    await message.answer(
        "💕 <b>Кого вы ищете?</b>\n\n"
        "Выберите, какие анкеты показывать в поиске:",
        parse_mode=ParseMode.HTML,
        reply_markup=get_partner_gender_keyboard(),
    )


async def upload_profile_photo_to_profile_service(
    user_id: int,
    file_name: str,
    content: bytes,
    content_type: str = "image/jpeg",
) -> bool:
    """Загружает фото пользователя в MinIO через Profile Service."""
    url = f"{PROFILE_SERVICE_BASE_URL}/api/v1/profiles/{user_id}/photos"
    files = {"photo": (file_name, content, content_type)}
    try:
        response = await get_http_client().post(url, files=files)
        if response.status_code == 200:
            return True
        logger.error("failed_to_upload_photo", status=response.status_code, text=response.text)
        return False
    except Exception as exc:
        logger.error("failed_to_upload_photo", error=str(exc))
        return False


def format_user_chat_link(
    telegram_id: int | None,
    display_name: str,
    username: str | None = None,
) -> str:
    """HTML-ссылка на чат с пользователем в Telegram."""
    safe_name = escape(display_name or "Новый знакомый")
    if username:
        uname = username.lstrip("@")
        if uname.replace("_", "").isalnum():
            return f'<a href="https://t.me/{uname}">{safe_name}</a>'
    if telegram_id is not None:
        return f'<a href="tg://user?id={telegram_id}">{safe_name}</a>'
    return safe_name


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


def gender_emoji(gender: str | None) -> str:
    normalized = (gender or "").strip().lower()
    if normalized in {"female", "женский", "ж"}:
        return "👩"
    if normalized in {"male", "мужской", "м"}:
        return "👨"
    return "🧑"


def format_progress_bar(percent: float, width: int = 10) -> str:
    percent = max(0.0, min(100.0, percent))
    filled = int(round(percent / 100 * width))
    return "▰" * filled + "▱" * (width - filled)


def normalize_rating_for_display(score: float) -> float:
    """В БД рейтинг 0–100, в боте показываем шкалу 0–10."""
    value = max(0.0, float(score or 0))
    if value <= 10:
        return round(value, 1)
    return round(value / 10, 1)


def format_score_stars(score_on_10: float) -> str:
    score_on_10 = max(0.0, min(10.0, score_on_10))
    filled = int(round(score_on_10 / 10 * 5))
    filled = max(0, min(5, filled))
    return "★" * filled + "☆" * (5 - filled)


def _display_value(value: str | int | None, *, empty: str = "—") -> str:
    if value is None:
        return empty
    text = str(value).strip()
    return escape(text) if text else empty


def _format_age(age: str | int | None) -> str:
    if age is None:
        return "возраст не указан"
    if isinstance(age, int):
        return f"{age} лет"
    text = str(age).strip()
    if not text:
        return "возраст не указан"
    if text.isdigit():
        return f"{text} лет"
    return escape(text)


def format_profile_card(
    *,
    title: str,
    first_name: str,
    age: str | int | None,
    gender: str | None,
    city: str | None,
    bio: str | None,
    completeness: float | None = None,
    rating: dict | None = None,
    show_rating_details: bool = False,
) -> str:
    """Красивое HTML-оформление анкеты для Telegram."""
    name = escape(first_name or "Без имени")
    age_text = _format_age(age)
    city_text = _display_value(city, empty="город не указан")
    gender_text = escape(format_gender(gender))
    g_emoji = gender_emoji(gender)
    bio_raw = (bio or "").strip()
    bio_text = escape(bio_raw) if bio_raw else "<i>пока ничего не написано</i>"

    lines = [
        f"{title}",
        "━━━━━━━━━━━━━━━━",
        f"👤 <b>{name}</b>",
        f"🎂 {age_text}  ·  {g_emoji} {gender_text}",
        f"📍 {city_text}",
        "━━━━━━━━━━━━━━━━",
        f"💬 <b>О себе</b>",
        bio_text,
    ]

    if completeness is not None:
        pct = max(0.0, min(100.0, completeness))
        bar = format_progress_bar(pct)
        lines.extend([
            "━━━━━━━━━━━━━━━━",
            f"📊 <b>Заполненность:</b> {pct:.0f}%",
            f"<code>{bar}</code>",
        ])

    if rating and show_rating_details:
        combined_raw = float(rating.get("combined_score") or 0)
        primary = float(rating.get("primary_score") or 0)
        behavioral = float(rating.get("behavioral_score") or 0)
        combined_10 = normalize_rating_for_display(combined_raw)
        stars = format_score_stars(combined_10)
        lines.extend([
            "━━━━━━━━━━━━━━━━",
            f"⭐ <b>Рейтинг анкеты:</b> {combined_10:.1f}/10  {stars}",
            f"<i>заполненность {primary:.0f}/100 · отклики {behavioral:.0f}/100</i>",
        ])
    elif rating:
        combined_raw = float(rating.get("combined_score") or 0)
        combined_10 = normalize_rating_for_display(combined_raw)
        stars = format_score_stars(combined_10)
        lines.extend([
            "━━━━━━━━━━━━━━━━",
            f"⭐ <b>Рейтинг анкеты:</b> {combined_10:.1f}/10  {stars}",
        ])

    return "\n".join(lines)


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

    candidate_text = format_profile_card(
        title="💘 <b>Новая анкета</b>",
        first_name=str(candidate.get("first_name") or "Без имени"),
        age=candidate.get("age"),
        gender=candidate.get("gender"),
        city=candidate.get("city"),
        bio=candidate.get("bio"),
        rating=candidate.get("rating"),
        show_rating_details=False,
    )
    keyboard = get_search_keyboard(candidate["profile_id"])

    photo_urls = candidate.get("photo_urls") or []
    await send_photo_caption_or_text(
        message=message,
        photo_urls=photo_urls,
        caption_text=candidate_text,
        telegram_id=telegram_id,
        log_prefix="candidate",
        reply_markup=keyboard,
    )
    return True


def get_main_keyboard() -> InlineKeyboardMarkup:
    """Главное меню бота"""
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 Заполнить анкету", callback_data="fill_profile")],
        [InlineKeyboardButton(text="👤 Моя анкета", callback_data="my_profile")],
        [InlineKeyboardButton(text="💞 Мои мэтчи", callback_data="matches")],
        [InlineKeyboardButton(text="🔍 Поиск пары", callback_data="search")],
        [InlineKeyboardButton(text="⚙️ Настройки поиска", callback_data="settings")],
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
        if not user_data:
            logger.warning("user_not_found", telegram_id=telegram_id)
            await message.answer(" Профиль не найден. Нажмите /start для регистрации.")
            return

        profile_data = await get_profile_from_profile_service(user_data["id"])

        if profile_data:
            completeness = float(profile_data.get("completeness_score", 0)) * 100
            profile_text = format_profile_card(
                title="✨ <b>Ваша анкета</b>",
                first_name=str(user_data.get("first_name") or "Без имени"),
                age=profile_data.get("age"),
                gender=profile_data.get("gender"),
                city=profile_data.get("city"),
                bio=profile_data.get("bio"),
                completeness=completeness,
            )

            photo_urls = profile_data.get("photo_urls") or []
            await send_photo_caption_or_text(
                message=message,
                photo_urls=photo_urls,
                caption_text=profile_text,
                telegram_id=telegram_id,
                log_prefix="profile",
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
    if should_rate_limit_search_action(message.from_user.id):
        await message.answer("Слишком быстро. Подождите секунду.")
        return
    async with get_user_action_lock(message.from_user.id):
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

    lines = ["💞 <b>Ваши мэтчи</b>", "━━━━━━━━━━━━━━━━", ""]
    for idx, match in enumerate(matches, start=1):
        name = format_user_chat_link(
            match.get("telegram_id"),
            match.get("first_name") or "Без имени",
            match.get("username"),
        )
        age = match.get("age", "—")
        city = escape(str(match.get("city") or "город не указан"))
        g_emoji = gender_emoji(match.get("gender"))
        lines.append(f"<b>{idx}.</b> {name}")
        lines.append(f"   {g_emoji} {age} лет  ·  📍 {city}")
        lines.append("")
    await message.answer("\n".join(lines).strip(), parse_mode=ParseMode.HTML)


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
        "🪪 <b>Ваш пол</b>\n\n"
        "Выберите вариант на кнопках ниже:",
        parse_mode=ParseMode.HTML,
        reply_markup=get_own_gender_keyboard(),
    )


@router.callback_query(ProfileStates.waiting_for_gender, F.data.startswith("own_gender:"))
async def cb_own_gender(callback: CallbackQuery, state: FSMContext):
    """Выбор своего пола кнопкой."""
    gender = (callback.data or "").split(":", 1)[-1]
    if gender not in {"мужской", "женский", "другой"}:
        await callback.answer("Некорректный выбор", show_alert=True)
        return

    await state.update_data(gender=gender)
    await callback.answer("Сохранено")
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await prompt_partner_gender(callback.message, state)


@router.message(ProfileStates.waiting_for_gender)
async def process_gender(message: Message, state: FSMContext):
    """Обработка пола (текстом, если кнопки не нажали)."""
    gender_map = {"1": "мужской", "2": "женский", "3": "другой"}
    gender = gender_map.get((message.text or "").strip())

    if not gender:
        await message.answer(
            "Выберите пол на кнопках ниже или отправьте 1, 2 или 3:",
            reply_markup=get_own_gender_keyboard(),
        )
        return

    await state.update_data(gender=gender)
    await prompt_partner_gender(message, state)


@router.callback_query(ProfileStates.waiting_for_partner_gender, F.data.startswith("pref_gender:"))
async def cb_partner_gender(callback: CallbackQuery, state: FSMContext):
    """Выбор, кого показывать в поиске."""
    choice = (callback.data or "").split(":", 1)[-1]
    if choice not in PARTNER_GENDER_LABELS:
        await callback.answer("Некорректный выбор", show_alert=True)
        return

    gender_pref = None if choice == "any" else choice
    await state.update_data(partner_gender_pref=gender_pref)

    user_data = await get_user_from_profile_service(callback.from_user.id)
    if not user_data:
        await callback.answer("Сначала нажмите /start", show_alert=True)
        await state.clear()
        return

    if not await save_preferences_in_profile_service(user_data["id"], gender_pref=gender_pref):
        await callback.answer("Не удалось сохранить предпочтения", show_alert=True)
        return

    label = PARTNER_GENDER_LABELS[choice]
    await callback.answer(f"Ищем: {label}")
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    await callback.message.answer(f"✅ В поиске: <b>{label}</b>", parse_mode=ParseMode.HTML)
    await prompt_age_pref(callback.message, state)


@router.message(ProfileStates.waiting_for_partner_gender)
async def process_partner_gender_text(message: Message, state: FSMContext):
    """Напоминание выбрать кнопку «кого ищете»."""
    await message.answer(
        "Пожалуйста, выберите вариант на кнопках 👇",
        reply_markup=get_partner_gender_keyboard(),
    )


@router.callback_query(ProfileStates.waiting_for_age_pref, F.data.startswith("pref_age:"))
async def cb_age_pref_fill(callback: CallbackQuery, state: FSMContext):
    choice = (callback.data or "").split(":", 1)[-1]
    user_data = await get_user_from_profile_service(callback.from_user.id)
    if not user_data:
        await callback.answer("Сначала /start", show_alert=True)
        await state.clear()
        return

    ok, label = await apply_age_pref_choice(user_data["id"], choice, state)
    if not ok:
        await callback.answer("Некорректный выбор или ошибка сохранения", show_alert=True)
        return

    await callback.answer(label)
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    await state.set_state(ProfileStates.waiting_for_city)
    await callback.message.answer(
        f"✅ Возраст в поиске: <b>{escape(label)}</b>\n\n"
        "📍 <b>Ваш город</b> (для анкеты)\n"
        "Напишите город или «пропустить».",
        parse_mode=ParseMode.HTML,
    )


@router.message(ProfileStates.waiting_for_age_pref)
async def process_age_pref_text(message: Message, state: FSMContext):
    await message.answer("Выберите возраст на кнопках 👇", reply_markup=get_age_pref_keyboard())


@router.message(ProfileStates.waiting_for_city)
async def process_city(message: Message, state: FSMContext):
    """Обработка города"""
    city = (message.text or "").strip()
    city = None if is_skip_command(city) else city

    await state.update_data(city=city)
    await prompt_city_pref(message, state, city)


@router.callback_query(ProfileStates.waiting_for_city_pref, F.data.startswith("pref_city:"))
async def cb_city_pref_fill(callback: CallbackQuery, state: FSMContext):
    choice = (callback.data or "").split(":", 1)[-1]
    data = await state.get_data()
    profile_city = data.get("city")

    user_data = await get_user_from_profile_service(callback.from_user.id)
    if not user_data:
        await callback.answer("Сначала /start", show_alert=True)
        await state.clear()
        return

    ok, label = await apply_city_pref_choice(user_data["id"], choice, profile_city, state)
    if not ok:
        await callback.answer(
            "Укажите город в анкете или выберите «Любой город»",
            show_alert=True,
        )
        return

    await callback.answer(label)
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    await state.set_state(ProfileStates.waiting_for_photo)
    await callback.message.answer(
        f"✅ Город в поиске: <b>{escape(label)}</b>\n\n"
        "📷 Отправьте фото для анкеты одним сообщением.\n"
        "Или напишите «пропустить», чтобы завершить без фото.",
        parse_mode=ParseMode.HTML,
    )


@router.message(ProfileStates.waiting_for_city_pref)
async def process_city_pref_text(message: Message, state: FSMContext):
    data = await state.get_data()
    await message.answer(
        "Выберите вариант на кнопках 👇",
        reply_markup=get_city_pref_keyboard(data.get("city")),
    )


@router.message(ProfileStates.waiting_for_photo, F.photo)
async def process_photo(message: Message, state: FSMContext):
    """Загрузка фото в MinIO и завершение анкеты."""
    data = await state.get_data()
    logger.info("saving_profile_with_photo", telegram_id=message.from_user.id)

    user_data = await get_user_from_profile_service(message.from_user.id)
    if not user_data:
        await message.answer("⚠️ Пользователь не найден. Нажмите /start")
        await state.clear()
        return

    if not await save_profile_data(user_data["id"], data):
        await message.answer("⚠️ Не удалось сохранить анкету. Попробуйте позже.")
        await state.clear()
        return

    best_photo = message.photo[-1]
    file = await message.bot.get_file(best_photo.file_id)
    file_buffer = await message.bot.download_file(file.file_path)
    photo_bytes = file_buffer.read()

    uploaded = await upload_profile_photo_to_profile_service(
        user_id=user_data["id"],
        file_name=f"telegram_{best_photo.file_unique_id}.jpg",
        content=photo_bytes,
        content_type="image/jpeg",
    )

    await state.clear()
    if not uploaded:
        await message.answer("⚠️ Анкета сохранена, но фото не загрузилось в MinIO.")
    else:
        await message.answer("✅ Фото загружено в MinIO и добавлено в анкету.")

    await message.answer(
        "✅ Анкета заполнена!\n\n"
        "Теперь вы можете начать поиск пары! 💕\n"
        "Нажмите /search или используйте кнопку в меню.",
        parse_mode=ParseMode.HTML,
    )

    messages_processed.labels(service="bot_service", message_type="profile_filled").inc()


@router.message(ProfileStates.waiting_for_photo)
async def process_photo_skip(message: Message, state: FSMContext):
    """Пропуск шага загрузки фото и завершение анкеты."""
    text = (message.text or "").strip()
    if not is_skip_command(text):
        await message.answer("Отправьте фото или напишите 'пропустить'.")
        return

    data = await state.get_data()
    user_data = await get_user_from_profile_service(message.from_user.id)
    if not user_data:
        await message.answer("⚠️ Пользователь не найден. Нажмите /start")
        await state.clear()
        return

    if not await save_profile_data(user_data["id"], data):
        await message.answer("⚠️ Не удалось сохранить анкету. Попробуйте позже.")
        await state.clear()
        return

    await state.clear()
    await message.answer(
        "✅ Анкета заполнена без фото.\n\n"
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
    if should_rate_limit_search_action(callback.from_user.id):
        await callback.answer("Слишком быстро. Подождите секунду.", show_alert=False)
        return

    user_data = await get_user_from_profile_service(callback.from_user.id)
    if not user_data:
        await callback.answer("Сначала нажмите /start!", show_alert=True)
        return

    profile_data = await get_profile_from_profile_service(user_data["id"])

    if not profile_data or not profile_data.get("age"):
        await callback.answer("Сначала заполните анкету! Нажмите '📝 Заполнить анкету'", show_alert=True)
        return

    await callback.message.answer("🔍 Ищу анкету для вас...")
    async with get_user_action_lock(callback.from_user.id):
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
    if should_rate_limit_search_action(callback.from_user.id):
        await callback.answer("Слишком быстро. Подождите секунду.", show_alert=False)
        return

    await callback.answer("Сохраняю реакцию...")

    if action not in {"like", "pass"}:
        await callback.answer("Неизвестное действие", show_alert=True)
        return

    try:
        profile_id = int(profile_id_raw)
    except ValueError:
        await callback.answer("Некорректный id анкеты", show_alert=True)
        return

    if is_duplicate_rate_action(callback.from_user.id, profile_id, action):
        await callback.answer("Уже обработано", show_alert=False)
        return

    user_data = await get_user_from_profile_service(callback.from_user.id)
    if not user_data:
        await callback.answer("Сначала нажмите /start", show_alert=True)
        return

    async with get_user_action_lock(callback.from_user.id):
        interaction_result = await send_interaction_to_profile_service(
            actor_user_id=user_data["id"],
            target_profile_id=profile_id,
            action=action,
        )
        if not interaction_result:
            await callback.answer("Ошибка отправки реакции", show_alert=True)
            return

        if interaction_result.get("is_match"):
            partner = interaction_result.get("match_partner") or {}
            partner_link = format_user_chat_link(
                partner.get("telegram_id"),
                partner.get("first_name") or "Новый знакомый",
                partner.get("username"),
            )
            await callback.message.answer(
                f"🎉 Взаимный лайк! У вас мэтч с {partner_link}!\n"
                "Нажмите на имя, чтобы написать в Telegram.",
                parse_mode=ParseMode.HTML,
            )
        else:
            await callback.answer("Реакция сохранена")

        await send_next_candidate(callback.message, callback.from_user.id)


@router.callback_query(F.data == "settings")
async def cb_settings(callback: CallbackQuery):
    """Настройки поиска"""
    logger.info("callback_settings", user_id=callback.from_user.id)
    await callback.message.answer(
        "⚙️ <b>Настройки поиска</b>\n\n"
        "Выберите, что хотите изменить:",
        parse_mode=ParseMode.HTML,
        reply_markup=get_settings_menu_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "settings:gender")
async def cb_settings_gender_menu(callback: CallbackQuery):
    await callback.message.answer(
        "💕 <b>Кого показывать в поиске?</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=get_partner_gender_keyboard_for_settings(),
    )
    await callback.answer()


def get_partner_gender_keyboard_for_settings() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="👨 Мужчин", callback_data="set_gender:мужской"),
                InlineKeyboardButton(text="👩 Женщин", callback_data="set_gender:женский"),
            ],
            [InlineKeyboardButton(text="👥 Всех", callback_data="set_gender:any")],
            [InlineKeyboardButton(text="🔙 К настройкам", callback_data="settings")],
        ]
    )


@router.callback_query(F.data.startswith("set_gender:"))
async def cb_settings_gender_save(callback: CallbackQuery):
    choice = (callback.data or "").split(":", 1)[-1]
    if choice not in PARTNER_GENDER_LABELS:
        await callback.answer("Некорректный выбор", show_alert=True)
        return

    user_data = await get_user_from_profile_service(callback.from_user.id)
    if not user_data:
        await callback.answer("Сначала /start", show_alert=True)
        return

    gender_pref = None if choice == "any" else choice
    if not await save_preferences_in_profile_service(user_data["id"], gender_pref=gender_pref):
        await callback.answer("Ошибка сохранения", show_alert=True)
        return

    await callback.answer("Сохранено")
    await callback.message.answer(
        f"✅ Теперь в поиске: <b>{PARTNER_GENDER_LABELS[choice]}</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=get_settings_menu_keyboard(),
    )


@router.callback_query(F.data == "settings:age")
async def cb_settings_age_menu(callback: CallbackQuery):
    await callback.message.answer(
        "🎂 <b>Возраст партнёра в поиске</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=get_age_pref_keyboard(callback_prefix="set_age"),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("set_age:"))
async def cb_settings_age_save(callback: CallbackQuery):
    choice = (callback.data or "").split(":", 1)[-1]
    user_data = await get_user_from_profile_service(callback.from_user.id)
    if not user_data:
        await callback.answer("Сначала /start", show_alert=True)
        return

    ok, label = await apply_age_pref_choice(user_data["id"], choice)
    if not ok:
        await callback.answer("Ошибка сохранения", show_alert=True)
        return

    await callback.answer("Сохранено")
    await callback.message.answer(
        f"✅ Возраст в поиске: <b>{escape(label)}</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=get_settings_menu_keyboard(),
    )


@router.callback_query(F.data == "settings:city")
async def cb_settings_city_menu(callback: CallbackQuery):
    user_data = await get_user_from_profile_service(callback.from_user.id)
    if not user_data:
        await callback.answer("Сначала /start", show_alert=True)
        return

    profile = await get_profile_from_profile_service(user_data["id"])
    profile_city = (profile or {}).get("city")

    keyboard = get_city_pref_keyboard(profile_city, callback_prefix="set_city")
    rows = list(keyboard.inline_keyboard)
    rows.append([InlineKeyboardButton(text="🔙 К настройкам", callback_data="settings")])
    keyboard = InlineKeyboardMarkup(inline_keyboard=rows)

    await callback.message.answer(
        "📍 <b>Город в поиске</b>\n\n"
        "Ограничить выдачу по городу или показывать всех:",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("set_city:"))
async def cb_settings_city_save(callback: CallbackQuery):
    choice = (callback.data or "").split(":", 1)[-1]
    user_data = await get_user_from_profile_service(callback.from_user.id)
    if not user_data:
        await callback.answer("Сначала /start", show_alert=True)
        return

    profile = await get_profile_from_profile_service(user_data["id"])
    profile_city = (profile or {}).get("city")

    ok, label = await apply_city_pref_choice(user_data["id"], choice, profile_city)
    if not ok:
        await callback.answer(
            "Сначала укажите город в анкете или выберите «Любой город»",
            show_alert=True,
        )
        return

    await callback.answer("Сохранено")
    await callback.message.answer(
        f"✅ Город в поиске: <b>{escape(label)}</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=get_settings_menu_keyboard(),
    )


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
    try:
        await setup_bot_commands(bot)
    except TelegramNetworkError as e:
        logger.warning(
            "setup_bot_commands_skipped_network",
            error=str(e),
            hint="Проверьте доступ к api.telegram.org (VPN/файрвол). Бот продолжит без меню команд.",
        )

    try:
        me = await bot.get_me()
        logger.info("bot_starting", bot_id=me.id)
    except TelegramNetworkError as e:
        logger.warning("bot_get_me_failed", error=str(e))
    
    # Запуск polling
    try:
        await dp.start_polling(bot)
    finally:
        await close_http_client()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("bot_stopped")
