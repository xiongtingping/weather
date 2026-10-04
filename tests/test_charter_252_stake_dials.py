"""Charter 252: the Proper and far-tail lanes' per-trade budgets are top-bar
dials (user rulings 2026-10-04 「顶部增加：远尾做空 H1 的单笔预算调整」「还有Proper
下注线的单笔预算调整按钮」, with 「去掉同心线的按钮」).

Pinned here: one stake table (``strategy_lanes.STAKE_DIALS``) and one
setter (``POST /api/lane-stake``) for the tiebreak, far-tail and Proper
lanes; the Proper position filled up to its stake, never under the venue
minimum, the stake cut to the cash; at the default 1 USDC the minimum rule
wherever the leg is priced at 0.20 or more; the board drawing the three
dials and no 同心线 switch.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for entry in (ROOT, ROOT / "scripts"):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

from bayes_basket_target import proper_bet_lane as lane  # noqa: E402
from bayes_basket_target import strategy_lanes as lanes  # noqa: E402
import tests.test_charter_231_proper_bet_lane as base  # noqa: E402
from tests.test_charter_146_strategy_lanes import _control  # noqa: E402
from tests.test_charter_231_proper_bet_lane import (  # noqa: E402,F401 -- ``store`` is a fixture
    _ladder,
    _row,
    _seed,
    store,
)
from tests.test_charter_249_proper_market_mode_yes import V3, WARM, _run  # noqa: E402

CHARTER = "252"


def _leg(bucket, s, price, side="YES"):
    return {"bucket": bucket, "s": s, "side": side, "price": price, "reason": None}


# ---------------------------------------------------------------------------
# The Proper position.


def test_the_position_is_filled_up_to_the_stake() -> None:
    mins = {"a": 5.0}
    out = lane.size_position([_leg("a", 0.2, 0.33)], cash_usdc=None, min_shares=mins, stake_usdc=10.0)
    (leg,) = out["legs"]
    # floor(10 / 0.33) to the cent of a share: never more than the stake.
    assert leg["target_shares"] == pytest.approx(30.30)
    assert leg["cost_usdc"] <= 10.0 and out["budget_usdc"] == pytest.approx(9.999)
    assert out["need_min_usdc"] == pytest.approx(1.65) and out["capped_by_cash"] is False


def test_a_stake_under_the_minimum_is_the_minimum() -> None:
    mins = {"a": 5.0}
    for stake in (None, 1.0):
        out = lane.size_position([_leg("a", 0.2, 0.33)], cash_usdc=None, min_shares=mins, stake_usdc=stake)
        assert out["legs"][0]["target_shares"] == 5.0
    # Under 0.20 a dollar is more than five shares: the plan carries it.
    cheap = lane.size_position([_leg("a", 0.2, 0.16)], cash_usdc=None, min_shares=mins, stake_usdc=1.0)
    assert cheap["legs"][0]["target_shares"] == pytest.approx(6.25)


def test_a_stake_keeps_the_legs_in_ratio() -> None:
    legs = [_leg("a", -1.6, 0.51, "NO"), _leg("b", 1.1, 0.19), _leg("c", 0.4, 0.30)]
    mins = {"a": 5.0, "b": 5.0, "c": 5.0}
    out = lane.size_position(legs, cash_usdc=None, min_shares=mins, stake_usdc=50.0)
    shares = {leg["bucket"]: leg["target_shares"] for leg in out["legs"]}
    lam = 50.0 / (0.51 * 1.6 + 0.19 * 1.1 + 0.30 * 0.4)
    assert out["lambda_shares"] == pytest.approx(lam)
    for bucket, s in (("a", 1.6), ("b", 1.1), ("c", 0.4)):
        assert shares[bucket] == pytest.approx(lam * s, abs=0.01)
    assert out["budget_usdc"] <= 50.0


def test_the_stake_is_cut_to_the_cash_and_the_minimum_still_holds() -> None:
    mins = {"a": 5.0}
    cut = lane.size_position([_leg("a", 0.2, 0.33)], cash_usdc=4.0, min_shares=mins, stake_usdc=10.0)
    assert cut["capped_by_cash"] is True and cut["budget_usdc"] <= 4.0
    assert cut["legs"][0]["target_shares"] == pytest.approx(12.12)
    short = lane.size_position([_leg("a", 0.2, 0.33)], cash_usdc=1.0, min_shares=mins, stake_usdc=10.0)
    assert short["legs"] == [] and short["need_min_usdc"] == pytest.approx(1.65)


def test_a_ladder_short_of_cash_names_the_minimum_it_needs() -> None:
    reading = lane.evaluate_ladder(WARM, _ladder(), cash_usdc=1.0, min_shares={}, ticks={},
                                   sides=frozenset({lane.SIDE_YES}), buckets=lane.BUCKETS_MARKET_MODE,
                                   stake_usdc=10.0)
    assert reading["refusal"] == "insufficient_cash:1.00<2.45"


@pytest.mark.parametrize("bad", [None, "x", 0, -1, float("nan"), float("inf")])
def test_a_stake_nobody_can_act_on_raises(bad) -> None:
    with pytest.raises(ValueError):
        lane.position_stake({"stake_usdc": bad})


def test_the_planner_reads_the_dial(store, monkeypatch) -> None:
    monkeypatch.setattr(base, "MODEL", dict(WARM))
    _seed(store)
    sent: list = []
    receipt = _run(store, sent, stake_usdc=10.0)
    assert receipt["planned"] == 1 and receipt["rungs"] == 1
    payload = sent[0][1]
    assert payload["price"] == pytest.approx(0.49) and payload["size"] == pytest.approx(20.40)
    plan = json.loads(store.execute(f"SELECT plan_json FROM {lane.EVALUATION_TABLE}").fetchone()[0])
    assert plan["stake_usdc"] == 10.0
    assert plan["legs"][0]["target_shares"] == pytest.approx(20.40)
    assert V3["rule"] == lane.RULE_MAKER_MODE_YES_V3


# ---------------------------------------------------------------------------
# The dials.


def test_one_stake_table_for_the_three_lanes() -> None:
    assert set(lanes.STAKE_DIALS) == {lanes.LANE_TIEBREAK, lanes.LANE_TAIL_SHORT, lanes.LANE_PROPER_BET}
    for dial in lanes.STAKE_DIALS.values():
        assert dial["bounds"] == (1.0, 100.0) and dial["step"] == 1.0
    assert lanes.validate_lane_stake("tail_short", 12.345) == 12.35
    for lane_name, bad in (("tail_short", 0.5), ("proper_bet", 101), ("proper_bet", "x"),
                           ("tail_short", float("nan")), ("p1_consensus", 5)):
        with pytest.raises(ValueError):
            lanes.validate_lane_stake(lane_name, bad)
    defaults = lanes.PROPER_BET_DEFAULT_SETTINGS
    assert defaults["budget_rule"] == "stake_full_position" and defaults["stake_usdc"] == 1.0
    shipped = lanes.load_strategy_lanes(ROOT / "config" / "trading" / "strategy_lanes_v1.json")
    assert shipped["lanes"]["proper_bet"]["budget_rule"] == "stake_full_position"
    # The number is the operator's; the bounds are the rule.
    for name, dial in lanes.STAKE_DIALS.items():
        low, high = dial["bounds"]
        assert low <= float(shipped["lanes"][name]["stake_usdc"]) <= high


def test_the_server_moves_each_dial_and_keeps_the_rest(tmp_path, monkeypatch) -> None:
    control, lanes_path = _control(tmp_path, monkeypatch)
    before = control.state()["lane_stakes"]
    assert set(before) == set(lanes.STAKE_DIALS)
    assert before["tail_short"]["stake_usdc"] == 10.0 and before["proper_bet"]["stake_usdc"] == 1.0
    control.set_lane_stake("tail_short", 15)
    after = control.set_lane_stake("proper_bet", 4)["lane_stakes"]
    assert after["tail_short"]["stake_usdc"] == 15.0 and after["proper_bet"]["stake_usdc"] == 4.0
    assert after["tiebreak"]["stake_usdc"] == 5.0
    assert lanes.lane_settings("tail_short", lanes_path)["stake_usdc"] == 15.0
    assert lane.position_stake(lanes.lane_settings("proper_bet", lanes_path)) == 4.0
    doc = json.loads(lanes_path.read_text(encoding="utf-8"))
    assert doc["lanes"]["basket"] == {"enabled": False, "note": "kept"}
    with pytest.raises(ValueError):
        control.set_lane_stake("p1_consensus", 5)
    audit = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [a["knob"] for a in audit] == ["tail_short_stake_usdc", "proper_bet_stake_usdc"]


def test_the_board_draws_three_dials_and_no_consensus_switch() -> None:
    source = (ROOT / "scripts" / "calibration_dashboard.py").read_text(encoding="utf-8")
    strip = source[source.index("function liveControlStrip(){"):]
    strip = strip[: strip.index("\n}\n")]
    for key in ("proper_bet", "tiebreak", "tail_short"):
        assert f'stakeKnob("{key}"' in strip
        assert f'lcStakeKnob("{key}"' in source
    assert 'laneSwitch("p1_consensus")' not in source
    assert '"/api/tiebreak-stake"' not in source[source.index('HTML_TEMPLATE = r"""'):]
    server = (ROOT / "scripts" / "live_control_server.py").read_text(encoding="utf-8")
    assert '"/api/lane-stake"' in server


