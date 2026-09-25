#!/usr/bin/env python3
"""Build AIFS Single closed-day training facts from already retained evidence.

This is an intentionally read-only bridge between the official AIFS daily
curve decoder and the deterministic strict-OOS contract.  It never captures
raw data, opens SQLite for writing, creates a provider binding, or reaches an
order gateway.  A curve whose station-local settlement day is still open (or
which has no accepted closed AWC truth) is retained as a content-addressed
no-op rather than being allowed to become a partially labelled fact.
"""

from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.fleet import expected_station_count
from bayes_basket_target.canonical_contract import FORMAL_TRUTH_LANES_SQL  # noqa: E402
from bayes_basket_target.aifs_deterministic_oos import (  # noqa: E402
    PROVIDER_ID,
    build_aifs_deterministic_training_facts,
)
from bayes_basket_target.contracts import ContractError, content_sha256  # noqa: E402
from bayes_basket_target.model_arrival import (  # noqa: E402
    validate_model_arrival_capture,
)
from bayes_basket_target.native_arrival_decode import (  # noqa: E402
    ARRIVAL_WRAPPER_LIFECYCLE_FIELDS,
    arrival_identity_sha256,
    arrival_timing_identity_sha256,
    arrival_wrapper_immutable_sha256,
)
from bayes_basket_target.native_model_decode import (  # noqa: E402
    MIN_CURVE_POINTS,
    MIN_CURVE_POINTS_BY_PROVIDER,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    MIB,
    ResourceBoundError,
    assert_process_memory_budget,
    load_bounded_json,
    write_bounded_json_if_changed,
)
from bayes_basket_target.settlement_time import (  # noqa: E402
    settlement_day_state,
    settlement_day_window,
)


SCHEMA = "aifs_deterministic_training_facts_runner_receipt_v1"
DECODE_SCHEMA = "bayes_basket_target_native_model_arrival_decode_evidence_v1"
DEFAULT_DB = ROOT / "data/bayes_basket_target_ssot.db"
DEFAULT_OUTPUT = ROOT / "output/audits/aifs_deterministic_training_facts_latest.json"
# Wrapper-only decode receipts predate the immutable arrival lineage contract
# and are deliberately rejected by ``_load_matching_arrivals``.  Selecting
# only lifecycle-v2 evidence here prevents a lexical ``*_post_consumption``
# legacy receipt from becoming the implicit default again.
DEFAULT_DECODE_GLOB = "aifs_official_45station_native_decode_lifecycle_v2_*.json"
MAX_CURVES = expected_station_count()
MAX_ARRIVAL_ARTIFACTS = MAX_CURVES * 8
MAX_AUDIT_BYTES = 4 * MIB
MAX_ARRIVAL_ARTIFACT_BYTES = 1 * MIB
MAX_RETAINED_RAW_BYTES = 256 * MIB
MAX_OUTPUT_BYTES = 4 * MIB
MAX_WAIT_SECONDS = 4 * 60 * 60
MAX_POLL_SECONDS = 60
ALLOWED_TRUTH_SCOPES = (
    "FORMAL_NATIVE_ELIGIBLE",
    "NO_LIVE_REPLAY_CANDIDATE",
)


def _minimum_closed_curve_point_count(curve: Mapping[str, Any]) -> int:
    """Return the cadence-specific minimum already used by the native decoder."""

    return MIN_CURVE_POINTS_BY_PROVIDER.get(
        str(curve.get("provider_id") or "").strip().lower(),
        MIN_CURVE_POINTS,
    )


