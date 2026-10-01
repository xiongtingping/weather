"""One station-day replay across probability, inventory, and order state.

This is the integration seam that matters to the pure-YES basket:

* P0 may open the chain;
* P0-M and P1 hold under the production stage gate; an explicitly offline
  three-stage replay checks the inventory arithmetic after their Bayes updates;
* filled and confirmed-pending shares are one position, rather than a new
  allocation for every probability stage;
* a posterior move or a hard-dead bucket cannot manufacture a YES sell; and
* an exchange result that is not authoritative parks the scope before a new
  intent can be submitted.

The probability and plan portions use the production compilers.  The final
two checks use the real resting-order and execution-transition ledgers rather
than synthetic flags on a plan.
"""

from __future__ import annotations

import copy
import json

import pytest

from bayes_basket_target import (
    PolicyArtifact,
    ProbabilityEnvelope,
    StationBasketPolicyActivation,
)
from bayes_basket_target import resting_ladder_lifecycle as resting_lane
from bayes_basket_target.contracts import content_sha256
from bayes_basket_target.delta_v2_artifact import DeltaV2PolicyArtifact
from bayes_basket_target.execution_reconciliation import (
    apply_execution_receipt,
    can_submit_next_order,
)
from bayes_basket_target.materialization import materialize_p0m_observed
from bayes_basket_target.p1_compiler import compile_station_p1_packets
from bayes_basket_target.production import compile_production_plan
from bayes_basket_target.regime_materialization import build_regime_artifact
from bayes_basket_target.stages import select_current_stage
from tests import test_bayes_basket_target_execution_transition_runtime as transition_fixture
from tests import test_resting_ladder_lifecycle as resting_fixture
from tests.bayes_basket_target_e2e_fixtures import _cash_input
from tests.test_bayes_basket_target_production_chain_e2e import (
    _delta_v2_policy,
    _fill_resolver,
    _inputs,
    _p0_opening_compile,
)
from tests.test_charter_150_p1_joint_likelihood import (
    _observation,
    _p0m,
    _registry,
    _taf_artifact,
)
from tests.test_charter_161_p1_direct_update import _block, _with_block


BUCKETS = ("37", "38", "39")
P0_FILL_COST = {"37": 1.0, "38": 1.75, "39": 1.0}


def _root_geometry(root: ProbabilityEnvelope) -> ProbabilityEnvelope:
    """Give the production P0 fixture the exact Celsius axis Bayes reads."""

    raw = root.to_dict()
    raw["lineage"] = {
        **dict(root.lineage),
        "bucket_intervals_c_json": json.dumps({
            "37": [36.5, 37.5],
            "38": [37.5, 38.5],
            "39": [38.5, 39.5],
        }),
        "model_center_c": "38.0",
        "season_bin": "summer",
        "settlement_definition": "official_daily_tmax_c",
        "lead_bucket": "d1",
        "run_cycle": "00Z",
    }
    return ProbabilityEnvelope.from_mapping(raw)


def _stage_policy_inputs(stage: str):
    """Clone the promoted P1 fixture onto the exact stage being compiled."""

    _, priors, artifacts, activations, _, _ = _inputs()
    stage_artifacts: dict[str, PolicyArtifact] = {}
    for item in artifacts.values():
        clone = PolicyArtifact.create(
            artifact_id=item.artifact_id,
            family=item.family,
            station_id=item.station_id,
            stage=stage,
            season_bin=item.season_bin,
            maturity_bin=item.maturity_bin,
            liquidity_bin=item.liquidity_bin,
            strategy_version=item.strategy_version,
            theta_local=dict(item.theta_local),
            effective_sample_n=item.effective_sample_n,
            kappa=item.kappa,
            trained_through_utc=item.trained_through_utc,
            valid_from_utc=item.valid_from_utc,
            valid_until_utc=item.valid_until_utc,
        )
        stage_artifacts[clone.artifact_id] = clone
    stage_activations = [
        StationBasketPolicyActivation.create(
            activation_id=item.activation_id,
            station_id=item.station_id,
            stage=stage,
            season_bin=item.season_bin,
            maturity_bin=item.maturity_bin,
            liquidity_bin=item.liquidity_bin,
            strategy_version=item.strategy_version,
            global_prior_id=item.global_prior_id,
            target_artifact_id=item.target_artifact_id,
            reserve_artifact_id=item.reserve_artifact_id,
            cadence_artifact_id=item.cadence_artifact_id,
            fallback_families=list(item.fallback_families),
            generation=item.generation,
            valid_from_utc=item.valid_from_utc,
            valid_until_utc=item.valid_until_utc,
            activated_at_utc=item.activated_at_utc,
        )
        for item in activations
    ]
    raw_delta = _delta_v2_policy().to_dict()
    raw_delta.pop("content_sha256", None)
    raw_delta["stage"] = stage
    raw_delta["content_sha256"] = content_sha256(raw_delta)
    return (
        priors,
        stage_artifacts,
        stage_activations,
        DeltaV2PolicyArtifact.from_mapping(raw_delta),
    )


