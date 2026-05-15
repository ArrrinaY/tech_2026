from prometheus_client import Counter, Histogram, Gauge, generate_latest, CONTENT_TYPE_LATEST
from prometheus_client import CollectorRegistry
import time
from functools import wraps
from typing import Optional


registry = CollectorRegistry()

messages_processed = Counter(
    'bot_messages_processed_total',
    'Total number of bot messages processed',
    ['service', 'message_type'],
    registry=registry
)

api_requests_total = Counter(
    'api_requests_total',
    'Total number of API requests',
    ['service', 'endpoint', 'method', 'status'],
    registry=registry
)

errors_total = Counter(
    'errors_total',
    'Total number of errors',
    ['service', 'error_type'],
    registry=registry
)

user_registrations_total = Counter(
    'user_registrations_total',
    'Total number of user registrations',
    ['service'],
    registry=registry
)

request_duration = Histogram(
    'request_duration_seconds',
    'Request duration in seconds',
    ['service', 'endpoint'],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
    registry=registry
)

message_processing_duration = Histogram(
    'message_processing_duration_seconds',
    'Message processing duration in seconds',
    ['service', 'message_type'],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0),
    registry=registry
)

rating_calculation_duration = Histogram(
    'rating_calculation_duration_seconds',
    'Rating calculation duration in seconds',
    ['service', 'rating_type'],
    buckets=(0.1, 0.5, 1.0, 2.5, 5.0, 10.0),
    registry=registry
)

active_users = Gauge(
    'active_users',
    'Number of active users',
    ['service'],
    registry=registry
)

cached_profiles = Gauge(
    'cached_profiles_count',
    'Number of profiles in cache',
    ['service'],
    registry=registry
)

domain_events_published_total = Counter(
    'domain_events_published_total',
    'Domain events published to RabbitMQ (non-Celery topic exchange)',
    ['event_type'],
    registry=registry
)


def track_request_duration(service: str, endpoint: str):
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            start_time = time.time()
            try:
                result = await func(*args, **kwargs)
                status = "success"
                return result
            except Exception as e:
                status = "error"
                errors_total.labels(service=service, error_type=type(e).__name__).inc()
                raise
            finally:
                duration = time.time() - start_time
                request_duration.labels(service=service, endpoint=endpoint).observe(duration)
                api_requests_total.labels(
                    service=service,
                    endpoint=endpoint,
                    method="POST",
                    status=status
                ).inc()
        return wrapper
    return decorator


def get_metrics() -> str:
    return generate_latest(registry).decode('utf-8')


def get_metrics_content_type() -> str:
    return CONTENT_TYPE_LATEST
