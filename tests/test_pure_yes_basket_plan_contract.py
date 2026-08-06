from __future__ import annotations

from copy import deepcopy
import sqlite3

import pytest

from bayes_basket_target.contracts import ContractError, content_sha256
import bayes_basket_target.canonical_ingress as canonical_ingress
from bayes_basket_target.canonical_ssot_v17_migration import (
    TARGET_TABLE_DDL,
    TARGET_TRIGGER_DDL,
)
from bayes_basket_target.pure_yes_basket_plan_contract import (
    CONTRACT_SCHEMA,
    EXECUTION_GATE,
    MAIN_SELECTION_METHOD,
    POLICY_ARTIFACT_CONTENT_SHA256,
    POLICY_VERSION,
    SELECTION_METHOD,
    CanonicalPureYesBasketPlanContract,
    CanonicalPureYesBasketPlanFact,
)


def _sha(char: str) -> str:
    return char * 64


def _fact(**overrides: object) -> dict[str, object]:
    candidates = [5, 6]
    identity: dict[str, object] = {
        "station_id": "KMIA",
        "event_date": "2026-08-03",
        "settlement_timezone": "America/New_York",
        "probability_stage": "P1_BASE",
        "p1_base_content_sha256": _sha("a"),
        "p0_forecast_snapshot_content_sha256": _sha("b"),
        "market_bucket_bridge_content_sha256": _sha("c"),
        "surface_observation_winner_content_sha256": _sha("d"),
        "bucket_probability_vector_sha256": _sha("e"),
        "source_p1_decision_at_utc": "2026-08-05T16:30:00.000Z",
        "source_p1_freshness_expires_at_utc": "2026-08-05T16:45:00.000Z",
        "plan_at_utc": "2026-08-05T16:30:00.000Z",
        "policy_version": POLICY_VERSION,
        "policy_artifact_content_sha256": POLICY_ARTIFACT_CONTENT_SHA256,
        "selection_method": SELECTION_METHOD,
        "main_selection_method": MAIN_SELECTION_METHOD,
        "support_radius": 2,
        "main_ordinal": 6,
        "reported_high_ordinal": 5,
        "hard_excluded_ordinals": [0, 1, 2, 3, 4],
        "base_ordinals": [6],
        "underreach_ordinals": [5],
        "upside_breakthrough_ordinals": [],
        "upside_breakthrough_state": "BLOCKED_NO_REPORTED_HIGH_ADVANCEMENT_EVIDENCE",
        "candidate_ordinals": candidates,
        "candidate_ordinal_set_sha256": content_sha256(candidates),
        "execution_gate": EXECUTION_GATE,
        "buy_yes_only": True,
        "buy_no_intent_emitted": False,
        "orders_submitted": 0,
        "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
        "state": "MATERIALIZED_UNPUBLISHED",
    }
    identity.update(overrides)
    return {**identity, "content_sha256": content_sha256(identity)}


def _reseal(raw: dict[str, object]) -> dict[str, object]:
    raw["content_sha256"] = content_sha256({
        key: value for key, value in raw.items() if key != "content_sha256"
    })
    return raw


