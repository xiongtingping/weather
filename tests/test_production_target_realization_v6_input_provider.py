import sqlite3

import production_target_realization_v6_input_provider as provider
from dynamic_three_leg_v6_schema_contract import initialize_dynamic_three_leg_v6_schema
from sequential_bayes_schema_contract import TABLE_DDL


def test_v6_provider_consumes_only_committed_realization_and_freezes_full_bundle(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.execute(TABLE_DDL["posterior_execution_intents"])
    initialize_dynamic_three_leg_v6_schema(conn)
    realization = {
        "realization_state_id": "r1",
        "market_bucket_probabilities": {"34°C": 0.15, "35°C": 0.55, "36°C": 0.30},
        "settlement_exclusion_certificates": {},
        "strong_physical_evidence": {
            "canonical_observation_event_id": "event1",
            "bucket_evidence": {"34°C": {"soft_unreachability_diagnostic": True}},
        },
        "peak_timing_state": {
            "entry_peak_at": "2026-07-16T08:30:00Z",
            "future_peak_probability": 0.4,
        },
    }
    monkeypatch.setattr(provider, "load_latest_realization_state_for_outbox", lambda *_args, **_kwargs: {"ok": True, "realization_state": realization})

    def tokens(_conn, *, slug, event_date, buckets):
        return ([{"bucket": bucket, "yes_token_id": f"y{index}", "no_token_id": f"n{index}"} for index, bucket in enumerate(buckets)], [])

    monkeypatch.setattr(provider, "_market_token_rows", tokens)

    def exchange(request):
        quotes = {}
        metadata = {}
        for row in request["buckets"]:
            for token in (row["yes_token_id"], row["no_token_id"]):
                quotes[token] = {"asset_id": token, "best_bid": 0.2, "best_ask": 0.3, "timestamp": "2026-07-16T08:00:00Z"}
                metadata[token] = {"tick_size": "0.001", "neg_risk": True}
        return {
            "schema": "clob_dynamic_three_leg_snapshot_v1", "ok": True,
            "evidence_only": True, "authorizes_order": False,
            "request_id": request["request_id"], "observed_at": "2026-07-16T08:00:00Z",
            "amount_decimals": 6, "top_quotes_by_token": quotes, "order_books_by_token": {},
            "token_metadata_by_token": metadata,
            "conditional_balance_allowance_by_token": {},
            "collateral_balance_allowance": {"balance": "100000000", "allowance": "100000000"},
        }

    result = provider.freeze_target_realization_v6_inputs(
        conn,
        outbox={
            "slug": "zuuu", "event_date": "2026-07-16", "posterior_run_id": "p1",
            "fact_snapshot_id": "f1", "correlation_id": "c1",
            "observation_to_order_hard_deadline_at": "2026-07-16T08:00:03Z",
        },
        preliminary_as_of="2026-07-16T08:00:00Z",
        exchange_snapshot_fetcher=exchange,
        production_activation={"strategy_artifact_sha256": "a" * 64},
    )
    assert result["ok"] is True
    assert result["v6_plan"]["main_yes_bucket"] == "35°C"
    assert result["v6_plan"]["no_mode"] == "trajectory_no"
    assert len(result["intents"]) == 3
    assert all('"token_tick_size":"0.001"' in row["intent_json"] for row in result["intents"])
    assert result["places_orders"] is False

