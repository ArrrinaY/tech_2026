import argparse
import json
import random
import string
import time

import pika
import redis


QUEUE_NAME = "bench_q"


def wait_for_rabbitmq(host: str, port: int, retries: int = 30, delay_s: int = 2):
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            connection = pika.BlockingConnection(
                pika.ConnectionParameters(host=host, port=port, heartbeat=600)
            )
            channel = connection.channel()
            return connection, channel
        except Exception as exc:
            last_error = exc
            print(f"[Producer] RabbitMQ connect attempt {attempt}/{retries} failed: {exc}")
            time.sleep(delay_s)
    raise RuntimeError(f"RabbitMQ unavailable: {last_error}")


def wait_for_redis(host: str, port: int, retries: int = 30, delay_s: int = 2):
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            client = redis.Redis(host=host, port=port, db=0)
            client.ping()
            return client
        except Exception as exc:
            last_error = exc
            print(f"[Producer] Redis connect attempt {attempt}/{retries} failed: {exc}")
            time.sleep(delay_s)
    raise RuntimeError(f"Redis unavailable: {last_error}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--broker", choices=["rabbitmq", "redis"], required=True)
    parser.add_argument("--msg-size", type=int, default=1024)
    parser.add_argument("--rate", type=int, default=1000)
    parser.add_argument("--duration", type=int, default=30)
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--queue-name", default=QUEUE_NAME)
    parser.add_argument("--reset-queue", action="store_true")
    parser.add_argument("--output", default="prod_metrics.json")
    args = parser.parse_args()

    port = args.port or (5672 if args.broker == "rabbitmq" else 6379)
    payload = "".join(random.choices(string.ascii_letters + string.digits, k=args.msg_size))

    sent = 0
    errors = 0
    start = time.time()
    interval = 1.0 / max(args.rate, 1)
    next_send_time = start

    print(
        f"[Producer] broker={args.broker} host={args.host}:{port} "
        f"queue={args.queue_name} size={args.msg_size} rate={args.rate} dur={args.duration}s"
    )

    connection = None
    channel = None
    redis_client = None

    try:
        if args.broker == "rabbitmq":
            connection, channel = wait_for_rabbitmq(args.host, port)
            channel.queue_declare(queue=args.queue_name, durable=False)
            if args.reset_queue:
                channel.queue_purge(queue=args.queue_name)
        else:
            redis_client = wait_for_redis(args.host, port)
            if args.reset_queue:
                redis_client.delete(args.queue_name)

        while time.time() - start < args.duration:
            now = time.time()
            if now < next_send_time:
                time.sleep(next_send_time - now)

            msg = json.dumps({"ts": time.time_ns(), "data": payload}).encode()
            try:
                if args.broker == "rabbitmq":
                    channel.basic_publish(exchange="", routing_key=args.queue_name, body=msg)
                else:
                    redis_client.rpush(args.queue_name, msg)
                sent += 1
            except Exception as exc:
                errors += 1
                print(f"[Producer] publish error: {exc}")

            next_send_time += interval
            if sent > 0 and sent % max(args.rate * 5, 1) == 0:
                elapsed_total = time.time() - start
                print(f"[Producer] progress={int(elapsed_total / args.duration * 100)}% sent={sent}")

    finally:
        if connection:
            connection.close()

        elapsed = max(time.time() - start, 0.001)
        metrics = {
            "broker": args.broker,
            "msg_size_bytes": args.msg_size,
            "target_rate": args.rate,
            "duration_s": args.duration,
            "sent": sent,
            "errors": errors,
            "actual_throughput_msg_s": round(sent / elapsed, 2),
        }

        with open(args.output, "w", encoding="utf-8") as file_obj:
            json.dump(metrics, file_obj, indent=2)

        print(
            "[Producer] done "
            f"sent={sent} errors={errors} throughput={metrics['actual_throughput_msg_s']}"
        )


if __name__ == "__main__":
    main()