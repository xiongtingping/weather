"""Charter 84 (user ruling, 2026-09-05): a seat the venue left short is completed.

First live morning: ZGSZ 2026-09-06 filled 30°C and 31°C at five shares and
the venue refused 32°C under its dollar minimum.  With the holdings visible
the Kelly law sized the seat at 0.75 USDC against 3.4 held and offered
nothing, the grid's slivers failed the scenario floor, and the plan read
``target_inventory_satisfied``.  The candidate domain had no "buy the third
rung at the size the other two were bought at".  Now it does, and it is
judged by the same floor, the same increment edge, the same lot and dollar
lift as any other candidate.
"""
from __future__ import annotations

from dataclasses import replace
import inspect as _inspect
import pytest

from bayes_basket_target.basket_kernel import delta_v2 as K

from bayes_basket_target import delta_v2_runtime
from bayes_basket_target.basket_kernel.contracts import (
    ContractError,
    TargetBasketSnapshot,
)
from bayes_basket_target.basket_kernel.delta_v2 import (
    CANDIDATE_KIND_BASELINE,
    CANDIDATE_KIND_COMPLETE,
    KELLY_CANDIDATE_ORIGIN,
    LADDER_BALANCE_CANDIDATE_ORIGIN,
    ladder_balance_targets_v1,
    support_scenario_floor_v1,
    completion_floor_delta_v1,
    completion_repair_exemption_v1,
)
from bayes_basket_target.delta_v2_runtime import (
    _assert_candidate_support_contract,
    _target_candidates,
    compile_delta_v2_runtime_plan,
)
from tests.test_bayes_basket_target_delta_v2 import _value_weights
from tests.test_bayes_basket_target_delta_v2_runtime import (
    _basket_policy,
    _delta_policy,
    _fill_resolver,
)



@pytest.fixture(autouse=True)
def _charter_81_arm_on(monkeypatch):
    """Charter 81 (the centre-window claim lift) was retired 2026-09-12 and
    the shipped default is identity.  The station-day fixtures in this file
    were written under the lift -- their raw claims clear the whole-basket
    positive-EV gate only when lifted -- and what they assert is charter
    84/121/125 mechanics, not the lift.  The retired table is switched on
    for the file so the fixtures keep exercising those mechanics; a fixture
    rebuilt on raw claims can drop this."""
    import bayes_basket_target.basket_kernel.delta_v2 as _k81
    monkeypatch.setattr(
        _k81, "CENTER_WINDOW_CLAIM_BANDS",
        _k81._parse_claim_bands(_k81.CENTER_WINDOW_CLAIM_RETIRED_TABLE),
    )

def _held_snapshot(*, held_32: float = 0.0, ask_32: float = 0.15) -> TargetBasketSnapshot:
    """ZGSZ 2026-09-06 as the ledger saw it: 30°C and 31°C held, 32°C empty."""

    def bucket(name: str, ordinal: int, probability: float, ask: float,
               held: float, cost: float) -> dict:
        return {
            "bucket": name,
            "ordinal": ordinal,
            "probability": probability,
            "bid_price": max(0.01, ask - 0.02),
            "bid_levels": [{"price": max(0.01, ask - 0.02), "shares": 50.0}],
            "ask_levels": [{"price": ask, "shares": 100.0}],
            "price_tick_size": 0.01,
            "maker_queue_ahead_shares": 0.0,
            "maker_queue_known": True,
            "min_order_size": 5.0,
            "filled_shares": held,
            "filled_cost_usdc": cost,
        }

    return TargetBasketSnapshot.from_mapping({
        "schema": "target_basket_snapshot_v6",
        "snapshot_id": "rkpk-half-basket",
        "sequence": 1,
        "observed_at": "2026-07-28T03:00:00Z",
        "market_id": "rkpk-market",
        # The fixture policy is RKPK's; the shape is ZGSZ 2026-09-06's.
        "station_id": "RKPK",
        "event_date": "2026-07-28",
        "quote_version": "quote-v1",
        "quote_observed_at": "2026-07-28T03:00:00Z",
        "quote_source": "OFFICIAL_CLOB_WS",
        "inventory_version": "p0_preselection_ledger_test",
        "order_state_version": "order-v1",
        "evidence": {
            "probability_stage": "P0",
            "posterior_version": "p0-v1",
            "posterior_sha256": "a" * 64,
        },
        "buckets": [
            # A symmetric day around 31°C, so the seat is the centre and its
            # two neighbours (charter 79), and the window's claim (0.90)
            # clears its all-in offer (0.80).  30°C and 31°C were bought
            # earlier at 0.25 and 0.43; the book has since moved.
            bucket("29", 29, 0.05, 0.06, 0.0, 0.0),
            bucket("30", 30, 0.20, 0.20, 5.0, 1.25),
            bucket("31", 31, 0.50, 0.45, 5.0, 2.15),
            bucket("32", 32, 0.20, ask_32, held_32, held_32 * ask_32),
            bucket("33", 33, 0.05, 0.06, 0.0, 0.0),
        ],
        "checkpoint_changed": True,
        "quote_changed": True,
        "fill_changed": True,
        "time_gate_changed": True,
        "evidence_fresh": True,
        "contract_matches": True,
        "order_state_known": True,
        "risk_inputs_complete": True,
        "time_left_minutes": 1440.0,
    })


def _candidates(snapshot: TargetBasketSnapshot, *, desired=("30", "31", "32")):
    return _target_candidates(
        snapshot=snapshot,
        desired=desired,
        artifact=_delta_policy(),
        capital_available_usdc=10.0,
        minimum_clip_notional_usdc=0.01,
        bankroll_usdc=_basket_policy().total_budget_usdc,
        probabilities_by_bucket={
            row.bucket: row.probability for row in snapshot.buckets
        },
        book_equity_usdc=514.29,
    )


def _tilted_seat(monkeypatch) -> None:
    """Compile these seats under the consolidation tilt they were opened with.

    The fixtures are the 2026-09-05 half-baskets (ZGSZ, LTAC), seated while
    the tilt cut the capital; at that capital the whole-basket Kelly row
    cannot form and the completion is what is under test.  The tilt is
    retired as the default (charter 139, factor 1), where the same book
    hands Kelly enough capital to take the seat first -- charter 84's own
    ordering.  So the research arm is pinned here, lamp and depth both: a
    test of the completion must not re-break when a sizing ruling moves.
    """

    monkeypatch.setattr(
        delta_v2_runtime, "CONSOLIDATION_TILT_SIGNALS", ("stable", "sharp")
    )
    monkeypatch.setattr(delta_v2_runtime, "CONSOLIDATION_TILT_FACTOR", 0.05)


def test_the_balance_shape_raises_every_short_rung_to_the_held_level() -> None:
    assert ladder_balance_targets_v1(
        held_by_bucket={"30": 5.0, "31": 5.0, "32": 0.0}, support=("30", "31", "32")
    ) == {"30": 5.0, "31": 5.0, "32": 5.0}
    assert ladder_balance_targets_v1(
        held_by_bucket={"30": 5.0, "31": 3.0}, support=("30", "31", "32")
    ) == {"30": 5.0, "31": 5.0, "32": 5.0}
    # A whole ladder, and an empty one, have nothing to balance.
    assert ladder_balance_targets_v1(
        held_by_bucket={"30": 5.0, "31": 5.0, "32": 5.0}, support=("30", "31", "32")
    ) == {}
    assert ladder_balance_targets_v1(
        held_by_bucket={}, support=("30", "31", "32")
    ) == {}
    with pytest.raises(ContractError, match="delta_v2_balance_support_duplicate"):
        ladder_balance_targets_v1(held_by_bucket={"30": 5.0}, support=("30", "30"))


# Charter 96 (2026-09-06) turned P0's Kelly candidate back on, which exposed
# a defect that is older than it and has nothing to do with P0: the kernel's
# winner chain is ``kelly_rows`` > ``balance_rows`` with no
# stage in it, so a completion is discarded the moment ANY Kelly row is
# offered -- and charter 93 (2026-09-06) then refuses that Kelly row after
# the lift, leaving the day on HOLD with the ladder still short.  Measured on
# ``_held_snapshot``: Kelly wants 12.5 shares a rung for 10.15 USDC, charter
# 93 refuses it (``support_floor_after_lift``), and the 0.75 USDC completion
# that would have passed was dropped at ranking time.  P0-M and P1 have
# generated Kelly rows since charter 84 landed, so they have been losing
# completions this way all along.
#
# The fix is a re-rank after the post-lift floor, ~2000 lines downstream of
# the solver, and it needs its own measurement and ruling.  Until then these
# two tests pin BOTH arms so the defect cannot be forgotten and either side
# changing is a failure rather than a silence.
def test_a_venue_short_seat_gets_a_completion_candidate_and_nothing_else_does() -> None:
    candidates = _candidates(_held_snapshot())
    balance = [
        row for row in candidates
        if row["candidate_origin"] == LADDER_BALANCE_CANDIDATE_ORIGIN
    ]
    assert len(balance) == 1
    row = balance[0]
    assert row["candidate_kind"] == CANDIDATE_KIND_COMPLETE
    # The held rungs are left exactly as they are; the short rung is bought
    # to their level down the posted offer.
    assert row["shares_by_bucket"]["30"] == pytest.approx(5.0)
    assert row["shares_by_bucket"]["31"] == pytest.approx(5.0)
    assert row["shares_by_bucket"]["32"] == pytest.approx(5.0)
    assert row["cost_by_bucket"]["30"] == pytest.approx(1.25)
    assert row["cost_by_bucket"]["32"] == pytest.approx(0.75)
    balance_audit = dict(row["ladder_balance"])
    # Recorded alongside, never gated: the completion's other price.
    floor_delta = balance_audit.pop("floor_delta")
    assert balance_audit == {
        "level_shares": 5.0, "short_buckets": ["32"],
        "spend_usdc": pytest.approx(0.75), "full_spend_usdc": pytest.approx(0.75),
        # Constant from charter 120: all-or-none on depth and on cash, so
        # what it spends is what the level costs and the scale is one.
        "capital_scale": 1.0,
        "level_rule": "depth_priced_cheapest_floor_legal_held_level_v2",
    }
    assert floor_delta["schema"] == "completion_floor_delta_v1"
    # Buying the short rung is what lifts the worst in-support settlement off
    # minus-the-premium; that is the number no gate on this path reads.
    assert floor_delta["floor_before"] < 0.0 < floor_delta["floor_after"]
    assert floor_delta["floor_delta"] > 0.0
    # Charter 120 (2026-09-08) reversed this.  A completion that does not fit
    # the plan's capital whole used to be scaled to what fits; it is now
    # refused, because a scaled completion is the short ladder it was meant
    # to repair, and the floor refuses that shape anyway.  All-or-none, on
    # cash exactly as on depth.
    assert not [
        row for row in _target_candidates(
            snapshot=_held_snapshot(),
            desired=("30", "31", "32"),
            artifact=_delta_policy(),
            capital_available_usdc=0.30,
            minimum_clip_notional_usdc=0.01,
            bankroll_usdc=_basket_policy().total_budget_usdc,
            probabilities_by_bucket={
                row.bucket: row.probability for row in _held_snapshot().buckets
            },
            book_equity_usdc=514.29,
        )
        if row["candidate_origin"] == LADDER_BALANCE_CANDIDATE_ORIGIN
    ]
    # Charter 84's stated reason for this candidate -- "the Kelly law offered
    # nothing because the seat already holds more than it would size today" --
    # is FALSE on this shape, and only looked true while P0 could be sized
    # by a flat ticket instead of Kelly (retired 2026-09-08).  Kelly offers
    # 12.5 shares a rung here, well above the 5 held; what the completion
    # actually covers is a Kelly row that charter 93 refuses after the lift.
    kelly_rows = [
        row for row in candidates if row["candidate_origin"] == KELLY_CANDIDATE_ORIGIN
    ]
    assert len(kelly_rows) == 1
    assert kelly_rows[0]["shares_by_bucket"]["30"] == pytest.approx(12.5)
    # A whole ladder offers no completion.
    assert not [
        row for row in _candidates(_held_snapshot(held_32=5.0))
        if row["candidate_origin"] == LADDER_BALANCE_CANDIDATE_ORIGIN
    ]


