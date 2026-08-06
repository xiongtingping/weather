#!/usr/bin/env python3
"""Freeze exact five-model PR10 masks from event-only canonical facts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.canonical_ingress import ingest_canonical_contract  # noqa: E402
from bayes_basket_target.canonical_ssot_v15_migration import TARGET_TABLE_DDL, TARGET_TRIGGER_DDL  # noqa: E402
from bayes_basket_target.contracts import content_sha256  # noqa: E402
from bayes_basket_target.model_oos_selection import build_historical_holdout_reservation  # noqa: E402
from bayes_basket_target.resource_bounds import MAX_JSON_INPUT_BYTES, load_bounded_json, write_bounded_json  # noqa: E402


MODELS = ("CMA", "ECMWF", "GEM", "GFS", "ICON")
EXCLUDED_ALREADY_EVALUATED = {("CYYZ", "d1"), ("CYYZ", "d2"), ("NZWN", "d1"), ("NZWN", "d2")}


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(TARGET_TABLE_DDL)
    for ddl in TARGET_TRIGGER_DDL:
        conn.execute(ddl)
    return conn


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--truth-contract", type=Path, action="append", required=True)
    parser.add_argument("--event-bundle", type=Path, action="append", required=True)
    parser.add_argument("--registered-at-utc", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    conn = _db()
    bundles = [load_bounded_json(path.resolve(strict=True), limit=64 * 1024 * 1024) for path in args.event_bundle]
    try:
        for path in args.truth_contract:
            contract = load_bounded_json(path.resolve(strict=True), limit=MAX_JSON_INPUT_BYTES)
            ingest_canonical_contract(conn, contract, commit=True, ingested_at_utc=str(contract["as_of_utc"]))
        events: dict[tuple[str, str, str], dict[str, dict[str, object]]] = {}
        for bundle in bundles:
            for contract in bundle["contracts"]:
                ingest_canonical_contract(conn, contract, commit=True, ingested_at_utc=str(contract["as_of_utc"]))
                for event in contract["event_facts"]:
                    key = (event["station_id"], event["lead_bucket"], event["model_id"])
                    by_date = events.setdefault(key, {})
                    prior = by_date.get(event["event_date"])
                    if prior is not None and prior["content_sha256"] != event["content_sha256"]:
                        raise RuntimeError("pr10_reserved_event_date_conflict")
                    by_date[event["event_date"]] = event
        absence_basis = content_sha256({
            "prior_cohort_bundle_content_sha256": "149bd131222d56d3255ec4dfa1f74e9b721862358402bbf5c35d8ecbebfc0565",
            "excluded_already_evaluated_scopes": sorted([list(item) for item in EXCLUDED_ALREADY_EVALUATED]),
            "rule": "NO_PRIOR_MODEL_OOS_COHORT_FOR_RESERVED_SCOPE",
        })
        reservations = []
        gaps = []
        stations = sorted({key[0] for key in events})
        for station in stations:
            for lead in ("d1", "d2"):
                if (station, lead) in EXCLUDED_ALREADY_EVALUATED:
                    gaps.append({"station_id": station, "lead_bucket": lead, "blocker": "HOLDOUT_ALREADY_EVALUATED"})
                    continue
                model_rows = [events.get((station, lead, model), {}) for model in MODELS]
                common = sorted(set.intersection(*(set(rows) for rows in model_rows))) if all(model_rows) else []
                if len(common) < 28:
                    gaps.append({"station_id": station, "lead_bucket": lead, "blocker": "OOS_SAMPLE_SHORT", "common_event_count": len(common)})
                    continue
                chosen = common[-28:]
                vectors = [[rows[day]["content_sha256"] for day in chosen] for rows in model_rows]
                reservation = build_historical_holdout_reservation(
                    conn,
                    component_event_content_sha256s=vectors,
                    season_bin="JJA",
                    registered_at_utc=args.registered_at_utc,
                    prior_evaluation_absence_basis_sha256=absence_basis,
                )
                reservations.append(reservation)
        identity = {
            "schema": "bayes_basket_target_pr10_reserved_mask_bundle_v1",
            "registered_at_utc": args.registered_at_utc,
            "state": "MATERIALIZED_UNPUBLISHED" if reservations else "BLOCKED",
            "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
            "source_event_bundle_content_sha256s": [item["content_sha256"] for item in bundles],
            "prior_evaluation_absence_basis_sha256": absence_basis,
            "reservation_count": len(reservations),
            "reservations": reservations,
            "gaps": gaps,
            "consumable": False,
        }
        output = {**identity, "content_sha256": content_sha256(identity)}
        write_bounded_json(args.output.resolve(), output, limit=64 * 1024 * 1024, max_nodes=2_000_000)
        print(json.dumps({"reservation_count": len(reservations), "gap_count": len(gaps), "content_sha256": output["content_sha256"]}, sort_keys=True))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
