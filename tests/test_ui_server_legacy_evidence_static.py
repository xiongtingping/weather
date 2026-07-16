from pathlib import Path


UI_SERVER = Path(__file__).resolve().parents[1] / "ui" / "server.py"


def _function_block(name: str) -> str:
    text = UI_SERVER.read_text(encoding="utf-8")
    marker = f"def {name}("
    start = text.index(marker)
    next_function = text.find("\ndef ", start + len(marker))
    if next_function == -1:
        return text[start:]
    return text[start:next_function]


def test_most_likely_history_items_do_not_emit_legacy_evidence_fields():
    block = _function_block("query_most_likely_history")
    item_start = block.index("items.append(")
    item_end = block.index("ordered_bucket_series", item_start)
    item_block = block[item_start:item_end]

    assert "_compact_bayes_chain_for_bucket_series" in block
    for legacy_key in (
        "q_bayes_model_evidence",
        "q_bayes_wu_forecast_evidence",
        "q_bayes_taf_peak_evidence",
        "q_bayes_observed_nowcast_evidence",
        "q_bayes_temperature_trajectory_evidence",
        "q_bayes_independent_physics_residual_evidence",
        "q_bayes_climo_regime_evidence",
        "q_bayes_post_cool_rebound_regime_prior_evidence",
        "q_bayes_post_cool_rebound_intraday_likelihood_evidence",
    ):
        assert legacy_key not in item_block


def test_temperature_timeline_payload_does_not_emit_legacy_evidence_fields():
    for function_name in (
        "_ui_temperature_timeline_signature",
        "_ui_temperature_timeline_sample",
        "_ui_temperature_timeline_points_from_sql",
        "_build_temperature_trajectory_timeline_for_ui_snapshot",
    ):
        block = _function_block(function_name)
        assert "q_bayes_temperature_trajectory_evidence" not in block
        assert "q_bayes_meta.temperature_trajectory_evidence" not in block
        assert "temperature_trajectory_evidence" not in block
        assert "q_bayes_physical_likelihood_evidence" not in block
        assert "physical_likelihood_evidence" not in block
