"""Rolling-origin NWP P0 point predictions for conditional TAF calibration."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .calibration import validate_formal_ensemble_artifact
from .calibration_evidence import (
    ENSEMBLE_MAX_WEIGHT_DELTA,
    _fit_rolling_probability_ensemble,
)
from .contracts import (
    ContractError,
    FORMAL_ENSEMBLE_METHOD_VERSION,
    ModelEnsembleArtifact,
    content_sha256,
)
from .model_sources import model_id_for_provider
from .taf_exact_join import NWP_CROSSFIT_SCHEMA
from .taf_oos_contract import assert_historical_taf_oos
from .settlement_time import settlement_day_window
from .station_season import station_season_bin


MIN_TRAIN_DAYS = 3
SUPPORTED_LEAD_BUCKETS = frozenset({"d1", "d2", "d2_fallback"})
SUPPORTED_RUN_CYCLES = frozenset({"00Z", "06Z", "12Z", "18Z"})


def _season_bin(station_id: str, event_date: str) -> str:
    """The station-local season the P0 lineage stamps on its envelopes.

    This must be the P0 vocabulary and no other, because the crossfit's season
    travels into the TAF revision artifact's scope, and resident P0-M selects
    an artifact by exact equality against the live root's lineage.  The one
    artifact ever activated was fit while P0 still wrote ``summer``; when P0
    moved to ``JJA`` on 2026-07-31 the join stopped matching and the stage
    went silent.  Charter 62 (2026-08-25) moved the lineage again, from the
    calendar trigrams to the station-local labels -- so this delegates to the
    same function P0 compiles with rather than keeping a third copy that can
    drift.  The English hemisphere-flipped vocabulary still exists for policy
    scope -- that is configured, not joined against lineage, so the two
    coexist -- but anything on the exact-join path has to speak lineage.
    """

    return station_season_bin(station_id, event_date)


def _file_sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verified_model_rows(
    evidence_paths: Sequence[Path],
    *,
    raw_root: Path,
    station_id: str,
    run_cycle: str,
    models: tuple[str, ...],
) -> dict[tuple[str, str], dict[str, Any]]:
    import json

    result: dict[tuple[str, str], dict[str, Any]] = {}
    for path in evidence_paths:
        with path.open("r", encoding="utf-8") as handle:
            packet = json.load(handle)
        rows = packet.get("model_rows")
        if not isinstance(rows, list):
            raise ContractError("taf_crossfit_model_rows_missing")
        for row in rows:
            if not isinstance(row, Mapping):
                raise ContractError("taf_crossfit_model_row_invalid")
            if str(row.get("station_id") or "").upper() != station_id:
                continue
            provider = str(row.get("provider_id") or "")
            try:
                model = model_id_for_provider(provider)
            except ContractError:
                continue
            if model not in models:
                continue
            if row.get("daily_curve_complete") is not True:
                raise ContractError("taf_crossfit_daily_curve_incomplete")
            if str(row.get("target_timezone") or "") != "Asia/Seoul":
                raise ContractError("taf_crossfit_settlement_timezone_mismatch")
            initialization_text = str(
                row.get("model_run_initialization_utc") or ""
            )
            try:
                initialization = datetime.fromisoformat(
                    initialization_text.replace("Z", "+00:00")
                )
            except ValueError as exc:
                raise ContractError(
                    "taf_crossfit_model_run_initialization_invalid"
                ) from exc
            if (
                initialization.tzinfo is None
                or initialization.utcoffset() is None
                or initialization.astimezone(timezone.utc).hour
                != int(run_cycle[:2])
            ):
                raise ContractError(
                    "taf_crossfit_model_run_cycle_mismatch:"
                    f"{provider}:{row.get('event_date')}:{run_cycle}"
                )
            shas = row.get("component_raw_message_sha256")
            if not isinstance(shas, list) or not shas:
                raise ContractError("taf_crossfit_component_lineage_missing")
            provider_root = raw_root / provider / "raw"
            for value in shas:
                sha = str(value or "").lower()
                candidates = tuple(
                    provider_root / f"{sha}{suffix}"
                    for suffix in (".grib2", ".json", ".bin", "")
                )
                matches = [
                    candidate
                    for candidate in candidates
                    if candidate.is_file() and _file_sha(candidate) == sha
                ]
                if len(matches) != 1:
                    raise ContractError(
                        "taf_crossfit_raw_object_verification_failed:"
                        f"{provider}:{row.get('event_date')}:{sha}"
                    )
            key = (str(row["event_date"]), model)
            normalized = {
                **dict(row),
                "_model_id": model,
                "_evidence_path": str(path.resolve()),
            }
            previous = result.get(key)
            if previous is not None and previous["model_run_lineage_sha256"] != normalized["model_run_lineage_sha256"]:
                raise ContractError("taf_crossfit_duplicate_model_day_conflict")
            result[key] = normalized
    return result


def build_nwp_crossfit_predictions(
    *,
    taf_dataset: Mapping[str, Any],
    evidence_paths: Sequence[Path],
    raw_root: Path,
    lead_bucket: str,
    run_cycle: str,
    ensemble_artifact: Mapping[str, Any],
    station_id: str = "RKPK",
) -> dict[str, Any]:
    normalized_lead = str(lead_bucket).strip()
    normalized_cycle = str(run_cycle).strip().upper()
    if normalized_lead not in SUPPORTED_LEAD_BUCKETS:
        raise ContractError("taf_crossfit_lead_bucket_invalid")
    if normalized_cycle not in SUPPORTED_RUN_CYCLES:
        raise ContractError("taf_crossfit_run_cycle_invalid")
    if (
        content_sha256({
            str(key): value
            for key, value in ensemble_artifact.items()
            if key != "content_sha256"
        })
        != str(ensemble_artifact.get("content_sha256") or "").lower()
    ):
        raise ContractError("taf_crossfit_ensemble_content_sha256_mismatch")
    ensemble = ModelEnsembleArtifact.from_mapping(ensemble_artifact)
    validate_formal_ensemble_artifact(ensemble)
    if (
        ensemble.station_id != station_id
        or ensemble.lead_bucket != normalized_lead
        or ensemble.run_cycle != normalized_cycle
        or ensemble.settlement_definition != "official_daily_tmax_c"
        or ensemble.weight_method != FORMAL_ENSEMBLE_METHOD_VERSION
    ):
        raise ContractError("taf_crossfit_ensemble_scope_mismatch")
    models = tuple(ensemble.candidate_models)
    ensemble.resolve_weights(models)
    samples = taf_dataset.get("samples")
    if not isinstance(samples, list) or len(samples) > 366:
        raise ContractError("taf_crossfit_taf_samples_invalid")
    model_rows = _verified_model_rows(
        evidence_paths,
        raw_root=raw_root,
        station_id=station_id,
        run_cycle=normalized_cycle,
        models=models,
    )
    ordered = sorted(
        (row for row in samples if str(row.get("station_id") or "").upper() == station_id),
        key=lambda row: str(row.get("event_date") or ""),
    )
    history_dates: list[str] = []
    forecasts_by_model_date: dict[tuple[str, str], float] = {}
    residuals_by_model_date: dict[tuple[str, str], float] = {}
    truths_by_date: dict[str, float] = {}
    predictions: list[dict[str, Any]] = []
    previous_weights: Mapping[str, float] | None = None
    for sample in ordered:
        event_date = str(sample["event_date"])
        pair = {model: model_rows.get((event_date, model)) for model in models}
        window = sample.get("settlement_window")
        # Older immutable historical TAF packets carry the settlement
        # timezone but not a duplicated window object.  Derive the same
        # station-local day-end from that explicit timezone; never infer it
        # from the host clock or from a forward receipt.
        if not isinstance(window, Mapping):
            timezone_name = str(sample.get("settlement_timezone") or "").strip()
            if not timezone_name:
                raise ContractError("taf_crossfit_settlement_window_missing")
            try:
                window = settlement_day_window(event_date, timezone_name)
            except (TypeError, ValueError, KeyError) as exc:
                raise ContractError("taf_crossfit_settlement_window_missing") from exc
        try:
            closed_at = datetime.fromisoformat(
                str(
                    window.get("end_utc")
                    or settlement_day_window(
                        event_date, str(sample.get("settlement_timezone") or "")
                    )["target_day_end_utc"]
                ).replace("Z", "+00:00")
            )
        except (TypeError, ValueError) as exc:
            raise ContractError("taf_crossfit_truth_close_invalid") from exc
        if closed_at.tzinfo is None or closed_at.utcoffset() is None:
            raise ContractError("taf_crossfit_truth_close_timezone_required")
        assert_historical_taf_oos(
            sample,
            event_date=event_date,
            truth_closed_at=closed_at,
            transport_values=tuple(
                str(pair[model].get("provider_id") or "")
                for model in models
                if pair.get(model) is not None
            ),
            name="taf_crossfit",
        )
        if str((sample.get("taf") or {}).get("source_class") or "") == "official_forward_native_receipt":
            raise ContractError("taf_crossfit_forward_taf_not_oos")
        if any(pair[model] is None for model in models):
            continue
        truth = float(sample["truth"]["tmax_c"])
        raw = {
            model: float(pair[model]["forecast_daily_max_c"])  # type: ignore[index]
            for model in models
        }
        if len(history_dates) >= MIN_TRAIN_DAYS:
            selected_weights, parameters, fit_metrics = (
                _fit_rolling_probability_ensemble(
                    models=models,
                    history_dates=history_dates,
                    forecasts_by_model_date=forecasts_by_model_date,
                    residuals_by_model_date=residuals_by_model_date,
                    truths_by_date=truths_by_date,
                    previous_weights=previous_weights,
                    max_weight_delta=ENSEMBLE_MAX_WEIGHT_DELTA,
                )
            )
            previous_weights = selected_weights
            biases = {
                model: parameters[model][0]
                for model in models
            }
            fold_identity = {
                "method_version": FORMAL_ENSEMBLE_METHOD_VERSION,
                "ensemble_artifact_content_sha256": ensemble.content_sha256,
                "station_id": station_id,
                "event_date": event_date,
                "train_dates": history_dates,
                "biases_c": biases,
                "residual_scales_c": {
                    model: parameters[model][1]
                    for model in models
                },
                "selected_weights": selected_weights,
                "fit_metrics": fit_metrics,
            }
            model_lineage = []
            latest_publication = ""
            for model in models:
                item = pair[model]
                assert item is not None
                latest_publication = max(
                    latest_publication,
                    str(item["published_at_utc"]),
                )
                model_lineage.append({
                    "model_id": model,
                    "model_run_id": item["model_run_lineage_sha256"],
                    "published_at_utc": item["published_at_utc"],
                    "available_at_utc": None,
                    "availability_class": "official_publication_schedule_historical_replay",
                    "source_object_sha256": item["model_run_lineage_sha256"],
                    "raw_object_ref": (
                        f"{item['_evidence_path']}#{station_id}/{event_date}/{model}"
                    ),
                    "raw_object_verified": True,
                })
            predictions.append({
                "station_id": station_id,
                "event_date": event_date,
                "decision_at_utc": latest_publication,
                "settlement_timezone": sample["settlement_timezone"],
                "settlement_definition": "official_daily_tmax_c",
                "season_bin": _season_bin(station_id, event_date),
                "lead_bucket": normalized_lead,
                "run_cycle": normalized_cycle,
                "prediction_oos": True,
                "selection_evidence_sha256": content_sha256(fold_identity),
                "selected_models": list(models),
                "selected_weights": selected_weights,
                "combined_tmax_c": sum(
                    selected_weights[model] * (raw[model] + biases[model])
                    for model in models
                ),
                "model_rows": model_lineage,
                "train_start_date": history_dates[0],
                "train_end_date": history_dates[-1],
                "train_n": len(history_dates),
            })
        for model in models:
            forecasts_by_model_date[(model, event_date)] = raw[model]
            residuals_by_model_date[(model, event_date)] = truth - raw[model]
        truths_by_date[event_date] = truth
        history_dates.append(event_date)
    identity = {
        "schema": NWP_CROSSFIT_SCHEMA,
        "method_version": FORMAL_ENSEMBLE_METHOD_VERSION,
        "ensemble_artifact_content_sha256": ensemble.content_sha256,
        "station_id": station_id,
        "lead_bucket": normalized_lead,
        "run_cycle": normalized_cycle,
        "minimum_train_days": MIN_TRAIN_DAYS,
        "minimum_selected_models": 2,
        "prediction_count": len(predictions),
        "excluded_burn_in_dates": [
            row["event_date"]
            for row in ordered[:MIN_TRAIN_DAYS]
        ],
        "predictions": predictions,
        "boundary": {
            "historical_replay_only": True,
            "native_arrival_claimed": False,
            "writes_production_db": False,
            "calls_order_gateway": False,
        },
    }
    artifact_sha = content_sha256(identity)
    finalized = [
        {**row, "crossfit_prediction_artifact_sha256": artifact_sha}
        for row in predictions
    ]
    return {
        **identity,
        "predictions": finalized,
        "crossfit_prediction_artifact_sha256": artifact_sha,
    }
