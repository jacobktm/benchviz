from flask import Flask
import os
import sqlite3
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import event, inspect, text
from sqlalchemy.engine import Engine

db = SQLAlchemy()


@event.listens_for(Engine, "connect")
def _configure_sqlite_connection(dbapi_connection, connection_record):
    """Allow concurrent reads during background writes (WAL + busy wait)."""
    if not isinstance(dbapi_connection, sqlite3.Connection):
        return
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=30000")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()

def ensure_schema_compatibility():
    """Backfill lightweight schema changes for existing local SQLite DBs."""
    inspector = inspect(db.engine)
    table_names = inspector.get_table_names()
    if 'systems' not in table_names:
        return

    system_columns = {column['name'] for column in inspector.get_columns('systems')}
    system_column_definitions = {
        'custom_hardware': "ALTER TABLE systems ADD COLUMN custom_hardware VARCHAR(255)",
        'primary_system_name': "ALTER TABLE systems ADD COLUMN primary_system_name VARCHAR(255)",
        'cooler_model': "ALTER TABLE systems ADD COLUMN cooler_model VARCHAR(255)",
        'psu_model': "ALTER TABLE systems ADD COLUMN psu_model VARCHAR(255)",
        'psu_wattage': "ALTER TABLE systems ADD COLUMN psu_wattage VARCHAR(100)",
        'external_off': "ALTER TABLE systems ADD COLUMN external_off BOOLEAN NOT NULL DEFAULT 0",
        'gpu_fans': "ALTER TABLE systems ADD COLUMN gpu_fans BOOLEAN NOT NULL DEFAULT 0",
        'memory_fans': "ALTER TABLE systems ADD COLUMN memory_fans BOOLEAN NOT NULL DEFAULT 0",
        'nvme_fans': "ALTER TABLE systems ADD COLUMN nvme_fans BOOLEAN NOT NULL DEFAULT 0",
        'manual_notes': "ALTER TABLE systems ADD COLUMN manual_notes TEXT",
        'serial_number': "ALTER TABLE systems ADD COLUMN serial_number VARCHAR(128)",
    }

    with db.engine.begin() as connection:
        for column_name, statement in system_column_definitions.items():
            if column_name not in system_columns:
                connection.execute(text(statement))

        if 'system_nvme_configs' not in table_names:
            connection.execute(text(
                """
                CREATE TABLE system_nvme_configs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    system_id INTEGER NOT NULL,
                    slot_name VARCHAR(100) NOT NULL,
                    detected_name VARCHAR(255),
                    top_thermal_pad BOOLEAN NOT NULL DEFAULT 0,
                    bottom_thermal_pad BOOLEAN NOT NULL DEFAULT 0,
                    notes TEXT,
                    FOREIGN KEY(system_id) REFERENCES systems (id)
                )
                """
            ))

        if 'benchmark_analyses' not in table_names:
            connection.execute(text(
                """
                CREATE TABLE benchmark_analyses (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    benchmark_identifier VARCHAR(255) NOT NULL,
                    benchmark_title VARCHAR(255) NOT NULL,
                    benchmark_app_version VARCHAR(100),
                    last_updated DATETIME DEFAULT CURRENT_TIMESTAMP,
                    analysis_json JSON NOT NULL,
                    UNIQUE(benchmark_identifier, benchmark_title, benchmark_app_version)
                )
                """
            ))

        benchmark_columns = {column['name'] for column in inspector.get_columns('benchmarks')}
        if 'is_primary' not in benchmark_columns:
            connection.execute(text("ALTER TABLE benchmarks ADD COLUMN is_primary BOOLEAN NOT NULL DEFAULT 0"))
            # Backfill based on display_format so existing data is usable immediately.
            connection.execute(text(
                "UPDATE benchmarks SET is_primary = CASE WHEN display_format = 'BAR_GRAPH' THEN 1 ELSE 0 END"
            ))

        if 'hardware_theoretical_ranks' not in table_names:
            connection.execute(text(
                """
                CREATE TABLE hardware_theoretical_ranks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    part_kind VARCHAR(8) NOT NULL,
                    match_key VARCHAR(256) NOT NULL,
                    rank_value FLOAT NOT NULL,
                    rank_value_spec FLOAT,
                    display_label VARCHAR(512),
                    source_note VARCHAR(255),
                    UNIQUE (part_kind, match_key)
                )
                """
            ))

    inspector2 = inspect(db.engine)
    if 'hardware_theoretical_ranks' in inspector2.get_table_names():
        hw_rank_columns = {c['name'] for c in inspector2.get_columns('hardware_theoretical_ranks')}
        if 'rank_value_spec' not in hw_rank_columns:
            with db.engine.begin() as connection:
                connection.execute(text(
                    "ALTER TABLE hardware_theoretical_ranks ADD COLUMN rank_value_spec FLOAT"
                ))
                connection.execute(text(
                    "UPDATE hardware_theoretical_ranks SET rank_value_spec = rank_value "
                    "WHERE rank_value_spec IS NULL"
                ))

    if 'spec_field_schemas' not in table_names:
        with db.engine.begin() as connection:
            connection.execute(text(
                """
                CREATE TABLE spec_field_schemas (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    blob_column VARCHAR(20) NOT NULL,
                    field_name VARCHAR(100) NOT NULL,
                    label VARCHAR(255) NOT NULL,
                    field_type VARCHAR(10) NOT NULL DEFAULT 'text',
                    hint TEXT,
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    required BOOLEAN NOT NULL DEFAULT 0,
                    UNIQUE(blob_column, field_name)
                )
                """
            ))
            _seed_default_spec_schemas(connection)

    if 'hardware_specs' not in table_names:
        with db.engine.begin() as connection:
            connection.execute(text(
                """
                CREATE TABLE hardware_specs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    system_id INTEGER NOT NULL UNIQUE,
                    cpu_model VARCHAR(255),
                    cpu_cores INTEGER,
                    cpu_threads INTEGER,
                    gpu_model VARCHAR(255),
                    cpu_spec JSON,
                    gpu_spec JSON,
                    memory_spec JSON,
                    storage_spec JSON,
                    source VARCHAR(50) NOT NULL DEFAULT 'auto',
                    extra_json JSON,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(system_id) REFERENCES systems (id)
                )
                """
            ))

    inspector3 = inspect(db.engine)
    if 'benchmark_results' in inspector3.get_table_names():
        with db.engine.begin() as connection:
            _migrate_benchmark_results_v2(connection, inspector3)
        with db.engine.begin() as connection:
            connection.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_benchmark_results_bm_sys "
                "ON benchmark_results (benchmark_id, system_id)"
            ))


