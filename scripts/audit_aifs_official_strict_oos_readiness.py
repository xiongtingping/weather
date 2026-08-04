#!/usr/bin/env python3
"""Read-only readiness audit for deterministic official ECMWF AIFS Single.

AIFS Single is one deterministic official-native curve, never an AIFS
ensemble.  This audit verifies forward capture/decode separately from the
append-only deterministic training-fact -> strict-OOS -> promotion -> provider
authority chain.  Open-Meteo AIFS ensemble rows remain a republisher-only
comparison count and cannot satisfy any AIFS Single authority requirement.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import ContractError, content_sha256  # noqa: E402
from bayes_basket_target.aifs_deterministic_authority import (  # noqa: E402
    PROVIDER_AUTHORITY_SCHEMA,
    validate_aifs_deterministic_oos_report,
    validate_aifs_deterministic_promotion_receipt,
    validate_aifs_deterministic_provider_authority,
)
from bayes_basket_target.aifs_deterministic_oos import (  # noqa: E402
    MIN_HOLDOUT_DAYS,
    MIN_TRAIN_DAYS,
    TRAINING_BATCH_SCHEMA,
    validate_aifs_deterministic_training_batch,
)
from bayes_basket_target.model_arrival import (  # noqa: E402
    model_arrival_key,
    validate_model_arrival_capture,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    ResourceBoundError,
    assert_process_memory_budget,
    load_bounded_json,
    write_bounded_json,
)


SCHEMA = "aifs_official_strict_oos_readiness_audit_v1"
PROVIDER_ID = "ecmwf_aifs025_official"
PRODUCT_ID = "ECMWF_AIFS_SINGLE_0P25"
INDEPENDENCE_FAMILY_ID = "ECMWF"
# This ID is retained only to quantify existing Open-Meteo AIFS ensemble
# republisher history.  It is never a source of AIFS Single authority.
AIFS_REPUBLISHER_COMPARISON_SYSTEM_ID = "ECMWF_AIFS_ENS_GLOBAL"
OFFICIAL_AIFS_HOST = "ecmwf-forecasts.s3.eu-central-1.amazonaws.com"
MAX_RUN_RESULTS = 16
MAX_STATION_ARRIVALS = 256
MAX_INPUT_BYTES = 2 * 1024 * 1024
EXPECTED_STATION_COUNT = 45
LEGACY_EXPECTED_VALID_TIMES_PER_STATION = 4
DEFAULT_DB = ROOT / "data" / "bayes_basket_target_ssot.db"
DEFAULT_CAPTURE_ROOT = ROOT / "output/audits/bayes_basket_target_aifs_forward_runs"
DEFAULT_DECODE_EVIDENCE = (
    ROOT
    / "output/audits/aifs_official_45station_native_decode_lifecycle_v2_20260801.json"
)


class AifsReadinessError(ContractError):
    """Raised for malformed evidence; normal strict-OOS gaps are reported."""


def _mapping(name: str, value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise AifsReadinessError(f"aifs_readiness_{name}_mapping_required")
    return value


def _text(name: str, value: Any) -> str:
    result = str(value or "").strip()
    if not result:
        raise AifsReadinessError(f"aifs_readiness_{name}_missing")
    return result


def _under(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def _load_capture_result(root: Path) -> tuple[Path, Mapping[str, Any]]:
    if not root.is_dir() or root.is_symlink():
        raise AifsReadinessError("aifs_readiness_capture_root_invalid")
    paths = sorted(root.glob("*/run_result.json"))
    if not paths:
        raise AifsReadinessError("aifs_readiness_forward_capture_missing")
    if len(paths) > MAX_RUN_RESULTS:
        raise AifsReadinessError("aifs_readiness_run_result_bound_exceeded")
    candidates: list[tuple[str, Path, Mapping[str, Any]]] = []
    for path in paths:
        if path.is_symlink():
            raise AifsReadinessError("aifs_readiness_run_result_symlink_forbidden")
        value = _mapping("run_result", load_bounded_json(path, limit=MAX_INPUT_BYTES))
        if value.get("provider_id") != PROVIDER_ID:
            continue
        candidates.append((_text("run_result_decision_at", value.get("execution_decision_at_utc")), path.resolve(), value))
    if not candidates:
        raise AifsReadinessError("aifs_readiness_aifs_forward_capture_missing")
    return sorted(candidates, key=lambda item: (item[0], str(item[1])))[-1][1:]


def _validate_capture(
    root: Path,
    path: Path,
    result: Mapping[str, Any],
) -> dict[str, Any]:
    authority = _mapping("capture_authority", result.get("authority"))
    boundary = _mapping("capture_boundary", result.get("boundary"))
    if (
        result.get("schema") != "bayes_basket_target_aifs_forward_capture_scheduler_v1"
        or result.get("state") != "CAPTURED_NATIVE_FORWARD_CANDIDATE"
        or result.get("provider_id") != PROVIDER_ID
        or result.get("product_id") != PRODUCT_ID
        or result.get("independence_family_id") != INDEPENDENCE_FAMILY_ID
        or authority.get("source_role") != "official_native"
        or authority.get("native_arrival_index_eligible") is not True
        or authority.get("formal_p0_eligible_without_strict_oos") is not False
        or authority.get("counts_as_independent_family_from_ifs") is not False
        or boundary.get("writes_target_database") is not False
        or boundary.get("writes_production_db") is not False
        or boundary.get("calls_order_gateway") is not False
        or boundary.get("places_orders") is not False
        or boundary.get("live_submit_authorized") is not False
        or boundary.get("promotion_allowed") is not False
    ):
        raise AifsReadinessError("aifs_readiness_capture_authority_invalid")
    fanout = _mapping("capture_fanout", result.get("fanout"))
    raw_station_valid_hours = result.get("station_valid_hours")
    raw_station_timezones = result.get("station_timezones")
    station_valid_times: dict[str, set[str]] = {}
    station_timezones: dict[str, str] = {}
    if raw_station_valid_hours is not None or raw_station_timezones is not None:
        if (
            not isinstance(raw_station_valid_hours, Mapping)
            or not isinstance(raw_station_timezones, Mapping)
        ):
            raise AifsReadinessError("aifs_readiness_station_scope_mapping_invalid")
        for station_id, rows in raw_station_valid_hours.items():
            station = _text("capture_station_id", station_id).upper()
            if not isinstance(rows, list):
                raise AifsReadinessError("aifs_readiness_station_valid_scope_invalid")
            valid_times = {
                _text("capture_valid_time", item.get("valid_time_utc"))
                for item in rows
                if isinstance(item, Mapping)
            }
            if len(valid_times) < 3:
                raise AifsReadinessError("aifs_readiness_station_valid_scope_invalid")
            station_valid_times[station] = valid_times
        station_timezones = {
            _text("capture_timezone_station", station_id).upper(): _text(
                "capture_station_timezone", timezone_name
            )
            for station_id, timezone_name in raw_station_timezones.items()
        }
        if (
            len(station_valid_times) != EXPECTED_STATION_COUNT
            or set(station_valid_times) != set(station_timezones)
        ):
            raise AifsReadinessError("aifs_readiness_station_timezone_scope_invalid")
        expected = sum(len(values) for values in station_valid_times.values())
    else:
        # Test-only/legacy receipts predate per-station timezone fanout.
        expected = EXPECTED_STATION_COUNT * LEGACY_EXPECTED_VALID_TIMES_PER_STATION
    outputs = fanout.get("outputs")
    if (
        not isinstance(outputs, list)
        or len(outputs) != expected
        or int(fanout.get("expected_arrival_count") or 0) != expected
        or int(fanout.get("captured_count") or 0) != expected
        or int(fanout.get("blocked_count") or 0) != 0
        or list(fanout.get("blockers") or [])
    ):
        raise AifsReadinessError("aifs_readiness_capture_fanout_incomplete")
    station_dir = path.parent.resolve() / "station_arrivals"
    if not station_dir.is_dir() or station_dir.is_symlink():
        raise AifsReadinessError("aifs_readiness_station_arrival_root_invalid")
    if len(outputs) > MAX_STATION_ARRIVALS:
        raise AifsReadinessError("aifs_readiness_station_arrival_bound_exceeded")

    normalized: list[dict[str, Any]] = []
    source_paths: set[Path] = set()
    for entry in outputs:
        item = _mapping("capture_output", entry)
        if item.get("status") not in {"CAPTURED", "REUSED"}:
            raise AifsReadinessError("aifs_readiness_capture_output_state_invalid")
        item_path = Path(_text("capture_output_path", item.get("path"))).resolve()
        if item_path.is_symlink() or not item_path.is_file() or not _under(item_path, station_dir):
            raise AifsReadinessError("aifs_readiness_capture_output_path_invalid")
        source_paths.add(item_path)
    if len(source_paths) != expected:
        raise AifsReadinessError("aifs_readiness_capture_output_duplicate")
    for item_path in sorted(source_paths):
        packet = _mapping("station_arrival_packet", load_bounded_json(item_path, limit=MAX_INPUT_BYTES))
        if packet.get("status") not in {"CAPTURED", "REUSED"}:
            raise AifsReadinessError("aifs_readiness_station_arrival_state_invalid")
        arrival = validate_model_arrival_capture(
            _mapping("station_arrival", packet.get("arrival"))
        )
        if arrival.get("provider_id") != PROVIDER_ID or arrival.get("model_id") != "ECMWF":
            raise AifsReadinessError("aifs_readiness_station_arrival_provider_invalid")
        parsed = urlparse(_text("station_arrival_source_url", arrival.get("source_url")))
        if parsed.scheme != "https" or parsed.netloc.lower() != OFFICIAL_AIFS_HOST:
            raise AifsReadinessError("aifs_readiness_station_arrival_not_ecmwf_official")
        if (
            arrival.get("aggregator_or_republisher") is not False
            or arrival.get("open_meteo") is not False
            or arrival.get("production_authority") is not False
            or arrival.get("live_order_authorized") is not False
        ):
            raise AifsReadinessError("aifs_readiness_station_arrival_lineage_invalid")
        normalized.append(arrival)
        assert_process_memory_budget()
    keys = {model_arrival_key(row) for row in normalized}
    stations = {str(row["station_id"]) for row in normalized}
    valid_by_station: dict[str, set[str]] = {}
    raw_hashes: set[str] = set()
    for row in normalized:
        valid_by_station.setdefault(str(row["station_id"]), set()).add(
            str(row["valid_time_utc"])
        )
        raw_hashes.update(str(value) for value in row["raw_payload_sha256s"])
    valid_scope_ok = (
        valid_by_station == station_valid_times
        if station_valid_times
        else all(
            len(values) == LEGACY_EXPECTED_VALID_TIMES_PER_STATION
            for values in valid_by_station.values()
        )
    )
    timezone_scope_ok = (
        all(
            str(row.get("target_timezone") or "")
            == station_timezones.get(str(row["station_id"]).upper())
            for row in normalized
        )
        if station_timezones
        else True
    )
    expected_raw_count = len({
        valid for values in station_valid_times.values() for valid in values
    }) if station_valid_times else LEGACY_EXPECTED_VALID_TIMES_PER_STATION
    if (
        len(keys) != expected
        or len(stations) != EXPECTED_STATION_COUNT
        or not valid_scope_ok
        or not timezone_scope_ok
        or len(raw_hashes) != expected_raw_count
    ):
        raise AifsReadinessError("aifs_readiness_station_valid_scope_incomplete")
    return {
        "run_result_path": str(path),
        "run_result_sha256": content_sha256(dict(result)),
        "target_local_date": _text("capture_target_date", result.get("target_local_date")),
        "run_initialization_utc": _text("capture_run_initialization", result.get("run_initialization_utc")),
        "decision_at_utc": _text("capture_decision_at", result.get("execution_decision_at_utc")),
        "station_count": len(stations),
        "station_valid_times": {
            station: sorted(values)
            for station, values in sorted(station_valid_times.items())
        },
        "station_timezones": dict(sorted(station_timezones.items())),
        "valid_times_per_station": (
            "REGISTRY_BOUND_VARIABLE"
            if station_valid_times
            else LEGACY_EXPECTED_VALID_TIMES_PER_STATION
        ),
        "station_valid_arrival_count": len(normalized),
        "unique_raw_message_count": len(raw_hashes),
        "raw_message_sha256s": sorted(raw_hashes),
        "capture_state": "OFFICIAL_NATIVE_FORWARD_COMPLETE",
    }


def _validate_decode(
    path: Path,
    *,
    capture: Mapping[str, Any],
) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise AifsReadinessError("aifs_readiness_decode_evidence_missing")
    receipt = _mapping("decode_evidence", load_bounded_json(path, limit=MAX_INPUT_BYTES))
    boundary = _mapping("decode_boundary", receipt.get("boundary"))
    curves = _mapping("decode_curves", receipt.get("curves"))
    rows = curves.get("curves")
    registry_bound = bool(capture.get("station_timezones"))
    if (
        receipt.get("schema") != "bayes_basket_target_native_model_arrival_decode_evidence_v1"
        or boundary.get("read_only_decode") is not True
        or boundary.get("writes_production_db") is not False
        or boundary.get("calls_order_gateway") is not False
        or boundary.get("places_orders") is not False
        or curves.get("all_complete") is not True
        or not isinstance(rows, list)
        or not rows
        or (
            registry_bound
            and receipt.get("target_timezone") != "REGISTRY_BOUND_PER_STATION"
        )
        or (
            registry_bound
            and dict(receipt.get("station_timezones") or {})
            != dict(capture["station_timezones"])
        )
    ):
        raise AifsReadinessError("aifs_readiness_decode_evidence_invalid")
    raw_hashes = set(capture["raw_message_sha256s"])
    complete: list[Mapping[str, Any]] = []
    expected_times = {
        str(station): set(values)
        for station, values in (capture.get("station_valid_times") or {}).items()
    }
    expected_timezones = dict(capture.get("station_timezones") or {})
    for row in rows:
        curve = _mapping("decode_curve", row)
        coverage = _mapping("decode_coverage", curve.get("coverage"))
        if (
            curve.get("provider_id") != PROVIDER_ID
            or curve.get("state") != "COMPLETE"
            or curve.get("event_date") != capture["target_local_date"]
            or curve.get("model_run_initialization_utc") != capture["run_initialization_utc"]
            or (
                expected_times
                and int(coverage.get("point_count") or 0)
                != len(expected_times.get(str(curve.get("station_id")), set()))
            )
            or (
                expected_timezones
                and curve.get("target_timezone")
                != expected_timezones.get(str(curve.get("station_id")))
            )
        ):
            raise AifsReadinessError("aifs_readiness_decode_curve_invalid")
        points = curve.get("points")
        if (
            not isinstance(points, list)
            or (
                expected_times
                and {
                    str(point.get("valid_time_utc") or "")
                    for point in points if isinstance(point, Mapping)
                } != expected_times.get(str(curve.get("station_id")), set())
            )
            or (
                not expected_times
                and len(points) != LEGACY_EXPECTED_VALID_TIMES_PER_STATION
            )
        ):
            raise AifsReadinessError("aifs_readiness_decode_point_scope_invalid")
        if any(
            not isinstance(point, Mapping)
            or point.get("provider_id") != PROVIDER_ID
            or str(point.get("raw_message_sha256") or "") not in raw_hashes
            for point in points
        ):
            raise AifsReadinessError("aifs_readiness_decode_raw_lineage_invalid")
        complete.append(curve)
    if (
        registry_bound
        and (
            len(complete) != EXPECTED_STATION_COUNT
            or {str(row.get("station_id")) for row in complete}
            != set(expected_times)
        )
    ):
        raise AifsReadinessError("aifs_readiness_decode_station_scope_incomplete")
    return {
        "decode_evidence_path": str(path.resolve()),
        "decode_evidence_sha256": content_sha256(dict(receipt)),
        "complete_curve_count": len(complete),
        "decoded_station_ids": sorted(str(row["station_id"]) for row in complete),
        "coverage_scope": "FULL_45_STATION_FORWARD_NOT_CLOSED_DAY_STRICT_OOS",
    }


def _republisher_comparison_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Count Open-Meteo AIFS ensemble rows without granting them authority."""

    tables = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    member_count = 0
    bucket_count = 0
    if "calibration_native_ensemble_historical_member_run" in tables:
        member_count = int(conn.execute(
            "SELECT COUNT(*) FROM calibration_native_ensemble_historical_member_run "
            "WHERE ensemble_system_id=? AND transport_provider_id='OPEN_METEO'",
            (AIFS_REPUBLISHER_COMPARISON_SYSTEM_ID,),
        ).fetchone()[0])
    if "calibration_native_ensemble_historical_member_bucket" in tables:
        bucket_count = int(conn.execute(
            "SELECT COUNT(*) FROM calibration_native_ensemble_historical_member_bucket "
            "WHERE ensemble_system_id=? AND source_role='aggregator_republisher'",
            (AIFS_REPUBLISHER_COMPARISON_SYSTEM_ID,),
        ).fetchone()[0])
    return {
        "open_meteo_aifs_ensemble_republisher_member_run_count": member_count,
        "open_meteo_aifs_ensemble_republisher_member_bucket_count": bucket_count,
        "republisher_rows_are_aifs_single_authority_eligible": 0,
    }


