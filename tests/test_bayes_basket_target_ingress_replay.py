from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from scripts.bayes_basket_target_ingress_replay import (
    MAX_RECEIPT_BYTES,
    build_receipt,
)


ROOT = Path(__file__).resolve().parents[1]


def test_m2_m3_ingress_replay_receipt_passes_and_hash_is_stable() -> None:
    first = build_receipt()
    second = build_receipt()
    assert first == second
    assert first["status"] == "PASS"
    assert len(first["receipt_sha256"]) == 64
    assert first["boundary"]["isolated_in_memory_only"] is True
    assert first["boundary"]["writes_production_db"] is False


def test_exactly_once_repeats_same_event_and_p1_one_hundred_times() -> None:
    receipt = build_receipt()
    for row in receipt["stage_runs"]:
        assert row["p1_materialization_attempts"] == 101
        assert row["p1_replay_count"] == 100
        assert row["p1_replay_writes_false_count"] == 100
        assert row["p1_materialization_writes"] == 1
        assert row["p1_row_count"] == 1
        assert row["same_event_same_p1_only_one_write"] is True
    assert receipt["counts"]["p1_materialization_attempts"] == 202
    assert receipt["counts"]["p1_materialization_writes"] == 2


def test_replay_adversarial_rounds_and_revision_halts_are_explicit() -> None:
    receipt = build_receipt()
    replay = receipt["event_replay"]
    assert replay["same_id_conflict_blocked"] is True
    assert replay["rewind_blocked"] is True
    assert replay["late_timestamp_blocked"] is True
    assert replay["cross_day_scope_blocked"] is True
    assert replay["multi_round"]["deterministic_final_replay"] is True
    assert replay["multi_round"]["final_legal_result_determined"] is True
    assert replay["low_wrong_low"]["wrong_content_blocked"] is True
    assert replay["low_wrong_low"]["low_retry_duplicate_count"] == 1
    assert all(
        item["station_day_state"] == "HALTED_REVIEW"
        and item["review_required"] is True
        and item["post_event_blocked"] is True
        and item["automatic_reversal"] is False
        and item["secondary_materialization"] is False
        for item in receipt["revision_scenarios"]
    )


def test_stage_replay_uses_registry_station_and_timezone_not_et_override() -> None:
    receipt = build_receipt()
    by_lane = {row["lane"]: row for row in receipt["stage_runs"]}
    assert by_lane["AMSC"]["station_id"] == "ZUUU"
    assert by_lane["AMSC"]["settlement_timezone"] == "Asia/Shanghai"
    assert by_lane["AWC"]["station_id"] == "KATL"
    assert by_lane["AWC"]["settlement_timezone"] == "America/New_York"
    for row in receipt["stage_runs"]:
        assert row["settlement_timezone_source"] == "market_registry.settlement_timezone"
        assert row["event_date_source"] == "market_registry.event_date"
        assert row["operational_timezone_not_authoritative"] is True
    assert receipt["scope"]["operational_day_is_not_market_day"] is True


def test_cli_uses_bounded_atomic_receipt_and_leaves_no_temp_fragment(tmp_path: Path) -> None:
    output = tmp_path / "ingress-replay-receipt.json"
    command = [
        sys.executable,
        str(ROOT / "scripts/bayes_basket_target_ingress_replay.py"),
        "--write-receipt",
        str(output),
    ]
    completed = subprocess.run(
        command,
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    raw = output.read_bytes()
    assert len(raw) < MAX_RECEIPT_BYTES
    assert len(raw) < 1024 * 1024
    assert json.loads(raw)["status"] == "PASS"
    assert raw == completed.stdout.encode("utf-8")
    assert not list(tmp_path.glob(f".{output.name}.tmp.*"))
    assert not list(tmp_path.glob("*.tmp"))


def test_all_legacy_eight_checks_remain_green() -> None:
    receipt = build_receipt()
    legacy = {
        "amsc_stage_chain",
        "awc_stage_chain",
        "p1_exactly_once",
        "correction_requires_review",
        "strict_asof_required",
        "event_duplicate_is_idempotent",
        "event_rewind_fails_closed",
        "correction_halts_following_events",
    }
    assert legacy <= receipt["checks"].keys()
    assert all(receipt["checks"][name] for name in legacy)
    assert receipt["counts"]["checks"] >= len(legacy)