def test_the_bar_is_one_group_per_strategy_with_its_own_dials() -> None:
    # User ruling 2026-10-04 「重新布局，分别按照策略+按钮的形式」: each dial in
    # the group of the lane that reads it -- the five capital dials are the
    # basket's alone.
    source = (ROOT / "scripts" / "calibration_dashboard.py").read_text(encoding="utf-8")
    strip = source[source.index("function liveControlStrip(){"):]
    strip = strip[: strip.index("\n}\n")]
    titles = ["Proper 下注线", "远尾做空 H1", "秤砣线", "篮子", "0.998 回收"]
    starts = [strip.index(f'${{group("{t}"') for t in titles]
    assert starts == sorted(starts)
    parts = {t: strip[a:b] for t, a, b in zip(titles, starts, starts[1:] + [len(strip)])}
    expected = {
        "Proper 下注线": ['laneSwitch("proper_bet")', 'stakeKnob("proper_bet"'],
        "远尾做空 H1": ['laneSwitch("tail_short")', 'stakeKnob("tail_short"'],
        "秤砣线": ['laneSwitch("tiebreak")', 'stakeKnob("tiebreak"'],
        "篮子": ['laneSwitch("basket")', 'knob("kelly"', 'knob("exposure"', 'knob("sizing"',
                 'knob("ceiling"', 'knob("budget"', 'id="lc-capital"'],
        "0.998 回收": ['laneSwitch("certain_win_recycle")'],
    }
    for title, members in expected.items():
        for member in members:
            assert member in parts[title], (title, member)
            others = [t for t in titles if t != title]
            assert not any(member in parts[t] for t in others), (title, member)


