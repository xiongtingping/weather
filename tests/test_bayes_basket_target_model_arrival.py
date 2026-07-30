from __future__ import annotations

import pytest

from bayes_basket_target.contracts import ContractError
from bayes_basket_target.model_arrival import (
    build_blocked_receipt,
    load_model_arrival_receipts,
    validate_model_arrival_capture,
    validate_model_arrival,
)


SHA = "a" * 64


def _row() -> dict[str, object]:
    return {
        "arrival_id": "arrival-1",
        "provider_id": "icon_global_official",
        "model_id": "ICON",
        "station_id": "KJFK",
        "target_local_date": "2026-07-23",
        "target_timezone": "America/New_York",
        "run_initialization_utc": "2026-07-23T00:00:00Z",
        "valid_time_utc": "2026-07-23T16:00:00Z",
        "published_at_utc": "2026-07-23T05:30:00Z",
        "received_at_utc": "2026-07-23T05:34:12Z",
        "decision_at_utc": "2026-07-23T06:00:00Z",
        "publication_time_source": "provider_http_last_modified",
        "source_url": "https://opendata.dwd.de/weather/nwp/icon/grib/00/model.grib2.bz2",
        "raw_payload_sha256s": [SHA],
        "receipt_sha256": "b" * 64,
        "capture_mode": "prospective",
        "historical_arrival_time_observed": True,
        "aggregator_or_republisher": False,
        "open_meteo": False,
    }


def test_prospective_arrival_requires_native_ordering_and_binds_content() -> None:
    row = validate_model_arrival(_row())
    assert row["schema"] == "bayes_basket_target_prospective_model_arrival_v1"
    assert row["model_id"] == "ICON"
    assert row["published_at_utc"] <= row["received_at_utc"] <= row["decision_at_utc"]
    assert len(row["content_sha256"]) == 64
    assert row["live_order_authorized"] is False


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("historical_arrival_time_observed", False, "historical_arrival_time_not_observed"),
        ("capture_mode", "historical_replay", "model_arrival_capture_not_prospective"),
        ("aggregator_or_republisher", True, "model_arrival_aggregator_forbidden"),
        ("open_meteo", True, "model_arrival_open_meteo_forbidden"),
        ("received_at_utc", "2026-07-23T06:01:00Z", "model_received_after_decision"),
    ],
)
def test_historical_or_invalid_arrival_fails_closed(field: str, value: object, reason: str) -> None:
    row = _row()
    row[field] = value
    with pytest.raises(ContractError, match=reason):
        validate_model_arrival(row)


def test_valid_time_must_belong_to_target_local_date() -> None:
    row = _row()
    row["valid_time_utc"] = "2026-07-24T05:00:00Z"
    with pytest.raises(ContractError, match="model_valid_time_target_local_date_mismatch"):
        validate_model_arrival(row)


def test_future_et_target_day_is_rejected_before_arrival_indexing() -> None:
    row = _row()
    row.update({
        "target_local_date": "2026-07-24",
        "valid_time_utc": "2026-07-24T16:00:00Z",
        "decision_at_utc": "2026-07-23T18:00:00Z",
    })
    with pytest.raises(ContractError, match="future_event_day_not_allowed"):
        validate_model_arrival(row)


def test_receipt_index_rejects_bad_rows_but_never_promotes() -> None:
    accepted, audit = load_model_arrival_receipts(
        [_row(), {**_row(), "arrival_id": "arrival-2", "historical_arrival_time_observed": False}],
        decision_at_utc="2026-07-23T06:00:00Z",
    )
    assert len(accepted) == 1
    assert audit["schema"] == "bayes_basket_target_model_arrival_index_v2"
    assert audit["accepted_count"] == 1
    assert audit["blocked_count"] == 1
    assert audit["promotion_allowed"] is False
    assert audit["production_authority"] is False