def _market_state(
    base: dict,
    *,
    filled: dict[str, float],
    filled_cost: dict[str, float],
    pending: dict[str, float],
    identity: str,
) -> dict:
    """Materialize one coherent inventory/order-state view on one live book."""

    market = copy.deepcopy(base)
    for row in market["buckets"]:
        bucket = row["bucket"]
        row["ask_levels"] = [{"price": 0.06, "shares": 100.0}]
        row["bid_price"] = 0.05
        row["filled_shares"] = float(filled[bucket])
        row["filled_cost_usdc"] = float(filled_cost[bucket])
        row["pending_shares"] = float(pending[bucket])
        row["pending_notional_usdc"] = float(pending[bucket]) * 0.05
    market["snapshot_id"] = f"snapshot-{identity}"
    market["quote_version"] = f"quote-{identity}"
    market["inventory_version"] = f"inventory-{identity}"
    market["order_state_version"] = f"orders-{identity}"
    market["inventory_sha256"] = content_sha256({
        "filled": filled,
        "filled_cost": filled_cost,
    })
    market["order_state_sha256"] = content_sha256({"pending": pending})
    market["cadence_snapshot"] = {
        **market["cadence_snapshot"],
        "quote_version": market["quote_version"],
        "fill_version": market["inventory_version"],
    }
    return market


def _compile_stage(
    *,
    envelopes: list[ProbabilityEnvelope],
    market: dict,
    stage: str,
    cash_policy,
    at: str,
    last_decision_signature: dict | None = None,
) -> dict:
    priors, artifacts, activations, delta_policy = _stage_policy_inputs(stage)
    current = envelopes[-1]
    inputs = dict(
        probability_envelopes=envelopes,
        market_snapshot=market,
        policy_activations=activations,
        global_priors=priors,
        policy_artifacts=artifacts,
        evaluation_at_utc=at,
        season_bin="summer",
        maturity_bin="d1",
        liquidity_bin="liquid",
        strategy_version="target-v1",
        base_policy={"clip_max_notional_usdc": 3.0},
        delta_v2_policy=delta_policy,
        delta_v2_fill_resolver=_fill_resolver,
        regime_artifact=build_regime_artifact(current),
        cash_phase_policy=cash_policy,
        cash_phase_input=_cash_input(cash_policy),
        resource_probe_ok=True,
        last_decision_signature=last_decision_signature,
    )
    # Charter 229: later stages still compile, but cannot buy in production.
    held = compile_production_plan(**inputs)
    assert held["status"] == "hold"
    assert "basket_stage_not_buying" in held["plan"]["reason_codes"]
    assert held["plan"]["delta_v2_action_selection"]["selected_actions"] == []
    assert all(
        row["execution_candidate"] == "NONE" for row in held["plan"]["targets"]
    )
    # Exercise the pre-229 three-stage inventory mechanism offline only.
    # Each call above rechecks the production gate before this local override.
    from bayes_basket_target import delta_v2_runtime

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(delta_v2_runtime, "BASKET_BUYING_STAGES", ("P0", "P0-M", "P1"))
        return compile_production_plan(**inputs)


def _targets(plan: dict) -> dict[str, dict]:
    return {row["bucket"]: row for row in plan["targets"]}


