#!/usr/bin/env python3
"""Read-only audit of the isolated final-label accumulation registry."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from resolution_final_label_shadow_registry_runtime import build_resolution_final_label_shadow_registry_audit


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "shadow" / "resolution_final_labels.db")
    args = parser.parse_args()
    report = build_resolution_final_label_shadow_registry_audit(db_path=args.db)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report.get("ok") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
