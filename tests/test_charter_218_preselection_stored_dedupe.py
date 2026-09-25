"""Charter 218: the stored preselection leaves out what its own row repeats.

``plan.lineage`` repeated 97% of ``probability_envelope.lineage`` byte for
byte and ``plan.delta_v2_audit`` was the outer audit verbatim, about 30 KB of
a ~113 KB row at 24-28k rows a day.  The row now names where each omitted
piece lives; the in-memory plan, ``plan_sha256`` and every reader of stored
rows are unchanged.
"""

from __future__ import annotations

import copy
import json
import sqlite3

import pytest

from bayes_basket_target import ContractError
from bayes_basket_target.p0_preselection import (
    STORED_AUDIT_KEY,
    STORED_LINEAGE_KEY,
    UNPERSISTED_MARKET_SNAPSHOT_KEY,
    _decision_semantic_projection,
    commit_p0_preselection,
    expanded_stored_preselection,
    stored_preselection_payload,
)
from bayes_basket_target.production_worker import station_edge_from_plan
from bayes_basket_target.ssot import apply_production_ssot_schema


def _canon(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _audit() -> dict[str, object]:
    return {
        "delta_basket_score_v2": 0.0123,
        "policy_artifact_id": "policy-a",
        "policy_artifact_sha256": "d" * 64,
        "fill_distribution_artifact_sha256": "e" * 64,
        "window_gate_counts": {"open": 3, "closed": 1},
    }


def _envelope_lineage() -> dict[str, object]:
    return {
        "taf_nwp_live_compatibility_json": json.dumps({"rows": ["x" * 90] * 160}),
        "native_forward_availability_evidence_json": json.dumps({"systems": 6}),
        "p0m_taf_likelihood_json": json.dumps({"lambda": 0.8}),
        "bucket_intervals_c_json": "[[17.2,17.8],[17.8,18.9]]",
        "flag": True,
        "posterior_version": "p0-v14",
    }


def _value() -> dict[str, object]:
    envelope_lineage = _envelope_lineage()
    plan_lineage = {
        **copy.deepcopy(envelope_lineage),
        # Same key, different bytes: 1 == True in Python, not in the row.
        "flag": 1,
        # A key only the plan carries.
        "plan_only_note": "seated on 64-65F",
    }
    compiled = {
        "status": "ready",
        "reason_codes": [],
        "stage": "P0",
        "delta_v2_audit": _audit(),
        "probability_envelope": {
            "checkpoint_id": "p0-klga-0926",
            "stage": "P0",
            "probabilities": [0.23, 0.21, 0.16],
            "bucket_ordinals": [5, 4, 3],
            "lineage": envelope_lineage,
        },
        "plan": {
            "state": "READY",
            "center_bucket": "64-65°F",
            "direction": "NEUTRAL",
            "final_support": ["62-63°F", "64-65°F"],
            "candidate_support": ["62-63°F", "64-65°F", "66-67°F"],
            "target_solution": {"target_shares_by_bucket": {"64-65°F": 12}},
            "targets": [
                {"bucket": "64-65°F", "delta_shares": 12, "candidate_price": 0.21}
            ],
            "total_delta_notional_usdc": 2.52,
            "basket_support_selection": {
                "seated": {"window_probabilities": {"64-65°F": 0.23}}
            },
            "delta_v2_audit": _audit(),
            "lineage": plan_lineage,
        },
    }
    return {
        "schema": "bayes_basket_target_p0_preselection_v1",
        "preselection_id": "p0-preselection-218",
        "station_id": "KLGA",
        "event_date": "2026-09-26",
        "checkpoint_id": "p0-klga-0926",
        "probability_stage": "P0",
        "decision_at_utc": "2026-09-25T01:00:00Z",
        "quote_observed_at_utc": "2026-09-25T01:00:01Z",
        "quote_manifest_sha256": "a" * 64,
        "policy_activation_id": "candidate-activation",
        "policy_authority_scope": "PRODUCTION_NO_LIVE_CANDIDATE",
        "compiled_plan": compiled,
        "plan_sha256": "b" * 64,
        "state": "PRELIMINARY_NO_LIVE",
        "final_authority": "P1",
        "capital_authorized": False,
        "intents_written": 0,
        "reservations_written": 0,
        "live_submit_authorized": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        UNPERSISTED_MARKET_SNAPSHOT_KEY: {"never": "stored"},
    }


def _persisted(value: dict[str, object]) -> dict[str, object]:
    return {k: v for k, v in value.items() if k != UNPERSISTED_MARKET_SNAPSHOT_KEY}


def test_stored_row_leaves_out_shared_lineage_and_the_inner_audit() -> None:
    stored = stored_preselection_payload(_value())
    plan = stored["compiled_plan"]["plan"]
    assert "delta_v2_audit" not in plan
    assert plan[STORED_AUDIT_KEY] == "compiled_plan.delta_v2_audit"
    marker = plan[STORED_LINEAGE_KEY]
    assert marker["shared_with"] == "compiled_plan.probability_envelope.lineage"
    assert "taf_nwp_live_compatibility_json" in marker["shared_keys"]
    # Differing bytes and plan-only keys stay in the row.
    assert plan["lineage"] == {"flag": 1, "plan_only_note": "seated on 64-65F"}
    assert "flag" not in marker["shared_keys"]
    assert UNPERSISTED_MARKET_SNAPSHOT_KEY not in stored
    assert len(_canon(stored)) < 0.7 * len(_canon(_persisted(_value())))


def test_expanding_the_stored_row_gives_back_the_plan_byte_for_byte() -> None:
    value = _value()
    restored = expanded_stored_preselection(json.loads(_canon(stored_preselection_payload(value))))
    assert _canon(restored) == _canon(_persisted(value))


def test_the_in_memory_preselection_is_not_touched() -> None:
    value = _value()
    before = _canon(_persisted(value))
    stored_preselection_payload(value)
    assert _canon(_persisted(value)) == before
    assert "delta_v2_audit" in value["compiled_plan"]["plan"]
    assert "taf_nwp_live_compatibility_json" in value["compiled_plan"]["plan"]["lineage"]


def test_identity_and_stored_row_readers_are_unchanged() -> None:
    value = _value()
    full = value["compiled_plan"]
    stored = stored_preselection_payload(value)["compiled_plan"]
    # plan_sha256 hashes this projection; a stored-row verifier recomputes it.
    assert _canon(_decision_semantic_projection(stored)) == _canon(
        _decision_semantic_projection(full)
    )
    # The resident's one reader of stored rows (production_worker edge evidence).
    assert station_edge_from_plan(
        stored, station_id="KLGA", minimum_clip_usdc=1.0
    ) == station_edge_from_plan(full, station_id="KLGA", minimum_clip_usdc=1.0)


def test_commit_writes_the_compact_row() -> None:
    value = _value()
    with sqlite3.connect(":memory:") as conn:
        apply_production_ssot_schema(conn)
        result = commit_p0_preselection(
            conn, value, created_at_utc="2026-09-25T01:00:02Z"
        )
        assert result["state"] == "COMMITTED"
        (raw,) = conn.execute(
            "SELECT plan_json FROM basket_p0_preselection_plan"
        ).fetchone()
    row = json.loads(raw)
    assert STORED_LINEAGE_KEY in row["compiled_plan"]["plan"]
    assert _canon(expanded_stored_preselection(row)) == _canon(_persisted(value))


def test_a_differing_inner_audit_is_kept() -> None:
    value = _value()
    value["compiled_plan"]["plan"]["delta_v2_audit"] = {**_audit(), "delta_basket_score_v2": 0.5}
    plan = stored_preselection_payload(value)["compiled_plan"]["plan"]
    assert plan["delta_v2_audit"]["delta_basket_score_v2"] == 0.5
    assert STORED_AUDIT_KEY not in plan
    assert _canon(expanded_stored_preselection(stored_preselection_payload(value))) == _canon(
        _persisted(value)
    )


def test_a_blocked_compile_and_a_pre_218_row_are_stored_and_read_as_they_are() -> None:
    blocked = {**_value(), "compiled_plan": {"status": "blocked", "plan": None}}
    assert _canon(stored_preselection_payload(blocked)) == _canon(_persisted(blocked))
    # A row the 09-21 rebuild stripped carries no marker and expands to itself.
    old = _persisted(_value())
    del old["compiled_plan"]["plan"]["delta_v2_audit"]
    assert _canon(expanded_stored_preselection(old)) == _canon(old)


def test_the_stored_form_keys_are_reserved() -> None:
    value = _value()
    value["compiled_plan"]["plan"][STORED_LINEAGE_KEY] = {"schema": "x"}
    with pytest.raises(ContractError, match="stored_form_key_reserved"):
        stored_preselection_payload(value)


def test_a_marker_whose_source_is_gone_fails_closed() -> None:
    stored = stored_preselection_payload(_value())
    del stored["compiled_plan"]["probability_envelope"]["lineage"]
    with pytest.raises(ContractError, match="stored_lineage_source_missing"):
        expanded_stored_preselection(stored)
