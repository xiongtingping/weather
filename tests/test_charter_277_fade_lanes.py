"""Charter 277: the 反向线 -- fading the model where it leans away from the market.

User ruling 2026-10-22 「把格 A 和格 B 做成车道并开启实盘，单笔5u，前端统计数据」.
A: the station-day's newest stage is P0-M and its served mode is warm of the
market's mode -> FOK YES on the bucket one rung cold of the market's mode.
B: the newest stage is P0, more than 36 h out, the modes differ and the means
are more than 0.76 rungs apart -> FOK NO on the model's mode.
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

from bayes_basket_target import fade_lane as lane  # noqa: E402
from bayes_basket_target import strategy_lanes  # noqa: E402
from tests.test_charter_234_tail_short_lane import (  # noqa: E402,F401 -- ``store`` is a fixture
    ED,
    _book,
    _fill_transport,
    _seed,
    store,
)
from tests.test_tiebreak_lane import ADMIT, ST, TZ, _insert  # noqa: E402

DECIDED = "2026-09-18T12:02:00.000Z"
NOW = "2026-09-18T12:02:30.000Z"        # ~52h to settlement (ED settles 09-20T16:00Z)
NOW_P0M = "2026-09-19T00:02:30.000Z"    # ~40h out
ORDER = ["24°C", "25°C", "26°C", "27°C", "28°C", "29°C", "30°C", "31°C", "32°C"]
#: Market mode 28°C (the test ladder of charter 234).  A model mode of 29°C is
#: one rung warm of it; the cold neighbour of the market's mode is 27°C.
MODEL_WARM = {"24°C": 0.005, "25°C": 0.01, "26°C": 0.03, "27°C": 0.10, "28°C": 0.28, "29°C": 0.33,
              "30°C": 0.17, "31°C": 0.06, "32°C": 0.015}
MODEL_COLD = {b: MODEL_WARM[ORDER[len(ORDER) - 1 - i]] for i, b in enumerate(ORDER)}
MODEL_SAME = {"24°C": 0.01, "25°C": 0.02, "26°C": 0.07, "27°C": 0.18, "28°C": 0.36, "29°C": 0.2,
              "30°C": 0.1, "31°C": 0.04, "32°C": 0.02}


def _envelope(conn, *, stage, model, envelope_id="env_f1", fresh_until="2026-09-20T13:00:00Z", decided=DECIDED):
    _insert(conn, "basket_probability_envelope", {
        "envelope_id": envelope_id, "stage": stage, "checkpoint_id": f"cp_{envelope_id}", "station_id": ST,
        "event_date": ED, "settlement_timezone": TZ, "decision_at_utc": decided,
        "fresh_until_utc": fresh_until, "bucket_ordinals_json": json.dumps({b: i for i, b in enumerate(ORDER)}),
        "probabilities_json": json.dumps(model), "content_sha256": (envelope_id * 64)[:64],
        "state": "COMMITTED", "created_at": decided, "lineage_json": "{}",
    })


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch, tmp_path):
    monkeypatch.setattr(lane, "_cash_refusal", lambda conn, now_utc, needed: None)
    monkeypatch.setattr(lane, "PAUSE_FLAG_PATH", tmp_path / "live_paused.flag")
    monkeypatch.setattr("bayes_basket_target.lane_orders.live_intent_armed", lambda: True)
    monkeypatch.setattr("bayes_basket_target.lane_orders.egress_send_gate", lambda: {"hold": False})


def _settings(which: str, **overrides):
    base = strategy_lanes.FADE_P0M_DEFAULT_SETTINGS if which == "fade_p0m" else strategy_lanes.FADE_P0_DEFAULT_SETTINGS
    return {**base, "live_ws_book": False, **overrides}


def _run(conn, sent, *, which, now, fill=True, **overrides):
    return lane.execute_fade(
        conn, lane=which, transport=_fill_transport(sent, fill=fill), now_utc=now, live_submit_authorized=True,
        settings=_settings(which, **overrides), admission=ADMIT,
    )


def _intents(conn):
    return conn.execute(
        "SELECT bucket, token_id, side, limit_price, requested_size, state FROM basket_target_order_intents"
    ).fetchall()


def _evaluations(conn, which):
    return conn.execute(
        "SELECT bucket, side, refusal, sent, filled_shares FROM fade_lane_evaluation WHERE lane=?"
        " ORDER BY evaluated_at_utc, bucket", (which,)
    ).fetchall()


# ---------------------------------------------------------------------------
# The rule on one ladder.


def test_rule_a_fires_yes_one_rung_cold_of_the_market_mode_when_the_model_is_warm() -> None:
    reading = lane.fade_reading(MODEL_WARM, _book(), rule=lane.RULE_A)
    assert reading["state"] == "complete"
    assert reading["model_mode_bucket"] == "29°C" and reading["market_mode_bucket"] == "28°C"
    assert reading["mode_gap_rungs"] == 1
    assert reading["leg"] == {"bucket": "27°C", "side": "YES", "p": pytest.approx(MODEL_WARM["27°C"]),
                              "q": pytest.approx(reading["q"]["27°C"])}


def test_rule_a_does_not_fire_when_the_model_is_cold_or_agrees() -> None:
    assert lane.fade_reading(MODEL_COLD, _book(), rule=lane.RULE_A)["refusal"] == "signal:model_not_warm_of_market"
    assert lane.fade_reading(MODEL_SAME, _book(), rule=lane.RULE_A)["refusal"] == "signal:modes_agree"


def test_rule_b_fires_no_on_the_model_mode_only_past_the_mean_gap() -> None:
    far = lane.fade_reading(MODEL_WARM, _book(), rule=lane.RULE_B, min_mean_gap_rungs=0.5)
    assert far["leg"] == {"bucket": "29°C", "side": "NO", "p": pytest.approx(MODEL_WARM["29°C"]),
                          "q": pytest.approx(far["q"]["29°C"])}
    assert far["mean_gap_rungs"] > 0.5
    near = lane.fade_reading(MODEL_WARM, _book(), rule=lane.RULE_B, min_mean_gap_rungs=2.0)
    assert near["leg"] is None and near["refusal"] == "signal:mean_gap_small"
    assert lane.fade_reading(MODEL_SAME, _book(), rule=lane.RULE_B, min_mean_gap_rungs=0.0)["refusal"] == "signal:modes_agree"


def test_a_ladder_the_vector_does_not_cover_is_refused() -> None:
    assert lane.fade_reading({"28°C": 1.0}, _book(), rule=lane.RULE_A)["state"] == "ladder_mismatch"


# ---------------------------------------------------------------------------
# Lane A on the store.


def test_lane_a_buys_the_cold_neighbour_once_and_records_the_fill(store) -> None:
    _seed(store, stage="P0-M", decided=DECIDED, observed=NOW_P0M)
    _envelope(store, stage="P0-M", model=MODEL_WARM)
    sent: list = []
    receipt = _run(store, sent, which="fade_p0m", now=NOW_P0M)
    assert receipt["planned"] == 1 and receipt["submitted"] == 1 and receipt["filled"] == 1, receipt
    (bucket, token, side, price, size, state), = _intents(store)
    assert (bucket, token, side, state) == ("27°C", "yes_27°C", "YES", "filled")
    assert price == pytest.approx(0.20)   # the best ask of 27°C on the test ladder
    assert size == pytest.approx(25.0)    # 5 USDC at 0.20, under the 100-share level
    rows = _evaluations(store, "fade_p0m")
    assert [(r[0], r[1], r[2], r[3]) for r in rows] == [("27°C", "YES", None, 1)]
    assert rows[0][4] == pytest.approx(25.0)
    # The next pass holds: the leg is held to settlement.
    again = _run(store, sent, which="fade_p0m", now="2026-09-19T00:20:00.000Z")
    assert again["planned"] == 0 and again["skipped"].get("held") == 1
    assert len(sent) == 1


def test_lane_a_holds_when_the_model_is_cold_of_the_market_and_writes_it_once(store) -> None:
    _seed(store, stage="P0-M", decided=DECIDED, observed=NOW_P0M)
    _envelope(store, stage="P0-M", model=MODEL_COLD)
    sent: list = []
    receipt = _run(store, sent, which="fade_p0m", now=NOW_P0M)
    assert receipt["planned"] == 0 and sent == []
    assert _evaluations(store, "fade_p0m") == [("28°C", "YES", "signal:model_not_warm_of_market", 0, 0.0)]
    _run(store, sent, which="fade_p0m", now="2026-09-19T00:20:00.000Z")
    assert len(_evaluations(store, "fade_p0m")) == 1   # written once a station-day


def test_lane_a_does_not_read_a_p0_station_day(store) -> None:
    _seed(store, stage="P0", decided=DECIDED, observed=NOW)
    _envelope(store, stage="P0", model=MODEL_WARM)
    sent: list = []
    receipt = _run(store, sent, which="fade_p0m", now=NOW)
    assert receipt["planned"] == 0 and receipt["evaluated"] == 0 and sent == []


def test_lane_a_waits_on_an_expired_envelope(store) -> None:
    _seed(store, stage="P0-M", decided=DECIDED, observed=NOW_P0M)
    _envelope(store, stage="P0-M", model=MODEL_WARM, fresh_until="2026-09-18T13:00:00Z")
    sent: list = []
    receipt = _run(store, sent, which="fade_p0m", now=NOW_P0M)
    assert receipt["planned"] == 0 and receipt["skipped"].get("envelope_expired") == 1 and sent == []


def test_lane_a_stops_inside_its_window(store) -> None:
    _seed(store, stage="P0-M", decided=DECIDED, observed="2026-09-20T10:00:00.000Z")
    _envelope(store, stage="P0-M", model=MODEL_WARM)
    sent: list = []
    receipt = _run(store, sent, which="fade_p0m", now="2026-09-20T10:00:30.000Z")   # 6h out
    assert receipt["planned"] == 0 and receipt["skipped"].get("outside_window") == 1


# ---------------------------------------------------------------------------
# Lane B on the store.


def test_lane_b_buys_no_on_the_model_mode_when_the_means_are_far(store) -> None:
    _seed(store, stage="P0", decided=DECIDED, observed=NOW)
    _envelope(store, stage="P0", model=MODEL_WARM)
    sent: list = []
    receipt = _run(store, sent, which="fade_p0", now=NOW, min_mean_gap_rungs=0.5)
    assert receipt["planned"] == 1 and receipt["filled"] == 1, receipt
    (bucket, token, side, price, size, state), = _intents(store)
    assert (bucket, token, side, state) == ("29°C", "no_29°C", "NO", "filled")
    assert price == pytest.approx(1.0 - 0.18)   # 1 - the YES best bid of 29°C
    assert _evaluations(store, "fade_p0")[0][:4] == ("29°C", "NO", None, 1)


def test_lane_b_holds_under_the_gap_and_inside_36h(store) -> None:
    _seed(store, stage="P0", decided=DECIDED, observed=NOW)
    _envelope(store, stage="P0", model=MODEL_WARM)
    sent: list = []
    receipt = _run(store, sent, which="fade_p0", now=NOW, min_mean_gap_rungs=5.0)
    assert receipt["planned"] == 0 and sent == []
    assert _evaluations(store, "fade_p0") == [("28°C", "NO", "signal:mean_gap_small", 0, 0.0)]
    late = _run(store, sent, which="fade_p0", now="2026-09-19T06:00:00.000Z", min_mean_gap_rungs=0.5)  # 34h
    assert late["planned"] == 0 and late["skipped"].get("outside_window") == 1


def test_a_killed_take_is_retried_up_to_the_attempt_limit(store) -> None:
    _seed(store, stage="P0-M", decided=DECIDED, observed=NOW_P0M)
    _envelope(store, stage="P0-M", model=MODEL_WARM)
    sent: list = []
    # Four passes on the same fresh book (a killed FOK is a send that
    # reached the venue; the next pass may try again at once).
    for i in range(4):
        _run(store, sent, which="fade_p0m", now=f"2026-09-19T00:02:3{i + 1}.000Z", fill=False)
    assert len(sent) == 3
    refusals = [r[2] for r in _evaluations(store, "fade_p0m")]
    assert "max_attempts_reached" in refusals


# ---------------------------------------------------------------------------
# The switches and dials.


def test_both_lanes_are_known_dialled_and_on_by_default() -> None:
    assert strategy_lanes.LANE_FADE_P0M in strategy_lanes.KNOWN_LANES
    assert strategy_lanes.LANE_FADE_P0 in strategy_lanes.KNOWN_LANES
    for name in (strategy_lanes.LANE_FADE_P0M, strategy_lanes.LANE_FADE_P0):
        assert strategy_lanes.DEFAULT_LANES[name]["enabled"] is True
        assert strategy_lanes.DEFAULT_LANES[name]["stake_usdc"] == 5.0
        assert strategy_lanes.STAKE_DIALS[name]["bounds"] == (1.0, 100.0)
    assert strategy_lanes.FADE_P0_DEFAULT_SETTINGS["min_mean_gap_rungs"] == 0.76
    assert strategy_lanes.FADE_P0_DEFAULT_SETTINGS["min_hours_to_settlement"] == 36.0
    assert strategy_lanes.FADE_P0M_DEFAULT_SETTINGS["stage"] == "P0-M"


def test_the_lane_file_has_both_lanes_live_at_five() -> None:
    doc = json.loads((ROOT / "config" / "trading" / "strategy_lanes_v1.json").read_text(encoding="utf-8"))
    for name in ("fade_p0m", "fade_p0"):
        assert doc["lanes"][name]["enabled"] is True
        assert doc["lanes"][name]["stake_usdc"] == 5.0


def test_the_daemon_runs_both_lanes_at_head_and_tail() -> None:
    text = (ROOT / "scripts" / "weather_basket_yes_daemon.py").read_text(encoding="utf-8")
    assert text.count("execute_fade(") == 2
    assert "LANE_FADE_P0M" in text and "LANE_FADE_P0" in text
