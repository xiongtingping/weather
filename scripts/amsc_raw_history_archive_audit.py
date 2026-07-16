#!/usr/bin/env python3
"""Read-only integrity audit for the append-only AMSC raw report archive."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from amsc_raw_history_archive import audit_amsc_raw_history_archive


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--archive-dir",
        default=str(ROOT / "data" / "amsc_raw_history"),
        help="raw archive root (default: %(default)s)",
    )
    parser.add_argument(
        "--require-nonempty",
        action="store_true",
        help="fail when no raw objects or manifests have been retained yet",
    )
    parser.add_argument(
        "--require-coverage",
        action="store_true",
        help="fail when no persisted daily coverage manifest has been retained yet",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = audit_amsc_raw_history_archive(args.archive_dir)
    if args.require_nonempty and (not result["object_count"] or not result["manifest_count"]):
        result = {
            **result,
            "ok": False,
            "issues": [*result["issues"], "raw_archive_empty"],
        }
    if args.require_coverage and not result.get("coverage_manifest_count"):
        result = {
            **result,
            "ok": False,
            "issues": [*result["issues"], "daily_coverage_manifest_missing"],
        }
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
