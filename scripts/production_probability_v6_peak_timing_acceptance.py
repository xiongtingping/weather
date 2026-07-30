#!/usr/bin/env python3
"""Read-only acceptance for v6 peak timing in the unique public SSOT."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from peak_timing_state_runtime import validate_peak_timing_state_contract  # noqa: E402
from production_probability_v6_activation_contract import IDENTIFIABLE_PHYSICAL_COMPOSER_SCHEMA  # noqa: E402


SCHEMA_VERSION = "production_probability_v6_peak_timing_acceptance_v1"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _parse(value: Any, default: Any) -> Any:
    try:
        return json.loads(_text(value))
    except (TypeError, ValueError):
        return default


def audit_connection(conn: sqlite3.Connection, *, event_date: str) -> dict[str, Any]:
    day = _text(event_date)[:10]
    row = conn.execute(
        "SELECT h.fact_json,h.posterior_run_id,h.composer_version FROM event_fact_latest l "
        "JOIN event_fact_history h ON h.id=l.fact_id "
        "WHERE l.event_date=? "
        "ORDER BY h.id DESC LIMIT 1",
        (day,),
    ).fetchone()
    if row is not None and _text(row[0]):
        payload = _mapping(_parse(row[0], {}))
        fact = _mapping(payload.get("posterior_ssot_fact"))
        if _text(row[2]) == "identifiable_physical_state_bayes_production" or _text(fact.get("composer_schema")) == IDENTIFIABLE_PHYSICAL_COMPOSER_SCHEMA:
            return {
                "schema": SCHEMA_VERSION,
                "ok": True,
                "state": "not_applicable_identifiable_physical_state_space",
                "event_date": day,
                "posterior_run_id": _text(row[1]),
                "posterior_checkpoint_id": "",
                "canonical_observation_event_id": "",
                "strong_observation_update_id": "",
                "peak_timing": {
                    "state": "not_applicable",
                    "does_not_update_physical_x": True,
                    "read_only": True,
                },
                "reported_x_normalized": True,
                "blockers": [],
                "read_only": True,
                "places_orders": False,
            }
    blockers: list[str] = []
    if row is None:
        blockers.append("v6_peak_timing_public_ssot_missing")
        ssot: Mapping[str, Any] = {}
    else:
        ssot = _mapping(_mapping(_parse(row[0], {})).get("posterior_ssot_fact"))
    checkpoint_id = _text(ssot.get("posterior_checkpoint_id"))
    event_id = _text(ssot.get("last_canonical_event_id"))
    update_id = _text(ssot.get("last_strong_update_id"))
    timing = _mapping(ssot.get("peak_timing_state"))
    surface, timing_issues = validate_peak_timing_state_contract(
        timing,
        expected_event_id=event_id,
        expected_update_id=update_id,
    )
    blockers.extend(timing_issues)
    persisted = None
    if checkpoint_id and update_id:
        persisted = conn.execute(
            "SELECT p.posterior_run_id,p.last_canonical_event_id,p.last_strong_update_id,"
            "u.path_auxiliary_json FROM posterior_checkpoints p "
            "JOIN strong_observation_updates u ON u.strong_observation_update_id=p.last_strong_update_id "
            "WHERE p.posterior_checkpoint_id=? AND u.strong_observation_update_id=?",
            (checkpoint_id, update_id),
        ).fetchone()
    if persisted is None:
        blockers.append("v6_peak_timing_checkpoint_update_lineage_missing")
    else:
        persisted_timing = _mapping(
            _mapping(_parse(persisted[3], {})).get("peak_timing_state")
        )
        if (
            _text(persisted[0]) != _text(row[1] if row else "")
            or _text(persisted[1]) != event_id
            or _text(persisted[2]) != update_id
            or dict(persisted_timing) != dict(timing)
        ):
            blockers.append("v6_peak_timing_public_persisted_binding_mismatch")
    probabilities = _mapping(ssot.get("reported_physical_x_distribution"))
    try:
        normalized = bool(probabilities) and abs(
            sum(float(value) for value in probabilities.values()) - 1.0
        ) <= 1e-8
    except (TypeError, ValueError):
        normalized = False
    if not normalized:
        blockers.append("v6_peak_timing_acceptance_reported_x_invalid")
    return {
        "schema": SCHEMA_VERSION,
        "ok": not blockers,
        "state": "accepted" if not blockers else "blocked",
        "event_date": day,
        "posterior_run_id": _text(row[1] if row else ""),
        "posterior_checkpoint_id": checkpoint_id,
        "canonical_observation_event_id": event_id,
        "strong_observation_update_id": update_id,
        "peak_timing": surface,
        "reported_x_normalized": normalized,
        "blockers": sorted(set(blockers)),
        "read_only": True,
        "places_orders": False,
    }


def audit(*, db: Path, event_date: str) -> dict[str, Any]:
    with sqlite3.connect(f"file:{db.expanduser().resolve()}?mode=ro", uri=True) as conn:
        return audit_connection(conn, event_date=event_date)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=ROOT / "data/archive.db")
    parser.add_argument("--event-date", required=True)
    args = parser.parse_args()
    result = audit(db=args.db, event_date=args.event_date)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("ok") is True else 2


if __name__ == "__main__":
    raise SystemExit(main())
