#!/usr/bin/env python3
"""Rebuild TAF exact-join quarantine evidence from persisted local receipts.

This command is intentionally read-only with respect to the SSOT database.
It re-runs the existing ``build_taf_native_exact_join`` query against the
current P0 inbox and writes a new, content-addressed audit directory.  No
TAF is fetched, no historical revision is invented, and no P0-M or production
row is materialized.  The old exact-join artifacts remain untouched.

The source database stores receipt pointers (including ``source_object`` and
``content`` hashes), not all raw TAF bytes.  The receipt is therefore recorded
as the evidence boundary; a missing local raw object is reported explicitly
as ``pointer_only_not_retained`` rather than being treated as a valid raw
payload.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.fleet import expected_station_count
from bayes_basket_target.contracts import ContractError, content_sha256  # noqa: E402
from bayes_basket_target.database import DEFAULT_TARGET_DB, resolve_target_database  # noqa: E402
from bayes_basket_target.native_ensemble_training_facts import (  # noqa: E402
    build_taf_native_exact_join,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    MAX_JSON_INPUT_BYTES,
    MAX_JSON_OUTPUT_BYTES,
    load_bounded_json,
    write_bounded_json_if_changed,
)
from bayes_basket_target.station_registry import coordinate_registry_manifest  # noqa: E402
from bayes_basket_target.taf_native_ensemble_oos import (  # noqa: E402
    resolve_native_live_contract,
)


SCHEMA = "bayes_basket_target_taf_exact_join_quarantine_rebuild_v1"
REVISION_QUARANTINE_SCHEMA = "bayes_basket_target_taf_revision_quarantine_receipt_v1"
MAX_STATIONS = expected_station_count()
# The exact-join builder deliberately caps a usable station-day at 16
# corrections and raises above that bound.  The receipt export has a wider
# (still bounded) cap so it does not hide the tail of an already quarantined
# day while explaining that fail-closed decision.
MAX_CORRECTIONS_PER_DAY = 64
MAX_CORRECTION_DAYS = MAX_STATIONS * 366
# Forward receipts are allowed to retain repeated network captures, but no
# consumer may infer a TAF revision chain from them unless every sequence is
# unique and contiguous.  Keep the audit bounded independently of the
# correction-pointer cap: non-correction TAF rows are necessary to prove that
# an AMD/COR has a valid root and predecessor.
MAX_FORWARD_REVISIONS_PER_DAY = 128
_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")


def _path(value: Path, *, must_exist: bool = True) -> Path:
    candidate = Path(value).expanduser()
    if candidate.is_symlink():
        raise RuntimeError("taf_quarantine_rebuild_symlink_path_forbidden")
    return candidate.resolve(strict=must_exist)


def _open_read_only(path: Path) -> sqlite3.Connection:
    database = resolve_target_database(path)
    if not database.is_file():
        raise RuntimeError("taf_quarantine_rebuild_database_missing")
    conn = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _mapping(value: Any, *, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractError(f"taf_quarantine_rebuild_{name}_mapping_required")
    return value


def _safe_filename(*parts: str) -> str:
    return "_".join(_SAFE.sub("-", str(part).strip()) for part in parts if str(part).strip())


def _p0_scopes(
    inbox: Mapping[str, Any],
    requested_stations: set[str],
) -> list[dict[str, Any]]:
    """Resolve current live contracts without reconstructing any model data."""

    wrappers = inbox.get("packets")
    if not isinstance(wrappers, list) or len(wrappers) > MAX_STATIONS:
        raise ContractError("taf_quarantine_rebuild_p0_packets_invalid")
    scopes: list[dict[str, Any]] = []
    seen: set[str] = set()
    for wrapper in wrappers:
        packet = _mapping(_mapping(wrapper, name="p0_wrapper").get("packet"), name="p0_packet")
        envelope = _mapping(packet.get("native_probability_envelope"), name="p0_envelope")
        lineage = _mapping(envelope.get("lineage"), name="p0_lineage")
        station = str(packet.get("station_id") or "").strip().upper()
        if not station or station in seen:
            raise ContractError("taf_quarantine_rebuild_p0_station_duplicate_or_missing")
        if requested_stations and station not in requested_stations:
            continue
        season_bin = str(lineage.get("season_bin") or "").strip()
        lead_bucket = str(lineage.get("lead_bucket") or "").strip()
        run_cycle = str(
            lineage.get("cross_calibration_scope_run_cycle")
            or lineage.get("run_cycle")
            or ""
        ).strip()
        settlement_definition = str(lineage.get("settlement_definition") or "").strip()
        timezone_name = str(packet.get("settlement_timezone") or "").strip()
        if not all((season_bin, lead_bucket, run_cycle, settlement_definition, timezone_name)):
            raise ContractError("taf_quarantine_rebuild_p0_scope_missing")
        # This call verifies the wrapper's packet hash and binds the scope to
        # the current P0 contract; it does not fetch or write anything.
        _envelope, fingerprint, intervals = resolve_native_live_contract(
            inbox,
            station_id=station,
            season_bin=season_bin,
            lead_bucket=lead_bucket,
            run_cycle=run_cycle,
        )
        seen.add(station)
        scopes.append({
            "station_id": station,
            "season_bin": season_bin,
            "lead_bucket": lead_bucket,
            "run_cycle": run_cycle,
            "settlement_definition": settlement_definition,
            "settlement_timezone": timezone_name,
            "ensemble_system_ids": [str(value) for value in fingerprint["ensemble_system_ids"]],
            "independence_family_ids": [str(value) for value in fingerprint["independence_family_ids"]],
            "family_weight_caps": {
                str(key): float(value)
                for key, value in fingerprint["family_weight_caps"].items()
            },
            "bucket_intervals": intervals,
        })
    if requested_stations and requested_stations - {row["station_id"] for row in scopes}:
        raise ContractError("taf_quarantine_rebuild_requested_station_scope_missing")
    return sorted(scopes, key=lambda row: str(row["station_id"]))


def _revision_quarantine_receipt(
    conn: sqlite3.Connection,
    *,
    p0_stations: set[str],
) -> dict[str, Any]:
    """Export all persisted forward AMD/COR pointers, grouped by station-day."""

    rows = conn.execute(
        "SELECT station_id,event_date,settlement_timezone,revision_kind,"
        "revision_id,revision_sequence,provider_available_at_utc,"
        "issued_at_utc,received_at_utc,valid_from_utc,valid_until_utc,"
        "source_object_sha256,source_capture_sha256,content_sha256,"
        "source_class,authority_scope FROM calibration_forward_taf_capture "
        "WHERE revision_kind IN ('AMD','COR') "
        "AND authority_scope='PRODUCTION_NO_LIVE_ONLY' "
        "ORDER BY station_id,event_date,provider_available_at_utc,revision_sequence "
        "LIMIT ?",
        (MAX_CORRECTION_DAYS * (MAX_CORRECTIONS_PER_DAY + 1),),
    ).fetchall()
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        record = {
            "station_id": str(row[0]).upper(),
            "event_date": str(row[1]),
            "settlement_timezone": str(row[2]),
            "revision_kind": str(row[3]),
            "revision_id": str(row[4]),
            "revision_sequence": int(row[5]),
            "provider_available_at_utc": str(row[6]),
            "issued_at_utc": str(row[7]),
            "received_at_utc": str(row[8]),
            "valid_from_utc": str(row[9]),
            "valid_until_utc": str(row[10]),
            "source_object_sha256": str(row[11]),
            "source_capture_sha256": str(row[12]),
            "content_sha256": str(row[13]),
            "source_class": str(row[14]),
            "authority_scope": str(row[15]),
            # A receipt hash is available in SSOT, but no path to the raw
            # bulletin is persisted in this table.  Never claim raw bytes.
            "raw_object_status": "pointer_only_not_retained",
            "quarantine_reason": "forward_taf_correction_requires_manual_review",
        }
        grouped[(record["station_id"], record["event_date"], record["settlement_timezone"])].append(record)
    days: list[dict[str, Any]] = []
    for (station, event_date, timezone_name), corrections in sorted(grouped.items()):
        overflow = len(corrections) > MAX_CORRECTIONS_PER_DAY
        try:
            truth = conn.execute(
                "SELECT truth_id,revision_no,canonical_committed_at_utc,"
                "source_payload_sha256,authority_scope FROM "
                "calibration_station_daily_high_truth WHERE station_id=? "
                "AND local_date=? AND source_lane='AWC' "
                "AND qc_status='ACCEPTED' ORDER BY revision_no DESC LIMIT 1",
                (station, event_date),
            ).fetchone()
        except sqlite3.OperationalError:
            # A stripped test/archive DB may not carry the truth table.  Keep
            # the receipt useful while explicitly marking truth as missing.
            truth = None
        days.append({
            "station_id": station,
            "event_date": event_date,
            "settlement_timezone": timezone_name,
            "current_p0_scope_present": station in p0_stations,
            "awc_truth_closed": truth is not None,
            "awc_truth": (
                None
                if truth is None
                else {
                    "truth_id": str(truth[0]),
                    "revision_no": int(truth[1]),
                    "canonical_committed_at_utc": str(truth[2] or ""),
                    "source_payload_sha256": str(truth[3]),
                    "authority_scope": str(truth[4]),
                    "source_lane": "AWC",
                    "qc_status": "ACCEPTED",
                }
            ),
            "manual_review_required": True,
            "correction_count": len(corrections),
            "corrections": corrections[:MAX_CORRECTIONS_PER_DAY],
            "correction_overflow": overflow,
            "blockers": (["correction_count_exceeds_bound"] if overflow else []),
        })
    chain_audit = _forward_revision_chain_audit(conn)
    identity = {
        "schema": REVISION_QUARANTINE_SCHEMA,
        "state": "RECEIPT_POINTERS_EXPORTED",
        "station_count": len({row["station_id"] for row in days}),
        "station_day_count": len(days),
        "correction_count": sum(int(row["correction_count"]) for row in days),
        "awc_truth_closed_station_day_count": sum(
            int(row["awc_truth_closed"]) for row in days
        ),
        "awc_truth_missing_station_day_count": sum(
            int(not row["awc_truth_closed"]) for row in days
        ),
        "current_p0_station_count": len(p0_stations),
        "revision_days": days,
        "forward_revision_chain_audit": chain_audit,
        "authority": {
            "source_lane": "AWC",
            "source_class": "official_forward_native_receipt",
            "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
            "raw_bytes_claimed": False,
        },
        "boundary": {
            "read_only": True,
            "network_fetches": 0,
            "writes_database": False,
            "writes_production_p0m": False,
            "calls_order_gateway": False,
            "orders_submitted": 0,
            "requires_manual_review_before_p0m": True,
            "ambiguous_forward_revision_chain_never_auto_resolved": True,
        },
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def _forward_revision_chain_audit(conn: sqlite3.Connection) -> dict[str, Any]:
    """Describe forward TAF-chain eligibility without inventing a sequence.

    The append-only capture table intentionally preserves every received
    official bulletin.  It may therefore contain multiple capture attempts
    which share an upstream sequence value.  Such a group is useful receipt
    evidence, but it is not a linear ForecastRevision chain and must not
    provide an AMD/COR predecessor for P0-M training.
    """

    rows = conn.execute(
        "SELECT station_id,event_date,settlement_timezone,revision_kind,"
        "revision_id,revision_sequence,provider_available_at_utc,"
        "issued_at_utc,received_at_utc,source_object_sha256,"
        "source_capture_sha256,content_sha256 "
        "FROM calibration_forward_taf_capture "
        "WHERE authority_scope='PRODUCTION_NO_LIVE_ONLY' "
        "ORDER BY station_id,event_date,revision_sequence,"
        "provider_available_at_utc,revision_id "
        "LIMIT ?",
        (MAX_CORRECTION_DAYS * MAX_FORWARD_REVISIONS_PER_DAY,),
    ).fetchall()
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        record = {
            "station_id": str(row[0]).upper(),
            "event_date": str(row[1]),
            "settlement_timezone": str(row[2]),
            "revision_kind": str(row[3]).upper(),
            "revision_id": str(row[4]),
            "revision_sequence": int(row[5]),
            "provider_available_at_utc": str(row[6]),
            "issued_at_utc": str(row[7]),
            "received_at_utc": str(row[8]),
            "source_object_sha256": str(row[9]),
            "source_capture_sha256": str(row[10]),
            "content_sha256": str(row[11]),
        }
        grouped[(record["station_id"], record["event_date"], record["settlement_timezone"])].append(record)

    station_days: list[dict[str, Any]] = []
    for (station, event_date, timezone_name), records in sorted(grouped.items()):
        overflow = len(records) > MAX_FORWARD_REVISIONS_PER_DAY
        bounded = records[:MAX_FORWARD_REVISIONS_PER_DAY]
        sequence_counts = Counter(int(item["revision_sequence"]) for item in bounded)
        duplicate_sequences = sorted(
            sequence for sequence, count in sequence_counts.items() if count > 1
        )
        expected = list(range(1, len(sequence_counts) + 1))
        actual = sorted(sequence_counts)
        timestamp_order_invalid = any(
            not (
                str(item["issued_at_utc"])
                <= str(item["provider_available_at_utc"])
                <= str(item["received_at_utc"])
            )
            for item in bounded
        )
        blockers: list[str] = []
        if overflow:
            blockers.append("forward_revision_count_exceeds_bound")
        if duplicate_sequences:
            blockers.append("forward_revision_sequence_duplicate")
        if actual != expected:
            blockers.append("forward_revision_sequence_gap")
        if not bounded or bounded[0]["revision_kind"] != "TAF":
            blockers.append("forward_revision_root_taf_missing")
        if timestamp_order_invalid:
            blockers.append("forward_revision_timestamp_order_invalid")
        state = "LINEAR_CHAIN_VERIFIED" if not blockers else "AMBIGUOUS_FAIL_CLOSED"
        station_days.append({
            "station_id": station,
            "event_date": event_date,
            "settlement_timezone": timezone_name,
            "state": state,
            "revision_count": len(records),
            "revision_count_bounded": len(bounded),
            "duplicate_revision_sequences": duplicate_sequences,
            "sequence_values": actual,
            "blockers": blockers,
            # Pointers only: the audit deliberately carries no raw bulletin.
            "revisions": bounded,
        })
    identity = {
        "schema": "bayes_basket_target_forward_taf_revision_chain_audit_v1",
        "state": (
            "ALL_LINEAR_CHAINS_VERIFIED"
            if station_days and all(
                item["state"] == "LINEAR_CHAIN_VERIFIED"
                for item in station_days
            )
            else "AMBIGUOUS_CHAINS_FAIL_CLOSED"
        ),
        "station_day_count": len(station_days),
        "linear_chain_station_day_count": sum(
            item["state"] == "LINEAR_CHAIN_VERIFIED" for item in station_days
        ),
        "ambiguous_station_day_count": sum(
            item["state"] != "LINEAR_CHAIN_VERIFIED" for item in station_days
        ),
        "station_days": station_days,
        "authority": {
            "source_lane": "AWC",
            "source_class": "official_forward_native_receipt",
            "raw_bytes_claimed": False,
            "duplicate_sequence_policy": "fail_closed_no_sequence_inference",
        },
        "boundary": {
            "read_only": True,
            "network_fetches": 0,
            "writes_database": False,
            "writes_production_p0m": False,
            "calls_order_gateway": False,
            "orders_submitted": 0,
        },
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def rebuild(
    *,
    db_path: Path,
    p0_inbox_path: Path,
    output_dir: Path,
    requested_stations: Sequence[str] = (),
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    inbox = _mapping(load_bounded_json(p0_inbox_path, limit=MAX_JSON_INPUT_BYTES), name="p0_inbox")
    scopes = _p0_scopes(
        inbox,
        {str(value).strip().upper() for value in requested_stations if str(value).strip()},
    )
    if len(scopes) > MAX_STATIONS:
        raise ContractError("taf_quarantine_rebuild_station_count_exceeded")
    canonical_station_ids = {
        str(value).upper()
        for value in coordinate_registry_manifest()["station_ids"]
    }
    if len(canonical_station_ids) != MAX_STATIONS:
        raise ContractError("taf_quarantine_rebuild_canonical_station_count_invalid")
    current_p0_station_ids = {row["station_id"] for row in scopes}
    conn = _open_read_only(db_path)
    entries: list[dict[str, Any]] = []
    try:
        for scope in scopes:
            exact_join = build_taf_native_exact_join(conn, **scope)
            filename = _safe_filename(
                scope["station_id"],
                scope["season_bin"],
                scope["lead_bucket"],
                scope["run_cycle"],
                "taf_native_exact_join.json",
            )
            destination = output_dir / filename
            wrote = write_bounded_json_if_changed(
                destination,
                exact_join,
                limit=MAX_JSON_OUTPUT_BYTES,
            )
            exclusions = exact_join.get("excluded_correction_samples") or []
            entries.append({
                "station_id": scope["station_id"],
                "scope": {
                    key: scope[key]
                    for key in (
                        "season_bin",
                        "lead_bucket",
                        "run_cycle",
                        "settlement_definition",
                        "settlement_timezone",
                    )
                },
                "artifact_path": str(destination),
                "artifact_content_sha256": exact_join["content_sha256"],
                "sample_count": int(exact_join["sample_count"]),
                "excluded_correction_sample_count": len(exclusions),
                "excluded_correction_samples": exclusions,
                "state": "QUARANTINED_DAYS_PRESENT" if exclusions else "NO_CORRECTION_DAYS_IN_JOIN",
                "wrote": wrote,
            })
        revision_receipt = _revision_quarantine_receipt(
            conn,
            p0_stations=current_p0_station_ids,
        )
    finally:
        conn.close()
    receipt_path = output_dir / "taf_revision_quarantine_receipt.json"
    receipt_wrote = write_bounded_json_if_changed(
        receipt_path,
        revision_receipt,
        limit=MAX_JSON_OUTPUT_BYTES,
    )
    identity = {
        "schema": SCHEMA,
        "state": "REBUILT_READ_ONLY",
        "canonical_station_count": len(canonical_station_ids),
        "canonical_station_ids": sorted(canonical_station_ids),
        "p0_station_count": len(scopes),
        "missing_current_p0_station_count": len(
            canonical_station_ids - current_p0_station_ids
        ),
        "missing_current_p0_station_ids": sorted(
            canonical_station_ids - current_p0_station_ids
        ),
        "exact_join_artifact_count": len(entries),
        "quarantined_exact_join_count": sum(
            int(row["excluded_correction_sample_count"]) > 0 for row in entries
        ),
        "entries": entries,
        "revision_quarantine_receipt": {
            "path": str(receipt_path),
            "content_sha256": revision_receipt["content_sha256"],
            "station_day_count": revision_receipt["station_day_count"],
            "correction_count": revision_receipt["correction_count"],
            "wrote": receipt_wrote,
        },
        "boundary": {
            "read_only": True,
            "network_fetches": 0,
            "writes_database": False,
            "writes_production_p0m": False,
            "calls_order_gateway": False,
            "orders_submitted": 0,
            "preserves_24_sample_gate": True,
            "old_artifacts_overwritten": False,
        },
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_TARGET_DB)
    parser.add_argument("--p0-inbox", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--station-id", action="append", default=[])
    args = parser.parse_args(argv)
    db_path = _path(args.db)
    inbox_path = _path(args.p0_inbox)
    output_dir = _path(args.output_dir, must_exist=False)
    if output_dir in {db_path, inbox_path}:
        raise RuntimeError("taf_quarantine_rebuild_output_input_collision")
    result = rebuild(
        db_path=db_path,
        p0_inbox_path=inbox_path,
        output_dir=output_dir,
        requested_stations=args.station_id,
    )
    manifest_path = output_dir / "manifest.json"
    wrote = write_bounded_json_if_changed(
        manifest_path,
        result,
        limit=MAX_JSON_OUTPUT_BYTES,
    )
    print(json.dumps({
        "state": result["state"],
        "p0_station_count": result["p0_station_count"],
        "exact_join_artifact_count": result["exact_join_artifact_count"],
        "quarantined_exact_join_count": result["quarantined_exact_join_count"],
        "revision_quarantine_receipt": result["revision_quarantine_receipt"],
        "manifest": str(manifest_path),
        "wrote": wrote,
        "network_fetches": 0,
        "writes_production_p0m": False,
        "orders_submitted": 0,
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
