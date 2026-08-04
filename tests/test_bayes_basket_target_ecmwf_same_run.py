from __future__ import annotations

from datetime import datetime, timezone
import json

import pytest

import scripts.capture_bayes_basket_target_ecmwf_same_run as ecmwf_same_run
from bayes_basket_target.contracts import ContractError
from bayes_basket_target.model_run_contract import build_model_run_contract
from bayes_basket_target.resource_bounds import write_bounded_json
from bayes_basket_target.station_registry import coordinate_registry_manifest
from scripts.capture_bayes_basket_target_ecmwf_same_run import (
    build_ecmwf_urls,
    build_plan,
    parse_2t_index_range,
    execute_plan,
)


RUN = datetime(2026, 7, 25, 0, tzinfo=timezone.utc)


def _row(**overrides):
    value = {
        "domain": "g",
        "date": "20260725",
        "time": "0000",
        "type": "fc",
        "stream": "oper",
        "levtype": "sfc",
        "step": "30",
        "param": "2t",
        "_offset": 100,
        "_length": 50,
    }
    value.update(overrides)
    return value


def test_index_parser_selects_unique_surface_2t_exact_range() -> None:
    body = b"\n".join([
        json.dumps(_row(param="tp", _offset=0)).encode(),
        json.dumps(_row()).encode(),
    ])

    start, end, selected = parse_2t_index_range(
        body,
        run_initialization=RUN,
        forecast_hour=30,
        max_message_bytes=1024,
    )

    assert (start, end) == (100, 149)
    assert selected["param"] == "2t"


@pytest.mark.parametrize(
    "rows",
    [
        [_row(param="tp")],
        [_row(), _row(_offset=200)],
        [_row(step="36")],
    ],
)
def test_index_parser_fails_closed_on_missing_or_ambiguous_2t(rows) -> None:
    body = b"\n".join(json.dumps(row).encode() for row in rows)
    with pytest.raises(ContractError, match="2t_index_cardinality_invalid"):
        parse_2t_index_range(
            body,
            run_initialization=RUN,
            forecast_hour=30,
            max_message_bytes=1024,
        )


def test_d1_plan_uses_30_36_42_48_51_and_only_official_index(tmp_path) -> None:
    contract = build_model_run_contract(
        target_local_date="2026-07-26",
        run_initialization_utc="2026-07-25T00:00:00Z",
        station_ids=coordinate_registry_manifest()["station_ids"],
        target_timezone="America/New_York",
        market_registry_snapshot_sha256="a" * 64,
    )
    contract_path = tmp_path / "contract.json"
    write_bounded_json(contract_path, contract)

    plan = build_plan(
        target_local_date="2026-07-26",
        run_initialization_utc="2026-07-25T00:00:00Z",
        as_of_utc="2026-07-26T12:00:00Z",
        contract_path=contract_path,
    )

    assert [point["forecast_hour"] for point in plan["points"]] == [30, 36, 42, 48, 51]
    assert plan["points"][0]["valid_time_utc"] == "2026-07-26T06:00:00Z"
    assert plan["points"][-1]["valid_time_utc"] == "2026-07-27T03:00:00Z"
    assert all(point["index_url"].endswith(".index") for point in plan["points"])
    assert all("ecmwf-forecasts.s3.eu-central-1.amazonaws.com" in point["index_url"] for point in plan["points"])
    assert plan["resource_budget"]["max_response_bytes"] == 128 * 1024 * 1024


def test_plan_allows_not_started_target_for_raw_capture_only(tmp_path) -> None:
    contract = build_model_run_contract(
        target_local_date="2026-07-26",
        run_initialization_utc="2026-07-25T00:00:00Z",
        station_ids=coordinate_registry_manifest()["station_ids"],
        target_timezone="America/New_York",
        market_registry_snapshot_sha256="a" * 64,
    )
    contract_path = tmp_path / "contract.json"
    write_bounded_json(contract_path, contract)

    plan = build_plan(
        target_local_date="2026-07-26",
        run_initialization_utc="2026-07-25T00:00:00Z",
        as_of_utc="2026-07-26T03:59:59Z",
        contract_path=contract_path,
    )
    assert plan["capture_window"]["state"] == "not_started"
    assert plan["boundary"]["p0_probability_input_eligible_now"] is True
    assert plan["boundary"]["p1_probability_input_eligible_now"] is False
    assert plan["boundary"]["p0_consumption_before_event"] is True
    assert plan["boundary"]["p1_consumption_before_event"] is False


def test_execute_turns_not_yet_published_index_into_bounded_manifest(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    contract = build_model_run_contract(
        target_local_date="2026-07-26",
        run_initialization_utc="2026-07-25T00:00:00Z",
        station_ids=coordinate_registry_manifest()["station_ids"],
        target_timezone="America/New_York",
        market_registry_snapshot_sha256="a" * 64,
    )
    contract_path = tmp_path / "contract.json"
    write_bounded_json(contract_path, contract)
    plan = build_plan(
        target_local_date="2026-07-26",
        run_initialization_utc="2026-07-25T00:00:00Z",
        as_of_utc="2026-07-26T03:59:59Z",
        contract_path=contract_path,
    )
    monkeypatch.setattr(
        ecmwf_same_run,
        "validate_prospective_capture_window",
        lambda **kwargs: {"state": "not_started"},
    )
    monkeypatch.setattr(ecmwf_same_run, "assert_process_memory_budget", lambda **kwargs: None)
    monkeypatch.setattr(ecmwf_same_run, "assert_target_raw_retention_budget", lambda *args, **kwargs: None)
    monkeypatch.setattr(ecmwf_same_run, "_find_existing_receipt", lambda **kwargs: None)
    monkeypatch.setattr(
        ecmwf_same_run,
        "_fetch_index",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("HTTP Error 404: Not Found")),
    )
    monkeypatch.setattr(
        ecmwf_same_run,
        "fanout",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("blocked raw cannot fan out")),
    )
    result = execute_plan(
        plan,
        raw_root=tmp_path / "raw",
        output_root=tmp_path / "output",
    )
    assert result["captured_or_reused_count"] == 0
    assert result["blocked_count"] == 5
    assert result["fanout"] is None
    assert all(row["status"] == "BLOCKED" for row in result["records"])


def test_url_uses_forecast_hour_in_native_ecmwf_filename() -> None:
    grib, index = build_ecmwf_urls(run_initialization=RUN, forecast_hour=30)
    assert grib.endswith("/20260725000000-30h-oper-fc.grib2")
    assert index.endswith("/20260725000000-30h-oper-fc.index")
