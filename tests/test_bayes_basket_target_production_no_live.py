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
            "lineage": {
                "canonical_observation_event_id": "obs-1",
                "canonical_observation_sha256": RAW_SHA,
                "canonical_observation_committed_at": NOW,
                "hard_bound_id": "bound-1",
                "source_lane": "amsc",
                "report_type": "METAR",
                "hard_excluded_buckets": "37",
            },
        }
    }


def test_p1_lineage_is_the_only_atomic_fact_source() -> None:
    facts = target_atomic_facts_from_cycle(_cycle())
    assert facts["observation"] == {
        "observation_id": "obs-1",
        "station_id": "ZUUU",
        "event_date": "2026-07-23",
        "source_lane": "AMSC",
        "report_type": "METAR",
        "raw_sha256": RAW_SHA,
        "canonical_committed_at": NOW,
    }
    assert facts["bound"]["bound_id"] == "bound-1"
    assert facts["bound"]["bound_json"]["probability_envelope_sha256"] == "b" * 64


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
