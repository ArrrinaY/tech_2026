import argparse
import json
import signal
import time
from typing import List

import pika
import redis


QUEUE_NAME = "bench_q"


class GracefulKiller:
    def __init__(self):
        self.kill_now = False
        signal.signal(signal.SIGINT, self.signal_handler)
        signal.signal(signal.SIGTERM, self.signal_handler)

    def signal_handler(self, signum, frame):
        self.kill_now = True


def wait_for_rabbitmq(host: str, port: int, retries: int = 30, delay_s: int = 2):
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            connection = pika.BlockingConnection(
                pika.ConnectionParameters(host=host, port=port, heartbeat=600)
            )
            channel = connection.channel()
            channel.queue_declare(queue=QUEUE_NAME, durable=False)
            return connection, channel
        except Exception as exc:
            last_error = exc
            print(f"[Consumer] RabbitMQ connect attempt {attempt}/{retries} failed: {exc}")
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
            print(f"[Consumer] Redis connect attempt {attempt}/{retries} failed: {exc}")
            time.sleep(delay_s)
    raise RuntimeError(f"Redis unavailable: {last_error}")


def p95(values: List[float]) -> float:
    if not values:
        return 0.0
    sorted_values = sorted(values)
    idx = min(int(len(sorted_values) * 0.95), len(sorted_values) - 1)
    return sorted_values[idx]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--broker", choices=["rabbitmq", "redis"], required=True)
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--idle-timeout", type=int, default=5)
    parser.add_argument("--max-runtime", type=int, default=180)
    parser.add_argument("--expected-msgs", type=int, default=0)
    parser.add_argument("--queue-name", default=QUEUE_NAME)
    parser.add_argument("--output", default="cons_metrics.json")
    args = parser.parse_args()

    port = args.port or (5672 if args.broker == "rabbitmq" else 6379)
    received = 0
    errors = 0
    latencies = []
    started_at = time.time()
    last_message_at = time.time()
    killer = GracefulKiller()

    print(
        f"[Consumer] broker={args.broker} host={args.host}:{port} "
        f"queue={args.queue_name} idle_timeout={args.idle_timeout}s"
    )

    rabbit_connection = None
    rabbit_channel = None
    redis_client = None

    try:
        if args.broker == "rabbitmq":
            rabbit_connection, rabbit_channel = wait_for_rabbitmq(args.host, port)
            rabbit_channel.basic_qos(prefetch_count=1000)
            rabbit_channel.queue_declare(queue=args.queue_name, durable=False)

            for method_frame, _, body in rabbit_channel.consume(
                queue=args.queue_name, inactivity_timeout=1, auto_ack=True
            ):
                if killer.kill_now:
                    break

                if method_frame:
                    received += 1
                    last_message_at = time.time()
                    try:
                        data = json.loads(body)
                        latencies.append((time.time_ns() - data["ts"]) / 1_000_000)
                    except Exception:
                        errors += 1

                if args.expected_msgs > 0 and received >= args.expected_msgs:
                    print("[Consumer] expected message count reached")
                    break
                if received > 0 and time.time() - last_message_at > args.idle_timeout:
                    print("[Consumer] queue is idle, finishing")
                    break
                if time.time() - started_at > args.max_runtime:
                    print("[Consumer] max runtime reached")
                    break

            rabbit_channel.cancel()
        else:
            redis_client = wait_for_redis(args.host, port)
            while not killer.kill_now:
                result = redis_client.blpop(args.queue_name, timeout=1)
                if result:
                    received += 1
                    last_message_at = time.time()
                    try:
                        data = json.loads(result[1])
                        latencies.append((time.time_ns() - data["ts"]) / 1_000_000)
                    except Exception:
                        errors += 1

                if args.expected_msgs > 0 and received >= args.expected_msgs:
                    print("[Consumer] expected message count reached")
                    break
                if received > 0 and time.time() - last_message_at > args.idle_timeout:
                    print("[Consumer] queue is idle, finishing")
                    break
                if time.time() - started_at > args.max_runtime:
                    print("[Consumer] max runtime reached")
                    break
    except Exception as exc:
        errors += 1
        print(f"[Consumer] fatal error: {exc}")
    finally:
        elapsed = max(time.time() - started_at, 0.001)
        avg_latency = sum(latencies) / len(latencies) if latencies else 0.0
        p95_latency = p95(latencies)
        remaining_in_queue = 0
        try:
            if args.broker == "rabbitmq" and rabbit_channel:
                queue_state = rabbit_channel.queue_declare(queue=args.queue_name, passive=True)
                remaining_in_queue = queue_state.method.message_count
            elif args.broker == "redis" and redis_client:
                remaining_in_queue = int(redis_client.llen(args.queue_name))
        except Exception as exc:
            print(f"[Consumer] queue size check failed: {exc}")

        if rabbit_connection:
            rabbit_connection.close()

        metrics = {
            "broker": args.broker,
            "queue_name": args.queue_name,
            "received": received,
            "errors": errors,
            "remaining_in_queue": remaining_in_queue,
            "duration_s": round(elapsed, 2),
            "throughput_msg_s": round(received / elapsed, 2),
            "avg_latency_ms": round(avg_latency, 2),
            "p95_latency_ms": round(p95_latency, 2),
        }

        with open(args.output, "w", encoding="utf-8") as file_obj:
            json.dump(metrics, file_obj, indent=2)

        print(
            "[Consumer] done "
            f"received={received} errors={errors} "
            f"throughput={metrics['throughput_msg_s']}"
        )


if __name__ == "__main__":
    main()