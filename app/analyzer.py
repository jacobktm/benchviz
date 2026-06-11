import statistics
from collections import defaultdict
from app import db, create_app
from app.models import Benchmark, BenchmarkResult, BenchmarkAnalysis, System
from app.components import get_system_components
from app.workload_profile import build_workload_profile, option_profile_key

# Minimum distinct systems required across all cohort values for a given feature.
# This allows you to get insights when you have (say) 3 distinct systems with 3 different
# CPU models; previously we required 3 systems per identical cohort value, which is too strict.
MIN_SYSTEMS_TOTAL = 3

# Keep a low per-cohort minimum to avoid hiding common real-world situations
# (where each distinct value may appear only once among a small cohort).
MIN_SYSTEMS_PER_COHORT = 1

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
    
    # We only analyze scalar primary BAR_GRAPH results for simplicity of the
    # correlation engine. Perf counters are BAR_GRAPH too, but non-primary.
    benchmarks = Benchmark.query.filter(
        Benchmark.display_format == 'BAR_GRAPH',
        Benchmark.is_primary.is_(True),
    ).all()
    
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
                systems_with_feature = set()
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
                    systems_with_feature.update(by_system.keys())
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
                total_systems_with_feature = len(systems_with_feature)

                # We need enough total coverage across the feature AND at least 2 distinct cohort values.
                if total_systems_with_feature >= MIN_SYSTEMS_TOTAL and len(valid_stats) >= 2:
                    valid_stats.sort(key=lambda x: x["mean"], reverse=not is_lower_better)
                    feature_stats[feature_name] = valid_stats
                elif len(stats_per_value) > 0:
                    feature_stats[feature_name] = [{
                        "error": (
                            f"Insufficient data to draw performance insights "
                            f"(requires >= {MIN_SYSTEMS_TOTAL} distinct systems with data and >= 2 distinct cohort values)"
                        )
                    }]
                    
            if feature_stats:
                argument_analyses[arg] = feature_stats
                
        workload_by_args = {}
        workload_by_option: dict[str, dict[str, dict]] = {}
        option_defs: dict[str, tuple[str, str]] = {}
        for bm in bm_list:
            ok = option_profile_key(bm.description, bm.scale)
            option_defs[ok] = (bm.description or "", bm.scale or "")

        for arg in args_groups.keys():
            arg_key = "default" if (not arg or arg == "default") else arg
            args_db = "" if arg_key == "default" else arg
            system_ids = sorted({r.system_id for r in args_groups[arg] if r.value is not None})
            workload_by_option[arg_key] = {}
            for ok, (opt_desc, opt_scale) in option_defs.items():
                workload_by_option[arg_key][ok] = build_workload_profile(
                    title,
                    app_version or "",
                    args_db,
                    system_ids=system_ids or None,
                    description=opt_desc or rep_bm.description or "",
                    option_description=opt_desc,
                    option_scale=opt_scale,
                )
            if len(workload_by_option[arg_key]) == 1:
                workload_by_args[arg_key] = next(iter(workload_by_option[arg_key].values()))
            elif workload_by_option[arg_key]:
                workload_by_args[arg_key] = build_workload_profile(
                    title,
                    app_version or "",
                    args_db,
                    system_ids=system_ids or None,
                    description=rep_bm.description or "",
                )

        if argument_analyses or workload_by_args or workload_by_option:
            payload = dict(argument_analyses)
            payload["_workload_by_args"] = workload_by_args
            payload["_workload_by_option"] = workload_by_option
            if workload_by_args:
                payload["_workload"] = next(iter(workload_by_args.values()))
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
                analysis_record.analysis_json = payload
                analysis_record.last_updated = db.func.now()
            
    db.session.commit()
    print("Background benchmark analysis complete.")

if __name__ == '__main__':
    # Can run standalone
    app = create_app()
    with app.app_context():
        analyze_benchmarks()
