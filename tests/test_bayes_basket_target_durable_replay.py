from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import bayes_basket_target.durable_replay as replay
from bayes_basket_target.contracts import ContractError


AS_OF = "2026-11-03T00:00:00Z"
TZ = "America/New_York"


def _event(
    event_id: str,
    *,
    event_type: str = "MODEL",
    event_date: str = "2026-11-01",
    event_time: str = "2026-11-01T00:00:00Z",
    available: str = "2026-11-01T00:05:00Z",
    scope: str = "RKPK:2026-11-01",
    settlement_timezone: str = TZ,
    revision: int = 0,
    payload: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "event_id": event_id,
        "scope_id": scope,
        "station_id": "RKPK",
        "event_date": event_date,
        "settlement_timezone": settlement_timezone,
        "event_type": event_type,
        "event_time_utc": event_time,
        "available_at_utc": available,
        "sequence": 0,
        "revision": revision,
        "payload": payload or {"entity_key": event_type, "value": event_id},
    }


def _b_events() -> list[dict[str, object]]:
    base = datetime(2026, 11, 1, 1, 0, tzinfo=timezone.utc)
    result: list[dict[str, object]] = []
    for index, code in enumerate(replay.B_CODES):
        moment = base + timedelta(seconds=index)
        stamp = moment.isoformat().replace("+00:00", "Z")
        result.append(_event(
            f"{code.lower()}-event",
            event_type=code,
            event_time=stamp,
            available=stamp,
            payload={"b_code": code, "checkpoint_id": f"cp-{code}", "value": index + 1},
        ))
    return result


def _assert_code(code: str) -> None:
    result = replay.replay_memory(_b_events(), as_of_utc=AS_OF, window_anchor_date="2026-11-01")
    assert result["status"] == "PASS"
    assert result["coverage"]["base_contracts"][code]["status"] == "PASS"
    assert result["coverage"]["base_contracts"][code]["strict_asof"] is True


def test_B00_historical_paired_residual_coverage() -> None:
    _assert_code("B00")


def test_B01_model_initial_probability_coverage() -> None:
    _assert_code("B01")


def test_B02_ordered_observation_update_coverage() -> None:
    _assert_code("B02")


def test_B03_market_bucket_projection_coverage() -> None:
    _assert_code("B03")


def test_B04_unique_center_coverage() -> None:
    _assert_code("B04")


def test_B05_stage_probability_direction_coverage() -> None:
    _assert_code("B05")


def test_B06_center_bias_confidence_gate_coverage() -> None:
    _assert_code("B06")


def test_B07_adaptive_candidate_support_coverage() -> None:
    _assert_code("B07")


def test_B08_role_decomposition_coverage() -> None:
    _assert_code("B08")


def test_B09_station_target_inventory_coverage() -> None:
    _assert_code("B09")


def test_B10_scenario_pnl_coverage() -> None:
    _assert_code("B10")


def test_B11_incremental_inventory_gap_coverage() -> None:
    _assert_code("B11")


def test_B12_effective_buy_price_coverage() -> None:
    _assert_code("B12")


def test_B13_order_state_hard_gate_coverage() -> None:
    _assert_code("B13")


def test_B14_mathematical_limit_and_reserve_coverage() -> None:
    _assert_code("B14")


def test_B15_trigger_and_cadence_coverage() -> None:
    _assert_code("B15")


def test_B16_cash_share_identity_coverage() -> None:
    _assert_code("B16")


def test_B17_adaptive_state_recompute_coverage() -> None:
    _assert_code("B17")


def test_B18_strategy_gain_attribution_coverage() -> None:
    _assert_code("B18")


def test_B00_B18_receipt_matrix_is_complete_and_named() -> None:
    result = replay.replay_memory(_b_events(), as_of_utc=AS_OF, window_anchor_date="2026-11-01")
    matrix = result["coverage"]["base_contracts"]
    assert tuple(matrix) == replay.B_CODES
    assert set(matrix) == {f"B{index:02d}" for index in range(19)}
    assert result["coverage"]["all_base_contracts_present"] is True
    assert all(row["contract"]["invariant"] for row in matrix.values())
    assert len(result["replay_sha256"]) == 64


def test_durable_and_memory_replay_have_one_semantic_result(tmp_path: Path) -> None:
    events = replay.build_generated_events(seed=7, days=55)
    memory = replay.replay_memory(events, as_of_utc="2026-07-28T00:00:00Z", window_anchor_date="2026-07-27")
    durable = replay.replay_durable_events(
        events,
        db_path=tmp_path / "isolated.sqlite",
        as_of_utc="2026-07-28T00:00:00Z",
        window_anchor_date="2026-07-27",
    )
    assert replay.compare_replay_results(memory, durable)["ok"] is True
    assert durable["boundary"]["calls_order_gateway"] is False
    assert durable["durable_store"]["stored_event_count"] == len(events)


