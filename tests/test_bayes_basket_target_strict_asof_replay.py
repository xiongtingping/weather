from __future__ import annotations

import copy
from datetime import datetime, timedelta
import hashlib

import pytest

from bayes_basket_target.contracts import ContractError
from bayes_basket_target.strict_asof_replay import (
    DEFAULT_TARGET_TIMEZONE,
    build_disjoint_windows,
    evaluate_strict_asof_replay,
    lead_bucket_for_maturity,
    materialize_model_station_days,
    season_bin_for_event_date,
)

TARGET_TIMEZONE = "America/New_York"

def _sha(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def _rows() -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    # The first four dates deliberately land in the four mutually exclusive
    # trailing windows for an 2026-12-01 local anchor.
    dates = [
        "2026-11-30",  # prior_3d, autumn
        "2026-11-23",  # prior_7d, autumn
        "2026-11-10",  # prior_15d, autumn
        "2026-10-10",  # prior_30d, autumn
        "2026-08-10",  # summer
        "2026-04-10",  # spring
        "2026-02-10",  # winter
    ]
    models: list[dict[str, str]] = []
    observations: list[dict[str, str]] = []
    for index, event_date in enumerate(dates, start=1):
        previous = __import__("datetime").date.fromisoformat(event_date) - __import__("datetime").timedelta(days=1)
        run_init = f"{previous.isoformat()}T00:00:00Z"
        # 12Z is safely inside the event day under the fixed ET boundary in
        # both standard time and daylight time.
        valid_time = f"{event_date}T12:00:00Z"
        for station_index, station in enumerate(("ZBAA", "ZGGG"), start=1):
            observed = str(20 + index + station_index)
            observations.append(
                {
                    "station_id": station,
                    "event_date": event_date,
                    "valid_time_utc": valid_time,
                    "temperature_c": observed,
                    "raw_sha256": _sha(f"o{index}{station_index}"),
                    "source_raw_sha256": _sha(f"s{index}{station_index}"),
                }
            )
            for provider_index, provider in enumerate(
                (
                    "ecmwf_ifs025_official",
                    "gfs_0p25_official",
                    "icon_global_official",
                ), start=1
            ):
                models.append(
                    {
                        "station_id": station,
                        "event_date": event_date,
                        "provider_id": provider,
                        "model_run_initialization_utc": run_init,
                        "valid_time_utc": valid_time,
                        "temperature_2m_c": str(20 + index + station_index - provider_index),
                        "raw_message_sha256": _sha(f"m{index}{station_index}{provider_index}"),
                        "receipt_sha256": _sha(f"r{index}{station_index}{provider_index}"),
                    }
                )
    return models, observations


def test_target_day_default_timezone_is_fixed_to_et() -> None:
    assert DEFAULT_TARGET_TIMEZONE == "America/New_York"


def test_windows_are_disjoint_and_have_exact_boundary_semantics() -> None:
    windows = build_disjoint_windows(__import__("datetime").date(2026, 12, 1))
    assert [(row["days"], row["start_event_date_inclusive"], row["end_event_date_exclusive"]) for row in windows] == [
        (3, "2026-11-28", "2026-12-01"),
        (7, "2026-11-21", "2026-11-28"),
        (15, "2026-11-06", "2026-11-21"),
        (30, "2026-10-07", "2026-11-06"),
    ]
    assert len({
        (row["start_event_date_inclusive"], row["end_event_date_exclusive"])
        for row in windows
    }) == 4


def test_strict_asof_replay_passes_windows_and_cross_season_lineage() -> None:
    models, observations = _rows()
    result = evaluate_strict_asof_replay(
        models,
        observations,
        as_of_utc="2026-12-01T00:00:00Z",
        target_timezone=TARGET_TIMEZONE,
    )
    assert result["windows"]["mutually_exclusive"] is True
    assert result["windows"]["same_maturity"] is True
    assert [row["window_id"] for row in result["windows"]["reports"]] == [
        "prior_3d", "prior_7d", "prior_15d", "prior_30d"
    ]
    assert all(row["lineage_complete"] for row in result["windows"]["reports"])
    assert result["crossseason"]["all_four_seasons_present"] is True
    assert result["crossseason"]["cross_station"] is True
    assert result["crossseason"]["strict_asof"] is True
    assert result["crossseason"]["lineage_complete"] is True
    assert result["boundary"]["promotion_allowed"] is False
    assert result["replay_sha256"]


def test_future_run_and_maturity_drift_fail_closed() -> None:
    models, observations = _rows()
    future = copy.deepcopy(models)
    future[0]["model_run_initialization_utc"] = "2026-12-01T06:00:00Z"
    with pytest.raises(ContractError, match="model_run_after_target_day"):
        evaluate_strict_asof_replay(future, observations, as_of_utc="2026-12-01T00:00:00Z", target_timezone=TARGET_TIMEZONE)

    drift = copy.deepcopy(models)
    drift[0]["model_run_initialization_utc"] = "2026-11-28T00:00:00Z"
    with pytest.raises(ContractError, match="model_run_maturity_drift"):
        evaluate_strict_asof_replay(drift, observations, as_of_utc="2026-12-01T00:00:00Z", target_timezone=TARGET_TIMEZONE)


def test_same_utc_date_00z_before_et_boundary_is_a_distinct_valid_maturity() -> None:
    models, observations = _rows()
    same_day = copy.deepcopy(models)
    for row in same_day:
        row["model_run_initialization_utc"] = f"{row['event_date']}T00:00:00Z"
    result = evaluate_strict_asof_replay(
        same_day, observations, as_of_utc="2026-12-01T00:00:00Z", target_timezone=TARGET_TIMEZONE
    )
    assert result["maturity_key"] == "PRE_ET_SAME_UTC_DATE_00Z"
    assert result["lead_bucket"] == "d1"
    assert result["run_cycle"] == "00Z"
    assert result["windows"]["same_maturity"] is True


def test_d0_run_inside_target_et_day_has_its_own_calibration_bucket() -> None:
    models, observations = _rows()
    d0 = copy.deepcopy(models)
    for row in d0:
        row["model_run_initialization_utc"] = f"{row['event_date']}T12:00:00Z"
        row["published_at_utc"] = f"{row['event_date']}T14:00:00Z"
        row["received_at_utc"] = f"{row['event_date']}T14:01:00Z"
    result = evaluate_strict_asof_replay(
        d0,
        observations,
        as_of_utc="2026-12-02T00:00:00Z",
        target_timezone=TARGET_TIMEZONE,
    )
    assert result["maturity_key"] == "D0_WITHIN_TARGET_ET_DAY"
    assert result["lead_bucket"] == "d0"
    assert result["run_cycle"] == "12Z"


def test_previous_18z_complete_release_uses_explicit_d2_bucket() -> None:
    models, observations = _rows()
    fallback = copy.deepcopy(models)
    for row in fallback:
        event_day = datetime.fromisoformat(row["event_date"]).date()
        run_day = event_day - timedelta(days=2)
        row["model_run_initialization_utc"] = f"{run_day.isoformat()}T18:00:00Z"
        row["published_at_utc"] = f"{run_day.isoformat()}T20:00:00Z"
        row["received_at_utc"] = f"{run_day.isoformat()}T20:01:00Z"
    result = evaluate_strict_asof_replay(
        fallback,
        observations,
        as_of_utc="2026-12-02T00:00:00Z",
        target_timezone=TARGET_TIMEZONE,
    )
    assert result["maturity_key"] == "D-2_UTC_DATE_18Z_TO_TARGET_DAY"
    assert result["lead_bucket"] == "d2"
    assert result["run_cycle"] == "18Z"


def test_season_bin_flips_for_southern_hemisphere_station() -> None:
    assert season_bin_for_event_date(
        "2026-07-29", latitude=-33.97
    ) == "winter"
    assert season_bin_for_event_date(
        "2026-07-29", latitude=37.46
    ) == "summer"


def test_previous_utc_date_official_cycle_is_valid_but_cycles_cannot_mix() -> None:
    models, observations = _rows()
    prior_06z = copy.deepcopy(models)
    for row in prior_06z:
        previous = __import__("datetime").date.fromisoformat(row["event_date"]) - __import__("datetime").timedelta(days=1)
        row["model_run_initialization_utc"] = f"{previous.isoformat()}T06:00:00Z"
    result = evaluate_strict_asof_replay(
        prior_06z, observations, as_of_utc="2026-12-01T00:00:00Z", target_timezone=TARGET_TIMEZONE
    )
    assert result["maturity_key"] == "D-1_UTC_DATE_TO_TARGET_ET_DAY"
    assert result["lead_bucket"] == "d2"
    assert result["run_cycle"] == "06Z"

    mixed_cycle = copy.deepcopy(models)
    for row in mixed_cycle:
        if row["station_id"] == "ZBAA":
            previous = __import__("datetime").date.fromisoformat(row["event_date"]) - __import__("datetime").timedelta(days=1)
            row["model_run_initialization_utc"] = f"{previous.isoformat()}T06:00:00Z"
    with pytest.raises(ContractError, match="strict_asof_run_cycle_not_uniform"):
        evaluate_strict_asof_replay(
            mixed_cycle, observations, as_of_utc="2026-12-01T00:00:00Z", target_timezone=TARGET_TIMEZONE
        )


def test_et_boundary_and_dst_are_explicit_and_06z_is_d0() -> None:
    models, _ = _rows()
    summer = copy.deepcopy(models[0])
    summer["event_date"] = "2026-07-24"
    summer["model_run_initialization_utc"] = "2026-07-24T00:00:00Z"
    summer["valid_time_utc"] = "2026-07-24T12:00:00Z"
    materialized, _ = materialize_model_station_days([summer], target_timezone=TARGET_TIMEZONE)
    assert materialized[0]["maturity_key"] == "PRE_ET_SAME_UTC_DATE_00Z"
    assert materialized[0]["lead_bucket"] == "d1"

    winter = copy.deepcopy(summer)
    winter["event_date"] = "2026-01-24"
    winter["model_run_initialization_utc"] = "2026-01-24T00:00:00Z"
    winter["valid_time_utc"] = "2026-01-24T12:00:00Z"
    materialized, _ = materialize_model_station_days([winter], target_timezone=TARGET_TIMEZONE)
    assert materialized[0]["maturity_key"] == "PRE_ET_SAME_UTC_DATE_00Z"

    after_boundary = copy.deepcopy(summer)
    after_boundary["model_run_initialization_utc"] = "2026-07-24T06:00:00Z"
    materialized, _ = materialize_model_station_days([after_boundary], target_timezone=TARGET_TIMEZONE)
    assert materialized[0]["maturity_key"] == "D0_WITHIN_TARGET_ET_DAY"
    assert materialized[0]["lead_bucket"] == "d0"


def test_run_level_arrival_uses_latest_complete_message_timestamp() -> None:
    models, _ = _rows()
    first = copy.deepcopy(models[0])
    second = copy.deepcopy(models[0])
    second["valid_time_utc"] = "2026-11-30T13:00:00Z"
    second["temperature_2m_c"] = "18"
    second["raw_message_sha256"] = _sha("latest-message")
    second["receipt_sha256"] = _sha("latest-receipt")
    first["published_at_utc"] = "2026-11-29T01:00:00Z"
    first["received_at_utc"] = "2026-11-29T02:00:00Z"
    second["published_at_utc"] = "2026-11-29T03:00:00Z"
    second["received_at_utc"] = "2026-11-29T04:00:00Z"
    materialized, _ = materialize_model_station_days([first, second], target_timezone=TARGET_TIMEZONE)
    assert materialized[0]["published_at_utc"] == "2026-11-29T03:00:00Z"
    assert materialized[0]["received_at_utc"] == "2026-11-29T04:00:00Z"


def test_mixed_maturity_keys_fail_closed_instead_of_mixing_training() -> None:
    models, observations = _rows()
    mixed = copy.deepcopy(models)
    for row in mixed:
        if row["station_id"] == "ZBAA":
            row["model_run_initialization_utc"] = f"{row['event_date']}T00:00:00Z"
    with pytest.raises(ContractError, match="strict_asof_maturity_not_uniform"):
        evaluate_strict_asof_replay(mixed, observations, as_of_utc="2026-12-01T00:00:00Z", target_timezone=TARGET_TIMEZONE)


def test_missing_sample_lineage_and_forecast_day_leak_fail_closed() -> None:
    models, observations = _rows()
    missing = copy.deepcopy(observations)
    missing[0]["source_raw_sha256"] = ""
    with pytest.raises(ContractError, match="source_raw_sha256_missing"):
        evaluate_strict_asof_replay(models, missing, as_of_utc="2026-12-01T00:00:00Z", target_timezone=TARGET_TIMEZONE)

    wrong_day = copy.deepcopy(models)
    wrong_day[0]["valid_time_utc"] = "2026-12-02T12:00:00Z"
    with pytest.raises(ContractError, match="forecast_valid_time_outside_target_local_day"):
        evaluate_strict_asof_replay(wrong_day, observations, as_of_utc="2026-12-01T00:00:00Z", target_timezone=TARGET_TIMEZONE)


def test_replay_is_deterministic_and_future_rows_do_not_enter_window() -> None:
    models, observations = _rows()
    future_models = copy.deepcopy(models)
    future_models.append(
        {
            **future_models[0],
            "event_date": "2026-12-02",
            "model_run_initialization_utc": "2026-12-01T00:00:00Z",
            "valid_time_utc": "2026-12-02T12:00:00Z",
        }
    )
    future_observations = copy.deepcopy(observations)
    future_observations.append(
        {
            **future_observations[0],
            "event_date": "2026-12-02",
            "valid_time_utc": "2026-12-02T00:00:00Z",
        }
    )
    first = evaluate_strict_asof_replay(
        future_models, future_observations, as_of_utc="2026-12-01T00:00:00Z", target_timezone=TARGET_TIMEZONE
    )
    second = evaluate_strict_asof_replay(
        future_models, future_observations, as_of_utc="2026-12-01T00:00:00Z", target_timezone=TARGET_TIMEZONE
    )
    assert first == second
    assert first["anchor_local_date"] == "2026-11-30"
    assert all("2026-12-02" not in row["event_dates"] for row in first["windows"]["reports"])
