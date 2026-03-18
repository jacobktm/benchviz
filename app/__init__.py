from flask import Flask
import os
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import inspect, text

db = SQLAlchemy()

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

def create_app():
    app = Flask(__name__)
    # Use an absolute path so the DB is consistent regardless of current working directory
    # (important for systemd service vs CLI commands).
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    instance_dir = os.path.join(project_root, 'instance')
    os.makedirs(instance_dir, exist_ok=True)
    db_path = os.path.join(instance_dir, 'benchmarks.db')
    app.config['SQLALCHEMY_DATABASE_URI'] = f"sqlite:///{db_path}"
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    
    db.init_app(app)

    with app.app_context():
        db.create_all()
        ensure_schema_compatibility()
    
    return app
