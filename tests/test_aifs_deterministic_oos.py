from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
import hashlib

import pytest

from bayes_basket_target.aifs_deterministic_oos import (
    INDEPENDENCE_FAMILY_ID,
    MIN_HOLDOUT_DAYS,
    MIN_TRAIN_DAYS,
    PRODUCT_ID,
    PROVIDER_ID,
    build_aifs_deterministic_strict_oos_candidate,
    build_aifs_deterministic_training_facts,
)
from bayes_basket_target.contracts import ContractError, content_sha256
from bayes_basket_target.model_arrival import validate_model_arrival_capture
from bayes_basket_target.native_model_decode import CURVE_SCHEMA


STATION = "KATL"
TIMEZONE = "America/New_York"


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _day(index: int) -> date:
    return date(2026, 6, 1) + timedelta(days=index)


def _case(index: int, *, truth_delay_hours: int = 0) -> tuple[dict[str, object], list[dict[str, object]], dict[str, object]]:
    day = _day(index)
    event_date = day.isoformat()
    run = datetime.combine(day - timedelta(days=1), time(18), tzinfo=timezone.utc)
    published = run + timedelta(hours=2)
    received = published + timedelta(minutes=10)
    values = [18.0 + index / 10.0, 19.0 + index / 10.0, 20.0 + index / 10.0, 21.0 + index / 10.0]
    valid_times = [
        datetime.combine(day, time(hour), tzinfo=timezone.utc)
        for hour in (6, 12, 18)
    ] + [datetime.combine(day + timedelta(days=1), time(0), tzinfo=timezone.utc)]
    points: list[dict[str, object]] = []
    arrivals: list[dict[str, object]] = []
    for ordinal, (valid, temperature) in enumerate(zip(valid_times, values)):
        raw_sha = _sha(f"raw-{index}-{ordinal}")
        receipt_sha = _sha(f"receipt-{index}-{ordinal}")
        points.append({
            "station_id": STATION,
            "event_date": event_date,
            "provider_id": PROVIDER_ID,
            "model_id": "ECMWF",
            "model_run_initialization_utc": _stamp(run),
            "valid_time_utc": _stamp(valid),
            "temperature_2m_c": temperature,
            "raw_message_sha256": raw_sha,
            "receipt_sha256": receipt_sha,
            "published_at_utc": _stamp(published),
            "received_at_utc": _stamp(received),
        })
        arrivals.append(validate_model_arrival_capture({
            "arrival_id": f"aifs-{index}-{ordinal}",
            "provider_id": PROVIDER_ID,
            "model_id": "ECMWF",
            "station_id": STATION,
            "target_local_date": event_date,
            "target_timezone": TIMEZONE,
            "run_initialization_utc": _stamp(run),
            "valid_time_utc": _stamp(valid),
            "published_at_utc": _stamp(published),
            "received_at_utc": _stamp(received),
            "decision_at_utc": _stamp(received + timedelta(minutes=1)),
            "publication_time_source": "provider_http_last_modified",
            "source_url": "https://ecmwf-forecasts.s3.eu-central-1.amazonaws.com/aifs.grib2",
            "raw_payload_sha256s": [raw_sha],
            "receipt_sha256": receipt_sha,
            "capture_mode": "prospective",
            "historical_arrival_time_observed": True,
            "aggregator_or_republisher": False,
            "open_meteo": False,
        }))
    coverage = {"point_count": 4, "endpoint_tolerance_hours": 6.0}
    curve_identity = {
        "station_id": STATION,
        "event_date": event_date,
        "provider_id": PROVIDER_ID,
        "model_id": "ECMWF",
        "model_run_initialization_utc": _stamp(run),
        "points": points,
        "coverage": coverage,
        "target_timezone": TIMEZONE,
        "blockers": [],
    }
    curve = {
        "schema": CURVE_SCHEMA,
        **curve_identity,
        "state": "COMPLETE",
        "forecast_daily_max_c": max(values),
        "curve_sha256": content_sha256(curve_identity),
    }
    committed = datetime.combine(day + timedelta(days=1), time(5), tzinfo=timezone.utc) + timedelta(hours=truth_delay_hours)
    truth = {
        "station_id": STATION,
        "event_date": event_date,
        "source_lane": "AWC",
        "station_timezone": TIMEZONE,
        "settlement_definition": "official_daily_tmax_c",
        "official_tmax_c": max(values) + (-0.4 if index % 2 else 0.6),
        "published_at_utc": _stamp(datetime.combine(day, time(23), tzinfo=timezone.utc)),
        "canonical_committed_at_utc": _stamp(committed),
        "qc_status": "ACCEPTED",
        "content_sha256": _sha(f"truth-{index}-{truth_delay_hours}"),
    }
    return curve, arrivals, truth


def _inputs(*, count: int = 28, delayed_truth_index: int | None = None):
    curves: list[dict[str, object]] = []
    arrivals: list[dict[str, object]] = []
    truths: list[dict[str, object]] = []
    for index in range(count):
        curve, point_arrivals, truth = _case(
            index,
            truth_delay_hours=24 if delayed_truth_index == index else 0,
        )
        curves.append(curve)
        arrivals.extend(point_arrivals)
        truths.append(truth)
    return curves, arrivals, truths


