#!/usr/bin/env python3
"""Audit retained native hourly-curve receipts for the 45-station P1 lane.

This is a read-only lineage audit.  It distinguishes a genuinely missing
hourly curve from a curve that exists only in a *late historical archive
query*.  The latter can support offline inspection but cannot be attached to
an earlier P0/P0-M root or used to manufacture a P1 training fold, because
its receipt was not available during the station day.

No model raw object is fetched and neither SQLite database is modified.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import ContractError, content_sha256  # noqa: E402
from bayes_basket_target.database import DEFAULT_TARGET_DB, resolve_target_database  # noqa: E402
from bayes_basket_target.future_temperature_curve import (  # noqa: E402
    build_native_family_temperature_curve,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    load_bounded_json,
    write_bounded_json_if_changed,
)
from bayes_basket_target.station_registry import coordinate_registry_manifest  # noqa: E402


SCHEMA = "bayes_basket_target_p1_hourly_curve_receipt_audit_45station_v1"
MANIFEST_SCHEMA = "bayes_basket_target_hourly_v3_multimodel_manifest_v1"
FORWARD_INBOX_SCHEMA = "native_ensemble_forward_inbox_v1"
MAX_STATIONS = 45
MAX_EVENT_DATES = 31
MAX_PACKETS_PER_INBOX = 45
MAX_INPUT_BYTES = 16 * 1024 * 1024
MAX_OUTPUT_BYTES = 8 * 1024 * 1024
DEFAULT_MANIFEST = (
    ROOT
    / "output/audits/native_ensemble_history/v3_hourly/"
    "hourly_v3_multimodel_manifest_20260727_20260731.json"
)


def _path(value: Path, *, must_exist: bool = True) -> Path:
    result = Path(value).expanduser()
    if result.is_symlink():
        raise RuntimeError("p1_hourly_curve_audit_symlink_path_forbidden")
    return result.resolve(strict=must_exist)


def _sha(name: str, value: Any) -> str:
    result = str(value or "").strip().lower()
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
        raise ContractError(f"p1_hourly_curve_audit_{name}_invalid")
    return result


def _verify_content_hash(value: Mapping[str, Any], *, name: str) -> None:
    supplied = _sha(f"{name}_content_sha256", value.get("content_sha256"))
    body = {key: child for key, child in value.items() if key != "content_sha256"}
    if content_sha256(body) != supplied:
        raise ContractError(f"p1_hourly_curve_audit_{name}_content_sha_mismatch")


def _utc(name: str, value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ContractError(f"p1_hourly_curve_audit_{name}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"p1_hourly_curve_audit_{name}_timezone_required")
    return parsed.astimezone(timezone.utc)


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _station_day_end_utc(*, event_date: str, settlement_timezone: str) -> str:
    try:
        zone = ZoneInfo(settlement_timezone)
        start = datetime.fromisoformat(event_date).replace(tzinfo=zone)
    except (TypeError, ValueError) as exc:
        raise ContractError("p1_hourly_curve_audit_event_day_invalid") from exc
    return _stamp((start + timedelta(days=1)).astimezone(timezone.utc))


def classify_hourly_curve_receipt(
    *,
    family_count: int,
    minimum_point_count: int,
    available_at_utc: str,
    station_day_end_utc: str,
    probability_root_count: int,
    root_curve_bound_count: int,
) -> tuple[str, list[str]]:
    """Classify one receipt without ever treating an archive query as arrival."""

    blockers: list[str] = []
    if family_count < 2 or minimum_point_count < 4:
        blockers.append("hourly_curve_content_incomplete")
    if _utc("curve_available_at", available_at_utc) >= _utc(
        "station_day_end", station_day_end_utc
    ):
        blockers.append("archive_query_after_station_day_not_strict_asof")
    if probability_root_count <= 0:
        blockers.append("materialized_p0_root_missing")
    if root_curve_bound_count <= 0:
        blockers.append("materialized_p0_root_curve_binding_missing")
    if not blockers:
        # A historical archive path is intentionally never promoted here.  A
        # future proof of same-day publication/arrival is a different source
        # contract and must be handled by the forward P0 producer.
        return "RECEIPT_REQUIRES_FORWARD_ARRIVAL_AUTHORITY", []
    return "ARCHIVE_CURVE_PRESENT_UNBINDABLE", blockers


def _open_target_db(path: Path) -> sqlite3.Connection:
    database = resolve_target_database(path)
    if not database.is_file():
        raise RuntimeError("p1_hourly_curve_audit_target_database_missing")
    conn = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _root_counts(conn: sqlite3.Connection) -> dict[tuple[str, str], dict[str, int]]:
    rows = conn.execute(
        "SELECT station_id,event_date,lineage_json FROM basket_probability_envelope "
        "WHERE state='COMMITTED' AND stage IN ('P0','P0-M')"
    ).fetchall()
    counts: dict[tuple[str, str], dict[str, int]] = {}
    for row in rows:
        key = (str(row[0]).upper(), str(row[1]))
        bucket = counts.setdefault(key, {"probability_root_count": 0, "root_curve_bound_count": 0})
        bucket["probability_root_count"] += 1
        try:
            lineage = json.loads(str(row[2] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ContractError("p1_hourly_curve_audit_root_lineage_invalid") from exc
        if not isinstance(lineage, Mapping):
            raise ContractError("p1_hourly_curve_audit_root_lineage_mapping_required")
        if (
            str(lineage.get("conditional_future_temperature_curve_json") or "")
            and str(lineage.get("conditional_future_temperature_curve_sha256") or "")
        ):
            bucket["root_curve_bound_count"] += 1
    return counts


def _manifest_sources(manifest: Mapping[str, Any], *, manifest_path: Path) -> list[Path]:
    if manifest.get("schema") != MANIFEST_SCHEMA:
        raise ContractError("p1_hourly_curve_audit_manifest_schema_invalid")
    _verify_content_hash(manifest, name="manifest")
    stations = tuple(str(item).upper() for item in coordinate_registry_manifest()["station_ids"])
    if len(stations) != MAX_STATIONS or int(manifest.get("station_count") or 0) != MAX_STATIONS:
        raise ContractError("p1_hourly_curve_audit_station_count_not_45")
    dates = manifest.get("event_dates")
    if not isinstance(dates, list) or not dates or len(dates) > MAX_EVENT_DATES:
        raise ContractError("p1_hourly_curve_audit_event_dates_invalid")
    source_rows = manifest.get("source_artifacts")
    if not isinstance(source_rows, list):
        raise ContractError("p1_hourly_curve_audit_manifest_sources_invalid")
    expected: list[Path] = []
    for row in source_rows:
        if not isinstance(row, Mapping):
            continue
        if str(row.get("source_class") or "") != "official_native_archive_query":
            continue
        raw_path = Path(str(row.get("path") or ""))
        source = _path(raw_path)
        payload = load_bounded_json(source, limit=MAX_INPUT_BYTES)
        if not isinstance(payload, Mapping):
            raise ContractError("p1_hourly_curve_audit_inbox_mapping_required")
        if _sha("manifest_source", row.get("content_sha256")) != _sha(
            "inbox_content", payload.get("content_sha256")
        ):
            raise ContractError("p1_hourly_curve_audit_manifest_source_sha_mismatch")
        expected.append(source)
    if len(expected) != len(dates) or len(set(expected)) != len(expected):
        raise ContractError("p1_hourly_curve_audit_official_inbox_set_invalid")
    # Only evidence retained under the manifest is accepted; callers cannot
    # smuggle in a newer unreferenced raw object through this scanner.
    return sorted(expected)


def _inbox_rows(paths: Sequence[Path]) -> dict[tuple[str, str], dict[str, Any]]:
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for path in paths:
        inbox = load_bounded_json(path, limit=MAX_INPUT_BYTES)
        if not isinstance(inbox, Mapping) or inbox.get("schema") != FORWARD_INBOX_SCHEMA:
            raise ContractError("p1_hourly_curve_audit_forward_inbox_schema_invalid")
        _verify_content_hash(inbox, name="forward_inbox")
        packets = inbox.get("packets")
        if not isinstance(packets, list) or len(packets) != MAX_PACKETS_PER_INBOX:
            raise ContractError("p1_hourly_curve_audit_forward_inbox_packet_count_invalid")
        for packet in packets:
            if not isinstance(packet, Mapping):
                raise ContractError("p1_hourly_curve_audit_forward_packet_invalid")
            station = str(packet.get("station_id") or "").strip().upper()
            event_date = str(packet.get("event_date") or "").strip()
            timezone_name = str(packet.get("settlement_timezone") or "").strip()
            if not station or not event_date or not timezone_name:
                raise ContractError("p1_hourly_curve_audit_forward_packet_scope_invalid")
            systems = packet.get("systems")
            if not isinstance(systems, list):
                raise ContractError("p1_hourly_curve_audit_forward_packet_systems_invalid")
            sealed_curves: list[dict[str, Any]] = []
            for system in systems:
                if not isinstance(system, Mapping):
                    raise ContractError("p1_hourly_curve_audit_forward_system_invalid")
                raw_curve = system.get("family_temperature_curve")
                if not isinstance(raw_curve, Mapping):
                    raise ContractError("p1_hourly_curve_audit_family_curve_missing")
                sealed = build_native_family_temperature_curve(raw_curve)
                if str(raw_curve.get("content_sha256") or "") != sealed["content_sha256"]:
                    raise ContractError("p1_hourly_curve_audit_family_curve_sha_mismatch")
                if sealed["station_id"] != station or sealed["event_date"] != event_date:
                    raise ContractError("p1_hourly_curve_audit_family_curve_scope_mismatch")
                sealed_curves.append(sealed)
            key = (station, event_date)
            if key in result:
                raise ContractError("p1_hourly_curve_audit_station_day_duplicate")
            result[key] = {
                "path": str(path),
                "inbox_content_sha256": _sha("inbox_content", inbox.get("content_sha256")),
                "packet_content_sha256": _sha(
                    "packet_snapshot", packet.get("snapshot_content_sha256")
                ),
                "station_id": station,
                "event_date": event_date,
                "settlement_timezone": timezone_name,
                "source_class": str(packet.get("source_class") or ""),
                "archive_arrival_claimed": False,
                "family_curves": sealed_curves,
            }
    return result


def build(*, manifest_path: Path, target_db: Path) -> dict[str, Any]:
    manifest = load_bounded_json(manifest_path, limit=MAX_INPUT_BYTES)
    if not isinstance(manifest, Mapping):
        raise ContractError("p1_hourly_curve_audit_manifest_mapping_required")
    inbox_paths = _manifest_sources(manifest, manifest_path=manifest_path)
    rows = _inbox_rows(inbox_paths)
    stations = tuple(str(item).upper() for item in coordinate_registry_manifest()["station_ids"])
    event_dates = tuple(str(value) for value in manifest["event_dates"])
    expected_keys = {(station, event_date) for station in stations for event_date in event_dates}
    if set(rows) != expected_keys:
        raise ContractError("p1_hourly_curve_audit_station_day_scope_incomplete")
    conn = _open_target_db(target_db)
    try:
        roots = _root_counts(conn)
    finally:
        conn.close()
    entries: list[dict[str, Any]] = []
    for key in sorted(rows):
        row = rows[key]
        curves = list(row["family_curves"])
        families = sorted({str(curve["independence_family_id"]) for curve in curves})
        minimum_points = min((len(curve["points"]) for curve in curves), default=0)
        available_at = max(
            str(curve["available_at_utc"]) for curve in curves
        )
        day_end = _station_day_end_utc(
            event_date=str(row["event_date"]),
            settlement_timezone=str(row["settlement_timezone"]),
        )
        root_counts = roots.get(key, {"probability_root_count": 0, "root_curve_bound_count": 0})
        state, blockers = classify_hourly_curve_receipt(
            family_count=len(families),
            minimum_point_count=minimum_points,
            available_at_utc=available_at,
            station_day_end_utc=day_end,
            probability_root_count=int(root_counts["probability_root_count"]),
            root_curve_bound_count=int(root_counts["root_curve_bound_count"]),
        )
        entries.append({
            "station_id": key[0],
            "event_date": key[1],
            "settlement_timezone": row["settlement_timezone"],
            "state": state,
            "blockers": blockers,
            "source_class": row["source_class"],
            "archive_arrival_claimed": False,
            "inbox_content_sha256": row["inbox_content_sha256"],
            "packet_snapshot_sha256": row["packet_content_sha256"],
            "family_curve_sha256_by_family": {
                str(curve["independence_family_id"]): str(curve["content_sha256"])
                for curve in curves
            },
            "independence_family_count": len(families),
            "minimum_family_curve_point_count": minimum_points,
            "available_at_utc": available_at,
            "station_day_end_utc": day_end,
            "strict_asof_bindable": False,
            "probability_root_count": int(root_counts["probability_root_count"]),
            "root_curve_bound_count": int(root_counts["root_curve_bound_count"]),
            "next_action": {
                "action": "CAPTURE_FORWARD_NATIVE_HOURLY_CURVE_WITH_ARRIVAL",
                "required_evidence": "same-capture official-native publication/arrival receipt at or before the P0/P1 cut-off",
                "automatic_write_authorized": False,
            },
        })
    blocker_counts = Counter(
        blocker for entry in entries for blocker in entry["blockers"]
    )
    identity = {
        "schema": SCHEMA,
        "state": "ARCHIVE_CURVES_PRESENT_FORWARD_ARRIVAL_REQUIRED",
        "canonical_station_count": len(stations),
        "event_dates": list(event_dates),
        "station_day_count": len(entries),
        "entries": entries,
        "summary": {
            "complete_native_curve_station_day_count": sum(
                int(
                    entry["independence_family_count"] >= 2
                    and entry["minimum_family_curve_point_count"] >= 4
                )
                for entry in entries
            ),
            "strict_asof_bindable_station_day_count": sum(
                int(entry["strict_asof_bindable"]) for entry in entries
            ),
            "root_curve_bound_station_day_count": sum(
                int(entry["root_curve_bound_count"] > 0) for entry in entries
            ),
            "blocker_counts": dict(sorted(blocker_counts.items())),
        },
        "input_artifacts": {
            "manifest": {
                "path": str(manifest_path),
                "content_sha256": _sha("manifest_content", manifest.get("content_sha256")),
            },
            "official_native_hourly_inbox_paths": [str(path) for path in inbox_paths],
            "target_db": str(resolve_target_database(target_db)),
        },
        "boundary": {
            "read_only": True,
            "network_fetches": 0,
            "writes_target_database": False,
            "writes_canonical_database": False,
            "writes_production_p0_or_p0m": False,
            "writes_p1_artifact": False,
            "calls_order_gateway": False,
            "orders_submitted": 0,
            "forbids_historical_arrival_inference": True,
        },
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--target-db", type=Path, default=DEFAULT_TARGET_DB)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    manifest_path = _path(args.manifest)
    database = _path(args.target_db)
    output = _path(args.output, must_exist=False)
    if output in {manifest_path, database}:
        raise RuntimeError("p1_hourly_curve_audit_output_input_collision")
    result = build(manifest_path=manifest_path, target_db=database)
    wrote = write_bounded_json_if_changed(output, result, limit=MAX_OUTPUT_BYTES)
    print(json.dumps({
        "state": result["state"],
        "canonical_station_count": result["canonical_station_count"],
        "station_day_count": result["station_day_count"],
        "summary": result["summary"],
        "output": str(output),
        "wrote": wrote,
        "network_fetches": 0,
        "writes_target_database": False,
        "orders_submitted": 0,
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
