#!/usr/bin/env python3
"""Apply an archived AMSC TAF/AMD/COR revision to production v6."""

from __future__ import annotations

import argparse
from datetime import date, timedelta
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from archive_db_write_queue import connect_archive_db_rw
from production_probability_v6_activation_contract import (
    IDENTIFIABLE_PHYSICAL_COMPOSER_SCHEMA,
    validate_v6_activation,
)
from production_probability_v6_market_inputs_runtime import prewarm_v6_market_inputs
from production_probability_v6_weak_revision_event_runtime import apply_v6_taf_revision_from_source_observations


SCHEMA_VERSION = "apply_amsc_taf_forecast_revision_v6_from_plan_v1"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(ROOT / "data" / "archive.db"))
    parser.add_argument("--plan", required=True)
    parser.add_argument("--activation", default=str(ROOT / "config/production_probability_runtime_activation.json"))
    args = parser.parse_args()
    try:
        plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
        effective = plan.get("effective_payload") if isinstance(plan.get("effective_payload"), dict) else {}
        plan_day = str(plan.get("event_date") or effective.get("event_date") or "")[:10]
        decision_as_of = str(plan.get("fact_as_of") or effective.get("fact_as_of") or "")
        observations = [
            row
            for row in (effective.get("source_observations") or [])
            if isinstance(row, dict) and str(row.get("source_id") or "") == "zuuu_taf_forecast_amsc"
        ]
        eligible_days = {plan_day, (date.fromisoformat(plan_day) + timedelta(days=1)).isoformat()}
        target_days = sorted(
            {
                str(row.get("event_date") or (row.get("payload") or {}).get("event_date") or "")[:10]
                for row in observations
                if str(row.get("event_date") or (row.get("payload") or {}).get("event_date") or "")[:10]
            }
            & eligible_days
        )
        activation = validate_v6_activation(json.loads(Path(args.activation).read_text(encoding="utf-8")))
        projection_role = (
            "basket_probability_only"
            if str(activation.get("composer_schema") or "")
            == IDENTIFIABLE_PHYSICAL_COMPOSER_SCHEMA
            else "legacy_realization"
        )
        market_inputs_by_day = {
            day: prewarm_v6_market_inputs(
                event_date=day,
                root=ROOT,
                strategy_projection_role=projection_role,
            )
            for day in target_days
        }
        results = []
        with connect_archive_db_rw(Path(args.db), owner="amsc_taf_forecast_revision_v6") as conn:
            for day in target_days:
                target = f"physical_weather_day_v1:ZUUU:{day}"
                initialized = conn.execute(
                    "SELECT 1 FROM production_initial_posterior_lineage "
                    "WHERE target_identity=? AND activation_contract_sha256=? LIMIT 1",
                    (target, str(activation.get("activation_contract_sha256") or "")),
                ).fetchone() is not None
                if not initialized:
                    results.append(
                        {
                            "event_date": day,
                            "schema": SCHEMA_VERSION,
                            "ok": day != plan_day,
                            "state": "deferred_until_p0" if day != plan_day else "blocked",
                            "reason": "target_p0_not_initialized",
                            "source_archive_remains_authoritative": True,
                            "places_orders": False,
                        }
                    )
                    continue
                outcome = apply_v6_taf_revision_from_source_observations(
                    conn,
                    activation=activation,
                    event_date=day,
                    source_observations=observations,
                    decision_as_of=decision_as_of,
                    public_slug="",
                    market_inputs=market_inputs_by_day[day],
                )
                results.append({"event_date": day, **outcome})
        failures = [item for item in results if item.get("ok") is not True]
        applied = [
            item
            for item in results
            if item.get("state")
            not in {"no_new_taf_revision", "revision_already_visible", "deferred_until_p0"}
            and item.get("ok") is True
        ]
        result = {
            "schema": SCHEMA_VERSION,
            "ok": not failures,
            "state": "blocked" if failures else "taf_revisions_applied" if applied else "no_new_taf_revision",
            "target_results": results,
            "target_day_count": len(target_days),
            "applied_target_count": len(applied),
            "places_orders": False,
        }
    except Exception as exc:
        result = {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "reason": f"taf_forecast_revision_v6_apply_failed:{type(exc).__name__}",
            "exception_detail": str(exc)[:300],
            "places_orders": False,
        }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("ok") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
