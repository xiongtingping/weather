#!/usr/bin/env python3
"""Register and activate the neutral strong-LR baseline in isolated shadow."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from p10_shadow_runtime_schema import build_p10_shadow_runtime_schema_plan  # noqa: E402
from strong_observation_lr_operational_baseline import build_neutral_strong_lr_operational_baseline  # noqa: E402
from strong_observation_lr_runtime_registry import (  # noqa: E402
    ACTIVATION_ATTESTATION_SCHEMA_VERSION,
    activate_strong_lr_runtime_artifact,
    register_strong_lr_runtime_artifact,
)


CONFIRMATION = "ACTIVATE_NEUTRAL_STRONG_LR_BASELINE_SHADOW"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shadow-db", type=Path, default=ROOT / "data" / "shadow" / "p10_runtime.db")
    parser.add_argument("--target-identity", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()
    artifact = build_neutral_strong_lr_operational_baseline()
    at = _now()
    reference = "runtime://strong_lr_neutral_operational_baseline_v1"
    result = {
        "schema": "neutral_strong_lr_baseline_shadow_activation_v1",
        "ok": True,
        "state": "ready_to_activate",
        "target_identity": args.target_identity,
        "artifact_sha256": artifact["model_artifact_sha256"],
        "shadow_db": str(args.shadow_db.expanduser().resolve()),
        "boundary": {"shadow_only": True, "writes_posterior": False, "places_orders": False},
    }
    if not args.apply:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if args.confirm != CONFIRMATION:
        result.update({"ok": False, "state": "blocked", "block_reasons": ["confirmation_phrase_required"]})
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 2
    try:
        with sqlite3.connect(args.shadow_db.expanduser().resolve(), timeout=30) as conn:
            if build_p10_shadow_runtime_schema_plan(conn).get("state") != "ready":
                raise ValueError("p10_shadow_schema_not_ready")
            conn.execute("BEGIN IMMEDIATE")
            registered = register_strong_lr_runtime_artifact(
                conn,
                artifact=artifact,
                registered_at=at,
                registered_by="ssot_probability_mainline",
                registration_basis="approved_neutral_runtime_baseline",
                registration_ref=reference,
            )
            evidence = {
                "schema": ACTIVATION_ATTESTATION_SCHEMA_VERSION,
                "target_identity": args.target_identity,
                "artifact_sha256": artifact["model_artifact_sha256"],
                "activation_basis": "approved_runtime_baseline",
                "activation_ref": reference,
                "attested_at": at,
                "attested_by": "ssot_probability_mainline",
                "baseline_approval": {
                    "approval_kind": "operational_precalibration_baseline",
                    "approval_state": "approved",
                    "not_trained_or_oos_promoted": True,
                    "risk_and_scope_statement": "neutral shadow runtime scorer; zero feature LR and identity latent transition; hard observed-max condition remains composer-owned",
                },
            }
            activated = activate_strong_lr_runtime_artifact(
                conn,
                target_identity=args.target_identity,
                artifact_sha256=artifact["model_artifact_sha256"],
                activated_at=at,
                activated_by="ssot_probability_mainline",
                activation_basis="approved_runtime_baseline",
                activation_ref=reference,
                activation_evidence=evidence,
            )
            conn.commit()
        result.update({"state": "activated", "registered": registered, "activated": activated})
    except (sqlite3.Error, ValueError) as exc:
        result.update({"ok": False, "state": "blocked", "block_reasons": [str(exc)]})
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
