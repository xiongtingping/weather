import plistlib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_amsc_poller_uses_release_window_polling_not_single_fetch():
    script = (ROOT / "scripts" / "amsc_zuuu_metar_taf_poll_once.sh").read_text(encoding="utf-8")

    assert "AMSC_ZUUU_RELEASE_DELAY_SEC" in script
    assert "AMSC_ZUUU_POLL_ATTEMPTS" in script
    assert "AMSC_ZUUU_POLL_INTERVAL_SEC" in script
    assert "for attempt in $(seq 1 \"$POLL_ATTEMPTS\")" in script
    assert "--count \"$REPORT_COUNT\"" in script
    assert "POLL_SLOT_UTC" in script
    assert "plan_matches_poll_slot" in script
    assert "latest_metar_before_poll_slot" in script
    assert "production_sequential_bayes_transaction_b" in script
    assert "com.weather.task_queue_worker" not in script
    assert "freeze_official_nwp_context_after_fresh_amsc" not in script
    slot_checked_at = script.index("slot_state=\"$(plan_matches_poll_slot")
    artifacts_persisted_at = script.index("artifact_state=\"$(persist_plan_artifacts")
    main_start = script.index("for attempt in $(seq 1 \"$POLL_ATTEMPTS\")")
    probability_fast_lane_at = script.index("run_probability_fast_lane_from_plan", main_start)
    hard_fact_commit_at = script.index('"current_source_fact" "" "1" "source_fact_fast_lane"', main_start)
    assert slot_checked_at < probability_fast_lane_at < artifacts_persisted_at < hard_fact_commit_at
    assert '"current_posterior_handoff"' not in script
    assert "AMSC_ZUUU_PHYSICAL_X_SHADOW_POSTERIOR_ENABLED" not in script
    assert "run_physical_x_shadow_after_fact_apply" not in script
    assert "run_physical_x_shadow_posterior.py" not in script


def test_slot_complete_candidate_applies_catchup_but_waits_for_exact_slot():
    script = (
        ROOT / "scripts" / "amsc_zuuu_metar_taf_poll_once.slot_complete_candidate.sh"
    ).read_text(encoding="utf-8")

    assert 'slot_reason="$(printf' in script
    assert '"latest_metar_before_poll_slot"' in script
    assert 'rm -f "$P2_CANONICAL_RESULT_PATH" "$P2_SHADOW_RESULT_PATH"' in script
    assert 'slot_completed=0' in script
    assert '[ "$scheduled_regular_slot" = "true" ] && [ "$slot_fresh" != "true" ]' in script
    assert '[ "$scheduled_regular_slot" = "true" ] && [ "$slot_completed" != "1" ]' in script
    assert "expired execution deadline prevents a late order side effect" in script


def test_amsc_poller_does_not_reuse_stale_apply_result_between_reports():
    script = (ROOT / "scripts" / "amsc_zuuu_metar_taf_poll_once.sh").read_text(encoding="utf-8")

    assert "rm -f \"$result_path\"" in script
    assert "write_missing_apply_result \"$result_path\" \"$rc\" \"$label\"" in script
    assert "apply_result_missing_after_apply_failure" in script
    assert "archive_db_write_queue_timeout" in script
    assert "db_writer.lock" in script
    assert "apply_output_tail" in script


def test_amsc_poller_allows_empty_optional_enqueue_argument_under_nounset():
    script = (ROOT / "scripts" / "amsc_zuuu_metar_taf_poll_once.sh").read_text(encoding="utf-8")

    handoff = script.index('"${enqueue_args[@]}"')
    assert script.rfind("set +u", 0, handoff) > script.rfind("while [ \"$apply_try\"", 0, handoff)
    assert script.find("set -u", handoff) > handoff


def test_monitor_daemon_owns_amsc_release_window_with_fast_attempts():
    plist_text = (ROOT / "launchd" / "com.weather.monitor.daemon.plist").read_text(encoding="utf-8")
    plist = plistlib.loads(plist_text.encode("utf-8"))
    env = plist["EnvironmentVariables"]

    monitor = (ROOT / "scripts" / "polymarket_monitor_daemon.sh").read_text(encoding="utf-8")
    assert "start_amsc_zuuu_poll()" in monitor
    assert "now_epoch % 1800" in monitor
    assert "AMSC_ZUUU_POLL_LAST_SLOT_KEY" in monitor
    assert '"$ROOT/scripts/run_amsc_zuuu_metar_taf_poll_immutable.sh"' in monitor
    assert "gui/$(id -u)/com.weather.amsc_zuuu_metar_taf_poll" not in monitor
    assert env["PM_AMSC_ZUUU_POLL_ENABLED"] == "1"
    assert env["PM_AMSC_ZUUU_SPECI_WATCH_ENABLED"] == "1"
    assert env["PM_AMSC_ZUUU_SPECI_PRODUCTION_PROBABILITY_INGRESS_ENABLED"] == "1"
    assert env["AMSC_ZUUU_PRODUCTION_PROBABILITY_UPDATE_ENABLED"] == "1"
    assert env["PM_AMSC_ZUUU_SPECI_WATCH_INTERVAL_SEC"] == "60"
    assert env["PM_AMSC_ZUUU_SPECI_WATCH_REPORT_COUNT"] == "24"
    assert env["PM_AMSC_ZUUU_SPECI_WATCH_TIMEOUT_SEC"] == "8"
    assert env["PM_AMSC_ZUUU_SPECI_WATCH_MAX_REPORT_AGE_SEC"] == "900"
    assert env["PM_AMSC_ZUUU_POLL_SLOT_START_WINDOW_SEC"] == "55"
    assert env["PM_AMSC_ZUUU_POLL_SLOT_COMPENSATION_SEC"] == "1800"
    assert env["AMSC_ZUUU_REPORT_COUNT"] == "6"
    assert env["AMSC_ZUUU_RELEASE_DELAY_SEC"] == "35"
    assert env["AMSC_ZUUU_POLL_ATTEMPTS"] == "40"
    assert env["AMSC_ZUUU_POLL_INTERVAL_SEC"] == "1"
    assert env["AMSC_ZUUU_KICK_TASK_QUEUE"] == "1"
    assert "AMSC_ZUUU_POLL_SLOT_COMPENSATION_SEC" in monitor
    assert "write_amsc_zuuu_scheduler_status" in monitor
    assert "slot_compensation_expired" in monitor
    assert "previous_poll_running" in monitor
    assert "start_amsc_zuuu_speci_watch()" in monitor
    assert '"$ROOT/scripts/watch_zuuu_speci_once.py"' in monitor
    assert "production_probability_ingress=${AMSC_ZUUU_SPECI_PRODUCTION_PROBABILITY_INGRESS_ENABLED}" in monitor
    assert "--realtime-ingress" not in monitor


