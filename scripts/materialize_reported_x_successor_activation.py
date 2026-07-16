#!/usr/bin/env python3
"""Build the frozen, orderless reported-X successor activation."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from production_probability_p0_runtime import build_production_p0_inputs  # noqa: E402
from production_probability_runtime_manifest import build_current_retirement_receipt  # noqa: E402
from production_probability_successor_activation_contract import (  # noqa: E402
    BAYES_FORMULA_ID,
    CHECKPOINT_SCHEMA,
    COMPOSER_SCHEMA,
    LATENT_TYPE,
    PROJECTION_SCHEMA,
    PUBLIC_SSOT_SCHEMA,
    SCHEMA_VERSION as ACTIVATION_SCHEMA,
    STRONG_UPDATE_SCHEMA,
    TARGET_TYPE,
    validate_successor_activation,
)
from production_probability_successor_runtime_manifest import (  # noqa: E402
    successor_runtime_manifest_sha256,
)


SCHEMA_VERSION = "materialize_reported_x_successor_activation_v1"
CONFIRMATION = "ACTIVATE_REPORTED_X_PROBABILITY_MAINLINE"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    temp.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    os.replace(temp, path)


def build_plan(*, event_date: str, decision_as_of: str) -> dict:
    inputs = build_production_p0_inputs(event_date=event_date, decision_as_of=decision_as_of)
    artifacts = dict(inputs.get("runtime_artifacts") or {})
    retirement = build_current_retirement_receipt(ROOT)
    blockers = list(inputs.get("block_reasons") or []) if inputs.get("ok") is not True else []
    if retirement.get("legacy_probability_writer_absent") is not True:
        blockers.append("legacy_probability_writer_not_retired")
    activation = {
        "schema": ACTIVATION_SCHEMA,
        "state": "production_enabled",
        "activation_effective_at": decision_as_of,
        "cutover_approval_id": "ADR-0002-reported-x-successor-2026-07-15",
        "bayes_formula_id": BAYES_FORMULA_ID,
        "composer_schema": COMPOSER_SCHEMA,
        "checkpoint_schema": CHECKPOINT_SCHEMA,
        "strong_update_schema": STRONG_UPDATE_SCHEMA,
        "public_ssot_schema": PUBLIC_SSOT_SCHEMA,
        "projection_schema": PROJECTION_SCHEMA,
        "latent_type": LATENT_TYPE,
        "target_type": TARGET_TYPE,
        "production_transaction_b_authorized": True,
        "unique_probability_mainline": True,
        "legacy_probability_writer_disabled": True,
        "no_order_validation_accepted": True,
        "speci_soft_evidence_only": True,
        "regular_metar_only_x_hard_bound": True,
        "bridge_consumes_reported_x_only": True,
        "public_ssot_exposes_reported_x_only": True,
        "prior_model_artifact_sha256": str(artifacts.get("prior_model_artifact_sha256") or ""),
        "weak_likelihood_artifact_sha256": str(artifacts.get("weak_likelihood_artifact_sha256") or ""),
        "strong_lr_artifact_sha256": str(artifacts.get("strong_lr_artifact_sha256") or ""),
        "runtime_manifest_sha256": successor_runtime_manifest_sha256(),
        "legacy_writer_retirement_receipt_sha256": hashlib.sha256(
            json.dumps(retirement, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "places_orders": False,
        "authorizes_clob": False,
    }
    if not blockers:
        validate_successor_activation(activation)
    return {
        "schema": SCHEMA_VERSION,
        "ok": not blockers,
        "state": "ready" if not blockers else "blocked",
        "blockers": blockers,
        "activation": activation,
        "event_date": event_date,
        "p0_inputs_ready": inputs.get("ok") is True,
        "places_orders": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-date", required=True)
    parser.add_argument("--decision-as-of", default="")
    parser.add_argument("--output", default=str(ROOT / "config/production_probability_runtime_activation.reported_x_candidate.json"))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--writer-quiescent", action="store_true")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()
    plan = build_plan(event_date=str(args.event_date)[:10], decision_as_of=str(args.decision_as_of or _now()))
    if args.apply:
        if args.confirm != CONFIRMATION or not args.writer_quiescent or plan.get("ok") is not True:
            plan["ok"] = False
            plan["state"] = "apply_blocked"
            plan.setdefault("blockers", []).append("confirmation_writer_quiescence_or_plan_required")
        else:
            output = Path(args.output).expanduser().resolve()
            _atomic_json(output, plan["activation"])
            plan["state"] = "activation_written"
            plan["activation_path"] = str(output)
    print(json.dumps(plan, ensure_ascii=False, sort_keys=True))
    return 0 if plan.get("ok") is True else 2


if __name__ == "__main__":
    raise SystemExit(main())
