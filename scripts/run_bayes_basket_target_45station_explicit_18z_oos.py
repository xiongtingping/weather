#!/usr/bin/env python3
"""Run registry-bound 45-station OOS for explicit 18Z/d2 model curves.

The runner accepts official GFS historical rebuild evidence and Open-Meteo
ICON single-run evidence with an explicit 18Z initialization.  It performs an
exact station/date/timezone/run join to retained truth, selects only
multi-model candidates, and writes no-live review artifacts only.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sys
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import ContractError, content_sha256
from bayes_basket_target.resource_bounds import load_bounded_json
from bayes_basket_target.station_registry import station_coordinates
from scripts.build_rkpk_open_meteo_five_model_historical_oos import (
    _atomic_write,
    _build_oos,
    _meteorological_season,
)


SCHEMA = "bayes_basket_target_45station_explicit_18z_d2_oos_v1"
GFS_SCHEMA = "bayes_basket_target_historical_native_rebuild_evidence_v1"
ICON_SCHEMA = "bayes_basket_target_historical_aggregator_rebuild_evidence_v1"
TRUTH_SCHEMA = "bayes_basket_station_calibration_dataset_v1"
GFS_PROVIDER = "gfs_0p25_official"
ICON_PROVIDER = "icon_global_open_meteo"
MODELS = ("GFS", "ICON")


def _calibration_residual_c(*, forecast_tmax_c: float, official_tmax_c: float) -> float:
    """Return the correction applied by the formal P0 contract.

    Calibration artifacts are consumed as ``calibrated = raw + bias``.
    Residual training rows must therefore use truth minus forecast.
    """

    return float(official_tmax_c) - float(forecast_tmax_c)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _registry_timezones(path: Path) -> dict[str, str]:
    payload = load_bounded_json(path.resolve())
    rows = payload.get("rows") if isinstance(payload, Mapping) else None
    if not isinstance(rows, list):
        raise ContractError("explicit_oos_registry_rows_missing")
    values: dict[str, set[str]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        station = str(row.get("station_id") or "").strip().upper()
        timezone_name = str(row.get("settlement_timezone") or "").strip()
        if station and timezone_name:
            values.setdefault(station, set()).add(timezone_name)
    result = {}
    for station, timezones in sorted(values.items()):
        if len(timezones) != 1:
            raise ContractError(f"explicit_oos_registry_timezone_invalid:{station}")
        result[station] = next(iter(timezones))
    if len(result) != 45:
        raise ContractError("explicit_oos_registry_station_count_invalid")
    return result


def _truth_manifest(path: Path) -> dict[str, Path]:
    payload = load_bounded_json(path.resolve())
    rows = payload.get("truth_results") if isinstance(payload, Mapping) else None
    if not isinstance(rows, list):
        raise ContractError("explicit_oos_truth_manifest_missing")
    result: dict[str, Path] = {}
    for row in rows:
        if not isinstance(row, Mapping) or row.get("state") != "TRUTH_READY":
            continue
        station = str(row.get("station_id") or "").strip().upper()
        dataset = Path(str(row.get("dataset") or "")).expanduser().resolve()
        if not station or not dataset.is_file():
            raise ContractError("explicit_oos_truth_dataset_missing")
        result[station] = dataset
    if len(result) != 45:
        raise ContractError("explicit_oos_truth_station_count_invalid")
    return result


def _truth_rows(path: Path, *, station: str, timezone_name: str) -> tuple[dict[str, dict[str, Any]], str]:
    payload = load_bounded_json(path)
    if (
        payload.get("schema") != TRUTH_SCHEMA
        or payload.get("station_id") != station
        or payload.get("settlement_timezone") != timezone_name
        or payload.get("source_lane") != "AWC"
        or payload.get("source_role") != "AWC_HISTORICAL_REPLAY_INGRESS"
        or payload.get("authority_scope") != "PRODUCTION_NO_LIVE_ONLY"
    ):
        raise ContractError(f"explicit_oos_truth_scope_invalid:{station}")
    supplied_content = str(payload.get("content_sha256") or "").lower()
    if supplied_content != content_sha256({
        key: value for key, value in payload.items() if key != "content_sha256"
    }):
        raise ContractError(f"explicit_oos_truth_content_hash_invalid:{station}")
    checks = payload.get("checks")
    if (
        not isinstance(checks, Mapping)
        or checks.get("input_evidence_content_hashes_verified") is not True
        or checks.get("settlement_timezone_exact_match") is not True
        or checks.get("reference_only_excluded") is not True
    ):
        raise ContractError(f"explicit_oos_truth_checks_invalid:{station}")
    label_authority = str(payload.get("label_authority") or "")
    if label_authority == "TEMPORARY_AWC_CALIBRATION_LABEL":
        raise ContractError(f"explicit_oos_temporary_awc_retired:{station}")
    if label_authority != "FORMAL_STATION_LANE_TRUTH":
        raise ContractError(f"explicit_oos_truth_authority_invalid:{station}")
    rows: dict[str, dict[str, Any]] = {}
    for sample in payload.get("samples") or []:
        if not isinstance(sample, Mapping):
            continue
        event_date = str(sample.get("event_date") or "")
        truth = sample.get("truth")
        window = sample.get("settlement_window")
        if not event_date or not isinstance(truth, Mapping) or not isinstance(window, Mapping):
            raise ContractError(f"explicit_oos_truth_sample_invalid:{station}")
        observations = truth.get("observations")
        if (
            sample.get("settlement_timezone") != timezone_name
            or truth.get("source_lane") != "AWC"
            or truth.get("source_role") != "AWC_HISTORICAL_REPLAY_INGRESS"
            or truth.get("authority_scope") != "PRODUCTION_NO_LIVE_ONLY"
            or not isinstance(observations, list)
            or not observations
        ):
            raise ContractError(f"explicit_oos_truth_sample_lineage_invalid:{station}")
        if any(
            not isinstance(item, Mapping)
            or item.get("source_lane") != "AWC"
            or item.get("source_role") != "AWC_HISTORICAL_REPLAY_INGRESS"
            or item.get("authority_scope") != "PRODUCTION_NO_LIVE_ONLY"
            or item.get("settlement_timezone") != timezone_name
            or item.get("reference_only") is not False
            or item.get("evidence_role") != "FORMAL"
            for item in observations
        ):
            raise ContractError(
                f"explicit_oos_truth_observation_lineage_invalid:{station}"
            )
        sample_identity = {
            key: value for key, value in sample.items() if key != "sample_id"
        }
        if str(sample.get("sample_id") or "") != (
            "truth-" + content_sha256(sample_identity)[:32]
        ):
            raise ContractError(f"explicit_oos_truth_sample_hash_invalid:{station}")
        rows[event_date] = {
            "official_tmax_c": float(truth["tmax_c"]),
            "truth_sample_id": str(sample.get("sample_id") or ""),
            "truth_rows_sha256": content_sha256(observations),
            "settlement_window": dict(window),
            "truth_source_lane": str(truth.get("source_lane") or "").upper(),
        }
    return rows, label_authority


def _expected_run(event_date: str) -> str:
    run = datetime.combine(
        date.fromisoformat(event_date) - timedelta(days=2),
        time(hour=18),
        tzinfo=timezone.utc,
    )
    return run.isoformat(timespec="seconds").replace("+00:00", "Z")


def _normalized_run(value: Any) -> str:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError("explicit_oos_run_timezone_missing")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _load_model_rows(
    paths: list[Path],
    *,
    model: str,
    registry_timezones: Mapping[str, str],
) -> tuple[dict[tuple[str, str], dict[str, Any]], list[dict[str, str]]]:
    expected_schema = GFS_SCHEMA if model == "GFS" else ICON_SCHEMA
    expected_provider = GFS_PROVIDER if model == "GFS" else ICON_PROVIDER
    result: dict[tuple[str, str], dict[str, Any]] = {}
    inputs: list[dict[str, str]] = []
    for raw_path in paths:
        path = raw_path.expanduser().resolve()
        payload = load_bounded_json(path)
        if (
            payload.get("schema") != expected_schema
            or payload.get("provider_id") != expected_provider
            or (payload.get("blockers") or [])
        ):
            raise ContractError(f"explicit_oos_model_evidence_invalid:{model}:{path.name}")
        source = payload.get("source") if isinstance(payload.get("source"), Mapping) else {}
        if model == "GFS" and (
            payload.get("run_cycle") != "18Z"
            or int(payload.get("run_day_offset") or 0) != 2
        ):
            raise ContractError("explicit_oos_gfs_run_scope_invalid")
        if model == "ICON" and (
            source.get("run_cycle") != "18Z"
            or int(source.get("run_day_offset") or 0) != 2
            or source.get("single_run_parameter_explicit") is not True
        ):
            raise ContractError("explicit_oos_icon_run_scope_invalid")
        rows = payload.get("model_rows")
        if not isinstance(rows, list):
            raise ContractError(f"explicit_oos_model_rows_missing:{model}")
        for row in rows:
            if not isinstance(row, Mapping):
                raise ContractError(f"explicit_oos_model_row_invalid:{model}")
            station = str(row.get("station_id") or "").strip().upper()
            event_date = str(row.get("event_date") or "")
            timezone_name = str(row.get("target_timezone") or "")
            run = _normalized_run(row.get("model_run_initialization_utc"))
            if (
                station not in registry_timezones
                or timezone_name != registry_timezones[station]
                or run != _expected_run(event_date)
                or row.get("daily_curve_complete") is not True
                or int(row.get("sample_count") or 0) <= 0
            ):
                raise ContractError(
                    f"explicit_oos_model_scope_mismatch:{model}:{station}:{event_date}"
                )
            item = {
                "event_date": event_date,
                "forecast_tmax_c": float(row.get("forecast_daily_max_c")),
                "provider_id": expected_provider,
                "source_class": (
                    "official_native_historical_rebuild"
                    if model == "GFS"
                    else "aggregator_republisher"
                ),
                "availability_class": "explicit_18z_d2_historical",
                "model_run_initialization_utc": run,
                "model_run_lineage_sha256": str(
                    row.get("model_run_lineage_sha256")
                    or row.get("receipt_sha256")
                    or ""
                ),
                "curve_receipt_sha256": str(row.get("receipt_sha256") or ""),
            }
            key = (station, event_date)
            if key in result and result[key] != item:
                raise ContractError(f"explicit_oos_model_date_conflict:{model}:{station}:{event_date}")
            result[key] = item
        inputs.append({"path": str(path), "sha256": _sha256(path)})
    return result, inputs


def run(args: argparse.Namespace) -> dict[str, Any]:
    registry = _registry_timezones(args.registry)
    truth_paths = _truth_manifest(args.truth_manifest)
    gfs, gfs_inputs = _load_model_rows(
        args.gfs_evidence, model="GFS", registry_timezones=registry
    )
    icon, icon_inputs = _load_model_rows(
        args.icon_evidence, model="ICON", registry_timezones=registry
    )
    output_root = args.output_root.expanduser().resolve()
    station_results = []
    for station in sorted(registry):
        truths, authority = _truth_rows(
            truth_paths[station],
            station=station,
            timezone_name=registry[station],
        )
        common_dates = sorted(
            set(truths)
            & {event_date for candidate, event_date in gfs if candidate == station}
            & {event_date for candidate, event_date in icon if candidate == station}
        )
        rows_by_model: dict[str, list[dict[str, Any]]] = {model: [] for model in MODELS}
        exact_join = []
        for event_date in common_dates:
            truth = truths[event_date]
            models = {"GFS": gfs[(station, event_date)], "ICON": icon[(station, event_date)]}
            exact_join.append({
                "event_date": event_date,
                "truth_sample_id": truth["truth_sample_id"],
                "truth_rows_sha256": truth["truth_rows_sha256"],
                "settlement_window": truth["settlement_window"],
                "models": models,
            })
            for model in MODELS:
                rows_by_model[model].append({
                    **models[model],
                    **truth,
                    "residual_c": _calibration_residual_c(
                        forecast_tmax_c=models[model]["forecast_tmax_c"],
                        official_tmax_c=truth["official_tmax_c"],
                    ),
                })
        latitude = float(station_coordinates(station)["latitude"])
        season = _meteorological_season(
            target_date=date.fromisoformat(common_dates[-1]),
            latitude=latitude,
        ) if common_dates else ""
        oos = _build_oos(
            rows_by_model=rows_by_model,
            lead_days=2,
            fetched_at_utc=args.as_of_utc,
            station_id=station,
            settlement_timezone=registry[station],
            season_bin=season,
            model_ids=MODELS,
            run_cycle="18Z",
        )
        payload = {
            "schema": SCHEMA,
            "station_id": station,
            "settlement_timezone": registry[station],
            "truth_label_authority": authority,
            "lead_bucket": "d2",
            "run_cycle": "18Z",
            "model_ids": list(MODELS),
            "exact_join_rows": exact_join,
            "exact_join_row_count": len(exact_join),
            "oos": oos,
            "boundary": {
                "production_no_live_only": True,
                "writes_production_database": False,
                "calls_order_gateway": False,
                "places_orders": False,
                "live_submit_authorized": False,
            },
        }
        payload["content_sha256"] = content_sha256(payload)
        output = output_root / station / "explicit_18z_d2_oos.json"
        _atomic_write(output, payload)
        station_results.append({
            "station_id": station,
            "truth_label_authority": authority,
            "common_sample_n": len(exact_join),
            "state": oos["state"],
            "selected_models": list((oos.get("research_winner") or {}).get("selected_models") or []),
            "probability_non_degradation_vs_uniform": bool(
                (oos.get("research_winner") or {}).get(
                    "probability_non_degradation_vs_uniform"
                )
            ),
            "p0_no_live_binding_review": (
                "ELIGIBLE"
                if (
                    authority == "FORMAL_STATION_LANE_TRUTH"
                    and bool(
                        (oos.get("research_winner") or {}).get(
                            "probability_non_degradation_vs_uniform"
                        )
                    )
                )
                else "BLOCKED_PROBABILITY_OOS_GATE"
            ),
            "output": str(output),
        })
    manifest = {
        "schema": SCHEMA,
        "as_of_utc": args.as_of_utc,
        "station_count": len(station_results),
        "run_cycle": "18Z",
        "lead_bucket": "d2",
        "model_ids": list(MODELS),
        "inputs": {
            "gfs": gfs_inputs,
            "icon": icon_inputs,
            "truth_manifest": {
                "path": str(args.truth_manifest.resolve()),
                "sha256": _sha256(args.truth_manifest.resolve()),
            },
        },
        "checks": {
            "exact_45_station_scope": len(station_results) == 45,
            "all_stations_minimum_16_samples": all(
                int(row["common_sample_n"]) >= 16 for row in station_results
            ),
            "all_winners_at_least_two_models": all(
                len(row["selected_models"]) >= 2 for row in station_results
            ),
            "single_model_winner_count": sum(
                len(row["selected_models"]) == 1 for row in station_results
            ),
            "probability_oos_gate_passed_station_count": sum(
                row["probability_non_degradation_vs_uniform"]
                for row in station_results
            ),
            "p0_no_live_binding_review_eligible_station_count": sum(
                row["p0_no_live_binding_review"] == "ELIGIBLE"
                for row in station_results
            ),
            "p0_activation_written": False,
            "live_submit_authorized": False,
            "calls_order_gateway": False,
        },
        "station_results": station_results,
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
    manifest["content_sha256"] = content_sha256(manifest)
    _atomic_write(output_root / "manifest.json", manifest)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--truth-manifest", type=Path, required=True)
    parser.add_argument("--gfs-evidence", type=Path, action="append", required=True)
    parser.add_argument("--icon-evidence", type=Path, action="append", required=True)
    parser.add_argument("--as-of-utc", required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    args = parser.parse_args()
    result = run(args)
    print(json.dumps({
        "schema": result["schema"],
        "station_count": result["station_count"],
        "minimum_common_sample_n": min(
            row["common_sample_n"] for row in result["station_results"]
        ),
        "single_model_winner_count": result["checks"]["single_model_winner_count"],
        "output": str((args.output_root / "manifest.json").resolve()),
        "live_submit_authorized": False,
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
