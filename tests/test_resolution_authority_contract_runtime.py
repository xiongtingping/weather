from __future__ import annotations

import json
from pathlib import Path

from resolution_authority_contract_runtime import load_resolution_authority_contract, validate_resolution_authority_contract


ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "config/resolution_authorities/zuuu_amsc_wunderground_daily_high_v4.json"


def test_accepted_zuuu_authority_is_fixed_but_not_live() -> None:
    _, report = load_resolution_authority_contract(CONTRACT)
    assert report["ok"] is True
    assert report["authority_fixed"] is True
    assert report["final_label_auto_ready"] is False
    assert report["posterior_or_execution_authorized"] is False


def test_contract_rejects_bridge_or_source_semantic_drift() -> None:
    value = json.loads(CONTRACT.read_text(encoding="utf-8"))
    value["physical_x"]["report_types"] = ["METAR", "SPECI"]
    value["x_to_s_to_b_bridge"]["closed"] = True
    report = validate_resolution_authority_contract(value)
    assert report["ok"] is False
    assert "physical_x_must_use_amsc_metar_only" in report["blockers"]
    assert "x_to_s_to_b_bridge_must_not_be_claimed_closed" in report["blockers"]


def test_contract_rejects_capture_url_that_drops_station_identity() -> None:
    value = json.loads(CONTRACT.read_text(encoding="utf-8"))
    value["settlement_s"]["capture_url_prefix"] = "https://www.wunderground.com/history/daily/cn/chengdu/date/"
    report = validate_resolution_authority_contract(value)
    assert report["ok"] is False
    assert "settlement_s_capture_url_prefix_invalid" in report["blockers"]