def test_pre_event_capture_is_raw_only_until_formal_clock_reaches_et_start() -> None:
    row = _row()
    row.update({
        "target_local_date": "2026-07-24",
        "valid_time_utc": "2026-07-24T16:00:00Z",
        "decision_at_utc": "2026-07-23T18:00:00Z",
    })
    captured = validate_model_arrival_capture(row)
    assert captured["capture_phase"] == "prospective_pre_event"
    assert captured["p0_consumption_eligible_at_capture"] is True
    assert captured["p1_consumption_eligible_at_capture"] is False

    early, early_audit = load_model_arrival_receipts(
        [row],
        decision_at_utc="2026-07-24T03:59:59Z",
    )
    assert early == []
    assert early_audit["blocked"][0]["reason"] == "future_event_day_not_allowed"

    accepted, audit = load_model_arrival_receipts(
        [row],
        decision_at_utc="2026-07-24T18:00:00Z",
    )
    assert len(accepted) == 1
    assert accepted[0]["capture_phase"] == "prospective_pre_event"
    assert audit["accepted_count"] == 1
    assert audit["blocked_count"] == 0


def test_capture_rejects_target_outside_bounded_d2_d1_window() -> None:
    row = _row()
    row.update({
        "target_local_date": "2026-07-26",
        "run_initialization_utc": "2026-07-23T00:00:00Z",
        "valid_time_utc": "2026-07-26T16:00:00Z",
        "decision_at_utc": "2026-07-23T18:00:00Z",
    })
    with pytest.raises(ContractError, match="outside_d2_d1_d0_window"):
        validate_model_arrival_capture(row)


def test_d0_model_update_is_valid_for_versioned_p0() -> None:
    row = _row()
    row.update({
        "target_local_date": "2026-07-23",
        "run_initialization_utc": "2026-07-23T12:00:00Z",
        "valid_time_utc": "2026-07-23T18:00:00Z",
        "published_at_utc": "2026-07-23T14:30:00Z",
        "received_at_utc": "2026-07-23T14:34:12Z",
        "decision_at_utc": "2026-07-23T15:00:00Z",
    })
    captured = validate_model_arrival_capture(row)
    assert captured["capture_phase"] == "prospective_open_event"
    assert captured["p0_consumption_eligible_at_capture"] is True


def test_previous_complete_18z_release_is_explicit_d2() -> None:
    row = _row()
    row.update({
        "target_local_date": "2026-07-27",
        "run_initialization_utc": "2026-07-25T18:00:00Z",
        "valid_time_utc": "2026-07-27T16:00:00Z",
        "published_at_utc": "2026-07-25T20:30:00Z",
        "received_at_utc": "2026-07-25T20:34:12Z",
        "decision_at_utc": "2026-07-26T03:50:00Z",
    })
    captured = validate_model_arrival_capture(row)
    assert captured["capture_phase"] == "prospective_pre_event"
    assert captured["p0_consumption_eligible_at_capture"] is True


def test_formal_gate_cannot_be_bypassed_by_low_high_low_clock_rewind() -> None:
    row = _row()
    row.update({
        "target_local_date": "2026-07-24",
        "valid_time_utc": "2026-07-24T16:00:00Z",
        "decision_at_utc": "2026-07-23T18:00:00Z",
    })
    with pytest.raises(ContractError, match="future_event_day_not_allowed"):
        validate_model_arrival(row, decision_at_utc="2026-07-24T03:59:59Z")
    assert validate_model_arrival(
        row,
        decision_at_utc="2026-07-24T04:00:00Z",
    )["capture_phase"] == "prospective_pre_event"
    with pytest.raises(ContractError, match="future_event_day_not_allowed"):
        validate_model_arrival(row, decision_at_utc="2026-07-24T03:59:59Z")


def test_receipt_index_accepts_distinct_valid_times_from_one_run() -> None:
    first = _row()
    second = {**_row(), "arrival_id": "arrival-2", "valid_time_utc": "2026-07-23T22:00:00Z"}

    accepted, audit = load_model_arrival_receipts(
        [first, second],
        decision_at_utc="2026-07-23T06:00:00Z",
    )

    assert len(accepted) == 2
    assert audit["accepted_count"] == 2
    assert audit["blocked_count"] == 0


def test_blocked_receipt_is_non_authorizing_and_content_addressed() -> None:
    receipt = build_blocked_receipt(reason_codes=["historical_arrival_time_not_observed"])
    assert receipt["schema"].endswith("_blocked_v1")
    assert receipt["status"] == "BLOCKED"
    assert receipt["orders_submitted"] == 0
    assert len(receipt["receipt_sha256"]) == 64
