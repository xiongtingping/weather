#!/usr/bin/env python3
"""Build a read-only 45-station republisher fallback closeout artifact.

The compiler audit and the historical bootstrap manifest are immutable input
evidence.  This join only classifies each exact station×event-day scope by its
local maturity (d0/d1), records the explicit republisher lineage, and checks
that the newly appended exact-mask activations have matching OOS evaluations.
It never opens the target database, fetches data, writes production P0/P1, or
calls the order gateway.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from typing import Any, Mapping
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import content_sha256  # noqa: E402
from bayes_basket_target.resource_bounds import (  # noqa: E402
    MAX_JSON_INPUT_BYTES,
    MAX_JSON_OUTPUT_BYTES,
    load_bounded_json,
    write_bounded_json_if_changed,
)


SCHEMA = "bayes_basket_target_republisher_fallback_closeout_v1"
INBOX_SCHEMA = "native_ensemble_forward_inbox_v1"
BOOTSTRAP_SCHEMA = "native_ensemble_forward_oos_manifest_v1"
AUDIT_SCHEMA = "bayes_basket_target_republisher_fallback_p0_candidate_audit_v1"
LINKAGE_SCHEMA = "native_ensemble_activation_oos_linkage_audit_v1"
PAIR_MASK = ["ECMWF_AIFS_ENS_GLOBAL", "NOAA_GEFS_05"]
HISTORICAL_SCOPE = "CLOSED_HISTORICAL_AWC_TRUTH"
BOOTSTRAP_PROFILE = "PRODUCTION_NO_LIVE_BOOTSTRAP_2_TRAIN_1_HISTORICAL_HOLDOUT"


def _load(path: Path) -> dict[str, Any]:
    value = load_bounded_json(path.expanduser().resolve(strict=True), limit=MAX_JSON_INPUT_BYTES)
    if not isinstance(value, Mapping):
        raise RuntimeError(f"json_object_required:{path}")
    return dict(value)


def _verify_hash(value: Mapping[str, Any], *, label: str) -> str:
    supplied = str(value.get("content_sha256") or "")
    identity = {key: item for key, item in value.items() if key != "content_sha256"}
    expected = content_sha256(identity)
    if supplied != expected:
        raise RuntimeError(f"{label}_content_sha256_mismatch")
    return supplied


def _maturity(packet: Mapping[str, Any]) -> str:
    event_date = str(packet.get("event_date") or "")
    captured = str(packet.get("aggregator_snapshot_at_utc") or "")
    timezone_name = str(packet.get("settlement_timezone") or "")
    try:
        event = datetime.fromisoformat(event_date).date()
        snapshot = datetime.fromisoformat(captured.replace("Z", "+00:00"))
        if snapshot.tzinfo is None:
            raise ValueError("snapshot_timezone_missing")
        local_date = snapshot.astimezone(ZoneInfo(timezone_name)).date()
    except (TypeError, ValueError, KeyError) as exc:
        raise RuntimeError("station_maturity_timestamp_invalid") from exc
    delta = (event - local_date).days
    if delta < 0:
        return f"d_minus_{abs(delta)}"
    return f"d{delta}"


def build(
    *,
    audit: Mapping[str, Any],
    bootstrap: Mapping[str, Any],
    inbox: Mapping[str, Any],
    linkage: Mapping[str, Any],
    audit_path: Path,
    bootstrap_path: Path,
    inbox_path: Path,
    linkage_path: Path,
) -> dict[str, Any]:
    if audit.get("schema") != AUDIT_SCHEMA:
        raise RuntimeError("fallback_audit_schema_invalid")
    if bootstrap.get("schema") != BOOTSTRAP_SCHEMA:
        raise RuntimeError("bootstrap_manifest_schema_invalid")
    if inbox.get("schema") != INBOX_SCHEMA:
        raise RuntimeError("forward_inbox_schema_invalid")
    if linkage.get("schema") != LINKAGE_SCHEMA:
        raise RuntimeError("linkage_audit_schema_invalid")
    audit_sha = _verify_hash(audit, label="fallback_audit")
    bootstrap_sha = _verify_hash(bootstrap, label="bootstrap_manifest")
    inbox_sha = _verify_hash(inbox, label="forward_inbox")
    linkage_sha = _verify_hash(linkage, label="linkage_audit")
    if int(audit.get("station_count") or 0) != 45:
        raise RuntimeError("fallback_audit_station_count_invalid")
    if bootstrap.get("system_mask") != PAIR_MASK:
        raise RuntimeError("bootstrap_manifest_system_mask_invalid")
    if int(bootstrap.get("min_train_days") or 0) != 2 or int(bootstrap.get("min_holdout_days") or 0) != 1:
        raise RuntimeError("bootstrap_manifest_window_invalid")

    packet_by_scope: dict[tuple[str, str], Mapping[str, Any]] = {}
    for raw in inbox.get("packets") or []:
        if not isinstance(raw, Mapping):
            continue
        station = str(raw.get("station_id") or "").strip().upper()
        event_date = str(raw.get("event_date") or "").strip()
        if station and event_date:
            packet_by_scope[(station, event_date)] = raw

    candidate_by_station: dict[str, Mapping[str, Any]] = {}
    for report in bootstrap.get("reports") or []:
        if not isinstance(report, Mapping):
            continue
        for candidate in report.get("cross_candidates") or []:
            if isinstance(candidate, Mapping):
                candidate_by_station[str(candidate.get("station_id") or "").upper()] = candidate

    linkage_pair = json.dumps(PAIR_MASK, ensure_ascii=False, separators=(",", ":"))
    pair_summary = (linkage.get("system_mask_summary") or {}).get(linkage_pair)
    if not isinstance(pair_summary, Mapping):
        raise RuntimeError("bootstrap_activation_linkage_mask_missing")
    pair_station_ids = sorted(str(item) for item in pair_summary.get("station_ids") or [])
    pass_station_ids = sorted(
        station for station, candidate in candidate_by_station.items()
        if candidate.get("state") == "PRODUCTION_NO_LIVE_OOS_PASS"
    )
    if (
        int(pair_summary.get("activation_count") or 0) != len(pass_station_ids)
        or int(pair_summary.get("consumable_exact_oos_pass_count") or 0) != len(pass_station_ids)
        or pair_station_ids != pass_station_ids
    ):
        raise RuntimeError("bootstrap_activation_linkage_station_set_mismatch")

    rows: list[dict[str, Any]] = []
    for raw in audit.get("rows") or []:
        if not isinstance(raw, Mapping):
            continue
        station = str(raw.get("station_id") or "").upper()
        event_date = str(raw.get("event_date") or "")
        packet = packet_by_scope.get((station, event_date))
        if packet is None:
            raise RuntimeError(f"fallback_inbox_scope_missing:{station}:{event_date}")
        expected_lead = _maturity(packet)
        selected = raw.get("selected") if isinstance(raw.get("selected"), Mapping) else {}
        candidate = candidate_by_station.get(station)
        if candidate is None:
            raise RuntimeError(f"bootstrap_candidate_station_missing:{station}")
        selected_state = str(selected.get("state") or "")
        if selected_state == "CANDIDATE_COMPILED":
            disposition = "COMPILED_REPUBLISHER_P0_CANDIDATE"
            blocker = None
        elif expected_lead == "d1":
            disposition = "BLOCKED"
            blocker = "d1_strict_oos_unavailable"
        elif expected_lead == "d0":
            disposition = "BLOCKED"
            blocker = "d0_bootstrap_oos_not_pass"
        else:
            disposition = "BLOCKED"
            blocker = "unsupported_maturity_scope"
        if disposition.startswith("COMPILED") and expected_lead != "d0":
            raise RuntimeError(f"non_d0_compiled:{station}")
        source_roles = candidate.get("source_role_by_system")
        rows.append({
            "station_id": station,
            "event_date": event_date,
            "expected_lead_bucket": expected_lead,
            "disposition": disposition,
            "blocker": blocker,
            "compiler_selected_state": selected_state,
            "compiler_selected_source_class": selected.get("source_class"),
            "compiler_selected_reason": selected.get("reason"),
            "bootstrap_candidate_state": candidate.get("state"),
            "bootstrap_candidate_content_sha256": content_sha256(dict(candidate)),
            "bootstrap_threshold_profile": candidate.get("threshold_profile"),
            "bootstrap_min_train_days": candidate.get("min_train_days"),
            "bootstrap_min_holdout_days": candidate.get("min_holdout_days"),
            "bootstrap_lead_bucket": candidate.get("lead_bucket"),
            "bootstrap_run_cycle": candidate.get("run_cycle"),
            "bootstrap_transport_provider_ids": dict(candidate.get("system_transport_provider_ids") or {}),
            "bootstrap_source_role_by_system": dict(source_roles or {}),
            "bootstrap_oos_event_temporal_scope": candidate.get("oos_event_temporal_scope"),
            "bootstrap_formal_p0_eligible": candidate.get("formal_p0_eligible"),
            "bootstrap_authority_scope": candidate.get("authority_scope"),
            "republisher_native_arrival_index_eligible": False,
            "republisher_p1_eligible": False,
        })
    if len(rows) != 45 or len({row["station_id"] for row in rows}) != 45:
        raise RuntimeError("fallback_closeout_station_set_invalid")
    rows.sort(key=lambda row: row["station_id"])
    d0 = [row for row in rows if row["expected_lead_bucket"] == "d0"]
    d1 = [row for row in rows if row["expected_lead_bucket"] == "d1"]
    compiled = [row for row in rows if row["disposition"].startswith("COMPILED")]
    blocked = [row for row in rows if row["disposition"] == "BLOCKED"]
    identity = {
        "schema": SCHEMA,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "station_count": len(rows),
        "compiled_station_ids": [row["station_id"] for row in compiled],
        "blocked_station_ids": [row["station_id"] for row in blocked],
        "d0_station_count": len(d0),
        "d0_compiled_count": sum(row in compiled for row in d0),
        "d0_blocked_count": sum(row in blocked for row in d0),
        "d1_station_count": len(d1),
        "d1_compiled_count": sum(row in compiled for row in d1),
        "d1_blocked_count": sum(row in blocked for row in d1),
        "bootstrap_pass_count": len(pass_station_ids),
        "bootstrap_rejected_count": sum(
            candidate.get("state") == "OOS_REJECTED"
            for candidate in candidate_by_station.values()
        ),
        "bootstrap_manifest": {"path": str(bootstrap_path), "content_sha256": bootstrap_sha},
        "fallback_compiler_audit": {"path": str(audit_path), "content_sha256": audit_sha},
        "forward_inbox": {"path": str(inbox_path), "content_sha256": inbox_sha},
        "activation_linkage_audit": {
            "path": str(linkage_path),
            "content_sha256": linkage_sha,
            "pair_mask": PAIR_MASK,
            "activation_count": pair_summary.get("activation_count"),
            "consumable_exact_oos_pass_count": pair_summary.get("consumable_exact_oos_pass_count"),
            "station_ids": pair_station_ids,
        },
        "formal_p0_eligible": False,
        "native_arrival_index_written": False,
        "p1_authority_written": False,
        "network_fetches": 0,
        "orders_submitted": 0,
        "production_database_writes": False,
        "rows": rows,
        "boundary": {
            "database_mode": "read_only_query_only",
            "calls_network": False,
            "calls_order_gateway": False,
            "writes_production_p0": False,
            "writes_production_p0m": False,
            "writes_production_p1": False,
            "native_arrival_index_written": False,
            "p1_authority_written": False,
            "orders_submitted": 0,
        },
        "policy": {
            "official_native_first": True,
            "republisher_is_not_native_arrival": True,
            "republisher_is_not_p1": True,
            "republisher_is_not_formal_live_authority": True,
            "future_data_is_not_oos": True,
            "d1_strict_oos_unavailable_is_fail_closed": True,
        },
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--bootstrap", type=Path, required=True)
    parser.add_argument("--inbox", type=Path, required=True)
    parser.add_argument("--linkage", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    audit_path = args.audit.expanduser().resolve(strict=True)
    bootstrap_path = args.bootstrap.expanduser().resolve(strict=True)
    inbox_path = args.inbox.expanduser().resolve(strict=True)
    linkage_path = args.linkage.expanduser().resolve(strict=True)
    result = build(
        audit=_load(audit_path),
        bootstrap=_load(bootstrap_path),
        inbox=_load(inbox_path),
        linkage=_load(linkage_path),
        audit_path=audit_path,
        bootstrap_path=bootstrap_path,
        inbox_path=inbox_path,
        linkage_path=linkage_path,
    )
    output = args.output.expanduser().resolve()
    wrote = write_bounded_json_if_changed(output, result, limit=MAX_JSON_OUTPUT_BYTES)
    print(json.dumps({
        "schema": result["schema"],
        "station_count": result["station_count"],
        "compiled_count": len(result["compiled_station_ids"]),
        "blocked_count": len(result["blocked_station_ids"]),
        "d0_blocked_count": result["d0_blocked_count"],
        "d1_blocked_count": result["d1_blocked_count"],
        "formal_p0_eligible": result["formal_p0_eligible"],
        "orders_submitted": result["orders_submitted"],
        "output": str(output),
        "wrote": wrote,
        "content_sha256": result["content_sha256"],
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
