#!/usr/bin/env python3
"""Append eligible official daily-peak revisions into P1, independently per source.

This is the prospective formal-data bridge for weak-likelihood learning.  It
never fetches data, builds a posterior, or touches decisions/orders.  Each
already immutable official daily-curve revision is checked by the canonical
Tier-A proposal builder, then appended idempotently in its own short P1
transaction.  One unavailable provider therefore creates a missingness signal
only; it cannot suppress another provider's valid revision.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from official_nwp_daily_peak_strict_asof_runtime import build_official_nwp_daily_peak_tier_a_proposal  # noqa: E402
from strict_asof_forecast_p1_ingress_writer import (  # noqa: E402
    StrictForecastP1IngressError,
    persist_tier_a_forecast_p1_proposal,
    verify_strict_forecast_p1_ingress_schema,
)


SCHEMA_VERSION = "official_nwp_daily_peak_p1_materialization_v1"
CONFIRMATION_PHRASE = "MATERIALIZE_OFFICIAL_NWP_DAILY_PEAK_P1"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _read_json(path: Path) -> Mapping[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, Mapping) else None


def _revision_paths(root: Path) -> list[Path]:
    return sorted(path for path in root.glob("*/daily_peak_revisions/*.json") if path.is_file())


def plan_official_nwp_daily_peak_p1_materialization(
    *,
    db: Path,
    curve_source_root: Path,
    station_id: str = "ZUUU",
) -> dict[str, Any]:
    """Return only individually valid official P1 proposals; no writes."""

    database = db.expanduser().resolve()
    root = curve_source_root.expanduser().resolve()
    try:
        with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as conn:
            schema = verify_strict_forecast_p1_ingress_schema(conn)
    except sqlite3.Error:
        schema = {"ok": False, "state": "db_unreadable"}
    proposals: list[dict[str, Any]] = []
    quarantined: list[dict[str, Any]] = []
    for path in _revision_paths(root):
        revision = _read_json(path)
        if revision is None:
            quarantined.append({"revision_path": str(path), "block_reasons": ["official_nwp_daily_peak_revision_unreadable"]})
            continue
        proposal = build_official_nwp_daily_peak_tier_a_proposal(
            revision=revision,
            station_id=station_id,
            revision_ref=str(path),
        )
        if proposal.get("ok") is not True:
            quarantined.append({
                "revision_path": str(path),
                "provider_id": _text(revision.get("source_id")),
                "event_date": _text(revision.get("event_date"))[:10],
                "block_reasons": list(proposal.get("block_reasons") or []),
            })
            continue
        proposals.append({
            "revision_path": str(path),
            "provider_id": _text(revision.get("source_id")),
            "event_date": _text(revision.get("event_date"))[:10],
            "proposal": proposal,
        })
    return {
        "schema": SCHEMA_VERSION,
        "ok": schema.get("ok") is True,
        "state": "ready_to_apply" if schema.get("ok") is True and proposals else "pending_or_blocked",
        "db": str(database),
        "curve_source_root": str(root),
        "schema_verification": schema,
        "eligible_proposals": proposals,
        "quarantined_revisions": quarantined,
        "boundary": {
            "official_complete_curve_only": True,
            "independent_provider_quarantine": True,
            "writes_posterior": False,
            "places_orders": False,
        },
    }


def materialize_official_nwp_daily_peak_p1(
    *,
    db: Path,
    curve_source_root: Path,
    station_id: str = "ZUUU",
    apply: bool = False,
) -> dict[str, Any]:
    """Apply each planned proposal separately, preserving provider independence."""

    result = plan_official_nwp_daily_peak_p1_materialization(
        db=db,
        curve_source_root=curve_source_root,
        station_id=station_id,
    )
    if not apply:
        result["apply_authorized"] = False
        return result
    if result.get("ok") is not True:
        result["state"] = "blocked"
        return result
    outcomes: list[dict[str, Any]] = []
    for item in result["eligible_proposals"]:
        try:
            with sqlite3.connect(db.expanduser().resolve(), timeout=0.25) as conn:
                conn.execute("BEGIN IMMEDIATE")
                appended = persist_tier_a_forecast_p1_proposal(conn, item["proposal"])
                conn.commit()
            outcomes.append({
                "revision_path": item["revision_path"],
                "provider_id": item["provider_id"],
                "event_date": item["event_date"],
                "state": appended.get("state"),
                "append_result": appended,
            })
        except (sqlite3.Error, StrictForecastP1IngressError) as exc:
            outcomes.append({
                "revision_path": item["revision_path"],
                "provider_id": item["provider_id"],
                "event_date": item["event_date"],
                "state": "quarantined_p1_append_failure",
                "block_reasons": [type(exc).__name__],
            })
    result["apply_authorized"] = True
    result["outcomes"] = outcomes
    result["state"] = "applied" if any(item["state"] in {"appended", "idempotent"} for item in outcomes) else "pending_or_blocked"
    result["ok"] = not any(item["state"] == "quarantined_p1_append_failure" for item in outcomes)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "archive.db")
    parser.add_argument("--curve-source-root", type=Path, default=ROOT / "data" / "runtime" / "official_nwp_curve_sources_v2")
    parser.add_argument("--station-id", default="ZUUU")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args(argv)
    if args.apply and args.confirm != CONFIRMATION_PHRASE:
        print(json.dumps({
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "block_reasons": ["official_nwp_daily_peak_p1_materialization_confirmation_required"],
            "confirmation_phrase_required": CONFIRMATION_PHRASE,
            "boundary": {"writes_posterior": False, "places_orders": False},
        }, ensure_ascii=False, indent=2, sort_keys=True))
        return 2
    result = materialize_official_nwp_daily_peak_p1(
        db=args.db,
        curve_source_root=args.curve_source_root,
        station_id=args.station_id,
        apply=args.apply,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("ok") is True else 2


if __name__ == "__main__":
    raise SystemExit(main())