def test_half_hour_poller_routes_taf_revisions_to_orderless_weak_replacement() -> None:
    script = (ROOT / "scripts" / "amsc_zuuu_metar_taf_poll_once.sh").read_text(encoding="utf-8")
    assert "classify_half_hour_fetch" in script
    assert "forecast_revision_candidate_count" in script
    assert "apply_amsc_taf_forecast_revision_v6_from_plan.py" in script
    assert '--activation "$PRODUCTION_PROBABILITY_ACTIVATION_PATH"' in script
    assert "half-hour fetch complete; no new METAR/TAF event" in script
    assert 'taf_revision_result_tmp="${TAF_FORECAST_REVISION_RESULT_PATH}.tmp.$$"' in script
    assert 'if [ ! -s "$taf_revision_result_tmp" ]; then' in script
    assert "taf_forecast_revision_runner_returned_no_receipt" in script
    assert 'mv "$taf_revision_result_tmp" "$TAF_FORECAST_REVISION_RESULT_PATH"' in script
    assert '"taf_forecast_revision": {' in script
    assert '"taf_forecast_revision_state": status["taf_forecast_revision"]["state"]' in script
    assert '"places_orders": False' in script


def test_anomaly_supervision_keeps_amsc_slot_monitoring_alive():
    monitor = (ROOT / "scripts" / "polymarket_monitor_daemon.sh").read_text(encoding="utf-8")
    anomaly_loop = monitor[monitor.index("while kill -0 \"$anomaly_pid\""):monitor.index("rc=0", monitor.index("while kill -0 \"$anomaly_pid\""))]

    assert "start_amsc_zuuu_poll" in anomaly_loop
    assert "start_amsc_zuuu_speci_watch" in anomaly_loop
    assert "start_amsc_p10_shadow_source_capture" in anomaly_loop


def test_monitor_runs_independent_physical_x_before_market_finality_stages():
    monitor = (ROOT / "scripts" / "polymarket_monitor_daemon.sh").read_text(encoding="utf-8")
    physical = monitor.index("run_resolution_capture_stage physical_x")
    weak_physical = monitor.index("run_resolution_research_stage weak_physical_x")
    strong_physical = monitor.index("run_resolution_research_stage strong_physical_x")
    weak_bias = monitor.index("run_resolution_research_stage joint_weak_bias")
    target = monitor.index("run_resolution_capture_stage target_rule")
    finality = monitor.index("run_resolution_capture_stage market_finality")
    assert physical < weak_physical < strong_physical < weak_bias < target < finality
    assert 'STATION_DAY_WEAK_PHYSICAL_MATERIALIZATION_ENABLED="${PM_STATION_DAY_WEAK_PHYSICAL_MATERIALIZATION_ENABLED:-1}"' in monitor
    assert 'JOINT_WEAK_BIAS_RESEARCH_PIPELINE_ENABLED="${PM_JOINT_WEAK_BIAS_RESEARCH_PIPELINE_ENABLED:-1}"' in monitor
    assert "run_joint_weak_bias_research_pipeline.py" in monitor
    assert 'STATION_DAY_STRONG_PHYSICAL_MATERIALIZATION_ENABLED="${PM_STATION_DAY_STRONG_PHYSICAL_MATERIALIZATION_ENABLED:-1}"' in monitor
    assert "materialize_station_day_strong_physical_candidates.py" in monitor


def test_task_queue_worker_default_idle_poll_is_fast_for_source_to_posterior():
    worker = (ROOT / "scripts" / "task_queue_worker.py").read_text(encoding="utf-8")
    plist = plistlib.loads((ROOT / "launchd" / "com.weather.task_queue_worker.plist").read_bytes())

    assert 'DEFAULT_IDLE_SLEEP_SEC = _env_float("TASK_QUEUE_IDLE_SLEEP_SEC", "1")' in worker
    assert plist["EnvironmentVariables"]["TASK_QUEUE_IDLE_SLEEP_SEC"] == "1"
    assert 'TASK_QUEUE_DB_WRITE_WAIT_SEC = _env_float("TASK_QUEUE_DB_WRITE_WAIT_SEC", "0.25")' in worker
    assert plist["EnvironmentVariables"]["TASK_QUEUE_DB_WRITE_WAIT_SEC"] == "0.25"
    assert plist["EnvironmentVariables"]["AMSC_ZUUU_OFFICIAL_NWP_REQUIRE_PINNED_CONTEXT_POLICY"] == "1"
