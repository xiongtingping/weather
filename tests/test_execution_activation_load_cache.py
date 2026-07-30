from __future__ import annotations

import json
import os
from pathlib import Path

import dynamic_three_leg_production_activation as dynamic
import market_alpha_take_profit_activation as alpha


def _replace(path: Path, value: dict) -> None:
    temporary = path.with_suffix(".next")
    temporary.write_text(json.dumps(value), encoding="utf-8")
    os.replace(temporary, path)


def test_dynamic_default_pointer_identity_caches_and_atomic_replace_invalidates(
    tmp_path: Path, monkeypatch,
) -> None:
    path = tmp_path / "dynamic.json"
    value = dynamic.materialize_dynamic_three_leg_activation({
        "schema": dynamic.SCHEMA_VERSION,
        "state": "enabled",
        "strategy_artifact_sha256": "strategy",
        "cutover_artifact_sha256": "cutover",
        "risk_reservation_write_authorized": True,
        "live_decision_write_authorized": True,
        "live_submit_authorized": True,
    })
    _replace(path, value)
    calls: list[str] = []
    monkeypatch.setattr(dynamic, "DEFAULT_PATH", path)
    monkeypatch.setattr(dynamic, "_ACTIVE_CACHE", None)
    monkeypatch.setattr(
        "dynamic_three_leg_release_runtime.validate_dynamic_three_leg_release_binding",
        lambda **_kwargs: calls.append("validated"),
    )
    assert dynamic.load_dynamic_three_leg_activation(path)["state"] == "enabled"
    assert dynamic.load_dynamic_three_leg_activation(path)["state"] == "enabled"
    assert calls == ["validated"]
    _replace(path, value)
    assert dynamic.load_dynamic_three_leg_activation(path)["state"] == "enabled"
    assert calls == ["validated", "validated"]


def test_market_alpha_default_pointer_identity_caches_and_atomic_replace_invalidates(
    tmp_path: Path, monkeypatch,
) -> None:
    path = tmp_path / "alpha.json"
    value = alpha.materialize_market_alpha_take_profit_activation({
        "schema": alpha.SCHEMA_VERSION,
        "state": "enabled",
        "strategy_artifact_sha256": "strategy",
        "cutover_artifact_sha256": "cutover",
        "live_decision_write_authorized": True,
        "live_submit_authorized": True,
        "reason_family": "market_alpha_take_profit",
        "reason_code": "market_alpha_take_profit_top_yes_wave_completed",
        "frozen_take_profit_price": 0.90,
    })
    _replace(path, value)
    calls: list[str] = []
    monkeypatch.setattr(alpha, "DEFAULT_PATH", path)
    monkeypatch.setattr(alpha, "_ACTIVE_CACHE", None)
    monkeypatch.setattr(
        "dynamic_three_leg_release_runtime.validate_dynamic_three_leg_release_binding",
        lambda **_kwargs: calls.append("validated"),
    )
    assert alpha.load_market_alpha_take_profit_activation(path)["state"] == "enabled"
    assert alpha.load_market_alpha_take_profit_activation(path)["state"] == "enabled"
    assert calls == ["validated"]
    _replace(path, value)
    assert alpha.load_market_alpha_take_profit_activation(path)["state"] == "enabled"
    assert calls == ["validated", "validated"]
