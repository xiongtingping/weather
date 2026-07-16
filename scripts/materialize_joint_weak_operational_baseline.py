#!/usr/bin/env python3
"""Materialise the immutable approved joint-weak operational baseline."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from joint_weak_baseline_artifact_runtime import (  # noqa: E402
    DEFAULT_ARTIFACT_PATH,
    build_joint_weak_operational_baseline_artifact,
)
from joint_forecast_weak_likelihood_runtime import validate_joint_forecast_weak_artifact  # noqa: E402


CONFIRMATION = "MATERIALIZE_APPROVED_JOINT_WEAK_OPERATIONAL_BASELINE"


def _write_immutable(path: Path, payload: dict) -> str:
    rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if path.exists():
        if path.read_text(encoding="utf-8") == rendered:
            return "idempotent"
        raise RuntimeError("joint_weak_operational_baseline_identity_collision")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    try:
        temporary.write_text(rendered, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return "materialized"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_ARTIFACT_PATH)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args(argv)
    artifact = build_joint_weak_operational_baseline_artifact()
    contract = validate_joint_forecast_weak_artifact(artifact)
    result = {
        "schema": "joint_weak_operational_baseline_materialization_v1",
        "ok": contract.get("ok") is True,
        "state": "ready_to_materialize" if contract.get("ok") is True else "blocked",
        "artifact_path": str(args.output.expanduser().resolve()),
        "artifact_sha256": artifact.get("artifact_sha256"),
        "artifact_contract": contract,
        "boundary": {
            "creates_immutable_artifact_only": True,
            "writes_active_pointer": False,
            "writes_archive_db": False,
            "writes_posterior": False,
            "places_orders": False,
        },
    }
    if args.apply:
        if args.confirm != CONFIRMATION:
            result.update({"ok": False, "state": "blocked", "block_reasons": ["confirmation_phrase_required"]})
        elif contract.get("ok") is not True:
            result.update({"ok": False, "state": "blocked", "block_reasons": list(contract.get("issues") or [])})
        else:
            try:
                result["materialization"] = _write_immutable(args.output.expanduser().resolve(), artifact)
                result["state"] = "materialized"
            except RuntimeError as exc:
                result.update({"ok": False, "state": "blocked", "block_reasons": [str(exc)]})
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("ok") is True else 2


if __name__ == "__main__":
    raise SystemExit(main())