def test_the_completion_is_all_or_none_and_switchable(monkeypatch: pytest.MonkeyPatch) -> None:
    # A short rung without an offer cannot be completed, so nothing is.
    raw = _held_snapshot().to_dict()
    raw["buckets"][3]["ask_levels"] = []
    raw["buckets"][3]["ask_available"] = False
    assert not [
        row for row in _candidates(TargetBasketSnapshot.from_mapping(raw))
        if row["candidate_origin"] == LADDER_BALANCE_CANDIDATE_ORIGIN
    ]
    monkeypatch.setattr(delta_v2_runtime, "CHARTER_84_LADDER_BALANCE", "off")
    assert not [
        row for row in _candidates(_held_snapshot())
        if row["candidate_origin"] == LADDER_BALANCE_CANDIDATE_ORIGIN
    ]


def test_the_solution_says_a_completion_was_offered_and_does_not_hash_it() -> None:
    """The instrument the charter-84 hunt needs, and the churn it must avoid.

    A plan used to say only who won and how many candidates ran, so a
    completion that was offered and then shadowed by a Kelly row could not be
    screened for at all -- which is why the loss went unseen on P0-M and P1.
    The counts are audit only and must stay OUTSIDE the hashed identity: put
    them in and every plan on the book re-shas for a field no rule reads.
    """

    from bayes_basket_target.contracts import content_sha256

    solution = compile_delta_v2_runtime_plan(
        snapshot=_held_snapshot(), basket_policy=_basket_policy(),
        delta_policy=_delta_policy(), fill_resolver=_fill_resolver,
        capital_available_usdc=10.0, minimum_profit_floor=-5.0,
        resource_probe_ok=True, book_equity_usdc=514.29,
    )["target_solution"]

    audit_only = {
        "candidate_origin_counts",
        "floor_refused_origin_counts",
        "shadowed_completion",
        "completion_floor_refused",
        "content_sha256",
    }
    assert solution["candidate_origin_counts"][
        LADDER_BALANCE_CANDIDATE_ORIGIN
    ] == 1
    # The Kelly row takes the seat, so the completion was offered and lost --
    # the exact case that had no name until now.
    assert solution["shadowed_completion"] is True
    # ...and it was floor-legal when it lost, which is what makes it
    # shadowing rather than a refusal.
    assert solution["completion_floor_refused"] is False
    identity = {k: v for k, v in solution.items() if k not in audit_only}
    assert content_sha256(identity) == solution["content_sha256"]


def test_a_completion_the_floor_refused_is_not_reported_as_shadowed() -> None:
    """The two ways a completion fails to be bought are different defects.

    Shadowed: the completion was floor-legal and the winner chain preferred
    another row (charter 84's own loss, repaired by reordering the chain).
    Refused: charter 93's in-support floor removed it before the chain ran.
    The first version of this flag reported both as shadowing, and over
    2026-09-06..08 that was 45 refusals against 3 real shadowings -- the
    screen the hunt runs on, wrong by 15x.

    Driven at the kernel, because the runtime can no longer produce the
    refused arm: since charter 120 the level search prices the completion on
    the same ladder the build walks, so a completion it offers is one the
    floor has already cleared.  The flag still has to tell the two apart --
    the kernel is a library and a caller may hand it any candidate set.
    """

    completion = {
        "candidate_id": "completion",
        # In-support and losing: settle on "a" and the basket pays 1 against
        # 4 spent, which is exactly what charter 93's floor is for.
        "shares_by_bucket": {"a": 1.0, "b": 4.0},
        "cost_by_bucket": {"a": 1.0, "b": 3.0},
        "candidate_kind": CANDIDATE_KIND_COMPLETE,
        "candidate_origin": LADDER_BALANCE_CANDIDATE_ORIGIN,
    }
    hold = {
        "candidate_id": "hold",
        "shares_by_bucket": {},
        "cost_by_bucket": {},
        "candidate_kind": CANDIDATE_KIND_BASELINE,
        "candidate_origin": "CLIP_GRID_PRODUCT",
    }
    solution = K.solve_target_inventory_v2(
        probabilities={"a": 0.2, "b": 0.8},
        feasible_inventory_candidates=[hold, completion],
        protected_buckets=["a", "b"],
        shortfall_buckets=[],
        breakthrough_buckets=[],
        weights=_value_weights(),
    )

    assert solution["candidate_origin_counts"][
        LADDER_BALANCE_CANDIDATE_ORIGIN
    ] == 1
    assert solution["floor_refused_origin_counts"][
        LADDER_BALANCE_CANDIDATE_ORIGIN
    ] == 1
    assert solution["completion_floor_refused"] is True
    assert solution["shadowed_completion"] is False


def test_the_search_prices_the_ladder_the_build_will_walk() -> None:
    """Charter 120 (user ruling 2026-09-08: "B+A").

    The level search used to price a gap at the first ask while the build
    walked the book with the cash that price implied.  A rung offering one
    share at 0.15 and the rest at 0.90 turned a certified five-share
    completion into 1.7 shares, and charter 93's floor then refused the
    under-completed basket the search had just cleared: 45 of the 78
    completions offered over 2026-09-06..08 died exactly there.

    Both halves are pinned.  B: the thin book is now judged on what five
    shares really cost (3.75, not 0.75), finds no legal level and offers
    nothing.  A: a book deep enough to fill the level still offers, at the
    price the walk will charge -- which is the middle ground all-or-none
    alone would have thrown away.
    """

    def _with_levels(levels: list[dict[str, float]]) -> TargetBasketSnapshot:
        raw = _held_snapshot().to_dict()
        raw["buckets"][3]["ask_levels"] = levels
        return TargetBasketSnapshot.from_mapping(raw)

    def _completion(snapshot: TargetBasketSnapshot) -> dict | None:
        rows = [
            row for row in _candidates(snapshot)
            if row["candidate_origin"] == LADDER_BALANCE_CANDIDATE_ORIGIN
        ]
        assert len(rows) <= 1
        return rows[0] if rows else None

    # B: one share at the posted ask and the rest four times dearer.  Five
    # shares cost 3.75, the completed basket costs 7.15 against a five-share
    # payout, and no held level is legal -- so nothing is offered.
    assert _completion(_with_levels([
        {"price": 0.15, "shares": 1.0}, {"price": 0.90, "shares": 100.0},
    ])) is None

    # A: deep enough to fill the level, and priced on the walk rather than on
    # the first ask -- 3 at 0.15 plus 2 at 0.20 is 0.85, not 5 x 0.15.
    row = _completion(_with_levels([
        {"price": 0.15, "shares": 3.0}, {"price": 0.20, "shares": 100.0},
    ]))
    assert row is not None
    assert row["shares_by_bucket"]["32"] == pytest.approx(5.0)
    assert sum(row["cost_by_bucket"].values()) == pytest.approx(4.25)
    # All-or-none: what it spends is what the level costs, so the scale the
    # receipt used to carry is now always one.
    assert row["ladder_balance"]["capital_scale"] == 1.0
    assert row["ladder_balance"]["spend_usdc"] == pytest.approx(0.85)
    assert row["ladder_balance"]["full_spend_usdc"] == pytest.approx(0.85)

    # A, on cash rather than depth: the same deep book with less money than
    # the level costs is refused whole, not bought in part.
    assert not [
        row for row in _target_candidates(
            snapshot=_with_levels([{"price": 0.15, "shares": 100.0}]),
            desired=("30", "31", "32"),
            artifact=_delta_policy(),
            capital_available_usdc=0.10,
            minimum_clip_notional_usdc=0.01,
            bankroll_usdc=_basket_policy().total_budget_usdc,
            probabilities_by_bucket={
                row.bucket: row.probability
                for row in _with_levels(
                    [{"price": 0.15, "shares": 100.0}]
                ).buckets
            },
            book_equity_usdc=514.29,
        )
        if row["candidate_origin"] == LADDER_BALANCE_CANDIDATE_ORIGIN
    ]


def test_the_depth_cost_walk_answers_none_when_the_book_is_short() -> None:
    """The kernel half, on its own: a ladder that cannot supply the quantity
    says so rather than returning what it can.  That distinction is the whole
    repair -- the cash-limited walk had no way to say "short"."""

    ladder = [{"price": 0.15, "shares": 1.0}, {"price": 0.90, "shares": 2.0}]
    assert K.ask_depth_cost_v1(levels=ladder, shares=0.0) == 0.0
    assert K.ask_depth_cost_v1(levels=ladder, shares=1.0) == pytest.approx(0.15)
    assert K.ask_depth_cost_v1(levels=ladder, shares=3.0) == pytest.approx(1.95)
    assert K.ask_depth_cost_v1(levels=ladder, shares=3.5) is None
    # A level with no price or no size is skipped, not treated as free.
    assert K.ask_depth_cost_v1(
        levels=[{"price": 0.0, "shares": 5.0}, {"price": 0.20, "shares": 5.0}],
        shares=2.0,
    ) == pytest.approx(0.40)


def test_the_seated_projection_carries_the_lambda_axis_it_was_priced_on() -> None:
    """Charter 97's retirement condition depends on a field a whitelist ate.

    The gate stamps ``effective_edge_lambda_source`` on every priced row, but
    ``_solve_over_support_windows`` projects seated and considered key by key,
    and the field was not on the list -- so every plan reported a lambda with
    no axis beside it.  The clause retires itself when the seats the
    claim-edge band opened realise under one; with the source dropped, that
    set could never have gained a member and the condition could never have
    fired.  Reported from the plan side, 2026-09-06.

    Pinned on both projections because they are two separate whitelists and
    fixing one is what left the other broken in the first place.
    """

    result = compile_delta_v2_runtime_plan(
        snapshot=_held_snapshot(), basket_policy=_basket_policy(),
        delta_policy=_delta_policy(), fill_resolver=_fill_resolver,
        capital_available_usdc=10.0, minimum_profit_floor=-5.0,
        resource_probe_ok=True, book_equity_usdc=514.29,
    )

    def find(node):
        if isinstance(node, dict):
            if "basket_support_selection" in node:
                return node["basket_support_selection"]
            for value in node.values():
                found = find(value)
                if found is not None:
                    return found
        elif isinstance(node, list):
            for value in node:
                found = find(value)
                if found is not None:
                    return found
        return None

    selection = find(result)
    assert selection is not None
    seated = selection["seated"]
    assert seated["effective_edge_lambda"] is not None
    assert seated["effective_edge_lambda_source"] in {
        "agreement_band", "claim_edge_thin_band",
    }
    for row in selection["considered"]:
        if row.get("effective_edge_lambda") is None:
            continue
        assert row["effective_edge_lambda_source"] in {
            "agreement_band", "claim_edge_thin_band",
        }


