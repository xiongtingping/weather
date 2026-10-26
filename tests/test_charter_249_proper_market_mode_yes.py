"""Charter 249: the Proper 下注线 reopens on YES at the market's modal bucket.

User rulings 2026-10-03 「重开Proper 线并放开 YES，只开YES，市场众数桶，挂单」
and 「限p0」.  One leg a station-day: YES on the bucket the market itself
prices highest, rested post-only at the best bid plus one tick, only under
``p``, only while the station-day is in P0 and more than 36 hours out.  The
station-day is judged on its NEWEST P0 decision on every pass until a plan is
made, and every re-post asks the standing book which bucket the mode is and
the newest P0 envelope what ``p`` is.  The far-tail lane (charter 234) is no
longer held back by another lane's acknowledged rung on a different bucket.
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

from bayes_basket_target import lane_orders  # noqa: E402
from bayes_basket_target import proper_bet_lane as lane  # noqa: E402
from bayes_basket_target import strategy_lanes  # noqa: E402
from bayes_basket_target import tail_short_lane  # noqa: E402
from bayes_basket_target.p1_tail_lane import open_buy_orders_on_station_day  # noqa: E402
from bayes_basket_target.tiebreak_lane import resting_order_fields  # noqa: E402
import tests.test_charter_231_proper_bet_lane as base  # noqa: E402
import tests.test_charter_234_tail_short_lane as tails  # noqa: E402
from tests.test_charter_231_proper_bet_lane import (  # noqa: E402,F401 -- ``store`` is a fixture
    ADMIT,
    ED,
    NOW,
    SETTINGS,
    _end,
    _fact,
    _intents,
    _ladder,
    _resting_transport,
    _row,
    _seed,
    _snapshot,
    store,
)
from tests.test_tiebreak_lane import ST, TZ  # noqa: E402

CHARTER = "249"
YES_ONLY = frozenset({lane.SIDE_YES})
#: The model above the market on the market's own mode (27°C, mid 0.49).
WARM = {"27°C": 0.60, "28°C": 0.30, "29°C": 0.10}
V3 = {**SETTINGS, "rule": lane.RULE_MAKER_MODE_YES_V3, "sides": ["YES"],
      "buckets": "market_mode", "entry": "latest_decision",
      "freshness": "envelope_validity"}
#: An envelope valid through the whole station-day window of these tests.
LONG = "2026-09-20T00:00:00Z"


def _run(conn, sent, now=NOW, **overrides):
    return lane.execute_proper_bets(
        conn, transport=_resting_transport(sent), now_utc=now, live_submit_authorized=True,
        settings={**V3, **overrides}, admission=ADMIT,
    )


@pytest.fixture()
def warm(monkeypatch):
    """The base suite's ``_fact`` writes ``MODEL``; this suite's is ``WARM``."""

    monkeypatch.setattr(base, "MODEL", dict(WARM))


# ---------------------------------------------------------------------------
# The rule.


def test_the_market_mode_is_the_largest_mid_and_the_lower_rung_on_a_tie() -> None:
    order = ["26°C", "27°C", "28°C"]
    assert lane.market_mode_bucket({"26°C": 0.2, "27°C": 0.5, "28°C": 0.3}, order) == "27°C"
    assert lane.market_mode_bucket({"26°C": 0.4, "27°C": 0.4, "28°C": 0.2}, order) == "26°C"
    assert lane.market_mode_bucket({}, order) is None


def test_only_the_yes_leg_on_the_markets_mode_is_kept() -> None:
    reading = lane.evaluate_ladder(WARM, _ladder(), cash_usdc=None, min_shares={}, ticks={},
                                   sides=YES_ONLY, buckets=lane.BUCKETS_MARKET_MODE)
    assert reading["refusal"] is None and reading["market_mode_bucket"] == "27°C"
    assert [(leg["bucket"], leg["side"], leg["price"]) for leg in reading["legs"]] == [("27°C", "YES", 0.49)]
    # One leg, so lambda puts it at the venue's five-share minimum.
    assert reading["legs"][0]["target_shares"] == 5.0
    quotes = {q["bucket"]: q for q in reading["quotes"]}
    assert quotes["27°C"]["reason"] is None
    # Legs off the mode obey the rule on their own book and are recorded, not posted.
    assert quotes["28°C"]["reason"] == lane.NOT_MARKET_MODE and quotes["28°C"]["price"] is not None
    assert quotes["29°C"]["reason"] == lane.NOT_MARKET_MODE


def test_a_model_under_the_market_on_the_mode_is_not_bet() -> None:
    # p(27°C) = 0.10 against a mid of 0.49: the Log position there is NO, and
    # the lane's sides are YES only.
    reading = lane.evaluate_ladder(base.MODEL, _ladder(), cash_usdc=None, min_shares={}, ticks={},
                                   sides=YES_ONLY, buckets=lane.BUCKETS_MARKET_MODE)
    assert reading["refusal"] == "side_stopped:NO" and reading["legs"] == []


def test_the_rung_on_the_mode_must_be_under_p() -> None:
    # Mids sum to 1.05, so q(27°C) = 0.486 and p = 0.50 is above it -- but
    # bid + 1 tick is 0.51, not under p.
    book = [
        _row("27°C", 27, [(0.50, 200.0)], [(0.52, 200.0)]),
        _row("28°C", 28, [(0.30, 200.0)], [(0.32, 200.0)]),
        _row("29°C", 29, [(0.22, 200.0)], [(0.24, 200.0)]),
    ]
    reading = lane.evaluate_ladder({"27°C": 0.50, "28°C": 0.30, "29°C": 0.20}, book, cash_usdc=None,
                                   min_shares={}, ticks={}, sides=YES_ONLY, buckets=lane.BUCKETS_MARKET_MODE)
    assert reading["refusal"] == "no_leg_obeys_rule"
    quote = next(q for q in reading["quotes"] if q["bucket"] == "27°C")
    assert quote["side"] == "YES" and quote["reason"].startswith("price_breaks_rule")


@pytest.mark.parametrize("key,bad", [("buckets", "tails"), ("buckets", None), ("entry", "every_tick")])
def test_a_scope_or_entry_nobody_can_act_on_raises(key, bad) -> None:
    reader = lane.bucket_scope if key == "buckets" else lane.entry_rule
    if bad is None:
        assert reader({}) in {lane.BUCKETS_ALL, lane.ENTRY_FIRST_DECISION}
        return
    with pytest.raises(ValueError):
        reader({key: bad})


# ---------------------------------------------------------------------------
# The lane end to end.


def test_one_yes_rung_rests_on_the_modal_bucket(store, warm) -> None:
    _seed(store)
    sent: list = []
    receipt = _run(store, sent)
    assert receipt["rule"] == "proper_bet_lane_v3"
    assert receipt["planned"] == 1 and receipt["rungs"] == 1 and receipt["submitted"] == 1
    (_path, payload), = sent
    assert payload["token_id"] == "t27" and payload["outcome_side"] == "YES"
    assert payload["price"] == pytest.approx(0.49) and payload["size"] == 5.0
    assert payload["order_type"] == "GTD" and payload["post_only"] is True
    plan = json.loads(store.execute(f"SELECT plan_json FROM {lane.EVALUATION_TABLE}").fetchone()[0])
    assert plan["buckets"] == "market_mode" and plan["entry"] == "latest_decision"
    assert plan["market_mode_bucket"] == "27°C" and plan["sides"] == ["YES"]
    assert [(leg["bucket"], leg["side"]) for leg in plan["legs"]] == [("27°C", "YES")]


def test_the_newest_decision_is_judged_until_a_plan_is_made(store, warm) -> None:
    # 29°C is asked at 0.20 with nobody bidding: the market reference refuses
    # the ladder, as it does for most ladders two days out.
    thin = _ladder()
    thin[2] = _row("29°C", 29, [], [(0.20, 200.0)])
    _seed(store, book=thin)
    sent: list = []
    first = _run(store, sent)
    assert first["evaluated"] == 0 and first["waiting"] == 1 and not sent
    assert list(first["waiting_reasons"]) == ["market_incomplete:one_sided_ask:29°C"]
    # At 13:30 the first decision's envelope (valid to 13:00) has run out.
    # Charter 231's entry wrote the station-day off an hour after the
    # decision; this one waits for the next decision.
    late = _run(store, sent, now="2026-09-18T13:30:00.000Z")
    assert late["evaluated"] == 0 and list(late["waiting_reasons"]) == ["envelope_expired"]
    assert store.execute(f"SELECT COUNT(*) FROM {lane.EVALUATION_TABLE}").fetchone()[0] == 0
    # A newer P0 decision on a ladder now quoted whole: planned.
    decided = "2026-09-18T13:40:00.000Z"
    _snapshot(store, "snap_f2", decided, _ladder(), 2)
    _fact(store, stage="P0", fact_id="f2", decided=decided, snapshot_id="snap_f2", fresh_until=LONG)
    planned = _run(store, sent, now="2026-09-18T13:40:20.000Z")
    assert planned["planned"] == 1 and len(sent) == 1 and sent[0][1]["token_id"] == "t27"
    row = store.execute(f"SELECT decision_at_utc, rule_version FROM {lane.EVALUATION_TABLE}").fetchone()
    assert row == (decided, "proper_bet_lane_v3")
    # One plan a station-day: a later decision does not open a second.
    again = _run(store, sent, now="2026-09-18T13:41:00.000Z")
    assert again["planned"] == 0 and len(sent) == 1


def test_a_plan_is_made_while_the_envelope_is_valid_however_old_the_decision(store, warm) -> None:
    """Charter 249 (十), 「按信封有效期」: on 2026-10-03 the newest P0 decision was
    3.7 h old on 35 of 36 station-days of 10-05, every envelope still valid."""

    _seed(store, fresh_until="2026-09-18T18:00:00Z")
    late = "2026-09-18T15:30:00.000Z"             # three and a half hours on
    _snapshot(store, "snap_late", late, _ladder(), 2)
    sent: list = []
    by_age = _run(store, sent, now=late, freshness="decision_age")
    assert by_age["planned"] == 0 and list(by_age["waiting_reasons"]) == ["decision_stale"] and not sent
    planned = _run(store, sent, now=late)
    assert planned["planned"] == 1 and len(sent) == 1 and sent[0][1]["token_id"] == "t27"


def test_a_rung_is_not_re_posted_on_an_envelope_that_ran_out(store, warm) -> None:
    _seed(store)                                   # valid to 13:00
    sent: list = []
    _run(store, sent)
    assert len(sent) == 1
    _end(store, _intents(store)[0][0])
    after = "2026-09-18T13:10:00.000Z"
    _snapshot(store, "snap_f2", after, _ladder(), 2)
    waited = _run(store, sent, now=after)
    assert waited["rungs"] == 0 and len(sent) == 1
    legs = json.loads(store.execute(f"SELECT legs_state_json FROM {lane.EVALUATION_TABLE}").fetchone()[0])
    assert legs["YES:27°C"]["status"] == "waiting:envelope_expired"
    # A newer P0 decision with a valid envelope: the leg is posted again.
    decided = "2026-09-18T13:12:00.000Z"
    _snapshot(store, "snap_f3", decided, _ladder(), 3)
    _fact(store, stage="P0", fact_id="f3", decided=decided, snapshot_id="snap_f3", fresh_until=LONG)
    again = _run(store, sent, now="2026-09-18T13:12:20.000Z")
    assert again["rungs"] == 1 and len(sent) == 2
    # Under the one-hour age rule the re-post never looked at the envelope.
    assert lane.freshness_rule({}) == "decision_age"
    with pytest.raises(ValueError, match="proper_bet_freshness_unknown"):
        lane.freshness_rule({"freshness": "sometimes"})


def test_a_day_that_has_left_p0_is_not_opened(store, warm) -> None:
    _seed(store)
    later = "2026-09-18T12:05:00.000Z"
    _snapshot(store, "snap_f2", later, _ladder(), 2)
    _fact(store, stage="P1", fact_id="f2", decided=later, snapshot_id="snap_f2")
    sent: list = []
    receipt = _run(store, sent, now="2026-09-18T12:05:20.000Z")
    assert receipt["planned"] == 0 and receipt["evaluated"] == 0 and not sent


def test_inside_thirty_six_hours_is_written_once(store, warm) -> None:
    _seed(store, decided="2026-09-19T05:00:00.000Z", observed="2026-09-19T05:00:00.000Z")
    sent: list = []
    receipt = _run(store, sent, now="2026-09-19T05:00:10.000Z")
    assert receipt["refusals"][0].startswith("inside_36h") and not sent
    again = _run(store, sent, now="2026-09-19T05:01:00.000Z")
    assert again["evaluated"] == 0 and again["waiting"] == 0


def test_a_repost_waits_while_the_bucket_is_not_the_markets_mode(store, warm) -> None:
    _seed(store)
    sent: list = []
    _run(store, sent)
    _end(store, _intents(store)[0][0])  # the first rung expired unfilled
    later = "2026-09-18T12:12:00.000Z"
    moved = [
        _row("27°C", 27, [(0.30, 200.0)], [(0.32, 200.0)]),
        _row("28°C", 28, [(0.48, 200.0)], [(0.50, 200.0)]),
        _row("29°C", 29, [(0.18, 200.0)], [(0.20, 200.0)]),
    ]
    _snapshot(store, "snap_f2", later, moved, 2)
    receipt = _run(store, sent, now=later)
    assert receipt["rungs"] == 0 and len(sent) == 1
    legs = json.loads(store.execute(f"SELECT legs_state_json FROM {lane.EVALUATION_TABLE}").fetchone()[0])
    assert legs["YES:27°C"]["status"] == "waiting:not_market_mode:28°C"
    # The mode comes back: the same leg is posted again on the book that stands.
    back = "2026-09-18T12:20:00.000Z"
    _snapshot(store, "snap_f3", back, _ladder(), 3)
    assert _run(store, sent, now=back)["rungs"] == 1 and sent[-1][1]["token_id"] == "t27"


def test_a_repost_reads_the_newest_p0_envelope(store, warm, monkeypatch) -> None:
    _seed(store)
    sent: list = []
    _run(store, sent)
    _end(store, _intents(store)[0][0])
    # The next P0 decision puts 27°C at 0.45: bid + 1 tick (0.49) is no longer under p.
    later = "2026-09-18T12:12:00.000Z"
    monkeypatch.setattr(base, "MODEL", {"27°C": 0.45, "28°C": 0.35, "29°C": 0.20})
    _snapshot(store, "snap_f2", later, _ladder(), 2)
    _fact(store, stage="P0", fact_id="f2", decided=later, snapshot_id="snap_f2")
    receipt = _run(store, sent, now="2026-09-18T12:12:20.000Z")
    assert receipt["rungs"] == 0 and len(sent) == 1
    legs = json.loads(store.execute(f"SELECT legs_state_json FROM {lane.EVALUATION_TABLE}").fetchone()[0])
    assert legs["YES:27°C"]["status"].startswith("waiting:")
    assert legs["YES:27°C"]["last_quote"]["p"] == pytest.approx(0.45, abs=1e-6)


def test_every_rung_names_the_envelope_its_p_was_read_from(store, warm, monkeypatch) -> None:
    """On 2026-10-04, 10 of the lane's 18 filled rungs were priced on a later
    P0 envelope than the plan's, and the rung kept only the number."""

    _seed(store)
    sent: list = []
    _run(store, sent)
    first = json.loads(_intents(store)[0][6])["rung"]
    assert first["p"] == pytest.approx(0.60, abs=1e-6)
    assert (first["p_envelope_id"], first["p_envelope_sha256"], first["p_envelope_decision_at_utc"]) == (
        "env_f1", ("f1" * 64)[:64], base.DECIDED)
    _end(store, _intents(store)[0][0])
    # The next P0 decision moves 27°C to 0.55; the re-post is priced on it.
    later = "2026-09-18T12:12:00.000Z"
    monkeypatch.setattr(base, "MODEL", {"27°C": 0.55, "28°C": 0.33, "29°C": 0.12})
    _snapshot(store, "snap_f2", later, _ladder(), 2)
    _fact(store, stage="P0", fact_id="f2", decided=later, snapshot_id="snap_f2")
    assert _run(store, sent, now="2026-09-18T12:12:20.000Z")["rungs"] == 1
    second = json.loads(_intents(store)[1][6])["rung"]
    assert second["p"] == pytest.approx(0.55, abs=1e-6)
    assert (second["p_envelope_id"], second["p_envelope_sha256"], second["p_envelope_decision_at_utc"]) == (
        "env_f2", ("f2" * 64)[:64], later)


