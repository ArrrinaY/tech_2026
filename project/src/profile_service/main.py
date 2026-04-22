from decimal import Decimal
from typing import Optional, List, Dict, Any

import redis.asyncio as redis
from fastapi import FastAPI, HTTPException, Depends, status
from pydantic import BaseModel, Field
from sqlalchemy import select, delete, func, and_
from sqlalchemy.ext.asyncio import AsyncSession

from common.database import get_db, init_db
from common.logging_config import setup_logging, get_logger
from common.metrics import user_registrations_total, track_request_duration
from common.models import User, Profile, Preferences, Rating, Interaction
from common.config import get_settings

setup_logging("profile_service")
logger = get_logger("profile_service")
settings = get_settings()

DISCOVERY_CACHE_BATCH_SIZE = 10
DISCOVERY_CACHE_PREFIX = "dating:discovery"

redis_client: Optional[redis.Redis] = None

app = FastAPI(
    title="Profile Service",
    description="Сервис управления анкетами пользователей для Dating Bot",
    version="1.0.0",
)


class UserRegisterRequest(BaseModel):
    telegram_id: int = Field(..., description="Telegram ID пользователя")
    username: Optional[str] = Field(None, max_length=128, description="Username в Telegram")
    first_name: Optional[str] = Field(None, max_length=128, description="Имя пользователя")


class UserResponse(BaseModel):
    id: int
    telegram_id: int
    username: Optional[str]
    first_name: Optional[str]

    class Config:
        from_attributes = True


class ProfileCreateRequest(BaseModel):
    user_id: int
    bio: Optional[str] = Field(None, description="О себе")
    interests: Optional[Dict[str, Any]] = Field(None, description="Интересы в формате JSON")
    photo_urls: Optional[List[str]] = Field(default_factory=list, description="Ссылки на фото")
    age: Optional[int] = Field(None, ge=18, description="Возраст (18+)")
    gender: Optional[str] = Field(None, max_length=20, description="Пол")
    city: Optional[str] = Field(None, max_length=100, description="Город")


class ProfileUpdateRequest(BaseModel):
    bio: Optional[str] = None
    interests: Optional[Dict[str, Any]] = None
    photo_urls: Optional[List[str]] = None
    age: Optional[int] = Field(None, ge=18)
    gender: Optional[str] = None
    city: Optional[str] = None


class PreferencesRequest(BaseModel):
    user_id: int
    age_min: Optional[int] = Field(18, ge=18, le=99)
    age_max: Optional[int] = Field(99, ge=18, le=99)
    gender_pref: Optional[str] = Field(None, max_length=20)
    city_pref: Optional[str] = Field(None, max_length=100)


class ProfileResponse(BaseModel):
    id: int
    user_id: int
    bio: Optional[str]
    interests: Optional[Dict[str, Any]]
    photo_urls: Optional[List[str]]
    completeness_score: float
    age: Optional[int]
    gender: Optional[str]
    city: Optional[str]

    class Config:
        from_attributes = True


class RatingResponse(BaseModel):
    primary_score: float
    behavioral_score: float
    combined_score: float


class DiscoveryProfileResponse(BaseModel):
    profile_id: int
    user_id: int
    age: Optional[int]
    gender: Optional[str]
    city: Optional[str]
    bio: Optional[str]
    photo_urls: Optional[List[str]]
    rating: RatingResponse


class InteractionRequest(BaseModel):
    actor_user_id: int
    target_profile_id: int
    action: str = Field(..., pattern="^(like|pass|super_like)$")


def calculate_completeness_score(profile_data: dict) -> float:
    score = 0.0

    if profile_data.get('bio'):
        score += 0.2

    if profile_data.get('age'):
        score += 0.2

    if profile_data.get('gender'):
        score += 0.2

    if profile_data.get('city'):
        score += 0.2

    photos = profile_data.get('photo_urls', [])
    if photos and len(photos) > 0:
        score += 0.2

    return round(score, 4)


def normalize_decimal(value: Decimal | float | int | None) -> float:
    if value is None:
        return 0.0
    if isinstance(value, Decimal):
        return float(value)
    return float(value)


def calculate_primary_score(profile: Profile) -> float:
    completeness = normalize_decimal(profile.completeness_score)
    photos_count = len(profile.photo_urls or [])
    photo_bonus = min(photos_count / 5, 1.0)
    score = (completeness * 70) + (photo_bonus * 30)
    return round(score, 2)


