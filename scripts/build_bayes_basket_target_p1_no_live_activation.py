#!/usr/bin/env python3
"""Build one explicit P1 cold-start activation for production-no-live.

The activation is stage-specific and does not clone P0 station coefficients.
Until P1-specific station OOS artifacts exist, all three policy families use
the already governed global prior through declared fallback identities.  The
packet grants no live, gateway, intent, reservation, or order authority.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.artifact_ingress import SCHEMA  # noqa: E402
from bayes_basket_target.database import resolve_target_database  # noqa: E402
from bayes_basket_target.policy import StationBasketPolicyActivation  # noqa: E402
from bayes_basket_target.resource_bounds import write_bounded_json  # noqa: E402


def build(*, db_path: Path, station_id: str) -> dict:
    station = station_id.strip().upper()
    db = resolve_target_database(db_path.expanduser().resolve())
    uri = f"file:{db}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=30.0) as conn:
        p1_row = conn.execute(
            "SELECT lineage_json FROM basket_probability_envelope "
            "WHERE station_id=? AND stage='P1' AND state='COMMITTED' "
            "ORDER BY decision_at_utc DESC LIMIT 1",
            (station,),
        ).fetchone()
        required_maturity = ""
        if p1_row is not None:
            try:
                lineage = json.loads(str(p1_row[0] or ""))
            except (TypeError, ValueError) as exc:
                raise ValueError("p1_activation_probability_lineage_invalid") from exc
            if not isinstance(lineage, dict):
                raise ValueError("p1_activation_probability_lineage_invalid")
            required_maturity = str(
                lineage.get("lead_bucket") or ""
            ).strip()
            if not required_maturity:
                raise ValueError("p1_activation_probability_maturity_missing")
        query = (
            "SELECT stage,season_bin,maturity_bin,liquidity_bin,"
            "strategy_version,global_prior_id,valid_from_utc,valid_until_utc "
            "FROM basket_policy_activation WHERE station_id=? AND stage='P0' "
        )
        params: list[str] = [station]
        if required_maturity:
            query += "AND maturity_bin=? "
            params.append(required_maturity)
        query += "ORDER BY generation DESC"
        rows = conn.execute(query, params).fetchall()
        if len(rows) != 1:
            raise ValueError("p1_activation_source_p0_scope_not_unique")
        (
            _stage,
            season_bin,
            maturity_bin,
            liquidity_bin,
            strategy_version,
            global_prior_id,
            valid_from_utc,
            valid_until_utc,
        ) = rows[0]
        prior = conn.execute(
            "SELECT 1 FROM basket_global_prior_artifact WHERE global_prior_id=?",
            (global_prior_id,),
        ).fetchone()
        if prior is None:
            raise ValueError("p1_activation_global_prior_missing")
    activation = StationBasketPolicyActivation.create(
        activation_id=f"target-no-live-{station}-p1-cold-start-v1",
        station_id=station,
        stage="P1",
        season_bin=str(season_bin),
        maturity_bin=str(maturity_bin),
        liquidity_bin=str(liquidity_bin),
        strategy_version=str(strategy_version),
        global_prior_id=str(global_prior_id),
        target_artifact_id=None,
        reserve_artifact_id=None,
        cadence_artifact_id=None,
        fallback_families=["target", "reserve", "cadence"],
        generation=1,
        valid_from_utc=str(valid_from_utc),
        valid_until_utc=str(valid_until_utc),
        activated_at_utc=str(valid_from_utc),
        promoted=False,
    )
    return {
        "schema": SCHEMA,
        "policy_authority_scope": "PRODUCTION_NO_LIVE_CANDIDATE",
        "execution_mode": "production_no_live",
        "calibration_artifacts": [],
        "ensemble_selection_evidence": [],
        "ensemble_artifacts": [],
        "calibration_activations": [],
        "global_priors": [],
        "policy_artifacts": [],
        "policy_activations": [activation.to_dict()],
        "cash_phase_policies": [],
        "calibration_promotions": [],
        "policy_promotions": [],
        "boundary": {
            "stage_specific": True,
            "p0_policy_reused": False,
            "p1_station_oos_claimed": False,
            "global_fallback_explicit": True,
            "writes_production_db": False,
            "calls_order_gateway": False,
            "places_orders": False,
            "orders_submitted": 0,
        },
        "live_order_authorized": False,
        "live_submit_authorized": False,
        "places_orders": False,
        "calls_order_gateway": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--station-id", default="RKPK")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = build(db_path=args.db, station_id=args.station_id)
    output = args.output.expanduser().resolve()
    write_bounded_json(output, result)
    activation = result["policy_activations"][0]
    print(json.dumps({
        "activation_id": activation["activation_id"],
        "stage": activation["stage"],
        "fallback_families": activation["fallback_families"],
        "p0_policy_reused": False,
        "live_submit_authorized": False,
        "output": str(output),
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
