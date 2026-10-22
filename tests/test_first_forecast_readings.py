"""First forecast against the truth (user 2026-10-22 「增加首次预报和真值的曲线对比（近30天）」)."""

from __future__ import annotations

import json
from pathlib import Path

from bayes_basket_target import first_forecast_readings as FF


def test_mean_on_the_ladder_closes_the_open_ends_one_width_out() -> None:
    labels = ["≤20°C", "21°C", "22°C", "≥23°C"]
    edges = [(19.5, 20.5), (20.5, 21.5), (21.5, 22.5), (22.5, 23.5)]
    assert FF._mean((labels, edges), {"21°C": 0.5, "22°C": 0.5}) == 21.5
    assert FF._mean((labels, edges), {"≤20°C": 1.0}) == 20.0
    assert FF._mean(None, {"21°C": 1.0}) is None


def test_first_history_row_is_the_earliest_for_the_day(tmp_path: Path, monkeypatch) -> None:
    d = tmp_path / "d0_history"
    d.mkdir()
    rows = [{"station": "ZUUU", "event_date": "2026-10-20", "as_of_utc": "2026-10-20T03:00:00Z", "centre_c": 25.0},
            {"station": "ZUUU", "event_date": "2026-10-20", "as_of_utc": "2026-10-20T01:00:00Z", "centre_c": 24.0},
            {"station": "ZUUU", "event_date": "2026-10-21", "as_of_utc": "2026-10-20T00:00:00Z", "centre_c": 9.0}]
    (d / "2026-10-20.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    monkeypatch.setattr(FF, "READINGS_DIR", tmp_path)
    assert FF._first_history("d0", "2026-10-20", "centre_c") == {"ZUUU": ("2026-10-20T01:00:00Z", 24.0)}
