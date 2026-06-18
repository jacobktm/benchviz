# BenchViz — Agent Guide

## Project Overview

BenchViz is a Flask-based web application for visualizing and analyzing Phoronix Test Suite (PTS) benchmark results. It ingests XML benchmark files from PTS, stores them in SQLite, and provides a dashboard, system detail views, interactive comparison charts (Plotly.js), and ML-powered performance insights (workload fingerprinting, score attribution via ElasticNet, thermal sensitivity detection).

## Technology Stack

- **Language**: Python 3.12+
- **Web framework**: Flask 3.x
- **ORM**: Flask-SQLAlchemy 3.x / SQLAlchemy 2.x
- **Database**: SQLite (WAL mode, busy timeout 30s)
- **XML parsing**: lxml 5.x
- **ML / statistics**: scikit-learn 1.3+, numpy 1.24+
- **Frontend**: Jinja2 templates, CSS3 (glassmorphism, dark theme), Plotly.js 2.32
- **Testing**: Python `unittest` (unit tests) + Playwright (E2E browser tests)
- **No JS framework**: plain HTML + CSS + Plotly.js with vanilla JS in template `<script>` blocks

## Key Commands

| Command | Purpose |
|---|---|
| `python app_main.py` | Run Flask dev server |
| `python -m flask rebuild-all-insights` | Rebuild legacy + ML insights |
| `python -m flask sync-openbenchmarking-cache` | Sync OB cache from git mirror |
| `python -m flask calibrate-hardware-ranks` | Blend spec ranks with real-world results |
| `python test_<name>.py` | Run a specific unit test file |

**Environment variables**: `BENCHVIZ_OB_LIVE_ON_COMPARE`, `BENCHVIZ_OB_LIVE_FETCH`, `BENCHVIZ_INSIGHTS_REBUILD_FULL`, `BENCHVIZ_DEBUG`, `BENCHVIZ_RELOADER`, `BENCHVIZ_HOST`, `BENCHVIZ_PORT`.

## Project Structure

