"""Append-only facts joining native-ensemble P0, TAF, and settled truth."""

from __future__ import annotations

import json
import math
import sqlite3
import time
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .contracts import ContractError, content_sha256
from .event_contracts import ForecastRevision
from .ssot import (
    native_ensemble_foundation_schema_status,
)
from .taf_native_ensemble_oos import EXACT_JOIN_SCHEMA
from .taf_oos_contract import HISTORICAL_OOS_TEMPORAL_SCOPE


TAF_INBOX_SCHEMA = "bayes_basket_target_taf_revision_inbox_v1"
# Must admit the full capture-side inbox (whole-scope retention, 2048 slots);
# a smaller ingest cap here would fail-close the very window that keeps every
# revision chain's root resident.
MAX_TAF_CAPTURES = 2048
MAX_FORWARD_TAF_CORRECTIONS_PER_DAY = 16
FORMAL_STATION_LANE_TRUTH = "FORMAL_STATION_LANE_TRUTH"


def _historical_taf_correction_kind(raw_taf: Any) -> str | None:
    """Return the explicit bulletin correction marker, never a substring guess."""

    tokens = str(raw_taf or "").upper().split()
    if len(tokens) >= 2 and tokens[0] == "TAF" and tokens[1] in {"AMD", "COR"}:
        return tokens[1]
    return None


def _historical_taf_truth_authority(source_lane: str) -> dict[str, Any]:
    lane = str(source_lane or "").strip().upper()
    # P0-M calibration is executable lineage, not the retired historical
    # comparison lane.  Only official AWC may supply it for any station.
    if lane != "AWC":
        raise ContractError("historical_taf_station_source_lane_invalid")
    return {
        "station_source_lane": "AWC",
        "evidence_source_lane": "AWC",
        "truth_label_authority": FORMAL_STATION_LANE_TRUTH,
        "reference_only": False,
        "enters_realtime_truth": False,
        "enters_p1": False,
        "enters_hard_no": False,
        "replacement_required_when_canonical_history_available": False,
    }


def _utc(name: str, value: Any) -> str:
    raw = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{name}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"{name}_timezone_required")
    return parsed.isoformat().replace("+00:00", "Z")


def _finite(name: str, value: Any) -> float:
    if isinstance(value, bool):
        raise ContractError(f"{name}_invalid")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{name}_invalid") from exc
    if not math.isfinite(result):
        raise ContractError(f"{name}_invalid")
    return result


def _sha(name: str, value: Any) -> str:
    result = str(value or "").strip().lower()
    if (
        len(result) != 64
        or any(char not in "0123456789abcdef" for char in result)
    ):
        raise ContractError(f"{name}_invalid")
    return result


def _insert_content_addressed(
    conn: sqlite3.Connection,
    row: Mapping[str, Any],
) -> int:
    natural = conn.execute(
        "SELECT content_sha256 FROM calibration_forward_taf_capture "
        "WHERE station_id=? AND event_date=? AND revision_id=?",
        (
            row["station_id"],
            row["event_date"],
            row["revision_id"],
        ),
    ).fetchone()
    if natural is not None:
        if str(natural[0]) != str(row["content_sha256"]):
            raise ContractError("forward_taf_capture_content_conflict")
        return 0
    existing = conn.execute(
        "SELECT content_sha256 FROM calibration_forward_taf_capture "
        "WHERE taf_capture_id=?",
        (row["taf_capture_id"],),
    ).fetchone()
    if existing is not None:
        if str(existing[0]) != str(row["content_sha256"]):
            raise ContractError("forward_taf_capture_content_conflict")
        return 0
    columns = tuple(row)
    conn.execute(
        "INSERT INTO calibration_forward_taf_capture("
        + ",".join(columns)
        + ") VALUES("
        + ",".join("?" for _ in columns)
        + ")",
        tuple(row[column] for column in columns),
    )
    return 1


def ingest_forward_taf_inbox(
    conn: sqlite3.Connection,
    inbox: Mapping[str, Any],
) -> dict[str, Any]:
    """Persist bounded official forward TAF receipts without touching P0-M."""

    if native_ensemble_foundation_schema_status(conn)["ok"] is not True:
        raise ContractError("native_ensemble_foundation_schema_required")
    if inbox.get("schema") != TAF_INBOX_SCHEMA:
        raise ContractError("forward_taf_inbox_schema_invalid")
    captures = inbox.get("captures")
    if (
        not isinstance(captures, list)
        or len(captures) > MAX_TAF_CAPTURES
    ):
        raise ContractError("forward_taf_capture_count_invalid")
    # Three phases under one name.  ``p0m_taf_fact_ingress`` measured
    # 68.0s of a 620s cycle on 2026-09-07 and the profile could say no
    # more than that, so the first fix was chosen by reading the code and
    # was wrong about the magnitude -- the phase that looked expensive is
    # 100ms on a warm copy.  A receipt that splits its own time costs one
    # clock read per phase and ends that guessing.
    phase_ms: dict[str, float] = {}
    _validate_started = time.perf_counter()
    rows: list[dict[str, Any]] = []
    rejected_timestamp_captures: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    # Asked once for the whole inbox, not once per capture.
    #
    # Three of the four lookups below do not vary with the capture at all --
    # the registry's row count, whether the market registry table exists --
    # and the fourth varies only by (station, event_date), of which 2048
    # captures hold about 500 distinct pairs and about 45 distinct stations.
    # Nothing in this loop writes, so none of the four answers can change
    # while it runs.
    #
    # It matters because the inbox is a rolling window that has grown to its
    # 2048 ceiling and now spans thirteen days: the per-capture form made
    # this ingest's cost scale with the window rather than with the work,
    # and the DISTINCT scan alone measured 5.4ms warm -- 11.1s a cycle,
    # every cycle, for an answer it had already computed.
    registry_station_count = int(conn.execute(
        "SELECT COUNT(*) FROM calibration_station_registry"
    ).fetchone()[0])
    market_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='basket_target_market_registry'"
    ).fetchone()
    registered_stations: dict[str, bool] = {}
    scope_timezones: dict[tuple[str, str], list[Any]] = {}
    for capture in captures:
        if not isinstance(capture, Mapping):
            raise ContractError("forward_taf_capture_invalid")
        source_capture_sha = _sha(
            "forward_taf_capture_content_sha256",
            capture.get("content_sha256"),
        )
        capture_identity = {
            str(key): value
            for key, value in capture.items()
            if key != "content_sha256"
        }
        if content_sha256(capture_identity) != source_capture_sha:
            raise ContractError("forward_taf_capture_sha_mismatch")
        revision = capture.get("forecast_revision")
        fields = capture.get("taf_fields")
        if (
            capture.get("source_class")
            != "official_forward_native_receipt"
            or not isinstance(revision, Mapping)
            or not isinstance(fields, Mapping)
        ):
            raise ContractError("forward_taf_native_receipt_required")
        normalized_revision = ForecastRevision.from_mapping(revision)
        station = str(
            capture.get("station_id")
            or revision.get("station_id")
            or ""
        ).strip().upper()
        event_date = str(
            capture.get("event_date")
            or revision.get("event_date")
            or ""
        ).strip()
        timezone_name = str(
            capture.get("settlement_timezone") or ""
        ).strip()
        revision_id = str(revision.get("revision_id") or "").strip()
        natural_key = (station, event_date, revision_id)
        if (
            not all(natural_key)
            or not timezone_name
            or natural_key in seen
        ):
            raise ContractError("forward_taf_capture_scope_invalid")
        seen.add(natural_key)
        if station not in registered_stations:
            registered_stations[station] = conn.execute(
                "SELECT 1 FROM calibration_station_registry "
                "WHERE station_id=?",
                (station,),
            ).fetchone() is not None
        station_registered = registered_stations[station]
        scope_key = (station, event_date)
        if scope_key not in scope_timezones:
            scope_timezones[scope_key] = (
                conn.execute(
                    "SELECT DISTINCT settlement_timezone "
                    "FROM basket_target_market_registry "
                    "WHERE station_id=? AND event_date=?",
                    (station, event_date),
                ).fetchall()
                if market_table is not None
                else []
            )
        timezone_rows = scope_timezones[scope_key]
        station_count = registry_station_count
        if station_count and not station_registered:
            raise ContractError("forward_taf_station_unregistered")
        if timezone_rows and (
            len(timezone_rows) != 1
            or str(timezone_rows[0][0]) != timezone_name
        ):
            raise ContractError("forward_taf_market_scope_invalid")
        received_at = _utc(
            "forward_taf_received_at_utc",
            revision.get("received_at_utc"),
        )
        available_at = _utc(
            "forward_taf_provider_available_at_utc",
            capture.get("provider_available_at_utc") or received_at,
        )
        issued_at = _utc(
            "forward_taf_issued_at_utc",
            revision.get("issued_at_utc"),
        )
        valid_from = _utc(
            "forward_taf_valid_from_utc",
            revision.get("valid_from_utc"),
        )
        valid_until = _utc(
            "forward_taf_valid_until_utc",
            revision.get("valid_until_utc"),
        )
        if not (
            issued_at <= available_at <= received_at
            and valid_from < valid_until
        ):
            # Timestamp corruption is local to one provider receipt.  Keep
            # the invalid capture in the bounded inbox as audit evidence, but
            # do not let one station poison valid forward facts for every
            # other station.  The affected scope has no committed TAF fact
            # and therefore still fails closed for P0-M.
            rejected_timestamp_captures.append({
                "station_id": station,
                "event_date": event_date,
                "revision_id": revision_id,
                "source_capture_sha256": source_capture_sha,
                "reason": "forward_taf_timestamp_order_invalid",
            })
            continue
        tx_raw = fields.get("tx_c")
        tx_c = (
            None
            if tx_raw is None
            else _finite("forward_taf_tx_c", tx_raw)
        )
        cloud = _finite(
            "forward_taf_cloud_cover_fraction",
            fields.get("cloud_cover_fraction"),
        )
        precip = _finite(
            "forward_taf_precipitation_probability",
            fields.get("precipitation_probability"),
        )
        wind = _finite(
            "forward_taf_wind_speed_mps",
            fields.get("wind_speed_mps"),
        )
        if (
            not 0.0 <= cloud <= 1.0
            or not 0.0 <= precip <= 1.0
            or wind < 0.0
        ):
            raise ContractError("forward_taf_fields_invalid")
        identity = {
            "station_id": station,
            "event_date": event_date,
            "settlement_timezone": timezone_name,
            "revision_id": revision_id,
            "revision_kind": str(
                normalized_revision.revision_kind
            ),
            "revision_sequence": normalized_revision.sequence,
            "issued_at_utc": issued_at,
            "received_at_utc": received_at,
            "provider_available_at_utc": available_at,
            "valid_from_utc": valid_from,
            "valid_until_utc": valid_until,
            "source_object_sha256": _sha(
                "forward_taf_source_object_sha256",
                revision.get("source_object_sha256"),
            ),
            "source_capture_sha256": source_capture_sha,
            "tx_c": tx_c,
            "cloud_cover_fraction": cloud,
            "precipitation_probability": precip,
            "wind_speed_mps": wind,
            "source_class": "official_forward_native_receipt",
            "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
        }
        if (
            not identity["revision_kind"]
            or identity["revision_sequence"] < 1
        ):
            raise ContractError("forward_taf_revision_invalid")
        row_sha = content_sha256(identity)
        rows.append({
            "taf_capture_id": "taf-capture-" + row_sha[:32],
            **identity,
            # Derived query index only.  Keep it outside the immutable
            # content identity so pre-migration TX-present facts retain
            # their original append-only hashes.
            "tx_available": int(tx_c is not None),
            "content_sha256": row_sha,
        })
    phase_ms["validate"] = round(
        (time.perf_counter() - _validate_started) * 1000.0, 1
    )
    _capture_started = time.perf_counter()
    wrote = 0
    fresh_scopes: set[tuple[str, str]] = set()
    conn.execute("SAVEPOINT forward_taf_capture_ingress")
    try:
        for row in rows:
            written = _insert_content_addressed(conn, row)
            wrote += written
            if written:
                # A genuinely new issuance (not a replay): the scopes named
                # here are what an arrival-triggered recompile should refresh.
                fresh_scopes.add((
                    str(row["station_id"]), str(row["event_date"])
                ))
    except Exception:
        conn.execute("ROLLBACK TO forward_taf_capture_ingress")
        conn.execute("RELEASE forward_taf_capture_ingress")
        raise
    conn.execute("RELEASE forward_taf_capture_ingress")
    phase_ms["capture_insert"] = round(
        (time.perf_counter() - _capture_started) * 1000.0, 1
    )
    _projection_started = time.perf_counter()
    # Projection into the cross-OOS member archive rides behind every capture
    # ingest so the TAF candidate's day coverage stays as dense as capture
    # itself.  Its own savepoint: a projection defect must not cost captures.
    conn.execute("SAVEPOINT taf_point_projection")
    try:
        projection = project_taf_point_member_runs(conn)
        conn.execute("RELEASE taf_point_projection")
    except Exception as exc:  # noqa: BLE001 -- reported, never masks capture
        conn.execute("ROLLBACK TO taf_point_projection")
        conn.execute("RELEASE taf_point_projection")
        projection = {
            "schema": "taf_point_member_run_projection_v1",
            "state": "PROJECTION_FAILED",
            "error": f"{type(exc).__name__}:{exc}",
        }
    phase_ms["point_projection"] = round(
        (time.perf_counter() - _projection_started) * 1000.0, 1
    )
    return {
        "schema": "forward_taf_capture_ingress_v1",
        "phase_ms": phase_ms,
        "member_run_projection": projection,
        "fresh_scopes": [
            {"station_id": station, "event_date": day}
            for station, day in sorted(fresh_scopes)
        ],
        "evidence_role": "observational_candidate_not_oos",
        "state": (
            "COMMITTED_WITH_REJECTED_CAPTURES"
            if rejected_timestamp_captures
            else "COMMITTED"
        ),
        "capture_count": len(rows),
        "wrote_count": wrote,
        "replay_count": len(rows) - wrote,
        "rejected_count": len(rejected_timestamp_captures),
        "rejected_captures": rejected_timestamp_captures,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "live_submit_authorized": False,
    }