def test_the_support_contract_is_atomic_at_the_result() -> None:
    baseline = {
        "candidate_id": "base", "candidate_kind": CANDIDATE_KIND_BASELINE,
        "shares_by_bucket": {"30": 5.0, "31": 5.0, "32": 0.0},
    }
    completion = {
        "candidate_id": "balance", "candidate_kind": CANDIDATE_KIND_COMPLETE,
        "shares_by_bucket": {"30": 5.0, "31": 5.0, "32": 5.0},
    }
    accepted = _assert_candidate_support_contract(
        [baseline, completion], ordered_support=("30", "31", "32")
    )
    assert [row["candidate_id"] for row in accepted] == ["base", "balance"]
    # A basket that leaves an unheld rung unbought is still incomplete, and
    # one that adds nothing is not an action.
    for shares in (
        {"30": 6.0, "31": 6.0, "32": 0.0},
        {"30": 5.0, "31": 5.0, "32": 0.0},
    ):
        with pytest.raises(
            ContractError, match="delta_v2_candidate_complete_basket_incomplete"
        ):
            _assert_candidate_support_contract(
                [baseline, {**completion, "shares_by_bucket": shares}],
                ordered_support=("30", "31", "32"),
            )


def test_charter_127_the_designated_completion_crosses_as_a_limit_fok(
    monkeypatch,
) -> None:
    """The completion is the one action that takes, and it takes or it does not.

    A completion exists because the ladder is already broken: the seat holds
    some legs and not the short one, and until that leg is bought the basket
    loses on the outcome it was built to cover.  Posting it to rest answers
    that with "perhaps, eventually", and eventually is measured: of the legs
    a seat wanted, truth landed inside the intended basket 60.7% of the time
    against 39.8% inside the basket that actually filled.

    The price it may pay is the one its own authorization was struck on --
    the lifted cost charter 123 re-read the edge on -- and the limit it sends
    is the deepest level that quantity consumes, because a take priced at the
    top of book for a quantity the top of book cannot supply is killed by
    construction.
    """

    _tilted_seat(monkeypatch)
    result = compile_delta_v2_runtime_plan(
        snapshot=_held_snapshot(),
        basket_policy=_basket_policy(),
        delta_policy=_delta_policy(),
        fill_resolver=_fill_resolver,
        capital_available_usdc=10.0,
        minimum_profit_floor=-5.0,
        resource_probe_ok=True,
        book_equity_usdc=514.29,
    )

    assert result["target_solution"]["winner_candidate_origin"] == (
        LADDER_BALANCE_CANDIDATE_ORIGIN
    )
    short = [
        row for row in result["targets"]
        if float(row.get("delta_shares") or 0.0) > 0.0
    ]
    assert [row["bucket"] for row in short] == ["32"]
    assert short[0]["execution_candidate"] == "TAKER_FOK"
    assert short[0]["post_only"] is False


def test_charter_127_off_leaves_the_completion_resting(monkeypatch) -> None:
    """The arm that restores what every completion was posted as."""

    _tilted_seat(monkeypatch)
    monkeypatch.setattr(
        delta_v2_runtime, "CHARTER_127_COMPLETION_TAKE", "off"
    )
    result = compile_delta_v2_runtime_plan(
        snapshot=_held_snapshot(),
        basket_policy=_basket_policy(),
        delta_policy=_delta_policy(),
        fill_resolver=_fill_resolver,
        capital_available_usdc=10.0,
        minimum_profit_floor=-5.0,
        resource_probe_ok=True,
        book_equity_usdc=514.29,
    )

    short = [
        row for row in result["targets"]
        if float(row.get("delta_shares") or 0.0) > 0.0
    ]
    assert [row["execution_candidate"] for row in short] == ["MAKER"]
    assert short[0]["post_only"] is True


def test_a_completion_that_would_walk_past_its_authorization_is_killed(
    monkeypatch,
) -> None:
    """Above its authorization the completion buys nothing, by name.

    The gates price the lot raise at the top of book (charter 93's lifted
    cost, which charter 123 then re-reads the edge on); a take has to walk
    the ladder for the whole quantity.  A book offering one share at the
    quoted price and the rest far dearer is the case where those two numbers
    part company, and it is the case charter 120 named on the other side of
    the same lane: the certified five-share completion that bought 1.7.

    Resting, the leg would post at the anchor and wait.  Taking, it either
    gets the whole quantity inside what authorized it or it gets nothing --
    and nothing is the right answer, because the basket the deeper walk would
    buy is not the basket any gate saw.
    """

    _tilted_seat(monkeypatch)
    snapshot = _held_snapshot()
    thin = type(snapshot.buckets[0].ask_levels[0])
    dearer = []
    for row in snapshot.buckets:
        if row.bucket == "32":
            row = replace(
                row,
                ask_levels=(
                    thin(price=0.15, shares=1.0),
                    thin(price=0.90, shares=100.0),
                ),
            )
        dearer.append(row)
    result = compile_delta_v2_runtime_plan(
        snapshot=replace(snapshot, buckets=tuple(dearer)),
        basket_policy=_basket_policy(),
        delta_policy=_delta_policy(),
        fill_resolver=_fill_resolver,
        capital_available_usdc=10.0,
        minimum_profit_floor=-5.0,
        resource_probe_ok=True,
        book_equity_usdc=514.29,
    )

    assert not [
        row for row in result["targets"]
        if float(row.get("delta_shares") or 0.0) > 0.0
        and str(row.get("execution_candidate") or "") != "NONE"
    ]