def test_a_rung_under_a_dollar_is_sized_up_to_it(store, warm) -> None:
    cheap = [
        _row("27°C", 27, [(0.15, 200.0)], [(0.17, 200.0)]),
        _row("28°C", 28, [(0.10, 200.0)], [(0.12, 200.0)]),
        _row("29°C", 29, [(0.08, 200.0)], [(0.10, 200.0)]),
    ]
    _seed(store, book=cheap)
    sent: list = []
    receipt = _run(store, sent)
    assert receipt["rungs"] == 1
    payload = sent[0][1]
    # Five shares at 0.16 are 0.80 USDC; the venue takes no order under a dollar.
    assert payload["price"] == pytest.approx(0.16) and payload["size"] == pytest.approx(6.25)
    assert payload["size"] * payload["price"] >= 1.0 - 1e-9


def test_a_rung_rests_fifteen_minutes_and_never_past_the_window(store, warm) -> None:
    _seed(store, fresh_until=LONG)
    sent: list = []
    _run(store, sent, rung_horizon_seconds=900.0)
    now = lane._utc(NOW).timestamp()
    assert sent[0][1]["expiration"] - now == pytest.approx(900.0, abs=1.0)
    body = json.loads(_intents(store)[0][6])
    assert body["expiration_basis"] == "rung_horizon_seconds"
    # Ten minutes before the 36-hour line the rung is signed to end on it ...
    _end(store, _intents(store)[0][0])
    near = "2026-09-19T03:50:00.000Z"   # 2026-09-20 in Shanghai settles 09-20T16:00Z
    _snapshot(store, "snap_f2", near, _ladder(), 2)
    _run(store, sent, now=near, rung_horizon_seconds=900.0)
    assert len(sent) == 2
    assert sent[1][1]["expiration"] - lane._utc(near).timestamp() == pytest.approx(600.0, abs=1.0)
    # ... and with less than the venue's floor left it is not posted at all.
    _end(store, _intents(store)[1][0], at="2026-09-19T03:55:00.000Z")
    last = "2026-09-19T03:57:00.000Z"
    _snapshot(store, "snap_f3", last, _ladder(), 3)
    receipt = _run(store, sent, now=last, rung_horizon_seconds=900.0)
    assert receipt["rungs"] == 0 and len(sent) == 2
    legs = json.loads(store.execute(f"SELECT legs_state_json FROM {lane.EVALUATION_TABLE}").fetchone()[0])
    assert legs["YES:27°C"]["status"] == "waiting:window_closing"