def _migrate_benchmark_results_v2(connection, inspector) -> None:
    """
    Allow multiple upload runs per (system, benchmark, arguments).
    Adds import_batch_id + profile_snapshot; drops legacy unique constraint.
    """
    if 'benchmark_results' not in inspector.get_table_names():
        return
    br_cols = {c['name'] for c in inspector.get_columns('benchmark_results')}
    if 'import_batch_id' in br_cols:
        return

    connection.execute(text(
        """
        CREATE TABLE benchmark_results_new (
            id INTEGER PRIMARY KEY,
            system_id INTEGER NOT NULL,
            benchmark_id INTEGER NOT NULL,
            arguments TEXT,
            value FLOAT,
            data_json JSON,
            imported_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            import_batch_id VARCHAR(36),
            profile_snapshot JSON,
            FOREIGN KEY(system_id) REFERENCES systems (id),
            FOREIGN KEY(benchmark_id) REFERENCES benchmarks (id)
        )
        """
    ))
    connection.execute(text(
        """
        INSERT INTO benchmark_results_new (
            id, system_id, benchmark_id, arguments, value, data_json,
            imported_at, import_batch_id, profile_snapshot
        )
        SELECT
            id, system_id, benchmark_id, arguments, value, data_json,
            CURRENT_TIMESTAMP,
            'legacy-' || id,
            NULL
        FROM benchmark_results
        """
    ))
    connection.execute(text("DROP TABLE benchmark_results"))
    connection.execute(text("ALTER TABLE benchmark_results_new RENAME TO benchmark_results"))
    connection.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_benchmark_results_import_batch_id "
        "ON benchmark_results (import_batch_id)"
    ))