def test_input_order_is_not_state_order_and_exact_duplicate_is_idempotent() -> None:
    events = _b_events()
    shuffled = list(reversed(events))
    shuffled.append(copy.deepcopy(events[3]))
    ordered = replay.replay_memory(events, as_of_utc=AS_OF)
    reordered = replay.replay_memory(shuffled, as_of_utc=AS_OF)
    assert replay.compare_replay_results(ordered, reordered)["ok"] is True
    assert reordered["input_audit"]["duplicate_event_ids"] == [events[3]["event_id"]]


def test_conflicting_duplicate_and_same_revision_logical_collision_fail_closed() -> None:
    event = _event("same-id", payload={"logical_key": "x", "value": 1})
    conflict = copy.deepcopy(event)
    conflict["payload"] = {"logical_key": "x", "value": 2}
    with pytest.raises(ContractError, match="event_id_content_conflict"):
        replay.replay_memory([event, conflict], as_of_utc=AS_OF)
    logical_conflict = copy.deepcopy(event)
    logical_conflict["event_id"] = "other-id"
    with pytest.raises(ContractError, match="logical_key_revision_conflict"):
        replay.replay_memory([event, logical_conflict], as_of_utc=AS_OF)


def test_strict_asof_excludes_future_event_without_changing_semantic_hash() -> None:
    base = _b_events()
    future = _event(
        "future-model",
        event_time="2026-11-04T00:00:00Z",
        available="2026-11-04T00:05:00Z",
        payload={"entity_key": "model", "value": "future"},
    )
    without_future = replay.replay_memory(base, as_of_utc=AS_OF)
    with_future = replay.replay_memory([*base, future], as_of_utc=AS_OF)
    assert with_future["input_audit"]["future_event_ids"] == ["future-model"]
    assert with_future["strict_asof"] is True
    assert with_future["replay_sha256"] == without_future["replay_sha256"]


def test_late_label_revision_is_asof_visible_only_after_arrival() -> None:
    old = _event(
        "label-v1",
        event_type="LABEL",
        event_time="2026-10-31T23:00:00Z",
        available="2026-11-01T00:00:00Z",
        payload={"logical_key": "truth", "label_key": "truth", "label_revision": 1, "value": 30},
    )
    late = _event(
        "label-v2",
        event_type="LABEL",
        event_time="2026-10-31T23:00:00Z",
        available="2026-11-02T12:00:00Z",
        revision=2,
        payload={"logical_key": "truth", "label_key": "truth", "label_revision": 2, "value": 31},
    )
    before = replay.replay_memory([old, late], as_of_utc="2026-11-02T00:00:00Z")
    after = replay.replay_memory([old, late], as_of_utc="2026-11-03T00:00:00Z")
    assert before["state"]["labels"]["truth"]["value"] == 30
    assert after["state"]["labels"]["truth"]["value"] == 31
    assert before["replay_sha256"] != after["replay_sha256"]


def test_station_settlement_timezone_and_dst_window_are_registry_bound() -> None:
    events = _b_events()
    registry = [{"station_id": "RKPK", "event_date": "2026-11-01", "settlement_timezone": TZ}]
    result = replay.replay_memory(events, as_of_utc=AS_OF, registry_rows=registry)
    window = result["state"]["settlement_windows"]["2026-11-01"]
    assert window["target_day_start_utc"] == "2026-11-01T04:00:00Z"
    assert window["target_day_end_utc"] == "2026-11-02T05:00:00Z"
    wrong = [{**registry[0], "settlement_timezone": "America/Los_Angeles"}]
    with pytest.raises(ContractError, match="timezone_mismatch"):
        replay.replay_memory(events, as_of_utc=AS_OF, registry_rows=wrong)


def test_mixed_scope_settlement_timezones_fail_closed() -> None:
    first = _event("seoul", settlement_timezone="Asia/Seoul")
    second = _event("new-york", settlement_timezone="America/New_York", scope="other-scope")
    with pytest.raises(ContractError, match="settlement_timezone_conflict"):
        replay.replay_memory([first, second], as_of_utc=AS_OF)


def test_windows_cross_season_and_cross_day_are_partitioned() -> None:
    events = replay.build_generated_events(seed=31337, anchor_date="2026-03-31", days=90, include_b_contract_events=False)
    result = replay.replay_memory(events, as_of_utc="2026-04-01T00:00:00Z", window_anchor_date="2026-03-31")
    windows = result["windows"]
    assert windows["requested_days"] == [3, 7, 15, 30]
    assert windows["mutually_exclusive"] is True
    assert all(windows["by_window"][str(days)]["event_count"] > 0 for days in (3, 7, 15, 30))
    assert len(result["seasons"]) >= 2