def test_the_venue_minimum_is_still_a_horizon_the_lane_can_be_set_to(store, warm) -> None:
    from bayes_basket_target.production import (  # noqa: PLC0415
        GTD_SIGNING_MARGIN_SECONDS,
        VENUE_GTD_MINIMUM_HORIZON_SECONDS,
    )
    _seed(store)
    sent: list = []
    _run(store, sent, rung_horizon_seconds=0.0)
    assert sent[0][1]["expiration"] - lane._utc(NOW).timestamp() == pytest.approx(
        VENUE_GTD_MINIMUM_HORIZON_SECONDS + GTD_SIGNING_MARGIN_SECONDS, abs=1.0)
    with pytest.raises(ValueError):
        lane.rung_horizon_seconds({"rung_horizon_seconds": -1})


# ---------------------------------------------------------------------------
# The far-tail lane beside a resting Proper rung.


def test_only_an_acknowledged_rung_of_another_lane_on_another_bucket_lets_a_take_through() -> None:
    resting = {"post_only": True, "state": "submitted", "lane": "proper_bet", "bucket": "28°C"}
    assert tail_short_lane.rests_on_another_bucket(resting, "24°C") is True
    assert tail_short_lane.rests_on_another_bucket(resting, "28°C") is False
    for change in ({"state": "reconcile_required"}, {"state": "submitting"}, {"state": "prepared"},
                   {"post_only": False}, {"lane": tail_short_lane.LANE}):
        assert tail_short_lane.rests_on_another_bucket({**resting, **change}, "24°C") is False


