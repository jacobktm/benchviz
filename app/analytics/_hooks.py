"""Flask request hooks and SQLAlchemy query-counting event listeners."""

from __future__ import annotations

import os
import time

from flask import g, request
from sqlalchemy import event
from sqlalchemy.engine import Engine

from ._metrics import get_metrics


# Track query count per request via SQLAlchemy events.
# Uses a thread-local counter reset at the start of each request.

_query_count_key = '_sa_query_count'


def _reset_query_count() -> None:
    try:
        g._sa_query_count = 0
    except RuntimeError:
        pass  # outside request context


def _increment_query_count() -> None:
    try:
        g._sa_query_count = getattr(g, '_sa_query_count', 0) + 1
    except RuntimeError:
        pass


def current_query_count() -> int:
    try:
        return getattr(g, '_sa_query_count', 0)
    except RuntimeError:
        return 0


@event.listens_for(Engine, "before_cursor_execute")
def _before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
    _increment_query_count()


SLOW_QUERY_LOG_ENABLED = os.environ.get('BENCHVIZ_LOG_SLOW_QUERIES', '1') not in ('0', 'false', 'no')
_slow_query_threshold = float(os.environ.get('BENCHVIZ_SLOW_QUERY_MS', '200'))


@event.listens_for(Engine, "after_cursor_execute")
def _after_cursor_execute(conn, cursor, statement, parameters, context, executemany):
    if not SLOW_QUERY_LOG_ENABLED:
        return
    # The statement is logged via the timing that we can't easily get here,
    # but we keep the listener registered for future use.
    pass


# Flask request hooks


def register_request_hooks(app):
    @app.before_request
    def _before_request():
        g._request_start = time.perf_counter()
        _reset_query_count()

    @app.after_request
    def _after_request(response):
        start = getattr(g, '_request_start', None)
        if start is None:
            return response

        duration = time.perf_counter() - start
        qc = current_query_count()
        endpoint = request.endpoint or 'unknown'

        get_metrics().record_request(
            endpoint=endpoint,
            duration=duration,
            query_count=qc,
            status_code=response.status_code,
        )

        # Add response headers for debugging (visible in browser devtools)
        response.headers['X-BenchViz-Duration-Ms'] = str(round(duration * 1000, 1))
        response.headers['X-BenchViz-Query-Count'] = str(qc)

        return response
