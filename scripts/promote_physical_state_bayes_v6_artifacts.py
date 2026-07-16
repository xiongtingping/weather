#!/usr/bin/env python3
"""Promote OOS-accepted v6 candidates to immutable production-replay artifacts."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from physical_state_bayes_v6.artifact_contracts import (
    artifact_sha256,
    validate_observation_artifact,
    validate_transition_artifact,
)


SCHEMA_VERSION = "physical_state_bayes_v6_artifact_promotion_v1"
DEFAULT_CANDIDATES = ROOT / "output/audits/physical_state_bayes_v6_artifact_candidates"


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError(f"v6_promotion_json_invalid:{path.name}")
    return dict(value)


def promote(candidate_dir: Path, config_dir: Path) -> dict[str, Any]:
    transition = validate_transition_artifact(_load(candidate_dir / "transition_artifact.candidate.json"))
    observation = validate_observation_artifact(_load(candidate_dir / "observation_artifact.candidate.json"))
    evaluation = _load(candidate_dir / "oos_evaluation.json")
    if (
        evaluation.get("ok") is not True
        or evaluation.get("state") != "accepted_for_canonical_replay"
        or evaluation.get("strict_holdout_not_used_for_training") is not True
        or evaluation.get("places_orders") is not False
        or evaluation.get("transition_artifact_sha256") != transition.get("artifact_sha256")
        or evaluation.get("observation_artifact_sha256") != observation.get("artifact_sha256")
    ):
        raise ValueError("v6_artifact_promotion_evidence_rejected")
    evidence_sha = str(evaluation.get("evaluation_sha256") or "")
    if len(evidence_sha) != 64:
        raise ValueError("v6_artifact_promotion_evidence_sha_invalid")
    promoted = []
    for artifact, validator, filename in (
        (transition, validate_transition_artifact, "physical_state_bayes_v6_transition_artifact.json"),
        (observation, validate_observation_artifact, "physical_state_bayes_v6_observation_artifact.json"),
    ):
        artifact.pop("artifact_sha256", None)
        artifact.update(
            {
                "state": "approved",
                "production_runtime_eligible": True,
                "shadow_only": False,
                "promotion_evidence_sha256": evidence_sha,
                "promotion_scope": "canonical_replay_then_controlled_production_cutover",
            }
        )
        artifact["artifact_sha256"] = artifact_sha256(artifact)
        validated = validator(artifact)
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / filename).write_text(
            json.dumps(validated, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        promoted.append({"path": str((config_dir / filename).resolve()), "artifact_sha256": validated["artifact_sha256"]})
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "state": "production_replay_artifacts_promoted",
        "promotion_evidence_sha256": evidence_sha,
        "artifacts": promoted,
        "activates_production": False,
        "places_orders": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate-dir", default=str(DEFAULT_CANDIDATES))
    parser.add_argument("--config-dir", default=str(ROOT / "config"))
    args = parser.parse_args()
    result = promote(Path(args.candidate_dir).resolve(), Path(args.config_dir).resolve())
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
