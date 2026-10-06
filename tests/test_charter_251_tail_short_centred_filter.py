"""Charter 251 (六): the 远尾做空线's model filter reads the served P0 vector
moved to the market's centre.

User ruling 2026-10-07 「改」: where the model's centre leaves the market's,
rule v2 read the far tail on the far side of the move as cheaper than it was
and the near side as dearer.  Rule v3 moves the served vector along the
station's ladder until its mean is the market's, floors it as served, and
judges p < YES bid on that -- the model's width and shape, the market's
centre.  Everything else is v2's.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target import realisation_layer  # noqa: E402
from bayes_basket_target import strategy_lanes  # noqa: E402
from bayes_basket_target import tail_short_lane as lane  # noqa: E402
from bayes_basket_target.basket_kernel.valuation_v1 import market_reference_v1  # noqa: E402
from bayes_basket_target.materialization import floor_probabilities  # noqa: E402
import tests.test_charter_234_tail_short_lane as tails  # noqa: E402
from tests.test_charter_234_tail_short_lane import _fill_transport, _intents, _seed  # noqa: E402
from tests.test_charter_251_tail_short_model_filter import MODEL, _envelope, _evaluations  # noqa: E402
from tests.test_tiebreak_lane import ADMIT, store  # noqa: E402,F401

#: Charter 234's test ladder: mode 28°C; the far-tail legs are 24°C (bid 2c),
#: 25°C (bid 3c) and 31°C (bid 3c).
#: Two rungs warmer than ``MODEL`` and about as narrow.
WARM = {"24°C": 0.001, "25°C": 0.001, "26°C": 0.01, "27°C": 0.02, "28°C": 0.05,
        "29°C": 0.20, "30°C": 0.35, "31°C": 0.22, "32°C": 0.08}
#: Centred near 30°C and wide.
WIDE_WARM = {"24°C": 0.002, "25°C": 0.008, "26°C": 0.03, "27°C": 0.07, "28°C": 0.13,
             "29°C": 0.19, "30°C": 0.21, "31°C": 0.19, "32°C": 0.17}


def _served(model):
    total = sum(model.values())
    floored, _ = floor_probabilities({b: v / total for b, v in model.items()})
    return {"state": "ok", "envelope_id": "env_x", "probabilities": floored}


def _run(conn, sent, rule=lane.RULE_V3):
    return lane.execute_tail_short(
        conn, transport=_fill_transport(sent), now_utc=tails.NOW, live_submit_authorized=True,
        settings={**strategy_lanes.TAIL_SHORT_DEFAULT_SETTINGS, "live_ws_book": False, "rule": rule},
        admission=ADMIT,
    )


# ---------------------------------------------------------------------------
# The vector on its own.


def test_the_centred_vector_sits_on_the_market_mean_with_the_model_spread() -> None:
    book = tails._book()
    order = list(tails.LADDER)
    centred = lane.centred_vector(_served(WIDE_WARM), book)
    assert centred["state"] == "ok" and centred["envelope_id"] == "env_x"
    q = realisation_layer.floored([market_reference_v1(book)["vector"][b] for b in order])
    p = [centred["probabilities"][b] for b in order]
    assert realisation_layer.moments(realisation_layer.floored(p))[0] == pytest.approx(
        realisation_layer.moments(q)[0], abs=0.02)
    # It moved about 1.7 rungs colder, and is floored as the served vector is
    # (lifted to the floor, then renormalised -- a lifted bucket ends a hair under it).
    assert centred["centre_shift_rungs"] == pytest.approx(-1.72, abs=0.01)
    assert min(p) == pytest.approx(1e-3, rel=1e-2) and sum(p) == pytest.approx(1.0)


def test_a_model_already_on_the_market_centre_reads_as_served() -> None:
    served = _served(MODEL)
    centred = lane.centred_vector(served, tails._book())
    for bucket, bid in (("24°C", 0.02), ("25°C", 0.03), ("31°C", 0.03)):
        assert lane.model_filter(centred, bucket, bid)["refusal"] == lane.model_filter(served, bucket, bid)["refusal"]


def test_states_that_refuse_without_barring() -> None:
    book = tails._book()
    assert lane.centred_vector({"state": "envelope_expired"}, book)["state"] == "envelope_expired"
    short = _served({b: v for b, v in MODEL.items() if b != "32°C"})
    assert lane.centred_vector(short, book)["state"] == "ladder_mismatch"
    crossed = [dict(row, bid_levels=[{"price": 0.5, "shares": 10.0}]) if row["bucket"] == "24°C" else row
               for row in book]
    assert lane.centred_vector(_served(MODEL), crossed)["state"] == "market_incomplete"
    assert lane.model_filter(lane.centred_vector(short, book), "24°C", 0.02)["refusal"] == (
        f"{lane.MODEL_UNAVAILABLE}:ladder_mismatch")


# ---------------------------------------------------------------------------
# The lane end to end: v2 and v3 on the same book.


def test_v3_refuses_the_far_side_tail_a_warm_model_passed(store) -> None:
    _seed(store)
    _envelope(store, model=WIDE_WARM)
    v2_sent: list = []
    # v2 on this envelope takes the two cold tails: the model's warm centre
    # puts them at 0.2% and 0.8%.
    assert lane.model_filter(_served(WIDE_WARM), "24°C", 0.02)["refusal"] is None
    receipt = _run(store, v2_sent)
    assert receipt["rule"] == "tail_short_lane_v3"
    assert v2_sent == [] and _intents(store) == []
    rows = _evaluations(store)
    assert rows["24°C"][0] == rows["25°C"][0] == lane.MODEL_NOT_BELOW_BID
    # model_p is the p the filter read -- the centred one.
    assert rows["25°C"][1] == pytest.approx(lane.centred_vector(_served(WIDE_WARM), tails._book())
                                            ["probabilities"]["25°C"], abs=1e-6)
    assert rows["25°C"][2] == "env_f1"


def test_v3_takes_the_near_side_tail_a_warm_model_refused(store) -> None:
    _seed(store)
    _envelope(store, model=WARM)
    assert lane.model_filter(_served(WARM), "31°C", 0.03)["refusal"] == lane.MODEL_NOT_BELOW_BID
    sent: list = []
    receipt = _run(store, sent)
    assert sorted(r[0] for r in _intents(store)) == ["24°C", "25°C", "31°C"]
    assert receipt["filled"] == 3
    plan = next(json.loads(r[6])["plan"] for r in _intents(store) if r[0] == "31°C")
    assert plan["rule"] == "tail_short_lane_v3" and plan["model_envelope_id"] == "env_f1"
    assert plan["model_centre_shift_rungs"] == pytest.approx(-1.99, abs=0.01)
    assert plan["model_p"] < 0.03


def test_v2_on_the_same_envelope_is_unchanged(store) -> None:
    _seed(store)
    _envelope(store, model=WARM)
    sent: list = []
    receipt = _run(store, sent, rule=lane.RULE_V2)
    assert receipt["rule"] == "tail_short_lane_v2"
    assert sorted(r[0] for r in _intents(store)) == ["24°C", "25°C"]
    assert _evaluations(store)["31°C"][0] == lane.MODEL_NOT_BELOW_BID


# ---------------------------------------------------------------------------
# Shipped and written down.


def test_v3_is_the_shipped_rule() -> None:
    assert strategy_lanes.TAIL_SHORT_DEFAULT_SETTINGS["rule"] == lane.RULE_V3 == lane.DEFAULT_RULE
    assert lane.RULE_VERSIONS[lane.RULE_V3] == "tail_short_lane_v3"
    assert lane.MODEL_FILTER_RULES == {lane.RULE_V2, lane.RULE_V3}
    assert lane.CENTRED_FILTER_RULES == {lane.RULE_V3}


def test_the_charter_and_agents_carry_the_centred_filter() -> None:
    charter = (ROOT / "docs" / "constitution" / "SYSTEM_CHARTER.md").read_text()
    clause = next(line for line in charter.splitlines() if line.startswith("251. "))
    assert "（六）中心对齐过滤" in clause and lane.RULE_V3 in clause and "centred_vector" in clause
    assert "251（六）" in (ROOT / "AGENTS.md").read_text()
