#!/usr/bin/env python3
"""Build a read-only audit packet for a retired legacy d1 projection.

This is the d1 deterministic-family builder's narrow revision bridge.  It
reads an already-closed AWC truth revision and existing model runs, emits the
legacy calibration evidence contract.  It never converts that legacy packet
into canonical facts; PR05 must rebuild the scope from native canonical
inputs through the one canonical ingress.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.calibration_ingress import (  # noqa: E402
    HISTORICAL_AWC_REPLAY,
    NO_LIVE_REPLAY,
)
from bayes_basket_target.contracts import MODEL_IDS, ContractError, content_sha256  # noqa: E402
from bayes_basket_target.resource_bounds import load_bounded_json, write_bounded_json  # noqa: E402
from bayes_basket_target.settlement_time import settlement_day_window  # noqa: E402


SCHEMA = "bayes_basket_target_calibration_evidence_v1"
SETTLEMENT_DEFINITION = "official_daily_tmax_c"
LEAD_BUCKET = "d1"
RUN_CYCLE = "PREVIOUS_DAY1"


def _utc(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError("d1_residual_as_of_timezone_required")
    return parsed.astimezone(timezone.utc)


def _stamp(value: datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def _ref(row: dict[str, Any]) -> dict[str, Any]:
    identity = dict(row)
    identity["reference_sha256"] = content_sha256(identity)
    return identity


def build_packet(*, db: Path, station: str, event_date: str, truth_id: str, as_of_utc: str) -> dict[str, Any]:
    station = station.strip().upper()
    event_date = event_date.strip()
    as_of = _utc(as_of_utc)
    try:
        parsed_date = date.fromisoformat(event_date)
    except ValueError as exc:
        raise ContractError("d1_residual_event_date_invalid") from exc
    database = db.expanduser().resolve(strict=True)
    if database.is_symlink():
        raise ContractError("d1_residual_database_symlink_forbidden")
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        station_row = conn.execute(
            "SELECT station_id,icao_code,city_label,timezone_name,source_lane,"
            "settlement_definition,registry_version,source_role,content_sha256 "
            "FROM calibration_station_registry WHERE station_id=?",
            (station,),
        ).fetchone()
        if station_row is None:
            raise ContractError("d1_residual_station_registry_missing")
        station_registry = dict(station_row)
        binding = conn.execute(
            "SELECT settlement_timezone,market_registry_timezone_count "
            "FROM calibration_station_market_timezone_binding WHERE station_id=?",
            (station,),
        ).fetchone()
        timezone_name = str(binding[0]).strip() if binding is not None else str(station_row[3]).strip()
        if binding is not None and int(binding[1] or 0) != 1:
            raise ContractError("d1_residual_timezone_binding_invalid")
        ZoneInfo(timezone_name)
        day_end = _utc(settlement_day_window(event_date, timezone_name)["target_day_end_utc"])
        if as_of < day_end:
            raise ContractError("d1_residual_truth_day_not_closed")

        truth = conn.execute(
            "SELECT truth_id,station_id,local_date,official_tmax_c,source_lane,"
            "settlement_definition,revision_no,qc_status,source_role,authority_scope,"
            "content_sha256,source_payload_sha256 FROM calibration_station_daily_high_truth "
            "WHERE truth_id=? AND station_id=? AND local_date=?",
            (truth_id, station, event_date),
        ).fetchone()
        if truth is None:
            raise ContractError("d1_residual_truth_missing")
        if (
            str(truth[4]).upper() != "AWC"
            or str(truth[5]) != SETTLEMENT_DEFINITION
            or str(truth[7]).upper() != "ACCEPTED"
            or str(truth[8]) != HISTORICAL_AWC_REPLAY
            or str(truth[9]) != NO_LIVE_REPLAY
            or int(truth[6] or 0) < 1
        ):
            raise ContractError("d1_residual_truth_lineage_invalid")
        if _utc(truth[0] and conn.execute(
            "SELECT canonical_committed_at_utc FROM calibration_station_daily_high_truth WHERE truth_id=?",
            (truth_id,),
        ).fetchone()[0]) > as_of:
            raise ContractError("d1_residual_truth_after_as_of")

        truth_reference = _ref({
            "truth_id": str(truth[0]),
            "station_id": str(truth[1]),
            "local_date": str(truth[2]),
            "official_tmax_c": float(truth[3]),
            "source_lane": str(truth[4]).upper(),
            "settlement_definition": str(truth[5]),
            "revision_no": int(truth[6]),
            "qc_status": str(truth[7]).upper(),
            "source_role": str(truth[8]),
            "authority_scope": str(truth[9]),
            "content_sha256": str(truth[10]),
        })
        models = conn.execute(
            "SELECT model_run_id,model_id,station_id,target_local_date,run_init_utc,"
            "lead_bucket,maturity_key,run_cycle,raw_tmax_c,settlement_definition,"
            "authority_scope,content_sha256,provider_id,provider_role,source_class,"
            "aggregator_or_republisher,open_meteo,asof_status,historical_replay_eligible,"
            "prospective_strict_asof_eligible FROM calibration_historical_asof_model_run "
            "WHERE station_id=? AND target_local_date=? AND lead_bucket=? AND run_cycle=? "
            "ORDER BY model_id",
            (station, event_date, LEAD_BUCKET, RUN_CYCLE),
        ).fetchall()
        if len(models) != len(MODEL_IDS) or {str(row[1]) for row in models} != set(MODEL_IDS):
            raise ContractError("d1_residual_exact_model_set_missing")
        model_references = []
        residuals = []
        for row in models:
            expected_init = datetime.combine(parsed_date, datetime.min.time(), tzinfo=timezone.utc)
            expected_init = expected_init.replace(day=parsed_date.day)  # keep type explicit
            expected_init = expected_init - __import__("datetime").timedelta(days=1)
            if _utc(row[4]) != expected_init:
                raise ContractError(f"d1_residual_run_init_invalid:{row[1]}")
            if str(row[10]) != NO_LIVE_REPLAY or str(row[14]) != "aggregator_republisher":
                raise ContractError(f"d1_residual_model_lineage_invalid:{row[1]}")
            model_references.append(_ref({
                "model_run_id": str(row[0]),
                "model_id": str(row[1]),
                "station_id": str(row[2]),
                "target_local_date": str(row[3]),
                "run_init_utc": str(row[4]),
                "lead_bucket": str(row[5]),
                "maturity_key": str(row[6]),
                "run_cycle": str(row[7]),
                "raw_tmax_c": float(row[8]),
                "settlement_definition": str(row[9]),
                "authority_scope": str(row[10]),
                "content_sha256": str(row[11]),
            }))
            residual_c = float(truth[3]) - float(row[8])
            pair_sha = content_sha256({
                "truth_id": str(truth[0]),
                "model_run_id": str(row[0]),
                "station_id": station,
                "model_id": str(row[1]),
                "event_date": event_date,
                "lead_bucket": LEAD_BUCKET,
                "forecast_tmax_c": float(row[8]),
                "official_tmax_c": float(truth[3]),
                "residual_c": residual_c,
            })
            residuals.append({
                "residual_id": f"d1-replay-residual-{station}-{event_date}-{row[1]}-r{int(truth[6])}",
                "truth_id": str(truth[0]),
                "model_run_id": str(row[0]),
                "station_id": station,
                "model_id": str(row[1]),
                "season_bin": "summer",
                "lead_bucket": LEAD_BUCKET,
                "maturity_key": str(row[6]),
                "run_cycle": RUN_CYCLE,
                "e_y_minus_f_raw_c": residual_c,
                "paired_asof_utc": str(row[4]),
                "qc_status": "ACCEPTED",
                "historical_replay_eligible": True,
                "prospective_strict_asof_eligible": False,
                "authority_scope": NO_LIVE_REPLAY,
                "pair_sha256": pair_sha,
                "truth_rows_sha256": str(truth[11]),
                "truth_sample_id": f"awc-truth-{truth[0]}",
            })
            residuals[-1]["content_sha256"] = content_sha256(residuals[-1])

    identity = {
        "schema": SCHEMA,
        "as_of_utc": _stamp(as_of),
        "source_name": "d1_historical_strict_awc_revision_bridge_v1",
        "station_registry": [station_registry],
        "truth_evidence": [],
        "truth_references": [truth_reference],
        "calibration_model_rows": [],
        "model_references": model_references,
        "residual_evidence": residuals,
        "temporal_boundary": {"closed_settlement_event_days_only": True, "future_target_used": False},
        "checks": {
            "truth_source_canonical_ingress": True,
            "temporary_awc_calibration_label_explicit": False,
            "existing_truth_bound_by_hash": True,
            "existing_model_bound_by_hash": True,
            "historical_awc_revision_append_only": True,
        },
        "boundary": {
            "calls_order_gateway": False,
            "places_orders": False,
            "live_order_authorized": False,
            "enters_realtime_truth": False,
            "enters_p1": False,
            "enters_hard_no": False,
            "writes_production_database": False,
            "writes_target_database": False,
            "reads_probability_state": False,
            "reads_market_state": False,
        },
        "scope": {
            "station_count": 1,
            "station_ids": [station],
            "lead_buckets": [LEAD_BUCKET],
            "run_cycles": [RUN_CYCLE],
            "model_ids": sorted(MODEL_IDS),
            "authority_scope_intent": NO_LIVE_REPLAY,
        },
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=ROOT / "data/bayes_basket_target_ssot.db")
    parser.add_argument("--station", required=True)
    parser.add_argument("--event-date", required=True)
    parser.add_argument("--truth-id", required=True)
    parser.add_argument("--as-of-utc", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    packet = build_packet(
        db=args.db,
        station=args.station,
        event_date=args.event_date,
        truth_id=args.truth_id,
        as_of_utc=args.as_of_utc,
    )
    output = args.output.expanduser().resolve()
    write_bounded_json(output, packet)
    if args.apply:
        raise ContractError("d1_legacy_projection_retired")
    receipt = {
        "schema": "bayes_basket_target_retired_legacy_builder_receipt_v1",
        "state": "BLOCKED",
        "blockers": ["LEGACY_PROJECTION_RETIRED"],
        "packet": str(output),
        "packet_content_sha256": packet["content_sha256"],
        "station_id": str(args.station).strip().upper(),
        "event_date": str(args.event_date),
        "writes_target_database": False,
        "writes_production_database": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }
    receipt_path = output.with_name(output.stem + "_receipt.json")
    write_bounded_json(receipt_path, receipt)
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
