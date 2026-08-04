#!/usr/bin/env python3
"""Audit official-first no-live P0 fallback compilation from retained packets.

This command is intentionally read-only.  It consumes the already retained
official-native and Open-Meteo forward inboxes, adds only explicit negative
authority metadata to legacy republisher packets in memory, and calls the
existing P0 compiler against a query-only SSOT connection.  It never fetches,
persists a packet, writes P0/P0-M/P1, or calls the order gateway.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import content_sha256  # noqa: E402
from bayes_basket_target.native_ensemble_p0 import compile_native_ensemble_p0  # noqa: E402
from bayes_basket_target.resource_bounds import (  # noqa: E402
    MAX_JSON_INPUT_BYTES,
    MAX_JSON_OUTPUT_BYTES,
    load_bounded_json,
    write_bounded_json_if_changed,
)


INBOX_SCHEMA = "native_ensemble_forward_inbox_v1"
OFFICIAL_SCHEMA = "official_native_ensemble_forward_packet_v1"
REPUBLISHER_SCHEMA = "open_meteo_native_ensemble_forward_packet_v1"
MAX_PACKETS = 256
MAX_SCOPES = 45
OBSERVATIONAL_ROLE = "observational_candidate_not_oos"
OBSERVATIONAL_SCOPE = "observational_candidate_not_oos"


def _utc(value: str) -> str:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RuntimeError("decision_at_utc_timezone_required")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _load_inbox(path: Path) -> tuple[dict[str, Any], str]:
    value = load_bounded_json(path.expanduser().resolve(strict=True), limit=MAX_JSON_INPUT_BYTES)
    if not isinstance(value, Mapping) or value.get("schema") != INBOX_SCHEMA:
        raise RuntimeError(f"inbox_schema_invalid:{path}")
    packets = value.get("packets")
    if not isinstance(packets, list) or len(packets) > MAX_PACKETS:
        raise RuntimeError(f"inbox_packets_invalid:{path}")
    identity = {key: item for key, item in value.items() if key != "content_sha256"}
    supplied = str(value.get("content_sha256") or "")
    if supplied != content_sha256(identity):
        raise RuntimeError(f"inbox_content_sha256_mismatch:{path}")
    return dict(value), supplied


def _normalize_republisher(packet: Mapping[str, Any]) -> dict[str, Any]:
    """Make the negative authority contract explicit without changing raw data."""

    result = dict(packet)
    result["evidence_role"] = OBSERVATIONAL_ROLE
    result["oos_event_temporal_scope"] = OBSERVATIONAL_SCOPE
    result["strict_oos_sample_eligible"] = False
    result["can_substitute_for_formal_promotion"] = False
    authority = result.get("source_authority")
    if not isinstance(authority, Mapping):
        raise RuntimeError("republisher_source_authority_missing")
    result["source_authority"] = {
        **dict(authority),
        "source_role": "aggregator_republisher",
        "native_run_init_known": False,
        "native_publication_at_known": False,
        "native_arrival_index_eligible": False,
        "formal_p0_eligible": False,
        "production_no_live_calibration_candidate": True,
        "evidence_role": OBSERVATIONAL_ROLE,
        "oos_event_temporal_scope": OBSERVATIONAL_SCOPE,
        "counts_as_strict_oos": False,
        "historical_oos_contract_bound": False,
        "can_substitute_for_formal_promotion": False,
    }
    result["packet_content_sha256"] = content_sha256({
        key: item for key, item in result.items() if key != "packet_content_sha256"
    })
    return result


def _packet_index(
    packets: list[Mapping[str, Any]],
    *,
    schema: str,
    normalize_republisher: bool = False,
) -> dict[tuple[str, str], Mapping[str, Any]]:
    selected: dict[tuple[str, str], Mapping[str, Any]] = {}
    for raw in packets:
        if str(raw.get("schema") or "") != schema:
            continue
        packet = _normalize_republisher(raw) if normalize_republisher else dict(raw)
        station = str(packet.get("station_id") or "").strip().upper()
        event_date = str(packet.get("event_date") or "").strip()
        if not station or not event_date:
            raise RuntimeError("packet_scope_missing")
        key = (station, event_date)
        prior = selected.get(key)
        if prior is not None:
            # The inbox should contain one packet per station-day.  Retain an
            # identical replay, but never choose between conflicting receipts.
            prior_hash = str(prior.get("packet_content_sha256") or prior.get("snapshot_content_sha256") or "")
            current_hash = str(packet.get("packet_content_sha256") or packet.get("snapshot_content_sha256") or "")
            if prior_hash != current_hash:
                raise RuntimeError(f"packet_scope_content_conflict:{station}:{event_date}")
            continue
        selected[key] = packet
    return selected


def _latest_by_station(
    index: Mapping[tuple[str, str], Mapping[str, Any]],
) -> dict[str, tuple[tuple[str, str], Mapping[str, Any]]]:
    result: dict[str, tuple[tuple[str, str], Mapping[str, Any]]] = {}
    for scope, packet in sorted(index.items()):
        station = scope[0]
        prior = result.get(station)
        if prior is None or scope[1] > prior[0][1]:
            result[station] = (scope, packet)
    return result


def _attempt(
    conn: sqlite3.Connection,
    *,
    scope: tuple[str, str],
    source_class: str,
    packet: Mapping[str, Any] | None,
    decision_at_utc: str,
) -> dict[str, Any]:
    if packet is None:
        return {
            "source_class": source_class,
            "state": "NOT_AVAILABLE",
            "reason": "packet_missing_for_exact_station_day",
        }
    try:
        compiled = compile_native_ensemble_p0(
            conn,
            forward_packet=packet,
            decision_at_utc=decision_at_utc,
        )
    except Exception as exc:
        return {
            "source_class": source_class,
            "state": "BLOCKED",
            "reason": f"{type(exc).__name__}:{exc}",
            "packet_content_sha256": str(
                packet.get("packet_content_sha256")
                or packet.get("snapshot_content_sha256")
                or ""
            ),
        }
    compiled_packet = compiled.get("packet") if isinstance(compiled, Mapping) else None
    if not isinstance(compiled_packet, Mapping):
        return {
            "source_class": source_class,
            "state": "BLOCKED",
            "reason": "compiler_result_packet_missing",
        }
    envelope = compiled_packet.get("native_probability_envelope")
    lineage = envelope.get("lineage") if isinstance(envelope, Mapping) else None
    return {
        "source_class": source_class,
        "state": "CANDIDATE_COMPILED",
        "packet_content_sha256": str(compiled.get("packet_content_sha256") or ""),
        "compiled_content_sha256": content_sha256(dict(compiled)),
        "transport_lineage_class": str((lineage or {}).get("transport_lineage_class") or ""),
        "native_arrival_index_eligible": str((lineage or {}).get("native_arrival_index_eligible") or ""),
        "formal_p0_eligible": False,
        "writes_p0": False,
        "writes_p0m": False,
        "writes_p1": False,
    }


def build(
    *,
    db: Path,
    official: Path,
    republisher: Path,
    decision_at_utc: str,
    event_date: str | None = None,
) -> dict[str, Any]:
    official_raw, official_sha = _load_inbox(official)
    republisher_raw, republisher_sha = _load_inbox(republisher)
    event_date_filter = str(event_date or "").strip()
    if event_date_filter:
        try:
            event_date_filter = datetime.strptime(
                event_date_filter, "%Y-%m-%d"
            ).date().isoformat()
        except ValueError as exc:
            raise RuntimeError("event_date_invalid") from exc

    def in_event_date(packet: Mapping[str, Any]) -> bool:
        return (
            not event_date_filter
            or str(packet.get("event_date") or "").strip()
            == event_date_filter
        )

    official_index = _packet_index(
        [
            item
            for item in official_raw["packets"]
            if isinstance(item, Mapping) and in_event_date(item)
        ],
        schema=OFFICIAL_SCHEMA,
    )
    republisher_index = _packet_index(
        [
            item
            for item in republisher_raw["packets"]
            if isinstance(item, Mapping) and in_event_date(item)
        ],
        schema=REPUBLISHER_SCHEMA,
        normalize_republisher=True,
    )
    official_latest = _latest_by_station(official_index)
    republisher_latest = _latest_by_station(republisher_index)
    stations = sorted(set(official_latest) | set(republisher_latest))
    if len(stations) > MAX_SCOPES:
        raise RuntimeError("fallback_candidate_station_count_exceeded")
    conn = sqlite3.connect(f"file:{db.expanduser().resolve(strict=True)}?mode=ro", uri=True)
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    rows: list[dict[str, Any]] = []
    try:
        for station in stations:
            official_scope, official_packet = official_latest.get(station, (None, None))
            republisher_scope, republisher_packet = republisher_latest.get(station, (None, None))
            # Official precedence is exact station × event-day.  A fallback
            # from a different event day is never eligible.
            scope = official_scope or republisher_scope
            assert scope is not None
            same_day_republisher = (
                republisher_packet
                if republisher_scope == scope
                else None
            )
            official_attempt = _attempt(
                conn,
                scope=scope,
                source_class="official_native",
                packet=official_packet if official_scope == scope else None,
                decision_at_utc=decision_at_utc,
            )
            fallback_attempt = None
            selected = official_attempt
            if official_attempt["state"] != "CANDIDATE_COMPILED":
                fallback_attempt = _attempt(
                    conn,
                    scope=scope,
                    source_class="aggregator_republisher",
                    packet=same_day_republisher,
                    decision_at_utc=decision_at_utc,
                )
                selected = fallback_attempt
            rows.append({
                "station_id": scope[0],
                "event_date": scope[1],
                "official_packet_present": official_scope == scope,
                "republisher_packet_present_same_station_day": same_day_republisher is not None,
                "official_attempt": official_attempt,
                "republisher_attempt": fallback_attempt,
                "selected": selected,
                "official_preferred": True,
                "republisher_native_arrival_index_eligible": False,
                "republisher_formal_p0_eligible": False,
            })
    finally:
        conn.close()
    compiled = [row for row in rows if row["selected"]["state"] == "CANDIDATE_COMPILED"]
    identity = {
        "schema": "bayes_basket_target_republisher_fallback_p0_candidate_audit_v1",
        "as_of_utc": _utc(decision_at_utc),
        "event_date_filter": event_date_filter or None,
        "official_inbox": {"path": str(official.resolve()), "content_sha256": official_sha},
        "republisher_inbox": {"path": str(republisher.resolve()), "content_sha256": republisher_sha},
        "station_count": len(rows),
        "candidate_count": len(compiled),
        "official_compiled_count": sum(
            row["selected"]["state"] == "CANDIDATE_COMPILED"
            and row["selected"]["source_class"] == "official_native"
            for row in rows
        ),
        "republisher_fallback_compiled_count": sum(
            row["selected"]["state"] == "CANDIDATE_COMPILED"
            and row["selected"]["source_class"] == "aggregator_republisher"
            for row in rows
        ),
        "rows": rows,
        "policy": {
            "official_native_first": True,
            "fallback_requires_same_station_event_day": True,
            "republisher_is_not_native_arrival": True,
            "republisher_is_not_p1": True,
            "republisher_is_not_formal_live_authority": True,
            "future_data_is_not_oos": True,
            "metadata_normalization_does_not_change_raw_receipt": True,
        },
        "boundary": {
            "read_only_database": True,
            "network_fetches": 0,
            "writes_production_p0": False,
            "writes_production_p0m": False,
            "writes_production_p1": False,
            "calls_order_gateway": False,
            "orders_submitted": 0,
        },
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--official-inbox", type=Path, required=True)
    parser.add_argument("--republisher-inbox", type=Path, required=True)
    parser.add_argument("--decision-at-utc", required=True)
    parser.add_argument(
        "--event-date",
        help="Restrict the audit to one exact station×event-date scope.",
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = build(
        db=args.db,
        official=args.official_inbox,
        republisher=args.republisher_inbox,
        decision_at_utc=args.decision_at_utc,
        event_date=args.event_date,
    )
    wrote = write_bounded_json_if_changed(
        args.output.expanduser().resolve(), result, limit=MAX_JSON_OUTPUT_BYTES
    )
    print(json.dumps({
        "schema": result["schema"],
        "state": "CANDIDATE_COMPILED" if result["candidate_count"] else "BLOCKED_NO_FALLBACK_P0",
        "station_count": result["station_count"],
        "candidate_count": result["candidate_count"],
        "official_compiled_count": result["official_compiled_count"],
        "republisher_fallback_compiled_count": result["republisher_fallback_compiled_count"],
        "output": str(args.output.expanduser().resolve()),
        "wrote": wrote,
        "network_fetches": 0,
        "orders_submitted": 0,
        "content_sha256": result["content_sha256"],
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
