#!/usr/bin/env python3
"""Materialize the immutable neutral strong-LR runtime baseline."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from strong_observation_lr_operational_baseline import build_neutral_strong_lr_operational_baseline  # noqa: E402


CONFIRMATION = "MATERIALIZE_NEUTRAL_STRONG_LR_OPERATIONAL_BASELINE"
DEFAULT_OUTPUT = ROOT / "data" / "runtime" / "strong_observation_lr" / "approved" / "strong_lr_neutral_operational_baseline_v1.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()
    artifact = build_neutral_strong_lr_operational_baseline()
    target = args.output.expanduser().resolve()
    rendered = json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    result = {
        "schema": "neutral_strong_lr_operational_baseline_materialization_v1",
        "ok": True,
        "state": "ready_to_materialize",
        "artifact_sha256": artifact["model_artifact_sha256"],
        "artifact_path": str(target),
        "boundary": {"activates_artifact": False, "writes_posterior": False, "places_orders": False},
    }
    if args.apply:
        if args.confirm != CONFIRMATION:
            result.update({"ok": False, "state": "blocked", "block_reasons": ["confirmation_phrase_required"]})
        elif target.exists() and target.read_text(encoding="utf-8") != rendered:
            result.update({"ok": False, "state": "blocked", "block_reasons": ["neutral_strong_lr_artifact_identity_collision"]})
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                temporary = target.with_name(f".{target.name}.tmp.{os.getpid()}")
                try:
                    temporary.write_text(rendered, encoding="utf-8")
                    os.replace(temporary, target)
                finally:
                    temporary.unlink(missing_ok=True)
                result["materialization"] = "materialized"
            else:
                result["materialization"] = "idempotent"
            result["state"] = "materialized"
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
