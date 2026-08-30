#!/usr/bin/env python3
"""Append exact PASS/REJECTED evidence from a completed OOS manifest.

This command never builds OOS, fetches data, creates calibration artifacts,
or creates activations.  It is the narrow recovery path for introducing the
append-only OOS-evaluation SSOT after a signed manifest already exists.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.fleet import expected_station_count
from bayes_basket_target.contracts import content_sha256  # noqa: E402
from bayes_basket_target.native_ensemble_oos import (  # noqa: E402
    SCHEMA as REPORT_SCHEMA,
    persist_native_ensemble_oos_evaluations,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    assert_connection_target_capacity,
    load_bounded_json,
)


DEFAULT_DB = ROOT / "data" / "bayes_basket_target_ssot.db"
MAX_MANIFEST_BYTES = 8 * 1024 * 1024
MAX_STATIONS = expected_station_count()
HISTORICAL_OOS_TEMPORAL_SCOPE = "CLOSED_HISTORICAL_AWC_TRUTH"
HISTORICAL_REPUBLISHER_RECEIPT = "HISTORICAL_ASOF_REPUBLISHER_RECEIPT"
HISTORICAL_HOLDOUT_CLASSES = {
    "OFFLINE_CHRONOLOGICAL_NATIVE_ARCHIVE",
    HISTORICAL_REPUBLISHER_RECEIPT,
}
ACCEPTED_MANIFEST_SCHEMAS = {
    "native_ensemble_forward_oos_manifest_v1",
    "native_ensemble_historical_oos_manifest_v1",
}


def _validated_manifest(path: Path) -> Mapping[str, Any]:
    if path.is_symlink():
        raise RuntimeError("native_oos_evaluation_manifest_symlink_forbidden")
    payload = load_bounded_json(path, limit=MAX_MANIFEST_BYTES)
    if not isinstance(payload, Mapping):
        raise RuntimeError("native_oos_evaluation_manifest_invalid")
    identity = {
        key: value for key, value in payload.items()
        if key != "content_sha256"
    }
    if (
        str(payload.get("schema") or "")
        not in ACCEPTED_MANIFEST_SCHEMAS
        or str(payload.get("content_sha256") or "")
        != content_sha256(identity)
        or payload.get("production_no_live") is not True
        or payload.get("calls_order_gateway") is not False
        or payload.get("live_submit_authorized") is not False
        or int(payload.get("orders_submitted") or 0) != 0
    ):
        raise RuntimeError("native_oos_evaluation_manifest_contract_invalid")
    # Keep the legacy manifest schema for immutable receipts, but require an
    # explicit historical-only policy when a producer has emitted it.  Older
    # native-only manifests may omit these metadata fields; each candidate is
    # still checked below.  Open-Meteo candidates always require the explicit
    # policy and historical contract lineage.
    if payload.get("oos_data_policy") not in (None, "HISTORICAL_CLOSED_DAY_ONLY"):
        raise RuntimeError("native_oos_evaluation_manifest_temporal_policy_invalid")
    if payload.get("future_data_policy") not in (None, "OBSERVATIONAL_CANDIDATE_NOT_OOS"):
        raise RuntimeError("native_oos_evaluation_manifest_future_policy_invalid")
    try:
        if int(payload.get("future_event_count", 0) or 0) != 0:
            raise RuntimeError("native_oos_evaluation_manifest_future_event_count_nonzero")
    except (TypeError, ValueError) as exc:
        raise RuntimeError("native_oos_evaluation_manifest_future_event_count_invalid") from exc
    reports = payload.get("reports")
    if (
        not isinstance(reports, list)
        or not reports
        or len(reports) > MAX_STATIONS
        or any(
            not isinstance(report, Mapping)
            or str(report.get("schema") or "") != REPORT_SCHEMA
            for report in reports
        )
    ):
        raise RuntimeError("native_oos_evaluation_manifest_reports_invalid")
    stations = [str(report.get("station_id") or "") for report in reports]
    if len(stations) != len(set(stations)) or any(not station for station in stations):
        raise RuntimeError("native_oos_evaluation_manifest_station_scope_invalid")
    for report in reports:
        _validate_historical_report(report)
    return payload


def _validate_historical_report(report: Mapping[str, Any]) -> None:
    """Fail closed if a signed manifest contains prospective holdout rows."""

    candidates = report.get("cross_candidates") or []
    if not isinstance(candidates, list):
        raise RuntimeError("native_oos_evaluation_manifest_candidates_invalid")
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            raise RuntimeError("native_oos_evaluation_manifest_candidate_invalid")
        temporal_scope = str(candidate.get("oos_event_temporal_scope") or "")
        transports = candidate.get("system_transport_provider_ids")
        evaluations = candidate.get("system_evaluations")
        if not isinstance(transports, Mapping) or not isinstance(evaluations, Mapping):
            raise RuntimeError("native_oos_evaluation_manifest_historical_lineage_missing")
        try:
            future_count = int(candidate.get("future_event_count", 0) or 0)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("native_oos_evaluation_manifest_future_event_count_invalid") from exc
        if future_count != 0:
            raise RuntimeError("native_oos_evaluation_manifest_future_event_count_nonzero")
        for system_id, raw_eval in evaluations.items():
            if not isinstance(raw_eval, Mapping):
                raise RuntimeError("native_oos_evaluation_manifest_system_evaluation_invalid")
            provider = str(transports.get(system_id) or raw_eval.get("transport_provider_id") or "")
            holdout_class = str(raw_eval.get("holdout_source_class") or "")
            # A legacy native report can omit the explicit scope because its
            # offline archive class is already historical.  Republisher rows
            # never receive this compatibility relaxation.
            if temporal_scope and temporal_scope != HISTORICAL_OOS_TEMPORAL_SCOPE:
                raise RuntimeError("native_oos_evaluation_manifest_forward_scope_forbidden")
            if provider == "OPEN_METEO":
                if temporal_scope != HISTORICAL_OOS_TEMPORAL_SCOPE or holdout_class != HISTORICAL_REPUBLISHER_RECEIPT:
                    raise RuntimeError("native_oos_evaluation_manifest_republisher_history_required")
                for prediction in raw_eval.get("predictions") or []:
                    if not isinstance(prediction, Mapping) or not prediction.get("historical_oos_contract_ids"):
                        raise RuntimeError("native_oos_evaluation_manifest_republisher_contract_missing")
            elif holdout_class not in HISTORICAL_HOLDOUT_CLASSES - {HISTORICAL_REPUBLISHER_RECEIPT}:
                raise RuntimeError("native_oos_evaluation_manifest_forward_holdout_forbidden")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--commit", action="store_true")
    args = parser.parse_args()

    requested_manifest = args.manifest.expanduser()
    if requested_manifest.is_symlink():
        raise RuntimeError("native_oos_evaluation_manifest_symlink_forbidden")
    manifest_path = requested_manifest.resolve(strict=True)
    manifest = _validated_manifest(manifest_path)
    requested_db = args.db.expanduser()
    if requested_db.is_symlink():
        raise RuntimeError("native_oos_evaluation_db_symlink_forbidden")
    db = requested_db.resolve(strict=True)
    reports = list(manifest["reports"])
    candidate_count = sum(
        len(report.get("cross_candidates") or []) for report in reports
    )
    result: dict[str, Any] = {
        "schema": "native_ensemble_oos_evaluation_manifest_append_v1",
        "manifest": str(manifest_path),
        "manifest_content_sha256": manifest["content_sha256"],
        "database": str(db),
        "evaluated_at_utc": manifest["as_of_utc"],
        "station_count": len(reports),
        "cross_candidate_count": candidate_count,
        "commit": bool(args.commit),
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "live_submit_authorized": False,
        "creates_activations": False,
        "creates_calibration_artifacts": False,
    }
    if not args.commit:
        print(json.dumps({**result, "state": "VALIDATED_NO_WRITE"}, sort_keys=True))
        return 0

    with sqlite3.connect(db, timeout=60.0) as conn:
        conn.execute("PRAGMA busy_timeout=60000")
        assert_connection_target_capacity(conn, recover_oversized_wal=True)
        if str(conn.execute("PRAGMA quick_check").fetchone()[0]).lower() != "ok":
            raise RuntimeError("native_oos_evaluation_database_quick_check_failed")
        conn.execute("BEGIN IMMEDIATE")
        try:
            receipts = [
                persist_native_ensemble_oos_evaluations(
                    conn,
                    report,
                    evaluated_at_utc=str(manifest["as_of_utc"]),
                )
                for report in reports
            ]
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    inserted = sum(int(receipt["oos_evaluations"]) for receipt in receipts)
    print(json.dumps({
        **result,
        "state": "COMMITTED" if inserted else "NOOP_IDEMPOTENT",
        "oos_evaluations_inserted": inserted,
        "oos_evaluations_idempotent": candidate_count - inserted,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
