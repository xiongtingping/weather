#!/usr/bin/env python3
"""Build a machine-readable roadmap compliance matrix.

The roadmap's checkbox remains the declared project status.  This receipt
adds exact item text, dependency context, and the newest isolated evidence so
``PARTIAL``/``TODO``/``BLOCKED`` work is not hidden by aggregate test counts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.resource_bounds import load_bounded_json, write_bounded_json  # noqa: E402

ROADMAP = ROOT / "data/reports/polymarket_wallet_0x8fbd7cf5_20260721/doctrine/bayes_basket_development_roadmap_v1_6.md"
CHECK_RE = re.compile(
    r"^\s*-\s+\[([xX\- !])\]\s+\*\*(M\d+(?:-\d+[A-Z]?)?)\s+\[(P[012](?:-[A-Z])?(?:/[A-Z]+)?)\]\s*([^*]+)\*\*[:：](.*)$"
)
STATUS = {"x": "DONE", "-": "PARTIAL", "": "TODO", "!": "BLOCKED"}


def _load(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        value = load_bounded_json(path)
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def build_matrix(*, roadmap: Path = ROADMAP) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for line_no, line in enumerate(roadmap.read_text(encoding="utf-8").splitlines(), start=1):
        match = CHECK_RE.match(line)
        if not match:
            continue
        marker = match.group(1).strip()
        priority = match.group(3).strip()
        title = match.group(4).strip()
        description = match.group(5).strip()
        item_id = match.group(2)
        items.append({
            "id": item_id,
            "priority": priority,
            "declared_status": STATUS[marker.lower()],
            "title": title,
            "acceptance_and_dependencies": description,
            "roadmap_line": line_no,
        })
    receipts = {
        "M0-02": ROOT / "output/audits/bayes_basket_target_m0_closure_audit.json",
        "M0-03": ROOT / "output/audits/bayes_basket_target_current_et_20260726/m0_closure_audit.json",
        "M0-04": ROOT / "output/audits/bayes_basket_target_current_et_20260726/m0_closure_audit.json",
        "M0-05": ROOT / "output/audits/bayes_basket_target_current_ssot_v4_rehearsal.json",
        "M0-06": ROOT / "output/audits/bayes_basket_target_current_et_20260726/m0_closure_audit.json",
        "M1-01": ROOT / "output/audits/bayes_basket_target_document_authority.json",
        "M1-02": ROOT / "output/audits/bayes_basket_target_current_et_20260726/focused_test_receipt.json",
        "M1-03": ROOT / "output/audits/bayes_basket_target_current_et_20260726/focused_test_receipt.json",
        "M1-04": ROOT / "output/audits/bayes_basket_target_current_et_20260726/focused_test_receipt.json",
        "M1-05": ROOT / "output/audits/bayes_basket_target_current_et_20260726/focused_test_receipt.json",
        "M1-11": ROOT / "output/audits/bayes_basket_target_production_closure_audit.json",
        "M2-01": ROOT / "output/audits/bayes_basket_target_source_routes.json",
        "M2-02": ROOT / "output/audits/bayes_basket_target_official_ingress_routes.json",
        "M2-03": ROOT / "output/audits/bayes_basket_target_official_ingress_routes.json",
        "M2-08": ROOT / "output/audits/bayes_basket_target_taf_role_isolation.json",
        "M2-09": ROOT / "output/audits/bayes_basket_target_time_contract.json",
        "M6-08": ROOT / "output/audits/bayes_basket_target_hold_closure_audit.json",
        "M7-01": ROOT / "output/audits/reported_trajectory_no_dependency_audit.json",
        "M8-10": ROOT / "output/audits/bayes_basket_target_promotion_boundary_audit.json",
        "M8-09": ROOT / "output/audits/bayes_basket_target_performance.json",
        "M8-05": ROOT / "output/audits/bayes_basket_target_strict_asof_replay.json",
        "M8-06": ROOT / "output/audits/bayes_basket_target_strict_asof_replay.json",
        "M8-12": ROOT / "output/audits/station_policy_transfer_gate_m8_12.json",
        "M8-13": ROOT / "output/audits/cash_phase_promotion_gate_m8_13.json",
        "M9-01": ROOT / "output/audits/bayes_basket_target_production_closure_latest.json",
        "M9-02": ROOT / "output/audits/bayes_basket_target_production_closure_latest.json",
        "M9-03": ROOT / "output/audits/bayes_basket_target_production_closure_latest.json",
        "M8-01": ROOT / "output/audits/bayes_basket_target_current_et_20260726/focused_test_receipt.json",
        "M2-05": ROOT / "output/audits/bayes_basket_target_ingress_replay.json",
        "M2-06": ROOT / "output/audits/bayes_basket_target_ingress_replay.json",
        "M2-07": ROOT / "output/audits/bayes_basket_target_ingress_replay.json",
        "M3-04": ROOT / "output/audits/bayes_basket_target_ingress_replay.json",
        "M3-05": ROOT / "output/audits/bayes_basket_target_ingress_replay.json",
        "M3-06": ROOT / "output/audits/bayes_basket_target_ingress_replay.json",
        "M5-01": ROOT / "output/audits/bayes_basket_target_migration_rehearsal.json",
        "M5-02": ROOT / "output/audits/bayes_basket_target_migration_rehearsal.json",
        "M5-03": ROOT / "output/audits/bayes_basket_target_migration_rehearsal.json",
        "M5-04": ROOT / "output/audits/bayes_basket_target_migration_rehearsal.json",
        "M5-05": ROOT / "output/audits/bayes_basket_target_migration_rehearsal.json",
        "M5-06": ROOT / "output/audits/bayes_basket_target_migration_rehearsal.json",
        "M5-07": ROOT / "output/audits/bayes_basket_target_migration_rehearsal.json",
    }
    for item in items:
        path = receipts.get(item["id"])
        evidence = _load(path) if path else None
        item["isolated_evidence"] = {
            "path": str(path.relative_to(ROOT)) if path else None,
            "present": evidence is not None,
            "status": evidence.get("status") if evidence else None,
            "verified": bool(
                evidence and (
                    evidence.get("status") == "PASS"
                    or evidence.get("state") == "passed"
                    or evidence.get("isolated") is True
                    or evidence.get("hold_closure_verified") is True
                    or evidence.get("promotion_boundary_verified") is True
                )
            ),
            "boundary": evidence.get("boundary") if evidence else None,
        }
    counts = {status: sum(item["declared_status"] == status for item in items) for status in STATUS.values()}
    incomplete = [item["id"] for item in items if item["declared_status"] != "DONE"]
    identity = {
        "schema": "bayes_basket_target_roadmap_compliance_v1",
        "roadmap": str(roadmap),
        "roadmap_sha256": hashlib.sha256(roadmap.read_bytes()).hexdigest(),
        "item_count": len(items),
        "declared_status_counts": counts,
        "incomplete_item_ids": incomplete,
        "items": items,
        "boundary": {
            "matrix_only": True,
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
    parser.add_argument("--roadmap", type=Path, default=ROADMAP)
    parser.add_argument("--write-receipt", type=Path, default=ROOT / "output/audits/bayes_basket_target_roadmap_compliance.json")
    args = parser.parse_args()
    roadmap = args.roadmap if args.roadmap.is_absolute() else ROOT / args.roadmap
    receipt = build_matrix(roadmap=roadmap)
    output = args.write_receipt if args.write_receipt.is_absolute() else ROOT / args.write_receipt
    write_bounded_json(output, receipt)
    print(json.dumps({
        "schema": receipt["schema"],
        "item_count": receipt["item_count"],
        "declared_status_counts": receipt["declared_status_counts"],
        "incomplete_item_count": len(receipt["incomplete_item_ids"]),
        "output": str(output),
        "receipt_sha256": receipt["receipt_sha256"],
    }, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if receipt["item_count"] > 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