def test_the_half_basket_compiles_to_its_missing_rung_at_the_seats_capital(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole chain on the ZGSZ shape: the solver seats the completion
    under the Kelly form, the floor accepts it, the increment edge authorizes
    it, and only the missing rung carries a gap, sized against the capital
    the seat was opened with rather than the tilt-cut capital of the day."""

    # Charter 84 (八) is what this test is about, not which lamp triggers the
    # tilt.  The trigger moved to ``stable`` alone on 2026-09-08 and these
    # fixtures are ``sharp`` shapes, so pin the signal set here: a test of the
    # completion exemption must not re-break every time the lamp ruling moves.
    _tilted_seat(monkeypatch)
    result = compile_delta_v2_runtime_plan(
        snapshot=_held_snapshot(),
        basket_policy=_basket_policy(),
        delta_policy=_delta_policy(),
        fill_resolver=_fill_resolver,
        capital_available_usdc=10.0,
        minimum_profit_floor=-5.0,
        resource_probe_ok=True,
        book_equity_usdc=514.29,
    )

    solution = result["target_solution"]
    # Charter 121 (2026-09-08) closed the defect this test used to pin open.
    # The Kelly row still takes the seat first and charter 93 still refuses it
    # after the lift; what changed is that the refusal now excludes that row
    # and the seat is solved again, so ``kelly_rows`` empties and charter 84's
    # completion becomes reachable.  Traced on this fixture: attempt 1 seats
    # WHOLE_BASKET_KELLY and the lifted shape fails the floor at -0.70 against
    # a cost of 7.70; attempt 2, with that row excluded, seats LADDER_BALANCE
    # and carries no reason code at all.
    assert solution["winner_candidate_origin"] == LADDER_BALANCE_CANDIDATE_ORIGIN
    assert solution["winner_candidate_kind"] == CANDIDATE_KIND_COMPLETE
    assert result["basket_target_activated"] is True
    assert result["state"] != "HOLD"
    assert result["delta_v2_action_selection"]["authorized"] is True
    targets = {row["bucket"]: row for row in result["targets"]}
    assert set(result["execution_gap_support"]) == {"32"}
    # (八): the completion is sized against the capital the seat was opened
    # with, so the plan-level dollar lift (charter 83) can afford the rung:
    # five shares at 0.15 is $0.75, raised to seven for $1.05.
    assert targets["32"]["delta_shares"] == pytest.approx(7.0)
    assert "32" in result["notional_raised_buckets"]
    tilt = result["delta_v2_audit"]["consolidation_tilt"]
    assert tilt["completion_exempt"] is True
    assert tilt["capital_after_usdc"] == pytest.approx(10.0)


def test_charter_121_off_reproduces_the_refusal_that_used_to_hold_the_day(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pre-ruling arm, kept so the change has a rollback and a control.

    Same fixture as the half-basket test above: with the re-solve off, the
    Kelly row takes the seat, charter 93's post-lift floor refuses it, and the
    day holds with the completion never asked -- which is what production did
    on P0-M and P1 for the three weeks after charter 84 landed.
    """

    _tilted_seat(monkeypatch)
    monkeypatch.setattr(delta_v2_runtime, "CHARTER_121_POST_LIFT_RERANK", "off")
    result = compile_delta_v2_runtime_plan(
        snapshot=_held_snapshot(),
        basket_policy=_basket_policy(),
        delta_policy=_delta_policy(),
        fill_resolver=_fill_resolver,
        capital_available_usdc=10.0,
        minimum_profit_floor=-5.0,
        resource_probe_ok=True,
        book_equity_usdc=514.29,
    )
    assert (
        result["target_solution"]["winner_candidate_origin"]
        == KELLY_CANDIDATE_ORIGIN
    )
    assert result["state"] == "HOLD"
    assert "support_floor_after_lift" in result["reason_codes"]
    assert result["execution_gap_support"] == []


def test_charter_121_exclusions_reach_the_kernel_on_every_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The retry is only a retry if the exclusion actually travels.

    Written because the first cut threaded ``excluded_candidate_ids`` into the
    window sweep and into the kernel and then did not pass it at the one call
    site inside the loop, so every attempt re-solved an identical problem and
    the Kelly row won all four of them.  Nothing downstream could tell the
    difference from the clause being off, which is precisely the class of
    defect a behavioural assertion alone does not catch.
    """

    _tilted_seat(monkeypatch)
    seen: list[tuple[str, ...]] = []
    original = delta_v2_runtime._solve_over_support_windows

    def _spy(**kwargs):
        seen.append(tuple(kwargs.get("excluded_candidate_ids") or ()))
        return original(**kwargs)

    monkeypatch.setattr(
        delta_v2_runtime, "_solve_over_support_windows", _spy
    )
    result = compile_delta_v2_runtime_plan(
        snapshot=_held_snapshot(),
        basket_policy=_basket_policy(),
        delta_policy=_delta_policy(),
        fill_resolver=_fill_resolver,
        capital_available_usdc=10.0,
        minimum_profit_floor=-5.0,
        resource_probe_ok=True,
        book_equity_usdc=514.29,
    )
    assert len(seen) == 2, seen
    assert seen[0] == ()
    # The refused Kelly row, by id, on the second pass -- not merely a second
    # pass that happens to land somewhere else.
    assert len(seen[1]) == 1 and seen[1][0].startswith("kelly-")
    assert (
        result["target_solution"]["winner_candidate_origin"]
        == LADDER_BALANCE_CANDIDATE_ORIGIN
    )


def test_the_plan_level_lift_binds_on_the_increment_not_on_what_is_held() -> None:
    from bayes_basket_target.basket_kernel.delta_v2 import raise_sub_lot_targets_v2

    common = dict(
        target_shares_by_bucket={"30": 5.0, "31": 5.0, "32": 5.0},
        support=("30", "31", "32"),
        min_lot_by_bucket={"30": 5.0, "31": 5.0, "32": 5.0},
        ask_by_bucket={"30": 0.20, "31": 0.45, "32": 0.15},
        held_shares_by_bucket={"30": 5.0, "31": 5.0},
    )
    lifted = raise_sub_lot_targets_v2(**common, capital_available_usdc=1.2)
    # 32°C's increment of five shares is $0.75, under the dollar: raised to
    # seven.  The held rungs are not orders and are neither raised nor
    # charged -- 0.75 + 0.30 fits 1.2 only because they are not.
    assert lifted["target_shares_by_bucket"] == {"30": 5.0, "31": 5.0, "32": 7.0}
    assert lifted["raised_buckets"] == ("32",)
    assert lifted["notional_raised_buckets"] == ("32",)
    assert lifted["lot_raise_usdc"] == pytest.approx(0.30)
    refused = raise_sub_lot_targets_v2(**common, capital_available_usdc=1.0)
    assert refused["refused_reason"] == "exchange_lot_top_up_unaffordable"
    # A held rung whose own dollar floor is above what it holds is still not
    # raised: nothing is being bought there.
    parked = raise_sub_lot_targets_v2(
        target_shares_by_bucket={"30": 5.0, "31": 5.0},
        support=("30", "31"),
        min_lot_by_bucket={"30": 5.0, "31": 5.0},
        ask_by_bucket={"30": 0.05, "31": 0.05},
        held_shares_by_bucket={"30": 5.0, "31": 5.0},
        capital_available_usdc=10.0,
    )
    assert parked["raised_buckets"] == ()
    # Without holdings every target is an increment: the pre-charter-84
    # reading the paper ledger still hands in.
    fresh = raise_sub_lot_targets_v2(
        target_shares_by_bucket={"32": 5.0}, support=("32",),
        min_lot_by_bucket={"32": 5.0}, ask_by_bucket={"32": 0.15},
        capital_available_usdc=10.0,
    )
    assert fresh["target_shares_by_bucket"] == {"32": 7.0}


def test_execution_sizing_reads_the_venue_minimum_as_lot_or_dollar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bayes_basket_target import runtime as production_runtime

    assert production_runtime.execution_minimum_lot_shares(
        min_order_size=5.0, limit_price=0.15
    ) == 7.0
    assert production_runtime.execution_minimum_lot_shares(
        min_order_size=5.0, limit_price=0.25
    ) == 5.0
    assert production_runtime.execution_minimum_lot_shares(
        min_order_size=5.0, limit_price=0.04
    ) == 25.0
    assert production_runtime.execution_minimum_lot_shares(
        min_order_size=0.0, limit_price=0.5
    ) == 2.0
    monkeypatch.setattr(production_runtime, "CHARTER_83_MIN_NOTIONAL", "off")
    assert production_runtime.execution_minimum_lot_shares(
        min_order_size=5.0, limit_price=0.04
    ) == 5.0


# --- the live cycle that traded is over, not still the day's candidate ----

def _live_cycle_with_fill(conn, *, index: int, fill_notional: float | None = 2.4,
                          intent_state: str = "filled", reconciled: bool = True):
    """One committed cycle (reservation 3.0) whose single intent ended."""

    from tests.test_bayes_basket_target_atomic_ledger import _committed_intent

    bundle, intent_id = _committed_intent(conn, index=index)
    cycle_id = bundle["cycle"]["cycle_id"]
    conn.execute(
        "UPDATE basket_target_order_intents SET state=? WHERE order_intent_id=?",
        (intent_state, intent_id),
    )
    if reconciled and fill_notional is not None:
        conn.execute(
            "INSERT INTO basket_target_order_reconciliations("
            "order_intent_id,exchange_order_id,state,filled_size,"
            "filled_notional_usdc,last_event_id,blocking_reason,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (intent_id, "0x" + intent_id, "resolved_filled", 5.0,
             fill_notional, None, None, "2026-07-23T04:10:00Z"),
        )
    conn.commit()
    return cycle_id, intent_id


def _pool(conn) -> tuple[float, float]:
    row = conn.execute(
        "SELECT balance_usdc,reserved_usdc FROM basket_target_capital_pool"
    ).fetchone()
    return float(row[0]), float(row[1])


def test_a_live_cycle_whose_orders_all_ended_is_settled_by_what_it_spent() -> None:
    from bayes_basket_target.atomic_ledger import (
        AtomicLedgerError,
        settle_live_cycle_after_terminal_intents,
    )
    from tests.test_bayes_basket_target_atomic_ledger import _db

    with _db() as conn:
        cycle_id, _intent = _live_cycle_with_fill(conn, index=60)
        balance_before, reserved_before = _pool(conn)
        settled = settle_live_cycle_after_terminal_intents(
            conn, cycle_id=cycle_id, settled_at_utc="2026-07-23T04:20:00Z"
        )
        assert settled["state"] == "settled"
        assert settled["consumed_usdc"] == pytest.approx(2.4)
        assert settled["reservation_released_usdc"] == pytest.approx(3.0)
        assert conn.execute(
            "SELECT state FROM basket_target_execution_cycles WHERE cycle_id=?",
            (cycle_id,),
        ).fetchone()[0] == "settled"
        reservation = conn.execute(
            "SELECT state,release_reason FROM basket_target_capital_reservations"
            " WHERE cycle_id=?", (cycle_id,),
        ).fetchone()
        assert reservation[0] == "consumed"
        assert reservation[1].startswith("live_terminal_fills_actual_usdc=2.4")
        balance_after, reserved_after = _pool(conn)
        assert balance_after == pytest.approx(balance_before - 2.4)
        assert reserved_after == pytest.approx(reserved_before - 3.0)
        # Idempotent, and the guards refuse what is not over yet.
        assert settle_live_cycle_after_terminal_intents(
            conn, cycle_id=cycle_id, settled_at_utc="2026-07-23T04:21:00Z"
        )["state"] == "already_settled"

        still_live, _ = _live_cycle_with_fill(
            conn, index=61, intent_state="submitted", reconciled=False
        )
        with pytest.raises(AtomicLedgerError, match="intent_not_terminal"):
            settle_live_cycle_after_terminal_intents(
                conn, cycle_id=still_live, settled_at_utc="2026-07-23T04:22:00Z"
            )
        unreconciled, _ = _live_cycle_with_fill(conn, index=62, reconciled=False)
        with pytest.raises(AtomicLedgerError, match="fill_unreconciled"):
            settle_live_cycle_after_terminal_intents(
                conn, cycle_id=unreconciled, settled_at_utc="2026-07-23T04:23:00Z"
            )
        inert, _ = _live_cycle_with_fill(
            conn, index=63, intent_state="failed", reconciled=False
        )
        with pytest.raises(AtomicLedgerError, match="nothing_filled"):
            settle_live_cycle_after_terminal_intents(
                conn, cycle_id=inert, settled_at_utc="2026-07-23T04:24:00Z"
            )


def test_the_resident_sweep_settles_exactly_the_cycles_that_traded_and_ended() -> None:
    from types import SimpleNamespace

    from bayes_basket_target.production_worker import TargetProductionWorker
    from tests.test_bayes_basket_target_atomic_ledger import _db

    with _db() as conn:
        done, _ = _live_cycle_with_fill(conn, index=70)
        live, _ = _live_cycle_with_fill(
            conn, index=71, intent_state="submitted", reconciled=False
        )
        inert, _ = _live_cycle_with_fill(
            conn, index=72, intent_state="failed", reconciled=False
        )
        receipt = TargetProductionWorker._settle_live_cycles(
            SimpleNamespace(commit_conn=conn), as_of_utc="2026-07-23T04:30:00Z"
        )
        assert receipt["state"] == "committed"
        assert receipt["examined"] == 1
        assert receipt["settled"] == 1
        assert receipt["settled_scopes"][0]["cycle_id"] == done
        states = {
            row[0]: row[1] for row in conn.execute(
                "SELECT cycle_id,state FROM basket_target_execution_cycles"
            )
        }
        assert states[done] == "settled"
        assert states[live] == "prepared"
        assert states[inert] == "prepared"
        # Nothing left to do on the next pass.
        again = TargetProductionWorker._settle_live_cycles(
            SimpleNamespace(commit_conn=conn), as_of_utc="2026-07-23T04:31:00Z"
        )
        assert again["state"] == "no_settleable_cycles"


def test_station_edge_evidence_reads_the_increment_not_the_sunk_basket() -> None:
    """The allocator's evidence is what a dollar buys *now*.

    ZGSZ 2026-09-06, 13:12Z: the 12:51Z completion plan, read whole (five
    shares a rung against 4.35 of sunk-plus-new cost, in-window mass 0.77),
    scored -0.51 per basket, the allocator gave the station a zero quota,
    and the next compile had no capital to seat the completion again.
    """

    import sqlite3
    from types import SimpleNamespace

    from bayes_basket_target.p0_preselection import commit_p0_preselection
    from bayes_basket_target.production_worker import TargetProductionWorker
    from bayes_basket_target.ssot import apply_production_ssot_schema
    from tests.test_bayes_basket_target_p0_preselection import _value

    probabilities = {"30": 0.20, "31": 0.357, "32": 0.21, "33": 0.233}
    value = {
        **_value(),
        "station_id": "ZGSZ",
        "event_date": "2026-09-06",
        "compiled_plan": {
            "probability_envelope": {"probabilities": probabilities},
            "plan": {
                # Charter 130 (amending 84 (七)): the allocator reads the
                # belief the seat was judged on, not the raw envelope.
                "basket_support_selection": {
                    "seated": {"window_probabilities": probabilities},
                },
                "final_support": ["30", "31", "32"],
                "execution_gap_support": ["32"],
                "total_delta_notional_usdc": 0.95,
                "targets": [
                    {"bucket": "30", "delta_shares": 0.0, "candidate_price": 0.0},
                    {"bucket": "31", "delta_shares": 0.0, "candidate_price": 0.0},
                    {"bucket": "32", "delta_shares": 5.0, "candidate_price": 0.19},
                ],
                "target_solution": {
                    "target_shares_by_bucket": {"30": 5.0, "31": 5.0, "32": 5.0},
                    "target_cost_by_bucket": {"30": 1.25, "31": 2.15, "32": 0.95},
                },
            },
        },
    }
    with sqlite3.connect(":memory:") as conn:
        apply_production_ssot_schema(conn)
        commit_p0_preselection(conn, value, created_at_utc="2026-09-05T12:51:33Z")
        rows = TargetProductionWorker._station_edge_evidence(
            SimpleNamespace(conn=conn), stations=["ZGSZ"], minimum_clip_usdc=0.25
        )
    assert len(rows) == 1
    # The increment: 5 shares of 32°C for 0.95 -- +0.10 expected on 0.95.
    assert rows[0]["edge_per_usdc"] == pytest.approx((0.21 * 5.0 - 0.95) / 0.95)
    assert rows[0]["minimum_basket_usdc"] == pytest.approx(0.75)
    # The whole-basket reading this replaces was negative.
    whole_cost = 4.35
    whole = sum(
        probabilities[b] * ((5.0 if b in ("30", "31", "32") else 0.0) - whole_cost)
        for b in probabilities
    )
    assert whole < 0.0
    # Charter 130: a plan that carries only the raw envelope -- the belief
    # the gates do not read -- gives the allocator nothing to judge on.
    envelope_only = dict(value, compiled_plan={
        "probability_envelope": {"probabilities": probabilities},
        "plan": {
            k: v for k, v in value["compiled_plan"]["plan"].items()
            if k != "basket_support_selection"
        },
    })
    with sqlite3.connect(":memory:") as conn:
        apply_production_ssot_schema(conn)
        commit_p0_preselection(conn, envelope_only, created_at_utc="2026-09-05T12:51:33Z")
        assert TargetProductionWorker._station_edge_evidence(
            SimpleNamespace(conn=conn), stations=["ZGSZ"], minimum_clip_usdc=0.25
        ) == []


# --- the rotation serves the station-days holding live positions first ----

def test_the_forward_pass_leads_with_the_held_station_days() -> None:
    from bayes_basket_target.production_worker import TargetProductionWorker

    keys = [f"S{index:02d}|d" for index in range(10)]
    order = TargetProductionWorker._forward_preselection_order(
        keys, resume_at="S06|d", priority={"S02|d", "S08|d"}
    )
    # Rotated to the cursor, then the held scopes lifted in that order.
    assert order[:2] == ["S08|d", "S02|d"]
    assert order[2] == "S06|d"
    assert sorted(order) == sorted(keys) and len(set(order)) == len(keys)
    # No priority: exactly the old rotation.
    assert TargetProductionWorker._forward_preselection_order(
        keys, resume_at="S06|d"
    )[0] == "S06|d"


def test_the_resident_rotation_key_puts_a_held_scope_before_everything() -> None:
    from bayes_basket_target.production_worker import TargetProductionWorker as W

    as_of = "2026-08-12T06:20:00Z"
    served = {
        "served_at_utc": "2026-08-12T06:00:00Z",
        "envelope_id": "env-1",
        "book_observed_at": "2026-08-12T06:00:00Z",
    }
    def scope(station, tz, day_state="open"):
        return {
            "scope_key": f"{station}|2026-08-12",
            "station_id": station,
            "event_date": "2026-08-12",
            "settlement_timezone": tz,
            "event_day_state": day_state,
            "manifest": {"as_of_utc": as_of, "connection_observed_at": as_of},
        }
    open_warm = scope("ZUUU", "Asia/Shanghai")
    held_future = scope("KORD", "America/Chicago", day_state="not_started")
    plain = W._scope_rotation_key(open_warm, as_of_utc=as_of, last_served=served, quota_usdc=4.0)
    held = W._scope_rotation_key(
        held_future, as_of_utc=as_of, last_served=served, quota_usdc=4.0, held=True
    )
    unheld_future = W._scope_rotation_key(
        held_future, as_of_utc=as_of, last_served=served, quota_usdc=4.0
    )
    assert held < plain < unheld_future


def test_the_ledger_names_the_station_days_the_book_holds() -> None:
    from types import SimpleNamespace

    from bayes_basket_target.production_worker import TargetProductionWorker
    from tests.test_bayes_basket_target_inventory_producer import (
        _insert_ledger_fill,
        _ledger_db,
    )

    with _ledger_db() as conn:
        conn.execute(
            "INSERT INTO basket_target_execution_cycles VALUES"
            "('cycle-flat','FLAT','2026-07-23','America/New_York')"
        )
        _insert_ledger_fill(conn, intent="a", bucket="30°C", token="t30", size=5, notional=1.25)
        _insert_ledger_fill(conn, intent="b", bucket="31°C", token="t31", size=5, notional=2.15)
        conn.execute(
            "INSERT INTO basket_target_order_intents VALUES('c','cycle-flat','20°C','t20','BUY')"
        )
        conn.execute(
            "INSERT INTO basket_target_order_reconciliations VALUES"
            "('c','resolved_filled',5,1.0,'0xc','2026-07-23T04:00:00Z')"
        )
        conn.execute(
            "INSERT INTO basket_target_order_intents VALUES('d','cycle-flat','20°C','t20','SELL')"
        )
        conn.execute(
            "INSERT INTO basket_target_order_reconciliations VALUES"
            "('d','resolved_filled',5,1.1,'0xd','2026-07-23T05:00:00Z')"
        )
        held = TargetProductionWorker._ledger_held_scope_keys(
            SimpleNamespace(commit_conn=conn, conn=conn)
        )
    # TEST holds two rungs; FLAT bought and sold flat; nothing else.
    assert held == frozenset({"TEST|2026-07-23"})


# --- (八) a partly held window is admitted on its increment ---------------

def _partly_held(*, rungs, held, cost, station="RKPK") -> TargetBasketSnapshot:
    """``rungs``: (bucket, ordinal, probability, ask); ``held``/``cost`` per bucket."""

    def bucket(name, ordinal, probability, ask):
        return {
            "bucket": name, "ordinal": ordinal, "probability": probability,
            "bid_price": max(0.001, ask - 0.01),
            "bid_levels": [{"price": max(0.001, ask - 0.01), "shares": 50.0}],
            "ask_levels": [{"price": ask, "shares": 100.0}],
            "price_tick_size": 0.01, "maker_queue_ahead_shares": 0.0,
            "maker_queue_known": True, "min_order_size": 5.0,
            "filled_shares": held.get(name, 0.0),
            "filled_cost_usdc": cost.get(name, 0.0),
        }
    return TargetBasketSnapshot.from_mapping({
        "schema": "target_basket_snapshot_v6", "snapshot_id": "partly-held",
        "sequence": 1, "observed_at": "2026-07-28T03:00:00Z",
        "market_id": "rkpk-market", "station_id": station, "event_date": "2026-07-28",
        "quote_version": "quote-v1", "quote_observed_at": "2026-07-28T03:00:00Z",
        "quote_source": "OFFICIAL_CLOB_WS", "inventory_version": "inv-v1",
        "order_state_version": "order-v1",
        "evidence": {"probability_stage": "P0", "posterior_version": "p0-v1",
                     "posterior_sha256": "a" * 64},
        "buckets": [bucket(*r) for r in rungs],
        "checkpoint_changed": True, "quote_changed": True, "fill_changed": True,
        "time_gate_changed": True, "evidence_fresh": True, "contract_matches": True,
        "order_state_known": True, "risk_inputs_complete": True,
        "time_left_minutes": 1440.0,
    })


def _compile(snapshot, *, minimum_support_buckets: int = 3):
    """Production's three-rung P0 floor, so a lone rung cannot take the seat."""

    from bayes_basket_target.basket_kernel.contracts import TargetBasketPolicy

    policy = TargetBasketPolicy.from_mapping({
        **_basket_policy().to_dict(),
        "minimum_support_buckets": minimum_support_buckets,
    })
    return compile_delta_v2_runtime_plan(
        snapshot=snapshot, basket_policy=policy, delta_policy=_delta_policy(),
        fill_resolver=_fill_resolver, capital_available_usdc=10.0,
        minimum_profit_floor=-5.0, resource_probe_ok=True, book_equity_usdc=514.29,
    )


def _production_arms(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the shipped arms: other suites flip seat and gate switches on the
    module and the compile below must be judged under production's."""

    for name, value in (
        ("MARKET_LEAN_GATE_MODE", "shadow"),
        ("CHARTER_78_INCUMBENT_NO_EXEMPTION", "on"), ("CHARTER_77_DECISION_MOMENTS", "on"),
        ("CHARTER_84_LADDER_BALANCE", "on"),
        ("TARGET_PRICING_BASIS", "ask_depth"), ("DECLINE_CLAMP_MODE", "off"),
        ("CHARTER_66A_CHAIN_PAR", "on"), ("CHARTER_71A_TOPUP_IDENTITY", "on"),
        ("CHARTER_71B_CHAIN_GROWTH", "on"), ("CHARTER_72_KELLY_SIZING", "on"),
        ("CHARTER_70_EXCHANGE_LOT", "on"), ("CHARTER_83_MIN_NOTIONAL", "on"),
    ):
        monkeypatch.setattr(delta_v2_runtime, name, value)


def _ltac(*, ask_31: float, ask_33: float) -> TargetBasketSnapshot:
    """LTAC 2026-09-06: 32°C held at 0.57, offered at 0.59 against a claim of
    0.40; the symmetric seat (charter 79) is 31/32/33."""

    return _partly_held(
        rungs=[("28", 28, 0.02, 0.02), ("29", 29, 0.02, 0.02), ("30", 30, 0.061, 0.04),
               ("31", 31, 0.319, ask_31), ("32", 32, 0.398, 0.59), ("33", 33, 0.157, ask_33),
               ("34", 34, 0.025, 0.02)],
        held={"32": 5.0}, cost={"32": 2.85},
    )


def test_ltac_the_held_rung_priced_above_its_claim_no_longer_refuses_the_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every window holding 32°C prices above its claim as a whole (31/32/33
    at 1.02 against 0.87), so the day compiled support_empty.  On the
    increment -- 31°C and 33°C, what a completion would buy -- the seat is
    admissible.  What still refuses it at the live prices is charter 66 A:
    0.57 sunk plus 0.34 plus 0.09 is a chain at par, a dollar for a dollar
    of payout, and no completion is allowed to build that.  A cent or two
    cheaper on the two rungs and the same completion trades."""
    # Charter 84 (八) is what this test is about, not which lamp triggers the
    # tilt.  The trigger moved to ``stable`` alone on 2026-09-08 and these
    # fixtures are ``sharp`` shapes, so pin the signal set here: a test of the
    # completion exemption must not re-break every time the lamp ruling moves.
    _tilted_seat(monkeypatch)

    _production_arms(monkeypatch)
    # At the live prices the only sanctioned window is refused by charter
    # 66 A -- a chain at par -- and with nothing else admitted the compile
    # has no window: support_empty, which is what LTAC's plans said all
    # afternoon.
    # (Whether the day compiles to a HOLD on an inadmissible wider window or
    # to support_empty depends on which seat arms the surrounding tests left
    # switched; both are the same refusal, and neither buys.)
    try:
        at_par = _compile(_ltac(ask_31=0.34, ask_33=0.09))
    except ContractError as exc:
        assert "delta_v2_support_empty" in str(exc)
    else:
        assert at_par["state"] == "HOLD"
        # Charter 87 (2026-09-06): the gate reads the increment's claim
        # through charter 68's lambda at the window's agreement (a = 1.08,
        # lambda 0.82): 0.514 x 0.82 = 0.42 against an offer of 0.43, so
        # the completion is refused at the gate, before the chain law sees
        # it.  Either refusal is the same answer: nothing is bought.
        selection = at_par["basket_support_selection"]
        assert selection["seated"]["admissible"] is False
        assert selection["effective_edge_gate_refused_window_count"] >= 1
    # The pre-87 gate: the increment is admitted on the lifted claim alone
    # and it is charter 66 A -- a chain at par -- that refuses it.
    monkeypatch.setattr(delta_v2_runtime, "CHARTER_87_GATE_EFFECTIVE_EDGE", "off")
    try:
        at_par = _compile(_ltac(ask_31=0.34, ask_33=0.09))
    except ContractError as exc:
        assert "delta_v2_support_empty" in str(exc)
    else:
        assert at_par["state"] == "HOLD"
        # Charter 131 (12b): the increment is admitted at the gate (0.476 on
        # 0.43, +10.7%) and the five-percent in-support floor refuses the
        # at-par chain after the lift, before charter 66 A ever sees it;
        # either refusal is the same answer -- past the gate, nothing bought.
        assert at_par["basket_support_selection"]["seated"]["admissible"] is True
        assert at_par["basket_target_activated"] is False
        assert (
            "support_floor_after_lift" in at_par["reason_codes"]
            or any(
                row["buckets"] == ["31", "32", "33"]
                and str(row["reason"]).startswith("chain_cost_at_par")
                for row in at_par["basket_support_selection"]["unsolvable_windows"]
            )
        )
    monkeypatch.setattr(delta_v2_runtime, "CHARTER_87_GATE_EFFECTIVE_EDGE", "on")

    cheaper = _compile(_ltac(ask_31=0.30, ask_33=0.07))
    seated = cheaper["basket_support_selection"]["seated"]
    assert seated["buckets"] == ["31", "32", "33"]
    assert seated["admissible"] is True
    assert seated["completion"]["held_buckets"] == ["32"]
    assert seated["completion"]["buyable_buckets"] == ["31", "33"]
    assert cheaper["target_solution"]["winner_candidate_origin"] == LADDER_BALANCE_CANDIDATE_ORIGIN
    # Charter 93 (2026-09-06): the completion is seated, and then the
    # dollar floor lifts 33°C at 0.07 from five shares to fifteen.  The
    # basket that would go out -- 5 / 5 / 15 for 5.40 -- loses 0.40 when
    # the truth lands on 31°C or 32°C, and a basket that loses inside its
    # own window is not bought: the plan holds by name.
    assert cheaper["state"] == "HOLD"
    assert cheaper["execution_gap_support"] == []
    assert "support_floor_after_lift" in cheaper["reason_codes"]
    # The pre-ruling arm buys the lifted shape, as production did.
    monkeypatch.setattr(delta_v2_runtime, "CHARTER_93_SUPPORT_FLOOR", "off")
    cheaper = _compile(_ltac(ask_31=0.30, ask_33=0.07))
    assert set(cheaper["execution_gap_support"]) == {"31", "33"}
    assert cheaper["state"] != "HOLD"
    assert cheaper["delta_v2_action_selection"]["authorized"] is True
    # The completion is sized against the capital the seat was opened with,
    # not the tilt-cut capital of this compile.
    tilt = cheaper["delta_v2_audit"]["consolidation_tilt"]
    assert tilt["completion_exempt"] is True
    assert tilt["capital_after_usdc"] == pytest.approx(tilt["capital_before_usdc"])
    monkeypatch.setattr(delta_v2_runtime, "CHARTER_93_SUPPORT_FLOOR", "on")

    # With the ruling off the seat is judged whole again: every window
    # holding 32°C prices above its claim, and the day has no window --
    # support_empty, LTAC's afternoon -- or at best a HOLD.
    monkeypatch.setattr(delta_v2_runtime, "CHARTER_84_LADDER_BALANCE", "off")
    try:
        before = _compile(_ltac(ask_31=0.30, ask_33=0.07))
    except ContractError as exc:
        assert "delta_v2_support_empty" in str(exc)
    else:
        assert before["state"] == "HOLD"


def test_lfpb_a_missing_rung_under_the_leg_floor_is_dead_not_a_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LFPB 2026-09-06: 29°C held; 27°C is offered at 0.02, under the leg
    floor the market ruling set at 0.03.  It is not bought, and it does not
    stop 28°C from being bought at 0.16 against a claim of 0.22.

    Re-shaped for charter 131 (2026-09-12): the day must read symmetric
    around 29°C for {28,29,30} to be the seat (the original mass read
    LEFT_SKEWED with the mean bucket at 28, whose tail shapes hold the
    sub-floor rungs and cannot be enumerated).  The whole window {28,29,30}
    prices at 0.94 against its 0.755 claim (a = 1.245, lambda 0.82) and is
    refused as a whole, so no Kelly candidate exists; the increment -- 28°C
    at 0.16 and 30°C at 0.18 -- claims 0.444 x 0.82 = 0.364 on 0.34 and
    clears the five-percent hurdle, and the completion is what trades."""

    _production_arms(monkeypatch)
    lfpb = _partly_held(
        rungs=[("26", 26, 0.03, 0.02), ("27", 27, 0.10, 0.02), ("28", 28, 0.222, 0.16),
               ("29", 29, 0.311, 0.60), ("30", 30, 0.222, 0.18), ("31", 31, 0.115, 0.07)],
        held={"29": 5.0}, cost={"29": 2.20},
    )
    result = _compile(lfpb)
    seated = result["basket_support_selection"]["seated"]
    assert seated["admissible"] is True
    completion = seated["completion"]
    assert completion["held_buckets"] == ["29"]
    assert "27" not in completion["buyable_buckets"]
    assert result["target_solution"]["winner_candidate_origin"] == LADDER_BALANCE_CANDIDATE_ORIGIN
    gap = set(result["execution_gap_support"])
    assert "28" in gap and "27" not in gap and "29" not in gap
    targets = {row["bucket"]: row for row in result["targets"]}
    assert targets["28"]["delta_shares"] > 0.0
    assert result["state"] != "HOLD"


def test_a_cycle_whose_orders_were_all_refused_is_released_not_stranded() -> None:
    """Refusals leave reconciliation facts, which supersession will not
    retire; the cycle is over with nothing spent, and its reservation goes
    back to the pool."""

    from types import SimpleNamespace

    from bayes_basket_target.atomic_ledger import (
        AtomicLedgerError,
        settle_live_cycle_after_terminal_intents,
    )
    from bayes_basket_target.production_worker import TargetProductionWorker
    from tests.test_bayes_basket_target_atomic_ledger import _committed_intent, _db

    with _db() as conn:
        bundle, intent_id = _committed_intent(conn, index=80)
        refused = bundle["cycle"]["cycle_id"]
        conn.execute(
            "UPDATE basket_target_order_intents SET state='failed' WHERE order_intent_id=?",
            (intent_id,),
        )
        conn.execute(
            "INSERT INTO basket_target_order_reconciliations("
            "order_intent_id,exchange_order_id,state,filled_size,"
            "filled_notional_usdc,last_event_id,blocking_reason,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (intent_id, None, "resolved_not_placed", 0.0, 0.0, None,
             "gateway_refused_submission", "2026-07-23T04:10:00Z"),
        )
        # An inert cycle -- failed with no reconciliation fact -- stays the
        # supersession path's.
        inert_bundle, inert_intent = _committed_intent(conn, index=81)
        conn.execute(
            "UPDATE basket_target_order_intents SET state='cancelled' WHERE order_intent_id=?",
            (inert_intent,),
        )
        conn.commit()
        balance_before, reserved_before = _pool(conn)
        settled = settle_live_cycle_after_terminal_intents(
            conn, cycle_id=refused, settled_at_utc="2026-07-23T04:20:00Z"
        )
        assert settled["outcome"] == "released"
        assert settled["consumed_usdc"] == 0.0
        assert settled["refused_intent_ids"] == [intent_id]
        balance_after, reserved_after = _pool(conn)
        assert balance_after == pytest.approx(balance_before)
        assert reserved_after == pytest.approx(reserved_before - 3.0)
        assert conn.execute(
            "SELECT state, release_reason FROM basket_target_capital_reservations WHERE cycle_id=?",
            (refused,),
        ).fetchone() == ("released", "live_orders_all_refused")
        with pytest.raises(AtomicLedgerError, match="nothing_filled"):
            settle_live_cycle_after_terminal_intents(
                conn, cycle_id=inert_bundle["cycle"]["cycle_id"],
                settled_at_utc="2026-07-23T04:21:00Z",
            )
        # The sweep selects the refused cycle and leaves the inert one.
        again_bundle, again_intent = _committed_intent(conn, index=82)
        conn.execute(
            "UPDATE basket_target_order_intents SET state='failed' WHERE order_intent_id=?",
            (again_intent,),
        )
        conn.execute(
            "INSERT INTO basket_target_order_reconciliations("
            "order_intent_id,exchange_order_id,state,filled_size,"
            "filled_notional_usdc,last_event_id,blocking_reason,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (again_intent, None, "resolved_not_placed", 0.0, 0.0, None,
             "order_never_reached_exchange", "2026-07-23T04:30:00Z"),
        )
        conn.commit()
        receipt = TargetProductionWorker._settle_live_cycles(
            SimpleNamespace(commit_conn=conn), as_of_utc="2026-07-23T04:40:00Z"
        )
        assert receipt["settled"] == 1
        assert receipt["settled_scopes"][0]["cycle_id"] == again_bundle["cycle"]["cycle_id"]


