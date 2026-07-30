#!/usr/bin/env python3
"""Build bounded, no-live cash-phase OOS evidence without touching a DB."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bayes_basket_target.cash_phase_oos import (  # noqa: E402
    DEFAULT_MIN_UNIQUE_EVENTS_PER_PHASE,
    EVIDENCE_BUNDLE_SCHEMA,
    MAX_ROWS,
    MAX_STATIONS,
    REQUIRED_FAULT_CASES,
    build_cash_phase_oos_evidence,
)
from bayes_basket_target.contracts import ContractError, content_sha256  # noqa: E402
from bayes_basket_target.resource_bounds import (  # noqa: E402
    load_bounded_json,
    write_bounded_json,
)
from bayes_basket_target.forward_observation_store import (  # noqa: E402
    DEFAULT_STORE_PATH,
    read_forward_observation_packet,
)


DEFAULT_OBSERVATIONS = DEFAULT_STORE_PATH
DEFAULT_POLICIES = (
    ROOT
    / "output/audits/bayes_basket_target_kst_recalibration_20260727"
    / "no_live_candidate_artifacts_RKPK_20260727.json"
)
DEFAULT_REGISTRY = ROOT / "data/bayes_basket_target_market_registry_snapshot_boundary_fixed.json"
DEFAULT_TRAINING_CUTOFFS = (
    ROOT / "output/audits/bayes_basket_target_cash_phase_training_cutoffs.json"
)
DEFAULT_FAULT_CASES = (
    ROOT / "output/audits/bayes_basket_target_cash_phase_fault_cases.json"
)
DEFAULT_OUTPUT = (
    ROOT / "output/audits/bayes_basket_target_cash_phase_oos_evidence.json"
)
MAX_OUTPUT_BYTES = 8 * 1024 * 1024


def _load_optional(path: Path) -> tuple[Any | None, str | None]:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        return None, f"input_missing:{resolved}"
    try:
        if resolved.suffix.lower() in {".sqlite", ".sqlite3"}:
            return read_forward_observation_packet(resolved), None
        return load_bounded_json(resolved), None
    except Exception as exc:
        return None, f"input_invalid:{resolved}:{type(exc).__name__}:{exc}"


def _sequence(
    value: Any,
    *,
    keys: Sequence[str],
    name: str,
) -> list[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        for key in keys:
            if key in value:
                value = value[key]
                break
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ContractError(f"{name}_sequence_required")
    result: list[Mapping[str, Any]] = []
    for row in value:
        if not isinstance(row, Mapping):
            raise ContractError(f"{name}_row_mapping_required")
        result.append(dict(row))
    return result


def _mapping(
    value: Any,
    *,
    keys: Sequence[str],
    name: str,
) -> dict[str, Any]:
    if isinstance(value, Mapping):
        for key in keys:
            if key in value and isinstance(value[key], Mapping):
                value = value[key]
                break
    if not isinstance(value, Mapping):
        raise ContractError(f"{name}_mapping_required")
    return {str(key): item for key, item in value.items()}


def _rkpk_inventory(
    *,
    policies: Sequence[Mapping[str, Any]],
    registry_rows: Sequence[Mapping[str, Any]],
    observations: Sequence[Mapping[str, Any]],
    training_cutoffs: Mapping[str, Any],
    fault_cases: Sequence[Mapping[str, Any]],
    minimum_unique_events_per_phase: int,
) -> dict[str, Any]:
    station = "RKPK"
    station_policies = [
        row for row in policies if str(row.get("station_id") or "") == station
    ]
    registry_timezones = sorted({
        str(row.get("settlement_timezone") or "")
        for row in registry_rows
        if str(row.get("station_id") or "") == station
        and str(row.get("settlement_timezone") or "")
    })
    station_rows = [
        row for row in observations if str(row.get("station_id") or "") == station
    ]
    phase_counts = {
        phase: sum(
            1
            for row in station_rows
            if str(row.get("phase", row.get("cash_phase", ""))).upper() == phase
        )
        for phase in ("HEAVY", "STANDARD", "TAIL")
    }
    settled = sum(
        1
        for row in station_rows
        if row.get("settlement_pnl_usdc") is not None
        and (row.get("labels") or row.get("label_revisions") or row.get("label"))
    )
    price = sum(
        1
        for row in station_rows
        if row.get("all_in_yes_ask", row.get("price")) is not None
    )
    missing: list[str] = []
    if len(station_policies) != 1:
        missing.append(f"design_candidate_policy_not_unique:{len(station_policies)}")
    else:
        policy = station_policies[0]
        if policy.get("evidence_status") != "DESIGN_CANDIDATE":
            missing.append("policy_not_design_candidate")
        if policy.get("promoted") is not False:
            missing.append("policy_promoted_flag_not_false")
        if registry_timezones != [str(policy.get("station_timezone") or "")]:
            missing.append("policy_registry_timezone_mismatch")
    if len(registry_timezones) != 1:
        missing.append(f"registry_timezone_not_unique:{len(registry_timezones)}")
    if station not in training_cutoffs:
        missing.append("policy_training_cutoff_missing")
    for phase, count in phase_counts.items():
        deficit = max(0, minimum_unique_events_per_phase - count)
        if deficit:
            missing.append(
                f"phase_{phase.lower()}_unique_event_deficit:{deficit}"
            )
    if not station_rows:
        missing.append("forward_realized_observations_missing")
    required_total = minimum_unique_events_per_phase * 3
    if settled < required_total:
        missing.append(
            "settled_attribution_deficit:"
            + str(required_total - settled)
        )
    if price < required_total:
        missing.append(
            "price_coverage_deficit:"
            + str(required_total - price)
        )
    fault_names = {
        str(row.get("name") or "")
        for row in fault_cases
        if row.get("passed") is True
    }
    for name in sorted(REQUIRED_FAULT_CASES - fault_names):
        missing.append("fault_case_missing:" + name)
    return {
        "station_id": station,
        "status": "PROMOTION_ELIGIBLE" if not missing else "NA",
        "registry_timezones": registry_timezones,
        "design_candidate_policy_count": len(station_policies),
        "observation_count": len(station_rows),
        "phase_observation_counts": phase_counts,
        "settled_attribution_count": settled,
        "price_coverage_count": price,
        "minimum_unique_events_per_phase": minimum_unique_events_per_phase,
        "missing_requirements": sorted(set(missing)),
    }


def build_from_paths(
    *,
    observations_path: Path,
    policies_path: Path,
    registry_path: Path,
    training_cutoffs_path: Path,
    fault_cases_path: Path,
    as_of_utc: str,
    evaluation_start_utc: str,
    evaluation_end_utc: str,
    minimum_unique_events_per_phase: int = DEFAULT_MIN_UNIQUE_EVENTS_PER_PHASE,
) -> dict[str, Any]:
    paths = {
        "observations": observations_path,
        "policies": policies_path,
        "registry": registry_path,
        "training_cutoffs": training_cutoffs_path,
        "fault_cases": fault_cases_path,
    }
    loaded: dict[str, Any] = {}
    input_gaps: list[str] = []
    for name, path in paths.items():
        value, gap = _load_optional(path)
        if gap:
            input_gaps.append(gap)
        else:
            loaded[name] = value
    try:
        observations = _sequence(
            loaded.get("observations", []),
            keys=("observations", "rows"),
            name="cash_phase_observations",
        )
        policies = _sequence(
            loaded.get("policies", []),
            keys=("cash_phase_policies", "policies"),
            name="cash_phase_policies",
        )
        registry_rows = _sequence(
            loaded.get("registry", []),
            keys=("rows", "registry_rows"),
            name="cash_phase_registry",
        )
        training_cutoffs = _mapping(
            loaded.get("training_cutoffs", {}),
            keys=("policy_training_cutoffs", "training_cutoffs"),
            name="cash_phase_training_cutoffs",
        )
        fault_cases = _sequence(
            loaded.get("fault_cases", []),
            keys=("fault_cases", "cases"),
            name="cash_phase_fault_cases",
        )
    except ContractError as exc:
        input_gaps.append("input_contract_invalid:" + str(exc))
        observations, policies, registry_rows, fault_cases = [], [], [], []
        training_cutoffs = {}

    expected_station_ids = sorted({
        str(row.get("station_id") or "")
        for row in registry_rows
        if str(row.get("station_id") or "")
    })
    rkpk = _rkpk_inventory(
        policies=policies,
        registry_rows=registry_rows,
        observations=observations,
        training_cutoffs=training_cutoffs,
        fault_cases=fault_cases,
        minimum_unique_events_per_phase=minimum_unique_events_per_phase,
    )
    evidence: dict[str, Any] | None = None
    if not input_gaps:
        try:
            evidence = build_cash_phase_oos_evidence(
                observations,
                policies=policies,
                registry_rows=registry_rows,
                policy_training_cutoffs=training_cutoffs,
                fault_cases=fault_cases,
                as_of_utc=as_of_utc,
                evaluation_start_utc=evaluation_start_utc,
                evaluation_end_utc=evaluation_end_utc,
                expected_station_ids=expected_station_ids,
                minimum_unique_events_per_phase=minimum_unique_events_per_phase,
                max_rows=MAX_ROWS,
            )
        except ContractError as exc:
            input_gaps.append("evidence_contract_blocked:" + str(exc))
    core = {
        "schema": EVIDENCE_BUNDLE_SCHEMA,
        "status": (
            "PROMOTION_ELIGIBLE"
            if evidence is not None
            and evidence.get("status") == "PROMOTION_ELIGIBLE"
            and not input_gaps
            else "NA"
        ),
        "as_of_utc": as_of_utc,
        "evaluation_window": {
            "start_utc_inclusive": evaluation_start_utc,
            "end_utc_exclusive": evaluation_end_utc,
        },
        "input_paths": {
            name: str(path.expanduser().resolve())
            for name, path in paths.items()
        },
        "input_gaps": sorted(input_gaps),
        "rkpk": rkpk,
        "evidence": evidence,
        "required_station_count": MAX_STATIONS,
        "design_candidate_mutated": False,
        "writes_authority": False,
        "writes_production_db": False,
        "calls_order_gateway": False,
        "live_order_authorized": False,
        "orders_submitted": 0,
    }
    return {**core, "content_sha256": content_sha256(core)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--observations", type=Path, default=DEFAULT_OBSERVATIONS)
    parser.add_argument("--policies", type=Path, default=DEFAULT_POLICIES)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument(
        "--training-cutoffs",
        type=Path,
        default=DEFAULT_TRAINING_CUTOFFS,
    )
    parser.add_argument("--fault-cases", type=Path, default=DEFAULT_FAULT_CASES)
    parser.add_argument("--as-of-utc", required=True)
    parser.add_argument("--evaluation-start-utc", required=True)
    parser.add_argument("--evaluation-end-utc", required=True)
    parser.add_argument(
        "--minimum-unique-events-per-phase",
        type=int,
        default=DEFAULT_MIN_UNIQUE_EVENTS_PER_PHASE,
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    receipt = build_from_paths(
        observations_path=args.observations,
        policies_path=args.policies,
        registry_path=args.registry,
        training_cutoffs_path=args.training_cutoffs,
        fault_cases_path=args.fault_cases,
        as_of_utc=args.as_of_utc,
        evaluation_start_utc=args.evaluation_start_utc,
        evaluation_end_utc=args.evaluation_end_utc,
        minimum_unique_events_per_phase=args.minimum_unique_events_per_phase,
    )
    write_bounded_json(args.output, receipt, limit=MAX_OUTPUT_BYTES)
    print(json.dumps({
        "schema": receipt["schema"],
        "status": receipt["status"],
        "rkpk_status": receipt["rkpk"]["status"],
        "rkpk_missing_requirements": receipt["rkpk"]["missing_requirements"],
        "content_sha256": receipt["content_sha256"],
        "output": str(args.output),
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
