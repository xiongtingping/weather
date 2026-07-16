from __future__ import annotations

from pathlib import Path

import pytest

from scripts import audit_zuuu_ecmwf_high_ventilation_deterministic_v313 as audit


def test_extracts_exact_wind_only_gate_fields(tmp_path: Path) -> None:
    script = tmp_path / "regime.py"
    script.write_text(
        "def _high_basin_ventilation(row):\n"
        "    return row.get('wind850') >= 8 or row.get('wind700') >= 10 or row.get('shear') >= 8\n",
        encoding="utf-8",
    )
    assert audit._function_get_fields(script, "_high_basin_ventilation") == (
        "shear",
        "wind700",
        "wind850",
    )


def test_decline_evidence_uses_any_observed_decline() -> None:
    assert audit._decline_evidence({"drop_from_observed_max_c": 0.1}) is True
    assert audit._decline_evidence({"reports_below_observed_max": 1}) is True
    assert audit._decline_evidence({"drop_from_observed_max_c": 0.0, "reports_below_observed_max": 0}) is False


def test_group_summary_uses_global_day_weights() -> None:
    rows = [
        {
            "event_date_local": "2025-01-01",
            "global_day_weight": 0.25,
            "actual_continue_numeric": 0.0,
            "continue_probability_delta": 0.10,
            "brier_delta": 0.02,
            "bucket_nll_delta": -0.1,
        },
        {
            "event_date_local": "2025-01-02",
            "global_day_weight": 0.75,
            "actual_continue_numeric": 1.0,
            "continue_probability_delta": 0.20,
            "brier_delta": -0.01,
            "bucket_nll_delta": -0.2,
        },
    ]
    summary = audit._group_summary(rows)
    assert summary["actual_continue_rate"] == pytest.approx(0.75)
    assert summary["brier_delta_contribution_to_total"] == pytest.approx(-0.0025)
    assert summary["brier_delta_mean_within_group"] == pytest.approx(-0.0025)