def test_the_sweep_is_not_blocked_by_orphan_cycles_that_never_held_an_intent() -> None:
    """A prepared cycle with no intent is not a live cycle and is not examined.

    2026-09-05 17:34Z restart: the all-refused branch of the settle query is
    vacuously true for a cycle with zero intents, so the forty oldest of the
    ~55,000 orphan cycles the identity migration left ``prepared`` filled the
    batch every pass and failed ``live_cycle_settle_no_intents``, while the
    eight cycles that had really filled that evening stayed prepared for
    seven hours -- reservations unconsumed, and every later top-up on those
    station-days due to be refused as supersession-required.
    """

    from types import SimpleNamespace

    from bayes_basket_target.production_worker import TargetProductionWorker
    from tests.test_bayes_basket_target_atomic_ledger import _db

    with _db() as conn:
        # Forty-one orphans, all older than the cycle that traded.
        for index in range(41):
            conn.execute(
                "INSERT INTO basket_target_execution_cycles("
                "cycle_id,plan_id,station_id,event_date,settlement_timezone,"
                "strategy_lane,plan_sha256,requested_reserve_usdc,state,"
                "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    f"cycle_orphan_{index}", f"plan_orphan_{index}", "RKPK",
                    "2026-07-27", "Asia/Seoul", "basket", "0" * 64, 0.0,
                    "prepared", f"2026-07-27T00:{index:02d}:00Z",
                    f"2026-07-27T00:{index:02d}:00Z",
                ),
            )
        conn.commit()
        done, _ = _live_cycle_with_fill(conn, index=90)
        receipt = TargetProductionWorker._settle_live_cycles(
            SimpleNamespace(commit_conn=conn), as_of_utc="2026-07-23T04:30:00Z"
        )
        assert receipt["examined"] == 1
        assert receipt["settled"] == 1
        assert receipt["failures"] == []
        assert receipt["settled_scopes"][0]["cycle_id"] == done
        assert conn.execute(
            "SELECT state FROM basket_target_execution_cycles WHERE cycle_id=?",
            (done,),
        ).fetchone()[0] == "settled"
        # The orphans are left exactly as they were: not this lane's.
        assert conn.execute(
            "SELECT COUNT(*) FROM basket_target_execution_cycles"
            " WHERE cycle_id LIKE 'cycle_orphan_%' AND state='prepared'"
        ).fetchone()[0] == 41


