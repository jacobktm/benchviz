"""API endpoints for hardware-adjusted ranking and discriminating benchmarks."""

from __future__ import annotations

from flask import request

from app.ml.discriminating_benchmarks import (
    find_discriminating_benchmarks,
    list_eligible_features,
)
from app.ml.hardware_ranking import (
    compute_system_overperformance,
    explain_ranking_sensors,
    list_rankable_benchmarks,
    rank_benchmark,
)
from app.repositories import BenchmarkRepository

from . import bp


@bp.route('/api/hardware_ranking/list')
def api_hardware_ranking_list():
    benchmarks = list_rankable_benchmarks()
    return {'benchmarks': benchmarks}, 200


@bp.route('/api/hardware_ranking/<benchmark_title>')
def api_hardware_ranking(benchmark_title: str):
    from urllib.parse import unquote

    title = unquote(benchmark_title).strip()
    app_version = (request.args.get('app_version') or '').strip()
    args_str = (request.args.get('args') or 'default').strip()

    if not title:
        return {'error': 'Missing benchmark_title'}, 400

    result = rank_benchmark(title, app_version, args_str)
    return result, 200


@bp.route('/api/system_overperformance/<int:system_id>')
def api_system_overperformance(system_id: int):
    result = compute_system_overperformance(system_id)
    return result, 200


@bp.route('/api/discriminating_benchmarks/eligible')
def api_discriminating_eligible():
    features = list_eligible_features()
    return {'features': features}, 200


@bp.route('/api/discriminating_benchmarks')
def api_discriminating_benchmarks():
    feature_key = (request.args.get('feature_key') or '').strip()
    value_a = (request.args.get('value_a') or '').strip()
    value_b = (request.args.get('value_b') or '').strip()
    args_str = (request.args.get('args') or 'default').strip()

    if not feature_key or not value_a or not value_b:
        return {'error': 'feature_key, value_a, and value_b are required'}, 400

    result = find_discriminating_benchmarks(feature_key, value_a, value_b, args_str)
    return result, 200


@bp.route('/api/hardware_ranking/<benchmark_title>/explain/<int:system_id>')
def api_ranking_sensor_explain(benchmark_title: str, system_id: int):
    from urllib.parse import unquote

    title = unquote(benchmark_title).strip()
    app_version = (request.args.get('app_version') or '').strip()
    args_str = (request.args.get('args') or 'default').strip()

    if not title:
        return {'error': 'Missing benchmark_title'}, 400

    result = explain_ranking_sensors(title, app_version, args_str, system_id=system_id)
    return result, 200
