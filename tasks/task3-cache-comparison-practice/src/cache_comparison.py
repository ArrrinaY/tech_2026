import random
import statistics
import time
from dataclasses import dataclass
from typing import Dict, List, Tuple


@dataclass
class Metrics:
    strategy: str
    profile: str
    requests: int
    duration_sec: float
    throughput_rps: float
    avg_latency_ms: float
    db_reads: int
    db_writes: int
    db_total: int
    cache_hits: int
    cache_misses: int
    cache_hit_rate: float
    max_pending_writes: int


class SimulatedDatabase:
    def __init__(self, initial_data: Dict[str, int], read_delay: float = 0.0015, write_delay: float = 0.0025):
        self.storage = dict(initial_data)
        self.read_delay = read_delay
        self.write_delay = write_delay
        self.read_count = 0
        self.write_count = 0

    def read(self, key: str) -> int:
        time.sleep(self.read_delay)
        self.read_count += 1
        return self.storage[key]

    def write(self, key: str, value: int) -> None:
        time.sleep(self.write_delay)
        self.write_count += 1
        self.storage[key] = value


class SimulatedCache:
    def __init__(self):
        self.storage: Dict[str, int] = {}
        self.hit_count = 0
        self.miss_count = 0

    def get(self, key: str):
        if key in self.storage:
            self.hit_count += 1
            return self.storage[key]
        self.miss_count += 1
        return None

    def set(self, key: str, value: int) -> None:
        self.storage[key] = value


class BaseApplication:
    def __init__(self, db: SimulatedDatabase, cache: SimulatedCache):
        self.db = db
        self.cache = cache

    def read(self, key: str) -> int:
        raise NotImplementedError

    def write(self, key: str, value: int) -> None:
        raise NotImplementedError

    def finalize(self) -> int:
        return 0


class CacheAsideApp(BaseApplication):
    name = "lazy-loading"

    def read(self, key: str) -> int:
        value = self.cache.get(key)
        if value is not None:
            return value
        value = self.db.read(key)
        self.cache.set(key, value)
        return value

    def write(self, key: str, value: int) -> None:
        self.db.write(key, value)


class WriteThroughApp(BaseApplication):
    name = "write-through"

    def read(self, key: str) -> int:
        value = self.cache.get(key)
        if value is not None:
            return value
        value = self.db.read(key)
        self.cache.set(key, value)
        return value

    def write(self, key: str, value: int) -> None:
        self.cache.set(key, value)
        self.db.write(key, value)


class WriteBackApp(BaseApplication):
    name = "write-back"

    def __init__(self, db: SimulatedDatabase, cache: SimulatedCache, flush_interval: float = 0.25, batch_size: int = 80):
        super().__init__(db, cache)
        self.flush_interval = flush_interval
        self.batch_size = batch_size
        self.pending: Dict[str, int] = {}
        self.pending_order: List[str] = []
        self.last_flush = time.perf_counter()
        self.max_pending = 0

    def read(self, key: str) -> int:
        value = self.cache.get(key)
        if value is not None:
            return value
        value = self.db.read(key)
        self.cache.set(key, value)
        return value

    def write(self, key: str, value: int) -> None:
        self.cache.set(key, value)
        if key not in self.pending:
            self.pending_order.append(key)
        self.pending[key] = value
        if len(self.pending) > self.max_pending:
            self.max_pending = len(self.pending)
        self._flush_if_needed()

    def _flush_if_needed(self) -> None:
        now = time.perf_counter()
        if len(self.pending) >= self.batch_size or (now - self.last_flush) >= self.flush_interval:
            self._flush_batch()

    def _flush_batch(self) -> None:
        if not self.pending:
            self.last_flush = time.perf_counter()
            return
        keys_to_flush = self.pending_order[: self.batch_size]
        self.pending_order = self.pending_order[self.batch_size :]
        for key in keys_to_flush:
            value = self.pending.pop(key)
            self.db.write(key, value)
        self.last_flush = time.perf_counter()

    def finalize(self) -> int:
        while self.pending:
            self._flush_batch()
        return self.max_pending