# --------------------------------------------------------------------------
# User ruling 2026-09-08: the level is the cheapest floor-legal held size.
# --------------------------------------------------------------------------

def test_unpriced_call_keeps_the_shipped_tallest_rung_level() -> None:
    """Every pre-ruling caller gets exactly what it got before."""

    assert ladder_balance_targets_v1(
        held_by_bucket={"a": 5.0, "b": 11.0, "c": 0.0}, support=("a", "b", "c")
    ) == {"a": 11.0, "b": 11.0, "c": 11.0}


def test_priced_search_takes_the_shortest_rung_when_it_is_legal() -> None:
    """SAEZ 2026-09-08: 18C held 5 @2.655, 19C held 6 @1.146, 17C missing.

    The tallest rung is 19C at 6, and completing to it buys 17C six times
    over.  Five is legal and is what the seat was actually sized at -- 19C
    reached six because the venue lifted it, not because the objective asked.
    """

    targets = ladder_balance_targets_v1(
        held_by_bucket={"17C": 0.0, "18C": 5.0, "19C": 6.0},
        support=("17C", "18C", "19C"),
        held_cost_by_bucket={"17C": 0.0, "18C": 2.655, "19C": 1.146},
        ask_by_bucket={"17C": 0.12, "18C": 0.48, "19C": 0.35},
    )
    assert targets == {"17C": 5.0, "18C": 5.0, "19C": 6.0}
    # A rung already above the level keeps what it holds; nothing is sold.
    assert targets["19C"] == 6.0


