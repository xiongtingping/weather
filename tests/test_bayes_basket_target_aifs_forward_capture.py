from __future__ import annotations

import json
import hashlib
from pathlib import Path

import pytest

from bayes_basket_target.model_arrival import ALLOWED_PROVIDERS
from bayes_basket_target.contracts import ContractError
from bayes_basket_target.model_run_contract import PROVIDERS
from bayes_basket_target.model_sources import (
    model_id_for_provider,
    provider_role,
    provider_source_class,
)
from bayes_basket_target.native_model_decode import materialize_native_daily_curves
from scripts.run_bayes_basket_target_aifs_forward_capture import (
    PROVIDER_ID,
    _existing_receipt,
    _temperature_range,
    build_plan,
)


def test_aifs_official_is_native_ecmwf_family_not_sixth_role() -> None:
    assert ALLOWED_PROVIDERS[PROVIDER_ID] == "ECMWF"
    assert PROVIDER_ID in PROVIDERS
    assert model_id_for_provider(PROVIDER_ID) == "ECMWF"
    assert provider_role(PROVIDER_ID) == "ecmwf_ifs025_official"
    assert provider_source_class(PROVIDER_ID) == "native_or_official_cloud_replica"


def test_aifs_plan_selects_one_six_hour_curve_and_keeps_oos_gate() -> None:
    plan = build_plan(
        event_date="2026-08-01",
        run_initialization_utc="2026-07-31T18:00:00Z",
        as_of_utc="2026-08-01T05:30:00Z",
        market_registry_snapshot_path=(
            Path(__file__).resolve().parents[1]
            / "data/bayes_basket_target_market_registry_snapshot.json"
        ),
    )

    assert plan["provider_id"] == PROVIDER_ID
    assert plan["product_id"] == "ECMWF_AIFS_SINGLE_0P25"
    assert plan["independence_family_id"] == "ECMWF"
    # The global raw carrier has a union of local settlement-day cadence; it
    # must not inherit KATL's ET window for every Asian/Pacific station.
    assert [row["forecast_hour"] for row in plan["valid_hours"]] == [0, 6, 12, 18, 24, 30, 36]
    assert plan["station_timezones"]["KATL"] == "America/New_York"
    assert plan["station_timezones"]["RKPK"] == "Asia/Seoul"
    assert plan["station_timezones"]["ZUUU"] == "Asia/Shanghai"
    assert plan["model_run_contract"]["station_timezones"] == plan["station_timezones"]
    assert plan["authority"]["source_role"] == "official_native"
    assert plan["authority"]["native_arrival_index_eligible"] is True
    assert plan["authority"]["formal_p0_eligible_without_strict_oos"] is False
    assert plan["authority"]["counts_as_independent_family_from_ifs"] is False
    assert plan["boundary"]["promotion_allowed"] is False
    assert plan["model_run_contract"]["providers"].count(PROVIDER_ID) == 1


def test_aifs_multistation_plan_rejects_anchor_timezone_fallback() -> None:
    with pytest.raises(ContractError, match="aifs_forward_station_timezone_registry_required"):
        build_plan(
            event_date="2026-08-01",
            run_initialization_utc="2026-07-31T18:00:00Z",
            as_of_utc="2026-08-01T05:30:00Z",
            station_ids=["KATL", "RKPK"],
            source_station_id="KATL",
        )


def test_aifs_multistation_plan_binds_each_exact_timezone() -> None:
    plan = build_plan(
        event_date="2026-08-01",
        run_initialization_utc="2026-07-31T18:00:00Z",
        as_of_utc="2026-08-01T05:30:00Z",
        station_ids=["KATL", "RKPK"],
        source_station_id="KATL",
        station_timezones={
            "KATL": "America/New_York",
            "RKPK": "Asia/Seoul",
        },
    )

    assert plan["target_timezone"] == "America/New_York"
    assert plan["station_timezones"] == {
        "KATL": "America/New_York",
        "RKPK": "Asia/Seoul",
    }
    assert plan["station_valid_hours"]["RKPK"][0]["forecast_hour"] == 0
    assert plan["station_valid_hours"]["KATL"][0]["forecast_hour"] == 12


def test_aifs_index_range_requires_exact_native_2t_message() -> None:
    rows = [
        {
            "model": "aifs-single",
            "stream": "oper",
            "type": "fc",
            "levtype": "sfc",
            "param": "2t",
            "step": "30",
            "_offset": 81654393,
            "_length": 552854,
        },
        {
            "model": "aifs-single",
            "stream": "oper",
            "type": "fc",
            "levtype": "sfc",
            "param": "2d",
            "step": "30",
            "_offset": 82855426,
            "_length": 554031,
        },
    ]
    raw = ("\n".join(json.dumps(row) for row in rows) + "\n").encode()

    start, end, digest = _temperature_range(raw, forecast_hour=30)

    assert start == 81654393
    assert end == 81654393 + 552854 - 1
    assert len(digest) == 64


def test_aifs_complete_six_hour_native_cadence_uses_one_interval_at_edges() -> None:
    points = []
    for hour, temperature in ((6, 23.0), (12, 24.0), (18, 29.0), (24, 27.0)):
        day = "2026-08-01" if hour < 24 else "2026-08-02"
        clock = hour if hour < 24 else 0
        points.append({
            "station_id": "KATL",
            "event_date": "2026-08-01",
            "provider_id": PROVIDER_ID,
            "model_run_initialization_utc": "2026-07-31T18:00:00Z",
            "valid_time_utc": f"{day}T{clock:02d}:00:00Z",
            "temperature_2m_c": temperature,
            "raw_message_sha256": "a" * 64,
            "receipt_sha256": "b" * 64,
            "published_at_utc": "2026-07-31T23:30:00Z",
            "received_at_utc": "2026-08-01T00:00:00Z",
        })

    result = materialize_native_daily_curves(
        points,
        target_timezone="America/New_York",
    )

    assert result["complete_curve_count"] == 1
    assert result["curves"][0]["coverage"]["max_gap_hours"] == 6.0
    assert result["curves"][0]["coverage"]["endpoint_tolerance_hours"] == 6.0
    assert result["curves"][0]["forecast_daily_max_c"] == 29.0


def test_aifs_reuses_only_receipt_with_intact_content_addressed_raw(
    tmp_path: Path,
) -> None:
    raw = tmp_path / "range.grib2"
    raw.write_bytes(b"native aifs range")
    digest = hashlib.sha256(raw.read_bytes()).hexdigest()
    receipt_dir = tmp_path / PROVIDER_ID / "receipts"
    receipt_dir.mkdir(parents=True)
    receipt_dir.joinpath("good.json").write_text(json.dumps({
        "provider_id": PROVIDER_ID,
        "station_id": "ZUUU",
        "target_local_date": "2026-08-01",
        "run_initialization_utc": "2026-07-31T18:00:00Z",
        "valid_time_utc": "2026-08-01T06:00:00Z",
        "capture_mode": "prospective",
        "raw_receipts": [{
            "raw_object_ref": str(raw),
            "raw_sha256": digest,
        }],
    }), encoding="utf-8")

    result = _existing_receipt(
        tmp_path,
        day="2026-08-01",
        run="2026-07-31T18:00:00Z",
        valid="2026-08-01T06:00:00Z",
        station="ZUUU",
    )
    assert result is not None

    raw.write_bytes(b"tampered")
    assert _existing_receipt(
        tmp_path,
        day="2026-08-01",
        run="2026-07-31T18:00:00Z",
        valid="2026-08-01T06:00:00Z",
        station="ZUUU",
    ) is None