def test_station_day_replays_bayes_over_one_inventory_without_selling() -> None:
    opening, opening_market, cash_policy, raw_p0 = _p0_opening_compile()
    p0 = _root_geometry(raw_p0)

    # P0 is the only empty-chain open.  The fixture establishes the initial
    # target before any TAF or observation exists.
    assert opening["status"] == "ready_no_live"
    assert opening["plan"]["lineage"]["probability_stage"] == "P0"
    assert opening["plan"]["target_solution"]["target_shares_by_bucket"] == {
        bucket: 16.0 for bucket in BUCKETS
    }

    # Five shares per leg actually filled from that P0 open.  P0-M is a Bayes
    # update on the P0 root and sees those shares as the same inventory.
    p0m = _p0m(p0, _taf_artifact(), tx=39)
    assert dict(p0m.probabilities) == pytest.approx({
        "37": 0.10726138441163449,
        "38": 0.6011719434468592,
        "39": 0.2915666721415064,
    })
    first_market = _market_state(
        opening_market,
        filled={bucket: 5.0 for bucket in BUCKETS},
        filled_cost=P0_FILL_COST,
        pending={bucket: 0.0 for bucket in BUCKETS},
        identity="p0m-first",
    )
    p0m_compile = _compile_stage(
        envelopes=[p0, p0m], market=first_market, stage="P0-M",
        cash_policy=cash_policy, at="2026-07-23T05:30:00Z",
    )
    assert p0m_compile["status"] == "ready_no_live"
    assert p0m_compile["plan"]["lineage"]["probability_stage"] == "P0-M"
    assert p0m_compile["plan"]["target_solution"]["target_shares_by_bucket"] == (
        pytest.approx({bucket: 80.0 for bucket in BUCKETS})
    )
    for row in p0m_compile["plan"]["targets"]:
        assert row["filled_shares"] == 5.0
        assert row["pending_shares"] == 0.0
        assert row["delta_shares"] == pytest.approx(75.0)
    # Changing the probability stage did not grant a fresh 80-share claim:
    # every leg is target minus the five shares already bought at P0.

    # Once those exact gaps are confirmed pending, another compile creates no
    # duplicate order.  A live partial fill simply moves quantity from pending
    # to filled; filled + pending still occupies the whole target.
    pending_market = _market_state(
        opening_market,
        filled={bucket: 5.0 for bucket in BUCKETS},
        filled_cost=P0_FILL_COST,
        pending={bucket: 75.0 for bucket in BUCKETS},
        identity="p0m-pending",
    )
    pending_compile = _compile_stage(
        envelopes=[p0, p0m], market=pending_market, stage="P0-M",
        cash_policy=cash_policy, at="2026-07-23T05:31:00Z",
    )
    assert pending_compile["status"] == "hold"
    assert pending_compile["plan"]["targets"] == []
    assert pending_compile["plan"]["target_solution"]["target_shares_by_bucket"] == (
        pytest.approx({bucket: 80.0 for bucket in BUCKETS})
    )

    partial_fill = {bucket: 35.0 for bucket in BUCKETS}
    partial_cost = {
        bucket: P0_FILL_COST[bucket] + 30.0 * 0.05 for bucket in BUCKETS
    }
    partial_pending_market = _market_state(
        opening_market,
        filled=partial_fill,
        filled_cost=partial_cost,
        pending={bucket: 45.0 for bucket in BUCKETS},
        identity="p0m-partial-resting",
    )
    partial_pending_compile = _compile_stage(
        envelopes=[p0, p0m], market=partial_pending_market, stage="P0-M",
        cash_policy=cash_policy, at="2026-07-23T05:32:00Z",
    )
    assert partial_pending_compile["status"] == "hold"
    assert partial_pending_compile["plan"]["targets"] == []
    assert all(
        partial_fill[bucket] + 45.0 == pytest.approx(80.0)
        for bucket in BUCKETS
    )

    # When the unfilled remainder is authoritatively gone, the same solver
    # recomputes only the true residual: 80 - 35 - 0 = 45.
    terminal_partial_market = _market_state(
        opening_market,
        filled=partial_fill,
        filled_cost=partial_cost,
        pending={bucket: 0.0 for bucket in BUCKETS},
        identity="p0m-partial-terminal",
    )
    terminal_partial_compile = _compile_stage(
        envelopes=[p0, p0m], market=terminal_partial_market, stage="P0-M",
        cash_policy=cash_policy, at="2026-07-23T05:33:00Z",
    )
    assert terminal_partial_compile["status"] == "ready_no_live"
    for row in terminal_partial_compile["plan"]["targets"]:
        assert row["filled_shares"] == 35.0
        assert row["pending_shares"] == 0.0
        assert row["delta_shares"] == pytest.approx(45.0)

    # P1 is rebuilt from the immutable P0-M prediction root.  The first high
    # moves the centre up to 39; replaying the complete two-report prefix from
    # the same root then moves it back to 38 and kills bucket 37.
    observation_a = _observation(p0m, "a", "2026-07-23T06:00:00Z", 36.8)
    observation_b = _observation(p0m, "b", "2026-07-23T07:00:00Z", 37.6)
    likelihood = _with_block(_block())
    first_packets = compile_station_p1_packets(
        predecessor=p0m,
        observations=[observation_a],
        registry_rows=_registry(p0m),
        compilation_at_utc="2026-07-23T06:00:00Z",
        observation_likelihood_artifact=likelihood,
    )
    replay_packets = compile_station_p1_packets(
        predecessor=p0m,
        observations=[observation_a, observation_b],
        registry_rows=_registry(p0m),
        compilation_at_utc="2026-07-23T07:00:00Z",
        observation_likelihood_artifact=likelihood,
    )
    assert len(first_packets) == 1
    assert len(replay_packets) == 2
    first_p1 = ProbabilityEnvelope.from_mapping(
        first_packets[-1]["expected_envelope"]
    )
    final_p1 = ProbabilityEnvelope.from_mapping(
        replay_packets[-1]["expected_envelope"]
    )
    assert first_p1.center_bucket == "39"
    assert dict(first_p1.probabilities) == pytest.approx({
        "37": 0.1188190456006916,
        "38": 0.3064933289705227,
        "39": 0.5746876254287857,
    })
    assert final_p1.center_bucket == "38"
    assert dict(final_p1.probabilities) == pytest.approx({
        "37": 0.0,
        "38": 0.8334789166820085,
        "39": 0.1665210833179915,
    })
    assert replay_packets[-1]["hard_excluded_buckets"] == ["37"]
    assert first_packets[-1]["prediction_root"] == p0m.to_dict()
    assert all(
        packet["prediction_root"] == p0m.to_dict()
        for packet in replay_packets
    )
    assert final_p1.lineage["canonical_observation_ids"] == "obs-a,obs-b"

    # A same-temperature report and a later lower report may refresh the
    # weather-path belief, but the refresh says explicitly that the high did
    # not advance.  The two actual high-advance packets remain byte-identical.
    unchanged_packets = compile_station_p1_packets(
        predecessor=p0m,
        observations=[
            observation_a,
            observation_b,
            _observation(p0m, "c", "2026-07-23T07:30:00Z", 37.6),
            _observation(p0m, "d", "2026-07-23T08:00:00Z", 37.2),
        ],
        registry_rows=_registry(p0m),
        compilation_at_utc="2026-07-23T08:00:00Z",
        observation_likelihood_artifact=likelihood,
    )
    assert unchanged_packets[:2] == replay_packets
    assert len(unchanged_packets) == 3
    refresh_p1 = ProbabilityEnvelope.from_mapping(
        unchanged_packets[-1]["expected_envelope"]
    )
    assert refresh_p1.lineage["reported_high_advanced"] == "false"
    assert refresh_p1.lineage["canonical_observation_event_id"] == "obs-d"
    assert refresh_p1.lineage["hard_excluded_buckets"] == "37"

    p1_compile = _compile_stage(
        envelopes=[p0, p0m, final_p1],
        market=terminal_partial_market,
        stage="P1",
        cash_policy=cash_policy,
        at="2026-07-23T07:10:00Z",
    )
    assert p1_compile["status"] == "ready_no_live"
    p1_plan = p1_compile["plan"]
    assert p1_plan["final_support"] == ["38", "39"]
    assert p1_plan["target_solution"]["target_shares_by_bucket"] == pytest.approx({
        "37": 35.0,
        "38": 100.0,
        "39": 100.0,
    })
    assert set(_targets(p1_plan)) == {"38", "39"}
    assert all(row["delta_shares"] > 0.0 for row in p1_plan["targets"])
    # The hard-dead 37 inventory stays held at 35, and the posterior's move
    # away from 39 does not sell any of its 35 actual YES shares.

    refresh_compile = _compile_stage(
        envelopes=[p0, p0m, final_p1, refresh_p1],
        market=terminal_partial_market,
        stage="P1",
        cash_policy=cash_policy,
        at="2026-07-23T08:01:00Z",
        last_decision_signature=dict(
            p1_plan["delta_v2_audit"]["decision_moment"]
        ),
    )
    assert refresh_compile["status"] == "hold"
    assert refresh_compile["plan"]["targets"] == []
    refresh_moment = refresh_compile["plan"]["delta_v2_audit"][
        "decision_moment"
    ]
    assert refresh_moment["reported_high_advanced"] is False
    assert refresh_moment["probability_change_is_moment"] is False
    assert refresh_moment["is_decision_moment"] is False

    # The suppression is limited to the cooling report itself.  If an actual
    # fill changes holdings under that refreshed head, inventory movement is
    # still a decision moment and the residual is recomputed.
    after_fill = {bucket: 36.0 for bucket in BUCKETS}
    after_fill_cost = {
        bucket: partial_cost[bucket] + 0.05 for bucket in BUCKETS
    }
    fill_recompute = _compile_stage(
        envelopes=[p0, p0m, final_p1, refresh_p1],
        market=_market_state(
            opening_market,
            filled=after_fill,
            filled_cost=after_fill_cost,
            pending={bucket: 0.0 for bucket in BUCKETS},
            identity="p1-refresh-after-fill",
        ),
        stage="P1",
        cash_policy=cash_policy,
        at="2026-07-23T08:02:00Z",
        last_decision_signature=dict(
            p1_plan["delta_v2_audit"]["decision_moment"]
        ),
    )
    assert fill_recompute["status"] == "ready_no_live"
    assert set(_targets(fill_recompute["plan"])) == {"38", "39"}
    assert all(
        row["delta_shares"] == pytest.approx(64.0)
        for row in fill_recompute["plan"]["targets"]
    )
    fill_moment = fill_recompute["plan"]["delta_v2_audit"]["decision_moment"]
    assert fill_moment["probability_change_is_moment"] is False
    assert fill_moment["is_decision_moment"] is True


