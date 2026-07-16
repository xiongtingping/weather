#!/usr/bin/env python3
"""Audit that one current probability document authority supersedes old epochs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from probability_document_authority_audit import audit_probability_document_authority  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--summary", action="store_true")
    args = parser.parse_args(argv)
    result = audit_probability_document_authority(args.root)
    if args.summary:
        result = {
            key: result[key]
            for key in ("schema", "ok", "state", "blockers", "checks", "read_only", "places_orders")
        }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
