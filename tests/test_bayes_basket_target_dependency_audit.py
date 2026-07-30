from __future__ import annotations

from pathlib import Path

from bayes_basket_target.dependency_audit import audit_official_ws_dependencies


def test_target_market_lane_is_official_ws_only() -> None:
    receipt = audit_official_ws_dependencies(project_root=Path(__file__).resolve().parents[1])
    assert receipt["official_ws_only"] is True
    assert receipt["rest_reads"] is False
    assert receipt["historical_tick_reads"] is False
    assert receipt["authorizes_order"] is False
    assert receipt["network_import_hits"] == []
    assert receipt["historical_tick_hits"] == []
    assert receipt["forbidden_market_call_hits"] == []
    assert "9.05" not in (
        Path(__file__).resolve().parents[1] / "bayes_basket_target" / "runtime.py"
    ).read_text(encoding="utf-8")


def test_audit_detects_rest_and_tick_reader_in_a_candidate_module(tmp_path: Path) -> None:
    bad = tmp_path / "bad_market_reader.py"
    bad.write_text(
        "import requests\n"
        "def read():\n"
        "    return 'event_market_ticks'\n"
        "",
        encoding="utf-8",
    )
    receipt = audit_official_ws_dependencies(project_root=tmp_path, roots=[bad])
    assert receipt["official_ws_only"] is False
    assert receipt["network_import_hits"][0]["module"] == "requests"
    assert receipt["historical_tick_hits"][0]["token"] == "event_market_ticks"
