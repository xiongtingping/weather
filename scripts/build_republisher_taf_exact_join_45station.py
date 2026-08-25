#!/usr/bin/env python3
"""Build the independent historical Open-Meteo -> AWC TAF exact-join lane.

The native TAF/P0-M join intentionally does not accept republisher rows.  This
bounded bridge consumes already retained deterministic AIFS and ICON Global
Open-Meteo curves, joins them chronologically to closed AWC truth and the
official historical TAF table, and writes candidate-only evidence.  It does
not write SQLite, native-arrival indexes, P1, production P0-M, or orders.

The replay anchor is the model's date-addressed historical run initialization;
it is never presented as a native publication/arrival timestamp.  Thus a
successful result is useful for no-live calibration/fallback only and remains
explicitly outside native/formal/live authority.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any, Mapping
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import ContractError, content_sha256  # noqa: E402
from bayes_basket_target.resource_bounds import (  # noqa: E402
    MAX_JSON_INPUT_BYTES,
    load_bounded_json,
    write_bounded_json_if_changed,
)
from bayes_basket_target.settlement_time import settlement_day_window  # noqa: E402
from bayes_basket_target.station_season import station_season_bin  # noqa: E402
from bayes_basket_target.taf_exact_join import build_taf_exact_join  # noqa: E402


SCHEMA = "bayes_basket_republisher_taf_exact_join_45station_v1"
CROSSFIT_SCHEMA = "bayes_basket_nwp_crossfit_predictions_v1"
RECEIPT_CLASS = "HISTORICAL_ASOF_REPUBLISHER_RECEIPT"
TEMPORAL_SCOPE = "CLOSED_HISTORICAL_AWC_TRUTH"
SOURCE_ROLE = "aggregator_republisher"
MIN_TRAIN_DAYS = 3
MIN_TAF_SAMPLE_N = 24
MAX_STATIONS = 45
MODEL_IDS = ("AIFS", "ICON")

DEFAULT_DB = ROOT / "data" / "bayes_basket_target_ssot.db"
DEFAULT_OUTPUT = ROOT / "output" / "audits" / (
    "republisher_taf_exact_join_45station_20260803.json"
)
DEFAULT_AIFS = (
    ROOT / "output" / "audits" / "open_meteo_aifs_45station_20260701_20260714.json",
    ROOT / "output" / "audits" / "open_meteo_aifs_45station_20260715_20260728.json",
    ROOT / "output" / "audits" / "open_meteo_aifs_45station_20260729_20260731.json",
)
DEFAULT_ICON = (
    ROOT / "output" / "audits" / "open_meteo_icon_45station_20260701_20260714.json",
    ROOT / "output" / "audits" / "open_meteo_icon_45station_20260715_20260728_direct.json",
    ROOT / "output" / "audits" / "open_meteo_icon_45station_20260729_20260731.json",
)


def _sha(value: Any, name: str) -> str:
    text = str(value or "").strip().lower()
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise ContractError(f"republisher_taf_{name}_sha_invalid")
    return text


def _utc(value: Any, name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ContractError(f"republisher_taf_{name}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"republisher_taf_{name}_timezone_required")
    return parsed.astimezone(timezone.utc)


def _text(value: Any, name: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise ContractError(f"republisher_taf_{name}_required")
    return result


def _load(path: Path) -> dict[str, Any]:
    requested = path.expanduser()
    if requested.is_symlink():
        raise ContractError("republisher_taf_input_symlink_forbidden")
    payload = load_bounded_json(requested.resolve(strict=True), limit=MAX_JSON_INPUT_BYTES)
    if not isinstance(payload, Mapping):
        raise ContractError("republisher_taf_input_mapping_required")
    return dict(payload)


def _capture_rows(paths: tuple[Path, ...], expected_model: str) -> tuple[str, dict[tuple[str, str], dict[str, Any]], list[dict[str, Any]]]:
    rows: dict[tuple[str, str], dict[str, Any]] = {}
    sources: list[dict[str, Any]] = []
    for path in paths:
        capture = _load(path)
        if capture.get("schema") != "bayes_basket_target_historical_aggregator_rebuild_evidence_v1":
            raise ContractError("republisher_taf_capture_schema_invalid")
        supplied = _sha(capture.get("receipt_sha256"), "capture_receipt")
        identity = {key: value for key, value in capture.items() if key != "receipt_sha256"}
        if content_sha256(identity) != supplied:
            raise ContractError("republisher_taf_capture_hash_mismatch")
        model = str(capture.get("model_id") or "").strip().upper()
        if model != expected_model:
            raise ContractError("republisher_taf_capture_model_scope_invalid")
        source = capture.get("source")
        checks = capture.get("checks")
        if not isinstance(source, Mapping) or source.get("source_class") != SOURCE_ROLE:
            raise ContractError("republisher_taf_capture_republisher_lineage_required")
        if not isinstance(checks, Mapping) or checks.get("native_arrival_index_eligible") is not False:
            raise ContractError("republisher_taf_capture_native_boundary_invalid")
        curves = (capture.get("curves") or {}).get("eligible_model_rows")
        if not isinstance(curves, list):
            raise ContractError("republisher_taf_capture_curves_missing")
        for raw in curves:
            if not isinstance(raw, Mapping):
                raise ContractError("republisher_taf_curve_invalid")
            station = _text(raw.get("station_id"), "curve_station").upper()
            day = _text(raw.get("event_date"), "curve_event_date")
            key = (station, day)
            if key in rows:
                raise ContractError("republisher_taf_curve_duplicate")
            if raw.get("complete") is not True or raw.get("open_meteo") is not True:
                raise ContractError("republisher_taf_curve_incomplete")
            raw_sha = _sha(raw.get("raw_message_sha256"), "curve_raw")
            raw_ref = Path(_text(raw.get("raw_object_ref"), "curve_raw_ref")).expanduser().resolve()
            if not raw_ref.is_file() or raw_ref.is_symlink():
                raise ContractError("republisher_taf_curve_raw_missing")
            digest = hashlib.sha256(raw_ref.read_bytes()).hexdigest()
            if digest != raw_sha:
                raise ContractError("republisher_taf_curve_raw_hash_mismatch")
            init = _utc(raw.get("model_run_initialization_utc"), "curve_run_init")
            rows[key] = {
                "station_id": station,
                "event_date": day,
                "forecast_daily_max_c": float(raw["forecast_daily_max_c"]),
                "model_run_initialization_utc": init.isoformat(timespec="seconds").replace("+00:00", "Z"),
                "raw_message_sha256": raw_sha,
                "raw_object_ref": str(raw_ref),
                "capture_receipt_sha256": supplied,
                "provider_id": str(raw.get("provider_id") or "").strip(),
                "model_id": model,
            }
        sources.append({
            "path": str(path.expanduser().resolve()),
            "content_sha256": supplied,
            "model_id": model,
            "curve_count": len(curves),
        })
    return expected_model, rows, sources


def _registry_timezones(conn: sqlite3.Connection) -> dict[str, str]:
    grouped: dict[str, set[str]] = defaultdict(set)
    for station, timezone_name in conn.execute(
        "SELECT station_id,settlement_timezone FROM basket_target_market_registry "
        "WHERE station_id IS NOT NULL AND settlement_timezone IS NOT NULL "
        "AND settlement_timezone<>'' GROUP BY station_id,settlement_timezone"
    ):
        grouped[str(station).upper()].add(str(timezone_name))
    result: dict[str, str] = {}
    for station, values in grouped.items():
        if len(values) == 1:
            ZoneInfo(next(iter(values)))
            result[station] = next(iter(values))
    return result


def _truth_index(conn: sqlite3.Connection, stations: set[str], dates: set[str], as_of: datetime) -> dict[tuple[str, str], dict[str, Any]]:
    if not stations or not dates:
        return {}
    sm = ",".join("?" for _ in stations)
    dm = ",".join("?" for _ in dates)
    rows = conn.execute(
        "SELECT truth_id,station_id,local_date,official_tmax_c,revision_no,"
        "canonical_committed_at_utc,content_sha256,observation_ids_json "
        "FROM calibration_station_daily_high_truth WHERE station_id IN (" + sm + ") "
        "AND local_date IN (" + dm + ") AND source_lane='AWC' AND qc_status='ACCEPTED' "
        "AND authority_scope IN ('FORMAL_NATIVE_ELIGIBLE','NO_LIVE_REPLAY_CANDIDATE') "
        "ORDER BY station_id,local_date,revision_no DESC,canonical_committed_at_utc DESC",
        tuple(sorted(stations)) + tuple(sorted(dates)),
    ).fetchall()
    result: dict[tuple[str, str], dict[str, Any]] = {}
    timezones = _registry_timezones(conn)
    for row in rows:
        station, day = str(row[1]).upper(), str(row[2])
        key = (station, day)
        if key in result:
            continue
        tz_name = timezones.get(station)
        if not tz_name:
            continue
        committed = _utc(row[5], "truth_committed")
        closed = _utc(settlement_day_window(day, tz_name)["target_day_end_utc"], "truth_closed")
        if committed > as_of or closed > as_of or committed < closed:
            continue
        try:
            observations = json.loads(str(row[7] or "[]"))
        except json.JSONDecodeError:
            observations = []
        result[key] = {
            "truth_id": str(row[0]),
            "official_tmax_c": float(row[3]),
            "revision_no": int(row[4]),
            "canonical_committed_at_utc": committed.isoformat(timespec="seconds").replace("+00:00", "Z"),
            "closed_at_utc": closed.isoformat(timespec="seconds").replace("+00:00", "Z"),
            "content_sha256": _sha(row[6], "truth_content"),
            "observation_count": len(observations) if isinstance(observations, list) else 0,
        }
    return result


def _taf_index(conn: sqlite3.Connection, stations: set[str], as_of: datetime) -> dict[tuple[str, str], dict[str, Any]]:
    if not stations:
        return {}
    sm = ",".join("?" for _ in stations)
    rows = conn.execute(
        "SELECT historical_taf_capture_id,station_id,event_date,settlement_timezone,"
        "decision_anchor_utc,issued_at_utc,provider_available_at_utc,tx_c,"
        "cloud_cover_fraction,precipitation_probability,wind_speed_mps,"
        "raw_taf_sha256,taf_response_sha256,source_sample_sha256,source_class,"
        "truth_label_authority,authority_scope,content_sha256 "
        "FROM calibration_historical_taf_capture WHERE station_id IN (" + sm + ") "
        "AND authority_scope='CALIBRATION_TRAIN_ONLY' AND tx_c IS NOT NULL "
        "ORDER BY station_id,event_date,provider_available_at_utc DESC,content_sha256 DESC",
        tuple(sorted(stations)),
    ).fetchall()
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        station, day = str(row[1]).upper(), str(row[2])
        key = (station, day)
        if key in result:
            continue
        available = _utc(row[6], "taf_available")
        issued = _utc(row[5], "taf_issued")
        if available < issued or available > as_of:
            continue
        timezone_name = _text(row[3], "taf_timezone")
        closed = _utc(settlement_day_window(day, timezone_name)["target_day_end_utc"], "taf_closed")
        if closed > as_of or available >= closed:
            continue
        raw_taf = _sha(row[11], "taf_raw")
        response_sha = _sha(row[12], "taf_response")
        truth_sha = _sha(row[13], "taf_truth_receipt")
        contract_identity = {
            "station_id": station,
            "event_date": day,
            "taf_capture_id": str(row[0]),
            "taf_content_sha256": _sha(row[17], "taf_content"),
            "raw_taf_sha256": raw_taf,
            "taf_response_sha256": response_sha,
            "truth_response_sha256": truth_sha,
            "receipt_class": RECEIPT_CLASS,
            "temporal_scope": TEMPORAL_SCOPE,
        }
        result[key] = {
            "sample_id": "taf-republisher-" + content_sha256(contract_identity)[:32],
            "station_id": station,
            "event_date": day,
            "settlement_timezone": timezone_name,
            "decision_anchor_utc": str(row[4]),
            "settlement_window": {
                "start_utc": settlement_day_window(day, timezone_name)["target_day_start_utc"],
                "end_utc": settlement_day_window(day, timezone_name)["target_day_end_utc"],
            },
            "season_bin": station_season_bin(station, day),
            "lead_bucket": "d0",
            "run_cycle": "REPUBLISHER_HISTORICAL_D0",
            "settlement_definition": "official_daily_tmax_c",
            "taf": {
                "issue_time_utc": str(row[5]),
                "available_time_utc": str(row[6]),
                "tx_c": float(row[7]),
                "cloud_cover_fraction": float(row[8]),
                "precipitation_probability": float(row[9]),
                "wind_speed_mps": float(row[10]),
                "raw_taf_sha256": raw_taf,
                "source_class": "official_date_addressed_historical_replay",
            },
            "receipts": {
                "taf_response_sha256": response_sha,
                "metar_response_sha256": truth_sha,
            },
            "source_class": "official_date_addressed_historical_replay",
            "truth_label_authority": str(row[15]),
            "authority_scope": str(row[16]),
            "historical_oos_contract_id": "taf-om-hist-" + content_sha256(contract_identity)[:40],
            "historical_oos_eligible": True,
            "receipt_class": RECEIPT_CLASS,
            "oos_event_temporal_scope": TEMPORAL_SCOPE,
            "future_event_count": 0,
            "taf_content_sha256": _sha(row[17], "taf_content"),
        }
    return result


def _model_lineage(curves: Mapping[str, Mapping[str, Any]], station: str, day: str, models: tuple[str, ...]) -> tuple[list[dict[str, Any]], str]:
    lineage: list[dict[str, Any]] = []
    anchors: list[datetime] = []
    for model in models:
        row = curves[(station, day, model)]
        anchor = _utc(row["model_run_initialization_utc"], "model_anchor")
        anchors.append(anchor)
        contract_identity = {
            "station_id": station,
            "event_date": day,
            "model_id": model,
            "source_object_sha256": row["raw_message_sha256"],
            "capture_receipt_sha256": row["capture_receipt_sha256"],
            "replay_anchor_utc": row["model_run_initialization_utc"],
            "receipt_class": RECEIPT_CLASS,
            "temporal_scope": TEMPORAL_SCOPE,
        }
        lineage.append({
            "model_id": model,
            "model_run_id": row["raw_message_sha256"],
            "published_at_utc": row["model_run_initialization_utc"],
            "historical_replay_anchor_utc": row["model_run_initialization_utc"],
            "available_at_utc": None,
            "availability_class": "historical_republisher_replay",
            "source_object_sha256": row["raw_message_sha256"],
            "raw_object_ref": row["raw_object_ref"],
            "raw_object_verified": True,
            "provider_id": row["provider_id"],
            "transport_provider_id": "OPEN_METEO",
            "source_role": SOURCE_ROLE,
            "receipt_class": RECEIPT_CLASS,
            "historical_oos_contract_id": "model-om-hist-" + content_sha256(contract_identity)[:40],
            "historical_oos_eligible": True,
            "oos_event_temporal_scope": TEMPORAL_SCOPE,
            "future_event_count": 0,
        })
    return lineage, max(anchors).isoformat(timespec="seconds").replace("+00:00", "Z")


def _crossfit(station: str, curves: Mapping[tuple[str, str, str], dict[str, Any]], truth: Mapping[tuple[str, str], dict[str, Any]], taf: Mapping[tuple[str, str], dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    model_days = sorted({day for st, day, model in curves if st == station and all((station, day, item) in curves for item in MODEL_IDS) and (station, day) in truth})
    history: list[str] = []
    predictions: list[dict[str, Any]] = []
    for day in model_days:
        raw = {model: float(curves[(station, day, model)]["forecast_daily_max_c"]) for model in MODEL_IDS}
        actual = float(truth[(station, day)]["official_tmax_c"])
        if len(history) >= MIN_TRAIN_DAYS and (station, day) in taf:
            weights = {model: 0.5 for model in MODEL_IDS}
            biases = {
                model: sum(float(truth[(station, prior)]["official_tmax_c"]) - float(curves[(station, prior, model)]["forecast_daily_max_c"]) for prior in history) / len(history)
                for model in MODEL_IDS
            }
            lineage, decision = _model_lineage(curves, station, day, MODEL_IDS)
            if _utc(decision, "crossfit_decision") > _utc(taf[(station, day)]["taf"]["available_time_utc"], "taf_available"):
                # No temporal claim is fabricated: this day remains an
                # explicit blocker if the replay anchor cannot precede TAF.
                pass
            fold_identity = {
                "station_id": station,
                "event_date": day,
                "train_dates": list(history),
                "selected_weights": weights,
                "biases_c": biases,
                "model_lineage": lineage,
            }
            predictions.append({
                "station_id": station,
                "event_date": day,
                "decision_at_utc": decision,
                "settlement_timezone": taf[(station, day)]["settlement_timezone"],
                "settlement_definition": "official_daily_tmax_c",
                "season_bin": taf[(station, day)]["season_bin"],
                "lead_bucket": "d0",
                "run_cycle": "REPUBLISHER_HISTORICAL_D0",
                "prediction_oos": True,
                "selected_models": list(MODEL_IDS),
                "selected_weights": weights,
                "combined_tmax_c": sum(weights[model] * (raw[model] + biases[model]) for model in MODEL_IDS),
                "model_rows": lineage,
                "train_start_date": history[0],
                "train_end_date": history[-1],
                "train_n": len(history),
                "selection_evidence_sha256": content_sha256(fold_identity),
                "oos_event_temporal_scope": TEMPORAL_SCOPE,
                "future_event_count": 0,
                "historical_oos_eligible": True,
                "receipt_class": RECEIPT_CLASS,
            })
        history.append(day)
    identity = {
        "schema": CROSSFIT_SCHEMA,
        "method_version": "republisher_historical_rolling_crossfit_v1",
        "station_id": station,
        "lead_bucket": "d0",
        "run_cycle": "REPUBLISHER_HISTORICAL_D0",
        "minimum_train_days": MIN_TRAIN_DAYS,
        "minimum_selected_models": 2,
        "prediction_count": len(predictions),
        "excluded_burn_in_dates": model_days[:MIN_TRAIN_DAYS],
        "predictions": predictions,
        "boundary": {
            "historical_replay_only": True,
            "native_arrival_claimed": False,
            "formal_p0_eligible": False,
            "p1_authority_written": False,
            "writes_production_db": False,
            "calls_order_gateway": False,
            "orders_submitted": 0,
        },
    }
    artifact_sha = content_sha256(identity)
    finalized = [{**row, "crossfit_prediction_artifact_sha256": artifact_sha} for row in predictions]
    crossfit = {**identity, "predictions": finalized, "crossfit_prediction_artifact_sha256": artifact_sha}
    exact_taf = {
        "schema": "bayes_basket_awc_taf_calibration_dataset_v1",
        "station_id": station,
        "source_class": "official_date_addressed_historical_replay",
        "source_lane": "AWC",
        "truth_label_authority": "FORMAL_STATION_LANE_TRUTH",
        "truth_authority": {
            "station_source_lane": "AWC",
            "evidence_source_lane": "AWC",
            "truth_label_authority": "FORMAL_STATION_LANE_TRUTH",
            "reference_only": False,
            "enters_realtime_truth": False,
            "enters_p1": False,
            "enters_hard_no": False,
            "replacement_required_when_canonical_history_available": False,
        },
        "samples": [],
    }
    for key, item in sorted(taf.items()):
        if key[0] != station:
            continue
        if not any(str(row["event_date"]) == key[1] for row in finalized):
            continue
        truth_item = truth.get(key)
        if truth_item is None:
            continue
        exact_taf["samples"].append({
            **item,
            "truth": {
                "source_lane": "AWC",
                "tmax_c": truth_item["official_tmax_c"],
                "observation_count": truth_item["observation_count"],
            },
        })
    exact_taf["sample_count"] = len(exact_taf["samples"])
    exact_taf["content_sha256"] = content_sha256(exact_taf)
    return crossfit, exact_taf


def build(*, db: Path, aifs: tuple[Path, ...], icon: tuple[Path, ...], as_of_utc: str, output: Path) -> dict[str, Any]:
    as_of = _utc(as_of_utc, "as_of")
    _model, aifs_curves, aifs_sources = _capture_rows(aifs, "AIFS")
    _model, icon_curves, icon_sources = _capture_rows(icon, "ICON")
    curves: dict[tuple[str, str, str], dict[str, Any]] = {}
    for key, row in aifs_curves.items():
        curves[(key[0], key[1], "AIFS")] = row
    for key, row in icon_curves.items():
        key3 = (key[0], key[1], "ICON")
        if key3 in curves:
            raise ContractError("republisher_taf_model_curve_duplicate")
        curves[key3] = row
    stations = {key[0] for key in curves}
    if len(stations) > MAX_STATIONS:
        raise ContractError("republisher_taf_station_scope_exceeded")
    with sqlite3.connect(f"file:{db.expanduser().resolve(strict=True)}?mode=ro", uri=True) as conn:
        conn.execute("PRAGMA query_only=ON")
        dates = {key[1] for key in curves}
        truth = _truth_index(conn, stations, dates, as_of)
        taf = _taf_index(conn, stations, as_of)
    station_reports: list[dict[str, Any]] = []
    for station in sorted(stations):
        crossfit, taf_dataset = _crossfit(station, curves, truth, taf)
        join = build_taf_exact_join(taf_dataset, crossfit)
        sample_count = int(join.get("sample_count") or 0)
        blockers = [str(row.get("reason") or "unknown") for row in join.get("blockers") or [] if isinstance(row, Mapping)]
        if sample_count < MIN_TAF_SAMPLE_N:
            blockers.append("taf_republisher_oos_sample_n_insufficient")
        station_reports.append({
            "station_id": station,
            "taf_tx_scope_count": len([key for key in taf if key[0] == station]),
            "crossfit_prediction_count": int(crossfit["prediction_count"]),
            "exact_join_sample_count": sample_count,
            "minimum_sample_n": MIN_TAF_SAMPLE_N,
            "state": "READY_REPUBLISHER_TAF_CANDIDATE" if sample_count >= MIN_TAF_SAMPLE_N and not join.get("blockers") else "BLOCKED",
            "blockers": sorted(set(blockers)),
            "crossfit": crossfit,
            "taf_dataset": taf_dataset,
            "exact_join": join,
        })
    identity = {
        "schema": SCHEMA,
        "as_of_utc": as_of.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "oos_data_policy": "HISTORICAL_CLOSED_DAY_ONLY",
        "future_data_policy": "OBSERVATIONAL_CANDIDATE_NOT_OOS",
        "source_class": SOURCE_ROLE,
        "transport_provider": "OPEN_METEO",
        "model_ids": list(MODEL_IDS),
        "minimum_crossfit_train_days": MIN_TRAIN_DAYS,
        "minimum_taf_sample_n": MIN_TAF_SAMPLE_N,
        "station_count": len(station_reports),
        "qualified_station_count": sum(row["state"] == "READY_REPUBLISHER_TAF_CANDIDATE" for row in station_reports),
        "source_captures": aifs_sources + icon_sources,
        "stations": station_reports,
        "boundary": {
            "historical_replay_only": True,
            "native_arrival_index_written": False,
            "p0m_materialized": False,
            "p1_authority_written": False,
            "formal_live_authority_written": False,
            "writes_target_database": False,
            "calls_order_gateway": False,
            "orders_submitted": 0,
        },
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--aifs", type=Path, action="append", default=list(DEFAULT_AIFS))
    parser.add_argument("--icon", type=Path, action="append", default=list(DEFAULT_ICON))
    parser.add_argument("--as-of-utc", default=datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"))
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    report = build(
        db=args.db,
        aifs=tuple(args.aifs),
        icon=tuple(args.icon),
        as_of_utc=args.as_of_utc,
        output=args.output,
    )
    output = args.output.expanduser().resolve()
    wrote = write_bounded_json_if_changed(output, report, limit=MAX_JSON_INPUT_BYTES)
    print(json.dumps({
        "schema": report["schema"],
        "station_count": report["station_count"],
        "qualified_station_count": report["qualified_station_count"],
        "minimum_taf_sample_n": report["minimum_taf_sample_n"],
        "output": str(output),
        "wrote": wrote,
        "writes_target_database": False,
        "p0m_materialized": False,
        "orders_submitted": 0,
        "content_sha256": report["content_sha256"],
    }, ensure_ascii=False, sort_keys=True))
    return 0 if report["qualified_station_count"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
