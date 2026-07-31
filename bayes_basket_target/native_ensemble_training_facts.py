"""Append-only facts joining native-ensemble P0, TAF, and settled truth."""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .contracts import ContractError, content_sha256
from .event_contracts import ForecastRevision
from .ssot import native_ensemble_foundation_schema_status
from .taf_native_ensemble_oos import EXACT_JOIN_SCHEMA


TAF_INBOX_SCHEMA = "bayes_basket_target_taf_revision_inbox_v1"
MAX_TAF_CAPTURES = 256


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
    rows: list[dict[str, Any]] = []
    rejected_timestamp_captures: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
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
        station_row = conn.execute(
            "SELECT 1 FROM calibration_station_registry "
            "WHERE station_id=?",
            (station,),
        ).fetchone()
        station_count = int(conn.execute(
            "SELECT COUNT(*) FROM calibration_station_registry"
        ).fetchone()[0])
        market_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='basket_target_market_registry'"
        ).fetchone()
        timezone_rows = (
            conn.execute(
                "SELECT DISTINCT settlement_timezone "
                "FROM basket_target_market_registry "
                "WHERE station_id=? AND event_date=?",
                (station, event_date),
            ).fetchall()
            if market_table is not None
            else []
        )
        if station_count and station_row is None:
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
    wrote = 0
    conn.execute("SAVEPOINT forward_taf_capture_ingress")
    try:
        for row in rows:
            wrote += _insert_content_addressed(conn, row)
    except Exception:
        conn.execute("ROLLBACK TO forward_taf_capture_ingress")
        conn.execute("RELEASE forward_taf_capture_ingress")
        raise
    conn.execute("RELEASE forward_taf_capture_ingress")
    return {
        "schema": "forward_taf_capture_ingress_v1",
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
        or int(artifact.get("sample_count") or -1) != len(samples)
    ):
        raise ContractError("historical_taf_artifact_invalid")
    station = str(artifact.get("station_id") or "").strip().upper()
    timezone_name = str(
        artifact.get("settlement_timezone") or ""
    ).strip()
    station_row = conn.execute(
        "SELECT timezone_name FROM calibration_station_registry "
        "WHERE station_id=?",
        (station,),
    ).fetchone()
    market_timezones = conn.execute(
        "SELECT DISTINCT settlement_timezone "
        "FROM basket_target_market_registry "
        "WHERE station_id=? AND settlement_timezone<>''",
        (station,),
    ).fetchall()
    expected_timezone = (
        str(market_timezones[0][0])
        if len(market_timezones) == 1
        else str(station_row[0])
        if station_row is not None and not market_timezones
        else ""
    )
    if (
        not station
        or not timezone_name
        or station_row is None
        or expected_timezone != timezone_name
    ):
        raise ContractError("historical_taf_station_scope_invalid")
    tz = ZoneInfo(timezone_name)
    rows: list[dict[str, Any]] = []
    seen_dates: set[str] = set()
    skipped_after_anchor: list[str] = []
    for sample in samples:
        if not isinstance(sample, Mapping):
            raise ContractError("historical_taf_sample_invalid")
        event_date = str(sample.get("event_date") or "")
        taf = sample.get("taf")
        receipts = sample.get("receipts")
        if (
            str(sample.get("station_id") or "").strip().upper()
            != station
            or str(sample.get("settlement_timezone") or "")
            != timezone_name
            or not event_date
            or event_date in seen_dates
            or not isinstance(taf, Mapping)
            or not isinstance(receipts, Mapping)
        ):
            raise ContractError("historical_taf_sample_scope_invalid")
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
    return {
        "schema": "historical_taf_calibration_ingress_v1",
        "station_id": station,
        "capture_count": len(rows),
        "wrote_count": wrote,
        "replay_count": len(rows) - wrote,
        "skipped_after_anchor_count": len(skipped_after_anchor),
        "skipped_after_anchor_dates": skipped_after_anchor,
        "authority_scope": "CALIBRATION_TRAIN_ONLY",
        "source_artifact_sha256": source_artifact_sha,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "live_submit_authorized": False,
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
            "SELECT official_tmax_c,canonical_committed_at_utc "
            "FROM calibration_station_daily_high_truth "
            "WHERE station_id=? AND local_date=? "
            "AND settlement_definition=? AND qc_status='ACCEPTED' "
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
        if truth_committed < day_end:
            continue
        taf = conn.execute(
            "SELECT provider_available_at_utc,tx_c,"
            "cloud_cover_fraction,precipitation_probability,"
            "wind_speed_mps,content_sha256,source_class "
            "FROM ("
            "SELECT provider_available_at_utc,tx_c,"
            "cloud_cover_fraction,precipitation_probability,"
            "wind_speed_mps,content_sha256,source_class,"
            "revision_sequence AS source_sequence "
            "FROM calibration_forward_taf_capture "
            "WHERE station_id=? AND event_date=? "
            "AND settlement_timezone=? "
            "AND tx_available=1 AND tx_c IS NOT NULL "
            "AND authority_scope='PRODUCTION_NO_LIVE_ONLY' "
            "UNION ALL "
            "SELECT provider_available_at_utc,tx_c,"
            "cloud_cover_fraction,precipitation_probability,"
            "wind_speed_mps,content_sha256,source_class,"
            "1 AS source_sequence "
            "FROM calibration_historical_taf_capture "
            "WHERE station_id=? AND event_date=? "
            "AND settlement_timezone=? "
            "AND authority_scope='CALIBRATION_TRAIN_ONLY'"
            ") WHERE provider_available_at_utc>? "
            "AND provider_available_at_utc<? "
            "ORDER BY provider_available_at_utc,source_sequence "
            "LIMIT 1",
            (
                station,
                event_date,
                settlement_timezone,
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
            },
            "truth": {"tmax_c": float(truth[0])},
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
    })
    identity = {
        "schema": EXACT_JOIN_SCHEMA,
        "source_class": "strict_oos_native_p0_taf_truth_exact_join",
        "nwp_crossfit_prediction_artifact_sha256": crossfit_sha,
        "sample_count": len(samples),
        "samples": samples,
    }
    return {
        **identity,
        "content_sha256": content_sha256(identity),
    }


__all__ = [
    "build_taf_native_exact_join",
    "ingest_forward_taf_inbox",
    "ingest_historical_taf_calibration",
]