def _load_deterministic_artifacts(
    *,
    training_batch_path: Path | None,
    strict_oos_report_path: Path | None,
    promotion_receipt_path: Path | None,
    provider_authority_path: Path | None,
) -> dict[str, Any]:
    paths = (
        training_batch_path,
        strict_oos_report_path,
        promotion_receipt_path,
        provider_authority_path,
    )
    if not any(paths):
        return {
            "state": "MISSING",
            "reason": "aifs_single_deterministic_artifacts_missing",
        }
    if any(path is None for path in paths):
        raise AifsReadinessError("aifs_readiness_deterministic_artifact_bundle_incomplete")
    resolved = [Path(path).expanduser().resolve() for path in paths if path is not None]
    if any(not path.is_file() or path.is_symlink() for path in resolved):
        raise AifsReadinessError("aifs_readiness_deterministic_artifact_path_invalid")
    batch = _mapping("deterministic_training_batch", load_bounded_json(resolved[0], limit=MAX_INPUT_BYTES))
    report = _mapping("deterministic_oos_report", load_bounded_json(resolved[1], limit=MAX_INPUT_BYTES))
    receipt = _mapping("deterministic_promotion_receipt", load_bounded_json(resolved[2], limit=MAX_INPUT_BYTES))
    authority = _mapping("deterministic_provider_authority", load_bounded_json(resolved[3], limit=MAX_INPUT_BYTES))
    if batch.get("schema") != TRAINING_BATCH_SCHEMA:
        raise AifsReadinessError("aifs_readiness_deterministic_training_schema_invalid")
    try:
        facts = validate_aifs_deterministic_training_batch(batch)
        candidates = validate_aifs_deterministic_oos_report(
            report,
            training_batch=batch,
        )
        candidate = validate_aifs_deterministic_promotion_receipt(
            receipt,
            strict_oos_report=report,
            training_batch=batch,
        )
        validate_aifs_deterministic_provider_authority(
            authority,
            training_batch=batch,
            strict_oos_report=report,
            promotion_receipt=receipt,
        )
    except ContractError as exc:
        raise AifsReadinessError(
            "aifs_readiness_deterministic_artifact_invalid:" + str(exc)
        ) from exc
    if (
        receipt.get("decision") != "PROMOTED"
        or str(candidate.get("content_sha256") or "") not in candidates
        or authority.get("schema") != PROVIDER_AUTHORITY_SCHEMA
        or authority.get("formal_provider_eligible") is not True
        or authority.get("formal_p0_role_eligible") is not False
        or authority.get("native_ensemble_member_tables_prohibited") is not True
    ):
        raise AifsReadinessError("aifs_readiness_deterministic_promotion_not_ready")
    return {
        "state": "FORMAL_DETERMINISTIC_PROVIDER_READY",
        "training_fact_count": len(facts),
        "strict_oos_candidate_count": len(candidates),
        "promoted_candidate_content_sha256": candidate["content_sha256"],
        "promotion_receipt_content_sha256": receipt["content_sha256"],
        "provider_authority_content_sha256": authority["content_sha256"],
        "provider_authority_schema": authority["schema"],
        "formal_provider_eligible": True,
        "formal_p0_role_eligible": False,
        "paths": {
            "training_batch": str(resolved[0]),
            "strict_oos_report": str(resolved[1]),
            "promotion_receipt": str(resolved[2]),
            "provider_authority": str(resolved[3]),
        },
    }


