#!/usr/bin/env python3
"""Read-only phase gates from physical-X shadow SSOT to production cutover."""

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

from observation_fast_lane_latency_runtime import build_observation_fast_lane_latency_slo_report  # noqa: E402
from scripts.materialize_probability_transaction_fault_injection_report import (  # noqa: E402
    load_verified_latest_report,
)
from scripts.materialize_probability_replay_equivalence_report import (  # noqa: E402
    load_verified_latest_report as load_verified_latest_replay_report,
)
from scripts.observation_fast_lane_shadow_db_slo_audit import _read_packets  # noqa: E402
from scripts.ssot_probability_runtime_acceptance import build_acceptance  # noqa: E402


SCHEMA_VERSION = "ssot_probability_cutover_readiness_v1"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())


def _count(conn: sqlite3.Connection, table: str, target: str) -> int:
    if not _table_exists(conn, table):
        return 0
    return int(conn.execute(f"SELECT COUNT(*) FROM {table} WHERE target_identity=?", (target,)).fetchone()[0] or 0)


def _latest_cycle(conn: sqlite3.Connection, target: str) -> dict[str, Any]:
    if not _table_exists(conn, "resident_p10_prepared_cycles_shadow"):
        return {}
    row = conn.execute(
        """
        SELECT cycle_json FROM resident_p10_prepared_cycles_shadow
        WHERE target_identity=? ORDER BY prepared_at DESC,rowid DESC LIMIT 1
        """,
        (target,),
    ).fetchone()
    if row is None:
        return {}
    try:
        value = json.loads(_text(row[0]))
    except json.JSONDecodeError:
        return {}
    return _mapping(value)


