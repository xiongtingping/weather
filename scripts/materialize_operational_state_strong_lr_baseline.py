#!/usr/bin/env python3
"""Materialize the approved state-aware operational strong-LR artifact."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from strong_observation_lr_operational_state_baseline import (  # noqa: E402
    build_operational_state_aware_strong_lr_baseline,
)


SCHEMA_VERSION = "materialize_operational_state_strong_lr_baseline_v1"
CONFIRMATION = "MATERIALIZE_OPERATIONAL_STATE_STRONG_LR_BASELINE_V2"
DEFAULT_OUTPUT = (
    ROOT
    / "data"
    / "runtime"
    / "strong_observation_lr"
    / "approved"
    / "strong_lr_operational_state_baseline_v2.json"
)


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
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()
    artifact = build_operational_state_aware_strong_lr_baseline()
    result = {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "state": "ready",
        "artifact_sha256": artifact["artifact_sha256"],
        "scoring_kind": artifact["scoring_kind"],
        "output": str(args.output.expanduser().resolve()),
        "writes_archive_db": False,
        "changes_activation": False,
        "places_orders": False,
    }
    if args.apply:
        if args.confirm != CONFIRMATION:
            result.update({"ok": False, "state": "blocked", "block_reasons": ["confirmation_required"]})
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            return 2
        _atomic_json(args.output.expanduser().resolve(), artifact)
        result["state"] = "materialized"
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
