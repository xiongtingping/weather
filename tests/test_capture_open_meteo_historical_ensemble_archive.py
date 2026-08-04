from __future__ import annotations

from datetime import datetime, timezone

from scripts.capture_open_meteo_historical_ensemble_archive import (
    _build_rows,
    _member_series_keys,
)


def _single_model_response(*, expected: int = 40) -> dict[str, object]:
    times = [f"2026-07-01T{hour:02d}:00" for hour in range(24)]
    hourly: dict[str, object] = {"time": times}
    hourly["temperature_2m"] = [20.0 + hour / 100.0 for hour in range(24)]
    for index in range(1, expected):
        hourly[f"temperature_2m_member{index:02d}"] = [
            20.0 + index + hour / 100.0 for hour in range(24)
        ]
    return {
        "response_sha256": "a" * 64,
        "request_url": "https://previous-runs-api.open-meteo.com/v1/forecast?test=1",
        "response_bytes": 123,
        "payload": {"hourly": hourly},
    }


def test_single_model_previous_runs_generic_keys_materialize_members() -> None:
    response = _single_model_response()
    rows, receipt = _build_rows(
        response=response,
        station="RKPK",
        event_dates=["2026-07-01"],
        systems=["DWD_ICON_GLOBAL_EPS"],
        captured_at_utc=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    )

    assert len(rows) == 40
    assert receipt["model_day_counts"] == {"DWD_ICON_GLOBAL_EPS": 1}
    assert receipt["unavailable"] == []
    assert rows[0]["member_id"] == "control"
    assert rows[0]["raw_object_ref"] == "open_meteo_response_sha256:" + "a" * 64
    assert rows[-1]["member_id"] == "perturbed_39"
    assert {row["archive_lead_bucket"] for row in rows} == {"d0"}
    assert {row["run_cycle"] for row in rows} == {
        "AGGREGATOR_UNKNOWN_NATIVE_CYCLE"
    }


def test_partial_generic_member_family_is_quarantined() -> None:
    response = _single_model_response()
    hourly = response["payload"]["hourly"]
    assert isinstance(hourly, dict)
    del hourly["temperature_2m_member39"]

    rows, receipt = _build_rows(
        response=response,
        station="RKPK",
        event_dates=["2026-07-01"],
        systems=["DWD_ICON_GLOBAL_EPS"],
        captured_at_utc="2026-08-02T11:00:00Z",
    )

    assert rows == []
    assert receipt["model_day_counts"] == {}
    assert receipt["unavailable"] == [{
        "event_date": "2026-07-01",
        "system_id": "DWD_ICON_GLOBAL_EPS",
        "reason": "member_set_incomplete",
    }]


def test_generic_family_is_not_reused_for_multiple_requested_models() -> None:
    response = _single_model_response()
    rows, receipt = _build_rows(
        response=response,
        station="RKPK",
        event_dates=["2026-07-01"],
        systems=["DWD_ICON_GLOBAL_EPS", "DWD_ICON_EPS_SEAMLESS"],
        captured_at_utc="2026-08-02T11:00:00Z",
    )

    assert rows == []
    assert receipt["model_day_counts"] == {}
    assert len(receipt["unavailable"]) == 2
    assert all(
        item["reason"] == "member_set_incomplete"
        for item in receipt["unavailable"]
    )


def test_namespaced_family_still_resolves() -> None:
    namespaced: dict[str, list[float]] = {
        "temperature_2m_icon_global_eps": [20.0] * 24,
    }
    namespaced.update({
        f"temperature_2m_member{index:02d}_icon_global_eps": [
            20.0 + index
        ] * 24
        for index in range(1, 40)
    })
    keys = _member_series_keys(
        {**namespaced},
        api_model="icon_global_eps",
        expected_member_count=40,
        allow_generic=False,
    )

    assert keys is not None
    assert keys[0] == "temperature_2m_icon_global_eps"
    assert keys[-1] == "temperature_2m_member39_icon_global_eps"


def test_daily_ensemble_endpoint_materializes_archived_member_maxima() -> None:
    daily: dict[str, object] = {"time": ["2026-07-01", "2026-07-02"]}
    daily["temperature_2m_max_icon_global_eps"] = [30.0, 31.0]
    for index in range(1, 40):
        daily[f"temperature_2m_max_member{index:02d}_icon_global_eps"] = [
            30.0 + index / 10.0,
            31.0 + index / 10.0,
        ]
    rows, receipt = _build_rows(
        response={
            "response_sha256": "b" * 64,
            "request_url": "https://ensemble-api.open-meteo.com/v1/ensemble",
            "response_bytes": 123,
            "payload": {"daily": daily},
        },
        station="RKPK",
        event_dates=["2026-07-01", "2026-07-02"],
        systems=["DWD_ICON_GLOBAL_EPS"],
        captured_at_utc="2026-08-02T12:00:00Z",
    )
    assert len(rows) == 80
    assert receipt["model_day_counts"] == {"DWD_ICON_GLOBAL_EPS": 2}
    assert receipt["unavailable"] == []
    assert rows[0]["raw_tmax_c"] == 30.0
    assert rows[-1]["raw_tmax_c"] == 34.9


def test_daily_missing_scope_does_not_materialize_complete_model() -> None:
    daily: dict[str, object] = {"time": ["2026-07-01"]}
    daily["temperature_2m_max_icon_global_eps"] = [30.0]
    for index in range(1, 40):
        daily[f"temperature_2m_max_member{index:02d}_icon_global_eps"] = [
            30.0 + index / 10.0
        ]
    rows, receipt = _build_rows(
        response={
            "response_sha256": "c" * 64,
            "request_url": "https://ensemble-api.open-meteo.com/v1/ensemble",
            "response_bytes": 123,
            "payload": {"daily": daily},
        },
        station="RKPK",
        event_dates=["2026-07-01"],
        systems=["DWD_ICON_GLOBAL_EPS"],
        captured_at_utc="2026-08-02T12:00:00Z",
        missing_scopes=set(),
    )
    assert rows == []
    assert receipt["model_day_counts"] == {}
    assert receipt["unavailable"] == []