def _contract(*facts: dict[str, object]) -> dict[str, object]:
    identity: dict[str, object] = {
        "schema": CONTRACT_SCHEMA,
        "as_of_utc": "2026-08-05T16:30:00.000Z",
        "builder_id": "canonical.pure_yes_basket_plan.v1",
        "basket_plan_facts": list(facts),
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def test_pure_yes_plan_is_only_a_bounded_ordinal_support_set() -> None:
    parsed = CanonicalPureYesBasketPlanFact.from_mapping(_fact())

    assert parsed.raw["main_ordinal"] == 6
    assert parsed.raw["reported_high_ordinal"] == 5
    assert parsed.raw["candidate_ordinals"] == [5, 6]
    assert parsed.raw["buy_yes_only"] is True
    assert parsed.raw["buy_no_intent_emitted"] is False
    assert "token" not in str(parsed.raw).lower()
    assert CanonicalPureYesBasketPlanContract.from_mapping(
        _contract(parsed.raw)
    ).basket_plan_facts[0].content_sha256 == parsed.content_sha256


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"buy_no_intent_emitted": True}, "pure_yes_basket_plan_role_invalid"),
        ({"orders_submitted": 1}, "pure_yes_basket_plan_role_invalid"),
        (
            {
                "underreach_ordinals": [4, 5],
                "candidate_ordinals": [4, 5, 6],
                "candidate_ordinal_set_sha256": content_sha256([4, 5, 6]),
            },
            "pure_yes_basket_plan_candidate_set_invalid",
        ),
        (
            {"plan_at_utc": "2026-08-05T16:45:00.000Z"},
            "pure_yes_basket_plan_clock_order_invalid",
        ),
    ],
)
def test_pure_yes_plan_fails_closed_on_no_side_excluded_or_stale_scope(
    overrides: dict[str, object], error: str,
) -> None:
    with pytest.raises(ContractError, match=error):
        CanonicalPureYesBasketPlanFact.from_mapping(_fact(**overrides))


def test_pure_yes_plan_rejects_extra_realtime_or_no_token_fields() -> None:
    raw = _fact()
    raw["no_token"] = "forbidden"
    _reseal(raw)
    with pytest.raises(ContractError, match="fact_fields_mismatch"):
        CanonicalPureYesBasketPlanFact.from_mapping(raw)


def test_pure_yes_plan_contract_rejects_duplicate_scope() -> None:
    first = _fact()
    second = deepcopy(first)
    with pytest.raises(ContractError, match="scope_duplicate"):
        CanonicalPureYesBasketPlanContract.from_mapping(_contract(first, second))


def test_pure_yes_plan_typed_ingress_preview_commit_replay_and_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = sqlite3.connect(":memory:")
    conn.create_function("canonical_ingress_authorized", 0, lambda: 0)
    conn.execute(TARGET_TABLE_DDL)
    for ddl in TARGET_TRIGGER_DDL:
        conn.execute(ddl)
    monkeypatch.setattr(
        canonical_ingress, "_assert_pure_yes_basket_plan_references",
        lambda _conn, _contract: None,
    )
    monkeypatch.setattr(
        canonical_ingress, "assert_connection_target_capacity", lambda _conn: None
    )
    monkeypatch.setattr(
        canonical_ingress, "assert_target_database_owner", lambda _conn: None
    )
    packet = CanonicalPureYesBasketPlanContract.from_mapping(_contract(_fact()))

    preview = canonical_ingress.ingest_canonical_contract(conn, packet, commit=False)
    assert preview["state"] == "VALIDATED_NO_WRITE"
    assert preview["canonical_schema"] == CONTRACT_SCHEMA
    assert preview["validated_counts"] == {"pure_yes_basket_plan": 1}
    assert conn.execute("SELECT COUNT(*) FROM canonical_ssot_fact").fetchone()[0] == 0

    committed = canonical_ingress.ingest_canonical_contract(
        conn, packet, commit=True, ingested_at_utc="2026-08-05T16:30:00Z"
    )
    assert committed["inserted_count"] == 1
    replayed = canonical_ingress.ingest_canonical_contract(
        conn, packet, commit=True, ingested_at_utc="2026-08-05T16:30:00Z"
    )
    assert replayed["inserted_count"] == 0
    assert replayed["replayed_count"] == 1

    conflicting = _fact(
        reported_high_ordinal=6,
        underreach_ordinals=[],
        candidate_ordinals=[6],
        candidate_ordinal_set_sha256=content_sha256([6]),
    )
    with pytest.raises(ContractError, match="canonical_scope_content_conflict"):
        canonical_ingress.ingest_canonical_contract(
            conn,
            CanonicalPureYesBasketPlanContract.from_mapping(_contract(conflicting)),
            commit=True,
            ingested_at_utc="2026-08-05T16:30:00Z",
        )
