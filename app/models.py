from . import db

class System(db.Model):
    __tablename__ = 'systems'
    
    id = db.Column(db.Integer, primary_key=True)
    identifier = db.Column(db.String(255), nullable=False)
    hardware = db.Column(db.Text, nullable=False)
    software = db.Column(db.Text, nullable=False)
    user = db.Column(db.String(100))
    timestamp = db.Column(db.String(100))
    primary_system_name = db.Column(db.String(255), nullable=True)
    serial_number = db.Column(db.String(128), nullable=True)
    chassis_version = db.Column(db.String(100), nullable=True) # Manually updateable
    custom_hardware = db.Column(db.String(255), nullable=True) # Manually updateable (e.g. CPU Coolers)
    cooler_model = db.Column(db.String(255), nullable=True)
    psu_model = db.Column(db.String(255), nullable=True)
    psu_wattage = db.Column(db.String(100), nullable=True)
    external_off = db.Column(db.Boolean, nullable=False, default=False)
    gpu_fans = db.Column(db.Boolean, nullable=False, default=False)
    memory_fans = db.Column(db.Boolean, nullable=False, default=False)
    nvme_fans = db.Column(db.Boolean, nullable=False, default=False)
    manual_notes = db.Column(db.Text, nullable=True)
    
    results = db.relationship('BenchmarkResult', back_populates='system', cascade='all, delete-orphan')
    nvme_configs = db.relationship('SystemNvmeConfig', back_populates='system', cascade='all, delete-orphan')

class SystemNvmeConfig(db.Model):
    __tablename__ = 'system_nvme_configs'

    id = db.Column(db.Integer, primary_key=True)
    system_id = db.Column(db.Integer, db.ForeignKey('systems.id'), nullable=False)
    slot_name = db.Column(db.String(100), nullable=False)
    detected_name = db.Column(db.String(255), nullable=True)
    top_thermal_pad = db.Column(db.Boolean, nullable=False, default=False)
    bottom_thermal_pad = db.Column(db.Boolean, nullable=False, default=False)
    notes = db.Column(db.Text, nullable=True)

    system = db.relationship('System', back_populates='nvme_configs')

class Benchmark(db.Model):
    __tablename__ = 'benchmarks'
    
    id = db.Column(db.Integer, primary_key=True)
    identifier = db.Column(db.String(255))
    title = db.Column(db.String(255), nullable=False)
    app_version = db.Column(db.String(100))
    description = db.Column(db.Text)
    scale = db.Column(db.String(50))
    proportion = db.Column(db.String(10))
    display_format = db.Column(db.String(50))
    is_primary = db.Column(db.Boolean, nullable=False, default=False)
    
    # Avoid duplicate benchmark definitions
    __table_args__ = (
        db.UniqueConstraint('identifier', 'title', 'app_version', 'description', 'scale', name='uix_benchmark_def'),
    )
    
    results = db.relationship('BenchmarkResult', back_populates='benchmark', cascade='all, delete-orphan')

class BenchmarkResult(db.Model):
    __tablename__ = 'benchmark_results'
    
    id = db.Column(db.Integer, primary_key=True)
    system_id = db.Column(db.Integer, db.ForeignKey('systems.id'), nullable=False)
    benchmark_id = db.Column(db.Integer, db.ForeignKey('benchmarks.id'), nullable=False)
    
    arguments = db.Column(db.Text)
    value = db.Column(db.Float, nullable=True) # For BAR_GRAPH (scalar values)
    data_json = db.Column(db.JSON, nullable=True) # For LINE_GRAPH (arrays/lists of values)
    imported_at = db.Column(db.DateTime, server_default=db.func.now(), nullable=False)
    import_batch_id = db.Column(db.String(36), nullable=True, index=True)
    profile_snapshot = db.Column(db.JSON, nullable=True)
    
    system = db.relationship('System', back_populates='results')
    benchmark = db.relationship('Benchmark', back_populates='results')

class BenchmarkAnalysis(db.Model):
    __tablename__ = 'benchmark_analyses'

    id = db.Column(db.Integer, primary_key=True)
    benchmark_identifier = db.Column(db.String(255), nullable=False)
    benchmark_title = db.Column(db.String(255), nullable=False)
    benchmark_app_version = db.Column(db.String(100), nullable=True)
    last_updated = db.Column(db.DateTime, server_default=db.func.now(), onupdate=db.func.now())
    analysis_json = db.Column(db.JSON, nullable=False)

    __table_args__ = (
        db.UniqueConstraint('benchmark_identifier', 'benchmark_title', 'benchmark_app_version', name='_benchmark_analysis_uc'),
    )


class HardwareTheoreticalRank(db.Model):
    """
    Reference CPU/GPU performance ordering (from your external rankings / scores).

    `rank_value`: score used for reference ordering (Kendall τ, etc.); may be **calibrated**
    from real benchmark results. Higher = faster / more capable.

    `rank_value_spec`: baseline from your parts API / JSON (spec-only). Preserved when you run
    `flask calibrate-hardware-ranks` so you can re-blend after new uploads.
    `match_key`: normalized name; must match `hardware_rank_match_key()` in components.py.
    """
    __tablename__ = "hardware_theoretical_ranks"

    id = db.Column(db.Integer, primary_key=True)
    part_kind = db.Column(db.String(8), nullable=False)  # "cpu" | "gpu"
    match_key = db.Column(db.String(256), nullable=False)
    rank_value = db.Column(db.Float, nullable=False)
    rank_value_spec = db.Column(db.Float, nullable=True)  # baseline before empirical calibration
    display_label = db.Column(db.String(512), nullable=True)
    source_note = db.Column(db.String(255), nullable=True)

    __table_args__ = (
        db.UniqueConstraint("part_kind", "match_key", name="uix_hw_rank_part_match"),
    )


class SavedComparison(db.Model):
    __tablename__ = 'saved_comparisons'

    id = db.Column(db.String(32), primary_key=True)  # short slug, not UUID v4 text
    payload_json = db.Column(db.JSON, nullable=False)
    created_at = db.Column(db.DateTime, server_default=db.func.now())