def calculate_combined_score(primary_score: float, behavioral_score: float, referral_bonus: float = 0.0) -> float:
    combined = (primary_score * 0.6) + (behavioral_score * 0.35) + (referral_bonus * 0.05)
    return round(combined, 2)


def get_discovery_cache_key(user_id: int) -> str:
    return f"{DISCOVERY_CACHE_PREFIX}:{user_id}:queue"


async def invalidate_discovery_cache(user_id: int) -> None:
    if redis_client is None:
        return
    await redis_client.delete(get_discovery_cache_key(user_id))


async def calculate_behavioral_score(session: AsyncSession, profile_id: int) -> float:
    likes_query = select(func.count(Interaction.id)).where(
        and_(Interaction.target_profile_id == profile_id, Interaction.action.in_(["like", "super_like"]))
    )
    passes_query = select(func.count(Interaction.id)).where(
        and_(Interaction.target_profile_id == profile_id, Interaction.action == "pass")
    )
    matches_query = select(func.count(Interaction.id)).where(
        and_(Interaction.target_profile_id == profile_id, Interaction.is_match.is_(True))
    )

    likes = (await session.execute(likes_query)).scalar() or 0
    passes = (await session.execute(passes_query)).scalar() or 0
    matches = (await session.execute(matches_query)).scalar() or 0

    total = likes + passes
    if total == 0:
        return 0.0

    like_ratio = likes / total
    match_ratio = matches / likes if likes else 0.0
    score = (like_ratio * 65) + (match_ratio * 35)
    return round(score, 2)


async def recalculate_rating_for_profile(session: AsyncSession, profile: Profile) -> Rating:
    result = await session.execute(select(Rating).where(Rating.profile_id == profile.id))
    rating = result.scalar_one_or_none()

    primary_score = calculate_primary_score(profile)
    behavioral_score = await calculate_behavioral_score(session, profile.id)
    combined_score = calculate_combined_score(primary_score, behavioral_score)

    if rating is None:
        rating = Rating(profile_id=profile.id)
        session.add(rating)

    rating.primary_score = primary_score
    rating.behavioral_score = behavioral_score
    rating.combined_score = combined_score
    return rating


async def get_or_create_default_preferences(session: AsyncSession, user_id: int) -> Preferences:
    result = await session.execute(select(Preferences).where(Preferences.user_id == user_id))
    preferences = result.scalar_one_or_none()
    if preferences:
        return preferences

    preferences = Preferences(user_id=user_id)
    session.add(preferences)
    await session.flush()
    return preferences


async def rebuild_discovery_cache(session: AsyncSession, user_id: int) -> int:
    if redis_client is None:
        return 0

    preferences = await get_or_create_default_preferences(session, user_id)

    interacted_subquery = select(Interaction.target_profile_id).where(Interaction.actor_user_id == user_id)

    base_query = (
        select(Profile.id)
        .join(Rating, Rating.profile_id == Profile.id, isouter=True)
        .where(Profile.user_id != user_id)
        .where(Profile.age.is_not(None))
        .where(Profile.gender.is_not(None))
    )

    query = base_query.where(~Profile.id.in_(interacted_subquery))

    if preferences.age_min is not None:
        query = query.where(Profile.age >= preferences.age_min)
    if preferences.age_max is not None:
        query = query.where(Profile.age <= preferences.age_max)
    if preferences.gender_pref:
        query = query.where(Profile.gender == preferences.gender_pref)
    if preferences.city_pref:
        query = query.where(Profile.city == preferences.city_pref)

    query = query.order_by(Rating.combined_score.desc().nullslast(), Profile.completeness_score.desc()).limit(
        DISCOVERY_CACHE_BATCH_SIZE
    )

    rows = (await session.execute(query)).scalars().all()

    # Если новых анкет больше нет, запускаем новый круг и выдаем уже просмотренные.
    if not rows:
        fallback_query = base_query
        if preferences.age_min is not None:
            fallback_query = fallback_query.where(Profile.age >= preferences.age_min)
        if preferences.age_max is not None:
            fallback_query = fallback_query.where(Profile.age <= preferences.age_max)
        if preferences.gender_pref:
            fallback_query = fallback_query.where(Profile.gender == preferences.gender_pref)
        if preferences.city_pref:
            fallback_query = fallback_query.where(Profile.city == preferences.city_pref)
        fallback_query = fallback_query.order_by(
            Rating.combined_score.desc().nullslast(),
            Profile.completeness_score.desc(),
        ).limit(DISCOVERY_CACHE_BATCH_SIZE)
        rows = (await session.execute(fallback_query)).scalars().all()

    profile_ids = [int(profile_id) for profile_id in rows]
    key = get_discovery_cache_key(user_id)
    await redis_client.delete(key)

    if profile_ids:
        await redis_client.rpush(key, *profile_ids)
        await redis_client.expire(key, 1800)

    return len(profile_ids)


