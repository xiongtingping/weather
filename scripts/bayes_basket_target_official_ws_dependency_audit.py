#!/usr/bin/env python3
"""Run the read-only official CLOB WS dependency audit."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.dependency_audit import audit_official_ws_dependencies  # noqa: E402


def main() -> int:
    receipt = audit_official_ws_dependencies(project_root=ROOT)
    print(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if receipt["official_ws_only"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
