#!/usr/bin/env python3
"""Run one registry-bound station's explicit-cycle d2 multi-model OOS.

This vertical-slice runner accepts content-verified Open-Meteo single-run
aggregator or official-native evidence. Every historical row must carry the
declared explicit ``event_date-2`` initialization. It emits production-no-live
review evidence only and never writes a database or order gateway.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, time, timedelta, timezone
import json
import math
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import ContractError, content_sha256  # noqa: E402
from bayes_basket_target.resource_bounds import load_bounded_json  # noqa: E402
from bayes_basket_target.station_registry import station_coordinates  # noqa: E402
from scripts.build_rkpk_open_meteo_five_model_historical_oos import (  # noqa: E402
    _atomic_write,
    _build_oos,
    _meteorological_season,
)
from scripts.run_bayes_basket_target_45station_explicit_18z_oos import (  # noqa: E402
    GFS_SCHEMA,
    ICON_SCHEMA,
    _calibration_residual_c,
    _expected_run,
    _normalized_run,
    _sha256,
    _truth_rows,
)


SCHEMA = "bayes_basket_target_station_explicit_lead_oos_v3"
REGIONAL_SCHEMA = (
    "bayes_basket_target_historical_regional_exact_run_evidence_v1"
)
MODEL_PROVIDERS = {
    "ICON": "icon_global_open_meteo",
    "CMA": "cma_grapes_global_open_meteo",
    "GFS": "gfs_0p25_official",
}
NATIVE_MODEL_PROVIDERS = {
    "ECMWF": "ecmwf_ifs025_official",
    "GFS": "gfs_0p25_official",
    "GEM": "gem_gdps_official",
}


def _point_oos(
    *,
    oos: Mapping[str, Any],
    exact_join_rows: Sequence[Mapping[str, Any]],
    truths: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    winner = oos.get("research_winner")
    if not isinstance(winner, Mapping):
        return {
            "holdout_n": 0,
            "passed_vs_uniform": False,
            "blocker": "station_explicit_oos_research_winner_missing",
        }
    selected_models = tuple(
        str(model).strip().upper()
        for model in winner.get("selected_models") or []
    )
    weights = {
        str(model).strip().upper(): float(weight)
        for model, weight in (winner.get("selected_weights") or {}).items()
    }
    biases = {
        str(row.get("model_id") or "").strip().upper():
            float(row.get("bias_c"))
        for row in oos.get("calibration_artifacts") or []
        if isinstance(row, Mapping)
    }
    joined = {
        str(row.get("event_date") or ""): row
        for row in exact_join_rows
        if isinstance(row, Mapping)
    }
    selected_errors: list[float] = []
    uniform_errors: list[float] = []
    for event_date in oos.get("holdout_dates") or []:
        row = joined.get(str(event_date))
        truth = truths.get(str(event_date))
        if (
            not isinstance(row, Mapping)
            or not isinstance(truth, Mapping)
            or not selected_models
            or set(selected_models) - set(weights)
            or set(selected_models) - set(biases)
        ):
            raise ContractError(
                "station_explicit_oos_point_scope_incomplete"
            )
        model_values = row.get("models")
        if not isinstance(model_values, Mapping):
            raise ContractError(
                "station_explicit_oos_point_models_missing"
            )
        corrected = {
            model: float(model_values[model]["forecast_tmax_c"])
            + biases[model]
            for model in selected_models
        }
        official = float(truth["official_tmax_c"])
        selected_errors.append(
            sum(weights[model] * corrected[model] for model in selected_models)
            - official
        )
        uniform_errors.append(
            sum(corrected.values()) / len(corrected) - official
        )

    def mae(values: Sequence[float]) -> float:
        return sum(abs(value) for value in values) / len(values)

    def rmse(values: Sequence[float]) -> float:
        return math.sqrt(
            sum(value * value for value in values) / len(values)
        )

    selected_mae = mae(selected_errors)
    selected_rmse = rmse(selected_errors)
    uniform_mae = mae(uniform_errors)
    uniform_rmse = rmse(uniform_errors)
    return {
        "holdout_n": len(selected_errors),
        "selected_mae_c": selected_mae,
        "selected_rmse_c": selected_rmse,
        "uniform_mae_c": uniform_mae,
        "uniform_rmse_c": uniform_rmse,
        "mae_improvement_vs_uniform_c": uniform_mae - selected_mae,
        "rmse_improvement_vs_uniform_c": uniform_rmse - selected_rmse,
        "passed_vs_uniform": (
            selected_mae <= uniform_mae + 1e-12
            and selected_rmse <= uniform_rmse + 1e-12
        ),
    }


def _station_timezone(registry_path: Path, *, station_id: str) -> str:
    payload = load_bounded_json(registry_path.resolve())
    rows = payload.get("rows") if isinstance(payload, Mapping) else None
    if not isinstance(rows, list):
        raise ContractError("station_explicit_oos_registry_rows_missing")
    values = {
        str(row.get("settlement_timezone") or "").strip()
        for row in rows
        if isinstance(row, Mapping)
        and str(row.get("station_id") or "").strip().upper() == station_id
        and str(row.get("settlement_timezone") or "").strip()
    }
    if len(values) != 1:
        raise ContractError("station_explicit_oos_registry_timezone_invalid")
    return next(iter(values))


def _truth_dataset(
    truth_manifest_path: Path,
    *,
    station_id: str,
) -> Path:
    payload = load_bounded_json(truth_manifest_path.resolve())
    matches = [
        Path(str(row.get("dataset") or "")).expanduser().resolve()
        for row in (payload.get("truth_results") or [])
        if isinstance(row, Mapping)
        and str(row.get("station_id") or "").strip().upper() == station_id
        and row.get("state") == "TRUTH_READY"
    ]
    if len(matches) != 1 or not matches[0].is_file():
        raise ContractError("station_explicit_oos_truth_dataset_missing")
    return matches[0]


def _load_aggregator_rows(
    paths: Sequence[Path],
    *,
    model_id: str,
    station_id: str,
    target_timezone: str,
    run_cycle: str = "18Z",
    lead_days: int = 2,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, str]]]:
    model = str(model_id or "").strip().upper()
    expected_provider = MODEL_PROVIDERS.get(model)
    if expected_provider is None:
        raise ContractError("station_explicit_oos_model_unsupported")
    result: dict[str, dict[str, Any]] = {}
    inputs: list[dict[str, str]] = []
    for raw_path in paths:
        path = raw_path.expanduser().resolve()
        payload = load_bounded_json(path)
        source = payload.get("source") if isinstance(payload.get("source"), Mapping) else {}
        if (
            payload.get("schema") != ICON_SCHEMA
            or payload.get("provider_id") != expected_provider
            or payload.get("model_id") != model
            or (payload.get("blockers") or [])
            or source.get("run_cycle") != run_cycle
            or int(source.get("run_day_offset") or 0) != lead_days
            or source.get("single_run_parameter_explicit") is not True
        ):
            raise ContractError(
                f"station_explicit_oos_model_evidence_invalid:{model}:{path.name}"
            )
        for row in payload.get("model_rows") or []:
            if not isinstance(row, Mapping):
                raise ContractError("station_explicit_oos_model_row_invalid")
            station = str(row.get("station_id") or "").strip().upper()
            event_date = str(row.get("event_date") or "")
            run = _normalized_run(row.get("model_run_initialization_utc"))
            if station != station_id:
                continue
            if (
                str(row.get("target_timezone") or "") != target_timezone
                or run != _expected_run_cycle(
                    event_date, run_cycle, lead_days=lead_days
                )
                or row.get("daily_curve_complete") is not True
                or int(row.get("sample_count") or 0) != 24
            ):
                raise ContractError(
                    f"station_explicit_oos_model_scope_mismatch:{model}:{event_date}"
                )
            item = {
                "event_date": event_date,
                "forecast_tmax_c": float(row["forecast_daily_max_c"]),
                "provider_id": expected_provider,
                "source_class": "aggregator_republisher",
                "availability_class":
                    f"explicit_{run_cycle.lower()}_d{lead_days}_historical",
                "model_run_initialization_utc": run,
                "curve_receipt_sha256": str(row.get("receipt_sha256") or ""),
            }
            previous = result.get(event_date)
            if previous is not None and previous != item:
                raise ContractError(
                    f"station_explicit_oos_model_date_conflict:{model}:{event_date}"
                )
            result[event_date] = item
        inputs.append({"path": str(path), "sha256": _sha256(path)})
    return result, inputs


def _load_model_rows(
    paths: Sequence[Path],
    *,
    model_id: str,
    station_id: str,
    target_timezone: str,
    run_cycle: str = "18Z",
    lead_days: int = 2,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, str]]]:
    model = str(model_id or "").strip().upper()
    if model in NATIVE_MODEL_PROVIDERS:
        expected_provider = NATIVE_MODEL_PROVIDERS[model]
        result: dict[str, dict[str, Any]] = {}
        inputs: list[dict[str, str]] = []
        for raw_path in paths:
            path = raw_path.expanduser().resolve()
            payload = load_bounded_json(path)
            if (
                payload.get("schema") != GFS_SCHEMA
                or payload.get("provider_id") != expected_provider
                or payload.get("run_cycle") != run_cycle
                or int(payload.get("run_day_offset") or 0) != lead_days
                or (payload.get("blockers") or [])
            ):
                raise ContractError(
                    f"station_explicit_oos_model_evidence_invalid:{model}:{path.name}"
                )
            for row in payload.get("model_rows") or []:
                if not isinstance(row, Mapping):
                    raise ContractError(
                        "station_explicit_oos_model_row_invalid"
                    )
                station = str(
                    row.get("station_id") or ""
                ).strip().upper()
                if station != station_id:
                    continue
                event_date = str(row.get("event_date") or "")
                run = _normalized_run(
                    row.get("model_run_initialization_utc")
                )
                if (
                    str(row.get("target_timezone") or "")
                    != target_timezone
                    or run != _expected_run_cycle(
                        event_date, run_cycle, lead_days=lead_days
                    )
                    or row.get("daily_curve_complete") is not True
                    or int(row.get("sample_count") or 0) <= 0
                ):
                    raise ContractError(
                        f"station_explicit_oos_model_scope_mismatch:{model}:{event_date}"
                    )
                item = {
                    "event_date": event_date,
                    "forecast_tmax_c": float(
                        row["forecast_daily_max_c"]
                    ),
                    "provider_id": expected_provider,
                    "source_class":
                        "official_native_historical_rebuild",
                    "availability_class":
                        f"explicit_{run_cycle.lower()}_d{lead_days}_historical",
                    "model_run_initialization_utc": run,
                    "model_run_lineage_sha256": str(
                        row.get("model_run_lineage_sha256") or ""
                    ),
                    "curve_receipt_sha256": str(
                        row.get("receipt_sha256") or ""
                    ),
                }
                previous = result.get(event_date)
                if previous is not None and previous != item:
                    raise ContractError(
                        f"station_explicit_oos_model_date_conflict:{model}:{event_date}"
                    )
                result[event_date] = item
            inputs.append({"path": str(path), "sha256": _sha256(path)})
        return result, inputs
    if model not in MODEL_PROVIDERS:
        result: dict[str, dict[str, Any]] = {}
        inputs: list[dict[str, str]] = []
        for raw_path in paths:
            path = raw_path.expanduser().resolve()
            payload = load_bounded_json(path)
            source = (
                payload.get("source")
                if isinstance(payload.get("source"), Mapping)
                else {}
            )
            if (
                payload.get("schema") != REGIONAL_SCHEMA
                or str(payload.get("model_id") or "").strip().upper()
                != model
                or (payload.get("blockers") or [])
                or source.get("run_cycle") != run_cycle
                or int(source.get("run_day_offset") or 0) != lead_days
                or source.get("single_run_parameter_explicit") is not True
            ):
                raise ContractError(
                    f"station_explicit_oos_model_evidence_invalid:"
                    f"{model}:{path.name}"
                )
            expected_provider = str(
                payload.get("provider_id") or ""
            ).strip().lower()
            if not expected_provider:
                raise ContractError(
                    "station_explicit_oos_regional_provider_missing"
                )
            for row in payload.get("model_rows") or []:
                if not isinstance(row, Mapping):
                    raise ContractError(
                        "station_explicit_oos_model_row_invalid"
                    )
                station = str(
                    row.get("station_id") or ""
                ).strip().upper()
                if station != station_id:
                    continue
                event_date = str(row.get("event_date") or "")
                run = _normalized_run(
                    row.get("model_run_initialization_utc")
                )
                if (
                    str(row.get("target_timezone") or "")
                    != target_timezone
                    or str(row.get("provider_id") or "").strip().lower()
                    != expected_provider
                    or row.get("model_pool_class") != "regional"
                    or run != _expected_run_cycle(
                        event_date, run_cycle, lead_days=lead_days
                    )
                    or row.get("daily_curve_complete") is not True
                    or int(row.get("sample_count") or 0) <= 0
                    or row.get("future_data_used") is not False
                ):
                    raise ContractError(
                        f"station_explicit_oos_model_scope_mismatch:"
                        f"{model}:{event_date}"
                    )
                item = {
                    "event_date": event_date,
                    "forecast_tmax_c": float(
                        row["forecast_daily_max_c"]
                    ),
                    "provider_id": expected_provider,
                    "source_class": "aggregator_republisher",
                    "model_pool_class": "regional",
                    "availability_class":
                        f"explicit_{run_cycle.lower()}_d{lead_days}_"
                        "historical_closed",
                    "model_run_initialization_utc": run,
                    "curve_receipt_sha256": str(
                        row.get("receipt_sha256") or ""
                    ),
                }
                previous = result.get(event_date)
                if previous is not None and previous != item:
                    raise ContractError(
                        f"station_explicit_oos_model_date_conflict:"
                        f"{model}:{event_date}"
                    )
                result[event_date] = item
            inputs.append({"path": str(path), "sha256": _sha256(path)})
        return result, inputs
    return _load_aggregator_rows(
        paths,
        model_id=model,
        station_id=station_id,
        target_timezone=target_timezone,
        run_cycle=run_cycle,
        lead_days=lead_days,
    )


def _expected_run_cycle(
    event_date: str,
    run_cycle: str,
    *,
    lead_days: int = 2,
) -> str:
    cycle = str(run_cycle or "").strip().upper()
    if cycle not in {"00Z", "06Z", "12Z", "18Z"}:
        raise ContractError("station_explicit_oos_run_cycle_invalid")
    if lead_days not in {1, 2}:
        raise ContractError("station_explicit_oos_lead_days_invalid")
    if cycle == "18Z" and lead_days == 2:
        return _expected_run(event_date)
    run = datetime.combine(
        date.fromisoformat(event_date) - timedelta(days=lead_days),
        time(hour=int(cycle[:2])),
        tzinfo=timezone.utc,
    )
    return run.isoformat(timespec="seconds").replace("+00:00", "Z")


def run(
    *,
    station_id: str,
    registry_path: Path,
    truth_manifest_path: Path | None,
    truth_dataset_path: Path | None = None,
    evidence_paths: Mapping[str, Sequence[Path]],
    as_of_utc: str,
    output_path: Path,
    run_cycle: str = "18Z",
    lead_bucket: str = "d2",
) -> dict[str, Any]:
    station = str(station_id or "").strip().upper()
    models = tuple(sorted(
        str(model).strip().upper()
        for model, paths in evidence_paths.items()
        if paths
    ))
    if len(models) < 2:
        raise ContractError("station_explicit_oos_minimum_two_models_required")
    if lead_bucket not in {"d1", "d2"}:
        raise ContractError("station_explicit_oos_lead_bucket_invalid")
    lead_days = int(lead_bucket[1:])
    timezone_name = _station_timezone(registry_path, station_id=station)
    if (truth_manifest_path is None) == (truth_dataset_path is None):
        raise ContractError(
            "station_explicit_oos_exactly_one_truth_input_required"
        )
    truth_path = (
        truth_dataset_path.expanduser().resolve()
        if truth_dataset_path is not None
        else _truth_dataset(
            truth_manifest_path,  # type: ignore[arg-type]
            station_id=station,
        )
    )
    if not truth_path.is_file():
        raise ContractError("station_explicit_oos_truth_dataset_missing")
    truths, truth_authority = _truth_rows(
        truth_path,
        station=station,
        timezone_name=timezone_name,
    )
    model_rows: dict[str, dict[str, dict[str, Any]]] = {}
    inputs: dict[str, list[dict[str, str]]] = {}
    for model in models:
        rows, model_inputs = _load_model_rows(
            evidence_paths[model],
            model_id=model,
            station_id=station,
            target_timezone=timezone_name,
            run_cycle=run_cycle,
            lead_days=lead_days,
        )
        model_rows[model] = rows
        inputs[model] = model_inputs
    common_dates = sorted(
        set(truths).intersection(*(set(model_rows[model]) for model in models))
    )
    rows_by_model: dict[str, list[dict[str, Any]]] = {
        model: [] for model in models
    }
    exact_join_rows: list[dict[str, Any]] = []
    for event_date in common_dates:
        truth = truths[event_date]
        joined_models = {
            model: model_rows[model][event_date] for model in models
        }
        exact_join_rows.append({
            "event_date": event_date,
            "truth_sample_id": truth["truth_sample_id"],
            "truth_rows_sha256": truth["truth_rows_sha256"],
            "settlement_window": truth["settlement_window"],
            "models": joined_models,
        })
        for model in models:
            rows_by_model[model].append({
                **joined_models[model],
                **truth,
                "residual_c": _calibration_residual_c(
                    forecast_tmax_c=joined_models[model]["forecast_tmax_c"],
                    official_tmax_c=truth["official_tmax_c"],
                ),
            })
    season = (
        _meteorological_season(
            target_date=date.fromisoformat(common_dates[-1]),
            latitude=float(station_coordinates(station)["latitude"]),
        )
        if common_dates else ""
    )
    oos = _build_oos(
        rows_by_model=rows_by_model,
        lead_days=lead_days,
        fetched_at_utc=as_of_utc,
        station_id=station,
        settlement_timezone=timezone_name,
        season_bin=season,
        model_ids=models,
        run_cycle=run_cycle,
        allow_extended_models=True,
    )
    point_oos = _point_oos(
        oos=oos,
        exact_join_rows=exact_join_rows,
        truths=truths,
    )
    winner = oos.get("research_winner")
    probability_passed = bool(
        isinstance(winner, Mapping)
        and winner.get("probability_non_degradation_vs_uniform") is True
    )
    selected_models = {
        str(model).strip().upper()
        for model in (
            winner.get("selected_models") if isinstance(winner, Mapping) else ()
        )
    }
    selected_regional_models = selected_models - set(MODEL_PROVIDERS)
    selected_regional_models -= set(NATIVE_MODEL_PROVIDERS)
    point_passed = point_oos.get("passed_vs_uniform") is True
    formal_quality_gate_passed = (
        truth_authority == "FORMAL_STATION_LANE_TRUTH"
        and probability_passed
        and not selected_regional_models
    )
    quality_blockers = []
    if truth_authority != "FORMAL_STATION_LANE_TRUTH":
        quality_blockers.append("formal_truth_authority_required")
    if not probability_passed:
        quality_blockers.append(
            "probability_oos_non_degradation_gate_failed"
        )
    if selected_regional_models:
        quality_blockers.append(
            "regional_model_formal_artifact_schema_not_promotable"
        )
    quality_diagnostics = []
    if not point_passed:
        quality_diagnostics.append(
            "point_oos_non_degradation_diagnostic_failed"
        )
    identity = {
        "schema": SCHEMA,
        "station_id": station,
        "settlement_timezone": timezone_name,
        "truth_label_authority": truth_authority,
        "lead_bucket": lead_bucket,
        "run_cycle": run_cycle,
        "model_ids": list(models),
        "exact_join_rows": exact_join_rows,
        "exact_join_row_count": len(exact_join_rows),
        "oos": oos,
        "point_oos": point_oos,
        "formal_quality_gate": {
            "passed": formal_quality_gate_passed,
            "blockers": quality_blockers,
            "diagnostics": quality_diagnostics,
            "activation_written": False,
        },
        "inputs": inputs,
        "boundary": {
            "production_no_live_only": True,
            "formal_promotion_written": False,
            "activation_written": False,
            "writes_production_database": False,
            "calls_order_gateway": False,
            "places_orders": False,
            "live_submit_authorized": False,
        },
    }
    result = {**identity, "content_sha256": content_sha256(identity)}
    _atomic_write(output_path.resolve(), result)
    return result


def _evidence_args(values: Sequence[str]) -> dict[str, list[Path]]:
    result: dict[str, list[Path]] = {}
    for value in values:
        model, separator, raw_path = str(value).partition("=")
        if not separator or not raw_path:
            raise ContractError("station_explicit_oos_model_evidence_argument_invalid")
        result.setdefault(model.strip().upper(), []).append(Path(raw_path))
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--station-id", required=True)
    parser.add_argument("--registry", type=Path, required=True)
    truth = parser.add_mutually_exclusive_group(required=True)
    truth.add_argument("--truth-manifest", type=Path)
    truth.add_argument("--truth-dataset", type=Path)
    parser.add_argument(
        "--model-evidence",
        dest="model_artifacts",
        action="append",
        required=True,
        help="MODEL=/absolute/or/relative/evidence.json; repeat as needed",
    )
    parser.add_argument("--as-of-utc", required=True)
    parser.add_argument(
        "--run-cycle",
        choices=("00Z", "06Z", "12Z", "18Z"),
        default="18Z",
    )
    parser.add_argument(
        "--lead-bucket",
        choices=("d1", "d2"),
        default="d2",
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    result = run(
        station_id=args.station_id,
        registry_path=args.registry,
        truth_manifest_path=args.truth_manifest,
        truth_dataset_path=args.truth_dataset,
        evidence_paths=_evidence_args(args.model_artifacts),
        as_of_utc=args.as_of_utc,
        output_path=args.output,
        run_cycle=args.run_cycle,
        lead_bucket=args.lead_bucket,
    )
    winner = result.get("oos", {}).get("research_winner") or {}
    print(json.dumps({
        "schema": result["schema"],
        "station_id": result["station_id"],
        "exact_join_row_count": result["exact_join_row_count"],
        "selected_models": winner.get("selected_models") or [],
        "selected_weights": winner.get("selected_weights") or {},
        "probability_non_degradation_vs_uniform":
            winner.get("probability_non_degradation_vs_uniform"),
        "point_non_degradation_vs_uniform":
            result["point_oos"]["passed_vs_uniform"],
        "formal_quality_gate_passed":
            result["formal_quality_gate"]["passed"],
        "output": str(args.output.resolve()),
        "writes_production_database": False,
        "calls_order_gateway": False,
        "live_submit_authorized": False,
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
