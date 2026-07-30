from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from bayes_basket_target import ContractError
import bayes_basket_target.historical_rebuild as historical_rebuild
from bayes_basket_target.historical_rebuild import rebuild_settlement_history
import scripts.rebuild_bayes_basket_target_settlement_history as rebuild_script
from bayes_basket_target.resource_bounds import MIN_TARGET_FREE_BYTES
from bayes_basket_target.settlement_time import settlement_day_state, settlement_day_window


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _registry(station: str = "RKPK", event_date: str = "2026-07-27", timezone_name: str = "Asia/Seoul") -> list[dict[str, str]]:
    return [{"station_id": station, "event_date": event_date, "settlement_timezone": timezone_name, "slug": f"{station}-{event_date}"}]


def _truth(*, timezone_name: str = "Asia/Seoul", valid: str = "2026-07-26T16:00:00Z", arrival: str = "2026-07-26T16:05:00Z", value: float = 34.0) -> dict[str, object]:
    return {
        "station_id": "RKPK", "event_date": "2026-07-27", "target_timezone": timezone_name,
        "source_lane": "awc", "valid_at_utc": valid, "reported_temperature_c": value,
        "arrival_id": "truth-arrival-1", "arrival_at_utc": arrival,
        "raw_sha256": _sha("truth-raw"), "receipt_sha256": _sha("truth-receipt"), "decode_sha256": _sha("truth-decode"),
    }


def _model(*, timezone_name: str = "Asia/Seoul", valid: str = "2026-07-26T16:00:00Z", arrival: str = "2026-07-26T16:06:00Z", value: float = 34.5, provider: str = "gfs_0p25_official") -> dict[str, object]:
    return {
        "station_id": "RKPK", "event_date": "2026-07-27", "target_timezone": timezone_name,
        "provider_id": provider, "model_id": provider.upper(), "run_initialization_utc": "2026-07-26T00:00:00Z",
        "valid_time_utc": valid, "temperature_2m_c": value,
        "arrival_id": f"{provider}-arrival-{valid}", "arrival_at_utc": arrival,
        "raw_sha256": _sha(f"{provider}-raw-{valid}"), "receipt_sha256": _sha(f"{provider}-receipt-{valid}"), "decode_sha256": _sha(f"{provider}-decode-{valid}"),
    }


def test_rkpk_rebuild_uses_registry_kst_and_marks_closed_truth() -> None:
    result = rebuild_settlement_history(
        registry_rows=_registry(),
        truth_rows=[_truth()],
        model_rows=[_model()],
        probability_artifacts=[{"schema": "probability", "settlement_timezone": "Asia/Seoul"}],
        cutoff_utc="2026-07-27T15:00:00Z",
        station_ids=["RKPK"], event_dates=["2026-07-27"],
    )
    assert result["settlement_timezone"] == "Asia/Seoul"
    assert result["windows"][0]["target_day_start_utc"] == "2026-07-26T15:00:00Z"
    assert result["windows"][0]["target_day_end_utc"] == "2026-07-27T15:00:00Z"
    assert result["exact_joins"][0]["truth_frozen"] is True
    assert result["exact_joins"][0]["calibration_eligible"] is True


def test_et_is_not_a_market_override() -> None:
    with pytest.raises(ContractError, match="historical_row_settlement_timezone_mismatch"):
        rebuild_settlement_history(
            registry_rows=_registry(), truth_rows=[_truth(timezone_name="America/New_York")], model_rows=[_model()],
            cutoff_utc="2026-07-27T15:00:00Z", station_ids=["RKPK"], event_dates=["2026-07-27"],
        )


@pytest.mark.parametrize(
    ("event_date", "start", "end"),
    [
        ("2026-03-08", "2026-03-08T05:00:00Z", "2026-03-09T04:00:00Z"),
        ("2026-11-01", "2026-11-01T04:00:00Z", "2026-11-02T05:00:00Z"),
    ],
)
def test_dst_spring_and_fall_use_local_midnight(event_date: str, start: str, end: str) -> None:
    assert settlement_day_window(event_date, "America/New_York")["target_day_start_utc"] == start
    assert settlement_day_window(event_date, "America/New_York")["target_day_end_utc"] == end


def test_kathmandu_non_integral_offset_and_state_boundary() -> None:
    window = settlement_day_window("2026-07-27", "Asia/Kathmandu")
    assert window["target_day_start_utc"] == "2026-07-26T18:15:00Z"
    assert window["target_day_end_utc"] == "2026-07-27T18:15:00Z"
    assert settlement_day_state("2026-07-27", as_of_utc="2026-07-26T18:14:59Z", settlement_timezone="Asia/Kathmandu")["state"] == "not_started"
    assert settlement_day_state("2026-07-27", as_of_utc="2026-07-27T18:15:00Z", settlement_timezone="Asia/Kathmandu")["state"] == "closed"