def test_priced_search_walks_up_when_the_shortest_rung_is_under_water() -> None:
    """KAUS 2026-09-08: completing to 5 costs 5.256 against 5 shares.

    -0.256 in support is exactly what charter 93 forbids, so the search walks
    to the next held size and reproduces the shipped answer.
    """

    held = {"98F": 5.0, "100F": 0.0, "102F": 11.0}
    cost = {"98F": 1.605, "100F": 0.0, "102F": 1.001}
    ask = {"98F": 0.34, "100F": 0.53, "102F": 0.12}
    assert ladder_balance_targets_v1(
        held_by_bucket=held, support=("98F", "100F", "102F"),
        held_cost_by_bucket=cost, ask_by_bucket=ask,
    ) == {"98F": 11.0, "100F": 11.0, "102F": 11.0}


def test_a_short_rung_with_no_offer_completes_at_no_level() -> None:
    """Charter 84 (八) is all-or-none: an unpriced short rung ends the search."""

    assert ladder_balance_targets_v1(
        held_by_bucket={"a": 5.0, "b": 0.0, "c": 9.0},
        support=("a", "b", "c"),
        held_cost_by_bucket={"a": 1.0, "b": 0.0, "c": 2.0},
        ask_by_bucket={"a": 0.20, "b": 0.0, "c": 0.25},
    ) == {}


@pytest.mark.parametrize(
    "held,cost,ask",
    [
        ({"a": 5.0, "b": 0.0, "c": 6.0}, {"a": 2.655, "b": 0.0, "c": 1.146},
         {"a": 0.48, "b": 0.12, "c": 0.35}),
        ({"a": 15.0, "b": 6.0, "c": 0.0, "d": 0.0},
         {"a": 1.050, "b": 1.020, "c": 0.0, "d": 0.0},
         {"a": 0.04, "b": 0.12, "c": 0.26, "d": 0.31}),
        ({"a": 5.0, "b": 5.0, "c": 0.0}, {"a": 1.155, "b": 2.555, "c": 0.0},
         {"a": 0.18, "b": 0.56, "c": 0.24}),
    ],
)
def test_whatever_level_it_picks_is_floor_legal(held, cost, ask) -> None:
    """Checked with production's own floor function, not a copy of it."""

    window = tuple(held)
    targets = ladder_balance_targets_v1(
        held_by_bucket=held, support=window,
        held_cost_by_bucket=cost, ask_by_bucket=ask,
    )
    if not targets:
        return
    after = dict(cost)
    for bucket, want in targets.items():
        gap = want - held[bucket]
        if gap > 0:
            after[bucket] += gap * ask[bucket]
    verdict = support_scenario_floor_v1(
        shares_by_bucket=targets, cost_by_bucket=after, support=window
    )
    assert verdict["legal"], verdict
    # And never sells: every rung ends at or above what it held.
    assert all(targets[b] >= held[b] - 1e-9 for b in window)


# --------------------------------------------------------------------------
# User ruling 2026-09-08: the completion's two prices, recorded not gated.
# --------------------------------------------------------------------------

def test_the_floor_delta_reading_prices_the_completion_both_ways() -> None:
    """SAEZ 2026-09-08: -0.0535 of edge buys +4.45 of floor.

    Charter 73/92 sees only the first number and refuses; charter 93 sees
    only the second and is what the completion is for.  The reading carries
    both so the disagreement is measurable rather than arguable.
    """

    reading = completion_floor_delta_v1(
        support=("17C", "18C", "19C"),
        held_shares={"17C": 0.0, "18C": 5.0, "19C": 6.0},
        held_cost_usdc={"17C": 0.0, "18C": 2.655, "19C": 1.146},
        target_shares={"17C": 5.0, "18C": 5.0, "19C": 6.0},
        ask_by_bucket={"17C": 0.11, "18C": 0.48, "19C": 0.35},
        claim_by_bucket={"17C": 0.0993, "18C": 0.5268, "19C": 0.4414},
    )
    assert reading["floor_before"] == pytest.approx(-3.801)
    assert reading["floor_after"] == pytest.approx(0.649)
    assert reading["floor_delta"] == pytest.approx(4.45)
    assert reading["increment_edge"] == pytest.approx(-0.0535, abs=1e-4)
    assert reading["floor_per_edge_usdc"] == pytest.approx(4.45 / 0.0535, rel=1e-3)
    assert reading["floor_legal_before"] is False
    assert reading["floor_legal_after"] is True


def test_the_rate_is_none_when_nothing_is_traded_away() -> None:
    """A completion the increment gate already likes has no exchange rate."""

    reading = completion_floor_delta_v1(
        support=("a", "b"),
        held_shares={"a": 5.0, "b": 0.0},
        held_cost_usdc={"a": 1.0, "b": 0.0},
        target_shares={"a": 5.0, "b": 5.0},
        ask_by_bucket={"a": 0.20, "b": 0.10},
        claim_by_bucket={"a": 0.30, "b": 0.40},
    )
    assert reading["increment_edge"] > 0
    assert reading["floor_per_edge_usdc"] is None
    # And with no claims at all there is no edge to compare against.
    bare = completion_floor_delta_v1(
        support=("a", "b"),
        held_shares={"a": 5.0, "b": 0.0}, held_cost_usdc={"a": 1.0, "b": 0.0},
        target_shares={"a": 5.0, "b": 5.0}, ask_by_bucket={"a": 0.20, "b": 0.10},
    )
    assert bare["increment_edge"] is None and bare["floor_per_edge_usdc"] is None


def test_an_unpriced_short_rung_is_flagged_not_silently_costed() -> None:
    reading = completion_floor_delta_v1(
        support=("a", "b"),
        held_shares={"a": 5.0, "b": 0.0}, held_cost_usdc={"a": 1.0, "b": 0.0},
        target_shares={"a": 5.0, "b": 5.0}, ask_by_bucket={"a": 0.20, "b": 0.0},
    )
    assert reading["completion_priced"] is False



# --------------------------------------------------------------------------
# Charter 116 (user ruling 2026-09-08): a completion that makes an illegal
# floor legal is a repair, and is exempt from the increment-edge gate.
# --------------------------------------------------------------------------

def test_a_completion_that_makes_an_illegal_floor_legal_is_a_repair() -> None:
    """SAEZ 2026-09-08: floor -3.801 illegal, +0.649 legal after 0.55 of buying."""

    verdict = completion_repair_exemption_v1(
        support=("17C", "18C", "19C"),
        held_shares={"17C": 0.0, "18C": 5.0, "19C": 6.0},
        held_cost_usdc={"17C": 0.0, "18C": 2.655, "19C": 1.146},
        target_shares={"17C": 5.0, "18C": 5.0, "19C": 6.0},
        ask_by_bucket={"17C": 0.11, "18C": 0.48, "19C": 0.35},
    )
    assert verdict["exempt"] is True
    assert verdict["floor_delta"]["floor_legal_before"] is False
    assert verdict["floor_delta"]["floor_legal_after"] is True
    # The increment is edge-negative -- which is the whole reason the
    # exemption has to exist -- and the reading says so rather than hiding it.
    reading = completion_floor_delta_v1(
        support=("17C", "18C", "19C"),
        held_shares={"17C": 0.0, "18C": 5.0, "19C": 6.0},
        held_cost_usdc={"17C": 0.0, "18C": 2.655, "19C": 1.146},
        target_shares={"17C": 5.0, "18C": 5.0, "19C": 6.0},
        ask_by_bucket={"17C": 0.11, "18C": 0.48, "19C": 0.35},
        claim_by_bucket={"17C": 0.0993, "18C": 0.5268, "19C": 0.4414},
    )
    assert reading["increment_edge"] < 0.0


@pytest.mark.parametrize(
    "case,held,cost,target,ask",
    [
        # A legal seat has nothing to repair: judged on its edge like any buy.
        ("already legal", {"a": 5.0, "b": 5.0}, {"a": 1.0, "b": 1.0},
         {"a": 8.0, "b": 8.0}, {"a": 0.20, "b": 0.20}),
        # A completion that leaves the seat illegal buys nothing this values.
        ("still illegal after", {"a": 1.0, "b": 0.0}, {"a": 0.90, "b": 0.0},
         {"a": 1.0, "b": 1.0}, {"a": 0.90, "b": 0.90}),
        # Charter 84 (八) is all-or-none: an unpriced short rung is no repair.
        ("short rung unpriced", {"a": 5.0, "b": 0.0}, {"a": 1.0, "b": 0.0},
         {"a": 5.0, "b": 5.0}, {"a": 0.20, "b": 0.0}),
    ],
)
def test_the_repair_exemption_refuses_everything_that_is_not_a_repair(
    case, held, cost, target, ask
) -> None:
    verdict = completion_repair_exemption_v1(
        support=tuple(held), held_shares=held, held_cost_usdc=cost,
        target_shares=target, ask_by_bucket=ask,
    )
    assert verdict["exempt"] is False, case


def test_the_exemption_waives_the_score_and_nothing_else() -> None:
    """Structural blockers still bind; only the edge test is waived."""

    source = _inspect.getsource(K._evaluate_maker_bundle_combo)
    assert "not structural_blockers" in source
    assert "bool(repair_exempt)" in source
    # And the arm can be switched off, which restores the pre-ruling refusal.
    assert K.CHARTER_116_COMPLETION_REPAIR == "on"
    assert K.COMPLETION_REPAIR_RULE_VERSION == "completion_repairs_an_illegal_floor_v1"


def test_the_repair_reports_what_it_costs_in_edge_beside_what_it_buys_in_floor() -> None:
    """Charter 116's own premise, finally computed.

    The clause exists because the two currencies habitually disagree on a
    completion: the leg is missing because the book would not sell it at our
    price, which is the region where ``claim < ask``, so the increment
    prices NEGATIVE -- and buying it is what lifts the seat's worst
    in-support settlement off ``-premium``, which the increment does not
    price at all.  "先只记账不进闸" cannot be honoured by an accounting that
    books one side of that.

    ``claim_by_bucket`` was optional and the exemption's own call site never
    passed it, though the charter 73 belief sat six lines above it.  So
    every exemption granted recorded a floor gain against a null edge:
    measured 2026-09-09 over the last 400 plans, seven exemptions, seven
    floor gains, zero edges.  The number that would have shown what the
    repair cost was the one not being computed.
    """

    kwargs = dict(
        support=("31C", "32C", "33C"),
        held_shares={"31C": 7.0, "32C": 0.0, "33C": 7.0},
        held_cost_usdc={"31C": 1.68, "32C": 0.0, "33C": 1.75},
        target_shares={"31C": 7.0, "32C": 7.0, "33C": 7.0},
        ask_by_bucket={"31C": 0.24, "32C": 0.36, "33C": 0.25},
    )
    reading = completion_repair_exemption_v1(
        **kwargs,
        # The missing leg asks 0.36 against a claim of 0.30 -- the region
        # the edge rule refuses, which is why it is still missing.
        claim_by_bucket={"31C": 0.17, "32C": 0.30, "33C": 0.44},
    )["floor_delta"]

    assert reading["floor_legal_before"] is False
    assert reading["floor_legal_after"] is True
    # Both currencies, and they disagree, which is the whole point.
    assert reading["increment_edge"] == pytest.approx(-0.42)
    assert reading["floor_delta"] == pytest.approx(4.48)
    assert reading["completion_cash_usdc"] == pytest.approx(2.52)
    # The rate is the thing a ruling would have to argue about: how much
    # worst case a unit of forgone edge buys.
    assert reading["floor_per_edge_usdc"] == pytest.approx(4.48 / 0.42)
    # The floor gain is NOT folded into the edge.  One number is what the
    # repair costs in expectation, the other is what it buys in risk, and
    # adding them would be counting the reason for the exemption as its
    # justification.
    assert reading["increment_edge"] < 0.0 < reading["floor_delta"]

    # Without the belief the reading still stands, minus the edge column --
    # a fixture or a replay that has no vector is not an error.
    bare = completion_repair_exemption_v1(**kwargs)["floor_delta"]
    assert bare["increment_edge"] is None
    assert bare["floor_per_edge_usdc"] is None
    assert bare["floor_delta"] == pytest.approx(reading["floor_delta"])