def _utc(value: str | None) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ContractError("aifs_training_runner_as_of_utc_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError("aifs_training_runner_as_of_utc_timezone_required")
    return parsed.astimezone(timezone.utc)


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise ContractError("aifs_training_runner_input_unreadable") from exc
    return digest.hexdigest()


def _bounded_raw_sha256(path: Path) -> str:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ContractError("aifs_training_runner_retained_raw_unreadable") from exc
    if size <= 0 or size > MAX_RETAINED_RAW_BYTES:
        raise ContractError("aifs_training_runner_retained_raw_size_invalid")
    return _file_sha256(path)


def _sha(name: str, value: Any) -> str:
    result = str(value or "").strip().lower()
    if len(result) != 64 or any(
        item not in "0123456789abcdef" for item in result
    ):
        raise ContractError(f"aifs_training_runner_{name}_invalid_sha256")
    return result


def _default_decode_path() -> Path:
    candidates = sorted((ROOT / "output/audits").glob(DEFAULT_DECODE_GLOB))
    if not candidates:
        raise ContractError("aifs_training_runner_decode_audit_missing")
    return candidates[-1].resolve(strict=True)


def _load_decode_audit(path: Path) -> tuple[dict[str, Any], str]:
    resolved = path.expanduser().resolve(strict=True)
    payload = load_bounded_json(resolved, limit=MAX_AUDIT_BYTES)
    if not isinstance(payload, Mapping) or payload.get("schema") != DECODE_SCHEMA:
        raise ContractError("aifs_training_runner_decode_audit_schema_invalid")
    evidence_identity = {
        key: value
        for key, value in payload.items()
        if key not in {"evidence_sha256", "receipt_sha256"}
    }
    evidence_sha = _sha(
        "decode_audit_evidence", payload.get("evidence_sha256")
    )
    if evidence_sha != content_sha256(evidence_identity):
        raise ContractError("aifs_training_runner_decode_audit_evidence_sha_mismatch")
    receipt_sha = _sha(
        "decode_audit_receipt", payload.get("receipt_sha256")
    )
    if receipt_sha != content_sha256({
        **evidence_identity,
        "evidence_sha256": evidence_sha,
    }):
        raise ContractError("aifs_training_runner_decode_audit_receipt_sha_mismatch")
    curves_container = payload.get("curves")
    curves = (
        curves_container.get("curves")
        if isinstance(curves_container, Mapping)
        else None
    )
    artifacts = payload.get("input_artifacts")
    if (
        not isinstance(curves, list)
        or len(curves) > MAX_CURVES
        or not isinstance(artifacts, list)
        or len(artifacts) > MAX_ARRIVAL_ARTIFACTS
    ):
        raise ContractError("aifs_training_runner_decode_audit_shape_invalid")
    return dict(payload), _file_sha256(resolved)


def _normalize_curves(
    audit: Mapping[str, Any], *, station_id: str | None
) -> list[dict[str, Any]]:
    container = audit["curves"]
    assert isinstance(container, Mapping)
    raw_curves = container["curves"]
    assert isinstance(raw_curves, list)
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for raw in raw_curves:
        if not isinstance(raw, Mapping):
            raise ContractError("aifs_training_runner_curve_mapping_required")
        curve = dict(raw)
        if str(curve.get("provider_id") or "").strip().lower() != PROVIDER_ID:
            raise ContractError("aifs_training_runner_curve_provider_invalid")
        current_station = str(curve.get("station_id") or "").strip().upper()
        event_date = str(curve.get("event_date") or "").strip()
        run = str(curve.get("model_run_initialization_utc") or "").strip()
        if not current_station or not event_date or not run:
            raise ContractError("aifs_training_runner_curve_scope_invalid")
        scope = (current_station, event_date, run)
        if scope in seen:
            raise ContractError("aifs_training_runner_curve_scope_duplicate")
        seen.add(scope)
        if station_id is None or current_station == station_id:
            result.append(curve)
    return result


def _read_closed_awc_truths(
    database: Path,
    *,
    curves: Sequence[Mapping[str, Any]],
    as_of_utc: str,
) -> dict[tuple[str, str], dict[str, Any]]:
    scopes = sorted({
        (str(curve["station_id"]).strip().upper(), str(curve["event_date"]).strip())
        for curve in curves
    })
    if not scopes:
        return {}
    if len(scopes) > MAX_CURVES:
        raise ContractError("aifs_training_runner_truth_scope_limit_exceeded")
    station_placeholders = ",".join("?" for _ in sorted({item[0] for item in scopes}))
    date_placeholders = ",".join("?" for _ in sorted({item[1] for item in scopes}))
    query = f"""
        WITH ranked AS (
          SELECT t.*, ROW_NUMBER() OVER (
            PARTITION BY t.station_id,t.local_date,t.settlement_definition
            ORDER BY t.revision_no DESC,t.canonical_committed_at_utc DESC,t.truth_id DESC
          ) AS rn
          FROM calibration_station_daily_high_truth AS t
          WHERE t.station_id IN ({station_placeholders})
            AND t.local_date IN ({date_placeholders})
            AND t.settlement_definition='official_daily_tmax_c'
            AND t.source_lane IN {FORMAL_TRUTH_LANES_SQL}
            AND t.qc_status='ACCEPTED'
            AND t.timestamps_complete=1
            AND t.authority_scope IN (?,?)
            AND t.canonical_committed_at_utc IS NOT NULL
            AND julianday(t.canonical_committed_at_utc)<=julianday(?)
        )
        SELECT station_id,local_date,official_tmax_c,source_lane,
               settlement_definition,qc_status,canonical_committed_at_utc,
               content_sha256
        FROM ranked WHERE rn=1
        ORDER BY station_id,local_date
    """
    params = [
        *sorted({item[0] for item in scopes}),
        *sorted({item[1] for item in scopes}),
        *ALLOWED_TRUTH_SCOPES,
        as_of_utc,
    ]
    uri = f"file:{database.as_posix()}?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ContractError("aifs_training_runner_ssot_quick_check_failed")
            rows = connection.execute(query, params).fetchall()
    except sqlite3.Error as exc:
        raise ContractError("aifs_training_runner_ssot_read_failed") from exc
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for raw in rows:
        row = dict(raw)
        station = str(row["station_id"]).strip().upper()
        event_date = str(row["local_date"]).strip()
        key = (station, event_date)
        if key in result:
            raise ContractError("aifs_training_runner_truth_scope_duplicate")
        result[key] = {
            "station_id": station,
            "event_date": event_date,
            "source_lane": "AWC",
            "settlement_definition": "official_daily_tmax_c",
            "qc_status": "ACCEPTED",
            "official_tmax_c": row["official_tmax_c"],
            "canonical_committed_at_utc": str(row["canonical_committed_at_utc"]),
            "content_sha256": str(row["content_sha256"]),
        }
    return result


def _eligible_closed_curves(
    curves: Sequence[Mapping[str, Any]],
    *,
    truths: Mapping[tuple[str, str], Mapping[str, Any]],
    as_of_utc: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], Counter[str]]:
    eligible: list[dict[str, Any]] = []
    eligible_truths: list[dict[str, Any]] = []
    skipped: Counter[str] = Counter()
    for curve in curves:
        station = str(curve["station_id"]).strip().upper()
        event_date = str(curve["event_date"]).strip()
        target_timezone = str(curve.get("target_timezone") or "").strip()
        if not target_timezone:
            raise ContractError("aifs_training_runner_curve_timezone_missing")
        state = settlement_day_state(
            event_date,
            as_of_utc=as_of_utc,
            settlement_timezone=target_timezone,
        )
        if state["state"] != "closed":
            skipped["station_local_day_not_closed"] += 1
            continue
        points = curve.get("points")
        minimum_point_count = _minimum_closed_curve_point_count(curve)
        if (
            not isinstance(points, list)
            or len(points) < minimum_point_count
        ):
            # One short curve must not poison a multi-station batch.  Keep it
            # as an explicit closed-day blocker while allowing every other
            # independently complete curve to produce its own fact.
            skipped["closed_aifs_curve_points_insufficient"] += 1
            continue
        truth = truths.get((station, event_date))
        if truth is None:
            skipped["closed_awc_truth_missing"] += 1
            continue
        eligible.append(dict(curve))
        eligible_truths.append({
            **dict(truth),
            "station_timezone": target_timezone,
        })
    return eligible, eligible_truths, skipped


