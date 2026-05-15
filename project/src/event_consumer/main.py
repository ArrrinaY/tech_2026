"""
Consumer доменных событий: при мэтче шлёт обоим пользователям сообщение в Telegram
через Bot API (работает даже когда человек не в чате с ботом на экране ленты).
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import urllib.error
import urllib.request
from html import escape

import pika

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
log = logging.getLogger("event_consumer")

EXCHANGE = "dating.events"
QUEUE = "dating.bot_match_push"


def _rabbit_params() -> pika.ConnectionParameters:
    host = os.environ.get("RABBITMQ_HOST", "localhost")
    port = int(os.environ.get("RABBITMQ_PORT", "5672"))
    user = os.environ.get("RABBITMQ_USER", "dating_user")
    password = os.environ.get("RABBITMQ_PASSWORD", "dating_password")
    creds = pika.PlainCredentials(user, password)
    return pika.ConnectionParameters(
        host=host,
        port=port,
        credentials=creds,
        heartbeat=600,
        blocked_connection_timeout=300,
    )


def _format_user_chat_link(
    telegram_id: int,
    display_name: str,
    username: str | None = None,
) -> str:
    safe_name = escape(display_name or "Новый знакомый")
    if username:
        uname = re.sub(r"[^a-zA-Z0-9_]", "", username.lstrip("@"))
        if uname:
            return f'<a href="https://t.me/{uname}">{safe_name}</a>'
    return f'<a href="tg://user?id={telegram_id}">{safe_name}</a>'


def _send_telegram_message(
    bot_token: str,
    chat_id: int,
    text: str,
    *,
    parse_mode: str | None = None,
) -> None:
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    body: dict = {"chat_id": chat_id, "text": text}
    if parse_mode:
        body["parse_mode"] = parse_mode
    payload = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status}: {raw}")
        data = json.loads(raw)
        if not data.get("ok"):
            raise RuntimeError(raw)


def _handle_match_created(bot_token: str, payload: dict) -> None:
    actor_chat = int(payload["actor_telegram_id"])
    target_chat = int(payload["target_telegram_id"])
    name_for_actor = str(payload.get("partner_name_for_actor") or "Новый знакомый")
    name_for_target = str(payload.get("partner_name_for_target") or "Новый знакомый")
    username_for_actor = payload.get("partner_username_for_actor")
    username_for_target = payload.get("partner_username_for_target")

    partner_for_actor = _format_user_chat_link(
        target_chat,
        name_for_actor,
        username_for_actor if isinstance(username_for_actor, str) else None,
    )
    partner_for_target = _format_user_chat_link(
        actor_chat,
        name_for_target,
        username_for_target if isinstance(username_for_target, str) else None,
    )

    msg_actor = (
        "💞 Это мэтч! "
        f"{partner_for_actor} тоже ответил(а) взаимностью.\n"
        "Нажмите на имя, чтобы написать в Telegram."
    )
    msg_target = (
        "💞 Это мэтч! "
        f"{partner_for_target} только что поставил(а) вам лайк — у вас взаимность.\n"
        "Нажмите на имя, чтобы написать в Telegram."
    )

    _send_telegram_message(bot_token, actor_chat, msg_actor, parse_mode="HTML")
    _send_telegram_message(bot_token, target_chat, msg_target, parse_mode="HTML")
    log.info("match_push_sent actor=%s target=%s", actor_chat, target_chat)


def main() -> None:
    bot_token = (os.environ.get("BOT_TOKEN") or "").strip()
    if not bot_token:
        log.error("BOT_TOKEN is empty: set it in .env for match notifications")
        sys.exit(1)

    connection = pika.BlockingConnection(_rabbit_params())
    channel = connection.channel()
    channel.exchange_declare(exchange=EXCHANGE, exchange_type="topic", durable=True)
    channel.queue_declare(queue=QUEUE, durable=True)
    channel.queue_bind(queue=QUEUE, exchange=EXCHANGE, routing_key="match.created")

    def on_message(
        ch: pika.channel.Channel,
        method: pika.spec.Basic.Deliver,
        _properties: pika.spec.BasicProperties,
        body: bytes,
    ) -> None:
        try:
            payload = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError:
            log.warning("bad json, ack skip: %s", body[:200])
            ch.basic_ack(delivery_tag=method.delivery_tag)
            return

        try:
            if method.routing_key == "match.created":
                _handle_match_created(bot_token, payload)
            else:
                log.info("ignored routing_key=%s", method.routing_key)
        except (KeyError, ValueError, TypeError) as exc:
            log.error("bad_payload key=%s err=%s", method.routing_key, exc)
            ch.basic_ack(delivery_tag=method.delivery_tag)
            return
        except (urllib.error.HTTPError, urllib.error.URLError, RuntimeError, OSError) as exc:
            log.error("telegram_or_network_failed key=%s err=%s", method.routing_key, exc)
            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=True)
            return

        ch.basic_ack(delivery_tag=method.delivery_tag)

    channel.basic_qos(prefetch_count=5)
    channel.basic_consume(queue=QUEUE, on_message_callback=on_message)

    log.info("consuming queue=%s for routing_key=match.created", QUEUE)
    try:
        channel.start_consuming()
    finally:
        connection.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("stopped")
    except pika.exceptions.AMQPConnectionError as e:
        log.error("cannot connect to RabbitMQ: %s", e)
        sys.exit(1)
