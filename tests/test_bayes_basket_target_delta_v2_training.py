from __future__ import annotations

import json
import sqlite3

import pytest
from tests.pr12_atomic_test_support import awc_contract, install_canonical_v12

from bayes_basket_target.production import PRODUCTION_EXECUTION_MODE
from bayes_basket_target.atomic_ledger import (
    AtomicLedgerError,
    initialize_capital_pool,
)
from bayes_basket_target.contracts import (
    ContractError,
    ProbabilityEnvelope,
    content_sha256,
)
from bayes_basket_target.market_registry import (
    apply_target_market_registry_schema,
)
from bayes_basket_target.production import commit_production_candidate_atomic
from bayes_basket_target.ssot import apply_production_ssot_schema
from bayes_basket_target.delta_v2_training import (
    FACT_SCHEMA,
    commit_delta_v2_training_fact,
    delta_v2_decision_scope_key,
)
from bayes_basket_target.delta_v2_settlement_label import (
    SETTLEMENT_VERSION_BINDING_SCHEMA,
    TERMS_SCHEMA,
    materialize_delta_v2_settled_training_labels,
)
from bayes_basket_target.settled_attribution import build_settled_attribution
from bayes_basket_target.settlement_version_scorecard import (
    build_settlement_version_evidence_packet,
)
from bayes_basket_target.station_policy_forward_outcome_binding import (
    UNAVAILABLE_REASONS,
)
NOW = "2026-07-23T05:00:00Z"
EVENT_DATE = "2026-07-23"
STATION = "ZUUU"
TZ = "America/New_York"
SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64


