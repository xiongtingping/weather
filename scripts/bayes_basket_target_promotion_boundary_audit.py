#!/usr/bin/env python3
"""Audit that research promotion gates cannot disable active runtime."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.promotion_boundary import build_promotion_boundary_decision  # noqa: E402


RUNTIME_FILES = (
    ROOT / "bayes_basket_target" / "production.py",
    ROOT / "bayes_basket_target" / "runtime.py",
    ROOT / "bayes_basket_target" / "policy.py",
    ROOT / "bayes_basket_target" / "cash_phase.py",
)
RESEARCH_MODULES = frozenset({
    "station_policy_candidate",
    "audit_cash_phase_promotion_gate",
    "audit_station_policy_transfer_gate",
})


def _runtime_import_hits() -> list[dict[str, object]]:
    hits: list[dict[str, object]] = []
    for path in RUNTIME_FILES:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            modules: list[str] = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules = [node.module or ""]
            for module in modules:
                root = module.rsplit(".", 1)[-1]
                if root in RESEARCH_MODULES:
                    hits.append({"file": str(path.relative_to(ROOT)), "line": int(node.lineno), "module": module})
    return sorted(hits, key=lambda item: (str(item["file"]), int(item["line"]), str(item["module"])))


def build_receipt() -> dict[str, object]:
    import_hits = _runtime_import_hits()
    boundary = build_promotion_boundary_decision(
        active_artifact={
            "artifact_id": "active-v1",
            "content_sha256": "a" * 64,
            "state": "ACTIVE",
            "research_gate_dependency": False,
        },
        research_gate={
            "checks": {"oos": False, "calibration": False},
            "promotion_requested": True,
        },
        candidate_artifact={"artifact_id": "candidate-v2"},
    )
    checks = {
        "runtime_does_not_import_research_gate": not import_hits,
        "failed_research_gate_blocks_candidate": boundary["candidate_promotion_allowed"] is False,
        "failed_research_gate_does_not_disable_active": boundary["active_runtime_allowed"] is True,
        "active_runtime_research_independent": boundary["active_runtime_reads_research_gate"] is False,
        "live_order_authorized_false": boundary["live_order_authorized"] is False,
    }
    identity = {
        "schema": "bayes_basket_target_promotion_boundary_audit_v1",
        "runtime_files": [str(path.relative_to(ROOT)) for path in RUNTIME_FILES],
        "research_import_hits": import_hits,
        "boundary_decision": boundary,
        "checks": checks,
        "promotion_boundary_verified": all(checks.values()),
        "boundary": {
            "read_only": True,
            "writes_production_db": False,
            "calls_order_gateway": False,
            "places_orders": False,
        },
    }
    return {**identity, "receipt_sha256": hashlib.sha256(
        json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write-receipt", type=Path)
    args = parser.parse_args()
    receipt = build_receipt()
    if args.write_receipt is not None:
        output = args.write_receipt if args.write_receipt.is_absolute() else ROOT / args.write_receipt
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if receipt["promotion_boundary_verified"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
