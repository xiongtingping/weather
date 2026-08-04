#!/usr/bin/env python3
"""Audit the resident P1-to-P0 replay binding without mutating the SSOT.

P1 remains the final stage authority even after it expires.  A stale P1
rooted at an older P0 must therefore trigger replay rather than permit a
silent fallback to a newer P0.  This command mirrors that narrow root check
for every station in the canonical 45-station registry and separately records
whether the formal METAR trajectory artifact required to perform a replay is
actually bound at the requested as-of time.

It is read-only: it does not fetch weather/model data, materialize a P1,
activate an artifact, write a database row, or contact an order gateway.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any, Mapping
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import content_sha256  # noqa: E402
from bayes_basket_target.production_io import (  # noqa: E402
    target_registry_source_lane,
)
from bayes_basket_target.probability_evidence_store import (  # noqa: E402
    metar_trajectory_scope_key,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    MAX_JSON_OUTPUT_BYTES,
    assert_process_memory_budget,
    assert_target_db_capacity,
    write_bounded_json_if_changed,
)
from bayes_basket_target.station_registry import (  # noqa: E402
    coordinate_registry_manifest,
)


SCHEMA = "resident_p1_replay_binding_audit_v1"
MAX_STATIONS = 45
MAX_ENVELOPES_PER_SCOPE = 512
MAX_LINEAGE_BYTES = 131_072
UTC = timezone.utc
FORMAL_SCOPE = "FORMAL_PROMOTED"
TRAJECTORY_KIND = "METAR_TRAJECTORY_LIKELIHOOD"


def _utc(value: Any, *, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field}_timezone_required")
    return parsed.astimezone(UTC)


def _utc_text(value: Any, *, field: str) -> str:
    return _utc(value, field=field).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _lineage(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_LINEAGE_BYTES:
        raise ValueError("lineage_json_unavailable_or_oversized")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("lineage_json_invalid") from exc
    if not isinstance(parsed, Mapping):
        raise ValueError("lineage_json_mapping_required")
    return dict(parsed)


def _connection(path: Path) -> sqlite3.Connection:
    requested = path.expanduser()
    if requested.is_symlink():
        raise RuntimeError("resident_p1_replay_binding_db_symlink_forbidden")
    database = assert_target_db_capacity(requested.resolve(strict=True), projected_bytes=0)
    conn = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA busy_timeout=15000")
    if str(conn.execute("PRAGMA quick_check(1)").fetchone()[0]).lower() != "ok":
        conn.close()
        raise RuntimeError("resident_p1_replay_binding_db_quick_check_failed")
    return conn


def _scope_envelopes(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    event_date: str,
    as_of_utc: str,
) -> list[dict[str, Any]]:
    total = int(
        conn.execute(
            "SELECT COUNT(*) FROM basket_probability_envelope "
            "WHERE station_id=? AND event_date=? AND state='COMMITTED' "
            "AND stage IN ('P0','P1') AND decision_at_utc<=?",
            (station_id, event_date, as_of_utc),
        ).fetchone()[0]
    )
    if total > MAX_ENVELOPES_PER_SCOPE:
        raise RuntimeError("resident_p1_replay_binding_envelope_limit_exceeded")
    rows: list[dict[str, Any]] = []
    for row in conn.execute(
        "SELECT stage,checkpoint_id,settlement_timezone,decision_at_utc,"
        "fresh_until_utc,lineage_json,content_sha256 "
        "FROM basket_probability_envelope "
        "WHERE station_id=? AND event_date=? AND state='COMMITTED' "
        "AND stage IN ('P0','P1') AND decision_at_utc<=? "
        "ORDER BY decision_at_utc,checkpoint_id",
        (station_id, event_date, as_of_utc),
    ):
        stage = str(row["stage"] or "")
        if stage not in {"P0", "P1"}:
            raise RuntimeError("resident_p1_replay_binding_stage_invalid")
        decision = _utc_text(row["decision_at_utc"], field="decision_at_utc")
        fresh_until = _utc_text(row["fresh_until_utc"], field="fresh_until_utc")
        if _utc(fresh_until, field="fresh_until_utc") < _utc(
            decision, field="decision_at_utc"
        ):
            raise RuntimeError("resident_p1_replay_binding_freshness_clock_invalid")
        checkpoint_id = str(row["checkpoint_id"] or "").strip()
        content_sha = str(row["content_sha256"] or "").lower()
        if not checkpoint_id or len(content_sha) != 64:
            raise RuntimeError("resident_p1_replay_binding_identity_invalid")
        rows.append(
            {
                "stage": stage,
                "checkpoint_id": checkpoint_id,
                "settlement_timezone": str(row["settlement_timezone"] or "").strip(),
                "decision_at_utc": decision,
                "fresh_until_utc": fresh_until,
                "lineage": _lineage(row["lineage_json"]),
                "content_sha256": content_sha,
            }
        )
    if len(rows) != total:
        raise RuntimeError("resident_p1_replay_binding_read_count_changed")
    return rows


def _latest(rows: list[dict[str, Any]], *, stage: str) -> tuple[dict[str, Any] | None, bool]:
    selected = [row for row in rows if row["stage"] == stage]
    if not selected:
        return None, False
    latest_at = max(_utc(row["decision_at_utc"], field="decision_at_utc") for row in selected)
    latest = [
        row
        for row in selected
        if _utc(row["decision_at_utc"], field="decision_at_utc") == latest_at
    ]
    return (dict(latest[0]) if len(latest) == 1 else None), len(latest) != 1


def _replay_state(
    *,
    latest_p0: Mapping[str, Any] | None,
    p0_ambiguous: bool,
    latest_p1: Mapping[str, Any] | None,
    p1_ambiguous: bool,
    as_of_utc: str,
) -> dict[str, Any]:
    if p0_ambiguous:
        return {"state": "P0_SCOPE_AMBIGUOUS", "replay_required": True}
    if latest_p0 is None:
        return {"state": "P0_ROOT_MISSING", "replay_required": True}
    if p1_ambiguous:
        return {"state": "P1_SCOPE_AMBIGUOUS", "replay_required": True}
    if latest_p1 is None:
        return {"state": "NO_VISIBLE_P1", "replay_required": False}
    p1_root = str(latest_p1.get("lineage", {}).get("p0_root_checkpoint_id") or "")
    p0_root = str(latest_p0.get("checkpoint_id") or "")
    result = {
        "latest_p0_checkpoint_id": p0_root,
        "latest_p1_checkpoint_id": str(latest_p1.get("checkpoint_id") or ""),
        "latest_p1_p0_root_checkpoint_id": p1_root or None,
        "latest_p1_fresh_at_as_of": (
            _utc(latest_p1["fresh_until_utc"], field="fresh_until_utc")
            > _utc(as_of_utc, field="as_of_utc")
        ),
    }
    if p1_root != p0_root:
        return {
            **result,
            "state": "RESIDENT_P1_REPLAY_REQUIRED",
            "replay_required": True,
        }
    return {**result, "state": "P1_ROOT_CURRENT", "replay_required": False}


def _formal_trajectory_binding(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    event_date: str,
    latest_p0: Mapping[str, Any] | None,
    as_of_utc: str,
) -> dict[str, Any]:
    if latest_p0 is None:
        return {"state": "NOT_EVALUABLE_P0_ROOT_MISSING"}
    lineage = latest_p0.get("lineage")
    if not isinstance(lineage, Mapping):
        return {"state": "NOT_EVALUABLE_P0_LINEAGE_INVALID"}
    try:
        source_lane = target_registry_source_lane(
            conn, station_id=station_id, event_date=event_date
        )
        timezone_name = str(latest_p0.get("settlement_timezone") or "").strip()
        local_hour = _utc(as_of_utc, field="as_of_utc").astimezone(
            ZoneInfo(timezone_name)
        ).hour
        local_time_bucket = (
            "NIGHT" if local_hour < 6 else "MORNING" if local_hour < 12
            else "AFTERNOON" if local_hour < 18 else "EVENING"
        )
        scope_key = metar_trajectory_scope_key(
            source_lane=source_lane,
            season_bin=str(lineage.get("season_bin") or ""),
            local_time_bucket=local_time_bucket,
            maturity_bin="D0",
            settlement_definition=str(lineage.get("settlement_definition") or ""),
        )
    except Exception as exc:
        return {
            "state": "FORMAL_TRAJECTORY_SCOPE_UNRESOLVED",
            "reason": f"{type(exc).__name__}:{exc}",
        }
    rows = conn.execute(
        "SELECT a.activation_id,a.artifact_id,a.generation,a.content_sha256 "
        "FROM basket_probability_evidence_activation a "
        "JOIN basket_probability_evidence_artifact x ON x.artifact_id=a.artifact_id "
        "WHERE a.artifact_kind=? AND a.station_id=? AND a.scope_key=? "
        "AND a.authority_scope=? AND a.valid_from_utc<=? AND a.valid_until_utc>? "
        "AND x.artifact_kind=? AND x.station_id=? AND x.scope_key=? "
        "AND x.authority_scope=? AND x.oos_gate_passed=1 "
        "AND x.valid_from_utc<=? AND x.valid_until_utc>? "
        "ORDER BY a.generation DESC,a.activation_id DESC LIMIT 2",
        (
            TRAJECTORY_KIND, station_id, scope_key, FORMAL_SCOPE, as_of_utc, as_of_utc,
            TRAJECTORY_KIND, station_id, scope_key, FORMAL_SCOPE, as_of_utc, as_of_utc,
        ),
    ).fetchall()
    identity = {
        "source_lane": source_lane,
        "local_time_bucket": local_time_bucket,
        "scope_key": scope_key,
    }
    if not rows:
        return {**identity, "state": "FORMAL_TRAJECTORY_BINDING_MISSING"}
    if len(rows) != 1:
        return {**identity, "state": "FORMAL_TRAJECTORY_BINDING_AMBIGUOUS"}
    row = rows[0]
    return {
        **identity,
        "state": "FORMAL_TRAJECTORY_BINDING_PRESENT",
        "activation_id": str(row["activation_id"]),
        "artifact_id": str(row["artifact_id"]),
        "generation": int(row["generation"]),
        "activation_content_sha256": str(row["content_sha256"]),
    }


def build_report(
    database: Path,
    *,
    event_date: str,
    as_of_utc: str,
) -> dict[str, Any]:
    as_of = _utc_text(as_of_utc, field="as_of_utc")
    stations = tuple(coordinate_registry_manifest()["station_ids"])
    if len(stations) != MAX_STATIONS or len(set(stations)) != MAX_STATIONS:
        raise RuntimeError("resident_p1_replay_binding_station_scope_invalid")
    conn = _connection(database)
    try:
        reports: list[dict[str, Any]] = []
        for station_id in stations:
            assert_process_memory_budget()
            try:
                envelopes = _scope_envelopes(
                    conn,
                    station_id=station_id,
                    event_date=event_date,
                    as_of_utc=as_of,
                )
                latest_p0, p0_ambiguous = _latest(envelopes, stage="P0")
                latest_p1, p1_ambiguous = _latest(envelopes, stage="P1")
                replay = _replay_state(
                    latest_p0=latest_p0,
                    p0_ambiguous=p0_ambiguous,
                    latest_p1=latest_p1,
                    p1_ambiguous=p1_ambiguous,
                    as_of_utc=as_of,
                )
                trajectory = _formal_trajectory_binding(
                    conn,
                    station_id=station_id,
                    event_date=event_date,
                    latest_p0=latest_p0,
                    as_of_utc=as_of,
                )
                state = str(replay["state"])
                if (
                    replay["replay_required"] is True
                    and trajectory["state"] != "FORMAL_TRAJECTORY_BINDING_PRESENT"
                ):
                    state = "REPLAY_REQUIRED_FORMAL_TRAJECTORY_UNBOUND"
                reports.append(
                    {
                        "station_id": station_id,
                        "event_date": event_date,
                        "state": state,
                        "p0_visible_count": sum(
                            item["stage"] == "P0" for item in envelopes
                        ),
                        "p1_visible_count": sum(
                            item["stage"] == "P1" for item in envelopes
                        ),
                        "replay": replay,
                        "formal_trajectory_binding": trajectory,
                    }
                )
            except Exception as exc:
                reports.append(
                    {
                        "station_id": station_id,
                        "event_date": event_date,
                        "state": "AUDIT_SCOPE_INVALID",
                        "reason": f"{type(exc).__name__}:{exc}",
                    }
                )
    finally:
        conn.close()
    state_counts = Counter(str(row["state"]) for row in reports)
    replay_counts = Counter(
        str((row.get("replay") or {}).get("state") or "UNAVAILABLE")
        for row in reports
    )
    trajectory_counts = Counter(
        str((row.get("formal_trajectory_binding") or {}).get("state") or "UNAVAILABLE")
        for row in reports
    )
    identity = {
        "schema": SCHEMA,
        "as_of_utc": as_of,
        "event_date": event_date,
        "station_count": len(stations),
        "station_ids": list(stations),
        "reports": reports,
        "summary": {
            "state_counts": dict(sorted(state_counts.items())),
            "replay_state_counts": dict(sorted(replay_counts.items())),
            "formal_trajectory_binding_counts": dict(sorted(trajectory_counts.items())),
            "resident_p1_replay_required_count": sum(
                row.get("replay", {}).get("state")
                == "RESIDENT_P1_REPLAY_REQUIRED"
                for row in reports
            ),
            "formal_replay_binding_present_count": sum(
                row.get("formal_trajectory_binding", {}).get("state")
                == "FORMAL_TRAJECTORY_BINDING_PRESENT"
                for row in reports
            ),
        },
        "boundary": {
            "database_mode": "read_only_query_only",
            "writes_target_database": False,
            "fetches_model_data": False,
            "materializes_p1": False,
            "calls_order_gateway": False,
            "orders_submitted": 0,
        },
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db", type=Path, default=ROOT / "data" / "bayes_basket_target_ssot.db"
    )
    parser.add_argument("--event-date", required=True)
    parser.add_argument("--as-of-utc", required=True)
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "output" / "audits" / "resident_p1_replay_binding_audit.json",
    )
    args = parser.parse_args()
    report = build_report(
        args.db, event_date=args.event_date, as_of_utc=args.as_of_utc
    )
    wrote = write_bounded_json_if_changed(
        args.output.expanduser().resolve(), report, limit=MAX_JSON_OUTPUT_BYTES
    )
    print(
        json.dumps(
            {
                "schema": report["schema"],
                "station_count": report["station_count"],
                "resident_p1_replay_required_count": report["summary"]["resident_p1_replay_required_count"],
                "formal_replay_binding_present_count": report["summary"]["formal_replay_binding_present_count"],
                "output": str(args.output.expanduser().resolve()),
                "wrote": wrote,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