def ingest_historical_taf_calibration(
    conn: sqlite3.Connection,
    artifact: Mapping[str, Any],
) -> dict[str, Any]:
    """Persist official date-addressed TAF as offline calibration facts."""

    if native_ensemble_foundation_schema_status(conn)["ok"] is not True:
        raise ContractError("native_ensemble_foundation_schema_required")
    identity = {
        str(key): value
        for key, value in artifact.items()
        if key != "content_sha256"
    }
    source_artifact_sha = _sha(
        "historical_taf_artifact_sha256",
        artifact.get("content_sha256"),
    )
    boundary = artifact.get("boundary")
    samples = artifact.get("samples")
    try:
        declared_sample_count = int(artifact.get("sample_count", -1))
    except (TypeError, ValueError, OverflowError):
        declared_sample_count = -1
    if (
        artifact.get("schema")
        != "bayes_basket_awc_taf_calibration_dataset_v1"
        or artifact.get("source_class")
        != "official_date_addressed_historical_replay"
        or content_sha256(identity) != source_artifact_sha
        or not isinstance(boundary, Mapping)
        or boundary.get("native_local_receipt") is not False
        or boundary.get(
            "official_awc_database_availability_preserved"
        ) is not True
        or boundary.get("historical_replay_only") is not True
        or boundary.get("truth_days_closed") is not True
        or not isinstance(samples, list)
        # ``0`` is a valid empty capture (for example, the official AWC
        # endpoint returned no usable TAF/truth pair for a closed day).  Do
        # not let truthiness turn that valid count into ``-1`` and reject the
        # whole bounded 45-station ingest.
        or declared_sample_count != len(samples)
    ):
        raise ContractError("historical_taf_artifact_invalid")
    station = str(artifact.get("station_id") or "").strip().upper()
    station_source_lane = str(artifact.get("source_lane") or "").strip().upper()
    truth_label_authority = str(
        artifact.get("truth_label_authority") or ""
    ).strip()
    timezone_name = str(
        artifact.get("settlement_timezone") or ""
    ).strip()
    station_row = conn.execute(
        "SELECT timezone_name,source_lane FROM calibration_station_registry "
        "WHERE station_id=?",
        (station,),
    ).fetchone()
    market_timezones = conn.execute(
        "SELECT DISTINCT settlement_timezone "
        "FROM basket_target_market_registry "
        "WHERE station_id=? AND settlement_timezone<>''",
        (station,),
    ).fetchall()
    # The market registry is append-only and therefore retains retired lane
    # rows.  The current authority is the latest active/source-eligible AWC
    # snapshot; no retired AMSC row can make a historical TAF executable.
    try:
        market_lane_row = conn.execute(
            "SELECT UPPER(source_lane) "
            "FROM basket_target_market_registry "
            "WHERE station_id=? AND source_lane<>'' "
            "AND source_eligible=1 AND market_active=1 "
            "AND market_accepting_orders=1 "
            "ORDER BY refreshed_at DESC, rowid DESC LIMIT 1",
            (station,),
        ).fetchone()
    except sqlite3.OperationalError:
        # Small isolated fixtures may expose only the legacy three-column
        # market table.  They have no active snapshot clock, so retain the
        # registry lane as the only available authority.
        market_lane_row = None
    expected_timezone = (
        str(market_timezones[0][0])
        if len(market_timezones) == 1
        else str(station_row[0])
        if station_row is not None and not market_timezones
        else ""
    )
    registry_lane = (
        str(station_row[1]).strip().upper()
        if station_row is not None
        else ""
    )
    expected_lane = (
        str(market_lane_row[0]).strip().upper()
        if market_lane_row is not None and str(market_lane_row[0]).strip()
        else registry_lane
    )
    expected_authority = (
        _historical_taf_truth_authority(expected_lane)
        if expected_lane == "AWC"
        else None
    )
    if (
        not station
        or not timezone_name
        or station_row is None
        or expected_timezone != timezone_name
        or station_source_lane != expected_lane
        or expected_authority is None
        or truth_label_authority != FORMAL_STATION_LANE_TRUTH
        or truth_label_authority != str(expected_authority["truth_label_authority"])
        or artifact.get("truth_authority") != expected_authority
        or (
            market_lane_row is not None
            and str(market_lane_row[0]).strip().upper() != expected_lane
        )
    ):
        raise ContractError("historical_taf_station_lane_authority_invalid")
    boundary_reference_only = artifact.get("boundary")
    if (
        not isinstance(boundary_reference_only, Mapping)
        or boundary_reference_only.get("reference_only")
        != expected_authority["reference_only"]
        or boundary_reference_only.get("enters_realtime_truth") is not False
        or boundary_reference_only.get("enters_p1") is not False
        or boundary_reference_only.get("enters_hard_no") is not False
        or boundary_reference_only.get(
            "replacement_required_when_canonical_history_available"
        )
        != expected_authority[
            "replacement_required_when_canonical_history_available"
        ]
    ):
        raise ContractError("historical_taf_truth_authority_boundary_invalid")
    tz = ZoneInfo(timezone_name)
    rows: list[dict[str, Any]] = []
    seen_dates: set[str] = set()
    skipped_after_anchor: list[str] = []
    correction_dates: set[str] = set()
    for sample in samples:
        if not isinstance(sample, Mapping):
            raise ContractError("historical_taf_sample_invalid")
        event_date = str(sample.get("event_date") or "")
        taf = sample.get("taf")
        if not event_date or not isinstance(taf, Mapping):
            raise ContractError("historical_taf_sample_invalid")
        if not str(taf.get("raw_taf") or "").strip():
            raise ContractError("historical_taf_raw_bulletin_required")
        if _historical_taf_correction_kind(taf.get("raw_taf")) is not None:
            correction_dates.add(event_date)
    for sample in samples:
        if not isinstance(sample, Mapping):
            raise ContractError("historical_taf_sample_invalid")
        event_date = str(sample.get("event_date") or "")
        taf = sample.get("taf")
        receipts = sample.get("receipts")
        sample_truth = sample.get("truth")
        if (
            str(sample.get("station_id") or "").strip().upper()
            != station
            or str(sample.get("settlement_timezone") or "")
            != timezone_name
            or not event_date
            or event_date in seen_dates
            or not isinstance(taf, Mapping)
            or not isinstance(receipts, Mapping)
            or str(sample.get("source_lane") or "").strip().upper()
            != expected_lane
            or str(sample.get("truth_label_authority") or "")
            != truth_label_authority
            or sample.get("truth_authority") != expected_authority
            or sample.get("reference_only")
            != expected_authority["reference_only"]
            or not isinstance(sample_truth, Mapping)
            or str(sample_truth.get("source_lane") or "").strip().upper()
            != "AWC"
            or str(sample_truth.get("truth_label_authority") or "")
            != truth_label_authority
        ):
            raise ContractError("historical_taf_sample_lane_authority_invalid")
        # AMD/COR is a whole station-day halt.  A normal TAF from the same
        # archive day is not a permissible fallback, so skip every sample for
        # that day; the companion append-only quarantine table records why.
        if event_date in correction_dates:
            continue
        seen_dates.add(event_date)
        day_start = datetime.fromisoformat(event_date).replace(
            tzinfo=tz
        ).astimezone(timezone.utc)
        day_end = (
            datetime.fromisoformat(event_date).replace(tzinfo=tz)
            + timedelta(days=1)
        ).astimezone(timezone.utc)
        anchor = _utc(
            "historical_taf_decision_anchor_utc",
            sample.get("decision_anchor_utc"),
        )
        issued = _utc(
            "historical_taf_issued_at_utc",
            taf.get("issue_time_utc"),
        )
        available = _utc(
            "historical_taf_provider_available_at_utc",
            taf.get("available_time_utc"),
        )
        tx_valid = _utc(
            "historical_taf_tx_valid_at_utc",
            taf.get("tx_valid_at_utc"),
        )
        if issued > available:
            raise ContractError(
                "historical_taf_timestamp_order_invalid"
            )
        if available > anchor:
            skipped_after_anchor.append(event_date)
            continue
        if not (
            day_start
            <= datetime.fromisoformat(
                tx_valid.replace("Z", "+00:00")
            )
            < day_end
            and datetime.fromisoformat(
                anchor.replace("Z", "+00:00")
            )
            < day_end
        ):
            raise ContractError(
                "historical_taf_timestamp_order_invalid"
            )
        cloud = _finite(
            "historical_taf_cloud_cover_fraction",
            taf.get("cloud_cover_fraction"),
        )
        precip = _finite(
            "historical_taf_precipitation_probability",
            taf.get("precipitation_probability"),
        )
        wind = _finite(
            "historical_taf_wind_speed_mps",
            taf.get("wind_speed_mps"),
        )
        if not 0.0 <= cloud <= 1.0 or not 0.0 <= precip <= 1.0 or wind < 0:
            raise ContractError("historical_taf_fields_invalid")
        row_identity = {
            "station_id": station,
            "event_date": event_date,
            "settlement_timezone": timezone_name,
            "decision_anchor_utc": anchor,
            "issued_at_utc": issued,
            "provider_available_at_utc": available,
            "tx_c": _finite("historical_taf_tx_c", taf.get("tx_c")),
            "tx_valid_at_utc": tx_valid,
            "cloud_cover_fraction": cloud,
            "precipitation_probability": precip,
            "wind_speed_mps": wind,
            "raw_taf_sha256": _sha(
                "historical_taf_raw_sha256",
                taf.get("raw_taf_sha256"),
            ),
            "taf_response_sha256": _sha(
                "historical_taf_response_sha256",
                receipts.get("taf_response_sha256"),
            ),
            "source_sample_sha256": content_sha256(sample),
            "source_class": (
                "official_date_addressed_historical_replay"
            ),
            "truth_label_authority": truth_label_authority,
            "authority_scope": "CALIBRATION_TRAIN_ONLY",
        }
        row_sha = content_sha256(row_identity)
        rows.append({
            "historical_taf_capture_id": (
                "historical-taf-" + row_sha[:32]
            ),
            **row_identity,
            "content_sha256": row_sha,
        })
    wrote = 0
    conn.execute("SAVEPOINT historical_taf_ingress")
    try:
        for row in rows:
            natural = conn.execute(
                "SELECT content_sha256 FROM "
                "calibration_historical_taf_capture "
                "WHERE station_id=? AND event_date=? "
                "AND source_sample_sha256=?",
                (
                    row["station_id"],
                    row["event_date"],
                    row["source_sample_sha256"],
                ),
            ).fetchone()
            if natural is not None:
                if str(natural[0]) != str(row["content_sha256"]):
                    raise ContractError(
                        "historical_taf_content_conflict"
                    )
                continue
            columns = tuple(row)
            conn.execute(
                "INSERT INTO calibration_historical_taf_capture("
                + ",".join(columns)
                + ") VALUES("
                + ",".join("?" for _ in columns)
                + ")",
                tuple(row[column] for column in columns),
            )
            wrote += 1
    except Exception:
        conn.execute("ROLLBACK TO historical_taf_ingress")
        conn.execute("RELEASE historical_taf_ingress")
        raise
    conn.execute("RELEASE historical_taf_ingress")
    conn.execute("SAVEPOINT taf_point_projection_hist")
    try:
        projection = project_taf_point_member_runs(conn)
        conn.execute("RELEASE taf_point_projection_hist")
    except Exception as exc:  # noqa: BLE001 -- reported, never masks capture
        conn.execute("ROLLBACK TO taf_point_projection_hist")
        conn.execute("RELEASE taf_point_projection_hist")
        projection = {
            "schema": "taf_point_member_run_projection_v1",
            "state": "PROJECTION_FAILED",
            "error": f"{type(exc).__name__}:{exc}",
        }
    return {
        "schema": "historical_taf_calibration_ingress_v1",
        "member_run_projection": projection,
        "station_id": station,
        "capture_count": len(rows),
        "wrote_count": wrote,
        "replay_count": len(rows) - wrote,
        "skipped_after_anchor_count": len(skipped_after_anchor),
        "skipped_after_anchor_dates": skipped_after_anchor,
        "skipped_correction_station_day_count": len(correction_dates),
        "skipped_correction_station_days": sorted(correction_dates),
        "authority_scope": "CALIBRATION_TRAIN_ONLY",
        "source_lane": expected_lane,
        "truth_label_authority": truth_label_authority,
        "source_artifact_sha256": source_artifact_sha,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "live_submit_authorized": False,
    }


