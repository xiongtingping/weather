#!/usr/bin/env python3
"""Build a read-only, content-addressed AIFS ingress receipt.

The receipt separates ECMWF AIFS Global/Europe Open-Meteo candidate data from
official-native evidence.  It never fetches upstream data, writes the target
database, or changes P0 activation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import content_sha256  # noqa: E402
from bayes_basket_target.open_meteo_ensemble import ENSEMBLE_SYSTEMS  # noqa: E402
from bayes_basket_target.resource_bounds import (  # noqa: E402
    load_bounded_json,
    write_bounded_json_if_changed,
)


DEFAULT_DB = ROOT / "data" / "bayes_basket_target_ssot.db"
DEFAULT_OUTPUT = ROOT / "output" / "audits" / "aifs_model_ingress_20260801.json"
DEFAULT_OFFICIAL_CAPTURE = (
    ROOT / "output/audits/aifs_official_forward_capture_20260801_full45.json"
)
DEFAULT_OFFICIAL_DECODE = (
    ROOT / "output/audits/aifs_official_station_candidates_20260801.json"
)
AIFS_SINGLE_CANDIDATE_SCHEMA = "bayes_basket_target_aifs_station_candidate_evidence_v1"
AIFS_SYSTEM_IDS = (
    "ECMWF_AIFS_ENS_GLOBAL",
    "ECMWF_AIFS_ENS_EUROPE",
)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _content_addressed(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    supplied = str(value.get("content_sha256") or "").strip().lower()
    return len(supplied) == 64 and supplied == content_sha256({
        key: item for key, item in value.items() if key != "content_sha256"
    })


def _counts(conn: sqlite3.Connection, system_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT canonical_name,independence_family_id,upstream_provider_id,"
        "transport_provider_id,source_role,expected_member_count "
        "FROM calibration_republisher_ensemble_product "
        "WHERE ensemble_system_id=?",
        (system_id,),
    ).fetchone()
    bucket_count = int(conn.execute(
        "SELECT count(*) FROM calibration_republisher_ensemble_member_bucket "
        "WHERE ensemble_system_id=?",
        (system_id,),
    ).fetchone()[0])
    member_run_count = int(conn.execute(
        "SELECT count(*) FROM calibration_republisher_ensemble_member_run "
        "WHERE ensemble_system_id=?",
        (system_id,),
    ).fetchone()[0])
    role_rows = conn.execute(
        "SELECT source_role,authority_scope,count(*) "
        "FROM calibration_republisher_ensemble_member_bucket "
        "WHERE ensemble_system_id=? GROUP BY source_role,authority_scope",
        (system_id,),
    ).fetchall()
    # The dedicated republisher table intentionally has no native timing
    # fields.  A non-zero count would therefore be impossible by schema,
    # rather than a convention a caller can accidentally bypass.
    native_timestamp_rows: list[tuple[object, int]] = [(0, member_run_count)]
    native_arrival_evidence_count = 0
    oos_count = int(conn.execute(
        "SELECT count(*) FROM calibration_cross_native_ensemble_oos_evaluation "
        "WHERE ensemble_system_ids_json LIKE ?",
        (f"%{system_id}%",),
    ).fetchone()[0])
    candidate_only = bool(ENSEMBLE_SYSTEMS[system_id].get("candidate_only"))
    lineage_ok = (
        candidate_only
        and all(
            str(item[0]) == "aggregator_republisher"
            and str(item[1]) == "PRODUCTION_NO_LIVE_CANDIDATE"
            for item in role_rows
        )
        and native_arrival_evidence_count == 0
    )
    return {
        "catalog": dict(ENSEMBLE_SYSTEMS[system_id]),
        # This row is a product/family catalog registration in a separate
        # republisher lane; it is not present in the native registry.
        "product_catalog_registration": None if row is None else {
            "canonical_name": row[0],
            "independence_family_id": row[1],
            "provider_id": row[2],
            "source_class": "aggregator_republisher",
            "transport_provider_id": row[3],
            "source_role": row[4],
            "expected_member_count": int(row[5]),
            "confers_transport_authority": False,
        },
        "member_bucket_count": bucket_count,
        "member_run_count": member_run_count,
        "bucket_source_role_authority_counts": [
            {
                "source_role": str(item[0]),
                "authority_scope": str(item[1]),
                "count": int(item[2]),
            }
            for item in role_rows
        ],
        "native_timestamps_complete_counts": {
            str(item[0]): int(item[1]) for item in native_timestamp_rows
        },
        "native_arrival_evidence_count": native_arrival_evidence_count,
        "candidate_only": candidate_only,
        "transport_authority_source": "member_run_and_member_bucket_lineage",
        "formal_p0_eligible": False,
        "native_arrival_index_eligible": False,
        "candidate_lineage_verified": lineage_ok,
        "cross_oos_evaluation_count": oos_count,
    }


def _official_single_status(
    capture_path: Path,
    decode_path: Path,
) -> dict[str, Any]:
    capture = load_bounded_json(capture_path)
    decode = load_bounded_json(decode_path)
    fanout = capture.get("fanout") if isinstance(capture, dict) else None
    curves = decode.get("curves") if isinstance(decode, dict) else None
    station_timezones = capture.get("station_timezones") if isinstance(capture, dict) else None
    checks = decode.get("checks") if isinstance(decode, dict) else None
    candidate_capture = decode.get("capture_evidence") if isinstance(decode, dict) else None
    candidate_authority = decode.get("authority") if isinstance(decode, dict) else None
    expected_station_count = 45
    expected_arrival_count = int((fanout or {}).get("expected_arrival_count") or 0)
    expected_raw_count = int(capture.get("captured_count") or 0) + int(
        capture.get("reused_count") or 0
    ) if isinstance(capture, dict) else 0
    registry_scope_valid = (
        isinstance(station_timezones, dict)
        and len(station_timezones) == expected_station_count
        and isinstance(decode.get("station_timezones") if isinstance(decode, dict) else None, dict)
        and dict(decode.get("station_timezones") or {}) == dict(station_timezones)
    )
    candidate_capture_bound = (
        isinstance(candidate_capture, dict)
        and str(candidate_capture.get("content_sha256") or "")
        == str(capture.get("content_sha256") if isinstance(capture, dict) else "")
        and str(candidate_capture.get("sha256") or "") == _sha256_file(capture_path)
        and Path(str(candidate_capture.get("path") or "")).resolve()
        == capture_path.resolve()
    )
    ready = (
        isinstance(capture, dict)
        and _content_addressed(capture)
        and capture.get("provider_id") == "ecmwf_aifs025_official"
        and capture.get("product_id") == "ECMWF_AIFS_SINGLE_0P25"
        and capture.get("independence_family_id") == "ECMWF"
        and int(capture.get("blocked_count") or 0) == 0
        and registry_scope_valid
        and expected_raw_count >= 4
        and isinstance(fanout, dict)
        and int(fanout.get("expected_arrival_count") or 0) == expected_arrival_count
        and int(fanout.get("captured_count") or 0) == expected_arrival_count
        and int(fanout.get("blocked_count") or 0) == 0
        and isinstance(decode, dict)
        and _content_addressed(decode)
        and decode.get("schema") == AIFS_SINGLE_CANDIDATE_SCHEMA
        and decode.get("state") == "OFFICIAL_NATIVE_AIFS_SINGLE_45_STATION_CANDIDATE_COMPLETE"
        and decode.get("provider_id") == "ecmwf_aifs025_official"
        and decode.get("product_id") == "ECMWF_AIFS_SINGLE_0P25"
        and decode.get("independence_family_id") == "ECMWF"
        and candidate_capture_bound
        and isinstance(checks, dict)
        and int(checks.get("canonical_station_count") or 0) == expected_station_count
        and int(checks.get("station_arrival_count") or 0) == expected_arrival_count
        and int(checks.get("decoded_point_count") or 0) == expected_arrival_count
        and checks.get("all_station_curves_complete") is True
        and checks.get("all_raw_objects_official_native") is True
        and checks.get("all_station_arrivals_prospective") is True
        and checks.get("raw_objects_reused_without_download") is True
        and checks.get("open_meteo_aifs_variants_not_upgraded") is True
        and isinstance(candidate_authority, dict)
        and candidate_authority.get("source_role") == "official_native"
        and candidate_authority.get("native_arrival_index_eligible") is True
        and candidate_authority.get("formal_p0_eligible") is False
        and candidate_authority.get("p1_or_hard_no_eligible") is False
        and isinstance(curves, dict)
        and curves.get("all_complete") is True
        and int(curves.get("complete_curve_count") or 0) == expected_station_count
    )
    return {
        "product_id": "ECMWF_AIFS_SINGLE_0P25",
        "provider_id": "ecmwf_aifs025_official",
        "source_role": "official_native",
        "independence_family_id": "ECMWF",
        "counts_as_independent_family_from_ifs": False,
        "native_arrival_index_eligible": True,
        "formal_p0_eligible_without_strict_oos": False,
        "strict_oos_activation_present": False,
        "state": (
            "OFFICIAL_NATIVE_FORWARD_CAPTURED_OOS_PENDING"
            if ready
            else "BLOCKED_FAIL_CLOSED_OFFICIAL_NATIVE_EVIDENCE"
        ),
        "capture_path": str(capture_path),
        "decode_path": str(decode_path),
        "captured_or_reused_valid_times": int(capture.get("captured_count") or 0)
        + int(capture.get("reused_count") or 0),
        "expected_raw_message_count": expected_raw_count,
        "expected_station_arrival_count": expected_arrival_count,
        "fanout_captured_count": int((fanout or {}).get("captured_count") or 0),
        "decoded_point_count": int((checks or {}).get("decoded_point_count") or 0),
        "complete_curve_count": int((curves or {}).get("complete_curve_count") or 0),
        "evidence_verified": ready,
    }


def build_receipt(
    db_path: Path,
    *,
    official_capture_path: Path = DEFAULT_OFFICIAL_CAPTURE,
    official_decode_path: Path = DEFAULT_OFFICIAL_DECODE,
) -> dict[str, Any]:
    with sqlite3.connect(db_path) as conn:
        systems = {system_id: _counts(conn, system_id) for system_id in AIFS_SYSTEM_IDS}
    lineage_blockers = [
        f"{system_id}:candidate_lineage_invalid"
        for system_id, detail in systems.items()
        if not bool(detail["candidate_lineage_verified"])
    ]
    official_single = _official_single_status(
        official_capture_path.expanduser().resolve(strict=True),
        official_decode_path.expanduser().resolve(strict=True),
    )
    if not official_single["evidence_verified"]:
        lineage_blockers.append("ECMWF_AIFS_SINGLE_0P25:official_native_evidence_invalid")
    identity = {
        "schema": "aifs_model_ingress_audit_v1",
        "database": str(db_path),
        "model_scope_by_system": {
            system_id: str(ENSEMBLE_SYSTEMS[system_id]["model_scope"])
            for system_id in AIFS_SYSTEM_IDS
        },
        "systems": systems,
        "official_native_single": official_single,
        "authority": {
            "open_meteo_aifs_ensemble_source_role": "aggregator_republisher",
            "open_meteo_aifs_ensemble_candidate_only": True,
            "open_meteo_aifs_ensemble_formal_p0_eligible": False,
            "open_meteo_aifs_ensemble_native_arrival_index_eligible": False,
            "official_aifs_single_source_role": "official_native",
            "official_aifs_single_native_arrival_index_eligible": True,
            "official_aifs_single_formal_p0_eligible_without_strict_oos": False,
            "p1_or_hard_no_eligible": False,
            "independence_family_id": "ECMWF",
            "official_aifs_native_adapter": (
                "prospective_native_arrival_and_decode_integrated_oos_pending"
            ),
        },
        "state": (
            "OFFICIAL_NATIVE_FORWARD_CAPTURED_STRICT_OOS_PENDING"
            if not lineage_blockers
            else "BLOCKED_FAIL_CLOSED_LINEAGE"
        ),
        "blockers": lineage_blockers,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "live_submit_authorized": False,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--official-capture", type=Path, default=DEFAULT_OFFICIAL_CAPTURE
    )
    parser.add_argument(
        "--official-decode", type=Path, default=DEFAULT_OFFICIAL_DECODE
    )
    args = parser.parse_args()
    receipt = build_receipt(
        args.db.expanduser().resolve(strict=True),
        official_capture_path=args.official_capture,
        official_decode_path=args.official_decode,
    )
    output = args.output.expanduser().resolve()
    wrote = write_bounded_json_if_changed(output, receipt)
    print(json.dumps({
        "output": str(output),
        "wrote": wrote,
        "state": receipt["state"],
        "systems": {
            key: {
                "member_run_count": value["member_run_count"],
                "member_bucket_count": value["member_bucket_count"],
                "cross_oos_evaluation_count": value["cross_oos_evaluation_count"],
            }
            for key, value in receipt["systems"].items()
        },
        "formal_p0_eligible": False,
        "official_native_single": receipt["official_native_single"],
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "live_submit_authorized": False,
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
