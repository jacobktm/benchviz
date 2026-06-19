from __future__ import annotations


def register_commands(app):
    """Register all CLI commands on the Flask app instance."""
    from app.cli.commands import (
        backfill_perf_counters,
        calibrate_hardware_ranks_cmd,
        debug_insights_analysis_features,
        debug_insights_coverage,
        debug_insights_feature_values,
        debug_insights_perf_args,
        debug_insights_summary,
        debug_ml_sensors,
        debug_pool_args_cmd,
        debug_pool_axes_cmd,
        debug_primary_perf_benchmarks,
        import_hardware_ranks_cmd,
        ingest,
        init_db,
        move_results,
        nuke_db,
        rebuild_all_insights,
        rebuild_ml_insights,
        rebuild_performance_insights,
        remove_results,
        sync_hardware_ranks_api_cmd,
        sync_openbenchmarking_cache_cmd,
    )

    app.cli.add_command(init_db)
    app.cli.add_command(move_results)
    app.cli.add_command(remove_results)
    app.cli.add_command(nuke_db)
    app.cli.add_command(ingest)
    app.cli.add_command(backfill_perf_counters)
    app.cli.add_command(rebuild_performance_insights)
    app.cli.add_command(rebuild_all_insights)
    app.cli.add_command(rebuild_ml_insights)
    app.cli.add_command(debug_ml_sensors)
    app.cli.add_command(debug_insights_coverage)
    app.cli.add_command(debug_insights_feature_values)
    app.cli.add_command(debug_insights_analysis_features)
    app.cli.add_command(debug_insights_summary)
    app.cli.add_command(debug_insights_perf_args)
    app.cli.add_command(debug_primary_perf_benchmarks)
    app.cli.add_command(import_hardware_ranks_cmd)
    app.cli.add_command(sync_openbenchmarking_cache_cmd)
    app.cli.add_command(sync_hardware_ranks_api_cmd)
    app.cli.add_command(calibrate_hardware_ranks_cmd)
    app.cli.add_command(debug_pool_args_cmd)
    app.cli.add_command(debug_pool_axes_cmd)
