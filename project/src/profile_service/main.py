from fastapi import FastAPI, HTTPException, Depends, status
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete
from sqlalchemy.exc import IntegrityError
import structlog

from common.database import get_db, init_db
from common.models import User, Profile, Preferences, Rating
from common.logging_config import setup_logging, get_logger
from common.metrics import user_registrations_total, track_request_duration

setup_logging("profile_service")
logger = get_logger("profile_service")

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

    await session.commit()
    await session.refresh(profile)

    logger.info("profile_updated", profile_id=profile.id, completeness_score=profile.completeness_score)

    return ProfileResponse.model_validate(profile)


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


@app.on_event("shutdown")
async def shutdown_event():
    from common.database import close_db
    logger.info("profile_service_shutdown")
    await close_db()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