async def pop_next_cached_profile_id(session: AsyncSession, user_id: int) -> Optional[int]:
    if redis_client is None:
        return None

    key = get_discovery_cache_key(user_id)
    cached_count = await redis_client.llen(key)
    if cached_count == 0:
        await rebuild_discovery_cache(session, user_id)

    profile_id = await redis_client.lpop(key)
    if profile_id is None:
        return None

    remaining = await redis_client.llen(key)
    if remaining == 0:
        await rebuild_discovery_cache(session, user_id)

    return int(profile_id)


async def get_user_or_404(telegram_id: int, session: AsyncSession) -> User:
    result = await session.execute(select(User).where(User.telegram_id == telegram_id))
    user = result.scalar_one_or_none()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Пользователь с telegram_id={telegram_id} не найден"
        )

    return user


@app.get("/health")
async def health_check():
    return {"status": "healthy", "service": "profile_service"}


@app.post("/api/v1/users/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
@track_request_duration("profile_service", "/api/v1/users/register")
async def register_user(
    request: UserRegisterRequest,
    session: AsyncSession = Depends(get_db)
):
    logger.info(
        "register_user",
        telegram_id=request.telegram_id,
        username=request.username
    )

    try:
        existing = await session.execute(
            select(User).where(User.telegram_id == request.telegram_id)
        )
        if existing.scalar_one_or_none():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Пользователь с таким telegram_id уже зарегистрирован"
            )

        user = User(
            telegram_id=request.telegram_id,
            username=request.username,
            first_name=request.first_name,
        )

        session.add(user)
        await session.flush()

        profile = Profile(user_id=user.id)
        session.add(profile)
        await session.flush()

        preferences = Preferences(user_id=user.id)
        session.add(preferences)

        rating = Rating(profile_id=profile.id)
        session.add(rating)

        await session.commit()
        await session.refresh(user)

        user_registrations_total.labels(service="profile_service").inc()

        logger.info(
            "user_registered",
            user_id=user.id,
            telegram_id=user.telegram_id
        )

        return UserResponse.model_validate(user)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("register_user_error", error=str(e), exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Ошибка регистрации: {str(e)}"
        )


@app.get("/api/v1/users/{telegram_id}", response_model=UserResponse)
@track_request_duration("profile_service", "/api/v1/users/{telegram_id}")
async def get_user(
    telegram_id: int,
    session: AsyncSession = Depends(get_db)
):
    user = await get_user_or_404(telegram_id, session)
    return UserResponse.model_validate(user)


@app.delete("/api/v1/users/{telegram_id}", status_code=status.HTTP_204_NO_CONTENT)
@track_request_duration("profile_service", "/api/v1/users/{telegram_id}")
async def delete_user(
    telegram_id: int,
    session: AsyncSession = Depends(get_db)
):
    logger.info("delete_user", telegram_id=telegram_id)

    user = await get_user_or_404(telegram_id, session)

    await session.execute(delete(User).where(User.id == user.id))
    await session.commit()

    logger.info("user_deleted", telegram_id=telegram_id)


