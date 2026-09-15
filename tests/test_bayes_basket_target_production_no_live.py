from __future__ import annotations

import sqlite3

import pytest

from bayes_basket_target.production_io import target_atomic_facts_from_cycle
from bayes_basket_target.ssot import apply_production_ssot_schema


NOW = "2026-07-23T00:00:00Z"
RAW_SHA = "a" * 64


def _cycle(stage: str = "P1") -> dict:
    return {
        "probability_envelope": {
            "stage": stage,
            "station_id": "ZUUU",
            "event_date": "2026-07-23",
            "content_sha256": "b" * 64,
            "settlement_timezone": "Asia/Shanghai",
            "lineage": {
                "canonical_observation_event_id": "obs-1",
                "canonical_observation_sha256": RAW_SHA,
                "canonical_observation_committed_at": NOW,
                "truth_observation_revision_content_sha256": "C" * 64,
                "settlement_timezone": "Asia/Shanghai",
                "hard_bound_id": "bound-1",
                "source_lane": "awc",
                "report_type": "METAR",
                "hard_excluded_buckets": "37",
            },
        }
    }


def test_p1_lineage_is_the_only_atomic_fact_source() -> None:
    # The P1 lineage names the truth revision it roots on (PR12) rather than
    # carrying a METAR-shaped observation fact, and AMSC is a retired lane:
    # the facts are the revision identity, the bound, and whatever contract
    # the caller carried (none here), all read off the lineage.
    facts = target_atomic_facts_from_cycle(_cycle())
    assert set(facts) == {
        "awc_observation_contract",
        "truth_observation_revision_content_sha256",
        "bound",
    }
    assert facts["awc_observation_contract"] is None
    assert facts["truth_observation_revision_content_sha256"] == "c" * 64
    assert facts["bound"]["bound_id"] == "bound-1"
    assert facts["bound"]["bound_json"]["probability_envelope_sha256"] == "b" * 64


def test_a_retired_or_mismatched_lane_is_not_an_atomic_fact_source() -> None:
    retired = _cycle()
    retired["probability_envelope"]["lineage"]["source_lane"] = "amsc"
    with pytest.raises(Exception, match="lineage_source_invalid"):
        target_atomic_facts_from_cycle(retired)
    borrowed = _cycle()
    borrowed["probability_envelope"]["lineage"]["report_type"] = "ASOS_1MIN"
    with pytest.raises(Exception, match="lineage_source_invalid"):
        target_atomic_facts_from_cycle(borrowed)


@pytest.mark.parametrize(
    "mutator,reason",
    [
        (lambda value: value.update({"probability_envelope": {**value["probability_envelope"], "stage": "P0"}}), "require_p1"),
        (lambda value: value["probability_envelope"]["lineage"].pop("canonical_observation_committed_at"), "lineage_incomplete"),
    ],
)
def test_incomplete_atomic_lineage_fails_closed(mutator, reason: str) -> None:
    value = _cycle()
    mutator(value)
    with pytest.raises(Exception, match=reason):
        target_atomic_facts_from_cycle(value)


def test_production_ssot_ddl_allows_multiple_legs_per_decision() -> None:
    with sqlite3.connect(":memory:") as conn:
        result = apply_production_ssot_schema(conn)
        assert result["ok"] is True
        columns = {row[1] for row in conn.execute("PRAGMA table_info(basket_target_order_intents)")}
        assert "decision_id" in columns
        indexes = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name='basket_target_order_intent_decision_idx'"
        ).fetchone()
        assert indexes is not None


def test_production_ssot_migration_rejects_legacy_decision_table_collision() -> None:
    with sqlite3.connect(":memory:") as conn:
        conn.execute("CREATE TABLE auto_trade_decisions(id INTEGER PRIMARY KEY, will_order INTEGER)")
        with pytest.raises(RuntimeError, match="legacy_collision"):
            apply_production_ssot_schema(conn)
