from __future__ import annotations

import csv
import json
from pathlib import Path

from scripts.bayes_basket_target_roadmap_compliance import build_matrix


def test_roadmap_compliance_matrix_has_119_items() -> None:
    matrix = build_matrix()
    assert matrix["item_count"] == 119
    assert matrix["declared_status_counts"] == {
        "DONE": 53,
        "PARTIAL": 58,
        "TODO": 8,
        "BLOCKED": 0,
    }
    assert matrix["boundary"]["writes_production_db"] is False


def test_roadmap_derived_checklists_match_declared_statuses() -> None:
    root = Path(__file__).resolve().parents[1]
    expected = {item["id"]: item["declared_status"] for item in build_matrix()["items"]}
    roadmap_json = json.loads(
        (root / "data/reports/polymarket_wallet_0x8fbd7cf5_20260721/doctrine/bayes_basket_development_roadmap_v1_6.json").read_text()
    )
    actual_json = {item["id"]: item["status"] for item in roadmap_json["checklist"]}
    with (root / "data/reports/polymarket_wallet_0x8fbd7cf5_20260721/derived/bayes_basket_development_checklist_v1_6.csv").open(newline="") as handle:
        actual_csv = {row.get("id", row.get("\ufeffid")): row["status"] for row in csv.DictReader(handle)}
    assert actual_json == expected
    assert actual_csv == expected


def test_current_m0_and_m1_evidence_are_not_mapped_to_stale_receipts() -> None:
    matrix = build_matrix()
    items = {item["id"]: item for item in matrix["items"]}

    assert items["M0-05"]["isolated_evidence"]["path"] == (
        "output/audits/bayes_basket_target_current_ssot_v4_rehearsal.json"
    )
    assert items["M0-05"]["isolated_evidence"]["verified"] is True
    assert items["M1-11"]["isolated_evidence"]["path"] == (
        "output/audits/bayes_basket_target_production_closure_audit.json"
    )
    assert items["M1-11"]["isolated_evidence"]["status"] == "PASS"
    for item_id in ("M9-01", "M9-02", "M9-03"):
        assert items[item_id]["isolated_evidence"]["path"] == (
            "output/audits/"
            "bayes_basket_target_production_closure_latest.json"
        )
        assert items[item_id]["isolated_evidence"]["status"] == "PASS"
