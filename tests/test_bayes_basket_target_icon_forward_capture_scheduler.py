from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json

import pytest

import scripts.run_bayes_basket_target_icon_forward_capture as icon_forward
from bayes_basket_target.resource_bounds import MAX_TARGET_PROCESS_RSS_BYTES
from scripts.run_bayes_basket_target_icon_forward_capture import (
    ContractError,
    build_plan,
    execute_plan,
    _all_icon_preflight_blocked,
    _find_existing_capture_receipt,
    _previous_icon_cycles,
)


AS_OF = "2026-07-24T23:24:00Z"


def test_previous_icon_cycles_roll_back_across_midnight() -> None:
    assert _previous_icon_cycles("2026-07-25T00:00:00Z", limit=3) == [
        "2026-07-24T18:00:00Z",
        "2026-07-24T12:00:00Z",
        "2026-07-24T06:00:00Z",
    ]


def test_only_complete_icon_preflight_failure_is_retryable() -> None:
    assert _all_icon_preflight_blocked({
        "capture_results": [
            {"status": "BLOCKED", "reason_codes": ["model_arrival_icon_preflight_http_404"]},
            {"status": "BLOCKED", "reason_codes": ["model_arrival_icon_preflight_unavailable:TimeoutError"]},
        ]
    }) is True
    assert _all_icon_preflight_blocked({
        "capture_results": [
            {"status": "BLOCKED", "reason_codes": ["model_arrival_min_free_space_guard"]},
        ]
    }) is False


def test_plan_uses_current_open_et_day_and_hourly_icon_urls() -> None:
    plan = build_plan(
        event_date="2026-07-24",
        run_initialization_utc="2026-07-24T00:00:00Z",
        as_of_utc=AS_OF,
    )

    assert plan["event_day_window"]["state"] == "open"
    assert plan["due_only"] is True
    assert len(plan["valid_hours"]) == 20
    assert plan["valid_hours"][0]["valid_time_utc"] == "2026-07-24T04:00:00Z"
    assert plan["valid_hours"][-1]["valid_time_utc"] == "2026-07-24T23:00:00Z"
    assert plan["valid_hours"][0]["url"].startswith("https://opendata.dwd.de/")
    assert plan["boundary"]["writes_target_database"] is False
    assert plan["boundary"]["creates_service"] is False
    assert plan["capture_config_enabled"] is True
    assert plan["capture_config"]["enabled"] is True
    assert plan["capture_config"]["target_only"] is True
    assert "icon_global_official" in plan["capture_config"]["providers"]
    assert plan["resource_budget"]["max_process_rss_bytes"] == 512 * 1024 * 1024
    assert plan["resource_budget"]["max_response_bytes"] == 128 * 1024 * 1024
    assert plan["resource_budget"]["max_capture_bytes"] == 256 * 1024 * 1024


def test_plan_fails_closed_when_target_capture_switch_is_disabled(tmp_path) -> None:
    config = {
        "schema": "bayes_basket_target_model_arrival_capture_v1",
        "enabled": False,
        "target_only": True,
        "scope": "canonical_45_station",
        "station_count": 45,
        "providers": [
            "ecmwf_ifs025_official",
            "ecmwf_aifs025_official",
            "gfs_0p25_official",
            "icon_global_official",
        ],
        "boundary": {
            "writes_target_database": False,
            "writes_production_database": False,
            "calls_order_gateway": False,
            "places_orders": False,
            "live_order_authorized": False,
            "protected_production_line_touched": False,
        },
    }
    path = tmp_path / "model_arrival_capture.json"
    path.write_text(json.dumps(config), encoding="utf-8")

    with pytest.raises(ContractError, match="target_capture_disabled"):
        build_plan(
            event_date="2026-07-24",
            run_initialization_utc="2026-07-24T00:00:00Z",
            as_of_utc=AS_OF,
            capture_config_path=path,
        )


def test_plan_fails_closed_when_configured_rss_exceeds_hard_limit(tmp_path) -> None:
    config = json.loads(
        open(
            "config/bayes_basket_target/model_arrival_capture_v1.json",
            encoding="utf-8",
        ).read()
    )
    config["resource_budget"]["max_process_rss_bytes"] = MAX_TARGET_PROCESS_RSS_BYTES + 1
    path = tmp_path / "model_arrival_capture.json"
    path.write_text(json.dumps(config), encoding="utf-8")

    with pytest.raises(ContractError, match="max_process_rss_bytes_exceeds_hard_limit"):
        build_plan(
            event_date="2026-07-24",
            run_initialization_utc="2026-07-24T00:00:00Z",
            as_of_utc=AS_OF,
            capture_config_path=path,
        )


