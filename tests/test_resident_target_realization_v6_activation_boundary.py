import resident_dynamic_three_leg_execution_loop as runtime


def test_v6_requested_with_disabled_activation_fails_closed_without_v5_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("PM_TARGET_REALIZATION_V6_PRODUCTION_ENABLED", "1")
    monkeypatch.setenv("PM_TARGET_REALIZATION_V6_LIVE_SUBMIT_ENABLED", "1")
    monkeypatch.setenv("PM_LIVE_PAUSE_FILE", str(tmp_path / "not-paused"))
    monkeypatch.setattr(
        runtime,
        "load_target_realization_v6_activation",
        lambda: {"state": "disabled", "v5_fallback_forbidden": True},
    )
    result = runtime.run_resident_dynamic_three_leg_execution_once(db_path=tmp_path / "missing.sqlite")
    assert result["state"] == "blocked"
    assert result["reason"] == "target_realization_v6_activation_not_enabled_no_v5_fallback"
    assert result["called_gateway"] is False