def test_probability_artifact_timezone_mismatch_fails_closed() -> None:
    with pytest.raises(ContractError, match="historical_probability_artifact_settlement_timezone_mismatch"):
        rebuild_settlement_history(
            registry_rows=_registry(), truth_rows=[_truth()], model_rows=[_model()],
            probability_artifacts=[{"schema": "probability", "target_timezone": "America/New_York"}],
            cutoff_utc="2026-07-27T15:00:00Z", station_ids=["RKPK"], event_dates=["2026-07-27"],
        )


def test_arrival_after_cutoff_is_not_backfilled() -> None:
    with pytest.raises(ContractError, match="historical_exact_arrival_after_cutoff"):
        rebuild_settlement_history(
            registry_rows=_registry(), truth_rows=[_truth(arrival="2026-07-27T15:01:00Z")], model_rows=[_model()],
            cutoff_utc="2026-07-27T15:00:00Z", station_ids=["RKPK"], event_dates=["2026-07-27"],
        )


def test_window_comparison_is_exact_at_fractional_second_boundary() -> None:
    result = rebuild_settlement_history(
        registry_rows=_registry(),
        truth_rows=[_truth(valid="2026-07-27T14:59:59.500Z")],
        model_rows=[_model()],
        cutoff_utc="2026-07-27T15:00:00Z", station_ids=["RKPK"], event_dates=["2026-07-27"],
    )
    assert result["truth"][0]["valid_at_utc"] == "2026-07-27T14:59:59.500000Z"
    with pytest.raises(ContractError, match="historical_truth_outside_registry_window"):
        rebuild_settlement_history(
            registry_rows=_registry(), truth_rows=[_truth(valid="2026-07-27T15:00:00Z")], model_rows=[_model()],
            cutoff_utc="2026-07-27T15:00:00Z", station_ids=["RKPK"], event_dates=["2026-07-27"],
        )


def test_low_high_low_replay_is_order_and_repeat_deterministic() -> None:
    points = [
        _model(valid="2026-07-26T16:00:00Z", value=30.0),
        _model(valid="2026-07-26T18:00:00Z", value=36.0),
        _model(valid="2026-07-26T20:00:00Z", value=30.0),
    ]
    kwargs = dict(
        registry_rows=_registry(), truth_rows=[_truth()], model_rows=points,
        cutoff_utc="2026-07-27T15:00:00Z", station_ids=["RKPK"], event_dates=["2026-07-27"],
    )
    first = rebuild_settlement_history(**kwargs)
    second = rebuild_settlement_history(**{**kwargs, "model_rows": list(reversed(points))})
    third = rebuild_settlement_history(**kwargs)
    assert first["evidence_sha256"] == second["evidence_sha256"] == third["evidence_sha256"]
    assert [row["temperature_2m_c"] for row in first["model_curves"][0]["points"]] == [30.0, 36.0, 30.0]


def test_historical_rebuild_row_budget_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(historical_rebuild, "MAX_REBUILD_ROWS", 1)
    with pytest.raises(ContractError, match="historical_rebuild_row_budget_exceeded"):
        rebuild_settlement_history(
            registry_rows=_registry(), truth_rows=[_truth()], model_rows=[_model()],
            cutoff_utc="2026-07-27T15:00:00Z", station_ids=["RKPK"], event_dates=["2026-07-27"],
        )


def test_historical_rebuild_output_disk_probe_fails_closed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    evidence = rebuild_settlement_history(
        registry_rows=_registry(), truth_rows=[_truth()], model_rows=[_model()],
        cutoff_utc="2026-07-27T15:00:00Z", station_ids=["RKPK"], event_dates=["2026-07-27"],
    )
    monkeypatch.setattr(rebuild_script.shutil, "disk_usage", lambda _path: SimpleNamespace(free=MIN_TARGET_FREE_BYTES - 1))
    with pytest.raises(ContractError, match="historical_rebuild_min_free_space_guard"):
        rebuild_script._assert_output_capacity(tmp_path / "evidence.json", evidence)


def test_historical_rebuild_input_file_count_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rebuild_script, "MAX_INPUT_FILES_PER_BATCH", 0)
    with pytest.raises(ContractError, match="historical_rebuild_input_file_budget_exceeded"):
        rebuild_script._load_rows(["unused.json"], kind="truth")
