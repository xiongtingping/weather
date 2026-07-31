from __future__ import annotations

from datetime import date, timedelta

import pytest

from bayes_basket_target import ContractError
from bayes_basket_target import rolling_native_taf_oos
from bayes_basket_target.rolling_native_taf_oos import (
    build_rolling_origin_native_oos,
    build_rolling_taf_exact_joins,
    plan_non_overlapping_rolling_blocks,
)


def _days(count: int) -> list[str]:
    start = date(2026, 6, 1)
    return [(start + timedelta(days=index)).isoformat() for index in range(count)]


def test_three_rolling_blocks_require_44_unique_common_days() -> None:
    plan = plan_non_overlapping_rolling_blocks(_days(44))

    assert plan["state"] == "READY"
    assert plan["block_count"] == 3
    assert [block["train_case_count"] for block in plan["blocks"]] == [20, 28, 36]
    holdouts = [day for block in plan["blocks"] for day in block["holdout_dates"]]
    assert len(holdouts) == 24
    assert len(set(holdouts)) == 24
    assert plan["blocks"][0]["holdout_dates"] == _days(44)[20:28]
    assert plan["blocks"][2]["holdout_dates"] == _days(44)[36:44]


def test_29_days_remain_fail_closed_and_report_the_15_day_gap() -> None:
    plan = plan_non_overlapping_rolling_blocks(_days(29))

    assert plan["state"] == "ACCUMULATING"
    assert plan["block_count"] == 1
    assert plan["missing_common_case_count"] == 15


def test_rolling_plan_rejects_duplicate_or_weakened_window() -> None:
    with pytest.raises(ContractError, match="event_dates_not_unique"):
        plan_non_overlapping_rolling_blocks(_days(43) + [_days(1)[0]])
    with pytest.raises(ContractError, match="window_contract_invalid"):
        plan_non_overlapping_rolling_blocks(_days(44), min_train_days=19)


def _cases(system_id: str) -> list[dict[str, object]]:
    rows = []
    for index, event_date in enumerate(_days(44)):
        day = date.fromisoformat(event_date)
        actual = 20.0 + float(index % 4)
        rows.append({
            "station_id": "TEST",
            "event_date": event_date,
            "aggregator_snapshot_at_utc": (
                day - timedelta(hours=6)
            ).isoformat() + "T00:00:00Z",
            "values": [actual - 0.2, actual, actual + 0.2],
            "member_set_sha256": ("a" if system_id == "ECMWF_TEST" else "b") * 64,
            "holdout_eligible": True,
            "transport_provider_id": "ECMWF_OPEN_DATA_S3" if system_id == "ECMWF_TEST" else "NOAA_GEFS_PDS_S3",
            "actual_tmax_c": actual,
            "settlement_definition": "official_daily_tmax_c",
            "timezone_name": "Etc/UTC",
        })
    return rows


def test_rolling_builder_emits_three_disjoint_20x8_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cases = {
        ("ECMWF_TEST", "JJA", "d2", "18Z"): _cases("ECMWF_TEST"),
        ("NOAA_TEST", "JJA", "d2", "18Z"): _cases("NOAA_TEST"),
    }
    systems = {
        "ECMWF_TEST": {"independence_family_id": "ECMWF"},
        "NOAA_TEST": {"independence_family_id": "NOAA_GEFS"},
    }
    monkeypatch.setattr(
        rolling_native_taf_oos,
        "native_ensemble_foundation_schema_status",
        lambda _conn: {"ok": True},
    )
    monkeypatch.setattr(
        rolling_native_taf_oos,
        "_load_cases",
        lambda _conn, **_kwargs: cases,
    )
    monkeypatch.setattr(
        rolling_native_taf_oos,
        "_load_systems",
        lambda _conn: systems,
    )

    report = build_rolling_origin_native_oos(
        object(),
        station_id="TEST",
        as_of_utc="2026-08-01T00:00:00Z",
    )

    assert report["state"] == "READY"
    assert len(report["candidates"]) == 3
    assert [
        candidate["rolling_origin_block"]["block_index"]
        for candidate in report["candidates"]
    ] == [0, 1, 2]
    assert all(candidate["state"] == "PRODUCTION_NO_LIVE_OOS_PASS" for candidate in report["candidates"])
    prediction_days = [
        prediction["event_date"]
        for candidate in report["candidates"]
        for prediction in candidate["predictions"]
    ]
    assert len(prediction_days) == 24
    assert len(set(prediction_days)) == 24


def test_rolling_exact_join_combines_three_blocks_without_promoting_awc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cases = {
        ("ECMWF_TEST", "JJA", "d2", "18Z"): _cases("ECMWF_TEST"),
        ("NOAA_TEST", "JJA", "d2", "18Z"): _cases("NOAA_TEST"),
    }
    systems = {
        "ECMWF_TEST": {"independence_family_id": "ECMWF"},
        "NOAA_TEST": {"independence_family_id": "NOAA_GEFS"},
    }
    monkeypatch.setattr(
        rolling_native_taf_oos,
        "native_ensemble_foundation_schema_status",
        lambda _conn: {"ok": True},
    )
    monkeypatch.setattr(
        rolling_native_taf_oos,
        "_load_cases",
        lambda _conn, **_kwargs: cases,
    )
    monkeypatch.setattr(
        rolling_native_taf_oos,
        "_load_systems",
        lambda _conn: systems,
    )
    report = build_rolling_origin_native_oos(
        object(),
        station_id="TEST",
        as_of_utc="2026-08-01T00:00:00Z",
    )
    monkeypatch.setattr(
        rolling_native_taf_oos,
        "_bucket_intervals_for_candidate",
        lambda _conn, _candidate: {
            "19°C or below": (None, 19.5),
            "20°C": (19.5, 20.5),
            "21°C": (20.5, 21.5),
            "22°C or higher": (21.5, None),
        },
    )
    monkeypatch.setattr(
        rolling_native_taf_oos,
        "_taf_truth_for_prediction",
        lambda _conn, **kwargs: (
            (22.0, "2026-08-01T00:00:00Z", "truth-test", "AWC", "FORMAL_NATIVE_ELIGIBLE"),
            ("2026-06-30T12:00:00Z", 22.0, 0.25, 0.1, 4.0, "c" * 64, "official_date_addressed_historical_replay", "FORMAL_STATION_LANE_TRUTH"),
            "2026-06-30T23:59:59Z",
        ),
    )

    joins = build_rolling_taf_exact_joins(object(), report)

    assert len(joins) == 1
    assert joins[0]["sample_count"] == 24
    assert len({row["event_date"] for row in joins[0]["samples"]}) == 24
    assert all(row["taf_lineage"]["enters_p1"] is False for row in joins[0]["samples"])
    assert all(row["taf_lineage"]["enters_hard_no"] is False for row in joins[0]["samples"])