def ingest_historical_taf_correction_quarantine(
    conn: sqlite3.Connection,
    artifact: Mapping[str, Any],
    *,
    event_dates: set[str] | None = None,
) -> dict[str, Any]:
    """Append AWC historical AMD/COR station-day halts from retained raw TAF.

    The raw bulletin is deliberately parsed only for its leading ``TAF AMD``
    or ``TAF COR`` marker.  No forecast value, METAR value, or replacement
    TAF is derived here.  The resulting fact blocks the whole station day in
    the exact join while retaining all prior immutable receipt rows.
    """

    if native_ensemble_foundation_schema_status(conn)["ok"] is not True:
        raise ContractError("native_ensemble_foundation_schema_required")
    identity = {key: value for key, value in artifact.items() if key != "content_sha256"}
    artifact_sha = _sha(
        "historical_taf_correction_artifact_sha256",
        artifact.get("content_sha256"),
    )
    if content_sha256(identity) != artifact_sha:
        raise ContractError("historical_taf_correction_artifact_sha_mismatch")
    if (
        artifact.get("schema") != "bayes_basket_awc_taf_calibration_dataset_v1"
        or artifact.get("source_class") != "official_date_addressed_historical_replay"
        or str(artifact.get("source_lane") or "").upper() != "AWC"
        or artifact.get("truth_label_authority") != FORMAL_STATION_LANE_TRUTH
        or not isinstance(artifact.get("samples"), list)
    ):
        raise ContractError("historical_taf_correction_artifact_invalid")
    station = str(artifact.get("station_id") or "").strip().upper()
    timezone_name = str(artifact.get("settlement_timezone") or "").strip()
    authority = _historical_taf_truth_authority("AWC")
    if (
        not station
        or not timezone_name
        or artifact.get("truth_authority") != authority
    ):
        raise ContractError("historical_taf_correction_authority_invalid")
    allowed_dates = None if event_dates is None else {
        str(value).strip() for value in event_dates if str(value).strip()
    }
    rows: list[dict[str, Any]] = []
    for sample in artifact["samples"]:
        if not isinstance(sample, Mapping):
            raise ContractError("historical_taf_correction_sample_invalid")
        taf = sample.get("taf")
        if not isinstance(taf, Mapping):
            raise ContractError("historical_taf_correction_taf_missing")
        event_date = str(sample.get("event_date") or "")
        kind = _historical_taf_correction_kind(taf.get("raw_taf"))
        if kind is None:
            continue
        if allowed_dates is not None and event_date not in allowed_dates:
            continue
        if (
            str(sample.get("station_id") or "").strip().upper() != station
            or str(sample.get("settlement_timezone") or "") != timezone_name
            or str(sample.get("source_lane") or "").upper() != "AWC"
            or sample.get("truth_label_authority") != FORMAL_STATION_LANE_TRUTH
            or sample.get("truth_authority") != authority
            or not event_date
        ):
            raise ContractError("historical_taf_correction_sample_scope_invalid")
        row_identity = {
            "station_id": station,
            "event_date": event_date,
            "settlement_timezone": timezone_name,
            "correction_kind": kind,
            "raw_taf_sha256": _sha(
                "historical_taf_correction_raw_sha256",
                taf.get("raw_taf_sha256"),
            ),
            "source_sample_sha256": content_sha256(sample),
            "source_artifact_sha256": artifact_sha,
            "source_class": "official_date_addressed_historical_replay",
            "authority_scope": "CALIBRATION_TRAIN_ONLY",
        }
        row_sha = content_sha256(row_identity)
        rows.append({
            "historical_taf_correction_quarantine_id": "historical-taf-correction-" + row_sha[:32],
            **row_identity,
            "content_sha256": row_sha,
        })
    wrote = 0
    conn.execute("SAVEPOINT historical_taf_correction_quarantine")
    try:
        for row in rows:
            existing = conn.execute(
                "SELECT content_sha256,settlement_timezone,correction_kind,"
                "raw_taf_sha256,source_class,authority_scope "
                "FROM calibration_historical_taf_correction_quarantine "
                "WHERE station_id=? AND event_date=? AND source_sample_sha256=?",
                (row["station_id"], row["event_date"], row["source_sample_sha256"]),
            ).fetchone()
            if existing is not None:
                # A bounded retry can rebuild the outer archive (and thus
                # change source_artifact_sha256) while retaining the exact
                # same AMD/COR bulletin.  The source sample is the immutable
                # evidence identity, so preserve the first append-only row;
                # only a change to the stable station-day/raw-bulletin
                # contract is a real conflict.
                stable_match = (
                    str(existing[1]) == str(row["settlement_timezone"])
                    and str(existing[2]) == str(row["correction_kind"])
                    and str(existing[3]) == str(row["raw_taf_sha256"])
                    and str(existing[4]) == str(row["source_class"])
                    and str(existing[5]) == str(row["authority_scope"])
                )
                if not stable_match:
                    raise ContractError("historical_taf_correction_content_conflict")
                continue
            columns = tuple(row)
            conn.execute(
                "INSERT INTO calibration_historical_taf_correction_quarantine("
                + ",".join(columns) + ") VALUES(" + ",".join("?" for _ in columns) + ")",
                tuple(row[column] for column in columns),
            )
            wrote += 1
    except Exception:
        conn.execute("ROLLBACK TO historical_taf_correction_quarantine")
        conn.execute("RELEASE historical_taf_correction_quarantine")
        raise
    conn.execute("RELEASE historical_taf_correction_quarantine")
    return {
        "schema": "historical_taf_correction_quarantine_ingress_v1",
        "station_id": station,
        "correction_count": len(rows),
        "wrote_count": wrote,
        "replay_count": len(rows) - wrote,
        "authority_scope": "CALIBRATION_TRAIN_ONLY",
        "source_lane": "AWC",
        "source_artifact_sha256": artifact_sha,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "live_submit_authorized": False,
    }


