import statistics
from collections import defaultdict
from app import db, create_app
from app.models import Benchmark, BenchmarkResult, BenchmarkAnalysis, System
from app.components import get_system_components

MIN_SYSTEMS_PER_COHORT = 3

# Component keys from get_system_components() that we treat as "insight features"
# (Exclude system_name/identifier because they are identifiers, not explanatory variables.)
INSIGHT_COMPONENT_KEYS = [
    "processor",
    "graphics",
    "memory",
    "motherboard",
    "chipset",
    "os",
    "kernel_version",
    "nvidia_driver",
    "mesa_version",
    "llvm_version",
    "vulkan_driver",
    "chassis_version",
    "cooler_model",
    "psu",
    "custom_hardware",
    "external_off",
    "gpu_fans",
    "memory_fans",
    "nvme_fans",
    "thermal_pad_above_nvme",
    "thermal_pad_below_nvme",
    "thermal_pad_sandwich_nvme",
]

def analyze_benchmarks():
    """Runs the background statistical analysis."""
    print("Starting background benchmark analysis...")
    
    # We only analyze scalar BAR_GRAPH results for simplicity of the correlation engine.
    # Group benchmarking instances by their definition to aggregate all system executions.
    benchmarks = Benchmark.query.filter_by(display_format='BAR_GRAPH').all()
    
    analysis_results = []
    
    # Group benchmarks that are functionally identical for insights.
    # Compare merges benchmark variants by (title, app_version) even when bm.identifier differs,
    # so we do the same here to avoid fragmenting cohorts across identifiers.
    groups = defaultdict(list)  # (title, app_version) -> [Benchmark]
    for bm in benchmarks:
        key = (bm.title, bm.app_version)
        groups[key].append(bm)
        
    for (title, app_version), bm_list in groups.items():
        # Representative benchmark for metadata
        rep_bm = bm_list[0]
        identifier = rep_bm.identifier or ''
        is_lower_better = "Lower is Better" in (rep_bm.proportion or "")
        
        # Collect all results across all these matching benchmark definitions
        all_results = []
        for bm in bm_list:
            all_results.extend(bm.results)
            
        if not all_results:
            continue
            
        # Group by argument combinations to avoid mixing different test parameters
        args_groups = defaultdict(list)
        for res in all_results:
            if res.value is not None:
                args_groups[res.arguments or 'default'].append(res)
                
        # Analyze each argument configuration independently
        argument_analyses = {}
        for arg, results in args_groups.items():
            # feature -> value -> system_id -> [scores]
            features = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
            
            for res in results:
                sys = res.system
                comps = get_system_components(sys)
                for key in INSIGHT_COMPONENT_KEYS:
                    val = (comps.get(key) or "").strip()
                    if not val:
                        continue
                    features[key][val][sys.id].append(res.value)

            # Compute stats per feature
            feature_stats = {}
            for feature_name, value_groups in features.items():
                stats_per_value = []
                for val_name, by_system in value_groups.items():
                    # Aggregate repeated runs per system down to a mean, then compute cohort stats across systems.
                    per_system_means = []
                    n_runs = 0
                    for sys_id, scores in by_system.items():
                        valid = [s for s in scores if s is not None]
                        if not valid:
                            continue
                        n_runs += len(valid)
                        per_system_means.append(statistics.mean(valid))
                    n_systems = len(per_system_means)
                    if n_systems == 0:
                        continue
                    stats_per_value.append({
                        "name": val_name,
                        "n": n_systems,          # backwards-compatible field name: number of distinct systems
                        "n_runs": n_runs,        # additional info: number of raw results contributing
                        "mean": statistics.mean(per_system_means),
                        "median": statistics.median(per_system_means),
                        "min": min(per_system_means),
                        "max": max(per_system_means),
                        # variance could be added here if needed
                    })
                
                # Filter out values with insufficient data to reduce noise
                valid_stats = [s for s in stats_per_value if s["n"] >= MIN_SYSTEMS_PER_COHORT]
                
                # We need at least 2 valid categories to make a comparison (e.g. comparing Cooler A vs Cooler B)
                if len(valid_stats) >= 2:
                    # Sort by best score
                    valid_stats.sort(key=lambda x: x["mean"], reverse=not is_lower_better)
                    feature_stats[feature_name] = valid_stats
                elif len(stats_per_value) > 0:
                    feature_stats[feature_name] = [{"error": f"Insufficient data to draw correlations (requires >= {MIN_SYSTEMS_PER_COHORT} distinct systems per cohort)"}]
                    
            if feature_stats:
                argument_analyses[arg] = feature_stats
                
        if argument_analyses:
            # Save or Update the Analysis Result.
            # Multiple existing rows may exist for the same title/app_version if identifier varied historically.
            # Update them all so the UI has consistent data.
            existing_records = BenchmarkAnalysis.query.filter_by(
                benchmark_title=title,
                benchmark_app_version=app_version
            ).all()

            if not existing_records:
                analysis_record = BenchmarkAnalysis(
                    benchmark_identifier=identifier,
                    benchmark_title=title,
                    benchmark_app_version=app_version
                )
                db.session.add(analysis_record)
                existing_records = [analysis_record]

            for analysis_record in existing_records:
                analysis_record.analysis_json = argument_analyses
                analysis_record.last_updated = db.func.now()
            
    db.session.commit()
    print("Background benchmark analysis complete.")

if __name__ == '__main__':
    # Can run standalone
    app = create_app()
    with app.app_context():
        analyze_benchmarks()