def test_execute_rejects_plan_resource_budget_tampering_before_network() -> None:
    plan = build_plan(
        event_date="2026-07-24",
        run_initialization_utc="2026-07-24T00:00:00Z",
        as_of_utc=AS_OF,
    )
    plan["resource_budget"]["max_response_bytes"] += 1

    with pytest.raises(ContractError, match="plan_sha256_mismatch"):
        execute_plan(plan)


@pytest.mark.parametrize("event_date", ["2026-07-23", "2026-07-26"])
def test_plan_rejects_invalid_closed_or_too_distant_target(event_date: str) -> None:
    with pytest.raises(
        ContractError,
        match="target_day_closed|model_run_after_target_day|outside_d2_d1_d0_window",
    ):
        build_plan(
            event_date=event_date,
            run_initialization_utc="2026-07-24T00:00:00Z",
            as_of_utc=AS_OF,
        )


def test_plan_allows_next_et_day_d2_raw_capture_without_fanout_authority() -> None:
    plan = build_plan(
        event_date="2026-07-25",
        run_initialization_utc="2026-07-24T00:00:00Z",
        as_of_utc=AS_OF,
    )
    assert plan["event_day_window"]["state"] == "not_started"
    assert len(plan["valid_hours"]) == 24
    assert plan["boundary"]["p0_probability_input_eligible_now"] is True
    assert plan["boundary"]["p1_probability_input_eligible_now"] is False
    assert plan["boundary"]["p0_consumption_before_event"] is True
    assert plan["boundary"]["p1_consumption_before_event"] is False