def ingest_historical_taf_calibration_with_quarantine(
    conn: sqlite3.Connection,
    artifact: Mapping[str, Any],
) -> dict[str, Any]:
    """Atomically append historical AWC correction halts and usable TAF facts.

    A normal historical TAF row is append-only.  Therefore discovering an
    AMD/COR bulletin after an ordinary row was already retained must add the
    station-day halt in the *same* ingress operation; merely skipping the new
    correction bulletin would leave the earlier row eligible for an exact
    join.  The caller may wrap a batch in ``BEGIN IMMEDIATE``; this savepoint
    additionally keeps the paired operation atomic for direct callers.
    """

    conn.execute("SAVEPOINT historical_taf_atomic_ingress")
    try:
        quarantine = ingest_historical_taf_correction_quarantine(conn, artifact)
        calibration = ingest_historical_taf_calibration(conn, artifact)
    except Exception:
        conn.execute("ROLLBACK TO historical_taf_atomic_ingress")
        conn.execute("RELEASE historical_taf_atomic_ingress")
        raise
    conn.execute("RELEASE historical_taf_atomic_ingress")
    return {
        **calibration,
        "schema": "historical_taf_calibration_atomic_ingress_v1",
        "quarantine": quarantine,
        "quarantine_correction_count": int(quarantine["correction_count"]),
        "quarantine_wrote_count": int(quarantine["wrote_count"]),
        "quarantine_replay_count": int(quarantine["replay_count"]),
    }


def _rebin_probabilities(
    probabilities: Mapping[str, Any],
    centers: Mapping[str, Any],
    intervals: Mapping[str, Any],
) -> tuple[dict[str, float], dict[str, float]]:
    result = {str(bucket): 0.0 for bucket in intervals}
    live_centers: dict[str, float] = {}
    ordered = []
    for bucket, raw_bounds in intervals.items():
        if (
            not isinstance(raw_bounds, (list, tuple))
            or len(raw_bounds) != 2
        ):
            raise ContractError("native_taf_join_interval_invalid")
        lower = (
            None if raw_bounds[0] is None else float(raw_bounds[0])
        )
        upper = (
            None if raw_bounds[1] is None else float(raw_bounds[1])
        )
        if (
            lower is not None
            and upper is not None
            and lower >= upper
        ):
            raise ContractError("native_taf_join_interval_invalid")
        ordered.append((str(bucket), lower, upper))
    ordered.sort(key=lambda row: (
        -math.inf if row[1] is None else row[1],
        math.inf if row[2] is None else row[2],
    ))
    for index, (bucket, lower, upper) in enumerate(ordered):
        if lower is None:
            live_centers[bucket] = float(upper) - 0.5
        elif upper is None:
            live_centers[bucket] = float(lower) + 0.5
        else:
            live_centers[bucket] = (lower + upper) / 2.0
        if index and ordered[index - 1][2] != lower:
            raise ContractError("native_taf_join_intervals_not_contiguous")
    if set(map(str, probabilities)) != set(map(str, centers)):
        raise ContractError("native_taf_join_historical_support_mismatch")
    for source_bucket, raw_probability in probabilities.items():
        probability = _finite(
            "native_taf_join_probability",
            raw_probability,
        )
        center = _finite(
            "native_taf_join_center",
            centers[str(source_bucket)],
        )
        matches = [
            bucket
            for bucket, lower, upper in ordered
            if (lower is None or center >= lower)
            and (upper is None or center < upper)
        ]
        if len(matches) != 1 or probability < 0.0:
            raise ContractError("native_taf_join_rebin_invalid")
        result[matches[0]] += probability
    total = sum(result.values())
    if total <= 0.0 or abs(total - 1.0) > 1e-8:
        raise ContractError("native_taf_join_probability_invalid")
    return (
        {bucket: value / total for bucket, value in result.items()},
        live_centers,
    )


