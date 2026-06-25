"""Thread-safe in-memory metrics store for request tracking."""

from __future__ import annotations

import time
from collections import deque
from datetime import datetime, timezone
from threading import Lock
from typing import Any


class EndpointStats:
    __slots__ = (
        'count', 'total_duration', 'max_duration',
        'query_count', 'status_2xx', 'status_4xx', 'status_5xx',
    )

    def __init__(self):
        self.count: int = 0
        self.total_duration: float = 0.0
        self.max_duration: float = 0.0
        self.query_count: int = 0
        self.status_2xx: int = 0
        self.status_4xx: int = 0
        self.status_5xx: int = 0

    def record(self, duration: float, query_count: int, status_code: int) -> None:
        self.count += 1
        self.total_duration += duration
        if duration > self.max_duration:
            self.max_duration = duration
        self.query_count += query_count
        if 200 <= status_code < 300:
            self.status_2xx += 1
        elif 400 <= status_code < 500:
            self.status_4xx += 1
        elif status_code >= 500:
            self.status_5xx += 1

    @property
    def avg_duration(self) -> float:
        return self.total_duration / self.count if self.count else 0.0

    def snapshot(self) -> dict[str, Any]:
        return {
            'count': self.count,
            'avg_duration_ms': round(self.avg_duration * 1000, 1),
            'max_duration_ms': round(self.max_duration * 1000, 1),
            'total_duration_ms': round(self.total_duration * 1000, 1),
            'avg_queries': round(self.query_count / self.count, 1) if self.count else 0,
            'total_queries': self.query_count,
            'status_2xx': self.status_2xx,
            'status_4xx': self.status_4xx,
            'status_5xx': self.status_5xx,
        }


class CacheCounters:
    __slots__ = ('hits', 'misses')

    def __init__(self):
        self.hits: int = 0
        self.misses: int = 0

    def hit(self) -> None:
        self.hits += 1

    def miss(self) -> None:
        self.misses += 1

    @property
    def total(self) -> int:
        return self.hits + self.misses

    @property
    def hit_rate(self) -> float | None:
        t = self.total
        return self.hits / t if t else None

    def snapshot(self) -> dict[str, Any]:
        return {
            'hits': self.hits,
            'misses': self.misses,
            'total': self.total,
            'hit_rate': round(self.hit_rate, 4) if self.hit_rate is not None else None,
        }


class AppMetrics:
    def __init__(self):
        self._lock = Lock()
        self.start_time: datetime = datetime.now(timezone.utc)
        self.total_requests: int = 0
        self._endpoints: dict[str, EndpointStats] = {}
        self._slow_requests: deque[dict[str, Any]] = deque(maxlen=100)
        self.ob_cache: CacheCounters = CacheCounters()
        self.signals_cache: CacheCounters = CacheCounters()
        self.slow_query_threshold: float = 0.2  # 200ms

    def record_request(
        self, endpoint: str, duration: float,
        query_count: int, status_code: int,
    ) -> None:
        with self._lock:
            self.total_requests += 1
            stats = self._endpoints.get(endpoint)
            if stats is None:
                stats = EndpointStats()
                self._endpoints[endpoint] = stats
            stats.record(duration, query_count, status_code)

            if duration > self.slow_query_threshold:
                self._slow_requests.append({
                    'endpoint': endpoint,
                    'duration_ms': round(duration * 1000, 1),
                    'query_count': query_count,
                    'status': status_code,
                    'timestamp': datetime.now(timezone.utc).isoformat(),
                })

    def get_endpoint_stats(self, endpoint: str) -> EndpointStats | None:
        with self._lock:
            return self._endpoints.get(endpoint)

    def snapshot_endpoints(self) -> dict[str, Any]:
        with self._lock:
            return {
                name: stats.snapshot()
                for name, stats in sorted(self._endpoints.items())
            }

    def snapshot_slow(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._slow_requests)[-limit:]

    def overview(self) -> dict[str, Any]:
        with self._lock:
            uptime = datetime.now(timezone.utc) - self.start_time
            total_duration = sum(s.total_duration for s in self._endpoints.values())
            total_queries = sum(s.query_count for s in self._endpoints.values())
            endpoint_count = len(self._endpoints)
        return {
            'uptime_seconds': int(uptime.total_seconds()),
            'total_requests': self.total_requests,
            'endpoint_count': endpoint_count,
            'total_duration_ms': round(total_duration * 1000, 1),
            'total_queries': total_queries,
            'avg_duration_ms': round(total_duration / self.total_requests * 1000, 1) if self.total_requests else 0,
            'avg_queries_per_request': round(total_queries / self.total_requests, 1) if self.total_requests else 0,
            'ob_cache': self.ob_cache.snapshot(),
            'signals_cache': self.signals_cache.snapshot(),
        }


_metrics = AppMetrics()


def get_metrics() -> AppMetrics:
    return _metrics
