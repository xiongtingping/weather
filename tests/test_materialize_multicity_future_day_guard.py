from __future__ import annotations

import pytest

from production_multicity_posterior_runtime import materialize_multicity_production_p0s
from scripts.materialize_multicity_production_posterior import (
    _event_day_preflight,
    _normalize_event_dates,
)


AS_OF = "2026-07-24T15:00:00Z"


def test_materializer_requires_explicit_dates_and_deduplicates() -> None:
    assert _normalize_event_dates(None) == []
    assert _normalize_event_dates(["2026-07-24", "", "2026-07-24"]) == ["2026-07-24"]


def test_script_preflight_rejects_future_et_day() -> None:
    reason, blocked = _event_day_preflight(["2026-07-25"], as_of_utc=AS_OF)

    assert reason == "future_event_day_not_allowed"
    assert blocked == ["2026-07-25"]


def test_runtime_rejects_future_et_day_before_opening_database(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "production_multicity_posterior_runtime.validate_v6_activation",
        lambda activation: {},
    )

    with pytest.raises(ValueError, match="future_event_day_not_allowed"):
        materialize_multicity_production_p0s(
            db_path="/definitely/not/opened/archive.db",
            activation={},
            event_dates=["2026-07-25"],
            decision_as_of=AS_OF,
            state_model_artifact_path="/definitely/not/read/state.json",
            reporting_operator_artifact_path="/definitely/not/read/operator.json",
            weak_artifact_path="/definitely/not/read/weak.json",
        )