def _accepted_cutover_adr() -> str:
    adr_root = ROOT / "docs" / "adr"
    for path in sorted(adr_root.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        header = text.split("---", 2)[1] if text.startswith("---") and text.count("---") >= 2 else ""
        lowered = (header + "\n" + text[:1200]).lower()
        if (
            "status: accepted" in header.lower()
            and "production cutover" in lowered
            and "shadow-only" not in lowered
            and path.name != "ADR-0001-PRODUCTION-PROBABILITY-MODEL-IDENTITY.md"
        ):
            return str(path)
    return ""


def build_cutover_readiness(
    *,
    shadow_db: Path,
    target_identity: str,
    monitor_status_path: Path,
    resident_status_path: Path,
    observed_at: datetime,
) -> dict[str, Any]:
    event_date = target_identity.rsplit(":", 1)[-1]
    runtime = build_acceptance(
        shadow_db=shadow_db,
        monitor_status_path=monitor_status_path,
        resident_status_path=resident_status_path,
        observed_at=observed_at,
    )
    with sqlite3.connect(f"file:{shadow_db.resolve()}?mode=ro", uri=True) as conn:
        events = _count(conn, "canonical_observation_events", target_identity)
        updates = _count(conn, "strong_observation_updates", target_identity)
        checkpoints = _count(conn, "posterior_checkpoints", target_identity)
        incremental = int(
            conn.execute(
                "SELECT COUNT(*) FROM posterior_checkpoints WHERE target_identity=? AND checkpoint_kind='incremental'",
                (target_identity,),
            ).fetchone()[0]
            or 0
        ) if _table_exists(conn, "posterior_checkpoints") else 0
        cycle = _latest_cycle(conn, target_identity)
    packets = [packet for packet in _read_packets(shadow_db) if _text(packet.get("event_date")) == event_date]
    latency = build_observation_fast_lane_latency_slo_report(packets, require_live_t1_to_t5=False)

    forward_blockers: list[str] = []
    if events == 0:
        forward_blockers.append("cutover_forward_canonical_observation_missing")
    if not (events == updates == incremental):
        forward_blockers.append("cutover_event_update_checkpoint_one_to_one_failed")
    if len(packets) != updates:
        forward_blockers.append("cutover_update_latency_packet_one_to_one_failed")
    if latency.get("ok") is not True:
        forward_blockers.extend(latency.get("blockers") or ["cutover_t1_to_t3_slo_not_proven"])

    resolution = _mapping(_mapping(cycle.get("posterior_context")).get("resolution_contract"))
    bridge_closed = resolution.get("bridge_closed") is True and resolution.get("validated") is True
    market_blockers = [] if bridge_closed else ["cutover_x_to_s_to_b_bridge_open_or_unvalidated"]

    replay_report: dict[str, Any] = {}
    replay_report_path = ""
    replay_report_blockers: list[str] = []
    if events == 0:
        replay_report_blockers.append("incremental_full_replay_equivalence_not_observable")
    else:
        replay_report, replay_report_path, replay_report_blockers = load_verified_latest_replay_report(
            shadow_db=shadow_db,
            target_identity=target_identity,
        )
    replay_blockers = [f"cutover_{item}" for item in replay_report_blockers]
    fault_report, fault_report_path, fault_report_blockers = load_verified_latest_report()
    replay_blockers.extend(f"cutover_{item}" for item in fault_report_blockers)

    cutover_adr = _accepted_cutover_adr()
    authority_blockers = [] if cutover_adr else ["cutover_accepted_production_probability_adr_missing"]
    production_blockers = sorted(set(
        ([] if runtime.get("ok") is True else runtime.get("blockers") or ["cutover_shadow_runtime_not_accepted"])
        + forward_blockers
        + market_blockers
        + replay_blockers
        + authority_blockers
    ))
    return {
        "schema": SCHEMA_VERSION,
        "ok": not production_blockers,
        "state": "eligible_for_separate_cutover_adr_activation" if not production_blockers else "shadow_progressing_production_cutover_blocked",
        "target_identity": target_identity,
        "physical_shadow_runtime": {
            "ok": runtime.get("ok") is True,
            "state": runtime.get("state"),
            "blockers": runtime.get("blockers") or [],
        },
        "forward_probability_ssot": {
            "ok": not forward_blockers,
            "blockers": sorted(set(forward_blockers)),
            "canonical_event_count": events,
            "strong_update_count": updates,
            "post_p0_checkpoint_count": incremental,
            "latency_packet_count": len(packets),
            "latency_slo": latency,
        },
        "market_projection": {
            "ok": not market_blockers,
            "blockers": market_blockers,
            "resolution_contract_id": resolution.get("resolution_contract_id"),
            "bridge_closed": resolution.get("bridge_closed") is True,
            "validated": resolution.get("validated") is True,
        },
        "replay_and_atomicity": {
            "ok": not replay_blockers,
            "blockers": replay_blockers,
            "incremental_full_replay": {
                "ok": not replay_report_blockers,
                "report_sha256": replay_report.get("report_sha256"),
                "report_path": replay_report_path or None,
                "bound_snapshot": replay_report.get("bound_snapshot") or {},
                "blockers": replay_report_blockers,
            },
            "transaction_b_fault_injection": {
                "ok": not fault_report_blockers,
                "report_sha256": fault_report.get("report_sha256"),
                "report_path": fault_report_path or None,
                "executed_at": fault_report.get("executed_at"),
                "bound_file_sha256": fault_report.get("file_sha256") or {},
                "blockers": fault_report_blockers,
            },
        },
        "authority": {
            "ok": not authority_blockers,
            "blockers": authority_blockers,
            "accepted_cutover_adr": cutover_adr,
            "current_production_authority": "ADR-0001:v3.1-only",
            "target_shadow_authority": "ADR-0002:sequential-physical-X-shadow-only",
        },
        "production_cutover_blockers": production_blockers,
        "boundary": {
            "read_only": True,
            "current_v31_production_remains_authoritative_until_all_gates_and_new_adr": True,
            "does_not_promote_or_activate": True,
            "places_orders": False,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-identity", required=True)
    parser.add_argument("--shadow-db", type=Path, default=ROOT / "data" / "shadow" / "p10_runtime.db")
    parser.add_argument("--monitor-status", type=Path, default=ROOT / "data" / "shadow" / "p10_monitor_runtime_status_latest.json")
    parser.add_argument("--resident-status", type=Path, default=ROOT / "data" / "shadow" / "resident_p10_shadow_status_latest.json")
    args = parser.parse_args()
    try:
        result = build_cutover_readiness(
            shadow_db=args.shadow_db.expanduser().resolve(),
            target_identity=args.target_identity,
            monitor_status_path=args.monitor_status.expanduser().resolve(),
            resident_status_path=args.resident_status.expanduser().resolve(),
            observed_at=datetime.now(timezone.utc),
        )
    except (OSError, sqlite3.Error, ValueError) as exc:
        result = {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "production_cutover_blockers": [f"cutover_readiness_failed:{type(exc).__name__}"],
        }
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("ok") is True else 2


if __name__ == "__main__":
    raise SystemExit(main())
