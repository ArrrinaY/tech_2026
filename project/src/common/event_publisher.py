"""
Публикация доменных событий в RabbitMQ (topic exchange), отдельно от Celery.
Сейчас используется для мэтчей: consumer шлёт push в Telegram обоим пользователям.
"""
from __future__ import annotations

import json
from typing import Any, Optional

import aio_pika
from aio_pika import DeliveryMode, ExchangeType

from common.logging_config import get_logger
from common.metrics import domain_events_published_total

logger = get_logger("event_bus")

EXCHANGE_NAME = "dating.events"

_exchange: Optional[aio_pika.Exchange] = None
_connection: Optional[aio_pika.RobustConnection] = None


async def init_event_publisher(amqp_url: str) -> None:
    global _exchange, _connection
    try:
        _connection = await aio_pika.connect_robust(amqp_url)
        channel = await _connection.channel()
        _exchange = await channel.declare_exchange(
            EXCHANGE_NAME,
            ExchangeType.TOPIC,
            durable=True,
        )
        logger.info("event_publisher_ready", exchange=EXCHANGE_NAME)
    except Exception as exc:
        logger.error("event_publisher_init_failed", error=str(exc))
        _exchange = None
        _connection = None


async def shutdown_event_publisher() -> None:
    global _exchange, _connection
    _exchange = None
    if _connection is not None:
        await _connection.close()
        _connection = None


async def publish_domain_event(routing_key: str, payload: dict[str, Any]) -> None:
    if _exchange is None:
        return
    try:
        body = json.dumps(payload, default=str).encode("utf-8")
        await _exchange.publish(
            aio_pika.Message(
                body=body,
                content_type="application/json",
                delivery_mode=DeliveryMode.PERSISTENT,
            ),
            routing_key=routing_key,
        )
        domain_events_published_total.labels(event_type=routing_key).inc()
    except Exception as exc:
        logger.error(
            "publish_domain_event_failed",
            routing_key=routing_key,
            error=str(exc),
        )
