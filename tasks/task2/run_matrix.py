import argparse
import csv
import json
import os
import subprocess
import time
import uuid

from tabulate import tabulate


DEFAULT_BROKERS = ["rabbitmq", "redis"]
DEFAULT_SIZES = [128, 1024, 10_240, 102_400]  # 128B, 1KB, 10KB, 100KB
DEFAULT_RATES = [1000, 5000, 10000]
DEGRADATION_LAT_THRESHOLD_MS = 500
DEGRADATION_LOSS_THRESHOLD = 0.01


def parse_csv_ints(value: str):
    return [int(part.strip()) for part in value.split(",") if part.strip()]


def run_single(
    broker: str,
    msg_size: int,
    rate: int,
    duration: int,
    host: str,
    rabbit_port: int,
    redis_port: int,
):
    results_dir = "results"
    os.makedirs(results_dir, exist_ok=True)
    producer_output = f"{results_dir}/{broker}_{msg_size}_{rate}_prod.json"
    consumer_output = f"{results_dir}/{broker}_{msg_size}_{rate}_cons.json"
    queue_name = f"bench_q_{broker}_{msg_size}_{rate}_{uuid.uuid4().hex[:8]}"

    broker_port = rabbit_port if broker == "rabbitmq" else redis_port

    consumer_cmd = [
        "python",
        "consumer.py",
        "--broker",
        broker,
        "--host",
        host,
        "--port",
        str(broker_port),
        "--queue-name",
        queue_name,
        "--idle-timeout",
        "4",
        "--max-runtime",
        str(duration + 45),
        "--output",
        consumer_output,
    ]

    producer_cmd = [
        "python",
        "producer.py",
        "--broker",
        broker,
        "--msg-size",
        str(msg_size),
        "--rate",
        str(rate),
        "--duration",
        str(duration),
        "--host",
        host,
        "--port",
        str(broker_port),
        "--queue-name",
        queue_name,
        "--reset-queue",
        "--output",
        producer_output,
    ]

    print(f"[run] {broker} size={msg_size} rate={rate} duration={duration}s")
    consumer_proc = subprocess.Popen(consumer_cmd)
    time.sleep(2)
    try:
        subprocess.run(producer_cmd, check=True)
    finally:
        try:
            consumer_proc.wait(timeout=max(duration + 20, 40))
        except subprocess.TimeoutExpired:
            consumer_proc.terminate()
            try:
                consumer_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                consumer_proc.kill()
                consumer_proc.wait(timeout=5)

    with open(producer_output, encoding="utf-8") as file_obj:
        producer_data = json.load(file_obj)
    with open(consumer_output, encoding="utf-8") as file_obj:
        consumer_data = json.load(file_obj)

    sent = producer_data["sent"]
    received = consumer_data["received"]
    backlog = int(consumer_data.get("remaining_in_queue", 0))
    estimated_loss = max(sent - received - backlog, 0)
    estimated_loss_rate = (estimated_loss / sent) if sent > 0 else 0
    not_consumed_in_window = max(sent - received, 0)
    not_consumed_rate = (not_consumed_in_window / sent) if sent > 0 else 0
    degraded = (
        consumer_data["p95_latency_ms"] > DEGRADATION_LAT_THRESHOLD_MS
        or not_consumed_rate > DEGRADATION_LOSS_THRESHOLD
    )

    return {
        "Broker": broker,
        "MessageSizeBytes": msg_size,
        "TargetRateMsgS": rate,
        "ProducerThroughputMsgS": producer_data["actual_throughput_msg_s"],
        "ConsumerThroughputMsgS": consumer_data["throughput_msg_s"],
        "Sent": sent,
        "Received": received,
        "BacklogAtEnd": backlog,
        "NotConsumedInWindow": not_consumed_in_window,
        "NotConsumedPct": round(not_consumed_rate * 100, 2),
        "EstimatedLoss": estimated_loss,
        "EstimatedLossPct": round(estimated_loss_rate * 100, 2),
        "AvgLatencyMs": consumer_data["avg_latency_ms"],
        "P95LatencyMs": consumer_data["p95_latency_ms"],
        "ErrorsProducer": producer_data["errors"],
        "ErrorsConsumer": consumer_data["errors"],
        "Degraded": "YES" if degraded else "NO",
    }


def save_reports(results):
    if not results:
        raise RuntimeError("no successful runs to report")

    os.makedirs("results", exist_ok=True)
    csv_path = "results/comparison_report.csv"
    json_path = "results/comparison_report.json"

    with open(csv_path, "w", newline="", encoding="utf-8") as file_obj:
        writer = csv.DictWriter(file_obj, fieldnames=results[0].keys())
        writer.writeheader()
        writer.writerows(results)

    with open(json_path, "w", encoding="utf-8") as file_obj:
        json.dump(results, file_obj, indent=2)

    print("\n[summary]")
    print(tabulate(results, headers="keys", tablefmt="grid"))
    print(f"\nSaved: {csv_path}, {json_path}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--brokers", default="rabbitmq,redis")
    parser.add_argument("--sizes", default="128,1024,10240,102400")
    parser.add_argument("--rates", default="1000,5000,10000")
    parser.add_argument("--duration", type=int, default=30)
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--rabbit-port", type=int, default=5672)
    parser.add_argument("--redis-port", type=int, default=6379)
    args = parser.parse_args()

    brokers = [b.strip() for b in args.brokers.split(",") if b.strip()] or DEFAULT_BROKERS
    sizes = parse_csv_ints(args.sizes) or DEFAULT_SIZES
    rates = parse_csv_ints(args.rates) or DEFAULT_RATES

    results = []
    for broker in brokers:
        for size in sizes:
            for rate in rates:
                try:
                    result = run_single(
                        broker=broker,
                        msg_size=size,
                        rate=rate,
                        duration=args.duration,
                        host=args.host,
                        rabbit_port=args.rabbit_port,
                        redis_port=args.redis_port,
                    )
                    results.append(result)
                except Exception as exc:
                    print(f"[error] {broker}/{size}/{rate}: {exc}")

    save_reports(results)


if __name__ == "__main__":
    main()