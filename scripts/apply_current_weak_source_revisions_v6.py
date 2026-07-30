#!/usr/bin/env python3
"""Apply newly visible official/independent weak-source revisions to v6."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from archive_db_write_queue import connect_archive_db_rw
from production_probability_v6_activation_contract import (
    IDENTIFIABLE_PHYSICAL_COMPOSER_SCHEMA,
    validate_v6_activation,
)
from production_probability_v6_market_inputs_runtime import prewarm_v6_market_inputs
from production_probability_v6_weak_revision_event_runtime import apply_v6_current_weak_source_revisions


SCHEMA_VERSION = "apply_current_weak_source_revisions_v6_v1"


def _target_days(*, event_date: str, day_offset: int) -> list[str]:
    if event_date:
        return [event_date[:10]]
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    # Forecast sources remain decision-relevant across the D1 -> D0 boundary.
    # Always reconcile D0 as well as the requested future offset so a target
    # cannot silently freeze when its local date becomes today.
    return sorted(
        {
            today.isoformat(),
            (today + timedelta(days=day_offset)).isoformat(),
        }
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(ROOT / "data/archive.db"))
    parser.add_argument("--activation", default=str(ROOT / "config/production_probability_runtime_activation.json"))
    parser.add_argument("--event-date")
    parser.add_argument("--day-offset", type=int, default=1)
    args = parser.parse_args()
    days = _target_days(event_date=str(args.event_date or ""), day_offset=args.day_offset)
    decision_as_of = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    results = []
    try:
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
            for day in days
        }
        with connect_archive_db_rw(Path(args.db), owner="current_weak_source_revisions_v6") as conn:
            for day in days:
                target = f"physical_weather_day_v1:ZUUU:{day}"
                initialized = conn.execute(
                    "SELECT 1 FROM production_initial_posterior_lineage "
                    "WHERE target_identity=? AND activation_contract_sha256=? LIMIT 1",
                    (target, str(activation.get("activation_contract_sha256") or "")),
                ).fetchone() is not None
                if initialized:
                    outcome = apply_v6_current_weak_source_revisions(
                        conn,
                        activation=activation,
                        event_date=day,
                        decision_as_of=decision_as_of,
                        market_inputs=market_inputs_by_day[day],
                    )
                else:
                    outcome = {
                        "ok": True,
                        "state": "deferred_until_p0",
                        "reason": "target_p0_not_initialized",
                        "source_archive_remains_authoritative": True,
                        "places_orders": False,
                    }
                results.append({"event_date": day, **outcome})
        failures = [row for row in results if row.get("ok") is not True]
        applied = [row for row in results if row.get("state") == "committed"]
        result = {
            "schema": SCHEMA_VERSION,
            "ok": not failures,
            "state": "blocked" if failures else "weak_revisions_applied" if applied else "weak_sources_current",
            "decision_as_of": decision_as_of,
            "target_day_count": len(days),
            "target_results": results,
            "places_orders": False,
        }
    except Exception as exc:
        result = {
            "schema": SCHEMA_VERSION,
            "event_dates": days,
            "ok": False,
            "state": "blocked",
            "reason": f"current_weak_source_revision_apply_failed:{type(exc).__name__}",
            "exception_detail": str(exc)[:300],
            "places_orders": False,
        }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("ok") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