def test_a_new_p0_root_replays_p1_instead_of_downgrading_the_scope() -> None:
    _, _, _, raw_p0 = _p0_opening_compile()
    p0 = _root_geometry(raw_p0)
    old_p0m = _p0m(p0, _taf_artifact(), tx=39)
    observations = [
        _observation(old_p0m, "a", "2026-07-23T06:00:00Z", 36.8),
        _observation(old_p0m, "b", "2026-07-23T07:00:00Z", 37.6),
    ]
    likelihood = _with_block(_block())
    old_p1 = ProbabilityEnvelope.from_mapping(
        compile_station_p1_packets(
            predecessor=old_p0m,
            observations=observations,
            registry_rows=_registry(old_p0m),
            compilation_at_utc="2026-07-23T07:00:00Z",
            observation_likelihood_artifact=likelihood,
        )[-1]["expected_envelope"]
    )

    new_root_raw = p0.to_dict()
    new_root_raw.update({
        "checkpoint_id": "e2e-p0-reroot",
        "decision_at_utc": "2026-07-23T07:30:00Z",
        "fresh_until_utc": "2026-07-23T12:00:00Z",
    })
    new_p0 = ProbabilityEnvelope.from_mapping(new_root_raw)
    new_p0m = materialize_p0m_observed(
        new_p0,
        p0_root=new_p0,
        forecast_revision_id="taf-reroot",
        forecast_source_object_sha256="f" * 64,
        taf_tx_c=39.0,
        decision_at_utc="2026-07-23T07:40:00Z",
        fresh_until_utc="2026-07-23T09:00:00Z",
        bridge_sha256=new_p0.bridge_sha256,
        taf_likelihood_artifact=_taf_artifact(),
    )
    replayed = compile_station_p1_packets(
        predecessor=new_p0m,
        observations=observations,
        registry_rows=_registry(new_p0m),
        compilation_at_utc="2026-07-23T08:00:00Z",
        observation_likelihood_artifact=likelihood,
    )
    assert len(replayed) == 2
    rerooted_p1 = ProbabilityEnvelope.from_mapping(
        replayed[-1]["expected_envelope"]
    )
    assert rerooted_p1.stage == "P1"
    assert rerooted_p1.lineage["prediction_root_checkpoint_id"] == (
        new_p0m.checkpoint_id
    )
    assert rerooted_p1.lineage["p0_root_checkpoint_id"] == new_p0.checkpoint_id
    # The remaining-hours feature moved between the two roots, so a faithful
    # replay is allowed to change the posterior.  It must preserve the hard
    # truth and materialize a new P1 branch on the new root.
    assert rerooted_p1.center_bucket == old_p1.center_bucket == "38"
    assert rerooted_p1.probabilities["37"] == old_p1.probabilities["37"] == 0.0
    assert rerooted_p1.content_sha256 != old_p1.content_sha256
    assert select_current_stage(
        [p0, old_p0m, old_p1, new_p0, new_p0m, rerooted_p1],
        at_utc="2026-07-23T08:00:00Z",
    ).checkpoint_id == rerooted_p1.checkpoint_id


