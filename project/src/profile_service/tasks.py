import asyncio

from sqlalchemy import select

from common.database import async_session_maker
from common.logging_config import get_logger, setup_logging
from common.metrics import rating_calculation_duration
from common.models import Profile, User
from profile_service.celery_app import celery_app
from profile_service.main import recalculate_rating_for_profile, rebuild_discovery_cache

setup_logging("profile_service_worker")
logger = get_logger("profile_service_worker")


@celery_app.task(name="profile_service.tasks.recalculate_all_ratings")
def recalculate_all_ratings() -> dict:
    return asyncio.run(_recalculate_all_ratings_async())


async def _recalculate_all_ratings_async() -> dict:
    async with async_session_maker() as session:
        profiles = (await session.execute(select(Profile))).scalars().all()
        recalculated = 0
        for profile in profiles:
            with rating_calculation_duration.labels(
                service="profile_service",
                rating_type="full_recalculation",
            ).time():
                await recalculate_rating_for_profile(session, profile)
            recalculated += 1

        await session.commit()
        logger.info("ratings_recalculated", profiles_total=recalculated)
        return {"status": "ok", "recalculated_profiles": recalculated}


@celery_app.task(name="profile_service.tasks.warm_discovery_cache")
def warm_discovery_cache() -> dict:
    return asyncio.run(_warm_discovery_cache_async())


async def _warm_discovery_cache_async() -> dict:
    async with async_session_maker() as session:
        users = (await session.execute(select(User.id))).scalars().all()
        warmed = 0
        for user_id in users:
            cached = await rebuild_discovery_cache(session, int(user_id))
            if cached > 0:
                warmed += 1

        logger.info("discovery_cache_warmed", users_total=len(users), users_with_cache=warmed)
        return {"status": "ok", "users_total": len(users), "users_with_cache": warmed}
