from scripts.production_dynamic_three_leg_strategy_contract_audit import run_audit


def test_production_dynamic_three_leg_strategy_contract_is_behaviorally_locked() -> None:
    result = run_audit()
    assert result["ok"] is True
    assert result["blockers"] == []
    assert all(result["checks"].values())
    assert result["limits_usdc"] == {
        "top_yes_wave": 20.0,
        "second_yes": 4.99,
        "strongly_excluded_no": 100.0,
    }
    assert result["places_orders"] is False
    assert result["writes_archive_db"] is False
