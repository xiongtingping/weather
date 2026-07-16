#!/usr/bin/env python3
"""Atomically activate additive P1 schema and append strict forecast evidence.

This is the only recommended apply path for the first P1 forecast ingress. It
keeps P1 DDL, exact-schema verification and Tier-A ``source_observations``
append in one SQLite transaction: failure of any stage rolls back the entire
operation.  Default mode is read-only planning.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.append_strict_asof_forecast_p1_ingress import (  # noqa: E402
    DEFAULT_DB,
    DEFAULT_ROOT,
    collect_tier_a_forecast_p1_proposals,
)
from scripts.p1_forecast_ingress_staging_probe import build_p1_forecast_ingress_staging_probe  # noqa: E402
from sequential_bayes_schema_contract import (  # noqa: E402
    MIGRATION_ID,
    apply_forward_schema,
    build_forward_schema_plan,
    verify_schema,
)
from strict_asof_forecast_p1_ingress_writer import (  # noqa: E402
    StrictForecastP1IngressError,
    persist_tier_a_forecast_p1_proposal,
    verify_strict_forecast_p1_ingress_schema,
)


SCHEMA_VERSION = "p1_schema_and_strict_forecast_ingress_activation_v1"
CONFIRMATION_PHRASE = "ACTIVATE_P1_SCHEMA_AND_STRICT_FORECAST_INGRESS"


def _text(value: Any) -> str:
    return str(value or "").strip()


def build_activation_contract(
    *,
    db_path: Path | str = DEFAULT_DB,
    root: Path = DEFAULT_ROOT,
    station_id: str = "ZUUU",
    apply: bool = False,
    confirm: str = "",
    writer_quiescent: bool = False,
) -> dict[str, Any]:
    """Produce a no-write preflight for the single P1 activation transaction."""

    db = Path(db_path).expanduser().resolve()
    scan = collect_tier_a_forecast_p1_proposals(root=Path(root), station_id=station_id)
    blockers: list[str] = []
    if not scan["eligible"]:
        blockers.append("no_tier_a_forecast_proposals_available")
    if scan["unreadable"]:
        blockers.append("forecast_capture_files_unreadable")
    event_dates = sorted({_text(item.get("event_date")) for item in scan["eligible"] if _text(item.get("event_date"))})
    staging_probes = [
        build_p1_forecast_ingress_staging_probe(
            root=Path(root),
            event_date=event_date,
            station_id=station_id,
            target_identity=f"p1-activation-preflight-{event_date}",
        )
        for event_date in event_dates
    ]
    if any(probe.get("ok") is not True for probe in staging_probes):
        blockers.append("p1_forecast_ingress_staging_probe_failed")
    try:
        schema_plan = build_forward_schema_plan(db)
        with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
            p1_before = verify_schema(conn)
            ingress_before = verify_strict_forecast_p1_ingress_schema(conn)
    except sqlite3.Error:
        schema_plan = {"actions": []}
        p1_before = {"ok": False, "state": "db_unreadable"}
        ingress_before = {"ok": False, "state": "db_unreadable"}
        blockers.append("archive_db_unreadable")
    if apply and _text(confirm) != CONFIRMATION_PHRASE:
        blockers.append("confirmation_phrase_required")
    if apply and not writer_quiescent:
        blockers.append("writer_quiescent_assertion_required")
    return {
        "schema": SCHEMA_VERSION,
        "migration_id": MIGRATION_ID,
        "ok": not blockers,
        "state": "ready_to_apply" if apply and not blockers else "planned" if not blockers else "blocked",
        "db": str(db),
        "apply_requested": bool(apply),
        "confirmation_phrase_required": CONFIRMATION_PHRASE,
        "writer_quiescent_required": True,
        "blockers": sorted(set(blockers)),
        "p1_schema_before": p1_before,
        "ingress_schema_before": ingress_before,
        "schema_plan": schema_plan,
        "scan": scan,
        "staging_probes": staging_probes,
        "eligible_append_count": len(scan["eligible"]),
        "quarantined_tier_a_capture_count": len(scan["blocked"]),
        "transaction_contract": {
            "single_begin_immediate": True,
            "schema_ddl_and_forecast_append_atomic": True,
            "any_failure_rolls_back_ddl_and_rows": True,
            "updates_existing_rows": False,
            "creates_posterior": False,
            "places_orders": False,
        },
        "guards": {
            "writes_archive_db": False,
            "writes_research_files": False,
            "updates_existing_rows": False,
            "creates_posterior": False,
            "places_orders": False,
            "restarts_launchd": False,
        },
        "read_only": True,
    }


def apply_activation_contract(
    *,
    db_path: Path | str,
    root: Path,
    station_id: str,
    confirm: str,
    writer_quiescent: bool,
) -> dict[str, Any]:
    """Atomically apply P1 DDL and the current set of Tier-A proposals."""

    contract = build_activation_contract(
        db_path=db_path,
        root=root,
        station_id=station_id,
        apply=True,
        confirm=confirm,
        writer_quiescent=writer_quiescent,
    )
    if contract["ok"] is not True:
        contract["execution_mode"] = "blocked"
        return contract
    db = Path(db_path).expanduser().resolve()
    conn = sqlite3.connect(db, timeout=30)
    results: list[dict[str, Any]] = []
    try:
        conn.execute("BEGIN IMMEDIATE")
        schema_apply = apply_forward_schema(conn)
        p1_after = verify_schema(conn)
        ingress_after = verify_strict_forecast_p1_ingress_schema(conn)
        if p1_after.get("ok") is not True or ingress_after.get("ok") is not True:
            raise StrictForecastP1IngressError("p1_schema_or_forecast_ingress_verification_failed")
        for item in contract["scan"]["eligible"]:
            result = persist_tier_a_forecast_p1_proposal(conn, item["proposal"])
            results.append({"path": item["path"], **result})
        conn.commit()
    except (sqlite3.Error, StrictForecastP1IngressError) as exc:
        conn.rollback()
        contract.update(
            {
                "execution_mode": "rolled_back",
                "error": type(exc).__name__,
                "error_reason": _text(exc),
                "guards": {**contract["guards"], "writes_archive_db": False},
            }
        )
        return contract
    finally:
        conn.close()
    contract.update(
        {
            "ok": True,
            "state": "applied",
            "read_only": False,
            "execution_mode": "applied",
            "schema_apply": schema_apply,
            "p1_schema_after": p1_after,
            "ingress_schema_after": ingress_after,
            "append_results": results,
            "guards": {**contract["guards"], "writes_archive_db": True},
        }
    )
    return contract


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--root", default=str(DEFAULT_ROOT))
    parser.add_argument("--station-id", default="ZUUU")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="")
    parser.add_argument("--writer-quiescent", action="store_true")
    args = parser.parse_args(argv)
    if args.apply:
        result = apply_activation_contract(
            db_path=args.db,
            root=Path(args.root),
            station_id=args.station_id,
            confirm=args.confirm,
            writer_quiescent=bool(args.writer_quiescent),
        )
    else:
        result = build_activation_contract(
            db_path=args.db,
            root=Path(args.root),
            station_id=args.station_id,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("ok") is True or not args.apply else 2


if __name__ == "__main__":
    raise SystemExit(main())
