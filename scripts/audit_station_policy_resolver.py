#!/usr/bin/env python3
"""Run the station-policy promotion/resolution fault matrix offline.

This audit deliberately promotes in-memory copies only.  It never ingests the
candidate bundle, writes SQLite, calls a gateway, or changes an activation
file.  The candidate bundle itself must remain fully unpromoted.
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import ContractError, content_sha256  # noqa: E402
from bayes_basket_target.policy import (  # noqa: E402
    GlobalBasketPriorArtifact,
    PolicyArtifact,
    StationBasketPolicyActivation,
    resolve_station_policy,
)
from bayes_basket_target.station_policy_candidate import (  # noqa: E402
    assert_candidate_bundle_boundary,
)


DEFAULT_BUNDLE = ROOT / "output/audits/station_policy_candidate_bundle_v1.json"
DEFAULT_OUTPUT = ROOT / "output/audits/station_policy_resolver_fault_matrix.json"
DECISION_AT = "2026-07-23T12:00:00Z"


def _promote(factory: Any, raw: dict[str, Any]) -> Any:
    values = {key: value for key, value in raw.items() if key not in {"schema", "content_sha256"}}
    values["promoted"] = True
    return factory.create(**values)


def _objects(bundle: dict[str, Any], station_id: str) -> tuple[Any, dict[str, PolicyArtifact], StationBasketPolicyActivation]:
    prior = _promote(GlobalBasketPriorArtifact, bundle["global_prior"])
    artifacts = {
        str(row["artifact_id"]): _promote(PolicyArtifact, row)
        for row in bundle["artifacts"]
        if row.get("station_id") == station_id
    }
    activation_raw = next(row for row in bundle["activations"] if row["station_id"] == station_id)
    activation = _promote(StationBasketPolicyActivation, activation_raw)
    return prior, artifacts, activation


def _resolve(
    *,
    prior: GlobalBasketPriorArtifact,
    artifacts: dict[str, PolicyArtifact],
    activation: StationBasketPolicyActivation,
    station_id: str,
    **overrides: Any,
) -> Any:
    values = {
        "activations": [activation],
        "global_priors": {prior.global_prior_id: prior},
        "artifacts": artifacts,
        "station_id": station_id,
        "stage": "P0-M",
        "season_bin": "summer",
        "maturity_bin": "d1",
        "liquidity_bin": "liquid",
        "strategy_version": "target-v1",
        "decision_at_utc": DECISION_AT,
    }
    values.update(overrides)
    return resolve_station_policy(**values)


def _expect_error(call: Callable[[], Any], code: str) -> dict[str, Any]:
    try:
        call()
    except ContractError as exc:
        return {"status": "PASS", "expected": code, "actual": str(exc)}
    return {"status": "FAIL", "expected": code, "actual": "NO_ERROR"}


def build_audit(bundle_path: Path = DEFAULT_BUNDLE) -> dict[str, Any]:
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    assert_candidate_bundle_boundary(bundle)
    station_id = str(bundle["activations"][0]["station_id"])
    prior, artifacts, activation = _objects(bundle, station_id)

    rows: dict[str, dict[str, Any]] = {}
    candidate_prior = GlobalBasketPriorArtifact.from_mapping(bundle["global_prior"])
    candidate_artifacts = {
        str(row["artifact_id"]): PolicyArtifact.from_mapping(row)
        for row in bundle["artifacts"]
        if row.get("station_id") == station_id
    }
    candidate_activation = StationBasketPolicyActivation.from_mapping(
        next(row for row in bundle["activations"] if row["station_id"] == station_id)
    )
    rows["candidate_bundle_default_off"] = _expect_error(
        lambda: _resolve(
            prior=candidate_prior,
            artifacts=candidate_artifacts,
            activation=candidate_activation,
            station_id=station_id,
        ),
        "FAIL_CLOSED_NO_STATION_POLICY",
    )

    resolved = _resolve(prior=prior, artifacts=artifacts, activation=activation, station_id=station_id)
    rows["exact_promoted_activation"] = {
        "status": "PASS" if resolved.fallback_reason == "exact_station_artifacts" else "FAIL",
        "fallback_reason": resolved.fallback_reason,
        "policy_sha256": resolved.policy_sha256,
    }

    expired = StationBasketPolicyActivation.create(
        **{
            **activation.to_dict(),
            "valid_from_utc": "2026-07-01T00:00:00Z",
            "valid_until_utc": "2026-07-23T11:00:00Z",
            "promoted": True,
        }
    )
    rows["expired_activation"] = _expect_error(
        lambda: _resolve(prior=prior, artifacts=artifacts, activation=expired, station_id=station_id),
        "FAIL_CLOSED_NO_STATION_POLICY",
    )

    wrong_scope = StationBasketPolicyActivation.create(
        **{**activation.to_dict(), "season_bin": "winter", "promoted": True}
    )
    rows["activation_scope_mismatch"] = _expect_error(
        lambda: _resolve(prior=prior, artifacts=artifacts, activation=wrong_scope, station_id=station_id),
        "FAIL_CLOSED_NO_STATION_POLICY",
    )

    missing = dict(artifacts)
    missing.pop(activation.target_artifact_id or "", None)
    rows["missing_target_artifact"] = _expect_error(
        lambda: _resolve(prior=prior, artifacts=missing, activation=activation, station_id=station_id),
        "FAIL_CLOSED_NO_STATION_POLICY",
    )

    unpromoted = dict(artifacts)
    target = unpromoted[activation.target_artifact_id or ""]
    unpromoted[target.artifact_id] = PolicyArtifact.create(
        **{**target.to_dict(), "promoted": False}
    )
    rows["unpromoted_target_artifact"] = _expect_error(
        lambda: _resolve(prior=prior, artifacts=unpromoted, activation=activation, station_id=station_id),
        "FAIL_CLOSED_NO_STATION_POLICY",
    )

    fallback_activation = StationBasketPolicyActivation.create(
        **{
            **activation.to_dict(),
            "target_artifact_id": None,
            "fallback_families": ["target"],
            "promoted": True,
        }
    )
    fallback = _resolve(
        prior=prior,
        artifacts=artifacts,
        activation=fallback_activation,
        station_id=station_id,
    )
    rows["explicit_promoted_global_fallback"] = {
        "status": "PASS" if fallback.fallback_reason == "target" else "FAIL",
        "fallback_reason": fallback.fallback_reason,
        "target_artifact_id": fallback.target_artifact_id,
    }

    rows["neighbor_station_no_borrow"] = _expect_error(
        lambda: _resolve(prior=prior, artifacts=artifacts, activation=activation, station_id="ZBAA"),
        "FAIL_CLOSED_NO_STATION_POLICY",
    )

    conflict = StationBasketPolicyActivation.create(
        **{**activation.to_dict(), "activation_id": f"{activation.activation_id}-conflict", "promoted": True}
    )
    rows["same_generation_conflict"] = _expect_error(
        lambda: resolve_station_policy(
            activations=[activation, conflict],
            global_priors={prior.global_prior_id: prior},
            artifacts=artifacts,
            station_id=station_id,
            stage="P0-M",
            season_bin="summer",
            maturity_bin="d1",
            liquidity_bin="liquid",
            strategy_version="target-v1",
            decision_at_utc=DECISION_AT,
        ),
        "ambiguous_station_policy_activation",
    )

    statuses = [row["status"] for row in rows.values()]
    identity = {
        "schema": "station_policy_resolver_fault_matrix_v1",
        "candidate_bundle": str(bundle_path),
        "candidate_bundle_sha256": bundle["bundle_sha256"],
        "station_id": station_id,
        "decision_at_utc": DECISION_AT,
        "case_count": len(rows),
        "cases": rows,
        "all_cases_passed": all(status == "PASS" for status in statuses),
        "candidate_bundle_remains_unpromoted": bundle["promotion_permitted"] is False
        and bundle["live_order_authorized"] is False
        and bundle["orders_submitted"] == 0,
        "promotion_simulation": "in_memory_only",
        "production_mutation": False,
        "writes_production_db": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }
    return {**identity, "receipt_sha256": content_sha256(identity)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, default=DEFAULT_BUNDLE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    receipt = build_audit(args.bundle)
    output = args.output if args.output.is_absolute() else ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({
        "schema": receipt["schema"],
        "receipt_sha256": receipt["receipt_sha256"],
        "case_count": receipt["case_count"],
        "all_cases_passed": receipt["all_cases_passed"],
        "candidate_bundle_remains_unpromoted": receipt["candidate_bundle_remains_unpromoted"],
        "production_mutation": receipt["production_mutation"],
        "output": str(output),
    }, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if receipt["all_cases_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