def audit_readiness(
    *,
    db_path: Path,
    capture_root: Path,
    decode_evidence_path: Path,
    deterministic_training_batch_path: Path | None = None,
    deterministic_strict_oos_report_path: Path | None = None,
    deterministic_promotion_receipt_path: Path | None = None,
    deterministic_provider_authority_path: Path | None = None,
) -> dict[str, Any]:
    database = db_path.expanduser().resolve()
    if not database.is_file() or database.is_symlink():
        raise AifsReadinessError("aifs_readiness_database_invalid")
    assert_process_memory_budget()
    capture_path, run_result = _load_capture_result(capture_root.expanduser().resolve())
    capture = _validate_capture(capture_root.expanduser().resolve(), capture_path, run_result)
    decode = _validate_decode(decode_evidence_path.expanduser().resolve(), capture=capture)
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=15) as conn:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA busy_timeout=15000")
        republisher_counts = _republisher_comparison_counts(conn)
    deterministic = _load_deterministic_artifacts(
        training_batch_path=deterministic_training_batch_path,
        strict_oos_report_path=deterministic_strict_oos_report_path,
        promotion_receipt_path=deterministic_promotion_receipt_path,
        provider_authority_path=deterministic_provider_authority_path,
    )
    requirement = {
        "official_native_closed_awc_train_cases": MIN_TRAIN_DAYS,
        "official_native_closed_awc_strict_holdout_cases": MIN_HOLDOUT_DAYS,
        "deterministic_product": PRODUCT_ID,
        "forecast_role": "DETERMINISTIC_SINGLE",
        "native_ensemble_member_tables_prohibited": True,
        "formal_p0_role_adapter_required": True,
    }
    blockers = [
        "aifs_single_deterministic_training_oos_promotion_authority_missing"
        if deterministic["state"] != "FORMAL_DETERMINISTIC_PROVIDER_READY"
        else "",
        "aifs_single_deterministic_p0_role_adapter_required"
        if deterministic["state"] == "FORMAL_DETERMINISTIC_PROVIDER_READY"
        else "",
    ]
    blockers = [item for item in blockers if item]
    provider_ready = deterministic["state"] == "FORMAL_DETERMINISTIC_PROVIDER_READY"
    core = {
        "schema": SCHEMA,
        "state": (
            "FORMAL_DETERMINISTIC_PROVIDER_READY"
            if provider_ready else "BLOCKED_STRICT_OOS"
        ),
        "provider": {
            "provider_id": PROVIDER_ID,
            "product_id": PRODUCT_ID,
            "publication_authority": "official_native",
            "independence_family_id": INDEPENDENCE_FAMILY_ID,
            "same_independence_family_as_ifs": True,
            "counts_as_distinct_independence_family": False,
        },
        "official_native_forward_capture": capture,
        "decode_evidence": decode,
        "closed_day_strict_oos": {
            "semantic_track": "AIFS_SINGLE_DETERMINISTIC",
            "requirements": requirement,
            "deterministic_artifacts": deterministic,
            "open_meteo_republisher_comparison": republisher_counts,
            "open_meteo_aifs_ensemble_rows_are_single_provider_binding_eligible": False,
        },
        "provider_binding": {
            "schema_required": PROVIDER_AUTHORITY_SCHEMA,
            "constructible": provider_ready,
            "formal_provider_eligible": provider_ready,
            "formal_p0_role_eligible": False,
            "requires_deterministic_p0_role_adapter": True,
            "reason": (
                "deterministic_training_oos_promotion_authority_required"
                if not provider_ready
                else "provider_ready_but_deterministic_p0_role_adapter_required"
            ),
        },
        "blockers": blockers,
        "boundary": {
            "read_only": True,
            "writes_target_database": False,
            "writes_production_db": False,
            "calls_order_gateway": False,
            "places_orders": False,
            "orders_submitted": 0,
            "live_submit_authorized": False,
            "network_access": False,
            "downloads_ifs_or_gefs": False,
        },
    }
    return {**core, "content_sha256": content_sha256(core)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--capture-root", type=Path, default=DEFAULT_CAPTURE_ROOT)
    parser.add_argument("--decode-evidence", type=Path, default=DEFAULT_DECODE_EVIDENCE)
    parser.add_argument("--deterministic-training-batch", type=Path)
    parser.add_argument("--deterministic-strict-oos-report", type=Path)
    parser.add_argument("--deterministic-promotion-receipt", type=Path)
    parser.add_argument("--deterministic-provider-authority", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        receipt = audit_readiness(
            db_path=args.db,
            capture_root=args.capture_root,
            decode_evidence_path=args.decode_evidence,
            deterministic_training_batch_path=args.deterministic_training_batch,
            deterministic_strict_oos_report_path=args.deterministic_strict_oos_report,
            deterministic_promotion_receipt_path=args.deterministic_promotion_receipt,
            deterministic_provider_authority_path=args.deterministic_provider_authority,
        )
    except (AifsReadinessError, ResourceBoundError, sqlite3.Error) as exc:
        core = {
            "schema": SCHEMA,
            "state": "AUDIT_ERROR_FAIL_CLOSED",
            "blockers": [str(exc)],
            "boundary": {
                "read_only": True,
                "writes_target_database": False,
                "writes_production_db": False,
                "calls_order_gateway": False,
                "places_orders": False,
                "orders_submitted": 0,
                "live_submit_authorized": False,
                "network_access": False,
                "downloads_ifs_or_gefs": False,
            },
        }
        receipt = {**core, "content_sha256": content_sha256(core)}
    write_bounded_json(args.output.expanduser().resolve(), receipt, limit=MAX_INPUT_BYTES)
    print(json.dumps({
        "state": receipt["state"],
        "blockers": receipt.get("blockers", []),
        "orders_submitted": receipt["boundary"]["orders_submitted"],
        "content_sha256": receipt["content_sha256"],
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