def _seed_default_spec_schemas(connection):
    """Populate the initial spec field schema table."""
    defaults = [
        # cpu_spec
        ('cpu_spec', 'arch_family', 'Microarchitecture', 'text', 'e.g. zen_5, raptor_cove, arrow_lake', 10),
        ('cpu_spec', 'clusters', 'Core Clusters', 'json', 'Array of {type, cores, threads, base_clock_mhz, boost_clock_mhz, tdp_watts}', 20),
        ('cpu_spec', 'boost_clock_mhz', 'Boost Clock (MHz)', 'number', 'Maximum single-core boost', 30),
        ('cpu_spec', 'base_clock_mhz', 'Base Clock (MHz)', 'number', 'Sustained clock under all-core load', 40),
        ('cpu_spec', 'tdp_pl1_watts', 'TDP PL1 (W)', 'number', 'Sustained power limit', 50),
        ('cpu_spec', 'tdp_pl2_watts', 'TDP PL2 (W)', 'number', 'Boost power limit', 60),
        ('cpu_spec', 'tdp_watts', 'TDP Class (W)', 'number', 'Thermal design power', 70),
        ('cpu_spec', 'l3_cache_kb', 'L3 Cache (KB)', 'number', 'Last-level cache', 80),
        ('cpu_spec', 'l2_cache_kb', 'L2 Cache (KB)', 'number', 'Per-core L2 cache', 90),
        # gpu_spec
        ('gpu_spec', 'vram_mb', 'VRAM (MB)', 'number', 'Video memory', 10),
        ('gpu_spec', 'shader_count', 'Shader / CUDA Cores', 'number', 'Universal shader count', 20),
        ('gpu_spec', 'boost_clock_mhz', 'Boost Clock (MHz)', 'number', 'Maximum boost clock', 30),
        ('gpu_spec', 'core_clock_mhz', 'Core Clock (MHz)', 'number', 'Base core clock', 40),
        ('gpu_spec', 'tdp_watts', 'TDP (W)', 'number', 'Thermal design power', 50),
        ('gpu_spec', 'tensor_cores', 'Tensor / AI Cores', 'number', 'Tensor / RT / AI accelerator count', 60),
        # memory_spec
        ('memory_spec', 'size_mb', 'Size (MB)', 'number', 'Total system memory', 10),
        ('memory_spec', 'type', 'Type', 'text', 'DDR4 / DDR5 / LPDDR5', 20),
        ('memory_spec', 'speed_mhz', 'Speed (MHz)', 'number', 'Memory clock rate', 30),
        ('memory_spec', 'channels', 'Channels', 'number', 'Memory channel count', 40),
        ('memory_spec', 'rank', 'Rank', 'text', 'Single / Dual / Quad', 50),
    ]
    for blob_col, field, label, ftype, hint, sort_order in defaults:
        connection.execute(
            text("""
                INSERT INTO spec_field_schemas (blob_column, field_name, label, field_type, hint, sort_order)
                VALUES (:bc, :fn, :lb, :ft, :hi, :so)
            """),
            {'bc': blob_col, 'fn': field, 'lb': label, 'ft': ftype, 'hi': hint, 'so': sort_order},
        )


def create_app():
    app = Flask(__name__)
    app.secret_key = os.environ.get('BENCHVIZ_SECRET_KEY', 'super-secret-benchmark-key')
    # Use an absolute path so the DB is consistent regardless of current working directory
    # (important for systemd service vs CLI commands).
    # Allow explicit override via env var (e.g. systemd service or production deploy).
    db_path = os.environ.get('BENCHVIZ_DB_PATH')
    if not db_path:
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        instance_dir = os.path.join(project_root, 'instance')
        os.makedirs(instance_dir, exist_ok=True)
        db_path = os.path.join(instance_dir, 'benchmarks.db')
    app.config['SQLALCHEMY_DATABASE_URI'] = f"sqlite:///{db_path}"
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
        'connect_args': {'timeout': 30},
    }

    db.init_app(app)

    from app.analytics import register_analytics
    register_analytics(app)

    from app.routes.pages import bp as pages_bp
    from app.routes.api import bp as api_bp
    from app.routes.export import bp as export_bp
    app.register_blueprint(pages_bp)
    app.register_blueprint(api_bp)
    app.register_blueprint(export_bp)

    from app.cli import register_commands
    register_commands(app)

    with app.app_context():
        db.create_all()
        ensure_schema_compatibility()
    
    return app
