#!/usr/bin/env python3
"""Read-only X/S bridge-pair coverage audit."""
from __future__ import annotations
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from resolution_bridge_pair_coverage_runtime import build_resolution_bridge_pair_coverage_audit  # noqa: E402

if __name__ == "__main__":
    print(json.dumps(build_resolution_bridge_pair_coverage_audit(db_path=ROOT / "data" / "shadow" / "resolution_final_labels.db"), ensure_ascii=False, indent=2, sort_keys=True))
