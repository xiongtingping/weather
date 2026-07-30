#!/usr/bin/env python3
"""Materialize a bounded, formal-only historical canonical observation packet.

The input packets are official historical rebuild receipts.  This command is
target-only: it never opens a production database, writes a target database,
fetches a source, or calls an order gateway.

AMSC hourly reports are the formal primary for AMSC-owned stations.  AMSC
half-hour reports and AWC hourly reports for AMSC-owned stations remain
reference-only.  Reference bodies are not copied into the formal packet;
their packet hashes and bounded counts are retained for audit lineage.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import ContractError, content_sha256  # noqa: E402
from bayes_basket_target.et_history import build_et_truth_rows  # noqa: E402
from bayes_basket_target.resource_bounds import (  # noqa: E402
    MAX_INPUT_FILES_PER_BATCH,
    MAX_JSON_INPUT_BYTES,
    MAX_JSON_OUTPUT_BYTES,
    load_bounded_json,
    write_bounded_json,
)
from bayes_basket_target.source_policy import (  # noqa: E402
    normalize_station_lane_map,
    reference_only_reason,
    source_policy_reason,
)
from bayes_basket_target.settlement_time import historical_registry_scope_settlement_timezone  # noqa: E402


SCHEMA = "bayes_basket_target_canonical_observation_evidence_v1"
MAX_EVENT_DATES = 32
MAX_BLOCKER_SAMPLES = 64
DEFAULT_LANE_MAP = ROOT / "config/trading/bayes_basket_target_station_lane_map_v1.json"
DEFAULT_TIMEZONE_REGISTRY = ROOT / "data/bayes_basket_target_market_registry_snapshot.json"
REFERENCE_SCHEMA = "bayes_basket_target_truth_reference_evidence_v1"


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _utc(name: str, value: Any) -> datetime:
    raw = str(value or "").strip()
    if not raw:
        raise ContractError(f"{name}_missing")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"{name}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"{name}_timezone_required")
    return parsed.astimezone(timezone.utc)


def _utc_text(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _sha(name: str, value: Any) -> str:
    result = str(value or "").strip().lower()
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
        raise ContractError(f"{name}_invalid_sha256")
    return result


def _event_date(valid_at: datetime, *, target_timezone: str) -> str:
    return valid_at.astimezone(ZoneInfo(target_timezone)).date().isoformat()


def _slot_key(raw: Mapping[str, Any]) -> tuple[str, str, str, str] | None:
    try:
        return (
            str(raw.get("station_id") or "").strip().upper(),
            str(raw.get("source_lane") or "").strip().lower(),
            str(raw.get("report_type") or "").strip().upper(),
            _utc_text(_utc("valid_at_utc", raw.get("valid_at_utc", raw.get("valid_at")))),
        )
    except ContractError:
        return None


def _lane_map(path: Path) -> dict[str, str]:
    payload = load_bounded_json(path.expanduser().resolve())
    stations = payload.get("stations") if isinstance(payload, Mapping) else payload
    if not isinstance(stations, Mapping):
        raise ValueError("station_lane_map_mapping_required")
    return normalize_station_lane_map(stations)


def _load_packet(
    path: Path,
    wanted_dates: set[str] | None,
    *,
    target_timezone: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    resolved = path.expanduser().resolve()
    payload = load_bounded_json(resolved, limit=MAX_JSON_INPUT_BYTES)
    if not isinstance(payload, Mapping) or payload.get("schema") != SCHEMA:
        raise ValueError(f"observation_schema_invalid:{resolved.name}")
    rows = payload.get("observations")
    if not isinstance(rows, list):
        raise ValueError(f"observation_rows_missing:{resolved.name}")
    retained: list[dict[str, Any]] = []
    discovered_dates: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        raw = dict(row)
        try:
            local_date = _event_date(
                _utc("valid_at_utc", raw.get("valid_at_utc", raw.get("valid_at"))),
                target_timezone=target_timezone,
            )
        except ContractError:
            # Validation is repeated by the materializer so malformed rows are
            # reported as bounded blockers rather than silently becoming data.
            retained.append(raw)
            continue
        discovered_dates.add(local_date)
        if wanted_dates is None or local_date in wanted_dates:
            retained.append(raw)
    return retained, {
        "path": str(resolved),
        "sha256": _file_sha256(resolved),
        "input_observation_count": len(rows),
        "retained_observation_count": len(retained),
        "discovered_event_dates": sorted(discovered_dates),
    }


def _load_reference_packet(path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    resolved = path.expanduser().resolve()
    payload = load_bounded_json(resolved, limit=MAX_JSON_INPUT_BYTES)
    if not isinstance(payload, Mapping) or payload.get("schema") != REFERENCE_SCHEMA:
        raise ValueError(f"reference_schema_invalid:{resolved.name}")
    rows = payload.get("reference_observations")
    if not isinstance(rows, list):
        raise ValueError(f"reference_rows_missing:{resolved.name}")
    return [dict(row) for row in rows if isinstance(row, Mapping)], {
        "path": str(resolved),
        "sha256": _file_sha256(resolved),
        "input_reference_observation_count": len(rows),
    }


def _minimal_row(raw: Mapping[str, Any], *, valid_at: datetime, lane: str, station: str, report_type: str, raw_sha: str, first_seen: datetime, committed: datetime, target_timezone: str, qc_status: str = "ACCEPTED", quarantine_reasons: Sequence[str] = ()) -> dict[str, Any]:
    """Keep only fields required by the settlement truth contract and lineage."""

    identity = {
        "schema": "bayes_basket_target_historical_observation_v1",
        "station_id": station,
        "source_lane": lane,
        "report_type": report_type,
        "valid_at_utc": _utc_text(valid_at),
        "raw_sha256": raw_sha,
    }
    result: dict[str, Any] = {
        # The historical packet is also a canonical-fact ingress packet.  Keep
        # the identity fields here instead of relying on a later adapter to
        # synthesize them; otherwise a packet can look EXPORTED while no fact
        # can be appended to the target SSOT.
        "observation_id": "histobs_" + content_sha256(identity)[:32],
        "station_id": station,
        "source_lane": lane,
        "report_type": report_type,
        "valid_at_utc": _utc_text(valid_at),
        "event_date_local": _event_date(
            valid_at,
            target_timezone=target_timezone,
        ),
        "reported_temperature_c": raw.get("reported_temperature_c", raw.get("temperature_c")),
        "raw_sha256": raw_sha,
        "source_poll_id": str(raw.get("source_poll_id") or "").strip(),
        "first_seen_at_utc": _utc_text(first_seen),
        "canonical_committed_at_utc": _utc_text(committed),
        "qc_status": qc_status,
        "evidence_role": "FORMAL" if qc_status == "ACCEPTED" else "QUARANTINED",
    }
    if qc_status != "ACCEPTED":
        result["qc_status"] = qc_status
    if raw.get("correction_suspected") in {1, True, "1"}:
        result["correction_suspected"] = True
    if quarantine_reasons:
        result["quarantine_reasons"] = sorted(set(str(item) for item in quarantine_reasons))
    return result


def _record_blocker(blockers: list[str], value: str) -> None:
    if len(blockers) < MAX_BLOCKER_SAMPLES:
        blockers.append(value)


def _registry_timezone(
    path: Path,
    *,
    station_ids: Sequence[str],
    event_dates: Sequence[str],
) -> str:
    payload = load_bounded_json(path.expanduser().resolve(), limit=MAX_JSON_INPUT_BYTES)
    rows = payload.get("rows") if isinstance(payload, Mapping) else None
    if not isinstance(rows, list):
        raise ValueError("historical_merge_timezone_registry_rows_missing")
    return historical_registry_scope_settlement_timezone(
        (row for row in rows if isinstance(row, Mapping)),
        station_ids=station_ids,
        event_dates=event_dates,
    )


def merge(
    *,
    base: Path,
    additional: list[Path],
    event_dates: list[str] | None,
    output: Path,
    station_lane_map: Mapping[str, Any] | None = None,
    station_ids: set[str] | None = None,
    reference_paths: list[Path] | None = None,
    as_of_utc: str = "",
    target_timezone: str,
) -> dict[str, Any]:
    """Merge canonical packets and emit only formal truth candidates."""

    paths = [base, *additional]
    if not paths or len(paths) > MAX_INPUT_FILES_PER_BATCH:
        raise ValueError("input_file_count_invalid")
    decision = _utc("as_of_utc", as_of_utc or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))
    lane_map = normalize_station_lane_map(station_lane_map or _lane_map(DEFAULT_LANE_MAP))
    station_scope = {
        str(item).strip().upper()
        for item in (station_ids or set())
        if str(item).strip()
    } or None
    if station_scope is not None:
        unknown = sorted(station_scope - set(lane_map))
        if unknown:
            raise ValueError("station_scope_not_in_target_registry:" + ",".join(unknown))

    explicit_dates = {str(item).strip() for item in (event_dates or []) if str(item).strip()}
    if len(explicit_dates) > MAX_EVENT_DATES:
        raise ValueError("event_dates_invalid")
    wanted_dates = explicit_dates or None
    packets: list[list[dict[str, Any]]] = []
    inputs: list[dict[str, Any]] = []
    for path in paths:
        rows, receipt = _load_packet(
            path,
            wanted_dates,
            target_timezone=target_timezone,
        )
        packets.append(rows)
        inputs.append(receipt)

    # Historical packets are the authoritative reconstruction for any exact
    # source/station/report/time slot they contain.  Do not merge a later
    # live-export copy at that slot: the two raw bodies can differ because of
    # re-ingest/revision and would turn the whole historical backfill into a
    # false revision storm.  Base rows with genuinely missing historical slots
    # are still retained as bounded gap-fill candidates.
    historical_slots = {
        slot
        for packet in packets[1:]
        for row in packet
        for slot in [_slot_key(row)]
        if slot is not None
    }
    if historical_slots:
        packets[0] = [
            row for row in packets[0]
            if _slot_key(row) not in historical_slots
        ]

    blockers: list[str] = []
    quarantine_notices: list[str] = []
    reference_counts: Counter[str] = Counter()
    reference_by_lane: Counter[str] = Counter()
    reference_raw_sha_count: Counter[str] = Counter()
    reference_identity_set: set[tuple[str, str, str, str, str]] = set()
    reference_inputs: list[dict[str, Any]] = []
    accepted_by_identity: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
    quarantined_by_identity: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
    slot_to_identities: dict[tuple[str, str, str, str], set[tuple[str, str, str, str, str]]] = defaultdict(set)
    event_date_set: set[str] = set()

    # Later historical packet arguments have priority over the base export for
    # exact duplicate identities.  Conflicting identities at one slot are
    # quarantined instead of choosing whichever packet happened to be read
    # first.
    for packet_index, rows in enumerate(packets):
        for row_index, raw in enumerate(rows):
            prefix = f"packet={packet_index}:row={row_index}"
            try:
                station = str(raw.get("station_id") or "").strip().upper()
                lane = str(raw.get("source_lane") or "").strip().lower()
                report_type = str(raw.get("report_type") or "").strip().upper()
                if station_scope is not None and station not in station_scope:
                    continue
                if station not in lane_map:
                    raise ContractError("station_not_in_target_registry")
                if lane not in {"amsc", "awc"}:
                    raise ContractError("source_lane_invalid")
                if report_type not in {"METAR", "SPECI"}:
                    raise ContractError("report_type_invalid")
                expected_lane = lane_map[station]
                valid_at = _utc("valid_at_utc", raw.get("valid_at_utc", raw.get("valid_at")))
                local_date = _event_date(
                    valid_at,
                    target_timezone=target_timezone,
                )
                if wanted_dates is not None and local_date not in wanted_dates:
                    continue
                event_date_set.add(local_date)
                reference_reason = reference_only_reason(
                    source_lane=lane,
                    report_type=report_type,
                    valid_at=valid_at,
                    expected_lane=expected_lane,
                )
                raw_sha = _sha("raw_sha256", raw.get("raw_sha256"))
                if reference_reason:
                    reference_identity = (station, lane, report_type, _utc_text(valid_at), raw_sha)
                    if reference_identity not in reference_identity_set:
                        reference_identity_set.add(reference_identity)
                        reference_counts[reference_reason] += 1
                        reference_by_lane[lane] += 1
                        reference_raw_sha_count[raw_sha] += 1
                    continue
                policy_reason = source_policy_reason(
                    source_lane=lane,
                    report_type=report_type,
                    valid_at=valid_at,
                    expected_lane=expected_lane,
                )
                if policy_reason:
                    raise ContractError(policy_reason)
                if lane != expected_lane:
                    raise ContractError("source_lane_cross_fallback_forbidden")
                first_seen = _utc("first_seen_at_utc", raw.get("first_seen_at_utc", raw.get("first_seen_at")))
                committed = _utc("canonical_committed_at_utc", raw.get("canonical_committed_at_utc", raw.get("canonical_committed_at")))
                if valid_at > first_seen or first_seen > committed:
                    raise ContractError("timestamp_order_invalid")
                if committed > decision:
                    raise ContractError("committed_after_asof")
                source_poll_id = str(raw.get("source_poll_id") or "").strip()
                if not source_poll_id:
                    raise ContractError("source_poll_id_missing")
                if raw.get("reported_temperature_c", raw.get("temperature_c")) is None:
                    raise ContractError("reported_temperature_missing")
                correction = raw.get("correction_suspected") in {1, True, "1"}
                qc_status = str(raw.get("qc_status") or "ACCEPTED").strip().upper()
                identity = (station, lane, report_type, _utc_text(valid_at), raw_sha)
                slot = (station, lane, report_type, _utc_text(valid_at))
                item = _minimal_row(
                    raw,
                    valid_at=valid_at,
                    lane=lane,
                    station=station,
                    report_type=report_type,
                    raw_sha=raw_sha,
                    first_seen=first_seen,
                    committed=committed,
                    target_timezone=target_timezone,
                    qc_status="QUARANTINED" if correction or qc_status != "ACCEPTED" else "ACCEPTED",
                    quarantine_reasons=("correction_suspected",) if correction else (("input_qc_not_accepted",) if qc_status != "ACCEPTED" else ()),
                )
                slot_to_identities[slot].add(identity)
                target = quarantined_by_identity if item.get("qc_status", "ACCEPTED") == "QUARANTINED" else accepted_by_identity
                # Historical packets win only for the same exact raw identity;
                # conflicting raw identities are handled below as revisions.
                if identity not in accepted_by_identity and identity not in quarantined_by_identity:
                    target[identity] = item
                elif item.get("qc_status", "ACCEPTED") == "QUARANTINED":
                    quarantined_by_identity[identity] = item
            except (ContractError, TypeError, ValueError) as exc:
                _record_blocker(blockers, f"{prefix}:{exc}")

    for reference_path in reference_paths or []:
        rows, receipt = _load_reference_packet(reference_path)
        retained = 0
        for row in rows:
            try:
                station = str(row.get("station_id") or "").strip().upper()
                lane = str(row.get("source_lane") or "").strip().lower()
                report_type = str(row.get("report_type") or "").strip().upper()
                valid_at = _utc("valid_at_utc", row.get("valid_at_utc", row.get("valid_at")))
                if station_scope is not None and station not in station_scope:
                    continue
                if wanted_dates is not None and _event_date(
                    valid_at,
                    target_timezone=target_timezone,
                ) not in wanted_dates:
                    continue
                raw_sha = _sha("raw_sha256", row.get("raw_sha256"))
                expected_lane = lane_map.get(station)
                if expected_lane is None or lane not in {"amsc", "awc"}:
                    raise ContractError("reference_station_or_lane_invalid")
                reason = reference_only_reason(
                    source_lane=lane,
                    report_type=report_type,
                    valid_at=valid_at,
                    expected_lane=expected_lane,
                )
                if not reason:
                    raise ContractError("reference_policy_reason_missing")
                identity = (station, lane, report_type, _utc_text(valid_at), raw_sha)
                if identity in reference_identity_set:
                    continue
                reference_identity_set.add(identity)
                reference_counts[reason] += 1
                reference_by_lane[lane] += 1
                reference_raw_sha_count[raw_sha] += 1
                retained += 1
            except (ContractError, TypeError, ValueError):
                _record_blocker(blockers, "invalid_reference_packet_row")
        receipt["retained_unique_reference_observation_count"] = retained
        reference_inputs.append(receipt)

    revised_slots = {
        slot for slot, identities in slot_to_identities.items()
        if len({identity[-1] for identity in identities}) > 1
    }
    for slot in sorted(revised_slots):
        _record_blocker(quarantine_notices, "same_slot_revision:" + "|".join(slot))
        for identity in slot_to_identities[slot]:
            if identity in accepted_by_identity:
                item = dict(accepted_by_identity.pop(identity))
                item["qc_status"] = "QUARANTINED"
                item["quarantine_reasons"] = ["same_slot_revision"]
                quarantined_by_identity[identity] = item

    formal_observations = [accepted_by_identity[key] for key in sorted(accepted_by_identity)]
    quarantined_observations = [quarantined_by_identity[key] for key in sorted(quarantined_by_identity)]
    truth_rows, truth_blockers = build_et_truth_rows(
        formal_observations,
        station_lane_map=lane_map,
        as_of_utc=_utc_text(decision),
        target_timezone=target_timezone,
        canonical_ingress=True,
    )
    for item in truth_blockers:
        _record_blocker(blockers, "truth:" + json.dumps(item, ensure_ascii=False, sort_keys=True))

    source_counts = Counter(item["source_lane"] for item in formal_observations)
    formal_hourly_amsc = sum(
        1 for item in formal_observations
        if item["source_lane"] == "amsc" and item["valid_at_utc"][14:16] == "00"
    )
    source_policy = {
        "amsc_hourly_metar_for_amsc_station": "FORMAL_PRIMARY",
        "amsc_half_hour_metar": "REFERENCE_ONLY",
        "awc_hourly_for_amsc_station": "REFERENCE_ONLY",
        "awc_hourly_precedence_over_amsc_hourly": False,
        "awc_hourly_for_awc_station": "FORMAL_PRIMARY",
        "reference_enters_formal_truth": False,
        "reference_enters_calibration": False,
    }
    checks = {
        "formal_observations_nonempty": bool(formal_observations),
        "formal_truth_nonempty": bool(truth_rows),
        # Canonical fact ingress uses this explicit contract bit as its
        # write gate.  The row loop above has already rejected any malformed
        # timestamp; recompute it over the emitted formal packet so the
        # exported evidence carries the same proof consumed downstream.
        "strict_timestamp_ordering": all(
            _utc("valid_at_utc", item["valid_at_utc"])
            <= _utc("first_seen_at_utc", item["first_seen_at_utc"])
            <= _utc("canonical_committed_at_utc", item["canonical_committed_at_utc"])
            for item in formal_observations
        ),
        "formal_source_lane_matches_registry": not any(
            item["source_lane"] != lane_map.get(item["station_id"]) for item in formal_observations
        ),
        "amsc_hourly_primary_present_in_formal_lane": formal_hourly_amsc > 0 or not any(
            lane_map.get(item["station_id"]) == "amsc" for item in formal_observations
        ),
        "amsc_half_hour_excluded_from_formal": reference_counts["amsc_half_hour_reference_only"] >= 0,
        "awc_hourly_for_amsc_excluded_from_formal": reference_counts["awc_hourly_reference_only_for_amsc_station"] >= 0,
        "same_slot_revisions_quarantined": not revised_slots or all(
            item.get("qc_status") == "QUARANTINED" and "same_slot_revision" in item.get("quarantine_reasons", [])
            for item in quarantined_observations
            if "same_slot_revision" in item.get("quarantine_reasons", [])
        ),
        "writes_production_db": False,
        "writes_target_db": False,
        "calls_order_gateway": False,
        "places_orders": False,
    }
    included_station_ids = sorted(station_scope or set(lane_map))
    included_source_lanes = {
        lane_map[station_id]
        for station_id in included_station_ids
    }
    identity = {
        "schema": SCHEMA,
        "target_timezone": target_timezone,
        "historical_only": True,
        "canonical_ingress": True,
        "scope": {
            "mode": "allowlist" if station_scope is not None else "full_lane_map",
            "included_station_ids": included_station_ids,
            "excluded_station_ids": sorted(set(lane_map) - (station_scope or set(lane_map))),
            "excluded_source_lanes": sorted(set(lane_map.values()) - included_source_lanes),
        },
        "event_dates": sorted(event_date_set),
        "source_policy": source_policy,
        "source_counts": dict(sorted(source_counts.items())),
        "reference_observation_count": sum(reference_counts.values()),
        "reference_reason_counts": dict(sorted(reference_counts.items())),
        "reference_lane_counts": dict(sorted(reference_by_lane.items())),
        "reference_lineage": {
            "raw_sha256_unique_count": len(reference_raw_sha_count),
            "raw_sha256_duplicate_count": sum(1 for count in reference_raw_sha_count.values() if count > 1),
            "bodies_copied_into_formal_packet": False,
            "reference_inputs": reference_inputs,
        },
        "observations": formal_observations,
        "truth_rows": truth_rows,
        "quarantined_observation_count": len(quarantined_observations),
        "quarantine_reason_counts": dict(sorted(Counter(
            reason
            for item in quarantined_observations
            for reason in item.get("quarantine_reasons", [])
        ).items())),
        "quarantine_notices": sorted(set(quarantine_notices)),
        "merge_inputs": inputs,
        "checks": checks,
        "blockers": sorted(set(blockers)),
        "boundary": {
            "target_only": True,
            "historical_only": True,
            "reads_shared_live_database": False,
            "writes_shared_live_database": False,
            "writes_production_db": False,
            "writes_target_db": False,
            "calls_order_gateway": False,
            "places_orders": False,
        },
    }
    result = {
        **identity,
        "state": "EXPORTED" if not blockers and formal_observations else "BLOCKED_FAIL_CLOSED",
        "observation_count": len(formal_observations),
        "accepted_truth_count": len(truth_rows),
        "quarantined_truth_count": len(quarantined_observations),
        "as_of_utc": _utc_text(decision),
    }
    result["evidence_sha256"] = content_sha256(result)
    result["receipt_sha256"] = content_sha256({key: value for key, value in result.items() if key != "receipt_sha256"})
    write_bounded_json(output.expanduser().resolve(), result, limit=MAX_JSON_OUTPUT_BYTES)
    return {
        "schema": SCHEMA,
        "state": result["state"],
        "observation_count": result["observation_count"],
        "accepted_truth_count": result["accepted_truth_count"],
        "quarantined_observation_count": result["quarantined_observation_count"],
        "reference_observation_count": result["reference_observation_count"],
        "event_dates": result["event_dates"],
        "blocker_count": len(result["blockers"]),
        "output": str(output.expanduser().resolve()),
        "receipt_sha256": result["receipt_sha256"],
        "boundary": result["boundary"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--additional", type=Path, action="append", default=[])
    parser.add_argument("--event-date", action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--station-lane-map", type=Path, default=DEFAULT_LANE_MAP)
    parser.add_argument("--timezone-registry", type=Path, default=DEFAULT_TIMEZONE_REGISTRY)
    parser.add_argument(
        "--station-id",
        action="append",
        help="Restrict the materialized historical packet to an explicit station allowlist; repeatable.",
    )
    parser.add_argument("--reference-evidence", type=Path, action="append", default=[])
    parser.add_argument("--as-of-utc", default="")
    args = parser.parse_args()
    station_lane_map = _lane_map(args.station_lane_map)
    station_scope = {
        str(item).strip().upper()
        for item in (args.station_id or [])
        if str(item).strip()
    } or set(station_lane_map)
    if not args.event_date:
        raise ValueError("event_dates_required_for_settlement_timezone_resolution")
    result = merge(
        base=args.base,
        additional=list(args.additional),
        event_dates=list(args.event_date or []),
        output=args.output,
        station_lane_map=station_lane_map,
        station_ids=station_scope,
        reference_paths=list(args.reference_evidence or []),
        as_of_utc=args.as_of_utc,
        target_timezone=_registry_timezone(
            args.timezone_registry,
            station_ids=sorted(station_scope),
            event_dates=args.event_date,
        ),
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["state"] == "EXPORTED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
