from __future__ import annotations

import hashlib

from forecast_weak_context_capture_runtime import build_forecast_weak_context_capture


def _row(*, regime: str = "", fetched_at: str = "2026-07-12T06:00:00Z") -> dict:
    return {
        "source_id": "gfs",
        "source_event_id": "gfs-revision-1",
        "source_class": "forecast_weak_source",
        "event_date": "2026-07-12",
        "fetched_at": fetched_at,
        "forecast_peak_c": 32.0,
        "raw_sha256": hashlib.sha256(b"gfs").hexdigest(),
        "payload": {
            "evidence_tier": "A_live_observed_transport",
            "response_headers_observed": True,
            **({"forecast_weather_regime": regime} if regime else {}),
        },
    }


def test_context_is_forecast_only_and_uses_explicit_unknown_regime() -> None:
    result = build_forecast_weak_context_capture(
        target_identity="target-20260712",
        event_date="2026-07-12",
        decision_as_of="2026-07-12T06:30:00Z",
        strict_forecast_observations=[_row()],
    )

    assert result["ok"] is True
    assert result["forecast_context"] == {
        "season": "summer",
        "weather_regime": "unknown_forecast_regime",
        "lead_bucket": "d0_1200_1459",
    }
    assert result["boundary"]["d0_observation_not_consumed"] is True
    assert result["context_raw_sha256"]


def test_context_rejects_unmapped_regime_and_never_uses_future_revision() -> None:
    unnormalised = build_forecast_weak_context_capture(
        target_identity="target-20260712",
        event_date="2026-07-12",
        decision_as_of="2026-07-12T06:30:00Z",
        strict_forecast_observations=[_row(regime="clearish")],
    )
    assert unnormalised["state"] == "blocked"
    assert unnormalised["block_reasons"] == ["forecast_context_weather_regime_not_normalized"]

    future = build_forecast_weak_context_capture(
        target_identity="target-20260712",
        event_date="2026-07-12",
        decision_as_of="2026-07-12T06:30:00Z",
        strict_forecast_observations=[_row(fetched_at="2026-07-12T06:31:00Z")],
    )
    assert future["state"] == "blocked"
    assert future["block_reasons"] == ["forecast_context_no_tier_a_forecast_visible_at_cutoff"]


def test_context_supports_station_day_identity_without_a_market_target() -> None:
    result = build_forecast_weak_context_capture(
        event_date="2026-07-12",
        decision_as_of="2026-07-12T06:30:00Z",
        strict_forecast_observations=[_row()],
        station_id="ZUUU",
    )

    assert result["ok"] is True
    assert result["identity_kind"] == "station_day"
    assert result["training_identity"] == "station_day:ZUUU:2026-07-12"
    assert "target_identity" not in result
    assert result["boundary"]["station_day_training_identity_without_market_target"] is True
