#!/usr/bin/env python3
"""Materialize an orderless operational-ingress patch attestation."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from production_probability_activation_contract import load_production_probability_activation  # noqa: E402
from production_probability_deployment_attestation import (  # noqa: E402
    CHANGE_SCOPE, DEFAULT_PATH, SCHEMA_VERSION, attestation_sha256,
    validate_deployment_attestation,
)
from production_probability_runtime_manifest import (  # noqa: E402
    current_retirement_receipt_sha256, current_runtime_manifest_sha256,
)


CONFIRMATION = "ATTEST_OPERATIONAL_PROBABILITY_INGRESS_PATCH"


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--activation", default=str(ROOT / "config/production_probability_runtime_activation.json"))
    parser.add_argument("--output", default=str(DEFAULT_PATH))
    parser.add_argument("--approval-id", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()

    activation = load_production_probability_activation(args.activation)
    runtime_sha = current_runtime_manifest_sha256(ROOT)
    retirement_sha = current_retirement_receipt_sha256(ROOT)
    value = {
        "schema": SCHEMA_VERSION, "state": "deployment_approved",
        "approval_id": str(args.approval_id),
        "approved_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "change_scope": CHANGE_SCOPE,
        "base_activation_contract_sha256": activation["activation_contract_sha256"],
        "runtime_manifest_sha256": runtime_sha,
        "retirement_receipt_sha256": retirement_sha,
        "probability_semantics_changed": False, "places_orders": False, "authorizes_clob": False,
    }
    value["attestation_sha256"] = attestation_sha256(value)
    validate_deployment_attestation(
        value, activation=activation, current_runtime_manifest_sha256=runtime_sha,
        current_retirement_receipt_sha256=retirement_sha,
    )
    state = "ready"
    if args.apply:
        if args.confirm != CONFIRMATION:
            print(json.dumps({"ok": False, "state": "apply_blocked", "blockers": ["confirmation_required"]}))
            return 2
        _atomic_json(Path(args.output).expanduser().resolve(), value)
        state = "written"
    print(json.dumps({
        "ok": True, "state": state, "attestation": value,
        "output": str(Path(args.output).expanduser().resolve()),
        "writes_archive_db": False, "changes_probability_semantics": False, "places_orders": False,
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