def test_pre_event_execute_builds_p0_station_lineage_without_p1_authority(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    plan = build_plan(
        event_date="2026-07-25",
        run_initialization_utc="2026-07-24T00:00:00Z",
        as_of_utc=AS_OF,
    )
    monkeypatch.setattr(icon_forward, "_now_utc", lambda: AS_OF)
    monkeypatch.setattr(icon_forward, "_icon_availability_preflight", lambda *args, **kwargs: {})
    monkeypatch.setattr(icon_forward, "assert_process_memory_budget", lambda **kwargs: None)
    monkeypatch.setattr(icon_forward, "assert_target_raw_retention_budget", lambda *args, **kwargs: None)
    monkeypatch.setattr(icon_forward, "_find_existing_capture_receipt", lambda **kwargs: None)
    monkeypatch.setattr(
        icon_forward,
        "capture",
        lambda **kwargs: {
            "status": "CAPTURED",
            "transport_receipt_path": str(tmp_path / "raw-receipt.json"),
            "receipt": {},
        },
    )

    fanout_calls = []

    def p0_only_fanout(**kwargs):
        fanout_calls.append(kwargs)
        return {
            "captured_count": 45 * 24,
            "blocked_count": 0,
            "boundary": {
                "p0_station_lineage_only": True,
                "p1_probability_input_authorized": False,
            },
        }

    monkeypatch.setattr(icon_forward, "fanout", p0_only_fanout)
    result = execute_plan(
        plan,
        source_root=tmp_path / "raw",
        output_root=tmp_path / "output",
    )
    assert result["captured_count"] == 24
    assert len(fanout_calls) == 1
    assert result["fanout"]["captured_count"] == 45 * 24
    assert result["fanout"]["boundary"]["p1_probability_input_authorized"] is False


def test_plan_rejects_run_after_decision_clock() -> None:
    with pytest.raises(ContractError, match="run_is_in_future"):
        build_plan(
            event_date="2026-07-24",
            run_initialization_utc="2026-07-25T00:00:00Z",
            as_of_utc=AS_OF,
        )


def test_plan_is_45_station_global_anchor_bound() -> None:
    with pytest.raises(ContractError, match="station_scope_not_45"):
        build_plan(
            event_date="2026-07-24",
            run_initialization_utc="2026-07-24T00:00:00Z",
            as_of_utc=AS_OF,
            station_ids=["ZUUU"],
        )


def test_registry_bound_plan_accepts_exact_station_timezone_scope() -> None:
    plan = build_plan(
        event_date="2026-07-27",
        run_initialization_utc="2026-07-25T18:00:00Z",
        as_of_utc="2026-07-26T03:00:00Z",
        station_ids=["RKPK"],
        target_timezone="Asia/Seoul",
        market_registry_snapshot_sha256="a" * 64,
        registry_bound=True,
    )

    assert plan["station_ids"] == ["RKPK"]
    assert plan["source_station_id"] == "RKPK"
    assert plan["target_timezone"] == "Asia/Seoul"
    assert plan["model_run_contract"]["market_registry_snapshot_sha256"] == "a" * 64
    assert plan["event_day_window"]["target_day_start_utc"] == "2026-07-26T15:00:00Z"


def test_cli_preserves_registry_station_timezone_scope(tmp_path, capsys) -> None:
    output = tmp_path / "plan.json"

    exit_code = icon_forward.main([
        "--event-date",
        "2026-07-29",
        "--run-initialization-utc",
        "2026-07-27T18:00:00Z",
        "--as-of-utc",
        "2026-07-28T12:00:00Z",
        "--station-id",
        "MPMG",
        "--source-station-id",
        "MPMG",
        "--target-timezone",
        "America/Panama",
        "--market-registry-snapshot-sha256",
        "a" * 64,
        "--registry-bound",
        "--output",
        str(output),
    ])

    assert exit_code == 0
    plan = json.loads(output.read_text(encoding="utf-8"))
    assert plan["station_ids"] == ["MPMG"]
    assert plan["source_station_id"] == "MPMG"
    assert plan["target_timezone"] == "America/Panama"
    assert plan["market_registry_snapshot_sha256"] == "a" * 64
    assert plan["event_day_window"]["target_day_start_utc"] == "2026-07-29T05:00:00Z"
    assert '"state": "PLAN_ONLY"' in capsys.readouterr().out


def test_plan_can_explicitly_include_not_yet_due_hours_for_diagnostics() -> None:
    plan = build_plan(
        event_date="2026-07-24",
        run_initialization_utc="2026-07-24T00:00:00Z",
        as_of_utc=AS_OF,
        due_only=False,
    )

    assert plan["due_only"] is False
    assert len(plan["valid_hours"]) == 24
    assert plan["valid_hours"][-1]["valid_time_utc"] == "2026-07-25T03:00:00Z"


def test_existing_exact_key_capture_is_reused_only_with_verified_raw(tmp_path) -> None:
    raw_dir = tmp_path / "icon_global_official" / "raw"
    raw_dir.mkdir(parents=True)
    temporary = raw_dir / "payload.bz2"
    temporary.write_bytes(b"native-icon")
    digest = hashlib.sha256(temporary.read_bytes()).hexdigest()
    # Use the actual digest in the content-addressed filename.
    actual = raw_dir / (digest + ".bz2")
    temporary.rename(actual)
    receipt_dir = tmp_path / "icon_global_official" / "receipts"
    receipt_dir.mkdir(parents=True)
    receipt = {
        "schema": "bayes_basket_target_native_model_transport_receipt_v1",
        "provider_id": "icon_global_official",
        "station_id": "ZUUU",
        "target_local_date": "2026-07-24",
        "run_initialization_utc": "2026-07-24T00:00:00Z",
        "valid_time_utc": "2026-07-24T04:00:00Z",
        "capture_mode": "prospective",
        "publication_time_source": "provider_http_last_modified",
        "raw_receipts": [{
            "raw_object_ref": str(actual),
            "raw_sha256": digest,
            "response_bytes": len(b"native-icon"),
            "response_headers": {
                "content-length": str(len(b"native-icon")),
            },
            "response_completed_at": "2026-07-24T04:01:00Z",
        }],
    }
    path = receipt_dir / "receipt.json"
    path.write_text(json.dumps(receipt), encoding="utf-8")

    found = _find_existing_capture_receipt(
        source_root=tmp_path,
        target_local_date="2026-07-24",
        run_initialization_utc="2026-07-24T00:00:00Z",
        valid_time_utc="2026-07-24T04:00:00Z",
    )

    assert found == path.resolve()
