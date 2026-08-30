#!/usr/bin/env python3
"""Build a read-only current-snapshot gap ledger for the 45-station lane.

This is deliberately a *diagnostic runner*, not an ingestion or recovery
tool.  It examines retained official-native and Open-Meteo packets at one
explicit UTC instant, records freshness and coverage separately, and never
creates P0/P0-M/P1, writes either database, or contacts a provider.  In
particular, a missing official packet is not evidence of HTTP 503: that label
is emitted only when a retained, content-addressed capture receipt says 503.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from typing import Any, Iterable, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.fleet import expected_station_count
from bayes_basket_target.contracts import ContractError, content_sha256  # noqa: E402
from bayes_basket_target.resource_bounds import (  # noqa: E402
    MAX_JSON_INPUT_BYTES,
    MAX_JSON_OUTPUT_BYTES,
    load_bounded_json,
    write_bounded_json_if_changed,
)
from bayes_basket_target.station_registry import coordinate_registry_manifest  # noqa: E402


SCHEMA = "bayes_basket_target_current_snapshot_gap_ledger_45station_v1"
INBOX_SCHEMA = "native_ensemble_forward_inbox_v1"
OFFICIAL_PACKET_SCHEMA = "official_native_ensemble_forward_packet_v1"
REPUBLISHER_PACKET_SCHEMA = "open_meteo_native_ensemble_forward_packet_v1"
P1_AUDIT_SCHEMA = "bayes_basket_target_p1_trajectory_45station_audit_v1"
P1_REPAIR_SCHEMA = "bayes_basket_target_p1_trajectory_repair_plan_v1"
MAX_PACKETS = 256
MAX_RECEIPTS = 512
MAX_SCOPES = expected_station_count() * 4


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _utc(value: str, *, field: str) -> tuple[str, datetime]:
    text = str(value or "").strip()
    if not text.endswith("Z"):
        raise ContractError(f"{field}_must_be_utc_z")
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as exc:
        raise ContractError(f"{field}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"{field}_timezone_required")
    normalized = parsed.astimezone(timezone.utc)
    return normalized.isoformat(timespec="milliseconds").replace("+00:00", "Z"), normalized


def _event_date(value: str, *, field: str) -> str:
    try:
        return datetime.strptime(str(value or ""), "%Y-%m-%d").date().isoformat()
    except ValueError as exc:
        raise ContractError(f"{field}_invalid") from exc


def _load_hashed_object(path: Path, *, label: str) -> tuple[dict[str, Any], str, Path]:
    requested = path.expanduser()
    if requested.is_symlink():
        raise ContractError(f"{label}_symlink_forbidden")
    resolved = requested.resolve(strict=True)
    payload = load_bounded_json(resolved, limit=MAX_JSON_INPUT_BYTES)
    if not isinstance(payload, Mapping):
        raise ContractError(f"{label}_not_object")
    identity = {key: value for key, value in payload.items() if key != "content_sha256"}
    supplied = str(payload.get("content_sha256") or "")
    if supplied != content_sha256(identity):
        raise ContractError(f"{label}_content_sha256_mismatch")
    return dict(payload), supplied, resolved


def _load_inbox(path: Path, *, packet_schema: str, label: str) -> tuple[dict[str, Any], str, Path]:
    payload, digest, resolved = _load_hashed_object(path, label=label)
    if payload.get("schema") != INBOX_SCHEMA:
        raise ContractError(f"{label}_schema_invalid")
    packets = payload.get("packets")
    if not isinstance(packets, list) or len(packets) > MAX_PACKETS:
        raise ContractError(f"{label}_packets_invalid")
    for packet in packets:
        if not isinstance(packet, Mapping) or packet.get("schema") != packet_schema:
            raise ContractError(f"{label}_packet_schema_invalid")
    return payload, digest, resolved


def _index_packets(
    packets: Iterable[Mapping[str, Any]], *, event_date: str, packet_schema: str,
    label: str, canonical: set[str],
) -> dict[str, Mapping[str, Any]]:
    indexed: dict[str, Mapping[str, Any]] = {}
    for packet in packets:
        if str(packet.get("event_date") or "") != event_date:
            continue
        station = str(packet.get("station_id") or "").strip().upper()
        if station not in canonical:
            raise ContractError(f"{label}_station_not_canonical:{station}")
        if packet.get("schema") != packet_schema:
            raise ContractError(f"{label}_packet_schema_invalid")
        prior = indexed.get(station)
        if prior is not None:
            prior_hash = str(prior.get("packet_content_sha256") or prior.get("snapshot_content_sha256") or "")
            current_hash = str(packet.get("packet_content_sha256") or packet.get("snapshot_content_sha256") or "")
            if not prior_hash or not current_hash or prior_hash != current_hash:
                raise ContractError(f"{label}_packet_scope_conflict:{station}:{event_date}")
            continue
        indexed[station] = packet
    return indexed


def _derive_event_date(*inboxes: Mapping[str, Any]) -> str:
    dates = {
        str(packet.get("event_date") or "")
        for inbox in inboxes
        for packet in inbox.get("packets") or ()
        if isinstance(packet, Mapping)
    }
    if len(dates) != 1:
        raise ContractError("current_snapshot_event_date_must_be_explicit")
    return _event_date(next(iter(dates)), field="event_date")


def _packet_freshness(packet: Mapping[str, Any] | None, *, as_of: datetime, max_age_seconds: int, kind: str) -> dict[str, Any]:
    if packet is None:
        return {"state": "MISSING", "reason": f"{kind}_PACKET_MISSING", "received_at_utc": None, "age_seconds": None, "fresh": False}
    # Open-Meteo packets do not claim a native receipt field.  Their
    # immutable aggregator snapshot timestamp is the only admissible
    # republisher freshness clock; never fall back to file mtime.
    received = packet.get("received_at_utc")
    if kind == "FALLBACK" and received in (None, ""):
        received = packet.get("aggregator_snapshot_at_utc")
    if received in (None, ""):
        return {"state": "TIMESTAMP_MISSING", "reason": f"{kind}_RECEIPT_TIMESTAMP_MISSING", "received_at_utc": None, "age_seconds": None, "fresh": False}
    normalized, timestamp = _utc(str(received), field=f"{kind.lower()}_received_at_utc")
    age = (as_of - timestamp).total_seconds()
    if age < 0:
        return {"state": "FUTURE_TIMESTAMP", "reason": f"{kind}_RECEIPT_AFTER_AS_OF", "received_at_utc": normalized, "age_seconds": age, "fresh": False}
    # Compare the unrounded duration.  Rounding before this gate would keep a
    # receipt 3600.999 seconds old alive under a 3600-second policy.
    age_seconds: int | float = int(age) if age.is_integer() else round(age, 3)
    if age > max_age_seconds:
        return {"state": "STALE", "reason": f"{kind}_SNAPSHOT_STALE", "received_at_utc": normalized, "age_seconds": age_seconds, "fresh": False}
    return {"state": "FRESH", "reason": None, "received_at_utc": normalized, "age_seconds": age_seconds, "fresh": True}


def _receipt_records(payload: Mapping[str, Any], *, digest: str) -> list[tuple[Mapping[str, Any], str]]:
    """Accept only a retained receipt object or a retained receipt envelope.

    The optional input intentionally has a narrow contract.  We do not scan
    arbitrary logs or use a file name, mtime, or a missing packet as a proxy
    for an HTTP status.
    """
    schema = str(payload.get("schema") or "")
    if schema in {"official_native_capture_receipt_v1", "official_native_http_capture_receipt_v1"}:
        return [(payload, digest)]
    if schema in {"official_native_capture_receipt_inbox_v1", "official_native_http_capture_receipt_inbox_v1"}:
        receipts = payload.get("receipts")
        if not isinstance(receipts, list) or len(receipts) > MAX_RECEIPTS:
            raise ContractError("official_capture_receipts_invalid")
        result: list[tuple[Mapping[str, Any], str]] = []
        for receipt in receipts:
            if not isinstance(receipt, Mapping):
                raise ContractError("official_capture_receipt_invalid")
            receipt_hash = str(receipt.get("content_sha256") or digest)
            if "content_sha256" in receipt:
                identity = {key: value for key, value in receipt.items() if key != "content_sha256"}
                if receipt_hash != content_sha256(identity):
                    raise ContractError("official_capture_receipt_content_sha256_mismatch")
            result.append((receipt, receipt_hash))
        return result
    raise ContractError("official_capture_receipt_schema_invalid")


def _retained_503_by_station(paths: Iterable[Path] | None, *, event_date: str, canonical: set[str]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = defaultdict(list)
    for path in paths or ():
        payload, digest, _ = _load_hashed_object(path, label="official_capture_receipt")
        for receipt, receipt_hash in _receipt_records(payload, digest=digest):
            station = str(receipt.get("station_id") or "").strip().upper()
            receipt_day = str(receipt.get("event_date") or "")
            if not station or receipt_day != event_date:
                continue
            if station not in canonical:
                raise ContractError(f"official_capture_receipt_station_not_canonical:{station}")
            status = receipt.get("http_status", receipt.get("status"))
            if isinstance(status, bool):
                raise ContractError("official_capture_receipt_status_invalid")
            if status == 503:
                result[station].append(receipt_hash)
    return {station: sorted(set(values)) for station, values in result.items()}


def _load_p1_pair(
    audit_path: Path, repair_path: Path, *, outer_as_of: datetime,
) -> tuple[Mapping[str, Any], Mapping[str, Any], str, str]:
    audit, audit_sha, _ = _load_hashed_object(audit_path, label="p1_audit")
    repair, repair_sha, _ = _load_hashed_object(repair_path, label="p1_repair")
    if audit.get("schema") != P1_AUDIT_SCHEMA or repair.get("schema") != P1_REPAIR_SCHEMA:
        raise ContractError("p1_evidence_pair_schema_invalid")
    audit_as_of, audit_at = _utc(str(audit.get("as_of_utc") or ""), field="p1_audit_as_of_utc")
    repair_as_of, repair_at = _utc(str(repair.get("as_of_utc") or ""), field="p1_repair_as_of_utc")
    if audit_as_of != repair_as_of:
        raise ContractError("p1_evidence_pair_as_of_mismatch")
    # P1 evidence is an input to this explicit current-snapshot boundary.  It
    # must already have existed at that boundary; a later file mtime cannot
    # turn future evidence into admissible evidence.
    if audit_at > outer_as_of or repair_at > outer_as_of:
        raise ContractError("p1_evidence_pair_after_outer_as_of")
    if str(repair.get("audit_content_sha256") or "") != audit_sha:
        raise ContractError("p1_evidence_pair_audit_hash_mismatch")
    if int(audit.get("scope_count") or MAX_SCOPES) != MAX_SCOPES or int(repair.get("scope_count") or 0) != MAX_SCOPES:
        raise ContractError("p1_repair_scope_count_invalid")
    canonical = set(coordinate_registry_manifest()["station_ids"])
    expected_scopes = {(station, bucket) for station in canonical for bucket in ("NIGHT", "MORNING", "AFTERNOON", "EVENING")}
    audit_scopes = audit.get("scopes")
    if not isinstance(audit_scopes, list):
        raise ContractError("p1_audit_scopes_invalid")
    observed_audit_scopes = {
        (str(item.get("station_id") or "").upper(), str(item.get("local_time_bucket") or "").upper())
        for item in audit_scopes if isinstance(item, Mapping)
    }
    if len(audit_scopes) != MAX_SCOPES or observed_audit_scopes != expected_scopes:
        raise ContractError("p1_audit_scope_identity_invalid")
    repair_scopes = repair.get("scopes")
    if not isinstance(repair_scopes, list):
        raise ContractError("p1_repair_scopes_invalid")
    observed_repair_scopes = {
        (str(item.get("station_id") or "").upper(), str(item.get("local_time_bucket") or "").upper())
        for item in repair_scopes if isinstance(item, Mapping)
    }
    if len(repair_scopes) != MAX_SCOPES or observed_repair_scopes != expected_scopes:
        raise ContractError("p1_repair_scope_identity_invalid")
    return audit, repair, audit_sha, repair_sha


def _p1_rebuild_state(audit: Mapping[str, Any], repair: Mapping[str, Any], *, audit_sha: str, repair_sha: str) -> dict[str, Any]:
    usable = int(repair.get("usable_candidate_pointer_count") or 0)
    raw_scopes = repair.get("scopes")
    if not isinstance(raw_scopes, list) or len(raw_scopes) != MAX_SCOPES:
        raise ContractError("p1_repair_scopes_invalid")
    queue: list[dict[str, Any]] = []
    for scope in raw_scopes:
        if not isinstance(scope, Mapping):
            raise ContractError("p1_repair_scope_invalid")
        station = str(scope.get("station_id") or "").upper()
        bucket = str(scope.get("local_time_bucket") or "").upper()
        for step in scope.get("repair_steps") or ():
            if not isinstance(step, Mapping):
                raise ContractError("p1_repair_step_invalid")
            queue.append({
                "station_id": station,
                "local_time_bucket": bucket,
                "blocker": str(step.get("blocker") or ""),
                "action": str(step.get("action") or ""),
                "required_evidence": str(step.get("required_evidence") or ""),
                "automatic": False,
                "old_root_rebind_authorized": False,
            })
    queue.sort(key=lambda item: (item["station_id"], item["local_time_bucket"], item["blocker"], item["action"]))
    if usable == 0 and not queue:
        raise ContractError("p1_repair_appendable_queue_missing")
    return {
        "audit_content_sha256": audit_sha,
        "repair_content_sha256": repair_sha,
        "as_of_utc": str(audit.get("as_of_utc") or ""),
        "audit_formal_eligible_scope_count": int(audit.get("formal_eligible_scope_count") or 0),
        "usable_candidate_pointer_count": usable,
        "p1_rebuild_state": "BLOCKED_NO_APPENDABLE_ROOT" if usable == 0 else "CANDIDATE_POINTERS_PRESENT_NO_AUTOMATIC_WRITE",
        "old_root_rebind_authorized": False,
        "appendable_queue": queue,
        "appendable_queue_count": len(queue),
        "safe_write_state": str(repair.get("safe_write_state") or ""),
        "boundary": {
            "writes_target_database": False,
            "writes_canonical_database": False,
            "writes_p0": False,
            "writes_p0m": False,
            "writes_p1": False,
            "calls_network": False,
            "calls_order_gateway": False,
            "orders_submitted": 0,
        },
    }


def build(
    *, official_inbox: Path, republisher_inbox: Path, as_of_utc: str,
    max_age_seconds: int, p1_audit: Path, p1_repair: Path,
    event_date: str | None = None, official_capture_receipts: Iterable[Path] | None = None,
) -> dict[str, Any]:
    if max_age_seconds < 0:
        raise ContractError("max_age_seconds_invalid")
    as_of_text, as_of = _utc(as_of_utc, field="as_of_utc")
    official, official_sha, official_path = _load_inbox(official_inbox, packet_schema=OFFICIAL_PACKET_SCHEMA, label="official_inbox")
    republisher, republisher_sha, republisher_path = _load_inbox(republisher_inbox, packet_schema=REPUBLISHER_PACKET_SCHEMA, label="republisher_inbox")
    selected_event_date = _event_date(event_date, field="event_date") if event_date else _derive_event_date(official, republisher)
    registry = coordinate_registry_manifest()
    stations = list(registry["station_ids"])
    canonical = set(stations)
    official_by_station = _index_packets(official["packets"], event_date=selected_event_date, packet_schema=OFFICIAL_PACKET_SCHEMA, label="official_inbox", canonical=canonical)
    republisher_by_station = _index_packets(republisher["packets"], event_date=selected_event_date, packet_schema=REPUBLISHER_PACKET_SCHEMA, label="republisher_inbox", canonical=canonical)
    retained_503 = _retained_503_by_station(official_capture_receipts, event_date=selected_event_date, canonical=canonical)
    audit, repair, audit_sha, repair_sha = _load_p1_pair(
        p1_audit, p1_repair, outer_as_of=as_of,
    )

    rows: list[dict[str, Any]] = []
    for station in stations:
        official_packet = official_by_station.get(station)
        republisher_packet = republisher_by_station.get(station)
        official_status = _packet_freshness(official_packet, as_of=as_of, max_age_seconds=max_age_seconds, kind="OFFICIAL")
        republisher_status = _packet_freshness(republisher_packet, as_of=as_of, max_age_seconds=max_age_seconds, kind="FALLBACK")
        receipt_hashes = retained_503.get(station, [])
        official_503 = {
            "state": "OFFICIAL_HTTP_503_RETAINED" if receipt_hashes else "OFFICIAL_CAPTURE_RECEIPT_MISSING_OR_NON503",
            "retained_receipt_content_sha256s": receipt_hashes,
        }
        rows.append({
            "station_id": station,
            "event_date": selected_event_date,
            "official": {
                "packet_present": official_packet is not None,
                "packet_content_sha256": None if official_packet is None else str(official_packet.get("packet_content_sha256") or official_packet.get("snapshot_content_sha256") or ""),
                **official_status,
                "http_503_evidence": official_503,
            },
            "fallback": {
                "packet_present": republisher_packet is not None,
                "packet_content_sha256": None if republisher_packet is None else str(republisher_packet.get("packet_content_sha256") or republisher_packet.get("snapshot_content_sha256") or ""),
                **republisher_status,
                "source_role": "aggregator_republisher",
                "native_arrival_index_eligible": False,
                "p1_eligible": False,
                "formal_p0_eligible": False,
                "formal_live_eligible": False,
            },
            "decision": "FAIL_CLOSED",
            "decision_reason": "CURRENT_SNAPSHOT_GAP_LEDGER_READ_ONLY_NO_PROBABILITY_OR_ORDER_WRITE",
        })
    summary = {
        "station_count": len(rows),
        "canonical_station_count": len(stations),
        "full_45_station_coverage_proven": len(rows) == len(stations) and {row["station_id"] for row in rows} == canonical,
        "official": {
            "packet_present_count": sum(row["official"]["packet_present"] for row in rows),
            "fresh_count": sum(row["official"]["state"] == "FRESH" for row in rows),
            "stale_count": sum(row["official"]["state"] == "STALE" for row in rows),
            "timestamp_missing_count": sum(row["official"]["state"] == "TIMESTAMP_MISSING" for row in rows),
            "missing_count": sum(row["official"]["state"] == "MISSING" for row in rows),
            "retained_http_503_count": sum(row["official"]["http_503_evidence"]["state"] == "OFFICIAL_HTTP_503_RETAINED" for row in rows),
        },
        "fallback": {
            "packet_present_count": sum(row["fallback"]["packet_present"] for row in rows),
            "fresh_count": sum(row["fallback"]["state"] == "FRESH" for row in rows),
            "stale_count": sum(row["fallback"]["state"] == "STALE" for row in rows),
            "timestamp_missing_count": sum(row["fallback"]["state"] == "TIMESTAMP_MISSING" for row in rows),
            "missing_count": sum(row["fallback"]["state"] == "MISSING" for row in rows),
        },
        "both_currently_unavailable_count": sum(not row["official"]["fresh"] and not row["fallback"]["fresh"] for row in rows),
    }
    p1_state = _p1_rebuild_state(audit, repair, audit_sha=audit_sha, repair_sha=repair_sha)
    identity = {
        "schema": SCHEMA,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "as_of_utc": as_of_text,
        "event_date": selected_event_date,
        "max_age_seconds": max_age_seconds,
        "station_registry": registry,
        "inputs": {
            "official_inbox": {"path": str(official_path), "content_sha256": official_sha},
            "republisher_inbox": {"path": str(republisher_path), "content_sha256": republisher_sha},
            "official_capture_receipt_paths": [str(path.expanduser().resolve()) for path in (official_capture_receipts or ())],
        },
        "summary": summary,
        "stations": rows,
        "p1": p1_state,
        "boundary": {
            "runner": "one_shot_read_only",
            "writes_target_database": False,
            "writes_canonical_database": False,
            "writes_p0": False,
            "writes_p0m": False,
            "writes_p1": False,
            "calls_network": False,
            "calls_order_gateway": False,
            "orders_submitted": 0,
            "live_submit_authorized": False,
        },
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--official-inbox", type=Path, required=True)
    parser.add_argument("--republisher-inbox", type=Path, required=True)
    parser.add_argument("--as-of-utc", required=True, help="explicit UTC Z decision boundary")
    parser.add_argument("--event-date", default=None)
    parser.add_argument("--max-age-seconds", type=int, default=3600)
    parser.add_argument("--official-capture-receipt", type=Path, action="append", default=[])
    parser.add_argument("--p1-audit", type=Path, required=True)
    parser.add_argument("--p1-repair", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    ledger = build(
        official_inbox=args.official_inbox,
        republisher_inbox=args.republisher_inbox,
        as_of_utc=args.as_of_utc,
        event_date=args.event_date,
        max_age_seconds=args.max_age_seconds,
        official_capture_receipts=args.official_capture_receipt,
        p1_audit=args.p1_audit,
        p1_repair=args.p1_repair,
    )
    wrote = write_bounded_json_if_changed(args.output.expanduser().resolve(), ledger, limit=MAX_JSON_OUTPUT_BYTES)
    print(_json({
        "schema": SCHEMA,
        "content_sha256": ledger["content_sha256"],
        "summary": ledger["summary"],
        "p1_rebuild_state": ledger["p1"]["p1_rebuild_state"],
        "appendable_queue_count": ledger["p1"]["appendable_queue_count"],
        "output": str(args.output.expanduser().resolve()),
        "wrote": wrote,
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