def _rest_a_proper_rung(conn, bucket, sent):
    venue = resting_order_fields(now_utc=tails.NOW, limit_price=0.31, belief_fresh_until_utc=tails.NOW)
    return lane_orders.send_lane_buy(
        conn, lane=lane.LANE, intent_id=f"proper_test_{bucket}", station_id=ST, event_date=tails.ED,
        settlement_timezone=TZ, bucket=bucket, token_id=f"yes_{bucket}", price=0.31, shares=5.0,
        side="YES", execution_candidate=lane.EXECUTION_CANDIDATE, expiration=int(venue["expiration"]),
        tick_size=0.01,
        intent_body={"schema": lane.SCHEMA, "order_type": venue["order_type"], "post_only": venue["post_only"]},
        transport=_resting_transport(sent), live_submit_authorized=True,
        decision_details={"schema": "proper_bet_lane_decision_v2", "lane": lane.LANE},
    )


def test_a_proper_rung_on_the_mode_does_not_stop_the_far_tail_takes(store, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(tail_short_lane, "_cash_refusal", lambda conn, now_utc, needed: None)
    monkeypatch.setattr(tail_short_lane, "PAUSE_FLAG_PATH", tmp_path / "live_paused.flag")
    monkeypatch.setattr("bayes_basket_target.lane_orders.live_intent_armed", lambda: True)
    monkeypatch.setattr("bayes_basket_target.lane_orders.egress_send_gate", lambda: {"hold": False})
    tails._seed(store)
    entry = _rest_a_proper_rung(store, "28°C", [])
    assert entry["event_type"] == "POST_ACK"
    orders = open_buy_orders_on_station_day(store, ST, tails.ED)
    assert [(o["lane"], o["bucket"], o["state"], o["post_only"]) for o in orders] == [
        ("proper_bet", "28°C", "submitted", True)]
    sent: list = []
    receipt = tails._run(store, sent)
    assert receipt["planned"] == 3 and receipt["filled"] == 3
    assert sorted(r[0] for r in tails._intents(store) if r[2] == "NO") == ["24°C", "25°C", "31°C"]


def test_a_rung_on_the_same_bucket_still_holds_the_take_back(store, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(tail_short_lane, "_cash_refusal", lambda conn, now_utc, needed: None)
    monkeypatch.setattr(tail_short_lane, "PAUSE_FLAG_PATH", tmp_path / "live_paused.flag")
    monkeypatch.setattr("bayes_basket_target.lane_orders.live_intent_armed", lambda: True)
    monkeypatch.setattr("bayes_basket_target.lane_orders.egress_send_gate", lambda: {"hold": False})
    tails._seed(store)
    _rest_a_proper_rung(store, "24°C", [])
    sent: list = []
    receipt = tails._run(store, sent)
    assert receipt["planned"] == 2
    assert sorted(r[0] for r in tails._intents(store) if r[2] == "NO") == ["25°C", "31°C"]
    refusal = store.execute(
        f"SELECT refusal FROM {tail_short_lane.EVALUATION_TABLE} WHERE bucket='24°C'"
    ).fetchone()[0]
    assert refusal == "open_order:proper_bet:submitted"


# ---------------------------------------------------------------------------
# What ships.


def test_v3_stays_selectable_after_charter_265() -> None:
    # Charter 265 (user ruling 2026-10-10 「改」) ships v4 -- the standard rule
    # on the realisation layer's belief, see test_charter_265_run_change_belief;
    # v3 (YES on the market mode, read off the weather vector) stays a rule the
    # lane file can name, and this suite pins it through V3.
    assert lane.RULE_VERSIONS[lane.RULE_MAKER_MODE_YES_V3] == "proper_bet_lane_v3"
    assert lane.allowed_sides(V3) == YES_ONLY and lane.bucket_scope(V3) == "market_mode"
    assert lane.belief_source(V3) == "weather_p"
    defaults = strategy_lanes.PROPER_BET_DEFAULT_SETTINGS
    assert defaults["entry"] == "latest_decision" and defaults["opening_stages"] == ["P0", "P1"]
    assert defaults["min_hours_to_settlement"] == 0.0 and defaults["rung_horizon_seconds"] == 900.0
    assert defaults["freshness"] == "envelope_validity"


def test_the_charter_and_agents_carry_the_ruling() -> None:
    text = (ROOT / "docs" / "constitution" / "SYSTEM_CHARTER.md").read_text(encoding="utf-8")
    body = text[text.index(f"\n{CHARTER}. "):]
    for phrase in ("重开Proper 线并放开 YES，只开YES，市场众数桶，挂单", "限p0",
                   "log_proper_maker_market_mode_yes_v3", "latest_decision", "rests_on_another_bucket",
                   "加长", "rung_horizon_seconds", "按信封有效期", "envelope_validity",
                   "envelope_expired"):
        assert phrase in body
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    # Charter 265 rewrote the AGENTS line: v3 (YES on the market mode) is named there as the fitted restriction.
    assert f"宪章 {CHARTER}" in agents and "众数 YES" in agents
    assert len(agents.splitlines()) <= 104