def test_property_replay_multiple_seeds_and_low_high_low_high_wrap() -> None:
    for seed in (1, 31337, 20260727):
        events = replay.build_generated_events(seed=seed, days=55)
        events.extend([
            _event(f"wrap-{seed}-{index}", event_type="CHECKPOINT", event_time=f"2026-07-27T02:0{index}:00Z", available=f"2026-07-27T02:0{index}:00Z", event_date="2026-07-27", scope=f"wrap-{seed}", settlement_timezone="Asia/Seoul", payload={"checkpoint_id": "same", "value": value})
            for index, value in enumerate((1, 4, 2, 5))
        ])
        result = replay.replay_memory(list(reversed(events)), as_of_utc="2026-07-28T00:00:00Z", window_anchor_date="2026-07-27")
        history = [row["value"] for row in result["state"]["checkpoint_history"] if row["checkpoint_id"] == "same"]
        assert history == [1, 4, 2, 5]
        assert result["coverage"]["all_base_contracts_present"] is True


def test_durable_append_is_idempotent_and_conflict_rolls_back(tmp_path: Path) -> None:
    db = tmp_path / "idempotent.sqlite"
    conn = replay.open_event_store(db)
    try:
        events = _b_events()
        first = replay.append_events(conn, events)
        second = replay.append_events(conn, events)
        assert first["stored_event_count"] == len(events)
        assert second["stored_event_count"] == len(events)
        assert len(second["duplicate_event_ids"]) == len(events)
        conflict = copy.deepcopy(events[0])
        conflict["payload"] = {"b_code": "B00", "value": "tampered"}
        with pytest.raises(ContractError, match="event_id_content_conflict"):
            replay.append_events(conn, [conflict])
        assert conn.execute("SELECT COUNT(*) FROM durable_replay_events").fetchone()[0] == len(events)
    finally:
        conn.close()


def test_resource_row_byte_depth_rss_and_disk_guards_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    events = _b_events()
    with pytest.raises(replay.ResourceBoundError, match="rows_exceeded"):
        replay.replay_memory(events, as_of_utc=AS_OF, limits=replay.ReplayLimits(max_events=2))
    with pytest.raises(replay.ResourceBoundError, match="event_bytes_exceeded"):
        replay.replay_memory([_event("large", payload={"x": "a" * 100})], as_of_utc=AS_OF, limits=replay.ReplayLimits(max_event_bytes=20))
    deep: object = "x"
    for _ in range(8):
        deep = {"x": deep}
    with pytest.raises(replay.ResourceBoundError, match="depth_exceeded"):
        replay.replay_memory([_event("deep", payload={"x": deep})], as_of_utc=AS_OF, limits=replay.ReplayLimits(max_payload_depth=3))
    monkeypatch.setattr(replay, "_rss_bytes", lambda: (_ for _ in ()).throw(replay.ResourceBoundError("probe")))
    with pytest.raises(replay.ResourceBoundError, match="probe"):
        replay.replay_memory([events[0]], as_of_utc=AS_OF)


def test_content_address_tamper_and_shared_database_boundary_fail_closed(tmp_path: Path) -> None:
    event = _event("tamper")
    normalized = replay.normalize_event(event)
    tampered = dict(event)
    tampered["event_sha256"] = "0" * 64
    with pytest.raises(ContractError, match="content_sha_mismatch"):
        replay.normalize_event(tampered)
    receipt = replay.replay_memory([event], as_of_utc=AS_OF)
    assert receipt["replay_sha256"] == replay.replay_memory([event], as_of_utc=AS_OF)["replay_sha256"]
    from bayes_basket_target.database import SHARED_LIVE_DB
    with pytest.raises(ContractError, match="shared_live_database"):
        replay.open_event_store(SHARED_LIVE_DB)


def test_cli_generated_replay_is_no_live_and_content_addressed(tmp_path: Path) -> None:
    import subprocess
    import sys

    receipt_path = tmp_path / "receipt.json"
    completed = subprocess.run(
        [
            sys.executable,
            "scripts/replay_bayes_basket_target_durable_events.py",
            "--generate",
            "--seed",
            "20260727",
            "--as-of-utc",
            "2026-07-28T00:00:00Z",
            "--write-receipt",
            str(receipt_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["status"] == "PASS"
    assert receipt["coverage"]["all_base_contracts_present"] is True
    assert receipt["boundary"]["places_orders"] is False
    assert len(receipt["receipt_sha256"]) == 64