def _readiness_snapshot(
    curves: Sequence[Mapping[str, Any]],
    *,
    truths: Mapping[tuple[str, str], Mapping[str, Any]],
    as_of_utc: str,
) -> dict[str, Any]:
    """Describe the next legal local-day/truth transition without guessing.

    The clock comes only from each decoded curve's registry-backed settlement
    timezone.  This metadata is deliberately advisory: reaching a close time
    does not make a fact legal until an accepted AWC truth is also present.
    """

    as_of = _utc(as_of_utc)
    open_rows: list[tuple[datetime, str, str]] = []
    closed_missing_truth: list[str] = []
    closed_with_truth: list[str] = []
    closed_points_insufficient: list[str] = []
    for curve in curves:
        station = str(curve["station_id"]).strip().upper()
        event_date = str(curve["event_date"]).strip()
        target_timezone = str(curve.get("target_timezone") or "").strip()
        if not target_timezone:
            raise ContractError("aifs_training_runner_curve_timezone_missing")
        close_text = settlement_day_window(
            event_date, target_timezone
        )["target_day_end_utc"]
        close = _utc(close_text)
        if as_of < close:
            open_rows.append((close, station, event_date))
        elif not isinstance(curve.get("points"), list) or len(
            curve["points"]
        ) < _minimum_closed_curve_point_count(curve):
            closed_points_insufficient.append(station)
        elif (station, event_date) in truths:
            closed_with_truth.append(station)
        else:
            closed_missing_truth.append(station)

    next_close: datetime | None = None
    next_stations: list[str] = []
    if open_rows:
        next_close = min(row[0] for row in open_rows)
        next_stations = sorted({row[1] for row in open_rows if row[0] == next_close})
    if closed_missing_truth:
        next_action = "WAIT_FOR_ACCEPTED_AWC_TRUTH"
        not_before = as_of
    elif next_close is not None:
        next_action = "WAIT_FOR_SETTLEMENT_CLOSE"
        not_before = next_close
    elif closed_with_truth:
        next_action = "BUILD_NOW"
        not_before = as_of
    else:
        next_action = "NO_MATCHING_CURVES"
        not_before = None
    return {
        "curve_count": len(curves),
        "open_station_count": len(open_rows),
        "closed_truth_missing_station_count": len(closed_missing_truth),
        "closed_truth_missing_station_ids": sorted(closed_missing_truth),
        "closed_truth_available_station_count": len(closed_with_truth),
        "closed_curve_points_insufficient_station_count": len(
            closed_points_insufficient
        ),
        "closed_curve_points_insufficient_station_ids": sorted(
            closed_points_insufficient
        ),
        "next_settlement_close_utc": _stamp(next_close) if next_close else None,
        "next_settlement_close_station_ids": next_stations,
        "next_action": next_action,
        "next_action_not_before_utc": _stamp(not_before) if not_before else None,
        "downloads_required": False,
        "reuses_retained_official_aifs_raw": True,
    }


