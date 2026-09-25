"""Pure strict-OOS evidence for the official deterministic ECMWF AIFS Single.

The AIFS 0.25-degree Single product is one deterministic forecast curve.  It
is deliberately *not* an ensemble member and therefore has no right to use
the ``calibration_native_ensemble_*`` schema.  This module only builds
content-addressed, product-specific training facts and rolling-origin OOS
candidates.  A later, separately governed P0 contract may consume an
accepted candidate; this module can never create a formal provider binding or
an activation.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timezone
import math
from typing import Any

from .canonical_contract import FORMAL_TRUTH_LANES
from .contracts import ContractError, content_sha256
from .model_arrival import (
    SCHEMA as ARRIVAL_SCHEMA,
    model_arrival_key,
    validate_model_arrival_capture,
)
from .native_model_decode import (
    CURVE_SCHEMA,
    MIN_CURVE_POINTS,
    MIN_CURVE_POINTS_BY_PROVIDER,
)
from .settlement_time import settlement_day_window
from .station_registry import station_coordinates
from .strict_asof_replay import (
    lead_bucket_for_maturity,
    maturity_key_for_model_run,
    season_bin_for_event_date,
)


TRAINING_FACT_SCHEMA = (
    "ecmwf_aifs_single_official_deterministic_training_fact_v1"
)
TRAINING_BATCH_SCHEMA = (
    "ecmwf_aifs_single_official_deterministic_training_fact_batch_v1"
)
STRICT_OOS_SCHEMA = "ecmwf_aifs_single_official_strict_rolling_oos_v1"
PREDICTION_SCHEMA = (
    "ecmwf_aifs_single_official_strict_rolling_oos_prediction_v1"
)
PROVIDER_ID = "ecmwf_aifs025_official"
PRODUCT_ID = "ECMWF_AIFS_SINGLE_0P25"
MODEL_ID = "ECMWF"
INDEPENDENCE_FAMILY_ID = "ECMWF"
MIN_TRAIN_DAYS = 20
MIN_HOLDOUT_DAYS = 8
MAX_FACTS = 366


def _utc(name: str, value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ContractError(f"aifs_deterministic_{name}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"aifs_deterministic_{name}_timezone_required")
    return parsed.astimezone(timezone.utc)


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")


def _sha(name: str, value: Any) -> str:
    result = str(value or "").strip().lower()
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
        raise ContractError(f"aifs_deterministic_{name}_invalid_sha256")
    return result


def _finite(name: str, value: Any) -> float:
    if isinstance(value, bool):
        raise ContractError(f"aifs_deterministic_{name}_invalid")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"aifs_deterministic_{name}_invalid") from exc
    if not math.isfinite(result):
        raise ContractError(f"aifs_deterministic_{name}_invalid")
    return result


def _curve_identity(curve: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "station_id": str(curve.get("station_id") or "").strip().upper(),
        "event_date": str(curve.get("event_date") or "").strip(),
        "provider_id": str(curve.get("provider_id") or "").strip().lower(),
        "model_id": str(curve.get("model_id") or "").strip().upper(),
        "model_run_initialization_utc": str(
            curve.get("model_run_initialization_utc") or ""
        ).strip().replace("+00:00", "Z"),
        "points": curve.get("points"),
        "coverage": curve.get("coverage"),
        "target_timezone": str(curve.get("target_timezone") or "").strip(),
        "blockers": curve.get("blockers"),
    }


def _validate_curve(curve: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(curve, Mapping) or curve.get("schema") != CURVE_SCHEMA:
        raise ContractError("aifs_deterministic_curve_schema_invalid")
    if curve.get("state") != "COMPLETE":
        raise ContractError("aifs_deterministic_curve_incomplete")
    identity = _curve_identity(curve)
    if (
        identity["provider_id"] != PROVIDER_ID
        or identity["model_id"] != MODEL_ID
        or not identity["station_id"]
        or not identity["event_date"]
        or not identity["target_timezone"]
    ):
        raise ContractError("aifs_deterministic_curve_identity_invalid")
    _utc("curve_run_initialization", identity["model_run_initialization_utc"])
    if str(curve.get("curve_sha256") or "") != content_sha256(identity):
        raise ContractError("aifs_deterministic_curve_sha_mismatch")
    points = identity["points"]
    # AIFS Single has a native six-hour cadence.  In settlement timezones
    # whose local-day edges fall between that cadence, a fully covered daily
    # curve legitimately has three in-day samples (for example NZWN).  The
    # native curve materializer has already applied the provider-specific
    # endpoint and gap gates before it signs a COMPLETE curve; do not
    # reintroduce a generic four-point rule at the fact boundary.
    minimum_point_count = MIN_CURVE_POINTS_BY_PROVIDER.get(
        identity["provider_id"], MIN_CURVE_POINTS
    )
    if not isinstance(points, list) or len(points) < minimum_point_count:
        raise ContractError("aifs_deterministic_curve_points_insufficient")
    by_valid: dict[str, dict[str, Any]] = {}
    for raw in points:
        if not isinstance(raw, Mapping):
            raise ContractError("aifs_deterministic_curve_point_invalid")
        provider = str(raw.get("provider_id") or "").strip().lower()
        station = str(raw.get("station_id") or "").strip().upper()
        event_date = str(raw.get("event_date") or "").strip()
        run_init = str(raw.get("model_run_initialization_utc") or "").strip().replace(
            "+00:00", "Z"
        )
        valid_time = _stamp(_utc("curve_point_valid_time", raw.get("valid_time_utc")))
        if (
            provider != PROVIDER_ID
            or station != identity["station_id"]
            or event_date != identity["event_date"]
            or run_init != identity["model_run_initialization_utc"]
            or valid_time in by_valid
        ):
            raise ContractError("aifs_deterministic_curve_point_identity_invalid")
        published = _utc("curve_point_published", raw.get("published_at_utc"))
        received = _utc("curve_point_received", raw.get("received_at_utc"))
        if published > received:
            raise ContractError("aifs_deterministic_curve_point_timestamp_invalid")
        by_valid[valid_time] = {
            "valid_time_utc": valid_time,
            "raw_message_sha256": _sha(
                "curve_point_raw_message", raw.get("raw_message_sha256")
            ),
            "receipt_sha256": _sha(
                "curve_point_receipt", raw.get("receipt_sha256")
            ),
            "published_at_utc": _stamp(published),
            "received_at_utc": _stamp(received),
        }
    forecast = _finite("curve_forecast_daily_max_c", curve.get("forecast_daily_max_c"))
    if abs(forecast - max(_finite("curve_point_temperature", row.get("temperature_2m_c")) for row in points)) > 1e-9:
        raise ContractError("aifs_deterministic_curve_daily_max_mismatch")
    return {
        **identity,
        "curve_sha256": str(curve["curve_sha256"]),
        "forecast_daily_max_c": forecast,
        "points_by_valid_time": by_valid,
    }


def _arrival_index(
    arrivals: Iterable[Mapping[str, Any]],
    *,
    as_of: datetime,
) -> dict[tuple[str, str, str, str, str], dict[str, Any]]:
    result: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
    for raw in arrivals:
        row = validate_model_arrival_capture(raw)
        if row.get("schema") != ARRIVAL_SCHEMA or row["provider_id"] != PROVIDER_ID:
            raise ContractError("aifs_deterministic_arrival_identity_invalid")
        if _utc("arrival_received", row["received_at_utc"]) > as_of:
            raise ContractError("aifs_deterministic_arrival_after_asof")
        supplied = raw.get("content_sha256")
        if supplied is not None and str(supplied) != row["content_sha256"]:
            raise ContractError("aifs_deterministic_arrival_sha_mismatch")
        key = model_arrival_key(row)
        previous = result.get(key)
        if previous is not None and previous != row:
            raise ContractError("aifs_deterministic_arrival_duplicate_conflict")
        result[key] = row
    if not result:
        raise ContractError("aifs_deterministic_arrivals_missing")
    return result


def _truth_index(
    truths: Iterable[Mapping[str, Any]],
    *,
    as_of: datetime,
) -> dict[tuple[str, str], dict[str, Any]]:
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for raw in truths:
        if not isinstance(raw, Mapping):
            raise ContractError("aifs_deterministic_truth_mapping_required")
        station = str(raw.get("station_id") or "").strip().upper()
        event_date = str(raw.get("local_date") or raw.get("event_date") or "").strip()
        source_lane = str(raw.get("source_lane") or "").strip().upper()
        timezone_name = str(
            raw.get("settlement_timezone")
            or raw.get("station_timezone")
            or raw.get("target_timezone")
            or ""
        ).strip()
        if (
            not station
            or not event_date
            or source_lane not in FORMAL_TRUTH_LANES
            or not timezone_name
            or str(raw.get("settlement_definition") or "official_daily_tmax_c")
            != "official_daily_tmax_c"
            or str(raw.get("qc_status") or "").upper() != "ACCEPTED"
        ):
            raise ContractError("aifs_deterministic_truth_authority_invalid")
        truth_available_at = _utc(
            "truth_available",
            raw.get("canonical_committed_at_utc")
            or raw.get("canonical_committed_at")
            or raw.get("received_at_utc"),
        )
        published_raw = raw.get("published_at_utc") or raw.get("published_at")
        if published_raw is not None and _utc("truth_published", published_raw) > truth_available_at:
            raise ContractError("aifs_deterministic_truth_timestamp_invalid")
        if truth_available_at > as_of:
            raise ContractError("aifs_deterministic_truth_after_asof")
        closed_at = _utc(
            "truth_settlement_closed",
            settlement_day_window(event_date, timezone_name)["target_day_end_utc"],
        )
        if as_of < closed_at:
            raise ContractError("aifs_deterministic_truth_settlement_open")
        item = {
            "station_id": station,
            "event_date": event_date,
            "target_timezone": timezone_name,
            "official_tmax_c": _finite(
                "truth_official_tmax_c", raw.get("official_tmax_c")
            ),
            "truth_content_sha256": _sha("truth_content", raw.get("content_sha256")),
            "truth_available_at_utc": _stamp(truth_available_at),
            "settlement_closed_at_utc": _stamp(closed_at),
        }
        key = (station, event_date)
        previous = result.get(key)
        if previous is not None and previous != item:
            raise ContractError("aifs_deterministic_truth_duplicate_conflict")
        result[key] = item
    if not result:
        raise ContractError("aifs_deterministic_truths_missing")
    return result


def build_aifs_deterministic_training_facts(
    curves: Iterable[Mapping[str, Any]],
    arrivals: Iterable[Mapping[str, Any]],
    awc_truths: Iterable[Mapping[str, Any]],
    *,
    as_of_utc: str,
) -> dict[str, Any]:
    """Join official AIFS curves to official closed-day AWC truth.

    The all-or-nothing join prevents a partial four-point curve, an unbound
    arrival, or a non-AWC truth row from silently becoming a calibration case.
    The returned facts are research evidence only and have no DB side effect.
    """

    as_of = _utc("as_of", as_of_utc)
    curve_rows = list(curves)
    if not curve_rows or len(curve_rows) > MAX_FACTS:
        raise ContractError("aifs_deterministic_curve_count_invalid")
    arrival_by_key = _arrival_index(arrivals, as_of=as_of)
    truth_by_key = _truth_index(awc_truths, as_of=as_of)
    facts: list[dict[str, Any]] = []
    seen_scope: set[tuple[str, str, str]] = set()
    for raw_curve in curve_rows:
        curve = _validate_curve(raw_curve)
        scope = (
            str(curve["station_id"]),
            str(curve["event_date"]),
            str(curve["model_run_initialization_utc"]),
        )
        if scope in seen_scope:
            raise ContractError("aifs_deterministic_curve_scope_duplicate")
        seen_scope.add(scope)
        matched_arrivals: list[dict[str, Any]] = []
        for valid_time, point in sorted(curve["points_by_valid_time"].items()):
            key = (
                PROVIDER_ID,
                str(curve["station_id"]),
                str(curve["event_date"]),
                str(curve["model_run_initialization_utc"]),
                valid_time,
            )
            arrival = arrival_by_key.get(key)
            if arrival is None:
                raise ContractError("aifs_deterministic_curve_arrival_missing")
            if point["raw_message_sha256"] not in set(arrival["raw_payload_sha256s"]):
                raise ContractError("aifs_deterministic_curve_arrival_raw_mismatch")
            if _utc("arrival_published", arrival["published_at_utc"]) != _utc(
                "curve_published", point["published_at_utc"]
            ) or _utc("arrival_received", arrival["received_at_utc"]) != _utc(
                "curve_received", point["received_at_utc"]
            ):
                raise ContractError("aifs_deterministic_curve_arrival_timestamp_mismatch")
            matched_arrivals.append(arrival)
        truth = truth_by_key.get((str(curve["station_id"]), str(curve["event_date"])))
        if truth is None:
            raise ContractError("aifs_deterministic_curve_truth_missing")
        if truth["target_timezone"] != curve["target_timezone"]:
            raise ContractError("aifs_deterministic_curve_truth_timezone_mismatch")
        identity = {
            "schema": TRAINING_FACT_SCHEMA,
            "product_id": PRODUCT_ID,
            "provider_id": PROVIDER_ID,
            "model_id": MODEL_ID,
            "independence_family_id": INDEPENDENCE_FAMILY_ID,
            "station_id": curve["station_id"],
            "event_date": curve["event_date"],
            "target_timezone": curve["target_timezone"],
            "model_run_initialization_utc": curve["model_run_initialization_utc"],
            "run_cycle": str(curve["model_run_initialization_utc"])[11:13] + "Z",
            "forecast_daily_max_c": curve["forecast_daily_max_c"],
            "official_tmax_c": truth["official_tmax_c"],
            "curve_sha256": curve["curve_sha256"],
            "arrival_content_sha256s": [
                item["content_sha256"] for item in matched_arrivals
            ],
            "arrival_receipt_sha256s": [item["receipt_sha256"] for item in matched_arrivals],
            "raw_payload_sha256s": sorted({
                item for arrival in matched_arrivals
                for item in arrival["raw_payload_sha256s"]
            }),
            "curve_available_at_utc": max(
                item["received_at_utc"] for item in matched_arrivals
            ),
            "truth_content_sha256": truth["truth_content_sha256"],
            "truth_available_at_utc": truth["truth_available_at_utc"],
            "settlement_closed_at_utc": truth["settlement_closed_at_utc"],
            "source_authority": {
                "forecast_source_role": "official_native",
                # AIFS Single's station arrival wrapper carries an immutable
                # provider receipt/raw hash for every decoded point.  Keep
                # this distinct from ICON's cohort-only timestamp lineage.
                "native_arrival_index_eligible": True,
                "arrival_lineage_status": "NATIVE_PROVIDER_RECEIPT_INDEX_VERIFIED",
                "truth_source_lane": "AWC",
                "truth_closed_day_required": True,
                "same_independence_family_as_ifs": True,
                "counts_as_distinct_independence_family": False,
            },
            "boundary": {
                "writes_production_database": False,
                "writes_native_ensemble_tables": False,
                "creates_formal_provider_binding": False,
                "formal_p0_eligible": False,
                "calls_order_gateway": False,
                "places_orders": False,
            },
        }
        fact = {
            **identity,
            "training_fact_id": "aifs-det-" + content_sha256(identity)[:32],
            "content_sha256": content_sha256(identity),
        }
        facts.append(fact)
    facts.sort(
        key=lambda item: (
            str(item["station_id"]), str(item["run_cycle"]), str(item["event_date"])
        )
    )
    identity = {
        "schema": TRAINING_BATCH_SCHEMA,
        "as_of_utc": _stamp(as_of),
        "product_id": PRODUCT_ID,
        "provider_id": PROVIDER_ID,
        "model_id": MODEL_ID,
        "independence_family_id": INDEPENDENCE_FAMILY_ID,
        "facts": facts,
        "fact_count": len(facts),
        "state": "ACCUMULATING_STRICT_OOS",
        "boundary": {
            "writes_production_database": False,
            "writes_native_ensemble_tables": False,
            "creates_formal_provider_binding": False,
            "formal_p0_eligible": False,
            "calls_order_gateway": False,
            "places_orders": False,
        },
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def _validate_training_batch(batch: Mapping[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(batch, Mapping) or batch.get("schema") != TRAINING_BATCH_SCHEMA:
        raise ContractError("aifs_deterministic_training_batch_schema_invalid")
    identity = {key: value for key, value in batch.items() if key != "content_sha256"}
    if str(batch.get("content_sha256") or "") != content_sha256(identity):
        raise ContractError("aifs_deterministic_training_batch_sha_mismatch")
    if (
        batch.get("provider_id") != PROVIDER_ID
        or batch.get("product_id") != PRODUCT_ID
        or batch.get("model_id") != MODEL_ID
        or batch.get("independence_family_id") != INDEPENDENCE_FAMILY_ID
    ):
        raise ContractError("aifs_deterministic_training_batch_identity_invalid")
    facts = batch.get("facts")
    if not isinstance(facts, list) or not facts or len(facts) > MAX_FACTS:
        raise ContractError("aifs_deterministic_training_facts_missing")
    if int(batch.get("fact_count") or -1) != len(facts):
        raise ContractError("aifs_deterministic_training_fact_count_mismatch")
    batch_as_of = _utc(
        "training_batch_as_of", batch.get("as_of_utc")
    )
    normalized: list[dict[str, Any]] = []
    for raw in facts:
        if not isinstance(raw, Mapping) or raw.get("schema") != TRAINING_FACT_SCHEMA:
            raise ContractError("aifs_deterministic_training_fact_schema_invalid")
        fact_identity = {
            key: value
            for key, value in raw.items()
            if key not in {"training_fact_id", "content_sha256"}
        }
        digest = content_sha256(fact_identity)
        if (
            str(raw.get("content_sha256") or "") != digest
            or str(raw.get("training_fact_id") or "") != "aifs-det-" + digest[:32]
            or raw.get("provider_id") != PROVIDER_ID
            or raw.get("product_id") != PRODUCT_ID
            or raw.get("model_id") != MODEL_ID
            or raw.get("independence_family_id") != INDEPENDENCE_FAMILY_ID
        ):
            raise ContractError("aifs_deterministic_training_fact_sha_mismatch")
        authority = raw.get("source_authority")
        boundary = raw.get("boundary")
        if (
            not isinstance(authority, Mapping)
            or authority.get("forecast_source_role") != "official_native"
            or authority.get("native_arrival_index_eligible", True) is not True
            or authority.get(
                "arrival_lineage_status",
                "NATIVE_PROVIDER_RECEIPT_INDEX_VERIFIED",
            )
            != "NATIVE_PROVIDER_RECEIPT_INDEX_VERIFIED"
            or authority.get("truth_source_lane") != "AWC"
            or authority.get("same_independence_family_as_ifs") is not True
            or authority.get("counts_as_distinct_independence_family") is not False
            or not isinstance(boundary, Mapping)
            or boundary.get("writes_native_ensemble_tables") is not False
            or boundary.get("creates_formal_provider_binding") is not False
            or boundary.get("formal_p0_eligible") is not False
        ):
            raise ContractError("aifs_deterministic_training_fact_authority_invalid")
        settlement_closed = _utc(
            "training_fact_settlement_closed",
            raw.get("settlement_closed_at_utc"),
        )
        truth_available = _utc(
            "training_fact_truth_available",
            raw.get("truth_available_at_utc"),
        )
        if settlement_closed > batch_as_of:
            raise ContractError("aifs_deterministic_oos_future_event_forbidden")
        if truth_available < settlement_closed:
            raise ContractError(
                "aifs_deterministic_oos_truth_before_settlement_close"
            )
        if truth_available > batch_as_of:
            raise ContractError("aifs_deterministic_oos_truth_after_asof")
        normalized.append(dict(raw))
    return normalized


def _fit_bias(train: Sequence[Mapping[str, Any]]) -> float:
    if not train:
        raise ContractError("aifs_deterministic_oos_train_missing")
    return sum(
        float(row["official_tmax_c"]) - float(row["forecast_daily_max_c"])
        for row in train
    ) / len(train)


def _point_metrics(errors: Sequence[float]) -> dict[str, float]:
    if not errors:
        raise ContractError("aifs_deterministic_oos_metric_rows_missing")
    return {
        "mae_c": sum(abs(value) for value in errors) / len(errors),
        "rmse_c": math.sqrt(
            sum(value * value for value in errors) / len(errors)
        ),
        "mean_error_c": sum(errors) / len(errors),
    }


def _p0_scope_for_fact(fact: Mapping[str, Any]) -> tuple[str, str]:
    """Resolve the same seasonal/lead scope consumed by a later P0 role."""

    station = str(fact["station_id"])
    event_date = str(fact["event_date"])
    timezone_name = str(fact["target_timezone"])
    run_init = str(fact["model_run_initialization_utc"])
    try:
        latitude = float(station_coordinates(station)["latitude"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ContractError("aifs_deterministic_oos_station_coordinate_missing") from exc
    maturity = maturity_key_for_model_run(
        run_init,
        event_date,
        target_timezone=timezone_name,
    )
    return (
        season_bin_for_event_date(event_date, latitude=latitude),
        lead_bucket_for_maturity(maturity),
    )


def build_aifs_deterministic_strict_oos_candidate(
    training_batch: Mapping[str, Any],
    *,
    min_train_days: int = MIN_TRAIN_DAYS,
    min_holdout_days: int = MIN_HOLDOUT_DAYS,
) -> dict[str, Any]:
    """Build strict rolling-origin point-forecast OOS evidence.

    A holdout forecast may only train on earlier station-days whose AWC truth
    was already committed before that forecast curve was fully received.  The
    candidate stays non-authorizing even when all metrics are present.
    """

    if (
        isinstance(min_train_days, bool)
        or isinstance(min_holdout_days, bool)
        or int(min_train_days) < MIN_TRAIN_DAYS
        or int(min_holdout_days) < MIN_HOLDOUT_DAYS
    ):
        raise ContractError("aifs_deterministic_oos_window_below_formal_minimum")
    train_min = int(min_train_days)
    holdout_min = int(min_holdout_days)
    facts = _validate_training_batch(training_batch)
    grouped: dict[tuple[str, str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for fact in facts:
        season_bin, lead_bucket = _p0_scope_for_fact(fact)
        grouped[(
            str(fact["station_id"]),
            str(fact["target_timezone"]),
            str(fact["run_cycle"]),
            season_bin,
            lead_bucket,
        )].append(fact)
    candidates: list[dict[str, Any]] = []
    insufficiencies: list[dict[str, Any]] = []
    for scope, rows in sorted(grouped.items()):
        rows.sort(key=lambda item: str(item["event_date"]))
        dates = [str(item["event_date"]) for item in rows]
        if len(set(dates)) != len(dates):
            raise ContractError("aifs_deterministic_oos_event_date_duplicate")
        if len(rows) < train_min + holdout_min:
            insufficiencies.append({
                "station_id": scope[0],
                "target_timezone": scope[1],
                "run_cycle": scope[2],
                "season_bin": scope[3],
                "lead_bucket": scope[4],
                "available_case_count": len(rows),
                "required_case_count": train_min + holdout_min,
            })
            continue
        holdouts = rows[-holdout_min:]
        predictions: list[dict[str, Any]] = []
        for holdout in holdouts:
            decision = _utc("oos_curve_available", holdout["curve_available_at_utc"])
            eligible_train = [
                row for row in rows
                if str(row["event_date"]) < str(holdout["event_date"])
                and _utc("oos_train_truth_available", row["truth_available_at_utc"])
                <= decision
            ]
            if len(eligible_train) < train_min:
                raise ContractError("aifs_deterministic_oos_train_truth_unavailable")
            bias_c = _fit_bias(eligible_train)
            prediction_c = float(holdout["forecast_daily_max_c"]) + bias_c
            actual_c = float(holdout["official_tmax_c"])
            error_c = prediction_c - actual_c
            climatology_prediction_c = sum(
                float(row["official_tmax_c"]) for row in eligible_train
            ) / len(eligible_train)
            climatology_error_c = climatology_prediction_c - actual_c
            identity = {
                "schema": PREDICTION_SCHEMA,
                "product_id": PRODUCT_ID,
                "provider_id": PROVIDER_ID,
                "independence_family_id": INDEPENDENCE_FAMILY_ID,
                "station_id": holdout["station_id"],
                "event_date": holdout["event_date"],
                "target_timezone": holdout["target_timezone"],
                "run_cycle": holdout["run_cycle"],
                "decision_at_utc": _stamp(decision),
                "trained_through_event_date": eligible_train[-1]["event_date"],
                "train_case_count": len(eligible_train),
                "train_fact_content_sha256s": [
                    row["content_sha256"] for row in eligible_train
                ],
                "holdout_fact_content_sha256": holdout["content_sha256"],
                "bias_c": bias_c,
                "predicted_tmax_c": prediction_c,
                "official_tmax_c": actual_c,
                "error_c": error_c,
                "climatology_predicted_tmax_c": climatology_prediction_c,
                "climatology_error_c": climatology_error_c,
                "strict_oos": True,
                "formal_p0_eligible": False,
            }
            predictions.append({
                **identity,
                "content_sha256": content_sha256(identity),
            })
        metric_identity = _point_metrics([
            float(row["error_c"]) for row in predictions
        ])
        climatology_metrics = _point_metrics([
            float(row["climatology_error_c"]) for row in predictions
        ])
        oos_pass = (
            metric_identity["mae_c"] <= climatology_metrics["mae_c"] + 1e-12
            and metric_identity["rmse_c"] <= climatology_metrics["rmse_c"] + 1e-12
        )
        identity = {
            "schema": STRICT_OOS_SCHEMA,
            "state": "STRICT_OOS_CANDIDATE_NOT_BINDING",
            "product_id": PRODUCT_ID,
            "provider_id": PROVIDER_ID,
            "model_id": MODEL_ID,
            "independence_family_id": INDEPENDENCE_FAMILY_ID,
            "same_independence_family_as_ifs": True,
            "counts_as_distinct_independence_family": False,
            "station_id": scope[0],
            "target_timezone": scope[1],
            "run_cycle": scope[2],
            "season_bin": scope[3],
            "lead_bucket": scope[4],
            "min_train_days": train_min,
            "min_holdout_days": holdout_min,
            "training_batch_content_sha256": training_batch["content_sha256"],
            "train_start_date": rows[0]["event_date"],
            "holdout_start_date": holdouts[0]["event_date"],
            "holdout_end_date": holdouts[-1]["event_date"],
            "predictions": predictions,
            "metrics": metric_identity,
            "climatology_baseline_metrics": climatology_metrics,
            "oos_state": (
                "STRICT_OOS_PASS" if oos_pass else "STRICT_OOS_REJECTED"
            ),
            "checks": {
                "official_native_aifs_only": True,
                "closed_day_awc_truth_only": True,
                "rolling_origin_strict": True,
                "training_truth_available_by_prediction": True,
                "native_ensemble_tables_written": False,
                "formal_provider_binding_generated": False,
                "formal_p0_eligible": False,
            },
            "boundary": {
                "writes_production_database": False,
                "writes_native_ensemble_tables": False,
                "creates_formal_provider_binding": False,
                "calls_order_gateway": False,
                "places_orders": False,
            },
        }
        candidates.append({**identity, "content_sha256": content_sha256(identity)})
    if insufficiencies:
        raise ContractError("aifs_deterministic_oos_insufficient_samples")
    if not candidates:
        raise ContractError("aifs_deterministic_oos_candidates_missing")
    identity = {
        "schema": STRICT_OOS_SCHEMA,
        "state": "STRICT_OOS_CANDIDATES_NOT_BINDING",
        "product_id": PRODUCT_ID,
        "provider_id": PROVIDER_ID,
        "model_id": MODEL_ID,
        "independence_family_id": INDEPENDENCE_FAMILY_ID,
        "same_independence_family_as_ifs": True,
        "counts_as_distinct_independence_family": False,
        "training_batch_content_sha256": training_batch["content_sha256"],
        "candidate_count": len(candidates),
        "candidates": candidates,
        "formal_provider_binding_generated": False,
        "formal_p0_eligible": False,
        "boundary": {
            "writes_production_database": False,
            "writes_native_ensemble_tables": False,
            "creates_formal_provider_binding": False,
            "calls_order_gateway": False,
            "places_orders": False,
        },
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def validate_aifs_deterministic_training_batch(
    batch: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Expose immutable fact-batch validation to a later authority gate."""

    return _validate_training_batch(batch)


__all__ = [
    "INDEPENDENCE_FAMILY_ID",
    "MIN_HOLDOUT_DAYS",
    "MIN_TRAIN_DAYS",
    "MODEL_ID",
    "PRODUCT_ID",
    "PROVIDER_ID",
    "PREDICTION_SCHEMA",
    "STRICT_OOS_SCHEMA",
    "TRAINING_BATCH_SCHEMA",
    "TRAINING_FACT_SCHEMA",
    "build_aifs_deterministic_strict_oos_candidate",
    "build_aifs_deterministic_training_facts",
    "validate_aifs_deterministic_training_batch",
]
