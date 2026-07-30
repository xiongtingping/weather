#!/usr/bin/env python3
"""Run the read-only hard-NO dependency wall audit."""

from __future__ import annotations

import json
import sys
import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from reported_trajectory_no_dependency_audit import (  # noqa: E402
    audit_reported_trajectory_no_dependencies,
)


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write-receipt", type=Path)
    args = parser.parse_args()
    receipt = audit_reported_trajectory_no_dependencies(project_root=ROOT)
    identity = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    receipt["receipt_sha256"] = hashlib.sha256(
        json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if args.write_receipt is not None:
        output = args.write_receipt if args.write_receipt.is_absolute() else ROOT / args.write_receipt
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if receipt["isolated"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