def _station_inventory(
    curves: Sequence[Mapping[str, Any]],
    *,
    truths: Mapping[tuple[str, str], Mapping[str, Any]],
    as_of_utc: str,
) -> list[dict[str, Any]]:
    """Emit one small, signed status row per scanned station-day.

    This makes the complete 45-station closed-day state directly inspectable
    from the target-owned receipt.  It contains no mutable state and does not
    alter the fact eligibility gate below.
    """

    result: list[dict[str, Any]] = []
    for curve in curves:
        station = str(curve["station_id"]).strip().upper()
        event_date = str(curve["event_date"]).strip()
        target_timezone = str(curve.get("target_timezone") or "").strip()
        points = curve.get("points")
        point_count = len(points) if isinstance(points, list) else 0
        minimum_point_count = _minimum_closed_curve_point_count(curve)
        day_state = settlement_day_state(
            event_date,
            as_of_utc=as_of_utc,
            settlement_timezone=target_timezone,
        )
        truth = truths.get((station, event_date))
        if day_state["state"] != "closed":
            outcome = "STATION_LOCAL_DAY_OPEN"
        elif point_count < minimum_point_count:
            outcome = "CLOSED_AIFS_CURVE_POINTS_INSUFFICIENT"
        elif truth is None:
            outcome = "CLOSED_AWC_TRUTH_MISSING"
        else:
            outcome = "CLOSED_AWC_FACT_ELIGIBLE"
        result.append({
            "station_id": station,
            "event_date": event_date,
            "target_timezone": target_timezone,
            "curve_state": str(curve.get("state") or ""),
            "curve_point_count": point_count,
            "minimum_curve_point_count": minimum_point_count,
            "station_local_day_state": str(day_state["state"]),
            "accepted_awc_truth_available": truth is not None,
            "accepted_awc_truth_content_sha256": (
                str(truth["content_sha256"]) if truth is not None else None
            ),
            "closed_day_outcome": outcome,
        })
    return sorted(result, key=lambda item: item["station_id"])


