"""Strict same-day OOS evidence for official DWD ICON Global/EPS.

The daily-member archive and its AWC join are useful calibration evidence, but
they are not, by themselves, a native-arrival index or a P0 authority.  This
module is the small, append-only bridge that makes that distinction explicit:
it validates one or more retained ``icon_eps_training_facts`` batches,
separates facts with an immutable native-arrival receipt from facts that only
have a provider publication/response timestamp, and emits a bounded rolling
20-train/8-holdout report.  A missing sample, truth, or native arrival index is
a normal reported gap for the *official-native* artifact; it cannot be filled
inside this native report by an Open-Meteo row.  When the native object is
unavailable, Open-Meteo follows the separate ``aggregator_republisher`` lane
and may form a no-live P0/P0-M candidate only after its own closed-day AWC
historical strict-OOS contract is activated.

No database, network, model capture, or order-gateway side effect is allowed.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
import math
from typing import Any

from .contracts import ContractError, content_sha256
from .icon_eps_training_facts import (
    TRAINING_BATCH_SCHEMA,
    TRAINING_FACT_SCHEMA,
)
from .station_registry import coordinate_registry_manifest, station_coordinates
from .strict_asof_replay import (
    lead_bucket_for_maturity,
    maturity_key_for_model_run,
    season_bin_for_event_date,
)


SCHEMA = "dwd_icon_global_eps_official_strict_oos_v1"
PREDICTION_SCHEMA = "dwd_icon_global_eps_official_strict_oos_prediction_v1"
PROVIDER_ID = "icon_global_eps_official"
PRODUCT_ID = "DWD_ICON_GLOBAL_EPS"
MODEL_ID = "ICON"
INDEPENDENCE_FAMILY_ID = "DWD_ICON_EPS"
MIN_TRAIN_DAYS = 20
MIN_HOLDOUT_DAYS = 8
MAX_FACTS = 45 * 366
MAX_SCOPES = 45 * 4 * 4
_EPS = 1e-12


def _utc(name: str, value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ContractError(f"icon_eps_oos_{name}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"icon_eps_oos_{name}_timezone_required")
    return parsed.astimezone(timezone.utc)


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _sha(name: str, value: Any) -> str:
    result = str(value or "").strip().lower()
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
        raise ContractError(f"icon_eps_oos_{name}_invalid_sha256")
    return result


def _finite(name: str, value: Any) -> float:
    if isinstance(value, bool):
        raise ContractError(f"icon_eps_oos_{name}_invalid")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"icon_eps_oos_{name}_invalid") from exc
    if not math.isfinite(result):
        raise ContractError(f"icon_eps_oos_{name}_invalid")
    return result


def _identity(raw: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in raw.items()
        if key not in {"content_sha256", "training_fact_id"}
    }


def _season_lead(fact: Mapping[str, Any]) -> tuple[str, str]:
    station = str(fact["station_id"]).upper()
    latitude = float(station_coordinates(station)["latitude"])
    season = season_bin_for_event_date(str(fact["event_date"]), latitude=latitude)
    maturity = maturity_key_for_model_run(
        str(fact["run_initialization_utc"]),
        str(fact["event_date"]),
        target_timezone=str(fact["settlement_timezone"]),
    )
    return season, lead_bucket_for_maturity(maturity)


def _validate_member_set(fact: Mapping[str, Any]) -> list[float]:
    raw = fact.get("member_daily_max_c")
    if not isinstance(raw, list) or len(raw) != 40:
        raise ContractError("icon_eps_oos_member_count_invalid")
    seen: set[str] = set()
    values: list[float] = []
    for row in raw:
        if not isinstance(row, Mapping):
            raise ContractError("icon_eps_oos_member_mapping_invalid")
        member = str(row.get("member_id") or "").strip()
        if member not in {f"member_{index:02d}" for index in range(1, 41)}:
            raise ContractError("icon_eps_oos_member_id_invalid")
        if member in seen:
            raise ContractError("icon_eps_oos_member_duplicate")
        seen.add(member)
        values.append(_finite("member_daily_max_c", row.get("daily_max_c", row.get("raw_tmax_c"))))
        _sha("member_curve", row.get("curve_sha256"))
        component = row.get("component_cohort_sha256")
        if not isinstance(component, list) or not component:
            raise ContractError("icon_eps_oos_member_lineage_missing")
        for digest in component:
            _sha("member_component_cohort", digest)
    if len(seen) != 40:
        raise ContractError("icon_eps_oos_member_set_incomplete")
    return values


def _normalize_fact(raw: Mapping[str, Any], *, as_of: datetime) -> dict[str, Any]:
    if not isinstance(raw, Mapping) or raw.get("schema") != TRAINING_FACT_SCHEMA:
        raise ContractError("icon_eps_oos_training_fact_schema_invalid")
    supplied = _sha("training_fact", raw.get("content_sha256"))
    if supplied != content_sha256(_identity(raw)):
        raise ContractError("icon_eps_oos_training_fact_sha_mismatch")
    if (
        raw.get("provider_id") != PROVIDER_ID
        or raw.get("model_id") != MODEL_ID
        or raw.get("ensemble_system_id") != PRODUCT_ID
        or raw.get("independence_family_id") != INDEPENDENCE_FAMILY_ID
    ):
        raise ContractError("icon_eps_oos_training_fact_identity_invalid")
    station = str(raw.get("station_id") or "").strip().upper()
    expected_stations = set(coordinate_registry_manifest()["station_ids"])
    if station not in expected_stations:
        raise ContractError("icon_eps_oos_station_invalid")
    event_date = str(raw.get("event_date") or "").strip()
    timezone_name = str(raw.get("settlement_timezone") or "").strip()
    run_init = _stamp(_utc("run_initialization", raw.get("run_initialization_utc")))
    if not event_date or not timezone_name:
        raise ContractError("icon_eps_oos_training_fact_scope_invalid")
    # Validate the maturity key now, before a bad run is allowed to enter a
    # scope where it could look like a d1/d2 sample.
    _season_lead({**raw, "station_id": station, "event_date": event_date,
                  "settlement_timezone": timezone_name,
                  "run_initialization_utc": run_init})
    values = _validate_member_set(raw)
    truth_at = _utc(
        "truth_available",
        raw.get("truth_canonical_committed_at_utc"),
    )
    if truth_at > as_of:
        raise ContractError("icon_eps_oos_truth_after_asof")
    settlement_closed = _utc("settlement_closed", raw.get("settlement_closed_at_utc"))
    if settlement_closed > as_of:
        raise ContractError("icon_eps_oos_truth_settlement_open")
    if truth_at < settlement_closed:
        raise ContractError("icon_eps_oos_truth_before_settlement_close")
    # ``forecast_available_at_utc`` is deliberately absent from older batches.
    # Such a fact remains useful inventory but cannot satisfy strict arrival
    # chronology.  Do not infer it from ``as_of_utc`` or the truth timestamp.
    forecast_raw = raw.get("forecast_available_at_utc")
    forecast_available = _utc("forecast_available", forecast_raw) if forecast_raw else None
    if forecast_available is not None and forecast_available > as_of:
        raise ContractError("icon_eps_oos_forecast_after_asof")
    lineage = raw.get("source_lineage")
    if not isinstance(lineage, Mapping):
        raise ContractError("icon_eps_oos_source_lineage_missing")
    if (
        lineage.get("forecast_source_role") != "official_native"
        or lineage.get("forecast_transport_provider_id") != "DWD"
        or lineage.get("truth_source_lane") != "AWC"
        or lineage.get("truth_authority_scope") != "FORMAL_NATIVE_ELIGIBLE"
        or lineage.get("truth_closed_day_required") is not True
    ):
        raise ContractError("icon_eps_oos_source_lineage_invalid")
    native_arrival_eligible = lineage.get("native_arrival_index_eligible") is True
    if native_arrival_eligible and forecast_available is None:
        raise ContractError("icon_eps_oos_native_arrival_timestamp_missing")
    arrival_index_evidence = None
    if native_arrival_eligible:
        # A boolean is never enough to establish native arrival.  The future
        # DWD adapter must bind the fact to an immutable, content-addressed
        # arrival-index receipt before this row can enter strict OOS.
        arrival_index_evidence = _sha(
            "native_arrival_index_evidence",
            lineage.get("native_arrival_index_evidence_sha256"),
        )
    return {
        "content_sha256": supplied,
        "station_id": station,
        "event_date": event_date,
        "settlement_timezone": timezone_name,
        "run_initialization_utc": run_init,
        "run_cycle": str(raw.get("run_cycle") or "").strip(),
        "season_bin": _season_lead({**raw, "station_id": station, "event_date": event_date,
                                    "settlement_timezone": timezone_name,
                                    "run_initialization_utc": run_init})[0],
        "lead_bucket": _season_lead({**raw, "station_id": station, "event_date": event_date,
                                      "settlement_timezone": timezone_name,
                                      "run_initialization_utc": run_init})[1],
        "values": values,
        "official_tmax_c": _finite("official_tmax", raw.get("official_tmax_c")),
        "truth_content_sha256": _sha("truth_content", raw.get("truth_content_sha256")),
        "truth_available_at_utc": _stamp(truth_at),
        "forecast_available_at_utc": _stamp(forecast_available) if forecast_available else None,
        "native_arrival_index_eligible": native_arrival_eligible,
        "native_arrival_index_evidence_sha256": arrival_index_evidence,
        "source_lineage": dict(lineage),
        "member_set_sha256": _sha("member_set", raw.get("member_set_sha256")),
    }


def _probability(values: list[float]) -> dict[str, float]:
    if not values:
        raise ContractError("icon_eps_oos_probability_values_missing")
    counts: dict[str, int] = defaultdict(int)
    for value in values:
        counts[str(math.floor(value + 0.5))] += 1
    total = float(len(values))
    return {key: counts[key] / total for key in sorted(counts, key=int)}


def _metrics(probability: Mapping[str, float], actual: float) -> dict[str, float]:
    actual_bucket = str(math.floor(actual + 0.5))
    support = sorted({*map(int, probability), int(actual_bucket)})
    brier = sum(
        (float(probability.get(str(bucket), 0.0)) - (1.0 if str(bucket) == actual_bucket else 0.0)) ** 2
        for bucket in support
    )
    nll = -math.log(max(float(probability.get(actual_bucket, 0.0)), _EPS))
    crps = 0.0
    for bucket in range(min(support), max(support) + 1):
        forecast = sum(float(value) for key, value in probability.items() if int(key) <= bucket)
        observed = 1.0 if int(actual_bucket) <= bucket else 0.0
        crps += (forecast - observed) ** 2
    return {"brier": brier, "nll": nll, "crps": crps}


def _mean_metrics(rows: Iterable[Mapping[str, float]]) -> dict[str, float]:
    values = list(rows)
    if not values:
        raise ContractError("icon_eps_oos_metric_rows_missing")
    return {key: sum(float(row[key]) for row in values) / len(values) for key in ("brier", "nll", "crps")}


def _fit(values: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    rows = list(values)
    if not rows:
        raise ContractError("icon_eps_oos_train_rows_missing")
    errors: list[float] = []
    spreads: list[float] = []
    climate: list[float] = []
    for row in rows:
        members = [float(item) for item in row["values"]]
        mean = sum(members) / len(members)
        errors.append(float(row["official_tmax_c"]) - mean)
        spreads.append(math.sqrt(sum((item - mean) ** 2 for item in members) / len(members)))
        climate.append(float(row["official_tmax_c"]))
    bias = sum(errors) / len(errors)
    rmse = math.sqrt(sum(item * item for item in errors) / len(errors))
    mean_spread = sum(spreads) / len(spreads)
    return {
        "bias_c": bias,
        "spread_factor": min(3.0, max(0.5, rmse / max(mean_spread, 0.1))),
        "reliability_weight": len(rows) / (len(rows) + 10.0),
        "climate_samples_c": climate[-180:],
        "train_rmse_c": rmse,
        "train_mean_spread_c": mean_spread,
    }


def _apply(values: list[float], params: Mapping[str, Any]) -> dict[str, float]:
    mean = sum(values) / len(values)
    corrected = [
        mean + float(params["bias_c"]) + float(params["spread_factor"]) * (value - mean)
        for value in values
    ]
    member = _probability(corrected)
    climate = _probability([float(value) for value in params["climate_samples_c"]])
    reliability = float(params["reliability_weight"])
    keys = set(member) | set(climate)
    result = {
        key: reliability * member.get(key, 0.0) + (1.0 - reliability) * climate.get(key, 0.0)
        for key in keys
    }
    total = sum(result.values())
    return {key: result[key] / total for key in sorted(result, key=int)}


def _scope_row(scope: tuple[str, str, str, str], *, rows: list[dict[str, Any]],
               strict_rows: list[dict[str, Any]], blockers: list[str]) -> dict[str, Any]:
    station, season, lead, run_cycle = scope
    train_count = max(0, len(strict_rows) - MIN_HOLDOUT_DAYS)
    holdout_count = min(MIN_HOLDOUT_DAYS, len(strict_rows))
    return {
        "station_id": station,
        "season_bin": season,
        "lead_bucket": lead,
        "run_cycle": run_cycle,
        "case_count": len(rows),
        "arrival_lineage_case_count": len(strict_rows),
        "training_case_count": train_count,
        "strict_holdout_case_count": holdout_count,
        "required_train_case_count": MIN_TRAIN_DAYS,
        "required_strict_holdout_case_count": MIN_HOLDOUT_DAYS,
        "missing_train_case_count": max(0, MIN_TRAIN_DAYS - train_count),
        "missing_strict_holdout_case_count": max(0, MIN_HOLDOUT_DAYS - holdout_count),
        "state": "ACCUMULATING_STRICT_OOS",
        "blockers": sorted(set(blockers)),
    }


def build_icon_eps_strict_oos_report(
    batches: Iterable[Mapping[str, Any]],
    *,
    as_of_utc: str,
    min_train_days: int = MIN_TRAIN_DAYS,
    min_holdout_days: int = MIN_HOLDOUT_DAYS,
) -> dict[str, Any]:
    """Build a no-side-effect strict-OOS report from retained training facts."""

    if min_train_days < MIN_TRAIN_DAYS or min_holdout_days < MIN_HOLDOUT_DAYS:
        raise ContractError("icon_eps_oos_window_below_formal_minimum")
    as_of = _utc("as_of", as_of_utc)
    facts: list[dict[str, Any]] = []
    batch_shas: list[str] = []
    unresolved: list[dict[str, Any]] = []
    for batch in batches:
        if not isinstance(batch, Mapping) or batch.get("schema") != TRAINING_BATCH_SCHEMA:
            raise ContractError("icon_eps_oos_training_batch_schema_invalid")
        batch_sha = _sha("training_batch", batch.get("content_sha256"))
        if batch_sha != content_sha256({key: value for key, value in batch.items() if key != "content_sha256"}):
            raise ContractError("icon_eps_oos_training_batch_sha_mismatch")
        batch_shas.append(batch_sha)
        raw_facts = batch.get("facts")
        if not isinstance(raw_facts, list) or len(raw_facts) > MAX_FACTS:
            raise ContractError("icon_eps_oos_training_batch_facts_invalid")
        for row in raw_facts:
            facts.append(_normalize_fact(row, as_of=as_of))
        for row in batch.get("unresolved_station_days") or []:
            if isinstance(row, Mapping):
                unresolved.append(dict(row))
    if len(facts) > MAX_FACTS:
        raise ContractError("icon_eps_oos_fact_limit_exceeded")
    by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    for fact in facts:
        key = (fact["station_id"], fact["event_date"], fact["run_initialization_utc"])
        previous = by_key.get(key)
        if previous is not None:
            if previous["content_sha256"] != fact["content_sha256"]:
                raise ContractError("icon_eps_oos_fact_scope_conflict")
            continue
        by_key[key] = fact
    facts = sorted(by_key.values(), key=lambda row: (row["station_id"], row["event_date"], row["run_cycle"]))
    grouped: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for fact in facts:
        grouped[(fact["station_id"], fact["season_bin"], fact["lead_bucket"], fact["run_cycle"])].append(fact)
    readiness: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    for scope, rows in sorted(grouped.items()):
        rows.sort(key=lambda row: row["event_date"])
        candidate_created = False
        strict_rows = [
            row for row in rows
            if row["native_arrival_index_eligible"] is True
            and row["forecast_available_at_utc"]
            and _utc("forecast_available", row["forecast_available_at_utc"]) < _utc("truth_available", row["truth_available_at_utc"])
        ]
        blockers: list[str] = []
        if len(strict_rows) < len(rows):
            blockers.append("arrival lineage 缺失")
        if not rows:
            blockers.append("数据缺失")
        if len(strict_rows) < min_train_days + min_holdout_days:
            blockers.append("样本不足")
        if not rows or any(row["truth_content_sha256"] == "" for row in rows):
            blockers.append("truth 缺失")
        if len(strict_rows) >= min_train_days + min_holdout_days:
            holdout = strict_rows[-min_holdout_days:]
            holdout_start = holdout[0]["event_date"]
            first_decision = min(_utc("holdout_decision", row["forecast_available_at_utc"]) for row in holdout)
            train = [
                row for row in strict_rows
                if row["event_date"] < holdout_start
                and _utc("train_truth_available", row["truth_available_at_utc"]) <= first_decision
            ]
            if len(train) < min_train_days:
                blockers.append("样本不足")
            else:
                params = _fit(train)
                predictions: list[dict[str, Any]] = []
                for holdout_row in holdout:
                    probability = _apply(holdout_row["values"], params)
                    prediction_identity = {
                        "schema": PREDICTION_SCHEMA,
                        "station_id": scope[0],
                        "event_date": holdout_row["event_date"],
                        "run_cycle": scope[3],
                        "decision_at_utc": holdout_row["forecast_available_at_utc"],
                        "member_set_sha256": holdout_row["member_set_sha256"],
                        "training_fact_content_sha256s": [row["content_sha256"] for row in train],
                        "holdout_fact_content_sha256": holdout_row["content_sha256"],
                        "actual_tmax_c": holdout_row["official_tmax_c"],
                        "probability": probability,
                        "metrics": _metrics(probability, holdout_row["official_tmax_c"]),
                        "strict_oos": True,
                        "formal_p0_eligible": False,
                    }
                    predictions.append({
                        **prediction_identity,
                        "content_sha256": content_sha256(prediction_identity),
                    })
                climate_probability = _probability([row["official_tmax_c"] for row in train])
                metrics = _mean_metrics(row["metrics"] for row in predictions)
                climate_metrics = _mean_metrics(
                    _metrics(climate_probability, row["official_tmax_c"]) for row in holdout
                )
                oos_state = (
                    "STRICT_OOS_PASS"
                    if metrics["brier"] <= climate_metrics["brier"] + _EPS
                    and metrics["crps"] <= climate_metrics["crps"] + _EPS
                    else "STRICT_OOS_REJECTED"
                )
                candidate_identity = {
                    "schema": SCHEMA,
                    "state": "STRICT_OOS_CANDIDATE_NOT_BINDING",
                    "product_id": PRODUCT_ID,
                    "provider_id": PROVIDER_ID,
                    "model_id": MODEL_ID,
                    "independence_family_id": INDEPENDENCE_FAMILY_ID,
                    "station_id": scope[0],
                    "season_bin": scope[1],
                    "lead_bucket": scope[2],
                    "run_cycle": scope[3],
                    "min_train_days": min_train_days,
                    "min_holdout_days": min_holdout_days,
                    "train_start_date": train[0]["event_date"],
                    "train_end_date": train[-1]["event_date"],
                    "holdout_start_date": holdout[0]["event_date"],
                    "holdout_end_date": holdout[-1]["event_date"],
                    "predictions": predictions,
                    "metrics": metrics,
                    "climatology_baseline_metrics": climate_metrics,
                    "oos_state": oos_state,
                    "formal_p0_eligible": False,
                    "promotion_authority_generated": False,
                    "boundary": {
                        "writes_production_database": False,
                        "creates_formal_provider_binding": False,
                        "activates_p0": False,
                        "calls_order_gateway": False,
                        "orders_submitted": 0,
                    },
                }
                candidates.append({**candidate_identity, "content_sha256": content_sha256(candidate_identity)})
                candidate_created = True
        if not candidate_created:
            readiness.append(_scope_row(scope, rows=rows, strict_rows=strict_rows, blockers=blockers))
        else:
            readiness.append({
                **_scope_row(scope, rows=rows, strict_rows=strict_rows, blockers=blockers),
                "state": "STRICT_OOS_READY_NOT_BINDING" if candidate_created else "ACCUMULATING_STRICT_OOS",
                "blockers": [] if candidate_created else sorted(set(blockers)),
            })
    # Preserve a visible row for every canonical station even when no closed
    # fact exists yet.  This keeps the 45-station gap ledger honest and makes
    # a zero-fact run useful instead of silently empty.
    existing_stations = {row["station_id"] for row in readiness}
    unresolved_by_station: dict[str, list[str]] = defaultdict(list)
    for row in unresolved:
        station = str(row.get("station_id") or "").strip().upper()
        if station:
            unresolved_by_station[station].append(str(row.get("reason") or "truth_missing"))
    for station in sorted(set(coordinate_registry_manifest()["station_ids"]) - existing_stations):
        reasons = unresolved_by_station.get(station, [])
        blockers = ["样本不足", "arrival lineage 缺失"]
        if not reasons or any("truth" in reason for reason in reasons):
            blockers.append("truth 缺失")
        if any("open" in reason for reason in reasons):
            blockers.append("数据缺失")
        readiness.append({
            "station_id": station,
            "season_bin": "*",
            "lead_bucket": "*",
            "run_cycle": "*",
            "case_count": 0,
            "arrival_lineage_case_count": 0,
            "training_case_count": 0,
            "strict_holdout_case_count": 0,
            "required_train_case_count": min_train_days,
            "required_strict_holdout_case_count": min_holdout_days,
            "missing_train_case_count": min_train_days,
            "missing_strict_holdout_case_count": min_holdout_days,
            "state": "PENDING_CLOSED_AWC_TRUTH",
            "blockers": sorted(set(blockers)),
            "unresolved_reasons": sorted(set(reasons)),
        })
    readiness.sort(key=lambda row: (row["station_id"], row["season_bin"], row["lead_bucket"], row["run_cycle"]))
    identity = {
        "schema": SCHEMA,
        "state": "STRICT_OOS_CANDIDATES_NOT_BINDING",
        "as_of_utc": _stamp(as_of),
        "product_id": PRODUCT_ID,
        "provider_id": PROVIDER_ID,
        "model_id": MODEL_ID,
        "independence_family_id": INDEPENDENCE_FAMILY_ID,
        "training_batch_content_sha256s": sorted(set(batch_shas)),
        "fact_count": len(facts),
        "readiness": readiness,
        "candidates": candidates,
        "candidate_count": len(candidates),
        "strict_oos_state": "READY_NOT_BINDING" if candidates else "PENDING_ARRIVAL_TRUTH_AND_SAMPLES",
        "formal_p0_eligible": False,
        "promotion_authority_generated": False,
        "source_authority": {
            "forecast_source_role": "official_native",
            "forecast_transport_provider_id": "DWD",
            "truth_source_lane": "AWC",
            "open_meteo_fallback_used": False,
            "native_arrival_index_required": True,
            "native_arrival_index_used": bool(candidates),
        },
        "boundary": {
            "writes_production_database": False,
            "creates_formal_provider_binding": False,
            "activates_p0": False,
            "calls_order_gateway": False,
            "orders_submitted": 0,
        },
    }
    return {**identity, "content_sha256": content_sha256(identity)}


__all__ = [
    "INDEPENDENCE_FAMILY_ID",
    "MIN_HOLDOUT_DAYS",
    "MIN_TRAIN_DAYS",
    "MODEL_ID",
    "PRODUCT_ID",
    "PROVIDER_ID",
    "PREDICTION_SCHEMA",
    "SCHEMA",
    "build_icon_eps_strict_oos_report",
]