def build_workload(
    profile_name: str,
    read_ratio: float,
    requests_count: int,
    key_count: int,
    seed: int,
) -> List[Tuple[str, str, int]]:
    random.seed(seed)
    ops: List[Tuple[str, str, int]] = []
    for i in range(requests_count):
        key = f"user:{random.randint(1, key_count)}"
        if random.random() < read_ratio:
            ops.append(("read", key, 0))
        else:
            new_value = random.randint(100, 999) + i
            ops.append(("write", key, new_value))
    print(f"[workload] {profile_name}: requests={requests_count}, read_ratio={read_ratio:.0%}")
    return ops


def run_test(profile_name: str, ops: List[Tuple[str, str, int]], app_factory) -> Metrics:
    initial_data = {f"user:{i}": i for i in range(1, 401)}
    db = SimulatedDatabase(initial_data=initial_data)
    cache = SimulatedCache()
    app = app_factory(db, cache)

    latencies_ms: List[float] = []
    started = time.perf_counter()
    for operation, key, value in ops:
        op_started = time.perf_counter()
        if operation == "read":
            app.read(key)
        else:
            app.write(key, value)
        latencies_ms.append((time.perf_counter() - op_started) * 1000)

    max_pending = app.finalize()
    duration_sec = time.perf_counter() - started
    avg_latency_ms = statistics.fmean(latencies_ms)
    throughput = len(ops) / duration_sec
    db_total = db.read_count + db.write_count
    requests_to_cache = cache.hit_count + cache.miss_count
    hit_rate = (cache.hit_count / requests_to_cache) if requests_to_cache else 0.0

    return Metrics(
        strategy=app.name,
        profile=profile_name,
        requests=len(ops),
        duration_sec=duration_sec,
        throughput_rps=throughput,
        avg_latency_ms=avg_latency_ms,
        db_reads=db.read_count,
        db_writes=db.write_count,
        db_total=db_total,
        cache_hits=cache.hit_count,
        cache_misses=cache.miss_count,
        cache_hit_rate=hit_rate,
        max_pending_writes=max_pending,
    )


def print_results(metrics: List[Metrics]) -> None:
    print("\n=== RESULTS ===")
    header = (
        f"{'strategy':<14} {'profile':<12} {'req':>6} {'sec':>8} {'rps':>10} "
        f"{'lat_ms':>10} {'db_total':>10} {'hit_rate':>10} {'max_queue':>10}"
    )
    print(header)
    print("-" * len(header))
    for m in metrics:
        print(
            f"{m.strategy:<14} {m.profile:<12} {m.requests:>6} {m.duration_sec:>8.3f} "
            f"{m.throughput_rps:>10.1f} {m.avg_latency_ms:>10.3f} {m.db_total:>10} "
            f"{m.cache_hit_rate:>10.2%} {m.max_pending_writes:>10}"
        )


def main() -> None:
    profiles = [
        ("read-heavy", 0.8, 5000),
        ("balanced", 0.5, 5000),
        ("write-heavy", 0.2, 5000),
    ]
    strategies = [
        CacheAsideApp,
        WriteThroughApp,
        WriteBackApp,
    ]
    all_results: List[Metrics] = []

    for profile_index, (profile_name, read_ratio, requests_count) in enumerate(profiles):
        ops = build_workload(
            profile_name=profile_name,
            read_ratio=read_ratio,
            requests_count=requests_count,
            key_count=400,
            seed=2026 + profile_index,
        )
        for strategy in strategies:
            print(f"[run] profile={profile_name}, strategy={strategy.name}")
            result = run_test(profile_name, ops, app_factory=strategy)
            all_results.append(result)
            print(
                f"  -> rps={result.throughput_rps:.1f}, lat={result.avg_latency_ms:.3f}ms, "
                f"db={result.db_total}, hit={result.cache_hit_rate:.2%}, queue={result.max_pending_writes}"
            )

    print_results(all_results)


if __name__ == "__main__":
    main()