@app.post("/api/v1/profiles", response_model=ProfileResponse, status_code=status.HTTP_201_CREATED)
@track_request_duration("profile_service", "/api/v1/profiles")
async def create_profile(
    request: ProfileCreateRequest,
    session: AsyncSession = Depends(get_db)
):
    logger.info("create_profile", user_id=request.user_id)

    result = await session.execute(select(User).where(User.id == request.user_id))
    user = result.scalar_one_or_none()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Пользователь с id={request.user_id} не найден"
        )

    result = await session.execute(select(Profile).where(Profile.user_id == request.user_id))
    profile = result.scalar_one_or_none()

    if profile:
        update_data = request.model_dump(exclude_unset=True)
        for field, value in update_data.items():
            setattr(profile, field, value)

        profile.completeness_score = calculate_completeness_score(
            {
                'bio': profile.bio,
                'age': profile.age,
                'gender': profile.gender,
                'city': profile.city,
                'photo_urls': profile.photo_urls,
            }
        )

        logger.info("profile_updated", profile_id=profile.id)
    else:
        profile_data = request.model_dump()
        completeness = calculate_completeness_score(profile_data)

        profile = Profile(
            user_id=request.user_id,
            bio=request.bio,
            interests=request.interests,
            photo_urls=request.photo_urls or [],
            age=request.age,
            gender=request.gender,
            city=request.city,
            completeness_score=completeness,
        )
        session.add(profile)
        logger.info("profile_created", user_id=request.user_id)

    await recalculate_rating_for_profile(session, profile)
    await session.commit()
    await session.refresh(profile)

    return ProfileResponse.model_validate(profile)


@app.get("/api/v1/profiles/{user_id}", response_model=ProfileResponse)
@track_request_duration("profile_service", "/api/v1/profiles/{user_id}")
async def get_profile(
    user_id: int,
    session: AsyncSession = Depends(get_db)
):
    result = await session.execute(select(Profile).where(Profile.user_id == user_id))
    profile = result.scalar_one_or_none()

    if not profile:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Анкета для user_id={user_id} не найдена"
        )

    return ProfileResponse.model_validate(profile)


@app.put("/api/v1/profiles/{user_id}", response_model=ProfileResponse)
@track_request_duration("profile_service", "/api/v1/profiles/{user_id}")
async def update_profile(
    user_id: int,
    request: ProfileUpdateRequest,
    session: AsyncSession = Depends(get_db)
):
    logger.info("update_profile", user_id=user_id)

    result = await session.execute(select(Profile).where(Profile.user_id == user_id))
    profile = result.scalar_one_or_none()

    if not profile:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Анкета для user_id={user_id} не найдена"
        )

    update_data = request.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(profile, field, value)

    profile.completeness_score = calculate_completeness_score(
        {
            'bio': profile.bio,
            'age': profile.age,
            'gender': profile.gender,
            'city': profile.city,
            'photo_urls': profile.photo_urls,
        }
    )

    await recalculate_rating_for_profile(session, profile)
    await session.commit()
    await session.refresh(profile)

    logger.info("profile_updated", profile_id=profile.id, completeness_score=profile.completeness_score)

    return ProfileResponse.model_validate(profile)


@app.delete("/api/v1/profiles/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
@track_request_duration("profile_service", "/api/v1/profiles/{user_id}")
async def delete_profile(
    user_id: int,
    session: AsyncSession = Depends(get_db)
):
    result = await session.execute(select(Profile).where(Profile.user_id == user_id))
    profile = result.scalar_one_or_none()
    if not profile:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Анкета для user_id={user_id} не найдена"
        )

    await session.execute(delete(Profile).where(Profile.user_id == user_id))
    await session.commit()
    await invalidate_discovery_cache(user_id)


@app.post("/api/v1/preferences", response_model=dict)
@track_request_duration("profile_service", "/api/v1/preferences")
async def set_preferences(
    request: PreferencesRequest,
    session: AsyncSession = Depends(get_db)
):
    logger.info("set_preferences", user_id=request.user_id)

    result = await session.execute(select(User).where(User.id == request.user_id))
    user = result.scalar_one_or_none()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Пользователь с id={request.user_id} не найден"
        )

    result = await session.execute(select(Preferences).where(Preferences.user_id == request.user_id))
    preferences = result.scalar_one_or_none()

    if preferences:
        update_data = request.model_dump(exclude={'user_id'}, exclude_unset=True)
        for field, value in update_data.items():
            setattr(preferences, field, value)
    else:
        preferences = Preferences(
            user_id=request.user_id,
            age_min=request.age_min,
            age_max=request.age_max,
            gender_pref=request.gender_pref,
            city_pref=request.city_pref,
        )
        session.add(preferences)

    await session.commit()

    logger.info("preferences_updated", user_id=request.user_id)

    return {"status": "success", "user_id": request.user_id}


