from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "materialize_ctp_latest_facts.py"


def load_materializer_module():
    spec = importlib.util.spec_from_file_location("materialize_ctp_latest_facts", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_ctp_materializer_clears_legacy_interval_fields_and_rebases_observed_aliases() -> None:
    module = load_materializer_module()
    row = {
        "outcome": "35°C or higher",
        "q_bayes_posterior": 1.1258069058642035e-10,
        "q_bayes_lcb": 1.1258069058642035e-10,
        "q_bayes_ucb": 1.1258069058642035e-10,
        "q_quote_lcb": 1.1258069058642035e-10,
        "q_quote_ucb": 1.1258069058642035e-10,
        "q_posterior_observed_constraints_applied": True,
        "q_posterior_observed_constraints_hard_zero": False,
        "q_posterior_observed_constraints_post_prob": 1.1258069058642035e-10,
        "q_posterior_observed_constraints_reason_codes": ["stale_reason"],
        "posterior_settlement_candidate_bucket_hard_zero": False,
    }

    module._sync_distribution_row_probability(
        row,
        label="35°C or higher",
        prob=1.2522957079290159e-12,
        posterior={"version": "continuous_temperature_posterior_v1"},
    )

    assert row["q_bayes_posterior"] == 1.2522957079290159e-12
    assert row["q_temperature_bucket_posterior"] == 1.2522957079290159e-12
    assert row["q_posterior_observed_constraints_post_prob"] == 1.2522957079290159e-12
    assert row["q_posterior_observed_constraints_hard_zero"] is False
    assert row["posterior_settlement_candidate_bucket_hard_zero"] is False
    assert row["q_posterior_observed_constraints_reason_codes"] == []
    assert row["legacy_interval_probability_fields_cleared_by_ctp_materializer"] is True
    assert "q_bayes_lcb" not in row
    assert "q_bayes_ucb" not in row
    assert "q_quote_lcb" not in row
    assert "q_quote_ucb" not in row


def test_ctp_materializer_preserves_hard_zero_observed_constraint_for_broken_lower_bucket() -> None:
    module = load_materializer_module()
    row = {
        "outcome": "33°C",
        "q_bayes_posterior": 0.0,
        "q_bayes_lcb": 0.0,
        "q_bayes_ucb": 0.0,
        "q_posterior_observed_constraints_applied": True,
        "q_posterior_observed_constraints_hard_zero": True,
        "q_posterior_observed_constraints_post_prob": 0.0,
        "q_posterior_observed_constraints_reason_codes": [
            "observed_today_max_above_bucket",
            "hard_source_lower_bound_constraint",
        ],
        "posterior_settlement_candidate_bucket_hard_zero": True,
        "posterior_settlement_candidate_bucket_hard_zero_reasons": [
            "observed_today_max_above_bucket",
            "hard_source_lower_bound_constraint",
        ],
    }

    module._sync_distribution_row_probability(
        row,
        label="33°C",
        prob=0.0,
        posterior={"version": "continuous_temperature_posterior_v1"},
    )

    assert row["q_bayes_posterior"] == 0.0
    assert row["q_temperature_bucket_posterior"] == 0.0
    assert row["q_posterior_observed_constraints_post_prob"] == 0.0
    assert row["q_posterior_observed_constraints_hard_zero"] is True
    assert row["posterior_settlement_candidate_bucket_hard_zero"] is True
    assert row["q_posterior_observed_constraints_reason_codes"] == [
        "observed_today_max_above_bucket",
        "hard_source_lower_bound_constraint",
    ]
    assert "q_bayes_lcb" not in row
    assert "q_bayes_ucb" not in row


def test_ctp_materializer_source_trace_has_source_observation_id() -> None:
    module = load_materializer_module()
    row = {
        "id": 42,
        "event_date": "2026-07-03",
        "fact_as_of": "2026-07-03T08:00:00Z",
    }
    payload = {
        "station_id": "ZUUU",
        "fact_as_of": "2026-07-03T08:00:00Z",
    }

    trace = module._materializer_source_trace(
        row=row,
        payload=payload,
        parent_raw_json='{"fact": 42}',
        created_at="2026-07-03T09:00:00Z",
    )

    assert trace["source_observation_ids"]
    assert trace["source_observations"][0]["source_observation_id"] == trace["source_observation_ids"][0]
    assert trace["source_latency_trace"][0]["source_observation_id"] == trace["source_observation_ids"][0]
    assert trace["source_observations"][0]["source_event_id"] == trace["source_event_ids"][0]
