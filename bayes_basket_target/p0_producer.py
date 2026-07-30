"""Build validated NWP P0 packets from native curves and calibration SSOT.

This package module is the sole implementation used by both the resident
producer and the command-line adapter.  It owns mature-run fallback,
calibration/ensemble resolution, packet compilation, and content-addressed
P0 inbox updates.  It never submits orders or calls an order gateway.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import itertools
import json
import math
from pathlib import Path
import sqlite3
from typing import Any, Mapping, Sequence

from bayes_basket_target.contracts import (  # noqa: E402
    CalibrationActivation,
    ContractError,
    CurrentAsOfModelRun,
    ModelEnsembleArtifact,
    content_sha256,
)
from bayes_basket_target.calibration import (  # noqa: E402
    resolve_active_calibration_artifact,
    resolve_active_ensemble_artifact,
    select_latest_complete_model_run_bundle,
)
from bayes_basket_target.database import resolve_target_database  # noqa: E402
from bayes_basket_target.market_registry import load_market_registry_snapshot  # noqa: E402
from bayes_basket_target.et_history import event_day_window  # noqa: E402
from bayes_basket_target.settlement_time import (  # noqa: E402
    assert_probability_timezone_matches_market,
    registry_settlement_timezone,
)
from bayes_basket_target.production_io import (  # noqa: E402
    ProductionArtifactBundle,
    load_production_artifacts,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    load_bounded_json,
    write_bounded_json_if_changed,
)
from bayes_basket_target.station_registry import station_coordinates  # noqa: E402
from bayes_basket_target.strict_asof_replay import (  # noqa: E402
    lead_bucket_for_maturity,
    maturity_key_for_model_run,
    season_bin_for_event_date,
)
from bayes_basket_target.probability_packet import build_probability_envelope  # noqa: E402
from bayes_basket_target.p0_historical_meta import (  # noqa: E402
    P0HistoricalMetaCombinationArtifact,
    compile_p0_historical_tilt_proof,
    compile_p0_historical_zero_tilt_proof,
)
from bayes_basket_target.same_day_climatology import (  # noqa: E402
    SameDayClimatologyArtifact,
)
from bayes_basket_target.probability_evidence_store import (  # noqa: E402
    p0_historical_meta_scope_key,
    resolve_active_probability_evidence_artifact,
    same_day_climatology_scope_key,
)


SCHEMA = "bayes_basket_target_p0_packet_build_v2"
DECODE_SCHEMA = "bayes_basket_target_native_model_arrival_decode_evidence_v1"
REGISTRY_SCHEMA = "bayes_basket_target_market_registry_snapshot_v1"
REGISTRY_SOURCE_SCOPES = frozenset({
    "shared_live_registry_export_only",
    "target_local_registry_scope_export",
})
MODEL_SET = frozenset({"ECMWF", "GFS", "ICON", "GEM", "CMA"})
SETTLEMENT_DEFINITION = "official_daily_tmax_c"


def _parse_utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ContractError("p0_packet_timestamp_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError("p0_packet_timestamp_timezone_required")
    return parsed.astimezone(timezone.utc)


def _temperature_to_c(value: float, unit: str) -> float:
    return float(value) if unit == "C" else (float(value) - 32.0) * 5.0 / 9.0


def bucket_intervals_from_registry_rows(rows: Sequence[Mapping[str, Any]]) -> dict[str, tuple[float | None, float | None]]:
    """Convert integer C/F settlement buckets to continuous Celsius edges."""

    if not rows:
        raise ContractError("p0_registry_rows_missing")
    parsed: list[dict[str, Any]] = []
    station = str(rows[0].get("station_id") or "")
    event_date = str(rows[0].get("event_date") or "")
    unit = str(rows[0].get("settlement_unit") or "").upper()
    if not station or not event_date or unit not in {"C", "F"}:
        raise ContractError("p0_registry_scope_invalid")
    for row in rows:
        if (
            str(row.get("station_id") or "") != station
            or str(row.get("event_date") or "") != event_date
            or str(row.get("settlement_unit") or "").upper() != unit
        ):
            raise ContractError("p0_registry_scope_mixed")
        try:
            registry = json.loads(str(row.get("registry_json") or ""))
        except (TypeError, ValueError) as exc:
            raise ContractError("p0_registry_json_invalid") from exc
        if not isinstance(registry, Mapping):
            raise ContractError("p0_registry_json_invalid")
        lower = registry.get("lower")
        upper = registry.get("upper")
        parsed.append({
            "bucket": str(row.get("outcome") or registry.get("outcome") or ""),
            "lower": None if lower is None else float(lower),
            "upper": None if upper is None else float(upper),
        })
    parsed.sort(key=lambda item: (
        -math.inf if item["lower"] is None else item["lower"],
        math.inf if item["upper"] is None else item["upper"],
    ))
    if not parsed[0]["bucket"] or parsed[0]["lower"] is not None or parsed[-1]["upper"] is not None:
        raise ContractError("p0_registry_tail_buckets_missing")
    for item in parsed[1:-1]:
        if (
            item["lower"] is None
            or item["upper"] is None
            or float(item["lower"]) > float(item["upper"])
            or not float(item["lower"]).is_integer()
            or not float(item["upper"]).is_integer()
        ):
            raise ContractError("p0_registry_finite_bucket_invalid")
    for left, right in zip(parsed, parsed[1:]):
        if left["upper"] is None or right["lower"] is None:
            raise ContractError("p0_registry_internal_tail_invalid")
        if float(left["upper"]) + 1.0 != float(right["lower"]):
            raise ContractError("p0_registry_bucket_gap")
    intervals: dict[str, tuple[float | None, float | None]] = {}
    for item in parsed:
        lower = item["lower"]
        upper = item["upper"]
        lower_edge = None if lower is None else _temperature_to_c(float(lower) - 0.5, unit)
        upper_edge = None if upper is None else _temperature_to_c(float(upper) + 0.5, unit)
        intervals[item["bucket"]] = (lower_edge, upper_edge)
    return intervals


def market_bucket_bridge_identity(
    rows: Sequence[Mapping[str, Any]],
    *,
    bucket_intervals: Mapping[str, tuple[float | None, float | None]],
    settlement_timezone: str,
) -> dict[str, Any]:
    """Bind one station-day bucket contract without binding unrelated catalog rows.

    A registry snapshot is a transport/catalog generation shared by many
    stations and dates.  Using its root SHA as a probability bridge made a
    P0 packet change when an unrelated market was added or refreshed.  The
    bridge authority is instead the exact station-day market/token/bucket
    semantics consumed by this probability vector.
    """

    if not rows:
        raise ContractError("p0_bridge_registry_rows_missing")
    station_id = str(rows[0].get("station_id") or "").strip().upper()
    event_date = str(rows[0].get("event_date") or "").strip()
    settlement_unit = str(
        rows[0].get("settlement_unit") or ""
    ).strip().upper()
    if (
        not station_id
        or not event_date
        or settlement_unit not in {"C", "F"}
        or set(bucket_intervals)
        != {str(row.get("outcome") or "") for row in rows}
    ):
        raise ContractError("p0_bridge_scope_invalid")
    contracts: list[dict[str, Any]] = []
    seen_outcomes: set[str] = set()
    for row in rows:
        if (
            str(row.get("station_id") or "").strip().upper() != station_id
            or str(row.get("event_date") or "").strip() != event_date
            or str(row.get("settlement_timezone") or "").strip()
            != settlement_timezone
            or str(row.get("settlement_unit") or "").strip().upper()
            != settlement_unit
        ):
            raise ContractError("p0_bridge_scope_mixed")
        try:
            registry = json.loads(str(row.get("registry_json") or ""))
        except (TypeError, ValueError) as exc:
            raise ContractError("p0_bridge_registry_json_invalid") from exc
        if not isinstance(registry, Mapping):
            raise ContractError("p0_bridge_registry_json_invalid")
        outcome = str(row.get("outcome") or "").strip()
        if not outcome or outcome in seen_outcomes:
            raise ContractError("p0_bridge_outcome_duplicate")
        seen_outcomes.add(outcome)
        lower, upper = bucket_intervals[outcome]
        contracts.append({
            "outcome": outcome,
            "market_id": str(registry.get("market_id") or "").strip(),
            "condition_id": str(
                registry.get("condition_id") or ""
            ).strip(),
            "yes_token": str(row.get("yes_token") or "").strip(),
            "no_token": str(row.get("no_token") or "").strip(),
            "rules_sha256": str(row.get("rules_sha256") or "").strip(),
            "lower_c": None if lower is None else float(lower),
            "upper_c": None if upper is None else float(upper),
        })
    if any(
        not item["market_id"]
        or not item["yes_token"]
        or not item["no_token"]
        or len(item["rules_sha256"]) != 64
        for item in contracts
    ):
        raise ContractError("p0_bridge_market_identity_incomplete")
    contracts.sort(
        key=lambda item: (
            -math.inf
            if item["lower_c"] is None
            else float(item["lower_c"]),
            math.inf
            if item["upper_c"] is None
            else float(item["upper_c"]),
            item["outcome"],
        )
    )
    return {
        "schema": "bayes_basket_target_market_bucket_bridge_v2",
        "station_id": station_id,
        "event_date": event_date,
        "settlement_timezone": settlement_timezone,
        "settlement_unit": settlement_unit,
        "bucket_contracts": contracts,
    }


def _validated_curve_entries(
    evidence: Mapping[str, Any],
    *,
    event_date: str,
) -> list[Mapping[str, Any]]:
    if evidence.get("schema") != DECODE_SCHEMA:
        raise ContractError("p0_native_decode_scope_invalid")
    declared_date = str(evidence.get("target_local_date") or "").strip()
    if declared_date and declared_date != event_date:
        raise ContractError("p0_native_decode_scope_invalid")
    supplied = str(evidence.get("receipt_sha256") or "")
    identity = {key: value for key, value in evidence.items() if key != "receipt_sha256"}
    if supplied != content_sha256(identity):
        raise ContractError("p0_native_decode_receipt_sha256_mismatch")
    container = evidence.get("curves")
    rows = container.get("curves") if isinstance(container, Mapping) else None
    if not isinstance(rows, list):
        raise ContractError("p0_native_curves_missing")
    if any(
        not isinstance(row, Mapping) or str(row.get("event_date") or "") != event_date
        for row in rows
    ):
        raise ContractError("p0_native_decode_curve_date_mismatch")
    return list(rows)


def _group_available_curve_entries(
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, list[dict[str, Mapping[str, Any]]]]:
    grouped: dict[tuple[str, str], dict[str, Mapping[str, Any]]] = {}
    for row in rows:
        if row.get("state") != "COMPLETE":
            continue
        station = str(row.get("station_id") or "")
        model = str(row.get("model_id") or "").upper()
        run_init = str(row.get("model_run_initialization_utc") or "")
        if (
            not station
            or model not in MODEL_SET
            or not run_init
        ):
            continue
        key = (station, run_init)
        if model in grouped.setdefault(key, {}):
            raise ContractError("p0_native_curve_duplicate_model_generation")
        grouped[key][model] = row
    available: dict[str, list[dict[str, Mapping[str, Any]]]] = {}
    for (station, _run_init), by_model in grouped.items():
        if by_model:
            available.setdefault(station, []).append(by_model)
    for bundles in available.values():
        bundles.sort(
            key=lambda by_model: _parse_utc(
                str(next(iter(by_model.values())).get("model_run_initialization_utc") or "")
            ),
            reverse=True,
        )
    return available


def _curve_rows(
    evidence: Mapping[str, Any],
    *,
    event_date: str,
) -> dict[str, list[dict[str, Mapping[str, Any]]]]:
    return _group_available_curve_entries(
        _validated_curve_entries(evidence, event_date=event_date)
    )


def _curve_rows_from_receipts(
    evidences: Sequence[Mapping[str, Any]],
    *,
    event_date: str,
) -> dict[str, list[dict[str, Mapping[str, Any]]]]:
    if not evidences:
        raise ContractError("p0_native_decode_receipts_missing")
    rows = [
        row
        for evidence in evidences
        for row in _validated_curve_entries(evidence, event_date=event_date)
    ]
    return _group_available_curve_entries(rows)


def _curve_points(curve: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw = curve.get("points") or curve.get("temperature_points")
    if not isinstance(raw, list) or not raw or not all(isinstance(item, Mapping) for item in raw):
        raise ContractError("p0_native_curve_points_missing")
    return list(raw)


def _bundle_scope(
    model_curves: Mapping[str, Mapping[str, Any]],
    *,
    station: str,
    event_date: str,
    settlement_timezone: str,
) -> tuple[str, str, str, str]:
    run_initializations = {
        str(item.get("model_run_initialization_utc") or "")
        for item in model_curves.values()
    }
    if len(run_initializations) != 1:
        raise ContractError("p0_native_run_generation_mismatch")
    run_init = next(iter(run_initializations))
    parsed_run = _parse_utc(run_init)
    run_cycle = f"{parsed_run.hour:02d}Z"
    maturity_key = maturity_key_for_model_run(
        run_init,
        event_date,
        target_timezone=settlement_timezone,
    )
    return (
        run_init,
        lead_bucket_for_maturity(maturity_key),
        run_cycle,
        season_bin_for_event_date(
            event_date,
            latitude=float(station_coordinates(station)["latitude"]),
        ),
    )


def _current_runs(
    model_curves: Mapping[str, Mapping[str, Any]],
    *,
    station: str,
    event_date: str,
    lead_bucket: str,
    run_cycle: str,
    season_bin: str,
    settlement_timezone: str,
) -> list[CurrentAsOfModelRun]:
    run_init = _bundle_scope(
        model_curves,
        station=station,
        event_date=event_date,
        settlement_timezone=settlement_timezone,
    )[0]
    runs: list[CurrentAsOfModelRun] = []
    for model in sorted(model_curves):
        curve = model_curves[model]
        points = _curve_points(curve)
        curve_sha = str(curve.get("curve_sha256") or "")
        source_class = str(
            curve.get("source_class")
            or points[0].get("source_class")
            or "native_official"
        ).strip().lower()
        publication_times = [
            str(point.get("published_at_utc") or "").strip()
            for point in points
            if str(point.get("published_at_utc") or "").strip()
        ]
        runs.append(CurrentAsOfModelRun.from_mapping({
            "model_run_id": "current-" + curve_sha[:32],
            "model_id": model,
            "station_id": station,
            "target_local_date": event_date,
            "run_init_utc": run_init,
            "published_at_utc": (
                max(publication_times) if publication_times else None
            ),
            "received_at_utc": max(str(point.get("received_at_utc") or "") for point in points),
            "season_bin": season_bin,
            "lead_bucket": lead_bucket,
            "run_cycle": run_cycle,
            "raw_tmax_c": curve.get("forecast_daily_max_c"),
            "settlement_definition": SETTLEMENT_DEFINITION,
            "source_object_sha256": curve_sha,
            "source_class": source_class,
        }))
    return runs


def _select_latest_compatible_bundle(
    curve_bundles: Sequence[Mapping[str, Mapping[str, Any]]],
    *,
    artifacts: ProductionArtifactBundle,
    station: str,
    event_date: str,
    settlement_timezone: str,
    decision_at_utc: str,
) -> tuple[
    list[CurrentAsOfModelRun],
    list[CurrentAsOfModelRun],
    ModelEnsembleArtifact,
]:
    """Choose one probability mask plus its full calibrated diagnostic set."""

    failures: list[str] = []
    for model_curves in curve_bundles:
        _run_init, lead_bucket, run_cycle, season_bin = _bundle_scope(
            model_curves,
            station=station,
            event_date=event_date,
            settlement_timezone=settlement_timezone,
        )
        diagnostic_by_model: dict[str, CurrentAsOfModelRun] = {}
        for model in sorted(model_curves):
            try:
                resolve_active_calibration_artifact(
                    activations=artifacts.calibration_activations,
                    artifacts=artifacts.calibration_artifacts,
                    station_id=station,
                    model_id=model,
                    season_bin=season_bin,
                    lead_bucket=lead_bucket,
                    run_cycle=run_cycle,
                    settlement_definition=SETTLEMENT_DEFINITION,
                    decision_at_utc=decision_at_utc,
                    required_authority_scope="PRODUCTION_NO_LIVE_ONLY",
                )
                run = _current_runs(
                    {model: model_curves[model]},
                    station=station,
                    event_date=event_date,
                    lead_bucket=lead_bucket,
                    run_cycle=run_cycle,
                    season_bin=season_bin,
                    settlement_timezone=settlement_timezone,
                )[0]
                if run.is_asof(decision_at_utc):
                    diagnostic_by_model[model] = run
            except (ContractError, TypeError, ValueError):
                continue
        available = tuple(sorted(diagnostic_by_model))
        if not available:
            failures.append("no_asof_calibrated_diagnostic_models")
            continue
        diagnostic = [diagnostic_by_model[model] for model in available]
        exact_masks = [
            subset
            for size in range(len(available), 1, -1)
            for subset in itertools.combinations(available, size)
        ]
        for available_models in exact_masks:
            try:
                ensemble, _activation = resolve_active_ensemble_artifact(
                    activations=artifacts.calibration_activations,
                    artifacts=artifacts.ensemble_artifacts,
                    station_id=station,
                    season_bin=season_bin,
                    lead_bucket=lead_bucket,
                    run_cycle=run_cycle,
                    settlement_definition=SETTLEMENT_DEFINITION,
                    available_models=available_models,
                    decision_at_utc=decision_at_utc,
                    required_authority_scope="PRODUCTION_NO_LIVE_ONLY",
                )
                for model in available_models:
                    resolve_active_calibration_artifact(
                        activations=artifacts.calibration_activations,
                        artifacts=artifacts.calibration_artifacts,
                        station_id=station,
                        model_id=model,
                        season_bin=season_bin,
                        lead_bucket=lead_bucket,
                        run_cycle=run_cycle,
                        settlement_definition=SETTLEMENT_DEFINITION,
                        decision_at_utc=decision_at_utc,
                        required_authority_scope="PRODUCTION_NO_LIVE_ONLY",
                    )
                selected_curves = {
                    model: model_curves[model]
                    for model in available_models
                }
                runs = _current_runs(
                    selected_curves,
                    station=station,
                    event_date=event_date,
                    lead_bucket=lead_bucket,
                    run_cycle=run_cycle,
                    season_bin=season_bin,
                    settlement_timezone=settlement_timezone,
                )
                selected = list(select_latest_complete_model_run_bundle(
                    runs,
                    decision_at_utc=decision_at_utc,
                    required_lead_bucket=lead_bucket,
                    required_run_cycle=run_cycle,
                    required_settlement_definition=SETTLEMENT_DEFINITION,
                ))
                return selected, diagnostic, ensemble
            except (ContractError, TypeError, ValueError) as exc:
                failures.append(str(exc))
    reason = failures[0] if failures and len(set(failures)) == 1 else "no_activated_compatible_model_mask"
    raise ContractError(f"p0_no_activated_compatible_model_mask_asof:{reason}")


def compatible_run_candidates(
    *,
    artifacts: ProductionArtifactBundle,
    station: str,
    event_date: str,
    settlement_timezone: str,
    decision_at_utc: str,
    run_initializations: Sequence[str],
) -> list[dict[str, Any]]:
    """Preflight run scopes before network capture.

    Publication maturity and calibration maturity are separate.  A newer run
    that has no activated station/season/lead/cycle exact mask must not be
    downloaded and then silently displace the last eligible run.
    """

    candidates: list[dict[str, Any]] = []
    for run_init in run_initializations:
        parsed_run = _parse_utc(run_init)
        run_cycle = f"{parsed_run.hour:02d}Z"
        maturity_key = maturity_key_for_model_run(
            run_init,
            event_date,
            target_timezone=settlement_timezone,
        )
        lead_bucket = lead_bucket_for_maturity(maturity_key)
        season_bin = season_bin_for_event_date(
            event_date,
            latitude=float(station_coordinates(station)["latitude"]),
        )
        # The prefetch authority is the exact OOS-selected ensemble artifact,
        # not an arbitrary subset of whichever providers happen to arrive.
        # Supported subset masks remain explicit materialization fallbacks,
        # but they must not redefine the primary acquisition target.
        resolved_by_sha: dict[
            str,
            tuple[ModelEnsembleArtifact, CalibrationActivation],
        ] = {}
        for candidate in artifacts.ensemble_artifacts.values():
            mask = tuple(candidate.candidate_models)
            try:
                ensemble, activation = resolve_active_ensemble_artifact(
                    activations=artifacts.calibration_activations,
                    artifacts=artifacts.ensemble_artifacts,
                    station_id=station,
                    season_bin=season_bin,
                    lead_bucket=lead_bucket,
                    run_cycle=run_cycle,
                    settlement_definition=SETTLEMENT_DEFINITION,
                    available_models=mask,
                    decision_at_utc=decision_at_utc,
                    required_authority_scope="PRODUCTION_NO_LIVE_ONLY",
                )
                for model in mask:
                    resolve_active_calibration_artifact(
                        activations=artifacts.calibration_activations,
                        artifacts=artifacts.calibration_artifacts,
                        station_id=station,
                        model_id=model,
                        season_bin=season_bin,
                        lead_bucket=lead_bucket,
                        run_cycle=run_cycle,
                        settlement_definition=SETTLEMENT_DEFINITION,
                        decision_at_utc=decision_at_utc,
                        required_authority_scope="PRODUCTION_NO_LIVE_ONLY",
                    )
                resolved_by_sha[ensemble.content_sha256] = (
                    ensemble,
                    activation,
                )
            except (ContractError, TypeError, ValueError):
                continue
        if len(resolved_by_sha) == 1:
            ensemble, activation = next(iter(resolved_by_sha.values()))
            binding_identity = {
                "selected_models": list(ensemble.candidate_models),
                "ensemble_artifact_id": ensemble.ensemble_artifact_id,
                "ensemble_artifact_sha256": ensemble.content_sha256,
                "selection_evidence_sha256": (
                    ensemble.selection_evidence_sha256
                ),
                "activation_id": activation.activation_id,
                "activation_generation": activation.generation,
                "activation_sha256": activation.content_sha256,
                "authority_scope": activation.authority_scope,
            }
            candidates.append({
                "run_initialization_utc": run_init,
                "lead_bucket": lead_bucket,
                "run_cycle": run_cycle,
                "season_bin": season_bin,
                "activation_binding": {
                    **binding_identity,
                    "binding_sha256": content_sha256(binding_identity),
                },
                "ensemble_activation": asdict(activation),
                "ensemble_artifact": asdict(ensemble),
            })
    return candidates


def _registry_scopes(payload: Mapping[str, Any], *, event_date: str) -> dict[str, list[Mapping[str, Any]]]:
    if (
        payload.get("schema") != REGISTRY_SCHEMA
        or payload.get("source_scope") not in REGISTRY_SOURCE_SCOPES
    ):
        raise ContractError("p0_registry_snapshot_invalid")
    rows = payload.get("rows")
    if not isinstance(rows, list) or int(payload.get("row_count") or -1) != len(rows):
        raise ContractError("p0_registry_row_count_invalid")
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        if (
            isinstance(row, Mapping)
            and row.get("event_date") == event_date
            and int(row.get("market_active") or 0) == 1
            and int(row.get("source_eligible") or 0) == 1
        ):
            grouped.setdefault(str(row.get("station_id") or ""), []).append(row)
    return grouped


def build_packets(
    *,
    db_path: Path,
    decode_evidence_path: Path | Sequence[Path],
    registry_snapshot_path: Path,
    event_date: str,
    decision_at_utc: str,
    fresh_until_utc: str,
    output_dir: Path,
    inbox_path: Path | None = None,
    emit_packet_files: bool = True,
) -> dict[str, Any]:
    _parse_utc(decision_at_utc)
    if _parse_utc(fresh_until_utc) <= _parse_utc(decision_at_utc):
        raise ContractError("p0_fresh_until_not_after_decision")
    decode_paths = (
        [decode_evidence_path]
        if isinstance(decode_evidence_path, Path)
        else list(decode_evidence_path)
    )
    decode_receipts = [load_bounded_json(path) for path in decode_paths]
    registry = load_market_registry_snapshot(registry_snapshot_path)
    if (
        not decode_receipts
        or any(not isinstance(item, Mapping) for item in decode_receipts)
        or not isinstance(registry, Mapping)
    ):
        raise ContractError("p0_input_mapping_required")
    registry_by_station = _registry_scopes(registry, event_date=event_date)
    for evidence in decode_receipts:
        if str(evidence.get("market_registry_snapshot_sha256") or "") != str(
            registry.get("snapshot_sha256") or ""
        ):
            raise ContractError("p0_decode_registry_snapshot_lineage_mismatch")
        evidence_timezone = str(evidence.get("target_timezone") or "")
        for station_id, rows in registry_by_station.items():
            assert_probability_timezone_matches_market(
                probability_timezone=evidence_timezone,
                market_timezone=registry_settlement_timezone(
                    rows,
                    station_id=station_id,
                    event_date=event_date,
                ),
            )
    curves = _curve_rows_from_receipts(decode_receipts, event_date=event_date)
    stations = sorted(set(curves) & set(registry_by_station))
    destination = output_dir.expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    db = resolve_target_database(db_path)
    packet_paths: list[str] = []
    inbox_rows: list[dict[str, Any]] = []
    blockers: list[dict[str, str]] = []
    selected_bundle_scopes: list[dict[str, str]] = []
    with sqlite3.connect(
        f"file:{db}?mode=ro",
        uri=True,
        timeout=30.0,
        isolation_level=None,
    ) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        for station in stations:
            try:
                settlement_timezone = registry_settlement_timezone(
                    registry_by_station[station],
                    station_id=station,
                    event_date=event_date,
                )
                event_state = event_day_window(
                    event_date,
                    as_of_utc=decision_at_utc,
                    target_timezone=settlement_timezone,
                )["state"]
                if (
                    event_state == "not_started"
                    and not bool(
                        (registry.get("filters") or {}).get(
                            "include_not_started_for_p0"
                        )
                    )
                ):
                    raise ContractError("p0_forward_registry_scope_not_authorized")
                if (
                    event_state == "not_started"
                    and registry.get("forward_p0_catalog_only") is not True
                ):
                    raise ContractError("p0_forward_registry_boundary_missing")
                bundle = load_production_artifacts(
                    conn,
                    station_id=station,
                    evaluation_at_utc=decision_at_utc,
                    require_calibration=True,
                    require_policy=False,
                )
                selected_runs, diagnostic_runs, ensemble = _select_latest_compatible_bundle(
                    curves[station],
                    artifacts=bundle,
                    station=station,
                    event_date=event_date,
                    settlement_timezone=settlement_timezone,
                    decision_at_utc=decision_at_utc,
                )
                current_runs = [asdict(item) for item in selected_runs]
                diagnostic_model_runs = [asdict(item) for item in diagnostic_runs]
                selected_calibrations = {
                    run.model_id: resolve_active_calibration_artifact(
                        activations=bundle.calibration_activations,
                        artifacts=bundle.calibration_artifacts,
                        station_id=station,
                        model_id=run.model_id,
                        season_bin=run.season_bin,
                        lead_bucket=run.lead_bucket,
                        run_cycle=run.run_cycle,
                        settlement_definition=run.settlement_definition,
                        decision_at_utc=decision_at_utc,
                        required_authority_scope="PRODUCTION_NO_LIVE_ONLY",
                    )[0]
                    for run in selected_runs
                }
                diagnostic_calibrations = {
                    run.model_id: resolve_active_calibration_artifact(
                        activations=bundle.calibration_activations,
                        artifacts=bundle.calibration_artifacts,
                        station_id=station,
                        model_id=run.model_id,
                        season_bin=run.season_bin,
                        lead_bucket=run.lead_bucket,
                        run_cycle=run.run_cycle,
                        settlement_definition=run.settlement_definition,
                        decision_at_utc=decision_at_utc,
                        required_authority_scope="PRODUCTION_NO_LIVE_ONLY",
                    )[0]
                    for run in diagnostic_runs
                }
                selected_scope = {
                    "station_id": station,
                    "settlement_timezone": settlement_timezone,
                    "run_initialization_utc": selected_runs[0].run_init_utc,
                    "lead_bucket": ensemble.lead_bucket,
                    "run_cycle": ensemble.run_cycle,
                    "probability_model_count": str(len(selected_runs)),
                    "diagnostic_model_count": str(len(diagnostic_runs)),
                }
                intervals = bucket_intervals_from_registry_rows(registry_by_station[station])
                bridge_sha = content_sha256(
                    market_bucket_bridge_identity(
                        registry_by_station[station],
                        bucket_intervals=intervals,
                        settlement_timezone=settlement_timezone,
                    )
                )
                packet = {
                    "stage": "P0",
                    "station_id": station,
                    "event_date": event_date,
                    "settlement_timezone": settlement_timezone,
                    "current_model_runs": current_runs,
                    "diagnostic_model_runs": diagnostic_model_runs,
                    "calibration_artifacts": {
                        model: asdict(item)
                        for model, item in selected_calibrations.items()
                    },
                    "diagnostic_calibration_artifacts": {
                        model: asdict(item)
                        for model, item in diagnostic_calibrations.items()
                    },
                    "ensemble_artifact": asdict(ensemble),
                    "calibration_activations": [
                        asdict(item) for item in bundle.calibration_activations
                    ],
                    "bucket_intervals": intervals,
                    "decision_at_utc": decision_at_utc,
                    "fresh_until_utc": fresh_until_utc,
                    "bridge_sha256": bridge_sha,
                    "created_at_utc": decision_at_utc,
                }
                envelope = build_probability_envelope(packet)
                settlement_definition = selected_runs[0].settlement_definition
                climate_row = resolve_active_probability_evidence_artifact(
                    conn,
                    artifact_kind="SAME_DAY_CLIMATOLOGY",
                    station_id=station,
                    scope_key=same_day_climatology_scope_key(
                        event_date, settlement_definition
                    ),
                    as_of_utc=decision_at_utc,
                    required_authority_scope="FORMAL_PROMOTED",
                )
                meta_row = resolve_active_probability_evidence_artifact(
                    conn,
                    artifact_kind="P0_HISTORICAL_META",
                    station_id=station,
                    scope_key=p0_historical_meta_scope_key(
                        season_bin=selected_runs[0].season_bin,
                        lead_bucket=selected_runs[0].lead_bucket,
                        run_cycle=selected_runs[0].run_cycle,
                        settlement_definition=settlement_definition,
                        settlement_timezone=settlement_timezone,
                    ),
                    as_of_utc=decision_at_utc,
                    required_authority_scope="FORMAL_PROMOTED",
                )
                if (climate_row is None) != (meta_row is None):
                    selected_scope["p0_historical_state"] = (
                        "THETA_ZERO_INCOMPLETE_ARTIFACT_PAIR"
                    )
                elif climate_row is None:
                    selected_scope["p0_historical_state"] = (
                        "THETA_ZERO_NO_FORMAL_ARTIFACT"
                    )
                else:
                    bucket_centers_c = json.loads(
                        str(
                            envelope.lineage[
                                "bucket_centers_c_json"
                            ]
                        )
                    )
                    historical_proof = compile_p0_historical_tilt_proof(
                        artifact=P0HistoricalMetaCombinationArtifact.from_mapping(
                            meta_row["artifact"]
                        ),
                        climatology=SameDayClimatologyArtifact.from_mapping(
                            climate_row["artifact"]
                        ),
                        station_id=station,
                        target_local_date=event_date,
                        settlement_timezone=settlement_timezone,
                        settlement_definition=settlement_definition,
                        season_bin=selected_runs[0].season_bin,
                        lead_bucket=selected_runs[0].lead_bucket,
                        run_cycle=selected_runs[0].run_cycle,
                        as_of_utc=decision_at_utc,
                        model_probabilities=envelope.probabilities,
                        bucket_centers_c=bucket_centers_c,
                        model_probability_sha256=content_sha256({
                            "bucket_centers_c": bucket_centers_c,
                            "probabilities": dict(envelope.probabilities),
                        }),
                        ensemble_artifact_sha256=str(
                            envelope.lineage[
                                "ensemble_artifact_content_sha256"
                            ]
                        ),
                        ensemble_selection_evidence_sha256=str(
                            envelope.lineage[
                                "selection_evidence_sha256"
                            ]
                        ),
                        ensemble_activation_sha256=str(
                            envelope.lineage[
                                "ensemble_activation_content_sha256"
                            ]
                        ),
                        climatology_activation_sha256=str(
                            climate_row["activation_content_sha256"]
                        ),
                        meta_activation_id=str(
                            meta_row["activation_id"]
                        ),
                        meta_activation_sha256=str(
                            meta_row["activation_content_sha256"]
                        ),
                    )
                    packet["p0_historical_tilt_proof"] = (
                        historical_proof.to_dict()
                    )
                    envelope = build_probability_envelope(packet)
                    selected_scope["p0_historical_state"] = (
                        "FORMAL_PROOF_APPLIED"
                        if not historical_proof.theta_zero_fallback
                        else "FORMAL_PROOF_THETA_ZERO"
                    )
                if "p0_historical_tilt_proof" not in packet:
                    bucket_centers_c = json.loads(
                        str(
                            envelope.lineage[
                                "bucket_centers_c_json"
                            ]
                        )
                    )
                    zero_proof = compile_p0_historical_zero_tilt_proof(
                        station_id=station,
                        target_local_date=event_date,
                        settlement_timezone=settlement_timezone,
                        settlement_definition=settlement_definition,
                        season_bin=selected_runs[0].season_bin,
                        lead_bucket=selected_runs[0].lead_bucket,
                        run_cycle=selected_runs[0].run_cycle,
                        as_of_utc=decision_at_utc,
                        model_probabilities=envelope.probabilities,
                        bucket_centers_c=bucket_centers_c,
                        ensemble_artifact_sha256=str(
                            envelope.lineage[
                                "ensemble_artifact_content_sha256"
                            ]
                        ),
                        ensemble_selection_evidence_sha256=str(
                            envelope.lineage[
                                "selection_evidence_sha256"
                            ]
                        ),
                        ensemble_activation_sha256=str(
                            envelope.lineage[
                                "ensemble_activation_content_sha256"
                            ]
                        ),
                    )
                    packet["p0_historical_tilt_proof"] = (
                        zero_proof.to_dict()
                    )
                    envelope = build_probability_envelope(packet)
                selected_bundle_scopes.append(selected_scope)
                inbox_rows.append({
                    "source_class": "official_forward_model_bundle",
                    "packet_content_sha256": content_sha256(packet),
                    "packet": packet,
                })
                if emit_packet_files:
                    path = destination / f"{station}_{event_date}_p0.json"
                    write_bounded_json_if_changed(path, packet)
                    packet_paths.append(str(path))
                    write_bounded_json_if_changed(
                        destination / f"{station}_{event_date}_p0_preview.json",
                        envelope.to_dict(),
                    )
            except (ContractError, ValueError, TypeError) as exc:
                blockers.append({"station_id": station, "reason": str(exc)})
    result = {
        "schema": SCHEMA,
        "event_date": event_date,
        "decision_at_utc": decision_at_utc,
        "complete_native_station_count": len(curves),
        "registry_station_count": len(registry_by_station),
        "eligible_station_count": len(stations),
        "packet_count": len(inbox_rows),
        "blocked_count": len(blockers),
        "packet_paths": packet_paths,
        "blockers": blockers,
        "selected_bundle_scopes": selected_bundle_scopes,
        "native_decode_receipts": [str(path.expanduser().resolve()) for path in decode_paths],
        "boundary": {
            "writes_target_database": False,
            "calls_order_gateway": False,
            "places_orders": False,
            "live_order_authorized": False,
        },
    }
    inbox_state = "not_requested"
    inbox_sha = ""
    if inbox_path is not None:
        inbox_identity = {
            "schema": "bayes_basket_target_p0_inbox_v1",
            "generated_at_utc": decision_at_utc,
            "packets": inbox_rows,
        }
        inbox = {
            **inbox_identity,
            "content_sha256": content_sha256(inbox_identity),
        }
        inbox_sha = str(inbox["content_sha256"])
        destination_path = inbox_path.expanduser().resolve()
        existing = (
            load_bounded_json(destination_path)
            if destination_path.is_file()
            else None
        )
        if (
            isinstance(existing, Mapping)
            and str(existing.get("content_sha256") or "") == inbox_sha
        ):
            inbox_state = "unchanged_zero_write"
        else:
            write_bounded_json_if_changed(destination_path, inbox)
            inbox_state = "updated"
    result["p0_inbox"] = {
        "state": inbox_state,
        "path": str(inbox_path.expanduser().resolve()) if inbox_path else "",
        "content_sha256": inbox_sha,
        "packet_count": len(inbox_rows),
    }
    result["manifest_state"] = (
        "updated"
        if write_bounded_json_if_changed(destination / "manifest.json", result)
        else "unchanged_zero_write"
    )
    return result
