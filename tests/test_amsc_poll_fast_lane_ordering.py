import os
from pathlib import Path
import subprocess
import time


ROOT = Path(__file__).resolve().parents[1]


def test_monitor_executes_syntax_checked_immutable_poller_snapshot() -> None:
    wrapper = (ROOT / "scripts" / "run_amsc_zuuu_metar_taf_poll_immutable.sh").read_text(
        encoding="utf-8"
    )
    monitor = (ROOT / "scripts" / "polymarket_monitor_daemon.sh").read_text(encoding="utf-8")

    assert 'cp "$SOURCE" "$SNAPSHOT"' in wrapper
    assert 'source_sha_before="$(shasum -a 256 "$SOURCE"' in wrapper
    assert '[ "$source_sha_before" = "$snapshot_sha256" ]' in wrapper
    assert '[ "$snapshot_sha256" = "$source_sha_after" ]' in wrapper
    assert '/bin/bash -n "$SNAPSHOT"' in wrapper
    assert '/bin/bash "$SNAPSHOT" "$@"' in wrapper
    assert 'export AMSC_POLL_PROJECT_ROOT="$ROOT"' in wrapper
    assert 'run_amsc_zuuu_metar_taf_poll_immutable.sh' in monitor
    assert '"$ROOT/scripts/amsc_zuuu_metar_taf_poll_once.sh" \\' not in monitor


