from dynamic_top_yes_wave_exit_runtime import build_dynamic_top_yes_wave_exit_candidate


NOW = "2026-07-14T08:00:00.500Z"
ARTIFACT = "strategy-approved"


def _position(bucket: str = "33°C") -> dict:
    return {
        "management_scope": "dynamic_three_leg_weather",
        "strategy_leg": "top_yes_wave",
        "bucket": bucket,
        "side": "YES",
        "token_id": "token-33-yes",
        "position_lot_id": "lot-33-yes",
        "position_lot_version": "4",
        "position_size": 20.0,
        "entry_strategy_artifact_sha256": ARTIFACT,
        "frozen_take_profit_price": 0.90,
    }


def _quote(price: float = 0.90) -> dict:
    return {
        "market_snapshot_id": "market-current",
        "token_id": "token-33-yes",
        "requested_size": 20.0,
        "price_for_requested_size": price,
        "as_of": "2026-07-14T08:00:00.300Z",
        "expires_at": "2026-07-14T08:00:01.000Z",
    }


def _candidate(*, top: str = "33°C", position: dict | None = None, quote: dict | None = None) -> dict:
    return build_dynamic_top_yes_wave_exit_candidate(
        posterior_ssot_fact={
            "posterior_run_id": "post-current",
            "realization_state_id": "realization-current",
            "target_bucket": top,
        },
        position=position or _position(),
        executable_sell_quote=quote or _quote(),
        approved_strategy_artifact_sha256=ARTIFACT,
        decision_as_of=NOW,
    )


def test_only_current_reselected_top_yes_with_frozen_090_and_fresh_quote_is_candidate() -> None:
    candidate = _candidate()

    assert candidate["state"] == "candidate"
    assert candidate["reason_family"] == "market_alpha_take_profit"
    assert candidate["reason_code"] == "market_alpha_take_profit_top_yes_wave_completed"
    assert candidate["bucket_selection_dynamic"] is True
    assert candidate["take_profit_price_frozen_at_entry"] is True


def test_old_top_bucket_cannot_use_wave_exit_after_posterior_switch() -> None:
    candidate = _candidate(top="34°C", quote=_quote(0.99))

    assert candidate["state"] == "hold"
    assert "position_not_current_dynamic_top_yes" in candidate["reason_codes"]


def test_wrong_artifact_target_or_stale_quote_fails_closed() -> None:
    position = _position()
    position["frozen_take_profit_price"] = 0.91
    quote = _quote(0.99)
    quote["expires_at"] = "2026-07-14T08:00:00.400Z"
    candidate = _candidate(position=position, quote=quote)

    assert candidate["state"] == "hold"
    assert "position_frozen_take_profit_price_invalid" in candidate["reason_codes"]
    assert "executable_sell_quote_not_current" in candidate["reason_codes"]