def build_taf_native_exact_join(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    season_bin: str,
    lead_bucket: str,
    run_cycle: str,
    settlement_definition: str,
    settlement_timezone: str,
    ensemble_system_ids: list[str],
    independence_family_ids: list[str],
    family_weight_caps: Mapping[str, float],
    bucket_intervals: Mapping[str, Any],
) -> dict[str, Any]:
    """Join immutable strict-OOS P0 predictions to the first later TAF."""

    if native_ensemble_foundation_schema_status(conn)["ok"] is not True:
        raise ContractError("native_ensemble_foundation_schema_required")
    station = station_id.strip().upper()
    systems_json = json.dumps(
        ensemble_system_ids,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    rows = conn.execute(
        "SELECT prediction_id,event_date,trained_through_event_date,"
        "decision_at_utc,independence_family_ids_json,"
        "system_family_ids_json,system_weights_json,"
        "family_weight_caps_json,probabilities_json,"
        "bucket_centers_c_json,p.content_sha256,a.generation "
        "FROM calibration_cross_native_ensemble_oos_prediction p "
        "JOIN calibration_native_ensemble_activation a "
        "ON a.artifact_kind='CROSS_NATIVE_ENSEMBLE' "
        "AND a.artifact_id=p.cross_ensemble_artifact_id "
        "AND a.station_id=p.station_id "
        "AND a.season_bin=p.season_bin "
        "AND a.lead_bucket=p.lead_bucket "
        "AND a.run_cycle=p.run_cycle "
        "WHERE p.station_id=? AND p.season_bin=? "
        "AND p.lead_bucket=? "
        "AND p.run_cycle=? AND p.settlement_definition=? "
        "AND p.settlement_timezone=? "
        "AND p.ensemble_system_ids_json=? "
        "AND p.prediction_oos=1 "
        "AND p.authority_scope='PRODUCTION_NO_LIVE_ONLY' "
        "AND a.authority_scope='PRODUCTION_NO_LIVE_ONLY' "
        "ORDER BY p.event_date,a.generation DESC,p.decision_at_utc",
        (
            station,
            season_bin,
            lead_bucket,
            run_cycle,
            settlement_definition,
            settlement_timezone,
            systems_json,
        ),
    ).fetchall()
    expected_families = list(independence_family_ids)
    expected_caps = {
        str(key): float(value)
        for key, value in family_weight_caps.items()
    }
    tz = ZoneInfo(settlement_timezone)
    samples: list[dict[str, Any]] = []
    prediction_shas: list[str] = []
    taf_lineages: list[dict[str, str | bool]] = []
    excluded_correction_samples: list[dict[str, Any]] = []
    observational_forward_dates = [
        str(item[0])
        for item in conn.execute(
            "SELECT DISTINCT event_date FROM calibration_forward_taf_capture "
            "WHERE station_id=? AND settlement_timezone=? "
            "AND source_class='official_forward_native_receipt' "
            "AND authority_scope='PRODUCTION_NO_LIVE_ONLY' "
            "ORDER BY event_date LIMIT ?",
            (station, settlement_timezone, MAX_TAF_CAPTURES),
        ).fetchall()
    ]
    generation_by_day: dict[str, int] = {}
    for row in rows:
        event_date = str(row[1])
        generation = int(row[11])
        previous_generation = generation_by_day.get(event_date)
        if previous_generation is not None:
            if previous_generation == generation:
                raise ContractError(
                    "native_taf_join_prediction_not_unique"
                )
            continue
        generation_by_day[event_date] = generation
        families = json.loads(str(row[4]))
        system_families = json.loads(str(row[5]))
        system_weights = json.loads(str(row[6]))
        caps = json.loads(str(row[7]))
        probabilities = json.loads(str(row[8]))
        centers = json.loads(str(row[9]))
        if (
            families != expected_families
            or {
                str(key): float(value) for key, value in caps.items()
            } != expected_caps
        ):
            continue
        decision_at = _utc(
            "native_taf_join_decision_at_utc",
            row[3],
        )
        day_end = (
            datetime.fromisoformat(event_date)
            .replace(tzinfo=tz)
            + timedelta(days=1)
        ).astimezone(timezone.utc)
        truth = conn.execute(
            "SELECT official_tmax_c,canonical_committed_at_utc,truth_id,"
            "source_lane,authority_scope "
            "FROM calibration_station_daily_high_truth "
            "WHERE station_id=? AND local_date=? "
            "AND settlement_definition=? AND source_lane='AWC' "
            "AND qc_status='ACCEPTED' "
            "AND authority_scope IN "
            "('FORMAL_NATIVE_ELIGIBLE','NO_LIVE_REPLAY_CANDIDATE') "
            "ORDER BY revision_no DESC LIMIT 1",
            (station, event_date, settlement_definition),
        ).fetchone()
        if truth is None or not truth[1]:
            continue
        truth_committed = datetime.fromisoformat(
            str(truth[1]).replace("Z", "+00:00")
        ).astimezone(timezone.utc)
        if truth_committed < day_end or truth_committed > datetime.now(timezone.utc):
            continue
        # A TAF AMD/COR is a station-day halt, just as an official
        # METAR/SPECI correction is.  This calibration table has no manual
        # review receipt authority, so it must never silently use the
        # preceding ordinary TAF (or the correction itself) as a P0-M label.
        # Keep a bounded, content-addressed exclusion in the exact join so
        # the missing sample is explainable without weakening the 24-sample
        # OOS gate.
        forward_corrections = conn.execute(
            "SELECT revision_kind,revision_id,revision_sequence,"
            "provider_available_at_utc,content_sha256 "
            "FROM calibration_forward_taf_capture "
            "WHERE station_id=? AND event_date=? "
            "AND settlement_timezone=? "
            "AND authority_scope='PRODUCTION_NO_LIVE_ONLY' "
            "AND revision_kind IN ('AMD','COR') "
            "ORDER BY provider_available_at_utc,revision_sequence "
            "LIMIT ?",
            (
                station,
                event_date,
                settlement_timezone,
                MAX_FORWARD_TAF_CORRECTIONS_PER_DAY + 1,
            ),
        ).fetchall()
        historical_corrections = conn.execute(
            "SELECT correction_kind,historical_taf_correction_quarantine_id,"
            "content_sha256 FROM calibration_historical_taf_correction_quarantine "
            "WHERE station_id=? AND event_date=? AND settlement_timezone=? "
            "AND authority_scope='CALIBRATION_TRAIN_ONLY' "
            "ORDER BY content_sha256 LIMIT ?",
            (
                station,
                event_date,
                settlement_timezone,
                MAX_FORWARD_TAF_CORRECTIONS_PER_DAY + 1,
            ),
        ).fetchall()
        if (
            len(forward_corrections) + len(historical_corrections)
            > MAX_FORWARD_TAF_CORRECTIONS_PER_DAY
        ):
            raise ContractError("native_taf_join_correction_scope_cap_exceeded")
        if forward_corrections or historical_corrections:
            corrections = [
                {
                    "revision_kind": str(correction[0]),
                    "revision_id": str(correction[1]),
                    "revision_sequence": int(correction[2]),
                    "provider_available_at_utc": str(correction[3]),
                    "content_sha256": str(correction[4]),
                }
                for correction in forward_corrections
            ] + [
                {
                    "revision_kind": str(correction[0]),
                    "revision_id": str(correction[1]),
                    "revision_sequence": 0,
                    "provider_available_at_utc": "",
                    "content_sha256": str(correction[2]),
                    "correction_source": "HISTORICAL_AWC_RAW_BULLETIN",
                }
                for correction in historical_corrections
            ]
            excluded_correction_samples.append({
                "station_id": station,
                "event_date": event_date,
                "reason": (
                    "taf_correction_requires_manual_review"
                    if historical_corrections
                    else "forward_taf_correction_requires_manual_review"
                ),
                "corrections": corrections,
            })
            continue
        taf = conn.execute(
            "SELECT provider_available_at_utc,tx_c,"
            "cloud_cover_fraction,precipitation_probability,"
            "wind_speed_mps,content_sha256,source_class,"
            "truth_label_authority "
            "FROM ("
            "SELECT provider_available_at_utc,tx_c,"
            "cloud_cover_fraction,precipitation_probability,"
            # Historical date-addressed AWC replay is the only TAF feature
            # source allowed in a strict OOS exact join.  Forward/local
            # receipts remain resident arrival evidence and are never reused
            # as historical labels.
            "wind_speed_mps,content_sha256,source_class,"
            "'FORMAL_STATION_LANE_TRUTH' AS truth_label_authority,"
            "1 AS source_sequence "
            "FROM calibration_historical_taf_capture "
            "WHERE station_id=? AND event_date=? "
            "AND settlement_timezone=? "
            "AND tx_c IS NOT NULL "
            "AND authority_scope='CALIBRATION_TRAIN_ONLY' "
            "AND source_class='official_date_addressed_historical_replay' "
            "AND truth_label_authority='FORMAL_STATION_LANE_TRUTH' "
            ") WHERE provider_available_at_utc>? "
            "AND provider_available_at_utc<? "
            "ORDER BY provider_available_at_utc,source_sequence "
            "LIMIT 1",
            (
                station,
                event_date,
                settlement_timezone,
                decision_at,
                day_end.isoformat().replace("+00:00", "Z"),
            ),
        ).fetchone()
        if taf is None:
            continue
        rebinned, live_centers = _rebin_probabilities(
            probabilities,
            centers,
            bucket_intervals,
        )
        prediction_sha = _sha(
            "native_taf_join_prediction_sha256",
            row[10],
        )
        prediction_shas.append(prediction_sha)
        taf_label_authority = str(taf[7] or "")
        taf_lineages.append({
            "capture_content_sha256": str(taf[5]),
            "truth_label_authority": taf_label_authority,
            "reference_only": False,
        })
        samples.append({
            "station_id": station,
            "event_date": event_date,
            "settlement_timezone": settlement_timezone,
            "calibration_scope": {
                "season_bin": season_bin,
                "lead_bucket": lead_bucket,
                "run_cycle": run_cycle,
                "settlement_definition": settlement_definition,
            },
            "native_p0": {
                "probability_pathway": (
                    "CALIBRATED_NATIVE_MEMBERS_CROSS_ENSEMBLE"
                ),
                "prediction_oos": True,
                "trained_through_event_date": str(row[2]),
                "decision_at_utc": decision_at,
                "ensemble_system_ids": ensemble_system_ids,
                "independence_family_ids": families,
                "system_family_ids": system_families,
                "system_weights": system_weights,
                "family_weight_caps": expected_caps,
                "probabilities": rebinned,
                "bucket_centers_c": live_centers,
                "prediction_content_sha256": prediction_sha,
            },
            "taf": {
                "available_at_utc": str(taf[0]),
                "tx_c": float(taf[1]),
                "cloud_cover_fraction": float(taf[2]),
                "precipitation_probability": float(taf[3]),
                "wind_speed_mps": float(taf[4]),
                "capture_content_sha256": str(taf[5]),
                "source_class": str(taf[6]),
                "truth_label_authority": taf_label_authority,
                "reference_only": False,
            },
            "oos_event_temporal_scope": HISTORICAL_OOS_TEMPORAL_SCOPE,
            "future_event_count": 0,
            "historical_oos_eligible": True,
            "truth": {
                "tmax_c": float(truth[0]),
                "canonical_truth_id": str(truth[2]),
                "source_lane": str(truth[3]),
                "authority_scope": str(truth[4]),
            },
            "taf_lineage": {
                "capture_content_sha256": str(taf[5]),
                "truth_label_authority": taf_label_authority,
                "reference_only": False,
                "enters_realtime_truth": False,
                "enters_p1": False,
                "enters_hard_no": False,
            },
            "settlement_window": {
                "end_utc": day_end.isoformat().replace("+00:00", "Z"),
            },
        })
    crossfit_sha = content_sha256({
        "station_id": station,
        "scope": [
            season_bin,
            lead_bucket,
            run_cycle,
            settlement_definition,
            settlement_timezone,
        ],
        "prediction_content_shas": prediction_shas,
        "taf_lineages": taf_lineages,
    })
    identity = {
        "schema": EXACT_JOIN_SCHEMA,
        "source_class": "strict_oos_native_p0_taf_truth_exact_join",
        # Keep the exact-join artifact self-describing.  The live contract is
        # keyed by this five-dimensional scope; hiding it only inside the
        # cross-fit digest made later bounded replays dependent on filenames
        # and allowed an otherwise valid artifact to be attached to the wrong
        # station/run at the ingress boundary.
        "station_id": station,
        "season_bin": season_bin,
        "lead_bucket": lead_bucket,
        "run_cycle": run_cycle,
        "settlement_definition": settlement_definition,
        "settlement_timezone": settlement_timezone,
        "nwp_crossfit_prediction_artifact_sha256": crossfit_sha,
        "sample_count": len(samples),
        "samples": samples,
        "excluded_correction_sample_count": len(excluded_correction_samples),
        "excluded_correction_samples": excluded_correction_samples,
        "observational_forward_event_count": len(observational_forward_dates),
        "observational_forward_event_dates": observational_forward_dates,
    }
    return {
        **identity,
        "content_sha256": content_sha256(identity),
    }


__all__ = [
    "build_taf_native_exact_join",
    "ingest_forward_taf_inbox",
    "ingest_historical_taf_calibration",
    "ingest_historical_taf_calibration_with_quarantine",
    "ingest_historical_taf_correction_quarantine",
    "project_taf_point_member_runs",
]


TAF_POINT_SYSTEM_ID = "OFFICIAL_AIRPORT_TAF_TX"
TAF_POINT_TRANSPORT_PROVIDER_ID = "AWC_TAF_DATA_API"
TAF_POINT_EXTRACTION_VERSION = "taf_tx_point_v4"
TAF_POINT_MEMBER_ID = "TX_POINT"
TAF_POINT_D0_LATEST_LOCAL_HOUR = 12


def project_taf_point_member_runs(
    conn: sqlite3.Connection,
    *,
    as_of_utc: str | None = None,
) -> dict[str, Any]:
    """Project captured TAF TX points into the cross-OOS member archive.

    One row per (station, day, lead): the latest admissible issuance.  Two
    rules protect the training evidence:

    - **No hindsight at d0.**  An issuance after local noon has seen the
      day's high; grading it would count hindsight as skill.
    - **Sticky scope label.**  TAF issue instants jitter across the 6h UTC
      grid, so rows are labelled with one constant cycle per (station, lead)
      -- the label already present in the archive when there is one, the
      modal cycle otherwise.  A drifting label would split the scope's days
      at the flip and starve the holdout on both sides.  The true issuance
      instant stays in ``native_run_init_utc``.

    Idempotent: content-addressed identities, OR IGNORE inserts, safe to run
    after every capture ingest.
    """

    from .contracts import content_sha256 as _content_sha256

    # The member archive's FK requires a registry row; the point system seeds
    # its own, idempotently, so any schema-complete database can project.
    seed_identity = {
        "ensemble_system_id": TAF_POINT_SYSTEM_ID,
        "canonical_name": "Official Airport TAF TX Point",
        "independence_family_id": "HUMAN_FORECASTER",
        "provider_id": "ICAO_MET_OFFICE",
        "source_class": "NATIVE_ENSEMBLE_PROVIDER",
        "aggregator_or_republisher": 0,
        "expected_member_count": 1,
        "member_identity_contract_version": "taf_tx_single_point_v1",
        "publication_contract_version": (
            "issue_time_known_amendments_supersede_v1"
        ),
        "valid_from_utc": "2026-06-01T00:00:00Z",
        "valid_until_utc": "9999-12-31T23:59:59Z",
    }
    conn.execute(
        "INSERT OR IGNORE INTO calibration_native_ensemble_system("
        "ensemble_system_id,canonical_name,independence_family_id,"
        "provider_id,source_class,aggregator_or_republisher,"
        "expected_member_count,member_identity_contract_version,"
        "publication_contract_version,valid_from_utc,valid_until_utc,"
        "content_sha256) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (*seed_identity.values(), _content_sha256(seed_identity)),
    )

    settlement_by_station = {
        str(row[0]): str(row[1] or "")
        for row in conn.execute(
            "SELECT station_id,settlement_definition "
            "FROM calibration_station_registry"
        )
    }
    captures: list[dict[str, Any]] = []
    for row in conn.execute(
        "SELECT station_id,event_date,settlement_timezone,tx_c,"
        "issued_at_utc,received_at_utc,source_object_sha256 "
        "FROM calibration_forward_taf_capture WHERE tx_c IS NOT NULL"
    ):
        captures.append(dict(zip(
            ("station_id", "event_date", "tz", "tx_c", "issued_at_utc",
             "query_at_utc", "source_sha"), row,
        )))
    for row in conn.execute(
        "SELECT station_id,event_date,settlement_timezone,tx_c,"
        "issued_at_utc,decision_anchor_utc,raw_taf_sha256 "
        "FROM calibration_historical_taf_capture WHERE tx_c IS NOT NULL"
    ):
        captures.append(dict(zip(
            ("station_id", "event_date", "tz", "tx_c", "issued_at_utc",
             "query_at_utc", "source_sha"), row,
        )))

    as_of = (
        datetime.now(timezone.utc)
        if as_of_utc is None
        else datetime.fromisoformat(
            str(as_of_utc).replace("Z", "+00:00")
        ).astimezone(timezone.utc)
    )
    latest: dict[tuple[str, str, str], tuple[str, dict[str, Any]]] = {}
    skipped = {
        "hindsight_d0": 0,
        "lead_out_of_scope": 0,
        "invalid": 0,
        "anchor_not_passed": 0,
    }
    for cap in captures:
        try:
            issued = datetime.fromisoformat(
                str(cap["issued_at_utc"]).replace("Z", "+00:00")
            ).astimezone(timezone.utc)
            local = issued.astimezone(ZoneInfo(str(cap["tz"])))
            event = datetime.fromisoformat(
                str(cap["event_date"]) + "T00:00:00"
            ).date()
        except (ValueError, KeyError):
            skipped["invalid"] += 1
            continue
        lead = (event - local.date()).days
        if lead not in (0, 1):
            skipped["lead_out_of_scope"] += 1
            continue
        if lead == 0 and local.hour >= TAF_POINT_D0_LATEST_LOCAL_HOUR:
            skipped["hindsight_d0"] += 1
            continue
        if not settlement_by_station.get(str(cap["station_id"])):
            skipped["invalid"] += 1
            continue
        # TAF amends continuously; the archive row must be the FINAL
        # pre-anchor issuance, not whichever amendment happened to be first
        # past the ingest.  The archive is append-only with one row per
        # (station, day, lead), so a scope is archived only once its anchor
        # has passed and "latest" cannot change any more.  Live serving is
        # different on purpose: it tracks the latest issuance intraday.
        try:
            tz_local = ZoneInfo(str(cap["tz"]))
            anchor_hour = TAF_POINT_D0_LATEST_LOCAL_HOUR if lead == 0 else 0
            anchor = datetime.fromisoformat(
                str(cap["event_date"]) + f"T{anchor_hour:02d}:00:00"
            ).replace(tzinfo=tz_local)
        except (ValueError, KeyError):
            skipped["invalid"] += 1
            continue
        if as_of < anchor:
            skipped["anchor_not_passed"] += 1
            continue
        key = (str(cap["station_id"]), str(cap["event_date"]), f"d{lead}")
        cycle = f"{(issued.hour // 6) * 6:02d}Z"
        prior = latest.get(key)
        if prior is None or str(cap["issued_at_utc"]) > str(
            prior[1]["issued_at_utc"]
        ):
            latest[key] = (cycle, cap)

    sticky: dict[tuple[str, str], str] = {
        (str(row[0]), str(row[1])): str(row[2])
        for row in conn.execute(
            "SELECT DISTINCT station_id,archive_lead_bucket,run_cycle "
            "FROM calibration_native_ensemble_historical_member_run "
            "WHERE ensemble_system_id=? AND extraction_version=?",
            (TAF_POINT_SYSTEM_ID, TAF_POINT_EXTRACTION_VERSION),
        )
    }
    modal_counts: dict[tuple[str, str], dict[str, int]] = {}
    for (station, _day, lead), (cycle, _cap) in latest.items():
        counts = modal_counts.setdefault((station, lead), {})
        counts[cycle] = counts.get(cycle, 0) + 1
    # Built first, written once.
    #
    # This was a per-candidate ``INSERT OR IGNORE`` followed by a
    # ``SELECT changes()`` to see whether it took -- and it runs behind
    # every TAF capture ingest, which is every production cycle.  Measured
    # 2026-09-07: 1,960 candidates and 1,960 rows already present, so the
    # loop issued 3,920 statements to write nothing, inside the write
    # lock's savepoint, while ``p0m_taf_fact_ingress`` cost 68.0s of a
    # 620s cycle.
    #
    # ``INSERT OR IGNORE`` is kept exactly as it was: this table has a
    # primary key, a UNIQUE on content_sha256 and a third UNIQUE besides,
    # so "already there" is not only a primary-key collision and
    # pre-filtering on the id would change which rows are attempted.  What
    # changes is that the attempts go through one ``executemany`` instead
    # of a Python round trip apiece, and the count comes from the
    # connection's own change counter instead of a statement per row.
    pending: list[tuple[Any, ...]] = []
    for (station, day, lead), (_cycle, cap) in sorted(latest.items()):
        label = sticky.get((station, lead))
        if label is None:
            counts = modal_counts[(station, lead)]
            label = max(counts.items(), key=lambda kv: (kv[1], kv[0]))[0]
            sticky[(station, lead)] = label
        identity = {
            "ensemble_system_id": TAF_POINT_SYSTEM_ID,
            "station_id": station,
            "target_local_date": day,
            "archive_lead_bucket": lead,
            "run_cycle": label,
            "member_id": TAF_POINT_MEMBER_ID,
            "raw_tmax_c": f"{float(cap['tx_c']):.12g}",
            "native_run_init_utc": str(cap["issued_at_utc"]),
            "extraction_version": TAF_POINT_EXTRACTION_VERSION,
        }
        sha = _content_sha256(identity)
        pending.append((
            "tafpt-" + sha[:32], TAF_POINT_SYSTEM_ID, station, day,
            lead, label, TAF_POINT_MEMBER_ID, "CONTROL",
            float(cap["tx_c"]),
            settlement_by_station[station],
            TAF_POINT_EXTRACTION_VERSION,
            "ICAO_MET_OFFICE", TAF_POINT_TRANSPORT_PROVIDER_ID,
            str(cap["issued_at_utc"]), 1,
            str(cap["query_at_utc"] or cap["issued_at_utc"]),
            "", "", str(cap["source_sha"]) or "0" * 64,
            "ACCEPTED", "CALIBRATION_TRAIN_ONLY", 0, sha,
        ))
    # Which of them the archive already holds, in one indexed read.
    #
    # Batching the attempts removed 3,920 Python round trips but not the
    # work behind them: this table carries a primary key, a UNIQUE on
    # content_sha256 and a third UNIQUE, so every attempted row probes
    # three B-trees over 1,485,727 rows -- and on 2026-09-07 all 1,960
    # candidates were already present, so every one of those probes was
    # spent to insert nothing.  On a warm isolated copy that is 100ms and
    # invisible; against a 38GB database where those pages are cold it is
    # not, and this projection runs behind every capture ingest.
    #
    # An accelerator, not a filter: a candidate whose primary key is
    # already stored is one ``INSERT OR IGNORE`` would have ignored, so
    # skipping it cannot change what the archive holds.  Rows that survive
    # this still go through ``OR IGNORE``, which is what keeps the other
    # two constraints deciding for themselves.
    # Both of the keys that can refuse a row, read once.
    #
    # The primary key is content-addressed, so an amended issuance for a
    # scope already archived produces a *new* id and clears that filter --
    # and is then refused by the natural-key UNIQUE
    # (ensemble_system_id, station_id, target_local_date,
    # archive_lead_bucket, member_id, extraction_version), which is the
    # append-only rule this projection's docstring states: one row per
    # (station, day, lead), written once the anchor has passed, and the
    # amendment after that must not rewrite it.  Measured 2026-09-07,
    # 1,558 of 1,960 candidates were refused by the id and the remaining
    # 402 by the natural key, every cycle, forever.
    #
    # So a nonzero attempt count with wrote_count 0 is this rule working,
    # not a lane refusing its own writes -- the receipt reports both
    # counts so the next reader does not have to chase it, which is what
    # this one nearly did.
    stored_ids: set[str] = set()
    stored_scopes: set[tuple[str, str, str, str]] = set()
    for row in conn.execute(
        "SELECT historical_member_run_id, station_id, target_local_date,"
        " archive_lead_bucket, member_id"
        " FROM calibration_native_ensemble_historical_member_run"
        " WHERE ensemble_system_id=? AND extraction_version=?",
        (TAF_POINT_SYSTEM_ID, TAF_POINT_EXTRACTION_VERSION),
    ):
        stored_ids.add(str(row[0]))
        stored_scopes.add((
            str(row[1]), str(row[2]), str(row[3]), str(row[4])
        ))
    candidate_total = len(pending)
    pending = [
        row for row in pending
        if str(row[0]) not in stored_ids
        and (str(row[2]), str(row[3]), str(row[4]), str(row[6]))
        not in stored_scopes
    ]
    inserted = 0
    if pending:
        # The connection's counter, not a statement per row: it counts rows
        # actually inserted across the batch, which is what the receipt has
        # always reported, and an ignored row does not move it.
        before = conn.total_changes
        conn.executemany(
            "INSERT OR IGNORE INTO "
            "calibration_native_ensemble_historical_member_run("
            "historical_member_run_id,ensemble_system_id,station_id,"
            "target_local_date,archive_lead_bucket,run_cycle,member_id,"
            "member_role,raw_tmax_c,settlement_definition,"
            "extraction_version,provider_id,transport_provider_id,"
            "native_run_init_utc,offline_oos_eligible,"
            "archive_query_at_utc,source_url,raw_object_ref,"
            "source_object_sha256,qc_status,authority_scope,"
            "holdout_eligible,content_sha256) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            pending,
        )
        inserted = conn.total_changes - before
    return {
        "schema": "taf_point_member_run_projection_v1",
        "candidate_count": len(latest),
        "wrote_count": inserted,
        # How many of the candidates the archive already held.  Without it
        # "wrote_count: 0" reads the same whether the projection had
        # nothing to do or was refusing everything, and the first of those
        # is a lane doing its job while the second is an outage.
        "already_stored_count": candidate_total - len(pending),
        "attempted_count": len(pending),
        "skipped": skipped,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "live_submit_authorized": False,
    }


REGIONAL_POINT_SYSTEM_VALID_FROM_UTC = "2026-08-25T00:00:00Z"


def register_regional_point_systems(conn: sqlite3.Connection) -> int:
    """Charter 95: the regional deterministic models as registry rows.

    The compiler resolves a served system's family and member count from
    ``calibration_native_ensemble_system``; the point systems were registered
    in code only, which was enough for the OOS lane but fails closed at
    compile.  Idempotent: rows already present are left alone.
    """

    from .contracts import content_sha256 as _content_sha256
    from .official_native_forward_packet import (
        POINT_FORECAST_SYSTEMS,
        REGIONAL_DETERMINISTIC_SYSTEM_IDS,
        REGIONAL_POINT_CONTRACT_VERSIONS,
    )

    inserted = 0
    for system_id in sorted(REGIONAL_DETERMINISTIC_SYSTEM_IDS):
        registration = POINT_FORECAST_SYSTEMS[system_id]
        member_contract, publication_contract = REGIONAL_POINT_CONTRACT_VERSIONS[
            system_id
        ]
        identity = {
            "ensemble_system_id": system_id,
            "canonical_name": str(registration["canonical_name"]),
            "independence_family_id": str(
                registration["independence_family_id"]
            ),
            "provider_id": str(registration["provider_id"]),
            "source_class": "NATIVE_ENSEMBLE_PROVIDER",
            "aggregator_or_republisher": 0,
            "expected_member_count": 1,
            "member_identity_contract_version": member_contract,
            "publication_contract_version": publication_contract,
            "valid_from_utc": REGIONAL_POINT_SYSTEM_VALID_FROM_UTC,
            "valid_until_utc": "9999-12-31T23:59:59Z",
        }
        cursor = conn.execute(
            "INSERT OR IGNORE INTO calibration_native_ensemble_system("
            "ensemble_system_id,canonical_name,independence_family_id,"
            "provider_id,source_class,aggregator_or_republisher,"
            "expected_member_count,member_identity_contract_version,"
            "publication_contract_version,valid_from_utc,valid_until_utc,"
            "content_sha256) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (*identity.values(), _content_sha256(identity)),
        )
        inserted += int(cursor.rowcount or 0)
    return inserted


def build_regional_point_forward_archives_for_station(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    event_date: str,
    settlement_timezone: str,
    as_of_utc: str,
    bucket_intervals: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Charter 95: each regional model's freshest run for this station-day.

    The serving rule is the training rule: the capture lane already wrote the
    run as a train-only fact; this projects the newest run whose archive
    query precedes ``as_of_utc`` into the packet's point-archive shape, one
    archive per regional system.  No run, no archive -- the packet then
    serves the set it would have served anyway.
    """

    from .contracts import content_sha256 as _content_sha256
    from .official_native_forward_packet import (
        POINT_FORECAST_SYSTEMS,
        REGIONAL_DETERMINISTIC_SYSTEM_IDS,
        REGIONAL_POINT_ARCHIVE_SCHEMA_BY_SYSTEM,
    )

    station = str(station_id).strip().upper()
    placeholders = ",".join("?" * len(REGIONAL_DETERMINISTIC_SYSTEM_IDS))
    rows = conn.execute(
        "SELECT ensemble_system_id,run_cycle,archive_lead_bucket,member_id,"
        "raw_tmax_c,native_run_init_utc,archive_query_at_utc,content_sha256 "
        "FROM calibration_native_ensemble_historical_member_run "
        "WHERE station_id=? AND target_local_date=? AND qc_status='ACCEPTED' "
        f"AND ensemble_system_id IN ({placeholders}) "
        "AND archive_query_at_utc<=? "
        "ORDER BY native_run_init_utc DESC, archive_query_at_utc DESC",
        (station, str(event_date), *sorted(REGIONAL_DETERMINISTIC_SYSTEM_IDS), str(as_of_utc)),
    ).fetchall()
    frozen_intervals = {
        str(bucket): [
            None if bounds[0] is None else float(bounds[0]),
            None if bounds[1] is None else float(bounds[1]),
        ]
        for bucket, bounds in dict(bucket_intervals).items()
    }
    archives: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        system_id = str(row[0])
        if system_id in seen or not str(row[5] or "").strip():
            continue
        seen.add(system_id)
        registration = POINT_FORECAST_SYSTEMS[system_id]
        family = str(registration["independence_family_id"])
        member = {
            "member_id": str(row[3]),
            "member_role": "CONTROL",
            "raw_tmax_c": float(row[4]),
        }
        capture_identity = {
            "station_id": station,
            "ensemble_system_id": system_id,
            "independence_family_id": family,
            "event_date": str(event_date),
            "settlement_timezone": str(settlement_timezone),
            "run_cycle": str(row[1]),
            "archive_lead_bucket": str(row[2]),
            "member_count": 1,
            "members": [member],
            "member_set_sha256": _content_sha256([member]),
            "bucket_intervals": frozen_intervals,
            "bucket_contract_sha256": _content_sha256(frozen_intervals),
            "source_capture_content_sha256": str(row[7] or ""),
        }
        capture = {
            **capture_identity,
            "content_sha256": _content_sha256(capture_identity),
        }
        archive_identity = {
            "schema": REGIONAL_POINT_ARCHIVE_SCHEMA_BY_SYSTEM[system_id],
            "ensemble_system_id": system_id,
            "independence_family_id": family,
            "event_date": str(event_date),
            "archive_lead_bucket": str(row[2]),
            "run_cycle": str(row[1]),
            "run_init_utc": str(row[5]),
            "archive_query_at_utc": str(row[6]),
            "member_count": 1,
            "training_only": True,
            "holdout_eligible": False,
            "captures": [capture],
        }
        archives.append({
            **archive_identity,
            "content_sha256": _content_sha256(archive_identity),
        })
    return archives


def build_taf_point_forward_archive_for_station(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    event_date: str,
    lead_bucket: str,
    as_of_utc: str,
    bucket_intervals: Mapping[str, Any],
) -> dict[str, Any] | None:
    """One station's TAF TX as a live forward-packet archive entry, or None.

    The serving rules are exactly the training rules -- latest admissible
    issuance, no post-noon d0 hindsight, the sticky scope label from the
    member-run archive -- so what the OOS gated is what live consumes.
    ``None`` (no admissible TAF, or no sticky label yet) lets the packet fall
    back to the model-only archive set rather than blocking it.
    """

    from .contracts import content_sha256 as _content_sha256

    if lead_bucket not in ("d0", "d1"):
        return None
    label_row = conn.execute(
        "SELECT DISTINCT run_cycle "
        "FROM calibration_native_ensemble_historical_member_run "
        "WHERE ensemble_system_id=? AND extraction_version=? "
        "AND station_id=? AND archive_lead_bucket=?",
        (
            TAF_POINT_SYSTEM_ID, TAF_POINT_EXTRACTION_VERSION,
            station_id, lead_bucket,
        ),
    ).fetchall()
    if len(label_row) != 1:
        return None
    label = str(label_row[0][0])
    best: dict[str, Any] | None = None
    for row in conn.execute(
        "SELECT tx_c,issued_at_utc,settlement_timezone,content_sha256 "
        "FROM calibration_forward_taf_capture "
        "WHERE station_id=? AND event_date=? AND tx_c IS NOT NULL "
        "AND issued_at_utc<=? ORDER BY issued_at_utc",
        (station_id, event_date, as_of_utc),
    ):
        try:
            issued = datetime.fromisoformat(
                str(row[1]).replace("Z", "+00:00")
            ).astimezone(timezone.utc)
            local = issued.astimezone(ZoneInfo(str(row[2])))
            event = datetime.fromisoformat(
                event_date + "T00:00:00"
            ).date()
        except (ValueError, KeyError):
            continue
        lead = (event - local.date()).days
        if f"d{lead}" != lead_bucket:
            continue
        if lead == 0 and local.hour >= TAF_POINT_D0_LATEST_LOCAL_HOUR:
            continue
        best = {
            "tx_c": float(row[0]),
            "issued_at_utc": str(row[1]),
            "settlement_timezone": str(row[2]),
            "capture_content_sha256": str(row[3] or ""),
        }
    if best is None:
        return None
    member = {
        "member_id": TAF_POINT_MEMBER_ID,
        "member_role": "CONTROL",
        "raw_tmax_c": best["tx_c"],
    }
    frozen_intervals = {
        str(bucket): [
            None if bounds[0] is None else float(bounds[0]),
            None if bounds[1] is None else float(bounds[1]),
        ]
        for bucket, bounds in dict(bucket_intervals).items()
    }
    capture_identity = {
        "station_id": station_id,
        "ensemble_system_id": TAF_POINT_SYSTEM_ID,
        "independence_family_id": "HUMAN_FORECASTER",
        "event_date": event_date,
        "settlement_timezone": best["settlement_timezone"],
        "run_cycle": label,
        "archive_lead_bucket": lead_bucket,
        "member_count": 1,
        "members": [member],
        "member_set_sha256": _content_sha256([member]),
        "bucket_intervals": frozen_intervals,
        "bucket_contract_sha256": _content_sha256(frozen_intervals),
        "source_capture_content_sha256": best["capture_content_sha256"],
    }
    capture = {
        **capture_identity,
        "content_sha256": _content_sha256(capture_identity),
    }
    archive_identity = {
        "schema": "official_airport_taf_tx_point_members_v1",
        "ensemble_system_id": TAF_POINT_SYSTEM_ID,
        "independence_family_id": "HUMAN_FORECASTER",
        "event_date": event_date,
        "archive_lead_bucket": lead_bucket,
        "run_cycle": label,
        "run_init_utc": best["issued_at_utc"],
        "archive_query_at_utc": as_of_utc,
        "member_count": 1,
        "training_only": True,
        "holdout_eligible": False,
        "captures": [capture],
    }
    return {
        **archive_identity,
        "content_sha256": _content_sha256(archive_identity),
    }