def _seed() -> tuple[sqlite3.Connection, ProbabilityEnvelope]:
    conn = sqlite3.connect(":memory:")
    apply_production_ssot_schema(conn)
    install_canonical_v12(conn)
    apply_target_market_registry_schema(conn)
    initialize_capital_pool(
        conn,
        balance_usdc=20.0,
        capital_epoch_id="strict-delta-forward-epoch",
        updated_at_utc=NOW,
    )
    conn.execute(
        "INSERT INTO basket_target_market_registry_snapshot VALUES(?,?,?,?,?,?,?,?)",
        (
            SHA_A,
            "reported_trajectory_market_registry_v1",
            "shared_live_registry_export_only",
            NOW,
            "/read-only/source.db",
            1,
            NOW,
            SHA_B,
        ),
    )
    conn.execute(
        """
        INSERT INTO basket_target_market_registry(
          snapshot_sha256,slug,event_date,outcome,city_slug,station_id,
          settlement_timezone,settlement_unit,source_lane,source_eligible,
          source_block_reason,resolution_url,rules_sha256,yes_token,no_token,
          market_active,market_accepting_orders,registry_json,refreshed_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            SHA_A, "zuzu-high", EVENT_DATE, "38°C", "chengdu", STATION,
            TZ, "C", "amsc", 1, "", "", SHA_C, "yes-38", "no-38",
            1, 1, "{}", NOW,
        ),
    )
    envelope = ProbabilityEnvelope.from_mapping(
        {
            "stage": "P1",
            "checkpoint_id": "p1-exact-1",
            "station_id": STATION,
            "event_date": EVENT_DATE,
            "settlement_timezone": TZ,
            "decision_at_utc": NOW,
            "fresh_until_utc": "2026-07-23T06:00:00Z",
            "bucket_ordinals": {"37°C": 0, "38°C": 1, "39°C": 2},
            "probabilities": {"37°C": 0.2, "38°C": 0.6, "39°C": 0.2},
            "posterior_version": "posterior-v1",
            "posterior_sha256": SHA_A,
            "bridge_sha256": SHA_B,
            "lineage": {
                "settlement_timezone": TZ,
                "settlement_definition": "daily_high_c",
                "lead_bucket": "d0",
                "ensemble_artifact_id": "ensemble-1",
                "canonical_observation_event_id": "obs-1",
                "truth_observation_revision_content_sha256": awc_contract(
                    station_id=STATION, observation_id="obs-1",
                    event_date=EVENT_DATE, settlement_timezone=TZ,
                    observed_at_utc=NOW, raw_object_sha256=SHA_A,
                )[1],
                "canonical_observation_committed_at": "2026-07-23T05:00:00.000Z",
                "hard_bound_id": "bound-1",
                "source_lane": "AWC",
                "report_type": "METAR",
                "hard_excluded_buckets": "NONE",
                "bucket_intervals_c_json": json.dumps(
                    {
                        "37°C": [None, 37.5],
                        "38°C": [37.5, 38.5],
                        "39°C": [38.5, None],
                    }
                ),
            },
        }
    )
    e = envelope.to_dict()
    conn.execute(
        """
        INSERT INTO basket_probability_envelope VALUES(
          ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
        )
        """,
        (
            e["checkpoint_id"], e["stage"], e["checkpoint_id"],
            e["station_id"], e["event_date"], e["settlement_timezone"],
            e["decision_at_utc"], e["fresh_until_utc"],
            json.dumps(e["bucket_ordinals"]), json.dumps(e["probabilities"]),
            e["posterior_version"], e["posterior_sha256"],
            e["bridge_sha256"], json.dumps(e["lineage"]),
            envelope.content_sha256, "COMMITTED", NOW,
        ),
    )
    conn.execute(
        """
        INSERT INTO basket_official_ws_snapshot VALUES(
          ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
        )
        """,
        (
            "ws-exact-1", "market-1", STATION, EVENT_DATE, TZ, 1, NOW,
            "quote-v1", NOW, "2026-07-23T05:00:05Z", SHA_D,
            "OFFICIAL_CLOB_WS", "inventory-v1",
            "order-state-v1", 20.0,
            json.dumps(
                [
                    {
                        "bucket": "38°C",
                        "token_id": "yes-38",
                        "ask_levels": [{"price": 0.4, "shares": 100.0}],
                    }
                ]
            ),
            "{}", SHA_C, "COMMITTED", NOW,
        ),
    )
    conn.execute(
        "INSERT INTO basket_confirmed_inventory_snapshot VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            "inventory-snapshot-1", STATION, EVENT_DATE, "inventory-v1",
            NOW, 1, "{}", SHA_B, "COMMITTED", NOW,
        ),
    )
    conn.execute(
        "INSERT INTO basket_order_state_snapshot VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            "order-state-snapshot-1", STATION, EVENT_DATE,
            "order-state-v1", NOW, 1, "{}", SHA_D, "COMMITTED", NOW,
        ),
    )
    conn.commit()
    return conn, envelope


def _compiled(envelope: ProbabilityEnvelope) -> dict:
    return {
        "status": "ready_no_live",
        "cadence_allows_candidate": True,
        "cadence_decision": "ALLOW_CANDIDATE",
        "execution_mode": PRODUCTION_EXECUTION_MODE,
        "live_submit_authorized": PRODUCTION_EXECUTION_MODE == "production_live",
        "production_plan_sha256": SHA_A,
        "production_cycle_sha256": SHA_B,
        "probability_envelope": envelope.to_dict(),
        "production_inputs": {
            "strategy_version": "basket-v2",
            "season_bin": "summer",
            "probability_maturity_bin": "d0",
            "liquidity_bin": "normal",
            "cash_phase_policy_version": "cash-v1",
            "cash_phase_policy_sha256": SHA_A,
            "delta_v2_artifact_id": "delta-v2-policy-1",
            "delta_v2_artifact_sha256": SHA_B,
        },
        "resolved_station_policy": {
            "activation_id": "station-policy-1",
            "policy_sha256": SHA_C,
        },
        "cash_phase_decision": {"phase": "STANDARD"},
        "plan": {
            "plan_id": "upstream-plan",
            "station_id": STATION,
            "event_date": EVENT_DATE,
            "state": "SEED",
            "snapshot_sha256": SHA_D,
            "candidate_support": ["38°C"],
            "final_support": ["38°C"],
            "current_core_buckets": [],
            "core_buckets": ["38°C"],
            "direction": "NEUTRAL",
            "current_direction": None,
            "core_transition": {
                "transition": "INITIALIZE",
                "direction_transition": "INITIALIZE",
            },
            "basket_state": "SEED",
            "basket_state_reason": "initial_complete_support_build",
            "basket_state_transition": {
                "state": "SEED",
                "reason": "initial_complete_support_build",
                "desired_support": ["38°C"],
                "selected_before": [],
                "added_buckets": ["38°C"],
                "retired_buckets": [],
                "positive_gap_buckets": ["38°C"],
            },
            "execution_gap_support": ["38°C"],
            "selected_buckets": ["38°C"],
            "targets": [
                {
                    "bucket": "38°C",
                    "target_price_id": "target-price-38",
                    "execution_candidate": "MAKER",
                }
            ],
            "delta_v2_required": True,
            "delta_v2_audit": {
                "schema": "delta_basket_score_authorization_v2",
                "authorized": True,
                "content_sha256": SHA_A,
            },
            "settlement_timezone": TZ,
            "lineage": {
                **dict(envelope.lineage),
                "probability_stage": "P1",
                "settlement_timezone": TZ,
                "probability_envelope_sha256": envelope.content_sha256,
                "policy_resolution_sha256": SHA_C,
                "cash_phase_decision_sha256": SHA_D,
            },
        },
        "order_sizing_decisions": [
            {
                "bucket": "38°C",
                "eligible": True,
                "c_order": 3.0,
                "q_order": 7.5,
                "r_limit": 0.4,
                "content_sha256": SHA_A,
            }
        ],
        "reason_codes": [],
        "boundary": {"calls_order_gateway": PRODUCTION_EXECUTION_MODE == "production_live"},
    }


def _commit(
    conn: sqlite3.Connection,
    envelope: ProbabilityEnvelope,
    *,
    created_at_utc: str = NOW,
    compiled_plan: dict | None = None,
    market_overrides: dict | None = None,
) -> dict:
    market_snapshot = {
        "snapshot_id": "ws-exact-1",
        "sequence": 1,
        "station_id": STATION,
        "event_date": EVENT_DATE,
        "settlement_timezone": TZ,
        "quote_version": "quote-v1",
        "quote_observed_at": NOW,
        "quote_expires_at": "2026-07-23T05:00:05Z",
        "quote_freshness_policy_sha256": SHA_D,
        "official_ws_quote_sha256": SHA_C,
        "inventory_sha256": SHA_B,
        "order_state_sha256": SHA_D,
        "buckets": [{"bucket": "38°C", "token_id": "yes-38"}],
    }
    if market_overrides:
        market_snapshot.update(market_overrides)
    contract, revision_sha = awc_contract(
        station_id=STATION, observation_id="obs-1", event_date=EVENT_DATE,
        settlement_timezone=TZ, observed_at_utc=NOW, raw_object_sha256=SHA_A,
    )
    return commit_production_candidate_atomic(
        conn,
        compiled_plan=compiled_plan or _compiled(envelope),
        market_snapshot=market_snapshot,
        awc_observation_contract=contract,
        truth_observation_revision_content_sha256=revision_sha,
        bound={
            "bound_id": "bound-1",
            "station_id": STATION,
            "event_date": EVENT_DATE,
            "bound_generation": 1,
            "hard_lower_bound": 37,
            "bound_json": {},
        },
        created_at_utc=created_at_utc,
    )


def _row_mapping(conn: sqlite3.Connection, fact_id: str) -> dict[str, object]:
    cursor = conn.execute(
        "SELECT * FROM basket_policy_training_fact WHERE fact_id=?",
        (fact_id,),
    )
    row = cursor.fetchone()
    assert row is not None
    return {
        str(description[0]): value
        for description, value in zip(cursor.description or (), row)
    }


def test_decision_scope_ignores_transport_versions_but_tracks_economic_changes() -> None:
    _conn, envelope = _seed()
    base_plan = _compiled(envelope)
    base_market = {
        "snapshot_id": "ws-1",
        "station_id": STATION,
        "event_date": EVENT_DATE,
        "settlement_timezone": TZ,
        "quote_version": "quote-low-1",
        "official_ws_quote_sha256": SHA_A,
        "inventory_sha256": SHA_B,
        "order_state_sha256": SHA_C,
        "quote_observed_at": NOW,
        "quote_expires_at": "2026-07-23T05:00:05Z",
        "remaining_budget_usdc": 12.0,
        "buckets": [
            {
                "bucket": "38°C",
                "token_id": "yes-38",
                "filled_shares": 0.0,
                "filled_cost_usdc": 0.0,
                "pending_shares": 0.0,
                "pending_notional_usdc": 0.0,
                "active_before": False,
            }
        ],
    }
    first = delta_v2_decision_scope_key(
        compiled_plan=base_plan,
        market_snapshot=base_market,
    )
    clock_only_plan = json.loads(json.dumps(base_plan))
    clock_only_plan["plan"]["decision_at_utc"] = (
        "2026-07-23T05:00:04Z"
    )
    clock_only_plan["plan"]["created_at"] = "2026-07-23T05:00:04Z"
    clock_only_plan["resolved_station_policy"][
        "cash_phase_observed_at_utc"
    ] = "2026-07-23T05:00:04Z"
    clock_only_market = {
        **base_market,
        "snapshot_id": "ws-keepalive-100",
        "quote_observed_at": "2026-07-23T05:00:04Z",
        "quote_expires_at": "2026-07-23T05:00:09Z",
    }
    assert delta_v2_decision_scope_key(
        compiled_plan=clock_only_plan,
        market_snapshot=clock_only_market,
    ) == first

    transport_only_market = {
        **base_market,
        "quote_version": "quote-high",
        "official_ws_quote_sha256": SHA_D,
        "inventory_sha256": SHA_D,
        "order_state_sha256": SHA_A,
    }
    assert delta_v2_decision_scope_key(
        compiled_plan=base_plan,
        market_snapshot=transport_only_market,
    ) == first

    changed_inventory_market = json.loads(json.dumps(base_market))
    changed_inventory_market["buckets"][0]["filled_shares"] = 1.0
    assert delta_v2_decision_scope_key(
        compiled_plan=base_plan,
        market_snapshot=changed_inventory_market,
    ) != first

    changed_plan = json.loads(json.dumps(base_plan))
    changed_plan["order_sizing_decisions"][0]["q_order"] = 8.5
    assert (
        delta_v2_decision_scope_key(
            compiled_plan=changed_plan,
            market_snapshot=base_market,
        )
        != first
    )

    # A plan with no lineage of its own is not a contract error.
    #
    # The delta_v2 optimizer never puts a lineage on its plan; the success
    # path in runtime.py adds one, so a compile blocked before that point
    # arrives here without it.  The mapping is read exactly once, as the last
    # of three fallbacks for probability_envelope_sha256.  Requiring it made
    # every such blocked compile die on delta_v2_plan_lineage_mapping_required
    # and discarded the reason the plan was actually blocked -- fourteen
    # stations stood on it at once on 2026-08-31.
    #
    # Equality is the load-bearing half: the key is an identity, so a key
    # that shifted would unbind every decision already carrying one.  With
    # the envelope carrying its own content_sha256, which is the reading
    # ahead of this one, dropping the plan lineage changes nothing.
    lineage_free_plan = json.loads(json.dumps(base_plan))
    lineage_free_plan["plan"].pop("lineage", None)
    sha_from_envelope = json.loads(json.dumps(lineage_free_plan))
    sha_from_envelope["probability_envelope"]["content_sha256"] = (
        base_plan["plan"]["lineage"]["probability_envelope_sha256"]
    )
    keyed_from_envelope = delta_v2_decision_scope_key(
        compiled_plan=sha_from_envelope,
        market_snapshot=base_market,
    )
    with_lineage_too = json.loads(json.dumps(sha_from_envelope))
    with_lineage_too["plan"]["lineage"] = dict(base_plan["plan"]["lineage"])
    assert keyed_from_envelope == delta_v2_decision_scope_key(
        compiled_plan=with_lineage_too,
        market_snapshot=base_market,
    )

    # And when nobody recorded the hash anywhere, it is derived from the
    # envelope rather than refused.  ProbabilityEnvelope.to_dict() omits
    # content_sha256 on purpose -- from_mapping rejects it as an unknown
    # field, so no caller can hand over a hash that disagrees with the body --
    # and the plan lineage that would otherwise carry it is added only on the
    # success path.  A blocked plan therefore has none of the three recorded
    # readings, and recomputing is asking the same question the writer asked,
    # not trusting a claim.
    envelope_only = json.loads(json.dumps(lineage_free_plan))
    envelope_only["probability_envelope"].pop("content_sha256", None)
    derived = delta_v2_decision_scope_key(
        compiled_plan=envelope_only,
        market_snapshot=base_market,
    )
    recorded = json.loads(json.dumps(envelope_only))
    recorded["probability_envelope"]["content_sha256"] = ProbabilityEnvelope.from_mapping(
        dict(envelope_only["probability_envelope"])
    ).content_sha256
    assert derived == delta_v2_decision_scope_key(
        compiled_plan=recorded,
        market_snapshot=base_market,
    )

    # A mapping that is not an envelope at all still fails closed on the
    # missing hash -- the honest reason -- and not on a parse error thrown
    # out of a fallback.
    # Keep the probability lineage -- its own mapping guard sits ahead of
    # this one -- and remove only what the hash needs to be derivable.
    not_an_envelope = json.loads(json.dumps(envelope_only))
    not_an_envelope["probability_envelope"].pop("probabilities", None)
    with pytest.raises(
        ContractError, match="delta_v2_probability_envelope_sha256_missing"
    ):
        delta_v2_decision_scope_key(
            compiled_plan=not_an_envelope,
            market_snapshot=base_market,
        )


def test_future_station_policy_outcome_contract_gap_is_atomic_fact_evidence() -> None:
    conn, envelope = _seed()
    receipt = _commit(conn, envelope)
    fact_id = receipt["delta_v2_training_fact"]["fact_id"]
    feature = json.loads(conn.execute(
        "SELECT feature_json FROM basket_policy_training_fact WHERE fact_id=?",
        (fact_id,),
    ).fetchone()[0])
    outcome_gap = feature["station_policy_outcome_materialization"]
    assert outcome_gap["state"] == "UNAVAILABLE"
    assert outcome_gap["bindings"] == {}
    assert outcome_gap["unavailable_families"] == UNAVAILABLE_REASONS
    assert outcome_gap["writes_production_db"] is False
    assert outcome_gap["promotion_permitted"] is False
    forbidden_legacy_fields = {
        f"candidate_{'score'}", f"baseline_{'score'}",
        f"bucket_{'probabilities'}",
        f"station_policy_family_{'outcomes'}",
        f"station_policy_family_{'scores'}",
    }
    assert not forbidden_legacy_fields.intersection(feature)


def test_unavailable_station_policy_outcome_contract_does_not_block_existing_fact_lane() -> None:
    conn, envelope = _seed()
    receipt = _commit(conn, envelope)
    fact_id = receipt["delta_v2_training_fact"]["fact_id"]
    feature = json.loads(conn.execute(
        "SELECT feature_json FROM basket_policy_training_fact WHERE fact_id=?",
        (fact_id,),
    ).fetchone()[0])

    gap = feature["station_policy_outcome_materialization"]
    assert gap["state"] == "UNAVAILABLE"
    assert gap["bindings"] == {}
    assert gap["unavailable_families"] == UNAVAILABLE_REASONS


def test_decision_and_exact_version_training_fact_commit_atomically() -> None:
    conn, envelope = _seed()
    with conn:
        receipt = _commit(conn, envelope)
        assert receipt["state"] == "committed"
        assert receipt["delta_v2_training_fact"]["state"] == "committed"
        fact = conn.execute(
            """
            SELECT stage,probability_envelope_id,ws_snapshot_id,
                   inventory_snapshot_id,order_state_snapshot_id,
                   intent_count,authority_scope,feature_json,
                   decision_scope_key,captured_at_utc,source_sequence,
                   quote_observed_at,quote_expires_at,
                   quote_freshness_policy_sha256
            FROM basket_policy_training_fact
            """
        ).fetchone()
        assert fact[:7] == (
            "P1", "p1-exact-1", "ws-exact-1", "inventory-snapshot-1",
            "order-state-snapshot-1", 1, "PRODUCTION_NO_LIVE_FORWARD",
        )
        feature = json.loads(fact[7])
        decision_lifecycle = json.loads(conn.execute(
            "SELECT details_json FROM auto_trade_decisions"
        ).fetchone()[0])["basket_lifecycle"]
        assert feature["basket_lifecycle"] == decision_lifecycle
        assert decision_lifecycle["state"] == "SEED"
        assert decision_lifecycle["transition"]["added_buckets"] == [
            "38°C"
        ]
        assert len(decision_lifecycle["support_version_sha256"]) == 64
        vectors = feature["execution_version_vectors"]
        assert len(vectors) == 1
        assert vectors[0]["probability_sha256"] == envelope.content_sha256
        assert vectors[0]["official_ws_quote_sha256"] == SHA_C
        assert vectors[0]["inventory_sha256"] == SHA_B
        assert vectors[0]["order_state_sha256"] == SHA_D
        assert vectors[0]["target_price_id"] == "target-price-38"
        assert vectors[0]["quote_observed_at"] == NOW
        assert vectors[0]["quote_expires_at"] == "2026-07-23T05:00:05Z"
        assert feature["no_execution_policy"] is None
        assert not {
            "posterior_version",
            "posterior_sha256",
            "snapshot_sha256",
            "quote_version",
            "inventory_version",
            "order_state_version",
        }.intersection(vectors[0])
        assert len(fact[8]) == 64
        assert fact[9:] == (
            NOW,
            1,
            NOW,
            "2026-07-23T05:00:05Z",
            SHA_D,
        )
        # The flag follows the plan that authorized the intent, rather than
        # the literal 0 it used to be written as.  This fixture compiles
        # under the deployed mode, so it asserts agreement with that mode --
        # pinning a constant here is what let the ledger keep writing
        # no-live intents for a live deployment.
        live_intents = conn.execute(
            "SELECT COUNT(*) FROM basket_target_order_intents "
            "WHERE live_submit_authorized=1"
        ).fetchone()[0]
        total_intents = conn.execute(
            "SELECT COUNT(*) FROM basket_target_order_intents"
        ).fetchone()[0]
        assert live_intents == (
            total_intents if PRODUCTION_EXECUTION_MODE == "production_live" else 0
        )

        repeated = _commit(conn, envelope)
        assert repeated["receipt"]["state"] == "already_committed"
        assert repeated["delta_v2_training_fact"]["state"] == "already_committed"
        assert conn.execute(
            "SELECT COUNT(*) FROM basket_policy_training_fact"
        ).fetchone()[0] == 1


def test_training_fact_fails_closed_on_registry_timezone_drift() -> None:
    conn, envelope = _seed()
    conn.execute(
        "UPDATE basket_target_market_registry SET settlement_timezone='Asia/Seoul'"
    )
    with pytest.raises(
        ContractError, match="delta_v2_training_registry_timezone_mismatch"
    ):
        _commit(conn, envelope)
    assert conn.execute(
        "SELECT COUNT(*) FROM auto_trade_decisions"
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM basket_policy_training_fact"
    ).fetchone()[0] == 0


def test_quote_expiry_exact_boundary_is_not_fresh_and_writes_nothing() -> None:
    conn, envelope = _seed()
    # Quote freshness is the upstream evidence gate.  At the exact expiry
    # boundary there is no valid first-actionable execution window from which
    # to derive a hard deadline, so the canonical blocker is quote freshness,
    # not a synthetic expired execution deadline.
    with pytest.raises(
        ContractError, match="delta_v2_quote_not_fresh_at_decision"
    ):
        _commit(
            conn,
            envelope,
            created_at_utc="2026-07-23T05:00:05Z",
        )
    for table in (
        "basket_target_observation_facts",
        "basket_target_bound_facts",
        "basket_target_execution_cycles",
        "auto_trade_decisions",
        "basket_target_order_intents",
        "basket_policy_training_fact",
    ):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


@pytest.mark.parametrize(
    ("market_overrides", "error"),
    [
        ({"sequence": 2}, "delta_v2_source_sequence_drift"),
        ({"sequence": "not-an-integer"}, "delta_v2_source_sequence_invalid"),
        ({"sequence": 1.5}, "delta_v2_source_sequence_invalid"),
        ({"quote_freshness_policy_sha256": SHA_A}, "delta_v2_quote_freshness_drift"),
    ],
)
def test_quote_source_sequence_and_freshness_evidence_drift_fails_closed(
    market_overrides: dict,
    error: str,
) -> None:
    conn, envelope = _seed()
    with pytest.raises(ContractError, match=error):
        _commit(conn, envelope, market_overrides=market_overrides)
    assert conn.execute(
        "SELECT COUNT(*) FROM auto_trade_decisions"
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM basket_policy_training_fact"
    ).fetchone()[0] == 0


def test_same_decision_scope_with_different_evidence_is_a_durable_conflict() -> None:
    conn, envelope = _seed()
    first_receipt = _commit(conn, envelope)
    first_id = first_receipt["delta_v2_training_fact"]["fact_id"]
    conflicting = _row_mapping(conn, first_id)
    conflicting["source_sequence"] = 2
    conflicting["quote_freshness_policy_sha256"] = SHA_A
    identity = {
        key: value
        for key, value in conflicting.items()
        if key not in {"fact_id", "content_sha256"}
    }
    conflicting_sha = content_sha256({"schema": FACT_SCHEMA, **identity})
    conflicting["content_sha256"] = conflicting_sha
    conflicting["fact_id"] = f"d2fact_{conflicting_sha[:40]}"
    conflicting["schema"] = FACT_SCHEMA

    with pytest.raises(
        ContractError,
        match="delta_v2_training_fact_scope_conflict",
    ):
        commit_delta_v2_training_fact(conn, fact=conflicting)
    assert conn.execute(
        "SELECT COUNT(*) FROM basket_policy_training_fact"
    ).fetchone()[0] == 1


def test_m5_failure_at_forward_fact_write_rolls_back_every_prior_write() -> None:
    conn, envelope = _seed()
    conn.execute(
        """
        CREATE TRIGGER inject_forward_fact_failure
        BEFORE INSERT ON basket_policy_training_fact
        BEGIN
          SELECT RAISE(ABORT, 'injected_forward_fact_failure');
        END
        """
    )
    with pytest.raises(
        AtomicLedgerError,
        match="training_fact_canonical_write_failed",
    ):
        _commit(conn, envelope)
    for table in (
        "basket_target_observation_facts",
        "basket_target_bound_facts",
        "basket_target_execution_cycles",
        "auto_trade_decisions",
        "basket_target_order_intents",
        "basket_target_execution_version_vectors",
        "basket_target_capital_reservations",
        "basket_policy_training_fact",
    ):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_zero_intent_commit_persists_explicit_no_execution_only() -> None:
    conn, envelope = _seed()
    blocked = _compiled(envelope)
    blocked.update(
        {
            "status": "blocked",
            "cadence_allows_candidate": False,
            "cadence_decision": "HOLD",
            "reason_codes": ["test_zero_intent_blocked"],
        }
    )
    receipt = _commit(conn, envelope, compiled_plan=blocked)

    assert receipt["candidate_will_order"] is False
    assert receipt["delta_v2_training_fact"]["state"] == "committed"
    assert conn.execute(
        "SELECT COUNT(*) FROM basket_target_order_intents"
    ).fetchone()[0] == 0
    vector = conn.execute(
        "SELECT vector_policy,order_intent_id,training_fact_id,vector_json "
        "FROM basket_target_execution_version_vectors"
    ).fetchone()
    assert vector[0] == "NO_EXECUTION"
    assert vector[1] is None
    # NO_EXECUTION is durable evidence, not an absence of evidence.  The
    # canonical M5 transaction binds it to the same forward fact even though
    # there is intentionally no order intent.
    assert vector[2] == receipt["delta_v2_training_fact"]["fact_id"]
    assert json.loads(vector[3])["policy"] == "NO_EXECUTION"
    decision = conn.execute(
        "SELECT decided_state,will_order FROM auto_trade_decisions"
    ).fetchone()
    assert decision == ("blocked", 0)
    feature = json.loads(
        conn.execute(
            "SELECT feature_json FROM basket_policy_training_fact"
        ).fetchone()[0]
    )
    assert feature["execution_version_vectors"] == []
    assert feature["no_execution_policy"]["policy"] == "NO_EXECUTION"


def test_final_label_waits_for_registry_close_and_accepted_truth() -> None:
    conn, envelope = _seed()
    _commit(conn, envelope)
    conn.execute(
        """
        INSERT INTO calibration_station_registry VALUES(
          ?,?,?,?,?,?,?,?,?
        )
        """,
        (
            STATION, STATION, "Chengdu", TZ, "AMSC", "daily_high_c",
            "registry-v1", "formal_truth", SHA_A,
        ),
    )
    conn.execute(
        """
        INSERT INTO calibration_station_daily_high_truth VALUES(
          ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
        )
        """,
        (
            "truth-1", STATION, EVENT_DATE, 38.2, "AMSC", "daily_high_c",
            0, "2026-07-24T04:01:00Z", "2026-07-24T04:01:01Z",
            "2026-07-24T04:01:02Z", 1, "ACCEPTED", "formal_truth",
            SHA_B, "[]", "FORMAL_NATIVE_ELIGIBLE", SHA_C,
        ),
    )
    open_result = materialize_delta_v2_settled_training_labels(
        conn,
        as_of_utc="2026-07-24T03:59:59Z",
    )
    assert open_result["appended_count"] == 0
    assert open_result["skipped_open_count"] == 0

    closed = materialize_delta_v2_settled_training_labels(
        conn,
        as_of_utc="2026-07-24T04:02:00Z",
    )
    assert closed["appended_count"] == 1
    label = conn.execute(
        "SELECT winning_bucket,outcome_json,authority_scope "
        "FROM basket_policy_training_label"
    ).fetchone()
    assert label[0] == "38°C"
    assert json.loads(label[1])["label_semantics"] == (
        "hypothetical_requested_intents_not_fills"
    )
    outcome = json.loads(label[1])
    binding = outcome["settlement_version_binding"]
    assert binding["schema"] == SETTLEMENT_VERSION_BINDING_SCHEMA
    assert binding["station_id"] == STATION
    assert binding["event_date"] == EVENT_DATE
    assert binding["settlement_timezone"] == TZ
    assert binding["truth_id"] == "truth-1"
    assert binding["truth_revision_no"] == 0
    binding_sha = binding.pop("content_sha256")
    assert content_sha256(binding) == binding_sha
    terms = outcome["settled_attribution"]
    assert terms["schema"] == TERMS_SCHEMA
    assert terms["attribution_basis"] == (
        "HYPOTHETICAL_REQUESTED_INTENTS"
    )
    assert terms["realized_attribution_status"] == (
        "NA_PRODUCTION_NO_LIVE"
    )
    assert (
        terms["coverage_usdc"]
        + terms["overlap_usdc"]
        + terms["edge_usdc"]
        - terms["execution_cost_usdc"]
        - terms["failure_cost_usdc"]
    ) == pytest.approx(terms["counterfactual_pnl_usdc"])
    assert label[2] == "FORMAL_NATIVE_ELIGIBLE"
    attribution = build_settled_attribution(
        conn,
        as_of_utc="2026-07-24T04:02:00Z",
    )
    assert attribution["state"] == (
        "COMPLETE_COUNTERFACTUAL_NO_LIVE"
    )
    assert attribution["rows"][0]["status"] == "COUNTERFACTUAL_CLOSED"
    assert attribution["rows"][0]["realized_pnl_status"] == (
        "NA_PRODUCTION_NO_LIVE"
    )
    version_packet = build_settlement_version_evidence_packet(
        conn,
        as_of_utc="2026-07-24T04:02:00Z",
        station_ids=[STATION],
        window_start_date=EVENT_DATE,
        window_end_date=EVENT_DATE,
    )
    assert version_packet["state"] == "BLOCKED_EVIDENCE"
    assert version_packet["integrity_blockers"] == []
    assert version_packet["rows"][0][
        "settlement_version_binding_status"
    ] == "BOUND"
    assert version_packet["rows"][0][
        "realized_pnl_status"
    ] == "NA_ACTUAL_FILL_EVIDENCE_MISSING"
    assert version_packet["promotion_receipt"][
        "production_no_live_blocked"
    ] is False

    repeated = materialize_delta_v2_settled_training_labels(
        conn,
        as_of_utc="2026-07-24T04:03:00Z",
    )
    assert repeated["appended_count"] == 0


def test_latest_quarantined_truth_revision_blocks_older_accepted_truth() -> None:
    conn, envelope = _seed()
    _commit(conn, envelope)
    conn.execute(
        """
        INSERT INTO calibration_station_registry VALUES(
          ?,?,?,?,?,?,?,?,?
        )
        """,
        (
            STATION, STATION, "Chengdu", TZ, "AMSC", "daily_high_c",
            "registry-v1", "formal_truth", SHA_A,
        ),
    )
    base = (
        STATION, EVENT_DATE, 38.2, "AMSC", "daily_high_c",
    )
    conn.execute(
        """
        INSERT INTO calibration_station_daily_high_truth VALUES(
          ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
        )
        """,
        (
            "truth-accepted", *base, 0,
            "2026-07-24T04:01:00Z", "2026-07-24T04:01:01Z",
            "2026-07-24T04:01:02Z", 1, "ACCEPTED", "formal_truth",
            SHA_B, "[]", "FORMAL_NATIVE_ELIGIBLE", SHA_C,
        ),
    )
    conn.execute(
        """
        INSERT INTO calibration_station_daily_high_truth VALUES(
          ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
        )
        """,
        (
            "truth-quarantined", *base, 1,
            "2026-07-24T04:02:00Z", "2026-07-24T04:02:01Z",
            "2026-07-24T04:02:02Z", 1, "QUARANTINED", "manual_review",
            SHA_C, "[]", "FORMAL_NATIVE_ELIGIBLE", SHA_A,
        ),
    )

    result = materialize_delta_v2_settled_training_labels(
        conn,
        as_of_utc="2026-07-24T04:03:00Z",
    )

    assert result["appended_count"] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM basket_policy_training_label"
    ).fetchone()[0] == 0


def test_an_edge_that_moved_nothing_is_one_fact_not_a_conflict() -> None:
    """A price is not a new decision.  An edge that moved the answer is.

    ``decision_scope_key`` addresses one economically distinct decision: the
    probability, the position book, the promoted policy artifacts, and --
    verbatim and unrounded -- what the objective did with the edge, which is
    ``selected_score``, ``final_support``, ``selected_buckets`` and every
    action's ``limit_price``, ``maximum_shares`` and ``cash_clip``.  So a key
    match is not a claim that the book stood still; it is the stronger claim
    that the edge curve did not move the sizing instruction by one bit.  Over
    the 145 adjacent fact pairs since 2026-08-28 whose belief was unchanged,
    the edge curve moved in 82 and a sizing output moved in all 82.

    The fact's own content hash covers, on top of that, the quote generation,
    the snapshot ids and the plan sha, which turn over every cycle whether or
    not anything was decided.  So the two halves disagreed about what "the
    same decision" means and the comparison half won by raising.

    That stayed rare while P0 re-minted an envelope every few minutes.
    Suppressing those restatements on 2026-09-01 made the key stable and the
    conflict routine -- single digits an hour to 50 and 63 -- and it became
    the largest stage-open blocker on the forward day, 126 of 200 sampled, on
    the very day the ladder exists to open early.
    """

    conn, envelope = _seed()
    first = _commit(conn, envelope)["delta_v2_training_fact"]
    stored = _row_mapping(conn, first["fact_id"])

    # Everything a later cycle turns over on its own: the clocks, the snapshot
    # generations, the ids, and the answer computed against a moved book.
    replay = dict(stored)
    replay.update({
        "source_sequence": int(stored["source_sequence"]) + 7,
        "decision_at_utc": "2027-01-01T00:00:00.000Z",
        "captured_at_utc": "2027-01-01T00:00:00.000Z",
        "created_at_utc": "2027-01-01T00:00:00.000Z",
        "quote_observed_at": "2027-01-01T00:00:00.000Z",
        "quote_expires_at": "2027-01-01T00:01:00.000Z",
        "ws_snapshot_id": "p0_ws_moved",
        "quote_version": "qv_moved",
        "quote_sha256": SHA_A,
        "inventory_snapshot_id": "inv_moved",
        "order_state_snapshot_id": "ord_moved",
        "cycle_id": "cycle_moved",
        "decision_id": "decision_moved",
        "decision_sha256": SHA_A,
        "plan_sha256": SHA_A,
        "feature_json": '{"moved": true}',
    })
    identity = {
        key: value for key, value in replay.items()
        if key not in {"fact_id", "content_sha256"}
    }
    replay_sha = content_sha256({"schema": FACT_SCHEMA, **identity})
    replay["content_sha256"] = replay_sha
    replay["fact_id"] = f"d2fact_{replay_sha[:40]}"
    replay["schema"] = FACT_SCHEMA
    assert replay["decision_scope_key"] == stored["decision_scope_key"]
    assert replay["content_sha256"] != stored["content_sha256"]

    receipt = commit_delta_v2_training_fact(conn, fact=replay)
    assert receipt["state"] == "already_committed"
    assert receipt["idempotent"] is True
    # The first row stands; the replay adds nothing.  One sample per belief
    # state is the training set this is for -- a repeated observation of an
    # unchanged belief is not new information about the belief.
    assert receipt["fact_id"] == stored["fact_id"]
    assert conn.execute(
        "SELECT COUNT(*) FROM basket_policy_training_fact"
    ).fetchone()[0] == 1


def test_a_scope_defining_field_that_differs_under_one_key_still_conflicts(
) -> None:
    """What the guard is actually for: a collision or a corrupt row.

    The scope-defining columns are inputs to the key, so they cannot differ
    between two rows sharing one unless something is wrong, and the blocker
    now names which field diverged instead of saying only that one did.
    """

    conn, envelope = _seed()
    first = _commit(conn, envelope)["delta_v2_training_fact"]
    stored = _row_mapping(conn, first["fact_id"])
    corrupt = dict(stored)
    corrupt["probability_envelope_id"] = "p0-native-someone-elses-envelope"
    identity = {
        key: value for key, value in corrupt.items()
        if key not in {"fact_id", "content_sha256"}
    }
    corrupt_sha = content_sha256({"schema": FACT_SCHEMA, **identity})
    corrupt["content_sha256"] = corrupt_sha
    corrupt["fact_id"] = f"d2fact_{corrupt_sha[:40]}"
    corrupt["schema"] = FACT_SCHEMA
    assert corrupt["decision_scope_key"] == stored["decision_scope_key"]

    with pytest.raises(
        ContractError,
        match="delta_v2_training_fact_scope_conflict:probability_envelope_id",
    ):
        commit_delta_v2_training_fact(conn, fact=corrupt)
    assert conn.execute(
        "SELECT COUNT(*) FROM basket_policy_training_fact"
    ).fetchone()[0] == 1
