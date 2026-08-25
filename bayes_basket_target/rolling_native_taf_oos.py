"""Bounded rolling-origin native P0 -> historical TAF exact-join evidence.

The ordinary native OOS builder deliberately persists only its latest
20-train/8-holdout candidate.  That is sufficient to qualify a P0 candidate,
but it can never supply the 24 independent strict-OOS samples required by a
TAF revision artifact.  This module builds *non-overlapping* eight-day blocks
from the same immutable official native cases.  It is offline evidence only:
it never writes SQLite, materializes P0/P0-M/P1, or reaches an order gateway.

The caller must still keep the regular P0 path's 20/8 artifact gate.  The
rolling series is a TAF-calibration input, not an alternative P0 authority.
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
from itertools import combinations
from typing import Any
from zoneinfo import ZoneInfo

from .contracts import ContractError, content_sha256
from .native_ensemble_contract import (
    P0_PROBABILITY_PATHWAY,
    native_ensemble_family_set_hash,
    validate_cross_native_ensemble_weights,
)
from .native_ensemble_oos import (
    DEFAULT_FAMILY_WEIGHT_CAPS,
    DEFAULT_MIN_HOLDOUT_DAYS,
    DEFAULT_MIN_TRAIN_DAYS,
    FORMAL_PROMOTION_THRESHOLD_PROFILE,
    _seat_weights,
    _correlation,
    _cycle_mapping_for_rows,
    _fit_parameters,
    _load_cases,
    _load_systems,
    _mean_metrics,
    _metrics,
    _probabilities,
)
from .native_ensemble_training_facts import (
    _rebin_probabilities,
)
from .ssot import native_ensemble_foundation_schema_status
from .station_season import season_bins_for_window


SCHEMA = "native_ensemble_rolling_origin_taf_oos_v1"
EXACT_JOIN_SCHEMA = "taf_native_ensemble_strict_oos_exact_join_v1"
SOURCE_CLASS = "strict_oos_native_p0_taf_truth_exact_join"
# A series has to carry both windows the artifact needs -- one to fit on and a
# strictly later one to be gated on -- so more blocks mean a better-supported
# fit.  But each block also costs eight more settled days, and settled truth is
# what this is short of: the archive holds 51 days for the leading pair at ZBAA
# while truth covers 34.  Asking for the larger series unconditionally would
# therefore push the first artifact further away, not closer.
#
# So the block count is negotiated per scope: take the most blocks the common
# days support, down to MIN_BLOCKS.  A station with 52 days gets the
# better-supported four-block series; one with 44 still qualifies on three.
MAX_BLOCKS = 4
# Two, not three.  The negotiation above already prefers more blocks whenever
# the days support them, so this is only the floor for a scope that would
# otherwise produce nothing at all: at three the requirement is 20+8*3=44 days
# and stations sat at 36 for weeks with no artifact, which is not a stricter
# evaluation but an absent one.  Two disjoint forward windows still refuse a
# single lucky window; a scope with the days for three or four is unaffected
# and still gets them.
MIN_BLOCKS = 2
# Doubled from 24 for charter 62: near a station-local season boundary each
# scope is legitimately built under two serving labels, and the old bound
# sized for one label would refuse the boundary month outright.
MAX_STATION_CANDIDATES = 48
MAX_JOIN_ROWS = 366


def _utc(name: str, value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ContractError(f"rolling_native_taf_{name}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"rolling_native_taf_{name}_timezone_required")
    return parsed.astimezone(timezone.utc)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _verify_hash(raw: Mapping[str, Any], *, name: str) -> None:
    supplied = str(raw.get("content_sha256") or "").strip().lower()
    if len(supplied) != 64 or any(ch not in "0123456789abcdef" for ch in supplied):
        raise ContractError(f"rolling_native_taf_{name}_content_sha_invalid")
    identity = {str(key): value for key, value in raw.items() if key != "content_sha256"}
    if content_sha256(identity) != supplied:
        raise ContractError(f"rolling_native_taf_{name}_content_sha_mismatch")


def plan_non_overlapping_rolling_blocks(
    event_dates: Sequence[str],
    *,
    min_train_days: int = DEFAULT_MIN_TRAIN_DAYS,
    min_holdout_days: int = DEFAULT_MIN_HOLDOUT_DAYS,
    required_blocks: int = MAX_BLOCKS,
) -> dict[str, Any]:
    """Plan the first bounded set of disjoint strict-OOS blocks.

    A block may use every earlier case for training, but its holdout dates are
    disjoint from all other blocks.  This means 44 common settled native days
    are required for 3 x (20 train, 8 holdout) evidence; no retrospective
    row is relabelled as a forward holdout to make that count appear early.
    """

    if (
        min_train_days < DEFAULT_MIN_TRAIN_DAYS
        or min_holdout_days != DEFAULT_MIN_HOLDOUT_DAYS
        or required_blocks < 1
        or required_blocks > MAX_BLOCKS
    ):
        raise ContractError("rolling_native_taf_window_contract_invalid")
    normalized = sorted({str(value).strip() for value in event_dates if str(value).strip()})
    if len(normalized) != len([str(value).strip() for value in event_dates if str(value).strip()]):
        raise ContractError("rolling_native_taf_event_dates_not_unique")
    for value in normalized:
        try:
            date.fromisoformat(value)
        except ValueError as exc:
            raise ContractError("rolling_native_taf_event_date_invalid") from exc
    blocks: list[dict[str, Any]] = []
    cursor = min_train_days
    while (
        len(blocks) < required_blocks
        and cursor + min_holdout_days <= len(normalized)
    ):
        holdout_dates = normalized[cursor:cursor + min_holdout_days]
        blocks.append({
            "block_index": len(blocks),
            "train_start_date": normalized[0],
            "train_end_date": normalized[cursor - 1],
            "train_case_count": cursor,
            "holdout_start_date": holdout_dates[0],
            "holdout_end_date": holdout_dates[-1],
            "holdout_dates": holdout_dates,
            "holdout_case_count": len(holdout_dates),
        })
        cursor += min_holdout_days
    required_common_days = min_train_days + min_holdout_days * required_blocks
    return {
        "state": "READY" if len(blocks) == required_blocks else "ACCUMULATING",
        "common_case_count": len(normalized),
        "required_common_case_count": required_common_days,
        "missing_common_case_count": max(0, required_common_days - len(normalized)),
        "min_train_days": min_train_days,
        "min_holdout_days": min_holdout_days,
        "required_block_count": required_blocks,
        "block_count": len(blocks),
        "blocks": blocks,
    }


def _live_run_cycle_by_system(
    conn: sqlite3.Connection,
    *,
    station_id: str,
) -> dict[str, str]:
    """The cycle each system is actually serving P0 from, if a root says so.

    Read rather than assumed: the artifact this evidence will support is
    selected at serving time against the root's own per-system cycles, so a
    tie resolved any other way trains on runs the root does not use.
    """

    # An absent or unreadable root yields no preference, never an error: this
    # only ever breaks ties, and a caller with no committed P0 -- a replay
    # fixture, a station before its first root -- must keep the conservative
    # drop rather than fail the whole station's evidence build.
    try:
        row = conn.execute(
            "SELECT lineage_json FROM basket_probability_envelope "
            "WHERE station_id=? AND stage='P0' AND state='COMMITTED' "
            "ORDER BY decision_at_utc DESC LIMIT 1",
            (station_id,),
        ).fetchone()
    except Exception:
        return {}
    if row is None:
        return {}
    try:
        lineage = json.loads(str(row[0] or "{}"))
        mapping = json.loads(
            str(lineage.get("run_cycle_by_system_json") or "{}")
        )
    except (TypeError, ValueError):
        return {}
    if not isinstance(mapping, Mapping):
        return {}
    return {
        str(system_id): str(cycle)
        for system_id, cycle in mapping.items()
        if str(cycle or "").strip()
    }


def _scope_system_cases(
    cases_by_scope: Mapping[tuple[str, str, str, str], Sequence[Mapping[str, Any]]],
    *,
    live_run_cycle_by_system: Mapping[str, str] | None = None,
) -> dict[tuple[str, str], dict[str, tuple[str, list[dict[str, Any]]]]]:
    """Return one native run-cycle per system and scope, fail closed on ties.

    A tie is real: an hour bucket holds both the 00Z and 18Z runs of the same
    system, and training across them would pool predictions of different age
    into one sample while P0 serves from exactly one of them.  Dropping the
    system outright was the conservative answer, but it is stricter than the
    question deserves -- it discarded the pair ZUUU actually serves from,
    leaving that scope permanently unevaluable while a scope nothing serves
    accumulated instead.  The root names the cycle per system, so prefer it
    and keep the drop for the case where nothing distinguishes them.
    """

    live_cycles = dict(live_run_cycle_by_system or {})
    grouped: dict[tuple[str, str], dict[str, list[tuple[str, list[dict[str, Any]]]]]] = defaultdict(lambda: defaultdict(list))
    for (system_id, season_bin, lead_bucket, run_cycle), cases in cases_by_scope.items():
        grouped[(season_bin, lead_bucket)][system_id].append((
            run_cycle,
            [dict(case) for case in cases],
        ))
    resolved: dict[tuple[str, str], dict[str, tuple[str, list[dict[str, Any]]]]] = {}
    for scope, systems in grouped.items():
        selected: dict[str, tuple[str, list[dict[str, Any]]]] = {}
        for system_id, rows in systems.items():
            if len(rows) == 1:
                selected[system_id] = rows[0]
                continue
            served = str(live_cycles.get(system_id) or "")
            matches = [row for row in rows if str(row[0]) == served]
            if len(matches) == 1:
                selected[system_id] = matches[0]
        resolved[scope] = selected
    return resolved


def _case_by_day(cases: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for raw in cases:
        day = str(raw.get("event_date") or "")
        if not day or day in result:
            raise ContractError("rolling_native_taf_case_day_not_unique")
        result[day] = dict(raw)
    return result


def _evaluate_system_block(
    *,
    system_id: str,
    cases: Sequence[Mapping[str, Any]],
    block: Mapping[str, Any],
    min_train_days: int,
) -> dict[str, Any]:
    by_day = _case_by_day(cases)
    holdout_days = [str(value) for value in block["holdout_dates"]]
    try:
        holdout = [by_day[day] for day in holdout_days]
    except KeyError as exc:
        raise ContractError("rolling_native_taf_system_holdout_missing") from exc
    if any(case.get("holdout_eligible") is not True for case in holdout):
        raise ContractError("rolling_native_taf_non_strict_holdout_forbidden")
    first_decision = min(_utc("holdout_decision", case["aggregator_snapshot_at_utc"]) for case in holdout)
    train = [
        dict(case)
        for case in cases
        if str(case["event_date"]) < holdout_days[0]
        and _utc("train_decision", case["aggregator_snapshot_at_utc"]) < first_decision
    ]
    if len(train) < min_train_days:
        raise ContractError("rolling_native_taf_system_train_insufficient")
    parameters = _fit_parameters(train)
    predictions = []
    for case in holdout:
        probability = _apply_parameters(case["values"], parameters)
        identity = {
            "station_id": str(case.get("station_id") or ""),
            "system_id": system_id,
            "event_date": str(case["event_date"]),
            "decision_at_utc": str(case["aggregator_snapshot_at_utc"]),
            "trained_through_event_date": str(train[-1]["event_date"]),
            "member_set_sha256": str(case["member_set_sha256"]),
            "probability": probability,
            "actual_tmax_c": float(case["actual_tmax_c"]),
        }
        predictions.append({
            **identity,
            "prediction_content_sha256": content_sha256(identity),
            "metrics": _metrics(probability, float(case["actual_tmax_c"])),
        })
    return {
        "system_id": system_id,
        "transport_provider_id": str(cases[0]["transport_provider_id"]),
        "train_start_date": str(train[0]["event_date"]),
        "train_end_date": str(train[-1]["event_date"]),
        "train_n": len(train),
        "holdout_n": len(holdout),
        "parameters": parameters,
        "predictions": predictions,
        "oos_metrics": _mean_metrics([row["metrics"] for row in predictions]),
        "train_cases": train,
    }


def _apply_parameters(values: Sequence[float], parameters: Mapping[str, Any]) -> dict[str, float]:
    """Keep the native OOS calibration transform local and deterministic."""

    current = [float(value) for value in values]
    if not current:
        raise ContractError("rolling_native_taf_member_values_missing")
    mean = sum(current) / len(current)
    corrected = [
        mean + float(parameters["bias_c"])
        + float(parameters["spread_factor"]) * (value - mean)
        for value in current
    ]
    member_probability = _probabilities(corrected)
    climate_probability = _probabilities([float(value) for value in parameters["climate_samples_c"]])
    reliability = float(parameters["reliability_weight"])
    support = set(member_probability) | set(climate_probability)
    result = {
        bucket: reliability * member_probability.get(bucket, 0.0)
        + (1.0 - reliability) * climate_probability.get(bucket, 0.0)
        for bucket in support
    }
    total = sum(result.values())
    if total <= 0.0:
        raise ContractError("rolling_native_taf_probability_empty")
    return {bucket: value / total for bucket, value in sorted(result.items(), key=lambda item: int(item[0]))}


def _build_cross_candidate(
    *,
    station_id: str,
    season_bin: str,
    lead_bucket: str,
    selected: Mapping[str, tuple[str, Sequence[Mapping[str, Any]]]],
    systems: Mapping[str, Mapping[str, Any]],
    block: Mapping[str, Any],
    min_train_days: int,
    min_holdout_days: int,
    family_weight_caps: Mapping[str, float],
) -> dict[str, Any]:
    evaluations = {
        system_id: _evaluate_system_block(
            system_id=system_id,
            cases=cases,
            block=block,
            min_train_days=min_train_days,
        )
        for system_id, (_cycle, cases) in selected.items()
    }
    rows = [
        (system_id, str(selected[system_id][0]), evaluations[system_id])
        for system_id in sorted(selected)
    ]
    cycle_by_system, cycle_sha, run_cycle, run_cycle_scope = _cycle_mapping_for_rows(rows)
    family_by_system = {
        system_id: str(systems[system_id]["independence_family_id"])
        for system_id in selected
    }
    if len(set(family_by_system.values())) < 2:
        raise ContractError("rolling_native_taf_two_families_required")
    # The same seat rule P0 serves: minimise the combined residual variance
    # where the train window supports a covariance, and fall back on the
    # inverse error scale where it does not.  This lane has the most to gain
    # from it -- the forecaster's TX and the METAR physical path correlate
    # with each other at 0.08 against the 0.63 the numerical families share,
    # so it is the one blend where redundancy genuinely differs between
    # members and inverse weighting was pricing them as if it did not.
    train_error_scale = {
        system_id: float(
            evaluations[system_id]["parameters"].get(
                "train_residual_sigma_c"
            )
            or evaluations[system_id]["parameters"]["train_rmse_c"]
        )
        for system_id in selected
    }
    train_residuals = {
        system_id: {
            str(case["event_date"]): (
                float(case["actual_tmax_c"])
                - sum(float(value) for value in case["values"])
                / len(case["values"])
            )
            for case in evaluations[system_id]["train_cases"]
        }
        for system_id in selected
    }
    weights, weight_rule = _seat_weights(
        residuals_by_system=train_residuals,
        train_error_scale_by_system=train_error_scale,
        family_by_system=family_by_system,
        family_caps=family_weight_caps,
    )
    receipt = validate_cross_native_ensemble_weights(
        system_weights=weights,
        systems_by_id=systems,
        family_weight_caps={family: float(family_weight_caps.get(family, 0.75)) for family in sorted(set(family_by_system.values()))},
    )
    predictions_by_system = {
        system_id: {str(row["event_date"]): row for row in evaluation["predictions"]}
        for system_id, evaluation in evaluations.items()
    }
    holdout_days = [str(value) for value in block["holdout_dates"]]
    cross_predictions = []
    baseline_metrics = []
    climate_probability = _probabilities([float(value) for value in next(iter(evaluations.values()))["parameters"]["climate_samples_c"]])
    for day in holdout_days:
        support = set().union(*(set(predictions_by_system[system_id][day]["probability"]) for system_id in weights))
        probability = {
            bucket: sum(weights[system_id] * float(predictions_by_system[system_id][day]["probability"].get(bucket, 0.0)) for system_id in weights)
            for bucket in support
        }
        total = sum(probability.values())
        if total <= 0.0 or abs(total - 1.0) > 1e-9:
            raise ContractError("rolling_native_taf_cross_probability_invalid")
        actual = float(predictions_by_system[next(iter(sorted(weights)))][day]["actual_tmax_c"])
        decision = max(str(predictions_by_system[system_id][day]["decision_at_utc"]) for system_id in weights)
        prediction_identity = {
            "station_id": station_id,
            "season_bin": season_bin,
            "lead_bucket": lead_bucket,
            "run_cycle_by_system": cycle_by_system,
            "block_index": int(block["block_index"]),
            "event_date": day,
            "decision_at_utc": decision,
            "trained_through_event_date": max(str(evaluations[system_id]["train_end_date"]) for system_id in weights),
            "probability": probability,
            "system_member_set_shas": {system_id: str(predictions_by_system[system_id][day]["member_set_sha256"]) for system_id in sorted(weights)},
        }
        cross_predictions.append({
            **prediction_identity,
            "actual_tmax_c": actual,
            "metrics": _metrics(probability, actual),
            "prediction_content_sha256": content_sha256(prediction_identity),
        })
        baseline_metrics.append(_metrics(climate_probability, actual))
    correlations: dict[str, dict[str, float]] = {}
    for left in sorted(weights):
        correlations[left] = {}
        left_train = {str(case["event_date"]): float(case["actual_tmax_c"]) - sum(float(value) for value in case["values"]) / len(case["values"]) for case in evaluations[left]["train_cases"]}
        for right in sorted(weights):
            right_train = {str(case["event_date"]): float(case["actual_tmax_c"]) - sum(float(value) for value in case["values"]) / len(case["values"]) for case in evaluations[right]["train_cases"]}
            common = sorted(set(left_train) & set(right_train))
            correlations[left][right] = _correlation([left_train[day] for day in common], [right_train[day] for day in common])
    cross_metrics = _mean_metrics([row["metrics"] for row in cross_predictions])
    baseline = _mean_metrics(baseline_metrics)
    selected_ids = sorted(weights)
    timezone_names = {
        str(case["timezone_name"])
        for system_id in selected_ids
        for case in selected[system_id][1]
    }
    definitions = {
        str(case["settlement_definition"])
        for system_id in selected_ids
        for case in selected[system_id][1]
    }
    if len(timezone_names) != 1 or len(definitions) != 1:
        raise ContractError("rolling_native_taf_scope_authority_ambiguous")
    identity = {
        "schema": "native_ensemble_rolling_origin_cross_candidate_v1",
        "state": "PRODUCTION_NO_LIVE_OOS_PASS" if (cross_metrics["brier"] <= baseline["brier"] and cross_metrics["crps"] <= baseline["crps"]) else "OOS_REJECTED",
        "station_id": station_id,
        "season_bin": season_bin,
        "lead_bucket": lead_bucket,
        "run_cycle": run_cycle,
        "run_cycle_scope": run_cycle_scope,
        "run_cycle_by_system": cycle_by_system,
        "run_cycle_by_system_sha256": cycle_sha,
        "settlement_definition": next(iter(definitions)),
        "settlement_timezone": next(iter(timezone_names)),
        "selected_system_ids": selected_ids,
        "independence_family_ids": sorted(set(family_by_system.values())),
        "system_family_ids": dict(sorted(family_by_system.items())),
        "system_weights": weights,
        # Which rule divided the seat.  A scope that fell back looks identical
        # in the weights alone, and the reason -- too few shared days for a
        # covariance -- is what a reader chasing an odd blend needs.
        "weight_rule": weight_rule,
        "family_weights": receipt["family_weights"],
        "family_weight_caps": {family: float(family_weight_caps.get(family, 0.75)) for family in sorted(set(family_by_system.values()))},
        "system_transport_provider_ids": {system_id: str(evaluations[system_id]["transport_provider_id"]) for system_id in selected_ids},
        "available_system_set_hash": content_sha256(selected_ids),
        "available_family_set_hash": native_ensemble_family_set_hash(list(family_by_system.values())),
        "rolling_origin_block": dict(block),
        "min_train_days": min_train_days,
        "min_holdout_days": min_holdout_days,
        "threshold_profile": FORMAL_PROMOTION_THRESHOLD_PROFILE,
        "oos_metrics": cross_metrics,
        "climatology_baseline_metrics": baseline,
        "cross_system_residual_correlation": correlations,
        "predictions": cross_predictions,
        "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
        "formal_p0_eligible": False,
        "production_p0_materialized": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def build_rolling_origin_native_oos(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    as_of_utc: str,
    required_blocks: int = MAX_BLOCKS,
    require_oos_pass: bool = False,
) -> dict[str, Any]:
    """Build up to three disjoint official-native OOS blocks without writes.

    ``require_oos_pass`` withholds a series whose blocks lost the strict-OOS
    comparison.  It defaults off because the only consumer is P0-M, which
    publishes on sample under the 2026-08-15 ruling; the losing verdict is
    still recorded on each candidate.
    """

    if native_ensemble_foundation_schema_status(conn)["ok"] is not True:
        raise ContractError("rolling_native_taf_foundation_schema_required")
    _utc("as_of", as_of_utc)
    station = str(station_id or "").strip().upper()
    if not station:
        raise ContractError("rolling_native_taf_station_required")
    systems = _load_systems(conn)
    cases_by_scope = _load_cases(conn, station_id=station, as_of_utc=as_of_utc)
    scoped = _scope_system_cases(
        cases_by_scope,
        live_run_cycle_by_system=_live_run_cycle_by_system(
            conn, station_id=station
        ),
    )
    # Charter 62 boundary continuity, same move as the native lane: the P0
    # root at a season boundary carries the next station-local label while
    # this builder's scopes carry the fit day's, and the worker's exact
    # scope join would starve P0-M for the days between.  Alias each scope
    # under every label the serving horizon needs BEFORE candidates are
    # built, so each label's series is content-addressed under its own name
    # rather than mutated after hashing.
    as_of_day = date.fromisoformat(str(as_of_utc)[:10])
    serve_labels = season_bins_for_window(
        station,
        (as_of_day - timedelta(days=1)).isoformat(),
        (as_of_day + timedelta(days=31)).isoformat(),
    )
    for scope_key in list(scoped):
        scope_season, scope_lead = scope_key
        # Only scopes already speaking the serving vocabulary are aliased;
        # a legacy-labelled scope from rows written before the switch keeps
        # its one label and ages out rather than being multiplied.
        if scope_season not in serve_labels:
            continue
        for label in serve_labels:
            aliased = (label, scope_lead)
            if aliased not in scoped:
                scoped[aliased] = scoped[scope_key]
    candidates: list[dict[str, Any]] = []
    accumulating: list[dict[str, Any]] = []
    waived: list[dict[str, Any]] = []
    caps = dict(DEFAULT_FAMILY_WEIGHT_CAPS)
    for (season_bin, lead_bucket), by_system in sorted(scoped.items()):
        for left, right in combinations(sorted(by_system), 2):
            if left not in systems or right not in systems:
                continue
            if systems[left]["independence_family_id"] == systems[right]["independence_family_id"]:
                continue
            selected = {left: by_system[left], right: by_system[right]}
            common_days = sorted(
                set(_case_by_day(selected[left][1]))
                & set(_case_by_day(selected[right][1]))
            )
            # Most blocks first, then fewer: a scope with the days for four
            # gets the better-supported series, and one that only reaches
            # three still qualifies instead of waiting eight more days for a
            # fourth it does not need.  The last plan computed is the smallest
            # ask, so an accumulating scope reports the nearest shortfall
            # rather than the largest.
            for blocks in range(int(required_blocks), MIN_BLOCKS - 1, -1):
                plan = plan_non_overlapping_rolling_blocks(
                    common_days,
                    required_blocks=blocks,
                )
                if plan["state"] == "READY":
                    break
            identity = {
                "station_id": station,
                "season_bin": season_bin,
                "lead_bucket": lead_bucket,
                "selected_system_ids": [left, right],
                "plan": plan,
            }
            if plan["state"] != "READY":
                accumulating.append(identity)
                continue
            block_candidates = [
                _build_cross_candidate(
                    station_id=station,
                    season_bin=season_bin,
                    lead_bucket=lead_bucket,
                    selected=selected,
                    systems=systems,
                    block=block,
                    min_train_days=DEFAULT_MIN_TRAIN_DAYS,
                    min_holdout_days=DEFAULT_MIN_HOLDOUT_DAYS,
                    family_weight_caps=caps,
                )
                for block in plan["blocks"]
            ]
            refused = [
                candidate for candidate in block_candidates
                if candidate["state"] != "PRODUCTION_NO_LIVE_OOS_PASS"
            ]
            if refused and require_oos_pass:
                accumulating.append({**identity, "state": "OOS_REJECTED"})
                continue
            if refused:
                # Ruling 2026-08-15: this series feeds P0-M only, and P0-M
                # publishes on sample rather than on beating a baseline out
                # of sample.  The refusal is recorded here rather than on the
                # candidates -- those are content-addressed and adding a key
                # to one breaks the hash every consumer verifies -- so a
                # losing pair stays legible without withholding the series.
                # P0's own lane does not run through here and keeps its
                # strict admission.
                waived.append({
                    "season_bin": season_bin,
                    "lead_bucket": lead_bucket,
                    "selected_system_ids": [left, right],
                    "refused_block_indexes": [
                        int(item["rolling_origin_block"]["block_index"])
                        for item in refused
                    ],
                    "refused_states": sorted({
                        str(item["state"]) for item in refused
                    }),
                })
            candidates.extend(block_candidates)
    if len(candidates) > MAX_STATION_CANDIDATES:
        raise ContractError("rolling_native_taf_candidate_bound_exceeded")
    identity = {
        "schema": SCHEMA,
        "station_id": station,
        "as_of_utc": as_of_utc,
        "required_block_count": required_blocks,
        # The smallest series that can qualify, since that is what a scope has
        # to reach to stop accumulating.
        "minimum_taf_oos_sample_n": DEFAULT_MIN_HOLDOUT_DAYS * MIN_BLOCKS,
        "candidates": candidates,
        "accumulating": accumulating,
        "oos_admission_waived": waived,
        "state": "READY" if candidates else "ACCUMULATING",
        "boundary": {
            "offline_only": True,
            "writes_target_database": False,
            "production_p0_materialized": False,
            "production_p0m_materialized": False,
            "enters_realtime_truth": False,
            "enters_p1": False,
            "enters_hard_no": False,
            "calls_order_gateway": False,
            "orders_submitted": 0,
        },
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def _bucket_intervals_for_candidate(
    conn: sqlite3.Connection,
    candidate: Mapping[str, Any],
) -> dict[str, dict[str, tuple[float | None, float | None]]]:
    """Each prediction day's frozen historical bucket contract, by day.

    The market ladder recentres around each event day's expected
    temperature, so a multi-day series legitimately spans several bucket
    contracts -- RPLL's moved eleven times in August 2026 alone.  The old
    single-contract reading only ever held in stable-weather stretches
    (RPLL's ladder sat still for all of July), and the moment ladders
    started moving daily every series in the fleet failed its
    "contract changed" guard -- which is what actually starved the
    calibrated TAF lane from late July on.  Each day now resolves its own
    ladder and is rebinned onto it; a day whose contract cannot be
    resolved makes the whole series incomplete evidence, because scoring
    that day on a neighbouring day's ladder is the wrong grid, not a
    fallback.  Never a live fallback, same as before.
    """

    system_id = str(candidate["selected_system_ids"][0])
    station_id = str(candidate["station_id"])
    intervals_by_day: dict[
        str, dict[str, tuple[float | None, float | None]]
    ] = {}
    for prediction in candidate.get("predictions") or []:
        # The candidate's lead is the training group, measured in hours ahead;
        # the archive keys its buckets by the collector's calendar label.
        # Matching one against the other found nothing for every hour-bucket
        # scope, which is what left a READY series with no exact join.  The
        # calendar label for this prediction is whatever the member run that
        # produced it was filed under, so resolve it from that run.
        run_cycle = str(
            (prediction.get("run_cycle_by_system") or {}).get(system_id) or ""
        )
        # One cycle legitimately appears under two calendar labels on the same
        # day -- ECMWF 00Z is ``d0`` from that morning's run and ``d1`` from
        # the previous morning's -- so the label alone is ambiguous while the
        # run initialisation is not.  The prediction states which run it was
        # made from, so match on that and let the label follow.
        try:
            calendar_leads = [
                str(row[0])
                for row in conn.execute(
                    "SELECT DISTINCT archive_lead_bucket FROM "
                    "calibration_native_ensemble_historical_member_run "
                    "WHERE ensemble_system_id=? AND station_id=? "
                    "AND target_local_date=? AND run_cycle=? "
                    "AND native_run_init_utc=? "
                    "ORDER BY archive_lead_bucket",
                    (
                        system_id,
                        station_id,
                        str(prediction["event_date"]),
                        run_cycle,
                        str(prediction.get("decision_at_utc") or ""),
                    ),
                ).fetchall()
            ]
        except sqlite3.Error:
            # The run facts are how the calendar label is resolved, not what
            # the bucket rows are read from.  A caller holding only the bucket
            # table keeps the original behaviour: ask for the candidate's own
            # lead and let the bucket lookup below decide.
            calendar_leads = [str(candidate["lead_bucket"])]
        if len(calendar_leads) != 1:
            # Zero means no run matches this prediction's own initialisation;
            # more than one means one run was filed under two labels, which
            # nothing here can resolve.  Both stay fail-closed.
            continue
        found = conn.execute(
            "SELECT bucket_intervals_json FROM "
            "calibration_native_ensemble_historical_member_bucket "
            "WHERE ensemble_system_id=? AND station_id=? "
            # Historical bucket facts are keyed by their immutable member
            # set and archive receipt.  ``run_cycle`` exists on the related
            # member-run facts, but intentionally is not duplicated in this
            # append-only bucket table.  Multiple captures of one day are
            # the norm, not an ambiguity: measured 2026-08-25, 3,296 groups
            # carry more than one row and 2,706 of them are byte-identical
            # re-captures.  Where the contracts genuinely differ (590
            # groups) the market re-listed the ladder, and settlement
            # happened on the final geometry -- so the newest capture is
            # the contract this day actually settled under.
            "AND target_local_date=? AND archive_lead_bucket=? "
            "ORDER BY archive_query_at_utc DESC LIMIT 1",
            (
                system_id,
                station_id,
                str(prediction["event_date"]),
                calendar_leads[0],
            ),
        ).fetchall()
        if not found:
            continue
        try:
            raw_intervals = json.loads(str(found[0][0]))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ContractError("rolling_native_taf_historical_bucket_invalid") from exc
        if not isinstance(raw_intervals, Mapping):
            raise ContractError("rolling_native_taf_historical_bucket_invalid")
        result: dict[str, tuple[float | None, float | None]] = {}
        for bucket, bounds in raw_intervals.items():
            if not isinstance(bounds, list) or len(bounds) != 2:
                raise ContractError("rolling_native_taf_historical_bucket_invalid")
            lower = None if bounds[0] is None else float(bounds[0])
            upper = None if bounds[1] is None else float(bounds[1])
            if lower is not None and upper is not None and lower >= upper:
                raise ContractError("rolling_native_taf_historical_bucket_invalid")
            result[str(bucket)] = (lower, upper)
        if not result:
            raise ContractError("rolling_native_taf_historical_bucket_empty")
        intervals_by_day[str(prediction["event_date"])] = result
    prediction_days = {
        str(prediction["event_date"])
        for prediction in candidate.get("predictions") or []
    }
    # Every day must speak its own ladder.  A day whose contract is absent
    # is missing evidence for the whole series -- the blocks are counted as
    # independent samples, and quietly scoring one day on another day's
    # grid is exactly the mismatch the per-day resolution exists to end.
    if not intervals_by_day or set(intervals_by_day) != prediction_days:
        raise ContractError("rolling_native_taf_historical_bucket_missing")
    return intervals_by_day


def _taf_truth_for_prediction(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    event_date: str,
    settlement_timezone: str,
    settlement_definition: str,
    decision_at_utc: str,
) -> tuple[tuple[Any, ...], tuple[Any, ...], str]:
    decision = _utc("prediction_decision", decision_at_utc)
    day_end = (datetime.fromisoformat(event_date).replace(tzinfo=ZoneInfo(settlement_timezone)) + timedelta(days=1)).astimezone(timezone.utc)
    truth = conn.execute(
        "SELECT official_tmax_c,canonical_committed_at_utc,truth_id,source_lane,authority_scope "
        "FROM calibration_station_daily_high_truth "
        "WHERE station_id=? AND local_date=? AND settlement_definition=? "
        "AND source_lane='AWC' AND qc_status='ACCEPTED' AND authority_scope IN "
        "('FORMAL_NATIVE_ELIGIBLE','NO_LIVE_REPLAY_CANDIDATE') "
        "ORDER BY revision_no DESC LIMIT 1",
        (station_id, event_date, settlement_definition),
    ).fetchone()
    if truth is None or not truth[1] or _utc("truth_committed", truth[1]) < day_end:
        raise ContractError("rolling_native_taf_canonical_truth_missing")
    taf = conn.execute(
        "SELECT provider_available_at_utc,tx_c,cloud_cover_fraction,"
        "precipitation_probability,wind_speed_mps,content_sha256,source_class,truth_label_authority "
        "FROM calibration_historical_taf_capture "
        "WHERE station_id=? AND event_date=? AND settlement_timezone=? "
        # Historical TAF captures have required ``tx_c`` by schema.  The
        # optional ``tx_available`` flag belongs only to forward receipts;
        # querying it here would reject every exact join against the real
        # append-only historical table once rolling OOS becomes READY.
        "AND tx_c IS NOT NULL "
        "AND authority_scope='CALIBRATION_TRAIN_ONLY' "
        "AND truth_label_authority='FORMAL_STATION_LANE_TRUTH' "
        "AND provider_available_at_utc>? AND provider_available_at_utc<? "
        # Historical captures are append-only and deliberately have no
        # revision sequence: a replay can retain distinct source samples for
        # the same bulletin time.  Resolve that legitimate tie by immutable
        # content identity instead of referring to the forward-only column.
        "ORDER BY provider_available_at_utc,content_sha256 LIMIT 1",
        (
            station_id,
            event_date,
            settlement_timezone,
            decision.isoformat().replace("+00:00", "Z"),
            day_end.isoformat().replace("+00:00", "Z"),
        ),
    ).fetchone()
    if taf is None:
        raise ContractError("rolling_native_taf_exact_taf_missing")
    return truth, taf, day_end.isoformat().replace("+00:00", "Z")


#: Outcomes that mean "this series has no complete evidence yet", as opposed
#: to a violated contract.  A day whose truth has not settled and a decision
#: with no bulletin after it are both states the archive grows out of; the
#: bucket contract is missing for scopes the archive never captured at all.
SCOPE_EVIDENCE_GAPS = frozenset({
    "rolling_native_taf_canonical_truth_missing",
    "rolling_native_taf_exact_taf_missing",
    "rolling_native_taf_historical_bucket_missing",
})


def _scope_evidence_gap(
    conn: sqlite3.Connection,
    candidates: Sequence[Mapping[str, Any]],
) -> str | None:
    """The first incomplete-evidence reason in this series, if any."""

    first = candidates[0]
    try:
        for candidate in candidates:
            _bucket_intervals_for_candidate(conn, candidate)
            for prediction in candidate.get("predictions") or []:
                _taf_truth_for_prediction(
                    conn,
                    station_id=str(first["station_id"]),
                    event_date=str(prediction.get("event_date") or ""),
                    settlement_timezone=str(first["settlement_timezone"]),
                    settlement_definition=str(first["settlement_definition"]),
                    decision_at_utc=str(prediction["decision_at_utc"]),
                )
    except ContractError as exc:
        if str(exc) in SCOPE_EVIDENCE_GAPS:
            return str(exc)
        raise
    return None


def build_rolling_taf_exact_joins(
    conn: sqlite3.Connection,
    rolling_manifest: Mapping[str, Any],
    *,
    skipped_scopes: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Join each complete rolling native series to later historical AWC TAF.

    Returned joins use the existing exact-history schema, so the regular TAF
    fitter performs its unchanged 24-sample and lineage validation.  Only
    formal global-AWC TAF lineage can enter the joined series.

    A series whose evidence is incomplete -- a day whose truth has not settled,
    or one with no bulletin arriving after the prediction -- is dropped whole
    rather than partially: the blocks are non-overlapping by construction and
    silently shortening one would break the independence the sample count is
    counted against.  Dropping it must not take the station's other series with
    it, though.  Lead buckets differ in whether a bulletin can arrive after
    their decision at all, so one structurally unservable bucket used to abort
    every usable bucket at the same station.  Pass ``skipped_scopes`` to
    receive what was dropped and why.
    """

    if rolling_manifest.get("schema") != SCHEMA:
        raise ContractError("rolling_native_taf_manifest_schema_invalid")
    _verify_hash(rolling_manifest, name="manifest")
    by_scope: dict[tuple[str, str, str, tuple[str, ...]], list[Mapping[str, Any]]] = defaultdict(list)
    for candidate in rolling_manifest.get("candidates") or []:
        if not isinstance(candidate, Mapping):
            raise ContractError("rolling_native_taf_candidate_invalid")
        _verify_hash(candidate, name="candidate")
        key = (
            str(candidate["station_id"]),
            str(candidate["season_bin"]),
            str(candidate["lead_bucket"]),
            tuple(str(value) for value in candidate["selected_system_ids"]),
        )
        by_scope[key].append(candidate)
    joins: list[dict[str, Any]] = []
    for _key, candidates in sorted(by_scope.items()):
        candidates.sort(key=lambda item: int((item.get("rolling_origin_block") or {}).get("block_index", -1)))
        first = candidates[0]
        expected_blocks = list(range(len(candidates)))
        actual_blocks = [int((item.get("rolling_origin_block") or {}).get("block_index", -1)) for item in candidates]
        if actual_blocks != expected_blocks:
            raise ContractError("rolling_native_taf_block_sequence_invalid")
        # Ask for this series' evidence before building it.  Only the two
        # incomplete-evidence outcomes are answered here; a contract violation
        # still aborts, because that is corruption rather than a series that is
        # merely unservable.
        incomplete = _scope_evidence_gap(conn, candidates)
        if incomplete is not None:
            if skipped_scopes is not None:
                skipped_scopes.append({
                    "station_id": str(first["station_id"]),
                    "season_bin": str(first["season_bin"]),
                    "lead_bucket": str(first["lead_bucket"]),
                    "run_cycle": str(first["run_cycle"]),
                    "selected_system_ids": [
                        str(value) for value in first["selected_system_ids"]
                    ],
                    "reason": incomplete,
                })
            continue
        samples = []
        prediction_shas: list[str] = []
        taf_lineages: list[dict[str, Any]] = []
        seen_days: set[str] = set()
        for candidate in candidates:
            if (
                candidate["settlement_timezone"] != first["settlement_timezone"]
                or candidate["settlement_definition"] != first["settlement_definition"]
                or candidate["run_cycle_by_system"] != first["run_cycle_by_system"]
            ):
                raise ContractError("rolling_native_taf_series_scope_mismatch")
            intervals_by_day = _bucket_intervals_for_candidate(conn, candidate)
            for prediction in candidate.get("predictions") or []:
                day = str(prediction.get("event_date") or "")
                if not day or day in seen_days:
                    raise ContractError("rolling_native_taf_prediction_day_not_unique")
                seen_days.add(day)
                if date.fromisoformat(str(prediction["trained_through_event_date"])) >= date.fromisoformat(day):
                    raise ContractError("rolling_native_taf_prediction_leakage")
                truth, taf, day_end = _taf_truth_for_prediction(
                    conn,
                    station_id=str(first["station_id"]),
                    event_date=day,
                    settlement_timezone=str(first["settlement_timezone"]),
                    settlement_definition=str(first["settlement_definition"]),
                    decision_at_utc=str(prediction["decision_at_utc"]),
                )
                probabilities = {str(bucket): float(value) for bucket, value in (prediction.get("probability") or {}).items()}
                centers = {bucket: float(bucket) for bucket in probabilities}
                rebinned, live_centers = _rebin_probabilities(
                    probabilities, centers, intervals_by_day[day]
                )
                label = str(taf[7] or "")
                reference_only = False
                prediction_sha = str(prediction["prediction_content_sha256"])
                prediction_shas.append(prediction_sha)
                taf_lineages.append({
                    "capture_content_sha256": str(taf[5]),
                    "truth_label_authority": label,
                    "reference_only": reference_only,
                })
                samples.append({
                    "station_id": str(first["station_id"]),
                    "event_date": day,
                    "settlement_timezone": str(first["settlement_timezone"]),
                    "calibration_scope": {
                        "season_bin": str(first["season_bin"]),
                        "lead_bucket": str(first["lead_bucket"]),
                        "run_cycle": str(first["run_cycle"]),
                        "settlement_definition": str(first["settlement_definition"]),
                    },
                    "native_p0": {
                        "probability_pathway": P0_PROBABILITY_PATHWAY,
                        "prediction_oos": True,
                        "trained_through_event_date": str(prediction["trained_through_event_date"]),
                        "decision_at_utc": str(prediction["decision_at_utc"]),
                        "ensemble_system_ids": list(first["selected_system_ids"]),
                        "independence_family_ids": list(first["independence_family_ids"]),
                        "system_family_ids": dict(first["system_family_ids"]),
                        "system_weights": dict(first["system_weights"]),
                        "family_weight_caps": dict(first["family_weight_caps"]),
                        "probabilities": rebinned,
                        "bucket_centers_c": live_centers,
                        "prediction_content_sha256": prediction_sha,
                    },
                    "taf": {
                        "available_at_utc": str(taf[0]),
                        "tx_c": float(taf[1]),
                        "cloud_cover_fraction": float(taf[2]),
                        "precipitation_probability": float(taf[3]),
                        "wind_speed_mps": float(taf[4]),
                        "capture_content_sha256": str(taf[5]),
                        "source_class": str(taf[6]),
                        "truth_label_authority": label,
                        "reference_only": reference_only,
                    },
                    "truth": {
                        "tmax_c": float(truth[0]),
                        "canonical_truth_id": str(truth[2]),
                        "source_lane": str(truth[3]),
                        "authority_scope": str(truth[4]),
                    },
                    "taf_lineage": {
                        "capture_content_sha256": str(taf[5]),
                        "truth_label_authority": label,
                        "reference_only": reference_only,
                        "enters_realtime_truth": False,
                        "enters_p1": False,
                        "enters_hard_no": False,
                    },
                    "settlement_window": {"end_utc": day_end},
                })
        if len(samples) > MAX_JOIN_ROWS:
            raise ContractError("rolling_native_taf_join_row_bound_exceeded")
        crossfit_sha = content_sha256({
            "rolling_manifest_sha256": str(rolling_manifest["content_sha256"]),
            "candidate_content_sha256s": [str(candidate["content_sha256"]) for candidate in candidates],
            "prediction_content_shas": prediction_shas,
            "taf_lineages": taf_lineages,
        })
        identity = {
            "schema": EXACT_JOIN_SCHEMA,
            "source_class": SOURCE_CLASS,
            # The downstream TAF artifact is keyed by this full scope.  Keep
            # it in the signed envelope as well as every sample so a bounded
            # replay never has to infer provenance from an output filename.
            "station_id": str(first["station_id"]),
            "season_bin": str(first["season_bin"]),
            "lead_bucket": str(first["lead_bucket"]),
            "run_cycle": str(first["run_cycle"]),
            "settlement_definition": str(first["settlement_definition"]),
            "settlement_timezone": str(first["settlement_timezone"]),
            "nwp_crossfit_prediction_artifact_sha256": crossfit_sha,
            "sample_count": len(samples),
            "samples": samples,
        }
        joins.append({**identity, "content_sha256": content_sha256(identity)})
    return joins


def split_rolling_taf_exact_join(
    join: Mapping[str, Any],
    *,
    min_fit_sample_n: int | None = None,
    min_transport_sample_n: int | None = None,
    preferred_transport_sample_n: int | None = None,
) -> dict[str, Any]:
    """Cut one rolling series into a fit window and a strictly later one.

    A TAF revision artifact cannot be qualified on the rows it was fitted on.
    The fit stamps ``trained_through_utc`` at the last truth close it consumed,
    and the native transport gate requires that instant to precede every
    evaluated row's decision -- so an artifact replayed on its own join fails
    ``taf_native_strict_asof_violation`` on the first row.  Handing the gate a
    freshly built join does not help either: the artifact carries the crossfit
    digest of the dataset it was fitted from, and a differently-built join
    carries its own, which is ``taf_native_artifact_crossfit_mismatch``.

    Both halves returned here quote the series' own
    ``nwp_crossfit_prediction_artifact_sha256``, which
    :func:`build_rolling_taf_exact_joins` already computes over every candidate
    in the scope rather than over one sample subset.  The fit and the gate
    therefore agree on the NWP combination that produced both windows while
    remaining disjoint in time.

    Rows falling between the two windows are dropped rather than assigned:
    with a two-day lead the day after the fit window is decided before that
    window's truth has closed, so keeping it would leak.  Neither the 24-sample
    fit minimum nor the 24-sample transport minimum is relaxed to compensate.
    """

    from .taf_calibration import MIN_TAF_CALIBRATION_SAMPLE_N
    from .taf_nwp_transport_oos import MIN_TRANSPORT_SAMPLE_N

    fit_minimum = int(
        MIN_TAF_CALIBRATION_SAMPLE_N
        if min_fit_sample_n is None
        else min_fit_sample_n
    )
    transport_minimum = int(
        MIN_TRANSPORT_SAMPLE_N
        if min_transport_sample_n is None
        else min_transport_sample_n
    )
    if fit_minimum < MIN_TAF_CALIBRATION_SAMPLE_N or transport_minimum < MIN_TRANSPORT_SAMPLE_N:
        raise ContractError("rolling_native_taf_split_minimum_below_gate")
    if join.get("schema") != EXACT_JOIN_SCHEMA or join.get("source_class") != SOURCE_CLASS:
        raise ContractError("rolling_native_taf_split_join_schema_invalid")
    _verify_hash(join, name="split_join")
    rows = join.get("samples")
    if not isinstance(rows, list) or int(join.get("sample_count", -1)) != len(rows):
        raise ContractError("rolling_native_taf_split_samples_invalid")
    ordered = sorted(rows, key=lambda item: str(item.get("event_date") or ""))
    if len({str(item.get("event_date") or "") for item in ordered}) != len(ordered):
        raise ContractError("rolling_native_taf_split_event_date_duplicate")

    def _document(selected: Sequence[Mapping[str, Any]], *, role: str) -> dict[str, Any]:
        identity = {
            str(key): value
            for key, value in join.items()
            if key not in {"content_sha256", "samples", "sample_count"}
        }
        identity["series_role"] = role
        identity["series_content_sha256"] = str(join["content_sha256"])
        identity["sample_count"] = len(selected)
        identity["samples"] = [dict(item) for item in selected]
        return {**identity, "content_sha256": content_sha256(identity)}

    # The transport window is what a later OOS verdict will be computed on,
    # so give it the size that verdict wants as soon as the series can afford
    # it -- but never let that stop a split existing.  Maximising the fit
    # window against the bare floor pinned transport at its minimum forever:
    # every new day went to the fit side, so the OOS block never grew and a
    # gate wanting more than the floor could never be satisfied by waiting.
    # Try the preferred size first, fall back to the floor, and keep growing
    # the fit window with whatever is left over after that.
    preferred = max(
        transport_minimum,
        int(
            transport_minimum
            if preferred_transport_sample_n is None
            else preferred_transport_sample_n
        ),
    )
    largest_fit = len(ordered) - preferred
    if largest_fit < fit_minimum:
        largest_fit = len(ordered) - transport_minimum
    for fit_count in range(largest_fit, fit_minimum - 1, -1):
        fit_rows = ordered[:fit_count]
        boundary = max(
            _utc("split_truth_closed", item.get("settlement_window", {}).get("end_utc"))
            for item in fit_rows
        )
        transport_rows = [
            item
            for item in ordered[fit_count:]
            if _utc(
                "split_decision_at",
                item.get("native_p0", {}).get("decision_at_utc"),
            ) > boundary
        ]
        if len(transport_rows) < transport_minimum:
            continue
        return {
            "state": "READY",
            "station_id": str(join.get("station_id") or ""),
            "season_bin": str(join.get("season_bin") or ""),
            "lead_bucket": str(join.get("lead_bucket") or ""),
            "run_cycle": str(join.get("run_cycle") or ""),
            "series_sample_count": len(ordered),
            "fit_sample_count": len(fit_rows),
            "transport_sample_count": len(transport_rows),
            "dropped_boundary_sample_count": (
                len(ordered) - len(fit_rows) - len(transport_rows)
            ),
            "strict_boundary_utc": boundary.isoformat().replace("+00:00", "Z"),
            "minimum_fit_sample_n": fit_minimum,
            "minimum_transport_sample_n": transport_minimum,
            "fit": _document(fit_rows, role="TAF_ARTIFACT_FIT"),
            "transport": _document(transport_rows, role="TAF_TRANSPORT_OOS"),
        }
    return {
        "state": "ACCUMULATING",
        "station_id": str(join.get("station_id") or ""),
        "season_bin": str(join.get("season_bin") or ""),
        "lead_bucket": str(join.get("lead_bucket") or ""),
        "run_cycle": str(join.get("run_cycle") or ""),
        "series_sample_count": len(ordered),
        "fit_sample_count": 0,
        "transport_sample_count": 0,
        "dropped_boundary_sample_count": 0,
        "strict_boundary_utc": "",
        "minimum_fit_sample_n": fit_minimum,
        "minimum_transport_sample_n": transport_minimum,
        "required_sample_count": fit_minimum + transport_minimum,
        "missing_sample_count": max(
            0, fit_minimum + transport_minimum - len(ordered)
        ),
        "fit": None,
        "transport": None,
    }


__all__ = [
    "EXACT_JOIN_SCHEMA",
    "MAX_BLOCKS",
    "SCHEMA",
    "build_rolling_origin_native_oos",
    "build_rolling_taf_exact_joins",
    "plan_non_overlapping_rolling_blocks",
    "split_rolling_taf_exact_join",
]