def _load_matching_arrivals(
    audit: Mapping[str, Any],
    *,
    curves: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    required = {
        (
            str(point.get("station_id") or "").strip().upper(),
            str(point.get("event_date") or "").strip(),
            str(point.get("model_run_initialization_utc") or "").strip(),
            str(point.get("valid_time_utc") or "").strip(),
        )
        for curve in curves
        for point in (curve.get("points") or [])
        if isinstance(point, Mapping)
    }
    if not required:
        raise ContractError("aifs_training_runner_curve_points_missing")
    artifacts = audit["input_artifacts"]
    assert isinstance(artifacts, list)
    arrivals: list[dict[str, Any]] = []
    lineage_inputs: list[dict[str, Any]] = []
    found: set[tuple[str, str, str, str]] = set()
    verified_raw_paths: dict[str, str] = {}
    verified_receipts: dict[str, tuple[str, ...]] = {}
    for raw_artifact in artifacts:
        if not isinstance(raw_artifact, Mapping):
            raise ContractError("aifs_training_runner_arrival_artifact_invalid")
        path_text = str(raw_artifact.get("path") or "").strip()
        if not path_text:
            raise ContractError("aifs_training_runner_arrival_artifact_lineage_invalid")
        # An old decode audit only froze the mutable wrapper file.  It cannot
        # distinguish a later legal lifecycle timestamp from an immutable
        # raw/receipt/timing mutation, so it is intentionally not reusable.
        required_sha_fields = (
            "wrapper_capture_sha256",
            "wrapper_current_sha256",
            "wrapper_immutable_sha256",
            "arrival_content_sha256",
            "arrival_identity_sha256",
            "arrival_receipt_sha256",
            "arrival_timing_identity_sha256",
        )
        if any(field not in raw_artifact for field in required_sha_fields):
            raise ContractError(
                "aifs_training_runner_arrival_artifact_redecode_required"
            )
        wrapper_capture_sha = _sha(
            "arrival_artifact_wrapper_capture", raw_artifact.get(
                "wrapper_capture_sha256"
            )
        )
        if _sha("arrival_artifact_legacy_capture", raw_artifact.get("sha256")) != (
            wrapper_capture_sha
        ):
            raise ContractError(
                "aifs_training_runner_arrival_artifact_capture_sha_mismatch"
            )
        wrapper_audit_current_sha = _sha(
            "arrival_artifact_wrapper_current", raw_artifact.get(
                "wrapper_current_sha256"
            )
        )
        wrapper_immutable_sha = _sha(
            "arrival_artifact_wrapper_immutable", raw_artifact.get(
                "wrapper_immutable_sha256"
            )
        )
        arrival_content_sha = _sha(
            "arrival_artifact_content", raw_artifact.get(
                "arrival_content_sha256"
            )
        )
        arrival_identity_sha = _sha(
            "arrival_artifact_identity", raw_artifact.get(
                "arrival_identity_sha256"
            )
        )
        arrival_receipt_sha = _sha(
            "arrival_artifact_receipt", raw_artifact.get(
                "arrival_receipt_sha256"
            )
        )
        arrival_timing_sha = _sha(
            "arrival_artifact_timing", raw_artifact.get(
                "arrival_timing_identity_sha256"
            )
        )
        raw_payload_sha256s = raw_artifact.get("arrival_raw_payload_sha256s")
        if (
            not isinstance(raw_payload_sha256s, list)
            or not raw_payload_sha256s
            or any(
                _sha("arrival_artifact_raw", value) != str(value).lower()
                for value in raw_payload_sha256s
            )
        ):
            raise ContractError(
                "aifs_training_runner_arrival_artifact_raw_lineage_invalid"
            )
        expected_raw_payload_sha256s = sorted({
            str(value).lower() for value in raw_payload_sha256s
        })
        lifecycle_fields = raw_artifact.get(
            "allowed_wrapper_lifecycle_fields"
        )
        if lifecycle_fields != list(ARRIVAL_WRAPPER_LIFECYCLE_FIELDS):
            raise ContractError(
                "aifs_training_runner_arrival_artifact_lifecycle_invalid"
            )
        # Both hashes are intentionally equal at decode time.  A later
        # builder records the fresh wrapper hash separately after it has
        # revalidated the immutable identity.
        if wrapper_capture_sha != wrapper_audit_current_sha:
            raise ContractError(
                "aifs_training_runner_arrival_artifact_decode_wrapper_drift"
            )
        path = Path(path_text).expanduser().resolve(strict=True)
        payload = load_bounded_json(path, limit=MAX_ARRIVAL_ARTIFACT_BYTES)
        wrapper_current_sha = _file_sha256(path)
        if (
            not isinstance(payload, Mapping)
            or payload.get("status") != "CAPTURED"
            or not isinstance(payload.get("arrival"), Mapping)
        ):
            raise ContractError("aifs_training_runner_arrival_wrapper_invalid")
        if arrival_wrapper_immutable_sha256(payload) != wrapper_immutable_sha:
            raise ContractError(
                "aifs_training_runner_arrival_wrapper_immutable_sha_mismatch"
            )
        formal_consumed = payload.get("formal_consumed_at_utc")
        if formal_consumed is not None:
            formal_at = _utc(str(formal_consumed))
        else:
            formal_at = None
        arrival = validate_model_arrival_capture(dict(payload["arrival"]))
        if (
            str(arrival.get("content_sha256") or "").lower()
            != arrival_content_sha
            or arrival_identity_sha256(arrival) != arrival_identity_sha
            or sorted({
                str(value).lower()
                for value in arrival.get("raw_payload_sha256s") or []
            }) != expected_raw_payload_sha256s
            or str(arrival.get("receipt_sha256") or "").lower()
            != arrival_receipt_sha
            or arrival_timing_identity_sha256(arrival) != arrival_timing_sha
        ):
            raise ContractError(
                "aifs_training_runner_arrival_immutable_identity_mismatch"
            )
        if formal_at is not None and formal_at < _utc(
            str(arrival["received_at_utc"])
        ):
            raise ContractError(
                "aifs_training_runner_arrival_formal_consumption_before_receive"
            )
        key = (
            str(arrival.get("station_id") or "").strip().upper(),
            str(arrival.get("target_local_date") or "").strip(),
            str(arrival.get("run_initialization_utc") or "").strip(),
            str(arrival.get("valid_time_utc") or "").strip(),
        )
        if key in required:
            if key in found:
                raise ContractError("aifs_training_runner_arrival_scope_duplicate")
            receipt_path = Path(
                str(payload.get("transport_receipt_path") or "")
            ).expanduser().resolve(strict=True)
            receipt_path_text = str(receipt_path)
            receipt_raw_refs = verified_receipts.get(receipt_path_text)
            if receipt_raw_refs is None:
                receipt = load_bounded_json(
                    receipt_path,
                    limit=MAX_ARRIVAL_ARTIFACT_BYTES,
                )
                if (
                    not isinstance(receipt, Mapping)
                    or not isinstance(receipt.get("raw_receipts"), list)
                    or _file_sha256(receipt_path) != arrival_receipt_sha
                ):
                    raise ContractError(
                        "aifs_training_runner_retained_receipt_lineage_mismatch"
                    )
                raw_refs: list[str] = []
                receipt_hashes: set[str] = set()
                for raw_receipt in receipt["raw_receipts"]:
                    if not isinstance(raw_receipt, Mapping):
                        raise ContractError(
                            "aifs_training_runner_retained_raw_receipt_invalid"
                        )
                    raw_ref = Path(
                        str(raw_receipt.get("raw_object_ref") or "")
                    ).expanduser().resolve(strict=True)
                    raw_sha = _sha(
                        "retained_raw_receipt",
                        raw_receipt.get("raw_sha256"),
                    )
                    current_sha = verified_raw_paths.get(str(raw_ref))
                    if current_sha is None:
                        current_sha = _bounded_raw_sha256(raw_ref)
                        verified_raw_paths[str(raw_ref)] = current_sha
                    if current_sha != raw_sha:
                        raise ContractError(
                            "aifs_training_runner_retained_raw_lineage_mismatch"
                        )
                    raw_refs.append(str(raw_ref))
                    receipt_hashes.add(raw_sha)
                if receipt_hashes != set(expected_raw_payload_sha256s):
                    raise ContractError(
                        "aifs_training_runner_retained_raw_set_mismatch"
                    )
                receipt_raw_refs = tuple(sorted(raw_refs))
                verified_receipts[receipt_path_text] = receipt_raw_refs
            found.add(key)
            arrivals.append(arrival)
            lineage_inputs.append({
                "path": str(path),
                "wrapper_capture_sha256": wrapper_capture_sha,
                "wrapper_current_sha256": wrapper_current_sha,
                "wrapper_immutable_sha256": wrapper_immutable_sha,
                "arrival_content_sha256": arrival_content_sha,
                "arrival_identity_sha256": arrival_identity_sha,
                "arrival_raw_payload_sha256s": expected_raw_payload_sha256s,
                "arrival_receipt_sha256": arrival_receipt_sha,
                "arrival_timing_identity_sha256": arrival_timing_sha,
                "transport_receipt_path": receipt_path_text,
                "verified_retained_raw_paths": list(receipt_raw_refs),
                "formal_consumed_at_utc": (
                    _stamp(formal_at) if formal_at is not None else None
                ),
            })
    missing = required - found
    if missing:
        raise ContractError("aifs_training_runner_curve_arrival_missing")
    return arrivals, lineage_inputs


def build_training_facts_receipt(
    *,
    aifs_decode_path: Path,
    database: Path,
    as_of_utc: str,
    station_id: str | None = None,
) -> dict[str, Any]:
    """Return a content-addressed batch or explicit closed-truth no-op."""

    as_of = _stamp(_utc(as_of_utc))
    db = database.expanduser().resolve(strict=True)
    if not db.is_file():
        raise ContractError("aifs_training_runner_ssot_missing")
    assert_process_memory_budget(projected_bytes=MAX_AUDIT_BYTES)
    audit, audit_sha = _load_decode_audit(aifs_decode_path)
    requested_station = str(station_id or "").strip().upper() or None
    curves = _normalize_curves(audit, station_id=requested_station)
    truths = _read_closed_awc_truths(db, curves=curves, as_of_utc=as_of)
    readiness = _readiness_snapshot(curves, truths=truths, as_of_utc=as_of)
    station_inventory = _station_inventory(
        curves,
        truths=truths,
        as_of_utc=as_of,
    )
    eligible_curves, eligible_truths, skipped = _eligible_closed_curves(
        curves,
        truths=truths,
        as_of_utc=as_of,
    )
    input_identity = {
        "aifs_decode_audit_sha256": audit_sha,
        "eligible_closed_awc_truth_content_sha256s": sorted(
            str(row["content_sha256"]) for row in eligible_truths
        ),
        "eligible_curve_sha256s": sorted(
            str(row.get("curve_sha256") or "") for row in eligible_curves
        ),
    }
    boundary = {
        "reads_ssot_read_only": True,
        "downloads_raw": False,
        "writes_production_database": False,
        "creates_formal_provider_binding": False,
        "formal_p0_eligible": False,
        "calls_order_gateway": False,
        "places_orders": False,
        "orders_submitted": 0,
    }
    if not eligible_curves:
        state = (
            "NO_COMPLETE_AIFS_CURVES"
            if not curves
            else "NO_ELIGIBLE_CLOSED_AWC_TRUTH_NOOP"
        )
        identity = {
            "schema": SCHEMA,
            "state": state,
            "as_of_utc": as_of,
            "station_id": requested_station,
            "curve_count_scanned": len(curves),
            "fact_count": 0,
            "skipped_reason_counts": dict(sorted(skipped.items())),
            "readiness": readiness,
            "station_inventory": station_inventory,
            "inputs": input_identity,
            "boundary": boundary,
        }
        return {**identity, "content_sha256": content_sha256(identity)}
    arrivals, arrival_lineage_inputs = _load_matching_arrivals(
        audit,
        curves=eligible_curves,
    )
    input_identity = {
        **input_identity,
        "eligible_arrival_lineage": sorted(
            arrival_lineage_inputs,
            key=lambda item: (
                str(item["arrival_content_sha256"]),
                str(item["path"]),
            ),
        ),
    }
    batch = build_aifs_deterministic_training_facts(
        eligible_curves,
        arrivals,
        eligible_truths,
        as_of_utc=as_of,
    )
    identity = {
        "schema": SCHEMA,
        "state": "ACCUMULATING_STRICT_OOS",
        "as_of_utc": as_of,
        "station_id": requested_station,
        "curve_count_scanned": len(curves),
        "fact_count": int(batch["fact_count"]),
        "skipped_reason_counts": dict(sorted(skipped.items())),
        "readiness": readiness,
        "station_inventory": station_inventory,
        "inputs": input_identity,
        "training_batch": batch,
        "training_batch_content_sha256": batch["content_sha256"],
        "boundary": boundary,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def run(args: argparse.Namespace) -> dict[str, Any]:
    decode = (
        args.aifs_decode.expanduser().resolve(strict=True)
        if args.aifs_decode is not None
        else _default_decode_path()
    )
    as_of = args.as_of_utc or _stamp(datetime.now(timezone.utc))
    return build_training_facts_receipt(
        aifs_decode_path=decode,
        database=args.db,
        as_of_utc=as_of,
        station_id=args.station_id,
    )


def run_with_bounded_wait(args: argparse.Namespace) -> dict[str, Any]:
    """Optionally poll the read-only join until one fact is legal or time expires."""

    max_wait = int(args.wait_max_seconds or 0)
    poll = int(args.poll_seconds or MAX_POLL_SECONDS)
    if max_wait < 0 or max_wait > MAX_WAIT_SECONDS:
        raise ContractError("aifs_training_runner_wait_limit_invalid")
    if poll < 1 or poll > MAX_POLL_SECONDS:
        raise ContractError("aifs_training_runner_poll_interval_invalid")
    if max_wait and args.as_of_utc:
        raise ContractError("aifs_training_runner_wait_fixed_asof_forbidden")
    if max_wait == 0:
        return run(args)
    started = datetime.now(timezone.utc)
    deadline = started.timestamp() + max_wait
    attempts = 0
    while True:
        attempts += 1
        receipt = run(args)
        if receipt["fact_count"] > 0:
            break
        now = datetime.now(timezone.utc)
        remaining = deadline - now.timestamp()
        if remaining <= 0:
            break
        readiness = receipt.get("readiness")
        not_before_raw = (
            readiness.get("next_action_not_before_utc")
            if isinstance(readiness, Mapping)
            else None
        )
        not_before = _utc(not_before_raw) if not_before_raw else now
        delay = max(0.0, (not_before - now).total_seconds())
        # Poll at most once per minute even when the next settlement boundary
        # is hours away; every iteration reopens SQLite in query-only mode.
        time.sleep(min(float(poll), remaining, max(delay, float(poll))))
    wait_identity = {
        "attempt_count": attempts,
        "wait_max_seconds": max_wait,
        "poll_seconds": poll,
        "wait_started_at_utc": _stamp(started),
        "wait_finished_at_utc": _stamp(datetime.now(timezone.utc)),
        "wait_outcome": (
            "FACTS_READY" if receipt["fact_count"] > 0
            else "BOUNDED_WAIT_EXPIRED"
        ),
    }
    identity = {key: value for key, value in receipt.items() if key != "content_sha256"}
    identity["bounded_wait"] = wait_identity
    return {**identity, "content_sha256": content_sha256(identity)}


def _blocked_receipt(*, as_of_utc: str | None, station_id: str | None, error: Exception) -> dict[str, Any]:
    normalized_as_of: str | None = None
    if as_of_utc:
        try:
            normalized_as_of = _stamp(_utc(as_of_utc))
        except ContractError:
            # Preserve a machine-readable fail-closed receipt even when the
            # caller's clock itself was malformed.
            normalized_as_of = None
    identity = {
        "schema": SCHEMA,
        "state": "BLOCKED_FAIL_CLOSED",
        "as_of_utc": normalized_as_of,
        "station_id": str(station_id or "").strip().upper() or None,
        "blockers": [f"{type(error).__name__}:{error}"],
        "fact_count": 0,
        "boundary": {
            "reads_ssot_read_only": True,
            "downloads_raw": False,
            "writes_production_database": False,
            "creates_formal_provider_binding": False,
            "formal_p0_eligible": False,
            "calls_order_gateway": False,
            "places_orders": False,
            "orders_submitted": 0,
        },
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--aifs-decode", type=Path)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--as-of-utc")
    parser.add_argument("--station-id")
    parser.add_argument(
        "--wait-max-seconds",
        type=int,
        default=0,
        help="bounded read-only retry window (0 disables; maximum 14400)",
    )
    parser.add_argument(
        "--poll-seconds",
        type=int,
        default=60,
        help="read-only retry cadence, 1..60 seconds",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    output = args.output.expanduser().resolve()
    try:
        receipt = run_with_bounded_wait(args)
        wrote = write_bounded_json_if_changed(output, receipt, limit=MAX_OUTPUT_BYTES)
        print(json.dumps({
            "state": receipt["state"],
            "fact_count": receipt["fact_count"],
            "output": str(output),
            "content_sha256": receipt["content_sha256"],
            "wrote": wrote,
            "writes_production_database": False,
            "calls_order_gateway": False,
            "orders_submitted": 0,
        }, ensure_ascii=False, sort_keys=True))
        return 0
    except (ContractError, ResourceBoundError, OSError, ValueError) as exc:
        blocked = _blocked_receipt(
            as_of_utc=args.as_of_utc,
            station_id=args.station_id,
            error=exc,
        )
        write_bounded_json_if_changed(output, blocked, limit=MAX_OUTPUT_BYTES)
        print(json.dumps(blocked, ensure_ascii=False, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
