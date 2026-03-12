import json
import statistics
from collections import defaultdict
from app import db, create_app
from app.models import Benchmark, BenchmarkResult, BenchmarkAnalysis, System

# Hardware components that we extract from the raw hardware string
EXTRACTED_HARDWARE = ['Processor', 'Motherboard', 'Chipset', 'Memory', 'Graphics']

def extract_hardware_component(hardware_string, component_prefix):
    """Extracts a specific component like 'Processor: ' from the Phoronix hardware string."""
    if not hardware_string:
        return None
    for part in hardware_string.split(','):
        part = part.strip()
        if part.startswith(f"{component_prefix}:"):
            return part.split(':', 1)[1].strip()
    return None

def analyze_benchmarks():
    """Runs the background statistical analysis."""
    print("Starting background benchmark analysis...")
    
    # We only analyze scalar BAR_GRAPH results for simplicity of the correlation engine.
    # Group benchmarking instances by their definition to aggregate all system executions.
    benchmarks = Benchmark.query.filter_by(display_format='BAR_GRAPH').all()
    
    analysis_results = []
    
    # Group benchmarks that are functionally identical (same identifier, title, app_version)
    groups = {}
    for bm in benchmarks:
        key = (bm.identifier, bm.title, bm.app_version)
        if key not in groups:
            groups[key] = []
        groups[key].append(bm)
        
    for (identifier, title, app_version), bm_list in groups.items():
        # Representative benchmark for metadata
        rep_bm = bm_list[0]
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
            features = defaultdict(lambda: defaultdict(list))
            
            for res in results:
                sys = res.system
                # Extracted components
                for comp in EXTRACTED_HARDWARE:
                    val = extract_hardware_component(sys.hardware, comp)
                    if val:
                        features[f"Detected {comp}"][val].append(res.value)
                        
                # Manual components
                if sys.chassis_version: features['Chassis Version'][sys.chassis_version].append(res.value)
                if sys.cooler_model: features['Cooler Model'][sys.cooler_model].append(res.value)
                if sys.psu_model: features['PSU Model'][sys.psu_model].append(res.value)
                if sys.psu_wattage: features['PSU Wattage'][sys.psu_wattage].append(res.value)
                if sys.custom_hardware: features['Custom Hardware Tag'][sys.custom_hardware].append(res.value)
                features['External Off'][str(sys.external_off)].append(res.value)
                features['GPU Fans'][str(sys.gpu_fans)].append(res.value)
                features['Memory Fans'][str(sys.memory_fans)].append(res.value)
                features['NVMe Fans'][str(sys.nvme_fans)].append(res.value)

            # Compute stats per feature
            feature_stats = {}
            for feature_name, value_groups in features.items():
                stats_per_value = []
                for val_name, scores in value_groups.items():
                    n = len(scores)
                    stats_per_value.append({
                        "name": val_name,
                        "n": n,
                        "mean": statistics.mean(scores),
                        "median": statistics.median(scores),
                        "min": min(scores),
                        "max": max(scores)
                        # variance could be added here if needed
                    })
                
                # Filter out values with insufficient data to reduce noise
                # MINIMUM SAMPLE SIZE THRESHOLD: 3
                valid_stats = [s for s in stats_per_value if s["n"] >= 3]
                
                # We need at least 2 valid categories to make a comparison (e.g. comparing Cooler A vs Cooler B)
                if len(valid_stats) >= 2:
                    # Sort by best score
                    valid_stats.sort(key=lambda x: x["mean"], reverse=not is_lower_better)
                    feature_stats[feature_name] = valid_stats
                elif len(stats_per_value) > 0:
                    feature_stats[feature_name] = [{"error": "Insufficient data to draw correlations (requires multiple distinct components with n >= 3)"}]
                    
            if feature_stats:
                argument_analyses[arg] = feature_stats
                
        if argument_analyses:
            # Save or Update the Analysis Result
            analysis_record = BenchmarkAnalysis.query.filter_by(
                benchmark_identifier=identifier or '',
                benchmark_title=title,
                benchmark_app_version=app_version
            ).first()
            
            if not analysis_record:
                analysis_record = BenchmarkAnalysis(
                    benchmark_identifier=identifier or '',
                    benchmark_title=title,
                    benchmark_app_version=app_version
                )
                db.session.add(analysis_record)
                
            analysis_record.analysis_json = argument_analyses
            analysis_record.last_updated = db.func.now()
            
    db.session.commit()
    print("Background benchmark analysis complete.")

if __name__ == '__main__':
    # Can run standalone
    app = create_app()
    with app.app_context():
        analyze_benchmarks()
