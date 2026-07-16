#!/usr/bin/env python3
"""Freeze the non-active v6 production candidate activation contract."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from physical_state_bayes_v6.artifact_contracts import validate_observation_artifact, validate_transition_artifact
from production_probability_v6_activation_contract import (
    CHECKPOINT_SCHEMA,
    COMPOSER_SCHEMA,
    FORMULA_ID,
    PUBLIC_SSOT_SCHEMA,
    SCHEMA_VERSION,
    STRONG_UPDATE_SCHEMA,
    activation_sha256,
    validate_v6_activation,
)
from production_probability_v6_runtime_manifest import build_v6_runtime_manifest


def build(root: Path) -> dict[str, object]:
    current_path = root / "config/production_probability_runtime_activation.json"
    current = json.loads(current_path.read_text(encoding="utf-8"))
    transition = validate_transition_artifact(
        json.loads((root / "config/physical_state_bayes_v6_transition_artifact.json").read_text(encoding="utf-8"))
    )
    observation = validate_observation_artifact(
        json.loads((root / "config/physical_state_bayes_v6_observation_artifact.json").read_text(encoding="utf-8"))
    )
    if transition.get("production_runtime_eligible") is not True or observation.get("production_runtime_eligible") is not True:
        raise ValueError("v6_candidate_activation_artifacts_not_production_eligible")
    manifest = build_v6_runtime_manifest(root)
    value = {
        "schema": SCHEMA_VERSION,
        "state": "production_enabled",
        "bayes_formula_id": FORMULA_ID,
        "composer_schema": COMPOSER_SCHEMA,
        "checkpoint_schema": CHECKPOINT_SCHEMA,
        "strong_update_schema": STRONG_UPDATE_SCHEMA,
        "public_ssot_schema": PUBLIC_SSOT_SCHEMA,
        "unique_probability_mainline": True,
        "legacy_probability_writer_disabled": True,
        "v5_fallback_forbidden": True,
        "pre_event_predict_does_not_consume_current_observation": True,
        "no_post_emission_state_transition": True,
        "nwp_hourly_curve_is_transition_control_only": True,
        "runtime_manifest_sha256": manifest["runtime_manifest_sha256"],
        "transition_artifact_sha256": transition["artifact_sha256"],
        "observation_artifact_sha256": observation["artifact_sha256"],
        "prior_model_artifact_sha256": str(current.get("prior_model_artifact_sha256") or ""),
        "weak_likelihood_artifact_sha256": str(current.get("weak_likelihood_artifact_sha256") or ""),
        "production_transaction_b_authorized": True,
        "places_orders": False,
        "authorizes_clob": False,
        "activation_requires_atomic_install": True,
        "supersedes_probability_epoch_schema_on_cutover": str(current.get("schema") or ""),
    }
    value["activation_contract_sha256"] = activation_sha256(value)
    return validate_v6_activation(value)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--root",
        type=Path,
        default=ROOT,
        help="Repository root to freeze; used by the isolated pre-maintenance candidate builder.",
    )
    parser.add_argument(
        "--output",
        default="",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Explicitly persist the candidate; default is a read-only preview.",
    )
    args = parser.parse_args()
    root = args.root.expanduser().resolve()
    value = build(root)
    output = (
        Path(args.output).expanduser().resolve()
        if args.output
        else root / "config/production_probability_runtime_activation.v6_candidate.json"
    )
    if args.write:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "ok": True,
                "state": (
                    "v6_candidate_activation_frozen_not_active"
                    if args.write
                    else "v6_candidate_activation_preview_not_written"
                ),
                "output": str(output),
                "activation_contract_sha256": value["activation_contract_sha256"],
                "activates_production": False,
                "writes_candidate_file": bool(args.write),
                "places_orders": False,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
