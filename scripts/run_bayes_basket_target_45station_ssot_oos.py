#!/usr/bin/env python3
"""Run bounded 45-station d1/d2 five-model OOS from materialized SSOT facts."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import MODEL_IDS, ContractError, content_sha256
from bayes_basket_target.et_history import assert_event_day_closed
from bayes_basket_target.resource_bounds import (
    assert_process_memory_budget,
    write_bounded_json_if_changed,
)
from bayes_basket_target.station_registry import station_coordinates
from scripts.build_rkpk_open_meteo_five_model_historical_oos import (
    _build_oos,
    _event_window,
    _meteorological_season,
    _utc_text,
)


SCHEMA = "bayes_basket_target_45station_ssot_oos_v1"
DEFAULT_DB = ROOT / "data/bayes_basket_target_ssot.db"
DEFAULT_OUTPUT = (
    ROOT / "output/audits/bayes_basket_target_45station_ssot_oos_20260730_v1"
)
MAX_STATIONS = 45
MAX_DAYS = 120
HISTORICAL_AWC_ROLE = "AWC_HISTORICAL_REPLAY_INGRESS"


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _read_rows(
    connection: sqlite3.Connection,
    *,
    start_date: str,
    end_date: str,
) -> tuple[list[sqlite3.Row], list[sqlite3.Row], list[sqlite3.Row]]:
    stations = connection.execute(
        "SELECT station_id,timezone_name,source_lane,content_sha256 "
        "FROM calibration_station_registry ORDER BY station_id"
    ).fetchall()
    truths = connection.execute(
        """
        WITH ranked AS (
          SELECT *,
                 ROW_NUMBER() OVER (
                   PARTITION BY station_id,local_date
                   ORDER BY revision_no DESC,truth_id DESC
                 ) AS rn
          FROM calibration_station_daily_high_truth
          WHERE local_date BETWEEN ? AND ?
        )
        SELECT truth_id,station_id,local_date,official_tmax_c,source_lane,
               source_role,authority_scope,content_sha256
        FROM ranked WHERE rn=1 ORDER BY station_id,local_date
        """,
        (start_date, end_date),
    ).fetchall()
    models = connection.execute(
        """
        SELECT model_run_id,model_id,station_id,target_local_date,lead_bucket,
               maturity_key,run_cycle,raw_tmax_c,provider_id,authority_scope,
               content_sha256
        FROM calibration_historical_asof_model_run
        WHERE target_local_date BETWEEN ? AND ?
          AND run_cycle IN ('PREVIOUS_DAY1','PREVIOUS_DAY2')
        ORDER BY station_id,target_local_date,lead_bucket,model_id
        """,
        (start_date, end_date),
    ).fetchall()
    return stations, truths, models


def run(args: argparse.Namespace) -> dict[str, Any]:
    first = date.fromisoformat(args.start_date)
    last = date.fromisoformat(args.end_date)
    day_count = (last - first).days + 1
    if first > last or not 1 <= day_count <= MAX_DAYS:
        raise ContractError("ssot_oos_date_range_invalid")
    database = args.db.expanduser().resolve()
    if not database.is_file():
        raise ContractError("ssot_oos_database_missing")
    output_root = args.output_root.expanduser().resolve()
    as_of_utc = args.as_of_utc or _utc_now()
    assert_process_memory_budget()

    uri = f"file:{database}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        connection.row_factory = sqlite3.Row
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ContractError("ssot_oos_database_quick_check_failed")
        stations, truths, models = _read_rows(
            connection,
            start_date=args.start_date,
            end_date=args.end_date,
        )

    if len(stations) != MAX_STATIONS:
        raise ContractError("ssot_oos_exact_45_station_registry_required")
    expected_dates = {
        (first.fromordinal(first.toordinal() + offset)).isoformat()
        for offset in range(day_count)
    }
    station_map = {str(row["station_id"]): dict(row) for row in stations}
    truth_map: dict[tuple[str, str], dict[str, Any]] = {}
    for raw in truths:
        row = dict(raw)
        key = (str(row["station_id"]), str(row["local_date"]))
        if key in truth_map:
            raise ContractError("ssot_oos_latest_truth_duplicate")
        station = station_map.get(key[0])
        if station is None:
            raise ContractError("ssot_oos_truth_station_missing")
        assert_event_day_closed(
            key[1],
            as_of_utc=as_of_utc,
            target_timezone=str(station["timezone_name"]),
        )
        if str(row.get("source_role") or "") == "TEMPORARY_AWC_CALIBRATION_LABEL":
            raise ContractError("ssot_oos_temporary_awc_truth_retired")
        if (
            str(row.get("source_lane") or "").upper() != "AWC"
            or str(row.get("source_role") or "") != HISTORICAL_AWC_ROLE
        ):
            raise ContractError("ssot_oos_historical_awc_lineage_invalid")
        truth_map[key] = row

    model_map: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for raw in models:
        row = dict(raw)
        key = (
            str(row["station_id"]),
            str(row["target_local_date"]),
            str(row["lead_bucket"]),
            str(row["model_id"]),
        )
        if key in model_map:
            raise ContractError("ssot_oos_model_natural_key_duplicate")
        model_map[key] = row

    summaries: list[dict[str, Any]] = []
    for station_id, station in sorted(station_map.items()):
        station_dates = {
            target for sid, target in truth_map if sid == station_id
        }
        if station_dates != expected_dates:
            raise ContractError(f"ssot_oos_truth_dates_incomplete:{station_id}")
        coordinates = station_coordinates(station_id)
        season_bin = _meteorological_season(
            target_date=first.fromordinal(
                first.toordinal() + (day_count // 2)
            ),
            latitude=float(coordinates["latitude"]),
        )
        for lead in (1, 2):
            lead_bucket = f"d{lead}"
            rows_by_model: dict[str, list[dict[str, Any]]] = defaultdict(list)
            lineage: list[dict[str, str]] = []
            for target in sorted(expected_dates):
                truth = truth_map[(station_id, target)]
                start, end = _event_window(
                    target,
                    str(station["timezone_name"]),
                )
                for model_id in MODEL_IDS:
                    model = model_map.get(
                        (station_id, target, lead_bucket, model_id)
                    )
                    if model is None:
                        raise ContractError(
                            f"ssot_oos_model_missing:{station_id}:{target}:"
                            f"{lead_bucket}:{model_id}"
                        )
                    forecast = float(model["raw_tmax_c"])
                    official = float(truth["official_tmax_c"])
                    rows_by_model[model_id].append({
                        "event_date": target,
                        "forecast_tmax_c": forecast,
                        "official_tmax_c": official,
                        "residual_c": official - forecast,
                        "truth_sample_id": str(truth["truth_id"]),
                        "truth_rows_sha256": str(truth["content_sha256"]),
                        "settlement_window": {
                            "start_utc": _utc_text(start),
                            "end_utc": _utc_text(end),
                        },
                    })
                    lineage.append({
                        "model_run_id": str(model["model_run_id"]),
                        "content_sha256": str(model["content_sha256"]),
                        "truth_id": str(truth["truth_id"]),
                        "truth_content_sha256": str(truth["content_sha256"]),
                    })
            oos = _build_oos(
                rows_by_model=rows_by_model,
                lead_days=lead,
                fetched_at_utc=as_of_utc,
                station_id=station_id,
                settlement_timezone=str(station["timezone_name"]),
                season_bin=season_bin,
                run_cycle=f"PREVIOUS_DAY{lead}",
            )
            winner = oos.get("research_winner") or {}
            selected = list(winner.get("selected_models") or [])
            probability_passed = bool(
                winner.get("probability_non_degradation_vs_uniform")
            )
            identity = {
                "schema": SCHEMA,
                "station_id": station_id,
                "settlement_timezone": str(station["timezone_name"]),
                "start_date": args.start_date,
                "end_date": args.end_date,
                "lead_bucket": lead_bucket,
                "as_of_utc": as_of_utc,
                "source_database": str(database),
                "source_database_quick_check": "ok",
                "source_lineage_sha256": content_sha256(lineage),
                "source_model_run_count": len(lineage),
                "source_truth_count": len(expected_dates),
                "oos": oos,
                "authority": {
                    "offline_oos_complete": oos.get("state")
                    == "OOS_COMPLETE_NO_LIVE_REVIEW",
                    "minimum_two_models_selected": len(selected) >= 2,
                    "probability_non_degradation_vs_uniform":
                        probability_passed,
                    "production_no_live_review_eligible":
                        probability_passed,
                    "formal_p0_activation_eligible": False,
                    "formal_blockers": [
                        "historical_replay_has_no_native_arrival"
                    ],
                },
                "boundary": {
                    "writes_production_database": False,
                    "calls_order_gateway": False,
                    "places_orders": False,
                    "live_submit_authorized": False,
                },
            }
            payload = {**identity, "content_sha256": content_sha256(identity)}
            path = output_root / station_id / f"five_model_{lead_bucket}_ssot_oos.json"
            write_bounded_json_if_changed(path, payload)
            summaries.append({
                "station_id": station_id,
                "lead_bucket": lead_bucket,
                "common_sample_n": int(oos.get("common_sample_n") or 0),
                "selected_models": selected,
                "selected_weights": dict(winner.get("selected_weights") or {}),
                "probability_non_degradation_vs_uniform": probability_passed,
                "artifact_path": str(path),
                "artifact_sha256": payload["content_sha256"],
            })
            assert_process_memory_budget()

    manifest_identity = {
        "schema": SCHEMA,
        "as_of_utc": as_of_utc,
        "start_date": args.start_date,
        "end_date": args.end_date,
        "station_count": len(station_map),
        "scope_count": len(summaries),
        "model_ids": list(MODEL_IDS),
        "summaries": summaries,
        "checks": {
            "exact_45_station_scope": len(station_map) == MAX_STATIONS,
            "all_scopes_have_full_sample": all(
                row["common_sample_n"] == day_count for row in summaries
            ),
            "all_scopes_select_at_least_two_models": all(
                len(row["selected_models"]) >= 2 for row in summaries
            ),
            "probability_non_degradation_passed_scope_count": sum(
                row["probability_non_degradation_vs_uniform"]
                for row in summaries
            ),
            "formal_p0_activation_eligible_scope_count": 0,
        },
        "boundary": {
            "writes_production_database": False,
            "calls_order_gateway": False,
            "places_orders": False,
            "live_submit_authorized": False,
        },
    }
    manifest = {
        **manifest_identity,
        "content_sha256": content_sha256(manifest_identity),
    }
    manifest_path = output_root / "manifest.json"
    write_bounded_json_if_changed(manifest_path, manifest)
    return {
        "manifest": str(manifest_path),
        **manifest["checks"],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--as-of-utc")
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def main() -> int:
    result = run(parse_args())
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
