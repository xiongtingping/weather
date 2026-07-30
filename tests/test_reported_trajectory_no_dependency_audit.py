from __future__ import annotations

from pathlib import Path

from reported_trajectory_no_dependency_audit import audit_reported_trajectory_no_dependencies


def test_hard_no_lane_has_no_bayes_forecast_or_taf_dependency() -> None:
    receipt = audit_reported_trajectory_no_dependencies(project_root=Path(__file__).resolve().parents[1])
    assert receipt["isolated"] is True
    assert receipt["bayes_probability_consumed"] is False
    assert receipt["taf_consumed"] is False
    assert receipt["nwp_consumed"] is False
    assert receipt["regime_consumed"] is False
    assert receipt["forbidden_import_hits"] == []
    assert receipt["forbidden_call_hits"] == []


def test_hard_no_audit_detects_forbidden_import_and_call(tmp_path: Path) -> None:
    bad = tmp_path / "bad_no.py"
    bad.write_text(
        "from bayes_basket_target import ProbabilityEnvelope\n"
        "def read():\n"
        "    return fetch_amsc_taf_reports([])\n",
        encoding="utf-8",
    )
    receipt = audit_reported_trajectory_no_dependencies(project_root=tmp_path, roots=[bad])
    assert receipt["isolated"] is False
    assert receipt["forbidden_import_hits"][0]["module"] == "bayes_basket_target"
    assert receipt["forbidden_call_hits"][0]["call"] == "fetch_amsc_taf_reports"
