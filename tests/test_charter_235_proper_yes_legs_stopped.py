"""Charter 235: the Proper lane's YES legs stop.

User ruling 2026-09-28 「Proper 线的 YES 腿停掉」.  The lane's ``sides`` setting
ships ``["NO"]``: a YES leg is still quoted and recorded, never sized or
posted, and an active plan made before the stop posts no further YES rung.
Charter 231's suite pins the two-sided rule with ``sides: ["YES", "NO"]``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target import proper_bet_lane as lane  # noqa: E402
from bayes_basket_target import strategy_lanes  # noqa: E402
from tests.test_charter_231_proper_bet_lane import (  # noqa: E402,F401 -- ``store`` is a fixture
    ADMIT,
    MODEL,
    NOW,
    SETTINGS,
    _end,
    _intents,
    _ladder,
    _resting_transport,
    _row,
    _run,
    _seed,
    _snapshot,
    store,
)

CHARTER = "235"
NO_ONLY = frozenset({lane.SIDE_NO})
#: The settings a production cycle reads: no ``sides`` of its own, so the
#: shipped default decides.
SHIPPED = {k: v for k, v in SETTINGS.items() if k != "sides"}


def test_the_lane_ships_with_the_yes_side_stopped() -> None:
    assert strategy_lanes.PROPER_BET_DEFAULT_SETTINGS["sides"] == ["NO"]
    shipped = strategy_lanes.load_strategy_lanes(ROOT / "config" / "trading" / "strategy_lanes_v1.json")
    assert shipped["lanes"]["proper_bet"]["sides"] == ["NO"]
    assert lane.allowed_sides(shipped["lanes"]["proper_bet"]) == NO_ONLY


@pytest.mark.parametrize("bad", ["NO", None, ["NO", "MAYBE"]])
def test_a_sides_setting_nobody_can_act_on_raises(bad) -> None:
    with pytest.raises(ValueError):
        lane.allowed_sides({"sides": bad})


def test_a_yes_leg_is_quoted_and_recorded_but_stopped() -> None:
    row = _row("29°C", 29, [(0.18, 200.0)], [(0.20, 200.0)])
    quote = lane.leg_quote("29°C", 0.60, 1.1, row, 0.01, sides=NO_ONLY)
    assert (quote["side"], quote["price"], quote["reason"]) == ("YES", 0.19, "side_stopped:YES")
    # A rung that breaks the rule says that first: stopping a side is not a
    # reason to forget why the leg would not have been posted anyway.
    breaking = lane.leg_quote("29°C", 0.15, 0.1, row, 0.01, sides=NO_ONLY)
    assert breaking["reason"].startswith("price_breaks_rule")


def test_lambda_is_the_minimum_over_the_no_legs_alone() -> None:
    both = lane.evaluate_ladder(MODEL, _ladder(), cash_usdc=None, min_shares={}, ticks={})
    no_only = lane.evaluate_ladder(MODEL, _ladder(), cash_usdc=None, min_shares={}, ticks={}, sides=NO_ONLY)
    assert {leg["bucket"]: leg["side"] for leg in both["legs"]} == {"27°C": "NO", "28°C": "NO", "29°C": "YES"}
    assert no_only["refusal"] is None
    assert {leg["bucket"] for leg in no_only["legs"]} == {"27°C", "28°C"}
    quotes = {q["bucket"]: q for q in no_only["quotes"]}
    assert quotes["29°C"]["reason"] == "side_stopped:YES" and quotes["29°C"]["price"] == pytest.approx(0.19)
    # The smallest |s| leg (28°C, a NO leg) sits at the venue minimum.
    assert {leg["bucket"]: leg["target_shares"] for leg in no_only["legs"]}["28°C"] == 5.0
    assert no_only["need_full_usdc"] < both["need_full_usdc"]


def test_a_ladder_whose_only_lawful_leg_is_yes_is_refused_as_stopped() -> None:
    # The mids sum to 0.975, so q sits above the ask on 27°C and 28°C: the
    # model is under q there (NO) but over the ask, and the NO rung breaks
    # the rule.  Only 29°C's YES rung obeys it.
    ladder = [
        _row("27°C", 27, [(0.48, 200.0)], [(0.49, 200.0)]),
        _row("28°C", 28, [(0.30, 200.0)], [(0.31, 200.0)]),
        _row("29°C", 29, [(0.18, 200.0)], [(0.19, 200.0)]),
    ]
    model = {"27°C": 0.495, "28°C": 0.311, "29°C": 0.194}
    both = lane.evaluate_ladder(model, ladder, cash_usdc=None, min_shares={}, ticks={})
    assert both["refusal"] is None and [leg["bucket"] for leg in both["legs"]] == ["29°C"]
    stopped = lane.evaluate_ladder(model, ladder, cash_usdc=None, min_shares={}, ticks={}, sides=NO_ONLY)
    assert stopped["refusal"] == "side_stopped:YES" and stopped["legs"] == []


def test_only_no_rungs_are_posted_as_shipped(store) -> None:
    _seed(store)
    sent: list = []
    receipt = lane.execute_proper_bets(
        store, transport=_resting_transport(sent), now_utc=NOW, live_submit_authorized=True,
        settings=SHIPPED, admission=ADMIT,
    )
    assert receipt["planned"] == 1 and receipt["rungs"] == 2 and receipt["submitted"] == 2
    assert sorted(payload["token_id"] for _path, payload in sent) == ["n27", "n28"]
    assert all(payload["outcome_side"] == "NO" for _path, payload in sent)
    planned, plan_json, buckets_json = store.execute(
        f"SELECT planned_legs, plan_json, buckets_json FROM {lane.EVALUATION_TABLE}"
    ).fetchone()
    assert planned == 2 and json.loads(plan_json)["sides"] == ["NO"]
    quotes = {q["bucket"]: q for q in json.loads(buckets_json)["quotes"]}
    assert quotes["29°C"]["reason"] == "side_stopped:YES"


def test_an_active_plan_stops_reposting_its_yes_leg(store) -> None:
    _seed(store)
    sent: list = []
    _run(store, sent)  # both sides: the plan was made before the stop
    for intent_id, *_rest in _intents(store):
        _end(store, intent_id)
    later = "2026-09-18T12:12:00.000Z"
    _snapshot(store, "snap_f2", later, _ladder(), 2)
    _run(store, sent, now=later, sides=["NO"])
    assert sorted(payload["token_id"] for _path, payload in sent[3:]) == ["n27", "n28"]
    state = json.loads(store.execute(f"SELECT legs_state_json FROM {lane.EVALUATION_TABLE}").fetchone()[0])
    assert state["YES:29°C"]["status"] == "side_stopped:YES"
    # The NO legs fill; what is left is the stopped side, and the plan ends
    # saying so rather than claiming every leg filled.
    for intent_id, _token, _side, price, size, _state, _body in _intents(store)[3:]:
        _end(store, intent_id, filled=size, price=price, at="2026-09-18T12:13:00.000Z")
    last = "2026-09-18T12:14:00.000Z"
    _snapshot(store, "snap_f3", last, _ladder(), 3)
    _run(store, sent, now=last, sides=["NO"])
    assert len(sent) == 5
    assert store.execute(
        f"SELECT plan_state, plan_state_reason FROM {lane.EVALUATION_TABLE}"
    ).fetchone() == (lane.PLAN_CLOSED, "side_stopped:YES")


def test_the_charter_and_agents_carry_the_ruling() -> None:
    text = (ROOT / "docs" / "constitution" / "SYSTEM_CHARTER.md").read_text(encoding="utf-8")
    body = text[text.index(f"\n{CHARTER}. "):]
    body = body[: body.index("\n", 1)]
    for phrase in ("Proper 线的 YES 腿停掉", '"sides": ["NO"]', "side_stopped:YES", "−29.0%"):
        assert phrase in body
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert f"宪章 {CHARTER}" in agents and "YES 腿已停" in agents
