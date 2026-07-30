from __future__ import annotations

from datetime import datetime, timezone
import json

from daily_market_ssot_materializer import _days
from production_probability_v6_market_context_runtime import resolve_active_v6_market_context


def test_daily_market_ssot_days_are_current_and_next_local_day() -> None:
    now = datetime(2026, 7, 19, 16, 30, tzinfo=timezone.utc)
    assert _days(now) == ["2026-07-20", "2026-07-21"]


def test_date_indexed_market_context_precedes_stale_global_pointer(monkeypatch, tmp_path) -> None:
    activation_dir = tmp_path / "market_context_activation_by_date"
    activation_dir.mkdir()
    (activation_dir / "2026-07-20.json").write_text(
        json.dumps({"schema": "date-indexed-test"}), encoding="utf-8"
    )
    monkeypatch.setattr(
        "production_probability_v6_market_context_runtime.DATE_INDEX_ACTIVATION_DIR",
        activation_dir,
    )
    calls: list[str] = []

    def fake_load(*, activation_path, root, event_date):
        calls.append(str(activation_path))
        return {"ok": True, "state": "available", "event_date": event_date}

    monkeypatch.setattr(
        "production_probability_v6_market_context_runtime.load_active_v6_market_context",
        fake_load,
    )
    result = resolve_active_v6_market_context(
        activation_path=tmp_path / "stale-global.json",
        root=tmp_path,
        event_date="2026-07-20",
    )
    assert result["state"] == "available"
    assert calls == [str(activation_dir / "2026-07-20.json")]
