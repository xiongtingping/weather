from scripts.forecast_source_weight_daily_acceptance_audit import build_daily_acceptance_audit


def test_daily_acceptance_proves_fail_closed_automation():
    report = build_daily_acceptance_audit(
        status={
            "schema": "forecast_source_weight_daily_pipeline_v1",
            "state": "champion_retained",
            "production_apply_allowed": False,
            "guards": {"changes_parameters": False, "applies_artifact": False, "places_orders": False, "restarts_launchd": False},
        },
        curve_inputs={"guards": {"requires_observed_first_seen_arrival_lineage": True, "uses_observations_after_curve_cutoff": False}},
        amsc_forward={"forecast_snapshots": [{"state": "available", "source_id": "zuuu_taf_forecast_amsc", "lineage_quality": "immutable_external_raw_object", "arrival_time_quality": "observed_first_seen"}]},
        research_plist={"ProgramArguments": ["python3", "pipeline.py", "--execute", "--max-incremental-requests", "12"], "KeepAlive": False},
        monitor_plist={"EnvironmentVariables": {"FORECAST_SOURCE_FORWARD_ARCHIVE_ENABLED": "1"}},
        forward_capture_plist={
            "Label": "com.weather.forecast_source_forward_capture",
            "StartCalendarInterval": {"Minute": 0},
            "ProgramArguments": [
                "/bin/zsh",
                "-c",
                "capture_current_forecast_source_forward.py",
                "--allow-research-write",
                "data/research/forecast_source_skill/forward/latest.json",
            ],
        },
        installed_research_plist={"Label": "com.weather.forecast_source_weight_research", "ProgramArguments": ["python3", "pipeline.py", "--execute", "--max-incremental-requests", "12"], "KeepAlive": False},
        installed_monitor_plist={"EnvironmentVariables": {"FORECAST_SOURCE_FORWARD_ARCHIVE_ENABLED": "1"}},
        installed_forward_capture_plist={
            "Label": "com.weather.forecast_source_forward_capture",
            "StartCalendarInterval": {"Minute": 0},
            "ProgramArguments": ["/bin/zsh", "-c", "capture_current_forecast_source_forward.py", "--allow-research-write", "data/research/forecast_source_skill/forward/latest.json"],
        },
    )

    assert report["ok"] is True
    assert report["guards"]["changes_parameters"] is False


def test_daily_acceptance_rejects_uninstalled_runtime_automation():
    report = build_daily_acceptance_audit(
        status={
            "schema": "forecast_source_weight_daily_pipeline_v1",
            "state": "champion_retained",
            "production_apply_allowed": False,
            "guards": {"changes_parameters": False, "applies_artifact": False, "places_orders": False, "restarts_launchd": False},
        },
        curve_inputs={"guards": {"requires_observed_first_seen_arrival_lineage": True, "uses_observations_after_curve_cutoff": False}},
        amsc_forward={"forecast_snapshots": [{"state": "available", "source_id": "zuuu_taf_forecast_amsc", "lineage_quality": "immutable_external_raw_object", "arrival_time_quality": "observed_first_seen"}]},
        research_plist={"ProgramArguments": ["python3", "pipeline.py", "--execute", "--max-incremental-requests", "12"], "KeepAlive": False},
        monitor_plist={"EnvironmentVariables": {"FORECAST_SOURCE_FORWARD_ARCHIVE_ENABLED": "1"}},
        forward_capture_plist={"Label": "com.weather.forecast_source_forward_capture", "StartCalendarInterval": {"Minute": 0}, "ProgramArguments": ["/bin/zsh", "-c", "capture_current_forecast_source_forward.py", "--allow-research-write", "forecast_source_skill/forward"]},
    )

    assert report["ok"] is False
    assert "installed_daily_research_schedule_matches_contract" in report["blockers"]
    assert "installed_hourly_forward_capture_matches_contract" in report["blockers"]


def test_daily_acceptance_rejects_missing_strict_taf_lineage():
    report = build_daily_acceptance_audit(
        status={
            "schema": "forecast_source_weight_daily_pipeline_v1",
            "state": "champion_retained",
            "production_apply_allowed": False,
            "guards": {"changes_parameters": False, "applies_artifact": False, "places_orders": False, "restarts_launchd": False},
        },
        curve_inputs={"guards": {"requires_observed_first_seen_arrival_lineage": True, "uses_observations_after_curve_cutoff": False}},
        amsc_forward={"forecast_snapshots": []},
        research_plist={"ProgramArguments": ["python3", "pipeline.py", "--execute", "--max-incremental-requests", "12"], "KeepAlive": False},
        monitor_plist={"EnvironmentVariables": {"FORECAST_SOURCE_FORWARD_ARCHIVE_ENABLED": "1"}},
        forward_capture_plist={"Label": "com.weather.forecast_source_forward_capture", "StartCalendarInterval": {"Minute": 0}, "ProgramArguments": ["/bin/zsh", "-c", "capture_current_forecast_source_forward.py", "--allow-research-write", "forecast_source_skill/forward"]},
        installed_research_plist={"Label": "com.weather.forecast_source_weight_research", "ProgramArguments": ["python3", "pipeline.py", "--execute", "--max-incremental-requests", "12"], "KeepAlive": False},
        installed_monitor_plist={"EnvironmentVariables": {"FORECAST_SOURCE_FORWARD_ARCHIVE_ENABLED": "1"}},
        installed_forward_capture_plist={"Label": "com.weather.forecast_source_forward_capture", "StartCalendarInterval": {"Minute": 0}, "ProgramArguments": ["/bin/zsh", "-c", "capture_current_forecast_source_forward.py", "--allow-research-write", "forecast_source_skill/forward"]},
    )

    assert report["ok"] is False
    assert "taf_forward_has_immutable_first_seen_lineage" in report["blockers"]
