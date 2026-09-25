"""Charter 220: repeated large blocks of a stored preselection live once.

The fleet-wide beta/gamma table and the embedded envelope lineage repeat row
after row (2 and 453 distinct values in 15,497 full-speed rows); they go to
``basket_p0_preselection_blob`` keyed by the sha256 of their canonical JSON
and the row names the digest.  The in-memory plan, ``plan_sha256`` and every
reader of stored rows are unchanged, and the row expands back byte for byte.
"""

from __future__ import annotations

import copy
import json
import sqlite3

import pytest

from bayes_basket_target import ContractError
from bayes_basket_target.p0_preselection import (
    STORED_BLOBS_KEY,
    UNPERSISTED_MARKET_SNAPSHOT_KEY,
    _decision_semantic_projection,
    commit_p0_preselection,
    expanded_stored_preselection,
    split_preselection_blobs,
    stored_preselection_payload,
)
from bayes_basket_target.production_worker import station_edge_from_plan
from bayes_basket_target.ssot import apply_production_ssot_schema


def _canon(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _beta_tables() -> dict[str, object]:
    row = {
        "beta": 0.546673,
        "beta_lcb": -0.150929,
        "beta_ucb": 1.201183,
        "accumulating": False,
        "joint_draws": [[0.160436, 0.495495, -0.085616]] * 200,
        "lead_bands": {"12-24h": {"accumulating": True, "beta": None, "day_count": 0}},
        "sha256": "7" * 64,
    }
    return {"P0": row, "P0-M": {**row, "accumulating": True}, "P1": {**row, "beta": None}}


def _value(*, preselection_id: str, quote: str, target_shares: int) -> dict[str, object]:
    lineage = {
        "taf_nwp_live_compatibility_json": json.dumps({"rows": ["x" * 90] * 160}),
        "native_forward_availability_evidence_json": json.dumps({"systems": 6}),
        "posterior_version": "p0-v14",
        "flag": True,
    }
    audit = {"delta_basket_score_v2": 0.0123, "policy_artifact_id": "policy-a"}
    compiled = {
        "status": "ready",
        "reason_codes": [],
        "stage": "P0",
        "delta_v2_audit": audit,
        "probability_envelope": {
            "checkpoint_id": "p0-klga-0926",
            "stage": "P0",
            "probabilities": [0.23, 0.21, 0.16],
            "lineage": lineage,
        },
        "plan": {
            "state": "READY",
            "center_bucket": "64-65°F",
            "final_support": ["62-63°F", "64-65°F"],
            "target_solution": {"target_shares_by_bucket": {"64-65°F": target_shares}},
            "targets": [
                {"bucket": "64-65°F", "delta_shares": target_shares, "candidate_price": 0.21}
            ],
            "total_delta_notional_usdc": 0.21 * target_shares,
            "basket_support_selection": {
                "seated": {"window_probabilities": {"64-65°F": 0.23}},
                "seat_shape": {"state": "SYMMETRIC"},
                "beta_edge_tables": _beta_tables(),
            },
            "delta_v2_audit": copy.deepcopy(audit),
            "lineage": {**copy.deepcopy(lineage), "flag": 1},
        },
    }
    return {
        "schema": "bayes_basket_target_p0_preselection_v1",
        "preselection_id": preselection_id,
        "station_id": "KLGA",
        "event_date": "2026-09-26",
        "checkpoint_id": "p0-klga-0926",
        "probability_stage": "P0",
        "decision_at_utc": "2026-09-25T01:00:00Z",
        "quote_observed_at_utc": "2026-09-25T01:00:01Z",
        "quote_manifest_sha256": quote * 64,
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


def _commit(conn: sqlite3.Connection, value: dict[str, object]) -> dict[str, object]:
    return commit_p0_preselection(conn, value, created_at_utc="2026-09-25T01:00:02Z")


def test_both_blocks_leave_the_row_and_are_named_by_digest() -> None:
    row, blobs = split_preselection_blobs(
        stored_preselection_payload(_value(preselection_id="a", quote="a", target_shares=12))
    )
    compiled = row["compiled_plan"]
    assert "lineage" not in compiled["probability_envelope"]
    assert "beta_edge_tables" not in compiled["plan"]["basket_support_selection"]
    # What readers of stored rows read is still in the row.
    assert compiled["plan"]["basket_support_selection"]["seated"]
    assert compiled["plan"]["basket_support_selection"]["seat_shape"]["state"] == "SYMMETRIC"
    paths = compiled[STORED_BLOBS_KEY]["paths"]
    assert set(paths) == {
        "probability_envelope.lineage",
        "plan.basket_support_selection.beta_edge_tables",
    }
    assert set(paths.values()) == set(blobs)


def test_two_cycles_store_each_block_once_and_expand_byte_for_byte() -> None:
    first = _value(preselection_id="a", quote="a", target_shares=12)
    second = _value(preselection_id="b", quote="c", target_shares=15)
    with sqlite3.connect(":memory:") as conn:
        apply_production_ssot_schema(conn)
        assert _commit(conn, first)["state"] == "COMMITTED"
        assert _commit(conn, second)["state"] == "COMMITTED"
        assert conn.execute("SELECT COUNT(*) FROM basket_p0_preselection_blob").fetchone()[0] == 2
        rows = conn.execute(
            "SELECT plan_json FROM basket_p0_preselection_plan ORDER BY rowid"
        ).fetchall()
        restored = [expanded_stored_preselection(json.loads(r[0]), conn) for r in rows]
    assert [_canon(r) for r in restored] == [_canon(_persisted(first)), _canon(_persisted(second))]
    assert len(rows[1][0]) < 0.5 * len(_canon(_persisted(second)))


def test_identity_and_stored_row_readers_are_unchanged() -> None:
    value = _value(preselection_id="a", quote="a", target_shares=12)
    full = value["compiled_plan"]
    row, _ = split_preselection_blobs(stored_preselection_payload(value))
    stored = row["compiled_plan"]
    assert _canon(_decision_semantic_projection(stored)) == _canon(
        _decision_semantic_projection(full)
    )
    assert station_edge_from_plan(
        stored, station_id="KLGA", minimum_clip_usdc=1.0
    ) == station_edge_from_plan(full, station_id="KLGA", minimum_clip_usdc=1.0)
    # The in-memory preselection keeps both blocks.
    assert "lineage" in full["probability_envelope"]
    assert "beta_edge_tables" in full["plan"]["basket_support_selection"]


def test_the_blob_table_is_append_only() -> None:
    with sqlite3.connect(":memory:") as conn:
        apply_production_ssot_schema(conn)
        _commit(conn, _value(preselection_id="a", quote="a", target_shares=12))
        with pytest.raises(sqlite3.IntegrityError, match="append_only"):
            conn.execute("UPDATE basket_p0_preselection_blob SET blob_json='{}'")
        with pytest.raises(sqlite3.IntegrityError, match="append_only"):
            conn.execute("DELETE FROM basket_p0_preselection_blob")


def test_a_missing_or_altered_block_fails_closed() -> None:
    with sqlite3.connect(":memory:") as conn:
        apply_production_ssot_schema(conn)
        _commit(conn, _value(preselection_id="a", quote="a", target_shares=12))
        (raw,) = conn.execute("SELECT plan_json FROM basket_p0_preselection_plan").fetchone()
        row = json.loads(raw)
        with pytest.raises(ContractError, match="blob_connection_required"):
            expanded_stored_preselection(row)
        conn.execute("DROP TRIGGER basket_p0_preselection_blob_no_update")
        digest = row["compiled_plan"][STORED_BLOBS_KEY]["paths"]["probability_envelope.lineage"]
        conn.execute(
            "UPDATE basket_p0_preselection_blob SET blob_json='{\"x\":1}' WHERE blob_sha256=?",
            (digest,),
        )
        with pytest.raises(ContractError, match="blob_digest_mismatch"):
            expanded_stored_preselection(row, conn)
    with sqlite3.connect(":memory:") as other:
        apply_production_ssot_schema(other)
        from bayes_basket_target.p0_preselection import ensure_preselection_blob_schema

        ensure_preselection_blob_schema(other)
        with pytest.raises(ContractError, match="blob_missing"):
            expanded_stored_preselection(row, other)


def test_small_or_absent_blocks_stay_in_the_row() -> None:
    value = _value(preselection_id="a", quote="a", target_shares=12)
    del value["compiled_plan"]["plan"]["basket_support_selection"]["beta_edge_tables"]
    value["compiled_plan"]["probability_envelope"]["lineage"] = {"posterior_version": "p0-v14"}
    value["compiled_plan"]["plan"]["lineage"] = {"posterior_version": "p0-v14"}
    row, blobs = split_preselection_blobs(stored_preselection_payload(value))
    assert blobs == {}
    assert STORED_BLOBS_KEY not in row["compiled_plan"]
    blocked = {**_persisted(value), "compiled_plan": {"status": "blocked", "plan": None}}
    assert split_preselection_blobs(blocked) == (blocked, {})


def test_the_marker_key_is_reserved() -> None:
    value = _value(preselection_id="a", quote="a", target_shares=12)
    value["compiled_plan"][STORED_BLOBS_KEY] = {"schema": "x"}
    with pytest.raises(ContractError, match="stored_form_key_reserved"):
        split_preselection_blobs(stored_preselection_payload(value))
