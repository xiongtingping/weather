#!/usr/bin/env python3
"""Read-only final X/S source readiness audit for the v2 label bridge."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from resolution_final_source_readiness_runtime import build_resolution_final_source_readiness_audit
from scripts.v2_training_data_readiness_audit import build_resolution_label_registry_readiness


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "archive.db")
    parser.add_argument("--resolution-label-registry", type=Path)
    parser.add_argument("--min-history-days", type=int, default=365)
    args = parser.parse_args()
    registry = build_resolution_label_registry_readiness(
        registry_path=args.resolution_label_registry,
        min_history_days=max(1, int(args.min_history_days)),
    )
    report = build_resolution_final_source_readiness_audit(
        db_path=args.db,
        registry_readiness=registry,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