def test_dead_buy_is_cancelled_and_unknown_execution_blocks_the_next_intent() -> None:
    # Put a real resting BUY on this replay's ZUUU/37 scope.  Once P1's 37.6C
    # observation makes 37 mathematically dead, the lifecycle cancels it
    # before consulting a book.  Existing filled inventory is untouched.
    conn = resting_fixture._ledger()
    conn.execute(
        "UPDATE basket_target_execution_cycles SET station_id=?,event_date=?,"
        "settlement_timezone=?",
        ("ZUUU", "2026-07-23", "Asia/Shanghai"),
    )
    conn.execute(
        "UPDATE basket_target_order_intents SET bucket=?,token_id=?",
        ("37", "yes-37"),
    )
    conn.commit()
    client = resting_fixture._Client([
        resting_fixture._live(),
        {"ok": True, "status": "cancelled", "response": {"size_matched": 0}},
    ])

    def no_book(*_args, **_kwargs):
        raise AssertionError("a dead bucket must be cancelled before book read")

    cancelled = resting_lane.reprice_resting_rungs(
        conn,
        as_of_utc="2026-07-23T07:00:10Z",
        client=client,
        book_loader=no_book,
        dead_bucket_loader=lambda *_args, **_kwargs: (("37",), 37.6),
    )
    assert cancelled["cancel_requested_dead_bucket"] == 1
    assert cancelled["cancel_confirmed"] == 1
    assert cancelled["rungs"][0]["verdict"]["reason"] == (
        "bucket_dead_by_observed_high"
    )
    assert resting_fixture._state(conn) == (
        "cancelled", "resolved_cancelled", 0.0
    )

    # A POST whose exchange outcome is unknown takes the real execution
    # transition to reconcile_required, cancels the unsubmitted sibling, and
    # leaves the same bucket/token gate shut.  No guessed second intent exists.
    execution_conn, vector, intent_id = transition_fixture._db()
    uncertain = apply_execution_receipt(
        execution_conn,
        transition_fixture._receipt(vector, intent_id, "POST_UNKNOWN"),
    )
    assert uncertain["state_after"] == "RECONCILE_REQUIRED"
    assert uncertain["intent_state"] == "reconcile_required"
    assert execution_conn.execute(
        "SELECT state FROM basket_target_execution_cycles"
    ).fetchone()[0] == "reconcile_required"
    assert execution_conn.execute(
        "SELECT COUNT(*) FROM basket_target_order_intents WHERE state='prepared'"
    ).fetchone()[0] == 0
    gate = can_submit_next_order(
        execution_conn,
        bucket="38",
        token_id="token-0",
    )
    assert gate["allowed"] is False
    assert gate["reason"] == "same_bucket_unknown_or_pending"
    assert gate["blockers"][0]["intent_state"] == "reconcile_required"
