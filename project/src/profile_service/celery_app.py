from celery import Celery

from common.config import get_settings

settings = get_settings()

celery_app = Celery(
    "profile_service",
    broker=settings.rabbitmq_url,
    include=["profile_service.tasks"],
)

celery_app.conf.update(
    task_serializer=settings.celery_task_serializer,
    result_serializer=settings.celery_result_serializer,
    accept_content=[settings.celery_accept_content],
    timezone=settings.celery_timezone,
    enable_utc=True,
    task_always_eager=settings.celery_task_always_eager,
)

# Periodic maintenance tasks for stage 4 requirements.
celery_app.conf.beat_schedule = {
    "recalculate-ratings-every-10-min": {
        "task": "profile_service.tasks.recalculate_all_ratings",
        "schedule": 600.0,
    },
    "warm-discovery-cache-every-5-min": {
        "task": "profile_service.tasks.warm_discovery_cache",
        "schedule": 300.0,
    },
}