```
app/                  # Main application package
  __init__.py         # Flask app factory, SQLAlchemy init, schema migrations
  models.py           # ORM models (System, Benchmark, BenchmarkResult, etc.)
  parser.py           # Phoronix XML benchmark file parser
  analyzer.py         # Legacy statistical analysis
  _util.py            # Misc helpers (flask CLI, generic utilities)
  benchmark_util.py   # Benchmark lookup / creation / orphan cleanup
  components.py       # Hardware/software component extraction + normalization
  system_util.py      # System identity, hardware fingerprinting, import resolution
  hardware_slug.py    # Compact hardware identifier slug builder (+ disk abbreviation)
  hardware_ranks.py   # Kendall tau rank correlation, CPU/GPU rank lookups
  hardware_ranks_calibrate.py # Blend spec ranks with real benchmark values
  hardware_ranks_api_sync.py  # Sync HW ranks from external API
  result_merge.py     # BAR_GRAPH / LINE_GRAPH result merging logic
  profile_snapshot.py # Capture system profile at import time
  insights_util.py    # Incremental rebuild helpers
  insights_runner.py  # Scheduled insights rebuild orchestration
  insights_lock.py    # File-based lock for insights rebuild
  pts_math.py         # Geometric/harmonic mean (ported from PHP PTS)
  sensor_quality.py   # Detect low-signal / flat MONITOR sensor series
  args_pooling.py     # Argument pooling utilities
  option_equivalence.py # Benchmark option equivalence detection
  workload_consensus.py # Consensus logic across argument variations

  cli/                # Flask CLI commands (registered via app.cli)
    commands.py

  ml/                 # Machine learning analysis sub-package
    __init__.py
    analyzer.py       # Batch ML analysis orchestrator
    features.py       # Feature extraction (sensors, perf counters, hardware)
    sensor_baselines.py # Per-hardware sensor idle/load baseline learning
    workload.py       # Workload fingerprinting (CPU/GPU/cache/memory/storage)
    attribution.py    # Score attribution via ElasticNet regression + LOOCV
    thermal.py        # Thermal sensitivity detection (temp vs score residuals)

  ob_cache_sync/      # OpenBenchmarking cache sync + lookup (package)
    __init__.py       # Re-exports public API
    _paths.py         # Cache directory, TTL constants
    _sync.py          # Git mirror sync logic
    _data.py          # Cache data read/write
    _lookup.py        # OB benchmark lookup with live-fetch fallback

  pts/                # PTS comparison utilities (package)
    __init__.py       # Lazy-imports compare module to avoid circular deps
    hashing.py        # PTS comparison hash generation (was pts_comparison.py)
    compare.py        # Build full PTS-style comparison payloads (was pts_compare.py)
    math_aggregation.py # PTS math helpers (geometric/harmonic mean)
    ob_baselines.py   # OB baseline normalization

  repositories/       # Data access layer
    benchmark_repo.py # BenchmarkRepository (find_primary_with_results, etc.)
    system_repo.py    # SystemRepository (get_by_id_or_404, etc.)

  route_helpers/      # View helper logic, extracted from app_main.py
    compare.py        # Comparison serialization, geometric mean, COMPARE_BY_OPTIONS
    insights.py       # Insights scoping, signal-to-noise, workload context
    system.py         # Profile labels, nvme config sync, system grouping
    string_utils.py   # Shared string formatting utilities

  routes/             # Flask route Blueprints
    __init__.py       # Registers all blueprints
    pages.py          # HTML page routes (dashboard, upload, compare, system, insights)
    export.py         # PDF/DOCX export routes
    api/              # JSON API Blueprint (package)
      __init__.py     # Blueprint creation
      benchmarks.py   # api_common_benchmarks, api_systems_for_benchmark, etc.
      compare.py      # api_compare, api_save_comparison, api_pool_flag_suggestions
      insights.py     # api_scatter_candidates, api_variance_feature_map, etc.

  static/
    css/style.css     # Main stylesheet (glassmorphism, dark theme)
    js/main.js        # Minimal vanilla JS

  templates/          # Jinja2 templates extending base.html
    dashboard.html
    system.html
    compare.html
    insights.html
    upload.html
    saved_comparisons.html
    export_slides.html

  workload_profile/   # Workload characterization from perf counters + MONITOR (package)
    __init__.py       # Public API: build_profile, classify_workload_profile, ...
    _constants.py     # SCOPE_HARDWARE_KEYS, sensor keywords, perf markers
    _helpers.py       # format_score, _args_matches_config, _monitor_result_matches_config
    _signals.py       # Signal collection (perf counter extraction, sensor extraction)
    _classification.py # Bottleneck classification, scope inference

app_main.py           # Flask application entrypoint (imports routes, cli, app factory)
requirements.txt      # Dependencies
setup.sh              # Environment setup (venv, deps, systemd service)
test_*.py             # Unit tests and test utilities
```

## Database Models

- **System**: hardware/software profile with editable fields (chassis, cooler, PSU, fans, serial number, notes)
- **SystemNvmeConfig**: per-drive NVMe configuration (slot, detected name, thermal pads)
- **Benchmark**: benchmark definition (identifier, title, description, scale, proportion, display format)
- **BenchmarkResult**: a single result value for a benchmark on a system (arguments, value, data_json for line graphs)
- **BenchmarkAnalysis**: cached analysis results stored as JSON blob
- **HardwareTheoreticalRank**: reference CPU/GPU performance scores
- **SavedComparison**: persisted comparison configurations (JSON payload, short slug ID)

## Coding Conventions

- **Naming**: snake_case for modules/functions/variables, PascalCase for classes, UPPER_CASE for constants
- **Type hints**: Python 3.10+ style (`str | None`, `dict[str, Any]`, `list[float]`) with `from __future__ import annotations`
- **Schema migration**: handled imperatively in `ensure_schema_compatibility()` via raw SQL (no Alembic)
- **Flask app factory**: `create_app()` in `app/__init__.py`
- **URL routes**: lowercase with underscores or hyphens, prefixed with `/api/` for JSON endpoints
- **Incremental analysis**: `benchmark_group_needs_rebuild()` skips groups whose data hasn't changed
- **File-based locking**: POSIX `flock` via `insights_lock.py` prevents concurrent insight rebuilds
- **Import resolution**: `resolve_system_for_import()` uses hardware fingerprinting + serial numbers to match/merge/deduplicate
- **OB integration**: PTS comparison hash generation, OB cache lookup with live-fetch fallback
- **Tests**: standard `unittest.TestCase`, run by executing `test_*.py` files directly. Playwright E2E tests use `playwright.sync_api` against `http://127.0.0.1:8765`.
- **No linting/formatter configs** are currently present in the repo.