def test_the_exemption_call_site_supplies_the_belief() -> None:
    """The regression guard: the parameter is optional, so nothing but a
    test stops it from silently going unpassed again -- which is exactly
    how it stayed null from the day it was added."""

    from pathlib import Path  # noqa: PLC0415

    runtime = (
        Path(__file__).resolve().parents[1]
        / "bayes_basket_target" / "delta_v2_runtime.py"
    ).read_text(encoding="utf-8")
    call = runtime[runtime.index("completion_repair = completion_repair_exemption_v1"):]
    call = call[:call.index("if ordered_support else None")]
    assert "claim_by_bucket=edge_probabilities" in call, call[-400:]


# --------------------------------------------------------------------------
# The odds structure of the basket that is actually bought (user ruling,
# 2026-09-09).  Coverage, net EV and the chance of ending ahead are three
# quantities; only an equal basket lets them be conflated.
# --------------------------------------------------------------------------


def test_the_scenario_multiple_belongs_to_the_basket_not_the_leg() -> None:
    """A leg bought at four cents does not pay twenty-five times.

    One share of bucket k pays 1 if the day settles there, so a basket
    holding n_k shares against an all-in premium C returns ``n_k - C`` and
    its gross multiple in that scenario is ``n_k / C`` -- a property of the
    whole basket, never the leg's own ``1 / a_k``.
    """

    from bayes_basket_target.basket_kernel.delta_v2 import (
        basket_return_structure_v1,
    )

    # The Kuala Lumpur basket of 2026-09-10, at the shares the lot left it.
    r = basket_return_structure_v1(
        shares_by_bucket={"32C": 5.0, "33C": 6.0, "34C": 25.0},
        price_by_bucket={"32C": 0.27, "33C": 0.17, "34C": 0.04},
        claim_by_bucket={"32C": 0.3141, "33C": 0.4395, "34C": 0.2011},
        support=("32C", "33C", "34C"),
    )
    assert r["all_in_cost_usdc"] == pytest.approx(3.37)
    # 25 shares at 4c is not 25x; it is 25/3.37.
    assert r["scenario_gross_multiple"]["34C"] == pytest.approx(25 / 3.37)
    assert r["scenario_gross_multiple"]["32C"] == pytest.approx(5 / 3.37)
    assert r["net_expected_value_usdc"] == pytest.approx(5.865, abs=1e-3)


def test_the_net_roi_target_judges_a_resting_leg_at_the_offer() -> None:
    """The one gate the 2026-08-27 basis ruling had not reached.

    Every other gate prices the basket at what the market will sell it for
    now -- the seat's edge gate, charter 68's lambda, charter 72's stake,
    charters 93/123's post-lift re-reads.  This one read ``r_limit``, the
    price a resting rung is SENT at, which is the price this lane hopes to
    fill at.  Measured on the 134 seats of the 08-28..09-08 book carrying a
    two-sided quote: the target admits 51.5% priced at the rung against
    35.1% priced at the offer, and the 22 baskets the two answers disagree
    about read -4.0% at the offer and +11.7% at the rung.

    A crossing leg keeps its own limit, because charter 127 makes that limit
    the offer it walks: for a take the two prices are one number.
    """

    from bayes_basket_target.runtime import roi_gate_admission_price_v1

    assert roi_gate_admission_price_v1(
        execution_candidate="MAKER", limit_price=0.28, ask_price=0.30,
    ) == pytest.approx(0.30)
    assert roi_gate_admission_price_v1(
        execution_candidate="TAKER_FOK", limit_price=0.30, ask_price=0.30,
    ) == pytest.approx(0.30)
    # No offer at all is an unanswerable test, not a stricter one.
    assert roi_gate_admission_price_v1(
        execution_candidate="MAKER", limit_price=0.28, ask_price=None,
    ) == pytest.approx(0.28)


def test_the_basis_moves_the_target_and_the_direction_is_refuse() -> None:
    """Same claim, same shares, two prices: the offer refuses what the rung
    admits.  The gate only ever gets tighter, which is the direction the
    single measured value of a gate on this book points -- it is worth money
    by buying less."""

    from bayes_basket_target.basket_kernel.delta_v2 import (
        basket_return_structure_v1,
    )

    shares = {"31C": 10.0, "32C": 10.0}
    claim = {"31C": 0.55, "32C": 0.45}
    at_rung = basket_return_structure_v1(
        shares_by_bucket=shares,
        price_by_bucket={"31C": 0.42, "32C": 0.42},
        claim_by_bucket=claim,
        support=("31C", "32C"),
    )
    at_offer = basket_return_structure_v1(
        shares_by_bucket=shares,
        price_by_bucket={"31C": 0.48, "32C": 0.48},
        claim_by_bucket=claim,
        support=("31C", "32C"),
    )

    assert at_rung["meets_target"] is True
    assert at_offer["meets_target"] is False


def test_coverage_and_the_chance_of_ending_ahead_are_two_numbers() -> None:
    """A rung with ``n_k <= C`` loses on the day it wins.

    MMMX 2026-09-06 covered 0.789 of the claim and had a 0.107 chance of
    ending ahead, because two of its rungs held fewer shares than the
    basket cost.  Coverage counts those days as hits.
    """

    from bayes_basket_target.basket_kernel.delta_v2 import (
        basket_return_structure_v1,
    )

    r = basket_return_structure_v1(
        # The cheap rung carries the shares, the dear one does not, and the
        # dear one is the one the model believes.
        shares_by_bucket={"A": 30.0, "B": 2.0},
        price_by_bucket={"A": 0.05, "B": 0.40},
        claim_by_bucket={"A": 0.10, "B": 0.70},
        support=("A", "B"),
    )
    cost = r["all_in_cost_usdc"]
    assert cost == pytest.approx(30 * 0.05 + 2 * 0.40)
    # Two shares against a 2.30 premium is the shape: the venue's dollar
    # floor put thirty shares on the nickel rung and left the dear one at
    # the lot.
    assert r["coverage"] == pytest.approx(0.80)
    # B holds two shares against a 2.30 premium: it wins and still loses.
    assert r["loss_on_win_buckets"] == ["B"]
    assert r["net_profit_probability"] == pytest.approx(0.10)
    assert r["net_profit_probability"] < r["coverage"]


def test_only_an_equal_basket_reduces_to_p_minus_q() -> None:
    """MPMG 2026-09-09: the equal reading clears the target, the bought one
    loses 7%.

    Q = 0.53 against a coverage of 0.613 needs Q <= P / 1.05 = 0.584, which
    it clears -- and the basket the lot actually built returns -6.95%.
    """

    from bayes_basket_target.basket_kernel.delta_v2 import (
        basket_return_structure_v1,
    )

    shares = {"30C": 25.0, "31C": 8.0, "32C": 5.0}
    price = {"30C": 0.04, "31C": 0.14, "32C": 0.35}
    claim = {"30C": 0.001, "31C": 0.172, "32C": 0.44}
    r = basket_return_structure_v1(
        shares_by_bucket=shares, price_by_bucket=price,
        claim_by_bucket=claim, support=tuple(shares),
    )
    equal = r["equal_basket"]
    assert equal["all_in_cost"] == pytest.approx(0.53)
    assert equal["max_cost_for_target"] == pytest.approx(0.613 / 1.05)
    assert equal["meets_target"] is True, "the reading everyone reasons in"
    assert r["net_roi"] == pytest.approx(-0.0695, abs=1e-3)
    assert r["meets_target"] is False, "the basket that was bought"

    # And where the lot left the basket alone the two agree, which is the
    # only case P - Q was ever true of.
    flat = basket_return_structure_v1(
        shares_by_bucket={k: 1.0 for k in shares},
        price_by_bucket=price, claim_by_bucket=claim, support=tuple(shares),
    )
    assert flat["meets_target"] == flat["equal_basket"]["meets_target"]
    assert flat["net_roi"] == pytest.approx(
        flat["coverage"] / flat["equal_basket"]["all_in_cost"] - 1.0
    )


def test_the_return_structure_survives_into_the_training_fact() -> None:
    """A gate that reads a number nobody records is not auditable.

    The compiled plan carried ``basket_return_structure`` from the day the
    5% target landed and the training fact recorded nothing, because
    ``feature_json`` is assembled key by key rather than inherited.  Thirty-
    four decisions went by on 2026-09-09 before anyone checked, and the
    commit that shipped it claimed "recorded whether it passed or not".

    Asserted on the assembly, because the shape is a name in a dict literal
    and nothing else would notice the next one going missing.
    """

    from pathlib import Path as _Path  # noqa: PLC0415

    root = _Path(__file__).resolve().parents[1]
    assembly = (
        root / "bayes_basket_target" / "forward_training_validation.py"
    ).read_text(encoding="utf-8")
    runtime = (
        root / "bayes_basket_target" / "runtime.py"
    ).read_text(encoding="utf-8")

    # Produced by the compile...
    assert '"basket_return_structure": basket_return_structure,' in runtime
    # ...and carried into the fact the fit and the audit read.
    assert '"basket_return_structure": compiled_plan.get(' in assembly


def test_the_below_floor_set_excuses_a_rung_from_both_bounds() -> None:
    """The set's two effects, pinned so the next caller sees what it costs.

    ``below_leg_floor_buckets`` does not merely skip a rung in the build.  It
    also stops the search charging for it -- in the cash bound AND in the cost
    the scenario floor reads -- because a rung the MARKET cannot supply must
    not refuse a level the seat could otherwise afford.

    That second effect is the trap this test exists for.  On 2026-09-09
    charter 110(四)'s refusal -- legs the market offers cheap and we DECLINE --
    was routed through this parameter while it was still called
    ``unbuyable_buckets``.  Declining a leg then excused it from both bounds,
    so the completion level rose on money the refusal had notionally freed:
    three quarters of short legs were refused and total stake barely moved
    (865 to 857), twelve station-days were staked HIGHER, and those twelve
    carried -21.70 of a -39.65 swing over ten days.

    A leg we decline is buyable and must keep paying for itself.  If a future
    ruling needs "buyable but not bought", it needs its own set, and this
    test is the reason why.
    """

    from bayes_basket_target.basket_kernel.delta_v2 import (
        ladder_balance_targets_v1,
    )

    common = {
        "held_by_bucket": {"30": 5.0, "31": 0.0},
        "support": ("30", "31"),
        "held_cost_by_bucket": {"30": 2.00, "31": 0.0},
        "ask_by_bucket": {"30": 0.40, "31": 0.40},
        "depth_by_bucket": {
            "30": ({"price": 0.40, "shares": 50.0},),
            "31": ({"price": 0.40, "shares": 50.0},),
        },
        # Enough to hold what is held, nowhere near enough to buy five shares
        # of the short rung at 0.40.
        "capital_available_usdc": 0.50,
    }

    # Charged: the short rung costs 2.00, the bound is 0.50, no level fits.
    assert ladder_balance_targets_v1(**common) == {}

    # Excused: the same rung, declared below the leg floor, is not priced --
    # so the level the seat already holds is affordable and legal.
    excused = ladder_balance_targets_v1(
        **common, below_leg_floor_buckets=("31",)
    )
    assert excused == {"30": 5.0, "31": 5.0}
    # And note what it did NOT do: the target still rises to the level, so
    # the floor judges the ladder rather than the purchase.  A caller reading
    # this as "we will not buy 31" gets a basket it never paid for.