def test_immutable_poller_snapshot_survives_source_replacement(tmp_path: Path) -> None:
    source = tmp_path / "mutable_poll.sh"
    snapshots = tmp_path / "snapshots"
    marker = tmp_path / "snapshot_started.marker"
    source.write_text(
        "#!/bin/bash\nset -euo pipefail\necho snapshot_started\n"
        '":" > "$AMSC_TEST_SNAPSHOT_STARTED_MARKER"\n'
        "sleep 0.25\necho snapshot_completed\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env.update(
        {
            "AMSC_POLL_PROJECT_ROOT": str(ROOT),
            "AMSC_POLL_SOURCE_PATH": str(source),
            "AMSC_POLL_SNAPSHOT_DIR": str(snapshots),
            "AMSC_TEST_SNAPSHOT_STARTED_MARKER": str(marker),
        }
    )
    process = subprocess.Popen(
        ["/bin/bash", str(ROOT / "scripts" / "run_amsc_zuuu_metar_taf_poll_immutable.sh")],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    for _ in range(100):
        if marker.exists():
            break
        time.sleep(0.01)
    assert marker.exists(), "immutable snapshot did not start within one second"
    source.write_text("#!/bin/bash\nthis is now invalid (\n", encoding="utf-8")
    stdout, stderr = process.communicate(timeout=5)

    assert process.returncode == 0, stderr
    assert "snapshot_started" in stdout
    assert "snapshot_completed" in stdout
    assert list(snapshots.glob("poll.*.sh")) == []


def test_amsc_probability_fast_lane_precedes_legacy_fact_projection() -> None:
    source = (ROOT / "scripts" / "amsc_zuuu_metar_taf_poll_once.sh").read_text(encoding="utf-8")
    source_commit = 'apply_plan_with_short_retry "$PLAN_PATH" "$FACT_APPLY_RESULT_PATH" "current_source_fact" "" "1" "source_fact_fast_lane"'
    fast_lane = "run_probability_fast_lane_from_plan"

    assert source_commit in source
    main_start = source.index('for attempt in $(seq 1 "$POLL_ATTEMPTS")')
    main_fast_lane = source.index(fast_lane, main_start)
    legacy_fact = source.index(source_commit, main_start)
    assert main_fast_lane < legacy_fact
    assert "Legacy event-fact projection is intentionally" in source


def test_amsc_daily_coverage_scan_is_deferred_until_after_probability_t3() -> None:
    source = (ROOT / "scripts" / "amsc_zuuu_metar_taf_poll_once.sh").read_text(encoding="utf-8")
    main_start = source.index('for attempt in $(seq 1 "$POLL_ATTEMPTS")')
    fetch_at = source.index('"$ROOT/scripts/fetch_zuuu_metar_taf.py"', main_start)
    defer_at = source.index("--defer-daily-coverage", fetch_at)
    probability_at = source.index("run_probability_fast_lane_from_plan", defer_at)
    coverage_at = source.index('"$ROOT/scripts/materialize_amsc_daily_coverage_v2.py"', probability_at)
    legacy_fact_at = source.index('"current_source_fact" "" "1" "source_fact_fast_lane"', coverage_at)

    assert fetch_at < defer_at < probability_at < coverage_at < legacy_fact_at
    assert "never an input to the" in source


def test_amsc_canonical_probability_work_is_supervised_and_not_detached() -> None:
    source = (ROOT / "scripts" / "amsc_zuuu_metar_taf_poll_once.sh").read_text(encoding="utf-8")

    scheduler_start = source.index("run_probability_fast_lane_from_plan()")
    runner_end = source.index("drain_pending_apply_queue()")
    scheduler = source[scheduler_start:runner_end]

    assert "probability fast lane completed" in scheduler
    assert "apply_amsc_probability_fast_lane_from_plan.py" in scheduler
    assert ") >>\"$P2_SIDECAR_LOG_PATH\" 2>&1; then" in scheduler
    assert ") >>\"$P2_SIDECAR_LOG_PATH\" 2>&1 &" not in scheduler
    assert "P2_SIDECAR_LOCK_DIR" in scheduler


def test_amsc_probability_fast_lane_recovers_only_unowned_stale_lock() -> None:
    source = (ROOT / "scripts" / "amsc_zuuu_metar_taf_poll_once.sh").read_text(encoding="utf-8")

    scheduler_start = source.index("run_probability_fast_lane_from_plan()")
    runner_end = source.index("drain_pending_apply_queue()")
    scheduler = source[scheduler_start:runner_end]

    assert 'probability_fast_lane_lock_pid_path="$P2_SIDECAR_LOCK_DIR/owner_pid"' in scheduler
    assert 'kill -0 "$owner_pid"' in scheduler
    assert 'pgrep -f "$ROOT/scripts/apply_amsc_probability_fast_lane_from_plan.py"' in scheduler
    assert 'mv "$P2_SIDECAR_LOCK_DIR" "$stale_lock_dir"' in scheduler
    assert "probability fast lane recovered stale lock" in scheduler
    assert "trap 'release_probability_fast_lane_lock' EXIT" in scheduler
    assert 'recorded_pid" = "$probability_fast_lane_lock_owner_pid' in scheduler


def test_amsc_probability_latest_receipt_is_atomically_replaced() -> None:
    source = (ROOT / "scripts" / "amsc_zuuu_metar_taf_poll_once.sh").read_text(encoding="utf-8")
    scheduler_start = source.index("run_probability_fast_lane_from_plan()")
    runner_end = source.index("drain_pending_apply_queue()")
    scheduler = source[scheduler_start:runner_end]

    assert 'result_tmp="${result_path}.tmp.${probability_fast_lane_lock_owner_pid}"' in scheduler
    assert '>"$result_tmp" 2>&1' in scheduler
    assert scheduler.count('mv "$result_tmp" "$result_path"') == 2
    assert 'rm -f "$result_path"' not in scheduler


def test_amsc_raw_plan_directly_drives_probability_before_compatibility_fact() -> None:
    source = (ROOT / "scripts" / "amsc_zuuu_metar_taf_poll_once.sh").read_text(encoding="utf-8")
    source_commit = '"current_source_fact" "" "1" "source_fact_fast_lane"'
    canonical_probability = "run_probability_fast_lane_from_plan"

    main_start = source.index("for attempt in $(seq 1 \"$POLL_ATTEMPTS\")")
    source_commit_at = source.index(source_commit, main_start)
    canonical_probability_at = source.index(canonical_probability, main_start)

    assert canonical_probability_at < source_commit_at
    assert '"current_posterior_handoff"' not in source
    assert "frozen source plan" in source


def test_amsc_source_fact_uses_one_bounded_fast_lane_attempt() -> None:
    source = (ROOT / "scripts" / "amsc_zuuu_metar_taf_poll_once.sh").read_text(encoding="utf-8")

    assert 'SOURCE_FACT_FAST_QUEUE_WAIT_SEC="${AMSC_ZUUU_SOURCE_FACT_FAST_QUEUE_WAIT_SEC:-0.20}"' in source
    assert 'SOURCE_FACT_FAST_DB_TIMEOUT_SEC="${AMSC_ZUUU_SOURCE_FACT_FAST_DB_TIMEOUT_SEC:-0.20}"' in source
    assert 'SOURCE_FACT_FAST_DB_BUSY_TIMEOUT_MS="${AMSC_ZUUU_SOURCE_FACT_FAST_DB_BUSY_TIMEOUT_MS:-100}"' in source
    assert '"current_source_fact" "" "1" "source_fact_fast_lane"' in source
    assert "source_fact_fast_lane)" in source
    assert "apply_max=1" in source
    assert "--fast-lane" in source
    assert "archive_db_fast_lane_deferred" in source
    assert "append_fact_only_no_late_posterior_handoff" in source


def test_amsc_probability_failure_is_durable_and_replayed_after_fresh_lane() -> None:
    source = (ROOT / "scripts" / "amsc_zuuu_metar_taf_poll_once.sh").read_text(encoding="utf-8")

    assert 'P2_PENDING_PROBABILITY_DIR="${AMSC_ZUUU_P2_PENDING_PROBABILITY_DIR:-$OUT_DIR/pending_probability}"' in source
    assert "queue_pending_probability_plan()" in source
    queue_module = (ROOT / "scripts" / "amsc_probability_pending_queue.py").read_text(encoding="utf-8")
    assert '"recovery_policy": "canonical_then_sequential_posterior_no_direct_order"' in queue_module
    assert "drain_pending_probability_queue()" in source
    assert "*.plan.json" in source
    main_start = source.index('for attempt in $(seq 1 "$POLL_ATTEMPTS")')
    fresh_lane = source.index("run_probability_fast_lane_from_plan", main_start)
    fact_projection = source.index('"current_source_fact" "" "1" "source_fact_fast_lane"', main_start)
    deferred_replay = source.index("drain_pending_probability_queue", fact_projection)
    assert fresh_lane < fact_projection < deferred_replay


def test_amsc_probability_deferral_is_keyed_by_observation_not_mutable_plan() -> None:
    source = (ROOT / "scripts" / "amsc_zuuu_metar_taf_poll_once.sh").read_text(encoding="utf-8")
    queue_start = source.index("queue_pending_probability_plan()")
    queue_end = source.index("is_apply_sqlite_locked()", queue_start)
    queue = source[queue_start:queue_end]

    assert "amsc_probability_pending_queue.py\" enqueue" in queue
    assert '--plan "$plan_path"' in queue
    assert '--result "$result_path"' in queue
    assert '--pending-dir "$P2_PENDING_PROBABILITY_DIR"' in queue
    assert "hashlib.sha256(plan_bytes)" not in queue

    drain_start = source.index("drain_pending_probability_queue()")
    drain_end = source.index("drain_pending_apply_queue()", drain_start)
    drain = source[drain_start:drain_end]
    assert "amsc_probability_pending_queue.py\" compact" in drain
    assert "archive-consumed" in drain
    assert "ARCHIVE_CONSUMED_AMSC_PROBABILITY_PLANS" in drain
    assert "COMPACT_AMSC_PROBABILITY_PENDING_QUEUE" in drain
    assert "expire-noncurrent" in drain
    assert "EXPIRE_NONCURRENT_AMSC_PROBABILITY_PLAN" in drain
    assert "pending probability plan archived as noncurrent evidence-only" in drain
