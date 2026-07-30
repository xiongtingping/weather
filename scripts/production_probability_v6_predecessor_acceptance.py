#!/usr/bin/env python3
"""Accept only the bounded stale-predecessor state caused by bundled drift."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.production_probability_v6_live_acceptance import audit as live_audit  # noqa: E402
from scripts.production_probability_v6_hotfix_bundle_audit import audit as bundle_audit  # noqa: E402
from physical_state_bayes_v6.schema_contract import build_forward_plan  # noqa: E402


def audit(db: Path, activation: Path, event_date: str, bundle: Path) -> dict:
    live = live_audit(db, activation, event_date)
    prepared = bundle_audit(ROOT, bundle, installed=False)
    blockers = set(live.get("blockers") or [])
    expected_blockers = {
        "active_v6_runtime_manifest_drift",
        "active_v6_schema_invalid",
        "active_v6_weak_source_snapshot_stale",
        "active_v6_source_time_freshness_invalid",
    }
    evidence = live.get("evidence") if isinstance(live.get("evidence"), dict) else {}
    freshness = evidence.get("weak_source_freshness") if isinstance(evidence.get("weak_source_freshness"), dict) else {}
    schema_plan = build_forward_plan(db)
    scenario_columns = {
        "scenario_joint_posterior_json", "scenario_joint_posterior_sha256",
        "scenario_joint_schema", "scenario_joint_forcing_generation_sha256",
    }
    schema_actions = list(schema_plan.get("actions") or [])
    schema_is_exact_additive_scenario_joint = (
        schema_plan.get("ok") is True
        and not list(schema_plan.get("incompatible_columns") or [])
        and {str(row.get("column") or "") for row in schema_actions} == scenario_columns
        and all(str(row.get("table") or "") == "posterior_checkpoints" for row in schema_actions)
    )
    expected_only = (
        "active_v6_runtime_manifest_drift" in blockers
        and blockers <= expected_blockers
        # Either freshness diagnostic may be emitted independently.  They are
        # separate checks, not an all-or-nothing pair: requiring both turns a
        # forward-repairable stale snapshot into a false hard block.
    )
    stale_state_is_forward_repairable = (
        blockers == {"active_v6_runtime_manifest_drift"}
        or (
            int(evidence.get("pending_canonical_event_count") or 0) == 0
            and freshness.get("state") == "stale_newer_revision_not_consumed"
            and not list(freshness.get("temporal_violations") or [])
            and bool(freshness.get("stale_sources"))
        )
    )
    if "active_v6_schema_invalid" in blockers:
        stale_state_is_forward_repairable = (
            stale_state_is_forward_repairable and schema_is_exact_additive_scenario_joint
        )
    ok = expected_only and stale_state_is_forward_repairable and prepared.get("ok") is True
    return {
        "schema": "production_probability_v6_drifted_predecessor_acceptance_v1",
        "ok": ok,
        "state": "accepted_expected_bundled_drift" if ok else "blocked",
        "blockers": [] if ok else sorted(blockers or {"predecessor_not_drifted_as_expected"}),
        "live_acceptance": live,
        "prepared_bundle": prepared,
        "scenario_joint_schema_forward_plan": schema_plan,
        "scenario_joint_schema_is_exact_additive": schema_is_exact_additive_scenario_joint,
        "allows_only_runtime_manifest_drift_or_its_forward_repairable_weak_freshness_pair": True,
        "previous_activation_remains_v6": True,
        "v5_fallback": False,
        "read_only": True,
        "places_orders": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=ROOT / "data/archive.db")
    parser.add_argument("--activation", type=Path, default=ROOT / "config/production_probability_runtime_activation.json")
    parser.add_argument("--event-date", required=True)
    parser.add_argument("--bundle", type=Path, default=ROOT / "config/production_probability_runtime_bundle.v6_candidate.json")
    args = parser.parse_args()
    result = audit(args.db, args.activation, args.event_date, args.bundle)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