def test_the_far_tail_switch_reaches_the_lane_and_names_its_stages(tmp_path, monkeypatch) -> None:
    # User ruling 2026-10-04 「加开关」: the far-tail lane gets its own switch.
    control, lanes_path = _control(tmp_path, monkeypatch)
    lane_state = control.state()["strategy_lanes"]["lanes"]["tail_short"]
    assert lane_state["enabled"] is True and lane_state["opening_stages"] == ["P0"]
    out = control.set_lane_enabled("tail_short", False, by="test")
    assert out["strategy_lanes"]["lanes"]["tail_short"]["enabled"] is False
    assert lanes.lane_enabled("tail_short", lanes_path) is False
    assert out["lane_stakes"]["tail_short"]["enabled"] is False
    source = (ROOT / "scripts" / "calibration_dashboard.py").read_text(encoding="utf-8")
    table = source[source.index("const LC_LANES = {"):]
    assert "tail_short: {name:\"远尾做空 H1\"" in table[: table.index("\n};\n")]


def test_the_charter_and_agents_carry_the_ruling() -> None:
    text = (ROOT / "docs" / "constitution" / "SYSTEM_CHARTER.md").read_text(encoding="utf-8")
    body = text[text.index(f"\n{CHARTER}. "):]
    assert "（占号）" not in body.splitlines()[1]
    for phrase in ("顶部增加：远尾做空 H1 的单笔预算调整", "还有Proper 下注线的单笔预算调整按钮",
                   "STAKE_DIALS", "/api/lane-stake", "stake_full_position", "去掉同心线的按钮"):
        assert phrase in body
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert f"宪章 {CHARTER}" in agents
