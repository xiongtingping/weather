"""Charter 280: the 共识线 -- every estimate on one bucket at local noon.

User ruling 2026-10-23 「做成车道」; rule v2 「把共识线投票改成 D0+P1+市场三家全一致」.
At the first cycle inside local 12:00-13:00 of the event day every estimate is
read once; D0, P1 and the market all present and on one bucket -> FOK YES on it
at the best ask.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from bayes_basket_target import consensus_lane as lane  # noqa: E402
from bayes_basket_target import strategy_lanes  # noqa: E402
from tests.test_charter_234_tail_short_lane import (  # noqa: E402,F401 -- ``store`` is a fixture
    _fill_transport,
    _seed,
    store,
)
from tests.test_tiebreak_lane import ADMIT  # noqa: E402

DECIDED = "2026-09-20T03:30:00.000Z"
#: ED 2026-09-20 in Asia/Shanghai: local noon is 04:00Z.
NOW = "2026-09-20T04:10:00.000Z"
#: D0, P1 and the market agree; F and member Bayes do not, and do not vote.
AGREE = {"F": "27°C", "member_bayes": "29°C", "D0": "28°C", "P1": "28°C", "market": "28°C", "MOS": "27°C"}
SPLIT = {**AGREE, "D0": "29°C"}
VOTERS = ("D0", "P1", "market")


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch, tmp_path):
    monkeypatch.setattr(lane, "_cash_refusal", lambda conn, now_utc, needed: None)
    monkeypatch.setattr(lane, "PAUSE_FLAG_PATH", tmp_path / "live_paused.flag")
    monkeypatch.setattr("bayes_basket_target.lane_orders.live_intent_armed", lambda: True)
    monkeypatch.setattr("bayes_basket_target.lane_orders.egress_send_gate", lambda: {"hold": False})


def _run(conn, sent, *, now=NOW, picks=AGREE, fill=True, calls=None, **overrides):
    def read_votes(c, station, day, when):
        if calls is not None:
            calls.append(when)
        return dict(picks)

    return lane.execute_consensus(
        conn, transport=_fill_transport(sent, fill=fill), now_utc=now, live_submit_authorized=True,
        settings={**strategy_lanes.CONSENSUS_NOON_DEFAULT_SETTINGS, "live_ws_book": False, **overrides},
        admission=ADMIT, read_votes=read_votes,
    )


def _evaluations(conn):
    return conn.execute(
        "SELECT bucket, refusal, sent, voters, agree FROM consensus_lane_evaluation ORDER BY evaluated_at_utc"
    ).fetchall()


def test_only_d0_p1_and_the_market_vote_and_all_three_must_be_there() -> None:
    v = lane.consensus_reading(AGREE, voters=VOTERS, min_voters=3)
    assert (v["bucket"], v["voters"], v["agree"], v["refusal"]) == ("28°C", 3, 3, None)
    # Every estimate read is written down; MOS is not read.
    assert v["votes"] == {"F": "27°C", "member_bayes": "29°C", "D0": "28°C", "P1": "28°C", "market": "28°C"}
    assert lane.consensus_reading(SPLIT, voters=VOTERS, min_voters=3)["refusal"] == "signal:not_all_agree"
    no_d0 = {**AGREE, "D0": None}
    assert lane.consensus_reading(no_d0, voters=VOTERS, min_voters=3)["refusal"] == "signal:too_few_voters"


def test_it_buys_the_agreed_bucket_once_inside_the_noon_window(store) -> None:
    _seed(store, stage="P1", decided=DECIDED, observed=NOW)
    sent: list = []
    receipt = _run(store, sent)
    assert receipt["planned"] == 1 and receipt["filled"] == 1, receipt
    (bucket, side, price, state), = store.execute(
        "SELECT bucket, side, limit_price, state FROM basket_target_order_intents").fetchall()
    assert (bucket, side, state) == ("28°C", "YES", "filled")
    assert price == pytest.approx(0.32)   # the best ask of 28°C on the test ladder
    (row,) = _evaluations(store)
    assert row == ("28°C", None, 1, 3, 3)
    again = _run(store, sent, now="2026-09-20T04:30:00.000Z")
    assert again["planned"] == 0 and again["skipped"].get("held") == 1 and len(sent) == 1


def test_a_split_vote_is_written_once_and_ends_the_station_day(store) -> None:
    _seed(store, stage="P1", decided=DECIDED, observed=NOW)
    sent: list = []
    calls: list = []
    _run(store, sent, picks=SPLIT, calls=calls)
    # Agreement later in the window does not reopen it: the vote is read once.
    later = _run(store, sent, now="2026-09-20T04:40:00.000Z", picks=AGREE, calls=calls)
    assert sent == [] and later["skipped"].get("voted") == 1 and len(calls) == 1
    assert _evaluations(store) == [("28°C", "signal:not_all_agree", 0, 3, 2)]


def test_outside_local_noon_nothing_is_read(store) -> None:
    _seed(store, stage="P1", decided=DECIDED, observed="2026-09-20T03:50:00.000Z")
    sent: list = []
    calls: list = []
    early = _run(store, sent, now="2026-09-20T03:50:30.000Z", calls=calls)   # 11:50 local
    late = _run(store, sent, now="2026-09-20T05:00:30.000Z", calls=calls)    # 13:00 local
    assert early["skipped"].get("outside_window") == 1 and late["skipped"].get("outside_window") == 1
    assert calls == [] and sent == []


def test_an_ask_outside_the_band_is_refused(store) -> None:
    _seed(store, stage="P1", decided=DECIDED, observed=NOW)
    sent: list = []
    receipt = _run(store, sent, max_ask=0.30)
    assert receipt["planned"] == 0 and sent == []
    assert _evaluations(store)[0][1] == "ask_outside:0.320"


def test_the_lane_is_known_dialled_off_in_code_and_live_in_the_file() -> None:
    assert strategy_lanes.LANE_CONSENSUS_NOON in strategy_lanes.KNOWN_LANES
    assert strategy_lanes.STAKE_DIALS[strategy_lanes.LANE_CONSENSUS_NOON]["bounds"] == (1.0, 100.0)
    assert strategy_lanes.CONSENSUS_NOON_DEFAULT_SETTINGS["enabled"] is False
    assert strategy_lanes.CONSENSUS_NOON_DEFAULT_SETTINGS["voters"] == ["D0", "P1", "market"]
    doc = json.loads((ROOT / "config" / "trading" / "strategy_lanes_v1.json").read_text(encoding="utf-8"))
    # Live by the ruling 2026-10-23 「开启共识线实盘，单笔 5 U」, rule v2.
    lane_doc = doc["lanes"]["consensus_noon"]
    assert lane_doc["enabled"] is True and lane_doc["stake_usdc"] == 5.0
    assert lane_doc["rule"] == lane.RULE and lane_doc["voters"] == ["D0", "P1", "market"]
    text = (ROOT / "scripts" / "weather_basket_yes_daemon.py").read_text(encoding="utf-8")
    assert text.count("execute_consensus(") == 2
