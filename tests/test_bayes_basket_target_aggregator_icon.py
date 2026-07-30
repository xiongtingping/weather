from __future__ import annotations

from datetime import date, timedelta
import json

import pytest

from bayes_basket_target.contracts import ContractError
from bayes_basket_target.et_history import TARGET_TIMEZONE, build_et_history_evidence, normalize_et_model_rows
from bayes_basket_target.historical_model_rows import (
    load_historical_model_rows,
    select_preferred_model_rows,
)
from bayes_basket_target.model_arrival import validate_model_arrival


SHA_A = "a" * 64
SHA_B = "b" * 64


def _row(provider: str, *, temperature: float, capture_mode: str = "historical_rebuild") -> dict[str, object]:
    return {
        "station_id": "ZUUU",
        "event_date": "2026-07-24",
        "target_timezone": TARGET_TIMEZONE,
        "provider_id": provider,
        "model_run_initialization_utc": "2026-07-23T00:00:00Z",
        "valid_time_utc": "2026-07-24T16:00:00Z",
        "temperature_2m_c": temperature,
        "raw_message_sha256": SHA_A if provider != "icon_global_open_meteo" else SHA_B,
        "receipt_sha256": SHA_A if provider != "icon_global_open_meteo" else SHA_B,
        "capture_mode": capture_mode,
        "reconstructed_at_utc": "2026-07-25T10:00:00Z",
        "historical_replay_eligible": True,
        "prospective_strict_asof_eligible": False,
        "source_class": "aggregator_republisher" if provider == "icon_global_open_meteo" else "native_or_official_cloud_replica",
        "aggregator_or_republisher": provider == "icon_global_open_meteo",
        "open_meteo": provider == "icon_global_open_meteo",
        "source_url": "https://historical-forecast-api.open-meteo.com/v1/forecast" if provider == "icon_global_open_meteo" else "https://native.example/model",
        "raw_object_ref": "/tmp/raw-object.json",
    }


def _truth() -> dict[str, object]:
    return {
        "station_id": "ZUUU",
        "source_lane": "amsc",
        "report_type": "METAR",
        "valid_at_utc": "2026-07-24T16:00:00Z",
        "reported_temperature_c": 31.0,
        "raw_sha256": SHA_A,
        "source_poll_id": "poll-1",
        "first_seen_at_utc": "2026-07-24T16:01:00Z",
        "canonical_committed_at_utc": "2026-07-24T16:02:00Z",
        "qc_status": "ACCEPTED",
        "correction_suspected": 0,
    }


def test_aggregator_icon_completes_icon_role_but_is_not_native_arrival() -> None:
    rows = [_row("ecmwf_ifs025_official", temperature=30.0), _row("gfs_0p25_official", temperature=29.5), _row("icon_global_open_meteo", temperature=28.5)]
    accepted, blocked = normalize_et_model_rows(rows, as_of_utc="2026-07-25T15:00:00Z", history_only=True)
    assert not blocked
    assert {item["provider_role"] for item in accepted} == {
        "ecmwf_ifs025_official", "gfs_0p25_official", "icon_global_official"
    }
    icon = next(item for item in accepted if item["provider_id"] == "icon_global_open_meteo")
    assert icon["source_class"] == "aggregator_republisher"
    assert icon["prospective_strict_asof_eligible"] is False
    assert icon["arrival_evidence_type"] == "RETROSPECTIVE_REBUILD_NOT_ARRIVAL"

    evidence = build_et_history_evidence(
        model_rows=rows,
        observations=[_truth()],
        station_lane_map={"ZUUU": "amsc"},
        as_of_utc="2026-07-25T15:00:00Z",
        history_only=True,
    )
    assert evidence["state"] == "READY_FOR_STRICT_ASOF"
    assert evidence["checks"]["all_joined_model_sets_at_least_two"] is True
    joined = evidence["joined_station_days"][0]
    assert joined["model_roles"]["icon_global_official"] == "icon_global_open_meteo"
    assert joined["model_source_classes"]["icon_global_open_meteo"] == ["aggregator_republisher"]


def test_native_icon_wins_same_role_overlap() -> None:
    native = _row("icon_global_official", temperature=30.0)
    aggregator = _row("icon_global_open_meteo", temperature=20.0)
    native["daily_curve_complete"] = True
    aggregator["daily_curve_complete"] = True
    native["sample_count"] = aggregator["sample_count"] = 24
    selected = select_preferred_model_rows([aggregator, native])
    assert len(selected) == 1
    assert selected[0]["provider_id"] == "icon_global_official"
    assert selected[0]["temperature_2m_c"] == 30.0


def test_historical_run_timestamp_formats_normalize_before_generation_join() -> None:
    icon = _row("icon_global_open_meteo", temperature=28.5)
    icon["model_run_initialization_utc"] = "2026-07-23T00:00Z"
    payload = {
        "schema": "bayes_basket_target_historical_aggregator_rebuild_evidence_v1",
        "reconstructed_at_utc": "2026-07-25T10:00:00Z",
        "model_rows": [icon],
    }
    rows = load_historical_model_rows(
        payload,
        as_of_utc="2026-07-25T15:00:00Z",
        station_timezone_map={"ZUUU": "America/New_York"},
    )
    assert rows[0]["model_run_initialization_utc"] == "2026-07-23T00:00:00Z"
    assert rows[0]["valid_time_utc"] == "2026-07-24T16:00:00Z"


def test_aggregator_cannot_enter_native_arrival_index() -> None:
    with pytest.raises(ContractError, match="model_arrival_provider_not_allowed"):
        validate_model_arrival({
            "provider_id": "icon_global_open_meteo",
            "model_id": "ICON",
            "station_id": "ZUUU",
            "target_local_date": "2026-07-24",
            "target_timezone": TARGET_TIMEZONE,
            "run_initialization_utc": "2026-07-23T00:00:00Z",
            "valid_time_utc": "2026-07-24T16:00:00Z",
            "published_at_utc": "2026-07-23T01:00:00Z",
            "received_at_utc": "2026-07-23T01:05:00Z",
            "decision_at_utc": "2026-07-25T15:00:00Z",
            "publication_time_source": "aggregator",
            "source_url": "https://historical-forecast-api.open-meteo.com/v1/forecast",
            "raw_payload_sha256s": [SHA_A],
            "receipt_sha256": SHA_A,
            "capture_mode": "historical_rebuild",
            "historical_arrival_time_observed": False,
            "aggregator_or_republisher": True,
            "open_meteo": True,
        })
