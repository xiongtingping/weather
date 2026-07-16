#!/usr/bin/env python3
"""Append one immutable Tier-A official-NWP daily peak to P1 explicitly.

The default is a read-only proposal.  Apply requires an explicit confirmation
and a caller assertion that P1 writers are quiescent; the only possible write
is one append to ``source_observations`` inside a transaction.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from official_nwp_daily_peak_strict_asof_runtime import build_official_nwp_daily_peak_tier_a_proposal  # noqa: E402
from strict_asof_forecast_p1_ingress_writer import (  # noqa: E402
    StrictForecastP1IngressError,
    persist_tier_a_forecast_p1_proposal,
    verify_strict_forecast_p1_ingress_schema,
)


CONFIRMATION_PHRASE = "APPEND_OFFICIAL_NWP_DAILY_PEAK_P1_INGRESS"


def build_contract(*, db: Path, revision_path: Path, station_id: str) -> dict:
    try:
        revision = json.loads(revision_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {"schema": "official_nwp_daily_peak_p1_ingress_v1", "ok": False, "state": "blocked", "blockers": ["official_nwp_daily_peak_revision_unreadable"]}
    proposal = build_official_nwp_daily_peak_tier_a_proposal(revision=revision, station_id=station_id, revision_ref=str(revision_path.resolve()))
    try:
        with sqlite3.connect(f"file:{db.resolve()}?mode=ro", uri=True) as conn:
            schema = verify_strict_forecast_p1_ingress_schema(conn)
    except sqlite3.Error:
        schema = {"ok": False, "state": "db_unreadable"}
    blockers = list(proposal.get("block_reasons") or [])
    if schema.get("ok") is not True:
        blockers.append("p1_ingress_schema_not_ready")
    return {
        "schema": "official_nwp_daily_peak_p1_ingress_v1",
        "ok": not blockers,
        "state": "ready_to_apply" if not blockers else "blocked",
        "db": str(db.resolve()),
        "revision_path": str(revision_path.resolve()),
        "proposal": proposal,
        "schema_verification": schema,
        "blockers": sorted(set(blockers)),
        "confirmation_phrase_required": CONFIRMATION_PHRASE,
        "writer_quiescent_required": True,
        "boundary": {"writes_posterior": False, "places_orders": False},
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision", type=Path, required=True)
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "archive.db")
    parser.add_argument("--station-id", default="ZUUU")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="")
    parser.add_argument("--writer-quiescent", action="store_true")
    args = parser.parse_args(argv)
    result = build_contract(db=args.db, revision_path=args.revision, station_id=args.station_id)
    if args.apply:
        if args.confirm != CONFIRMATION_PHRASE:
            result.update({"ok": False, "state": "blocked", "blockers": sorted(set([*result["blockers"], "confirmation_phrase_required"]))})
        elif not args.writer_quiescent:
            result.update({"ok": False, "state": "blocked", "blockers": sorted(set([*result["blockers"], "writer_quiescent_assertion_required"]))})
        elif result.get("ok") is True:
            try:
                with sqlite3.connect(args.db, timeout=30) as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    appended = persist_tier_a_forecast_p1_proposal(conn, result["proposal"])
                    conn.commit()
                result.update({"ok": True, "state": "applied", "append_result": appended})
            except (sqlite3.Error, StrictForecastP1IngressError) as exc:
                result.update({"ok": False, "state": "rolled_back", "blockers": [type(exc).__name__]})
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("ok") is True else 2


if __name__ == "__main__":
    raise SystemExit(main())