@app.get("/api/v1/discovery/{user_id}/next", response_model=DiscoveryProfileResponse)
@track_request_duration("profile_service", "/api/v1/discovery/{user_id}/next")
async def get_next_discovery_profile(
    user_id: int,
    session: AsyncSession = Depends(get_db)
):
    user_result = await session.execute(select(User).where(User.id == user_id))
    user = user_result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Пользователь не найден")

    profile_id = await pop_next_cached_profile_id(session, user_id)
    if profile_id is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Подходящие анкеты не найдены")

    result = await session.execute(
        select(Profile, Rating)
        .join(Rating, Rating.profile_id == Profile.id, isouter=True)
        .where(Profile.id == profile_id)
    )
    row = result.first()
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Анкета не найдена")

    profile, rating = row
    if rating is None:
        rating = await recalculate_rating_for_profile(session, profile)
        await session.commit()
        await session.refresh(rating)

    return DiscoveryProfileResponse(
        profile_id=profile.id,
        user_id=profile.user_id,
        age=profile.age,
        gender=profile.gender,
        city=profile.city,
        bio=profile.bio,
        photo_urls=profile.photo_urls or [],
        rating=RatingResponse(
            primary_score=normalize_decimal(rating.primary_score),
            behavioral_score=normalize_decimal(rating.behavioral_score),
            combined_score=normalize_decimal(rating.combined_score),
        )
    )


@app.post("/api/v1/interactions", response_model=dict)
@track_request_duration("profile_service", "/api/v1/interactions")
async def create_interaction(
    request: InteractionRequest,
    session: AsyncSession = Depends(get_db)
):
    actor_result = await session.execute(select(User).where(User.id == request.actor_user_id))
    actor = actor_result.scalar_one_or_none()
    if not actor:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Пользователь-инициатор не найден")

    target_profile_result = await session.execute(select(Profile).where(Profile.id == request.target_profile_id))
    target_profile = target_profile_result.scalar_one_or_none()
    if not target_profile:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Целевая анкета не найдена")

    if target_profile.user_id == request.actor_user_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Нельзя взаимодействовать со своей анкетой")

    actor_profile_result = await session.execute(select(Profile).where(Profile.user_id == request.actor_user_id))
    actor_profile = actor_profile_result.scalar_one_or_none()
    if actor_profile is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Сначала заполните собственную анкету")

    is_match = False
    if request.action in {"like", "super_like"}:
        reverse_like_result = await session.execute(
            select(Interaction).where(
                and_(
                    Interaction.actor_user_id == target_profile.user_id,
                    Interaction.target_profile_id == actor_profile.id,
                    Interaction.action.in_(["like", "super_like"]),
                )
            )
        )
        reverse_like = reverse_like_result.scalar_one_or_none()
        if reverse_like:
            is_match = True
            reverse_like.is_match = True

    interaction = Interaction(
        actor_user_id=request.actor_user_id,
        target_profile_id=request.target_profile_id,
        action=request.action,
        is_match=is_match,
    )
    session.add(interaction)

    target_rating = await recalculate_rating_for_profile(session, target_profile)
    await session.commit()
    await session.refresh(target_rating)

    await invalidate_discovery_cache(request.actor_user_id)

    return {
        "status": "ok",
        "is_match": is_match,
        "target_profile_id": request.target_profile_id,
        "updated_rating": {
            "primary_score": normalize_decimal(target_rating.primary_score),
            "behavioral_score": normalize_decimal(target_rating.behavioral_score),
            "combined_score": normalize_decimal(target_rating.combined_score),
        },
    }


@app.get("/metrics")
async def get_prometheus_metrics():
    from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
    from fastapi.responses import Response

    return Response(
        content=generate_latest(),
        media_type=CONTENT_TYPE_LATEST,
    )


@app.on_event("startup")
async def startup_event():
    logger.info("profile_service_startup")
    await init_db()
    global redis_client
    redis_client = redis.from_url(settings.redis_url, decode_responses=True)
    try:
        await redis_client.ping()
        logger.info("redis_connected", url=settings.redis_url)
    except Exception as exc:
        logger.error("redis_connection_failed", error=str(exc))
        redis_client = None


@app.on_event("shutdown")
async def shutdown_event():
    from common.database import close_db
    logger.info("profile_service_shutdown")
    if redis_client is not None:
        await redis_client.close()
    await close_db()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