def test_official_aifs_curves_build_content_addressed_facts_and_strict_oos() -> None:
    # A d2 curve is received before the immediately preceding local day has
    # settled.  29 cases therefore provide 20 truth-available training days
    # for each of eight genuinely forward holdouts.
    curves, arrivals, truths = _inputs(count=MIN_TRAIN_DAYS + MIN_HOLDOUT_DAYS + 1)
    batch = build_aifs_deterministic_training_facts(
        curves, arrivals, truths, as_of_utc="2026-07-31T00:00:00Z"
    )

    assert batch["fact_count"] == MIN_TRAIN_DAYS + MIN_HOLDOUT_DAYS + 1
    assert batch["independence_family_id"] == INDEPENDENCE_FAMILY_ID
    assert batch["boundary"]["writes_native_ensemble_tables"] is False
    assert all(
        fact["source_authority"]["truth_source_lane"] == "AWC"
        and fact["boundary"]["creates_formal_provider_binding"] is False
        for fact in batch["facts"]
    )
    result = build_aifs_deterministic_strict_oos_candidate(batch)

    assert result["state"] == "STRICT_OOS_CANDIDATES_NOT_BINDING"
    assert result["formal_provider_binding_generated"] is False
    candidate = result["candidates"][0]
    assert candidate["product_id"] == PRODUCT_ID
    assert candidate["same_independence_family_as_ifs"] is True
    assert candidate["counts_as_distinct_independence_family"] is False
    assert len(candidate["predictions"]) == MIN_HOLDOUT_DAYS
    assert all(item["strict_oos"] is True for item in candidate["predictions"])
    assert candidate["checks"]["native_ensemble_tables_written"] is False


def test_oos_fails_closed_when_20_train_plus_8_holdout_are_not_available() -> None:
    curves, arrivals, truths = _inputs(count=MIN_TRAIN_DAYS + MIN_HOLDOUT_DAYS - 1)
    batch = build_aifs_deterministic_training_facts(
        curves, arrivals, truths, as_of_utc="2026-07-31T00:00:00Z"
    )

    with pytest.raises(ContractError, match="aifs_deterministic_oos_insufficient_samples"):
        build_aifs_deterministic_strict_oos_candidate(batch)


def test_oos_fails_closed_when_prior_awc_truth_was_not_known_at_holdout_decision() -> None:
    curves, arrivals, truths = _inputs(delayed_truth_index=MIN_TRAIN_DAYS - 1)
    batch = build_aifs_deterministic_training_facts(
        curves, arrivals, truths, as_of_utc="2026-07-31T00:00:00Z"
    )

    with pytest.raises(ContractError, match="aifs_deterministic_oos_train_truth_unavailable"):
        build_aifs_deterministic_strict_oos_candidate(batch)


def test_republisher_curve_is_rejected_before_any_training_fact_is_created() -> None:
    curves, arrivals, truths = _inputs(count=1)
    curves[0]["provider_id"] = "icon_global_open_meteo"

    with pytest.raises(ContractError, match="aifs_deterministic_curve_identity_invalid"):
        build_aifs_deterministic_training_facts(
            curves, arrivals, truths, as_of_utc="2026-07-31T00:00:00Z"
        )


def test_missing_official_arrival_fails_closed_instead_of_backfilling_from_curve() -> None:
    curves, arrivals, truths = _inputs(count=1)

    with pytest.raises(ContractError, match="aifs_deterministic_curve_arrival_missing"):
        build_aifs_deterministic_training_facts(
            curves, arrivals[:-1], truths, as_of_utc="2026-07-31T00:00:00Z"
        )


def test_tampered_product_fact_cannot_be_promoted_into_oos() -> None:
    curves, arrivals, truths = _inputs(count=MIN_TRAIN_DAYS + MIN_HOLDOUT_DAYS + 1)
    batch = build_aifs_deterministic_training_facts(
        curves, arrivals, truths, as_of_utc="2026-07-31T00:00:00Z"
    )
    batch["facts"][0]["independence_family_id"] = "FORGED_EXTRA_FAMILY"

    with pytest.raises(ContractError, match="aifs_deterministic_training_batch_sha_mismatch"):
        build_aifs_deterministic_strict_oos_candidate(batch)


def test_future_or_preclose_aifs_truth_cannot_become_an_oos_fact() -> None:
    curves, arrivals, truths = _inputs(
        count=MIN_TRAIN_DAYS + MIN_HOLDOUT_DAYS + 1
    )
    batch = build_aifs_deterministic_training_facts(
        curves, arrivals, truths, as_of_utc="2026-07-31T00:00:00Z"
    )
    fact = dict(batch["facts"][0])
    fact["truth_available_at_utc"] = "2026-06-01T00:00:00Z"
    identity = {
        key: value
        for key, value in fact.items()
        if key not in {"training_fact_id", "content_sha256"}
    }
    fact["content_sha256"] = content_sha256(identity)
    fact["training_fact_id"] = "aifs-det-" + fact["content_sha256"][:32]
    mutated = {**batch, "facts": [fact, *batch["facts"][1:]]}
    mutated["content_sha256"] = content_sha256(
        {key: value for key, value in mutated.items() if key != "content_sha256"}
    )

    with pytest.raises(ContractError, match="truth_before_settlement_close"):
        build_aifs_deterministic_strict_oos_candidate(mutated)
