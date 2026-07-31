#!/usr/bin/env python3
"""Run the historical AWC TAF -> native P0-M candidate pipeline once.

The command deliberately separates the read-only plan from the explicit
``--commit`` ingress.  Historical TAF facts are committed atomically with
the exact-join read, while the P0-M result remains an orderless candidate
artifact.  It never materializes a production P0-M row and has no gateway
dependency.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import content_sha256  # noqa: E402
from bayes_basket_target.database import (  # noqa: E402
    DEFAULT_TARGET_DB,
    resolve_target_database,
)
from bayes_basket_target.native_ensemble_training_facts import (  # noqa: E402
    build_taf_native_exact_join,
    ingest_historical_taf_calibration,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    assert_connection_target_capacity,
    load_bounded_json,
    write_bounded_json,
    write_bounded_json_if_changed,
)
from bayes_basket_target.taf_native_ensemble_oos import (  # noqa: E402
    MIN_SAMPLE_N as MIN_TAF_OOS_SAMPLE_N,
    build_taf_native_ensemble_oos_report,
    resolve_native_live_contract,
)
from scripts.build_native_ensemble_training_facts import (  # noqa: E402
    _discover_live_scopes,
)


SCHEMA = "historical_taf_native_p0m_exact_join_manifest_v1"
P0M_CANDIDATE_SCHEMA = "historical_taf_p0m_candidate_v1"
MAX_STATIONS = 45
MAX_ARCHIVES = 45
MAX_TAF_ARTIFACTS = 45
MAX_SCOPES = 45
MAX_SAMPLES_PER_ARCHIVE = 366
REQUIRED_HISTORICAL_TAF_TABLE = "calibration_historical_taf_capture"


def _path(path: Path, *, must_exist: bool = True) -> Path:
    candidate = Path(path).expanduser()
    if candidate.is_symlink():
        raise RuntimeError("historical_taf_pipeline_symlink_input_forbidden")
    return candidate.resolve(strict=must_exist)


def _mapping(value: Any, *, reason: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RuntimeError(reason)
    return value


def _sha(value: Any, *, name: str) -> str:
    result = str(value or "").strip().lower()
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
        raise RuntimeError(f"{name}_invalid")
    return result


def _verify_content_hash(value: Mapping[str, Any], *, name: str) -> str:
    supplied = _sha(value.get("content_sha256"), name=f"{name}_content_sha256")
    identity = {
        str(key): child
        for key, child in value.items()
        if key != "content_sha256"
    }
    if content_sha256(identity) != supplied:
        raise RuntimeError(f"{name}_content_sha256_mismatch")
    return supplied


def _utc(value: Any, *, name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{name}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RuntimeError(f"{name}_timezone_required")
    return parsed.astimezone(timezone.utc)


def _archive_station(archive: Mapping[str, Any], *, source: Path) -> str:
    station = str(archive.get("station_id") or "").strip().upper()
    if not station:
        raise RuntimeError(f"historical_taf_archive_station_missing:{source}")
    return station


def _validate_archive_contract(
    archive: Mapping[str, Any],
    *,
    source: Path,
) -> dict[str, Any]:
    """Validate the immutable archive envelope without changing the DB.

    Field-level validation remains in ``ingest_historical_taf_calibration``;
    this preflight only rejects malformed envelopes before a commit can begin
    and records capture blockers so an incomplete station can never produce a
    P0-M candidate.
    """

    if archive.get("schema") != "bayes_basket_awc_taf_calibration_dataset_v1":
        raise RuntimeError(f"historical_taf_archive_schema_invalid:{source}")
    if archive.get("source_class") != "official_date_addressed_historical_replay":
        raise RuntimeError(f"historical_taf_archive_source_class_invalid:{source}")
    boundary = archive.get("boundary")
    if not isinstance(boundary, Mapping):
        raise RuntimeError(f"historical_taf_archive_boundary_missing:{source}")
    if (
        boundary.get("native_local_receipt") is not False
        or boundary.get("official_awc_database_availability_preserved") is not True
        or boundary.get("historical_replay_only") is not True
        or boundary.get("truth_days_closed") is not True
        or boundary.get("writes_production_db") is not False
        or boundary.get("calls_order_gateway") is not False
        or boundary.get("places_orders") is not False
    ):
        raise RuntimeError(f"historical_taf_archive_boundary_invalid:{source}")
    supplied_sha = _verify_content_hash(archive, name="historical_taf_archive")
    samples = archive.get("samples")
    if not isinstance(samples, list) or len(samples) > MAX_SAMPLES_PER_ARCHIVE:
        raise RuntimeError(f"historical_taf_archive_samples_invalid:{source}")
    if int(archive.get("sample_count", -1)) != len(samples):
        raise RuntimeError(f"historical_taf_archive_sample_count_invalid:{source}")
    station = _archive_station(archive, source=source)
    timezone_name = str(archive.get("settlement_timezone") or "").strip()
    if not timezone_name:
        raise RuntimeError(f"historical_taf_archive_timezone_missing:{source}")

    # The capture producer may have retained a row whose official database
    # availability is after the historical decision anchor.  The existing
    # ingest API deliberately skips that row; mirror that accounting here so
    # the CLI can still ingest the valid rows while making the station
    # ineligible for a P0-M candidate.  We never pass the late row downstream.
    late_sample_count = 0
    for sample in samples:
        if not isinstance(sample, Mapping):
            raise RuntimeError(f"historical_taf_archive_sample_invalid:{source}")
        if str(sample.get("station_id") or "").strip().upper() != station:
            raise RuntimeError(f"historical_taf_archive_sample_station_mismatch:{source}")
        if str(sample.get("settlement_timezone") or "") != timezone_name:
            raise RuntimeError(f"historical_taf_archive_sample_timezone_mismatch:{source}")
        taf = sample.get("taf")
        if not isinstance(taf, Mapping):
            raise RuntimeError(f"historical_taf_archive_sample_taf_missing:{source}")
        issued = _utc(taf.get("issue_time_utc"), name="historical_taf_issue_time")
        available = _utc(taf.get("available_time_utc"), name="historical_taf_available_time")
        anchor = _utc(
            sample.get("decision_anchor_utc"),
            name="historical_taf_decision_anchor",
        )
        if issued > available:
            raise RuntimeError("historical_taf_strict_asof_violation")
        if available > anchor:
            late_sample_count += 1

    blockers = archive.get("blockers")
    if not isinstance(blockers, list):
        raise RuntimeError(f"historical_taf_archive_blockers_invalid:{source}")
    excluded = archive.get("excluded_samples")
    if not isinstance(excluded, list):
        raise RuntimeError(f"historical_taf_archive_excluded_samples_invalid:{source}")
    if int(archive.get("excluded_sample_count", -1)) != len(excluded):
        raise RuntimeError(f"historical_taf_archive_excluded_count_invalid:{source}")
    return {
        "source": str(source),
        "station_id": station,
        "settlement_timezone": timezone_name,
        "content_sha256": supplied_sha,
        "sample_count": len(samples),
        "excluded_sample_count": len(excluded),
        "blocker_count": len(blockers) + late_sample_count,
        "blocker_codes": sorted({
            str(item.get("code") or item.get("reason") or "unknown")
            for item in blockers
            if isinstance(item, Mapping)
        } | ({"historical_taf_strict_asof_violation"} if late_sample_count else set())),
        "strict_asof_skipped_sample_count": late_sample_count,
        "candidate_input_complete": not blockers and not late_sample_count and bool(samples),
    }


def _archive_paths(
    explicit: Sequence[Path],
    archive_dir: Path | None,
) -> list[Path]:
    paths = [_path(value) for value in explicit]
    if archive_dir is not None:
        directory = _path(archive_dir)
        if not directory.is_dir():
            raise RuntimeError("historical_taf_archive_dir_invalid")
        paths.extend(
            _path(value)
            for value in sorted(directory.glob("AWC_TAF_*.json"))
            if value.is_file()
        )
    unique = sorted({str(value): value for value in paths}.values(), key=str)
    if not unique:
        raise RuntimeError("historical_taf_archive_required")
    if len(unique) > MAX_ARCHIVES:
        raise RuntimeError("historical_taf_archive_count_exceeded")
    return unique


def _load_archives(
    explicit: Sequence[Path],
    archive_dir: Path | None,
) -> tuple[list[Mapping[str, Any]], list[dict[str, Any]]]:
    archives: list[Mapping[str, Any]] = []
    metadata: list[dict[str, Any]] = []
    for source in _archive_paths(explicit, archive_dir):
        archive = _mapping(
            load_bounded_json(source),
            reason=f"historical_taf_archive_mapping_required:{source}",
        )
        archives.append(archive)
        metadata.append(_validate_archive_contract(archive, source=source))
    stations = {str(item["station_id"]) for item in metadata}
    if len(stations) > MAX_STATIONS:
        raise RuntimeError("historical_taf_station_count_exceeded")
    return archives, metadata


def _artifact_paths(
    explicit: Sequence[Path],
    artifact_dir: Path | None,
) -> list[Path]:
    paths = [_path(value) for value in explicit]
    if artifact_dir is not None:
        directory = _path(artifact_dir)
        if not directory.is_dir():
            raise RuntimeError("historical_taf_artifact_dir_invalid")
        paths.extend(
            _path(value)
            for value in sorted(directory.glob("*.json"))
            if value.is_file()
        )
    unique = sorted({str(value): value for value in paths}.values(), key=str)
    if len(unique) > MAX_TAF_ARTIFACTS:
        raise RuntimeError("historical_taf_artifact_count_exceeded")
    return unique


def _load_taf_artifacts(
    explicit: Sequence[Path],
    artifact_dir: Path | None,
) -> dict[tuple[str, str, str, str], Path]:
    result: dict[tuple[str, str, str, str], Path] = {}
    for source in _artifact_paths(explicit, artifact_dir):
        raw = _mapping(
            load_bounded_json(source),
            reason=f"historical_taf_artifact_mapping_required:{source}",
        )
        scope = (
            str(raw.get("station_id") or "").strip().upper(),
            str(raw.get("season_bin") or "").strip(),
            str(raw.get("lead_bucket") or "").strip(),
            str(raw.get("run_cycle") or "").strip(),
        )
        if any(not value for value in scope):
            raise RuntimeError(f"historical_taf_artifact_scope_invalid:{source}")
        if scope in result:
            raise RuntimeError("historical_taf_artifact_scope_not_unique")
        result[scope] = source
    return result


def _assert_capture_table(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (REQUIRED_HISTORICAL_TAF_TABLE,),
    ).fetchone()
    if row is None:
        raise RuntimeError("historical_taf_capture_schema_missing")


def _resolve_scope_contract(
    live_inbox: Mapping[str, Any],
    scope: tuple[str, str, str, str],
) -> tuple[Any, dict[str, Any], dict[str, tuple[float | None, float | None]]]:
    station_id, season_bin, lead_bucket, run_cycle = scope
    return resolve_native_live_contract(
        live_inbox,
        station_id=station_id,
        season_bin=season_bin,
        lead_bucket=lead_bucket,
        run_cycle=run_cycle,
    )


def _build_exact_join(
    conn: sqlite3.Connection,
    live_inbox: Mapping[str, Any],
    scope: tuple[str, str, str, str],
) -> dict[str, Any]:
    envelope, fingerprint, intervals = _resolve_scope_contract(live_inbox, scope)
    exact_join = build_taf_native_exact_join(
        conn,
        station_id=envelope.station_id,
        season_bin=scope[1],
        lead_bucket=scope[2],
        run_cycle=scope[3],
        settlement_definition=str(envelope.lineage["settlement_definition"]),
        settlement_timezone=envelope.settlement_timezone,
        ensemble_system_ids=[str(value) for value in fingerprint["ensemble_system_ids"]],
        independence_family_ids=[
            str(value) for value in fingerprint["independence_family_ids"]
        ],
        family_weight_caps={
            str(key): float(value)
            for key, value in fingerprint["family_weight_caps"].items()
        },
        bucket_intervals=intervals,
    )
    return exact_join


def _taf_oos_report(
    *,
    exact_join: Mapping[str, Any],
    live_p0_inbox_path: Path,
    taf_artifact_path: Path | None,
    scope: tuple[str, str, str, str],
) -> dict[str, Any] | None:
    if taf_artifact_path is None:
        return None
    # The existing report builder is the single owner of TAF OOS logic and
    # its 24-sample gate.  A disposable bounded file supplies the exact join
    # without creating a second permanent artifact or mutating the DB.
    with TemporaryDirectory(prefix="historical-taf-p0m-") as temporary:
        exact_path = Path(temporary) / "exact_join.json"
        write_bounded_json(exact_path, dict(exact_join))
        return build_taf_native_ensemble_oos_report(
            exact_join_path=exact_path,
            live_p0_inbox_path=live_p0_inbox_path,
            taf_artifact_path=taf_artifact_path,
            station_id=scope[0],
            season_bin=scope[1],
            lead_bucket=scope[2],
            run_cycle=scope[3],
            minimum_sample_n=MIN_TAF_OOS_SAMPLE_N,
            output_path=None,
        )


def _blocked_candidate(
    *,
    exact_join: Mapping[str, Any],
    report: Mapping[str, Any] | None,
    archive_blocker_count: int,
    reason: str | None = None,
) -> dict[str, Any]:
    sample_n = int((report or {}).get("sample_n") or 0)
    reasons = []
    if reason:
        reasons.append(reason)
    if archive_blocker_count:
        reasons.append("historical_taf_archive_blocked")
    if report is None:
        reasons.append("taf_revision_artifact_required")
    elif report.get("validated") is not True:
        reasons.extend(
            str(item.get("code") or "taf_native_oos_blocked")
            for item in (report.get("gaps") or [])
            if isinstance(item, Mapping)
        )
        if not reasons:
            reasons.append("taf_native_oos_not_validated")
    if sample_n < MIN_TAF_OOS_SAMPLE_N:
        reasons.append("taf_native_oos_sample_n_insufficient")
    identity = {
        "schema": P0M_CANDIDATE_SCHEMA,
        "state": "BLOCKED",
        "sample_n": sample_n,
        "minimum_sample_n": MIN_TAF_OOS_SAMPLE_N,
        "exact_join_content_sha256": str(exact_join.get("content_sha256") or ""),
        "taf_oos_report_content_sha256": (
            str((report or {}).get("content_sha256") or "")
        ),
        "blockers": sorted(set(reasons)),
        "production_p0m_materialized": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def _candidate(
    *,
    exact_join: Mapping[str, Any],
    report: Mapping[str, Any] | None,
    archive_blocker_count: int,
) -> dict[str, Any]:
    if (
        report is not None
        and report.get("validated") is True
        and int(report.get("sample_n") or 0) >= MIN_TAF_OOS_SAMPLE_N
        and archive_blocker_count == 0
    ):
        identity = {
            "schema": P0M_CANDIDATE_SCHEMA,
            "state": "READY",
            "sample_n": int(report["sample_n"]),
            "minimum_sample_n": MIN_TAF_OOS_SAMPLE_N,
            "exact_join_content_sha256": str(exact_join["content_sha256"]),
            "taf_oos_report_content_sha256": str(report["content_sha256"]),
            "production_p0m_materialized": False,
            "calls_order_gateway": False,
            "orders_submitted": 0,
        }
        return {**identity, "content_sha256": content_sha256(identity)}
    return _blocked_candidate(
        exact_join=exact_join,
        report=report,
        archive_blocker_count=archive_blocker_count,
    )


def _scope_result(
    conn: sqlite3.Connection,
    *,
    live_inbox: Mapping[str, Any],
    live_p0_inbox_path: Path,
    scope: tuple[str, str, str, str],
    artifact_by_scope: Mapping[tuple[str, str, str, str], Path],
    archive_blockers_by_station: Mapping[str, int],
) -> dict[str, Any]:
    exact_join = _build_exact_join(conn, live_inbox, scope)
    report = _taf_oos_report(
        exact_join=exact_join,
        live_p0_inbox_path=live_p0_inbox_path,
        taf_artifact_path=artifact_by_scope.get(scope),
        scope=scope,
    )
    candidate = _candidate(
        exact_join=exact_join,
        report=report,
        archive_blocker_count=int(archive_blockers_by_station.get(scope[0], 0)),
    )
    return {
        "station_id": scope[0],
        "season_bin": scope[1],
        "lead_bucket": scope[2],
        "run_cycle": scope[3],
        "exact_join": exact_join,
        "taf_oos_report": report,
        "p0m_candidate": candidate,
    }


def _read_db(path: Path) -> tuple[sqlite3.Connection, bool]:
    database = resolve_target_database(path)
    if not database.is_file():
        raise RuntimeError("historical_taf_pipeline_database_missing")
    uri = f"file:{database}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=60.0)
    conn.execute("PRAGMA query_only=ON")
    _assert_capture_table(conn)
    return conn, False


def _write_db(path: Path) -> tuple[sqlite3.Connection, bool]:
    database = resolve_target_database(path)
    if not database.is_file():
        raise RuntimeError("historical_taf_pipeline_database_missing")
    conn = sqlite3.connect(database, timeout=60.0)
    conn.execute("PRAGMA busy_timeout=60000")
    assert_connection_target_capacity(conn, recover_oversized_wal=True)
    _assert_capture_table(conn)
    conn.execute("BEGIN IMMEDIATE")
    return conn, True


def run_pipeline(
    *,
    db_path: Path,
    live_p0_inbox_path: Path,
    archives: Sequence[Mapping[str, Any]],
    archive_metadata: Sequence[Mapping[str, Any]],
    taf_artifact_by_scope: Mapping[tuple[str, str, str, str], Path],
    requested_stations: Sequence[str] = (),
    commit: bool = False,
) -> dict[str, Any]:
    """Run one bounded pipeline; this function is intentionally testable."""

    if len(archives) != len(archive_metadata) or len(archives) > MAX_ARCHIVES:
        raise RuntimeError("historical_taf_archive_scope_invalid")
    live_inbox = _mapping(
        load_bounded_json(live_p0_inbox_path),
        reason="historical_taf_live_p0_inbox_mapping_required",
    )
    _verify_content_hash(live_inbox, name="historical_taf_live_p0_inbox")
    scopes = _discover_live_scopes(live_inbox)
    requested = {
        str(value).strip().upper()
        for value in requested_stations
        if str(value).strip()
    }
    if requested:
        scopes = [scope for scope in scopes if scope[0] in requested]
        if set(requested) - {scope[0] for scope in scopes}:
            raise RuntimeError("historical_taf_requested_station_scope_missing")
    if not scopes:
        raise RuntimeError("historical_taf_live_scope_empty")
    if len(scopes) > MAX_SCOPES or len({scope[0] for scope in scopes}) > MAX_STATIONS:
        raise RuntimeError("historical_taf_live_station_scope_exceeded")

    archives_by_station: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    metadata_by_station: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for archive, metadata in zip(archives, archive_metadata):
        station = str(metadata["station_id"])
        archives_by_station[station].append(archive)
        metadata_by_station[station].append(metadata)
    archive_blockers_by_station = {
        station: sum(int(row.get("blocker_count") or 0) for row in rows)
        for station, rows in metadata_by_station.items()
    }
    missing_archives = {
        scope[0] for scope in scopes if scope[0] not in archives_by_station
    }
    if missing_archives:
        for station in missing_archives:
            archive_blockers_by_station[station] = 1

    connection_factory = _write_db if commit else _read_db
    conn, is_writer = connection_factory(db_path)
    receipts: list[dict[str, Any]] = []
    try:
        if is_writer:
            try:
                receipts = [
                    ingest_historical_taf_calibration(conn, archive)
                    for archive in archives
                ]
            except Exception:
                conn.rollback()
                raise
        results = [
            _scope_result(
                conn,
                live_inbox=live_inbox,
                live_p0_inbox_path=live_p0_inbox_path,
                scope=scope,
                artifact_by_scope=taf_artifact_by_scope,
                archive_blockers_by_station=archive_blockers_by_station,
            )
            for scope in scopes
        ]
        if is_writer:
            conn.commit()
    except Exception:
        if is_writer:
            conn.rollback()
        raise
    finally:
        conn.close()

    ready_count = sum(
        result["p0m_candidate"]["state"] == "READY"
        for result in results
    )
    identity = {
        "schema": SCHEMA,
        "state": (
            "COMMITTED" if commit else "VALIDATED_NOT_COMMITTED"
        ),
        "candidate_state": (
            "READY" if ready_count == len(results) else "BLOCKED"
        ),
        "minimum_taf_oos_sample_n": MIN_TAF_OOS_SAMPLE_N,
        "archive_count": len(archives),
        "station_count": len({str(item["station_id"]) for item in archive_metadata}),
        "scope_count": len(results),
        "ingest_receipts": receipts,
        "archive_metadata": list(archive_metadata),
        "results": results,
        "boundary": {
            "strict_as_of": True,
            "historical_replay_only": True,
            "production_p0m_materialized": False,
            "writes_database": bool(commit),
            "writes_production_db": False,
            "calls_order_gateway": False,
            "orders_submitted": 0,
            "live_submit_authorized": False,
        },
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_TARGET_DB)
    parser.add_argument("--live-p0-inbox", type=Path, required=True)
    parser.add_argument("--archive", type=Path, action="append", default=[])
    parser.add_argument("--archive-dir", type=Path)
    parser.add_argument("--taf-artifact", type=Path, action="append", default=[])
    parser.add_argument("--taf-artifact-dir", type=Path)
    parser.add_argument("--station-id", action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Explicitly commit historical TAF captures; absent means read-only.",
    )
    args = parser.parse_args(argv)

    db_path = _path(args.db)
    live_p0_inbox_path = _path(args.live_p0_inbox)
    output_path = _path(args.output, must_exist=False)
    if output_path == db_path or output_path == live_p0_inbox_path:
        raise RuntimeError("historical_taf_pipeline_output_input_collision")
    archives, archive_metadata = _load_archives(args.archive, args.archive_dir)
    artifact_by_scope = _load_taf_artifacts(
        args.taf_artifact,
        args.taf_artifact_dir,
    )
    result = run_pipeline(
        db_path=db_path,
        live_p0_inbox_path=live_p0_inbox_path,
        archives=archives,
        archive_metadata=archive_metadata,
        taf_artifact_by_scope=artifact_by_scope,
        requested_stations=args.station_id,
        commit=bool(args.commit),
    )
    wrote = write_bounded_json_if_changed(output_path, result)
    print(json.dumps({
        "state": result["state"],
        "candidate_state": result["candidate_state"],
        "archive_count": result["archive_count"],
        "station_count": result["station_count"],
        "scope_count": result["scope_count"],
        "minimum_taf_oos_sample_n": result["minimum_taf_oos_sample_n"],
        "output": str(output_path),
        "wrote": wrote,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }, ensure_ascii=False, sort_keys=True))
    return 0 if result["candidate_state"] == "READY" else 2


if __name__ == "__main__":
    raise SystemExit(main())
