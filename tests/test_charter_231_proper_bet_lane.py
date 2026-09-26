"""Charter 231: the Proper 下注线 replaces the basket's betting rule.

User rulings 2026-09-26 「调整下注规则」 (arXiv 2607.06166, LogScore) and
「实盘上线，p＝天气模型」: the weather model's served vector ``p`` against the
market's normalised mid ladder ``q`` gives the proper position
``s = log(p / q)`` on every bucket; YES where ``p`` clears the ask, NO where
``p`` is under the YES bid, nothing inside the band; one limit FOK per leg,
``lambda * |s|`` shares.
"""

from __future__ import annotations

import json
import math
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from bayes_basket_target import order_submission as sub  # noqa: E402
from bayes_basket_target import proper_bet_lane as lane  # noqa: E402
from bayes_basket_target import strategy_lanes  # noqa: E402
from bayes_basket_target.inventory_producer import _ledger_fill_rows  # noqa: E402
from bayes_basket_target.ssot import apply_production_ssot_schema  # noqa: E402
from tests.test_tiebreak_lane import (  # noqa: E402,F401 -- ``store`` is a fixture
    ADMIT,
    ORDS,
    ST,
    TZ,
    _insert,
    store,
)

ED = "2026-09-20"
DECIDED = "2026-09-18T12:02:00.000Z"
NOW = "2026-09-18T12:02:30.000Z"
TOKENS = {"27°C": ("t27", "n27"), "28°C": ("t28", "n28"), "29°C": ("t29", "n29")}
SETTINGS = {"stake_usdc": 20.0, "opening_stages": ["P0"], "live_ws_book": False,
            "send_book_fresh_seconds": 60.0, "first_decision_fresh_seconds": 3600.0}


def _row(bucket, ordinal, bids, asks):
    return {"bucket": bucket, "ordinal": ordinal, "ask_available": True,
            "ask_levels": [{"price": p, "shares": s} for p, s in asks],
            "bid_levels": [{"price": p, "shares": s} for p, s in bids]}


def _ladder():
    """27°C 0.48/0.50, 28°C 0.30/0.32, 29°C 0.18/0.20: mids sum to 0.99."""

    return [
        _row("27°C", 27, [(0.48, 200.0)], [(0.50, 200.0)]),
        _row("28°C", 28, [(0.30, 200.0)], [(0.32, 200.0)]),
        _row("29°C", 29, [(0.18, 200.0)], [(0.20, 200.0)]),
    ]


MODEL = {"27°C": 0.10, "28°C": 0.30, "29°C": 0.60}


def _seed(conn, *, stage="P0", probabilities=None, book=None, decided=DECIDED, fact_id="f1",
          observed=None, no_token=True):
    probabilities = probabilities or MODEL
    observed = observed or decided
    _insert(conn, "basket_probability_envelope", {
        "envelope_id": f"env_{fact_id}", "stage": stage, "checkpoint_id": f"cp_{fact_id}", "station_id": ST,
        "event_date": ED, "settlement_timezone": TZ, "decision_at_utc": decided,
        "fresh_until_utc": "2026-09-18T13:00:00Z", "bucket_ordinals_json": json.dumps(ORDS),
        "probabilities_json": json.dumps(probabilities), "content_sha256": (fact_id * 64)[:64],
        "state": "COMMITTED", "created_at": decided, "lineage_json": "{}",
    })
    _insert(conn, "basket_official_ws_snapshot", {
        "snapshot_id": f"snap_{fact_id}", "market_id": "m", "station_id": ST, "event_date": ED,
        "settlement_timezone": TZ, "sequence": 1, "observed_at": observed, "quote_version": "v",
        "quote_observed_at": observed, "quote_expires_at": "2026-09-18T13:00:00Z",
        "quote_source": "OFFICIAL_CLOB_WS", "remaining_budget_usdc": 100.0,
        "buckets_json": json.dumps(book if book is not None else _ladder()), "state": "COMMITTED",
        "created_at": observed,
    })
    _insert(conn, "basket_policy_training_fact", {
        "fact_id": fact_id, "station_id": ST, "event_date": ED, "settlement_timezone": TZ,
        "decision_at_utc": decided, "decision_scope_key": f"scope_{fact_id}", "captured_at_utc": decided,
        "source_sequence": 1, "quote_observed_at": decided, "quote_expires_at": "2026-09-18T13:00:00Z",
        "stage": stage, "probability_envelope_id": f"env_{fact_id}", "ws_snapshot_id": f"snap_{fact_id}",
        "intent_count": 0, "authority_scope": "PRODUCTION_NO_LIVE_FORWARD", "created_at_utc": decided,
        "cycle_id": f"cyc_{fact_id}", "decision_id": f"dec_{fact_id}",
    })
    registry_sha = f"reg{fact_id}".ljust(64, "0")[:64]
    _insert(conn, "basket_target_market_registry_snapshot",
            {"snapshot_sha256": registry_sha, "source_scope": "shared_live_registry_export_only"})
    for bucket, (yes, no) in TOKENS.items():
        raw = {"order_price_min_tick_size": 0.01, "order_min_size": 5.0}
        if no_token:
            raw["no_token"] = no
        _insert(conn, "basket_target_market_registry", {
            "snapshot_sha256": registry_sha, "slug": f"{ST}-{bucket}", "event_date": ED,
            "outcome": bucket, "city_slug": "shanghai", "station_id": ST, "settlement_timezone": TZ,
            "yes_token": yes, "no_token": no, "market_active": 1, "market_accepting_orders": 1,
            "registry_json": json.dumps(raw), "refreshed_at": decided, "source_eligible": 1,
            "source_lane": "awc",
        })


def _fill_transport(sent: list):
    def transport(path, payload):
        sent.append((path, payload))
        return {"ok": True, "accepted": True, "exchange_order_id": f"0x{len(sent)}",
                "status": "matched", "shares_matched": payload["size"],
                "exchange": {"size_matched": payload["size"],
                             "makingAmount": payload["size"] * payload["price"]}}
    return transport


# ---------------------------------------------------------------------------
# The rule.


def test_the_log_proper_position_and_divergence_on_the_paper_example() -> None:
    p = {"<=32": 0.10, "33": 0.30, "34": 0.40, ">=35": 0.20}
    q = {"<=32": 0.20, "33": 0.25, "34": 0.30, ">=35": 0.25}
    s = lane.proper_position(p, q)
    assert [round(s[b], 3) for b in p] == [-0.693, 0.182, 0.288, -0.223]
    d = lane.divergence_kl(p, q)
    assert d == pytest.approx(sum(q[b] * math.log(q[b] / p[b]) for b in q))
    assert d > 0.0
    # The pointwise identity the attribution closes on: s.(1_y - q) = log(p_y/q_y) + D.
    for y in p:
        lhs = sum(s[b] * ((1.0 if b == y else 0.0) - q[b]) for b in q)
        assert lhs == pytest.approx(math.log(p[y] / q[y]) + d)


def test_no_is_bought_only_under_the_yes_bid_not_under_one_minus_the_bid() -> None:
    """The pasted text had ``p < 1 - bid``; corrected before the ruling."""

    row = _row("x", 0, [(0.25, 100.0)], [(0.35, 100.0)])
    inside = lane.band_leg("x", 0.30, -0.1, row)
    # 0.30 < 1 - 0.25 would have bought NO at 0.75 for a 0.70 claim.
    assert inside["band"] == "none" and inside["side"] is None
    under = lane.band_leg("x", 0.20, -0.4, row)
    assert under["side"] == lane.SIDE_NO and under["limit_price"] == pytest.approx(0.75)
    assert 1.0 - 0.20 > under["limit_price"]
    assert under["depth_shares"] == 100.0


def test_yes_walks_only_the_ask_levels_under_the_model() -> None:
    row = _row("x", 0, [(0.05, 50.0)], [(0.10, 10.0), (0.15, 20.0), (0.25, 50.0)])
    leg = lane.band_leg("x", 0.20, 0.7, row)
    assert leg["side"] == lane.SIDE_YES
    assert leg["limit_price"] == pytest.approx(0.15) and leg["depth_shares"] == 30.0


def test_a_band_against_the_sign_of_the_position_is_not_traded() -> None:
    row = _row("x", 0, [(0.08, 50.0)], [(0.10, 50.0)])
    assert lane.band_leg("x", 0.12, -0.05, row)["band"] == "sign_conflict"
    assert lane.band_leg("x", 0.05, 0.05, row)["band"] == "sign_conflict"


def test_lambda_keeps_the_ratios_on_the_longest_prefix_that_clears_the_minimum() -> None:
    legs = [
        {"bucket": "a", "s": -1.6, "side": "NO", "limit_price": 0.52, "depth_shares": 500.0},
        {"bucket": "b", "s": 1.1, "side": "YES", "limit_price": 0.20, "depth_shares": 500.0},
        {"bucket": "c", "s": 0.2, "side": "YES", "limit_price": 0.30, "depth_shares": 500.0},
    ]
    mins = {"a": 5.0, "b": 5.0, "c": 5.0}
    wide = lane.size_legs(legs, budget_usdc=100.0, min_shares=mins)
    targets = {leg["bucket"]: leg["target_shares"] for leg in wide["legs"]}
    assert set(targets) == {"a", "b", "c"}
    # x_k = lambda * |s_k|: the proper position's ratios, exactly.
    assert targets["a"] / targets["b"] == pytest.approx(1.6 / 1.1, rel=1e-5)
    assert targets["b"] / targets["c"] == pytest.approx(1.1 / 0.2, rel=1e-5)
    assert sum(abs(l["s"]) * l["limit_price"] for l in legs) * wide["lambda_shares"] == pytest.approx(100.0)
    assert sum(leg["cost_usdc"] for leg in wide["legs"]) <= 100.0 + 1e-9
    # A small stake cannot put five shares on the smallest |s|: it drops out
    # and lambda is re-solved on the rest.
    small = lane.size_legs(legs, budget_usdc=6.0, min_shares=mins)
    assert [leg["bucket"] for leg in small["legs"]] == ["a", "b"]
    assert small["dropped"] == {"c": "below_min_size_at_budget"}
    tiny = lane.size_legs(legs, budget_usdc=1.0, min_shares=mins)
    assert tiny["legs"] == [] and tiny["lambda_shares"] is None


def test_a_leg_is_capped_by_its_in_band_depth() -> None:
    legs = [{"bucket": "a", "s": 1.0, "side": "YES", "limit_price": 0.10, "depth_shares": 9.0}]
    sized = lane.size_legs(legs, budget_usdc=50.0, min_shares={"a": 5.0})
    # 9 shares in band, a fifth held back as the tail lane's takes do.
    assert sized["legs"][0]["shares"] == 7.0


def test_the_ladder_must_be_the_markets_own() -> None:
    reading = lane.evaluate_ladder({"27°C": 0.5, "28°C": 0.5}, _ladder(), budget_usdc=20.0,
                                   min_shares={})
    assert reading["refusal"] == "ladder_mismatch" and reading["market_only"] == ["29°C"]


def test_evaluate_ladder_prices_both_sides_off_one_book() -> None:
    reading = lane.evaluate_ladder(MODEL, _ladder(), budget_usdc=20.0,
                                   min_shares={b: 5.0 for b in MODEL})
    assert reading["refusal"] is None
    q = reading["q"]
    assert q["27°C"] == pytest.approx(0.49 / 0.99)
    assert reading["s"]["27°C"] == pytest.approx(math.log(0.10 / (0.49 / 0.99)))
    bands = {b["bucket"]: b for b in reading["bands"]}
    assert bands["27°C"]["side"] == "NO" and bands["27°C"]["limit_price"] == pytest.approx(0.52)
    assert bands["28°C"]["band"] == "none"
    assert bands["29°C"]["side"] == "YES" and bands["29°C"]["limit_price"] == pytest.approx(0.20)
    legs = {leg["bucket"]: leg for leg in reading["legs"]}
    assert set(legs) == {"27°C", "29°C"}
    assert reading["divergence_kl"] == pytest.approx(lane.divergence_kl(reading["p"], q))


# ---------------------------------------------------------------------------
# End to end.


def test_it_buys_no_and_yes_as_limit_foks_on_their_own_tokens(store) -> None:
    _seed(store)
    sent: list = []
    receipt = lane.execute_proper_bets(
        store, transport=_fill_transport(sent), now_utc=NOW, live_submit_authorized=True,
        settings=SETTINGS, admission=ADMIT,
    )
    assert receipt["evaluated"] == 1 and receipt["planned"] == 1 and receipt["planned_legs"] == 2
    assert receipt["submitted"] == 2 and receipt["filled"] == 2
    payloads = {payload["token_id"]: payload for _path, payload in sent}
    assert set(payloads) == {"n27", "t29"}
    no_leg, yes_leg = payloads["n27"], payloads["t29"]
    assert no_leg["outcome_side"] == "NO" and no_leg["side"] == "BUY"
    assert no_leg["order_type"] == "FOK" and no_leg["post_only"] is False
    assert no_leg["price"] == pytest.approx(0.52) and no_leg["fok_price_cap"] == pytest.approx(0.52)
    assert yes_leg["outcome_side"] == "YES" and yes_leg["price"] == pytest.approx(0.20)
    # lambda * |s|: the NO leg's shares over the YES leg's is |s_27| / |s_29|
    # up to the venue's whole-share rounding.
    q27, q29 = 0.49 / 0.99, 0.19 / 0.99
    ratio = abs(math.log(0.10 / q27)) / abs(math.log(0.60 / q29))
    assert no_leg["size"] / yes_leg["size"] == pytest.approx(ratio, rel=0.08)
    assert no_leg["size"] * 0.52 + yes_leg["size"] * 0.20 <= 20.0 + 1e-9
    intents = dict(store.execute(
        "SELECT token_id, side FROM basket_target_order_intents"
    ).fetchall())
    assert intents == {"n27": "NO", "t29": "YES"}
    decision = store.execute(
        "SELECT decision_id, strategy_lane, details_json FROM auto_trade_decisions"
    ).fetchone()
    assert decision[0] == f"proper_bet_decision_{ST}_{ED}" and decision[1] == "basket"
    details = json.loads(decision[2])
    assert details["decision_signature"]["stage"] == "P0"
    assert details["proper_position"]["s"]["27°C"] < 0 < details["proper_position"]["s"]["29°C"]
    row = store.execute(
        f"SELECT refusal, planned_legs, divergence_kl, p_json, q_json, s_json, sends_json"
        f" FROM {lane.EVALUATION_TABLE}"
    ).fetchone()
    assert row[0] is None and row[1] == 2 and row[2] > 0
    assert json.loads(row[3]) == pytest.approx(MODEL)
    sends = json.loads(row[6])
    assert {s["side"] for s in sends} == {"NO", "YES"}
    assert all(float(s["matched_size"]) > 0 for s in sends)


def test_one_evaluation_per_stage_first_decision(store) -> None:
    _seed(store)
    sent: list = []
    lane.execute_proper_bets(store, transport=_fill_transport(sent), now_utc=NOW,
                             live_submit_authorized=True, settings=SETTINGS, admission=ADMIT)
    again = lane.execute_proper_bets(store, transport=_fill_transport(sent), now_utc=NOW,
                                     live_submit_authorized=True, settings=SETTINGS, admission=ADMIT)
    assert again["evaluated"] == 0 and again["planned"] == 0 and len(sent) == 2


def test_only_the_opening_stages_are_judged(store) -> None:
    _seed(store, stage="P1")
    receipt = lane.execute_proper_bets(store, transport=_fill_transport([]), now_utc=NOW,
                                       live_submit_authorized=True, settings=SETTINGS, admission=ADMIT)
    assert receipt["evaluated"] == 0 and receipt["planned"] == 0


def test_a_stale_book_waits_while_the_decision_is_young(store) -> None:
    _seed(store, observed="2026-09-18T11:50:00.000Z")
    sent: list = []
    receipt = lane.execute_proper_bets(store, transport=_fill_transport(sent), now_utc=NOW,
                                       live_submit_authorized=True, settings=SETTINGS, admission=ADMIT)
    assert receipt["waiting"] == 1 and receipt["evaluated"] == 0 and not sent
    assert store.execute(f"SELECT COUNT(*) FROM {lane.EVALUATION_TABLE}").fetchone()[0] == 0
    late = lane.execute_proper_bets(store, transport=_fill_transport(sent), now_utc="2026-09-18T13:30:00.000Z",
                                    live_submit_authorized=True, settings=SETTINGS, admission=ADMIT)
    assert late["evaluated"] == 1 and late["refusals"] == ["first_decision_stale"] and not sent


def test_a_missing_no_token_is_not_sent(store) -> None:
    _seed(store, no_token=False)
    sent: list = []
    lane.execute_proper_bets(store, transport=_fill_transport(sent), now_utc=NOW,
                             live_submit_authorized=True, settings=SETTINGS, admission=ADMIT)
    assert [payload["token_id"] for _path, payload in sent] == ["t29"]


def test_the_yes_inventory_never_reads_a_no_fill(store) -> None:
    _seed(store)
    lane.execute_proper_bets(store, transport=_fill_transport([]), now_utc=NOW,
                             live_submit_authorized=True, settings=SETTINGS, admission=ADMIT)
    fills = _ledger_fill_rows(store, station_id=ST, event_date=ED)
    assert [fill["token_id"] for fill in fills] == ["t29"]


def test_the_live_book_pays_a_no_leg_when_its_bucket_does_not_settle(store) -> None:
    _seed(store)
    lane.execute_proper_bets(store, transport=_fill_transport([]), now_utc=NOW,
                             live_submit_authorized=True, settings=SETTINGS, admission=ADMIT)
    import live_book  # noqa: PLC0415

    legs = live_book.load_legs(store)
    assert {leg["side"] for leg in legs} == {"NO", "YES"}
    summary = live_book.stage_summary(legs)
    no_shares = summary["held_no_shares"]["27°C"]
    yes_shares = summary["held_shares"]["29°C"]
    assert summary["held_buckets"] == ["29°C"] and summary["filled_legs"] == 2
    for truth, expected in (("27°C", 0.0), ("28°C", no_shares), ("29°C", no_shares + yes_shares)):
        assert live_book._payout(summary["held_shares"], {}, truth, summary["held_no_shares"]) == pytest.approx(expected)


# ---------------------------------------------------------------------------
# The ledger and the switches.


def test_the_submitter_takes_a_no_buy_and_refuses_a_no_sell() -> None:
    base = {"order_intent_id": "i", "idempotency_key": "i", "token_id": "n", "limit_price": 0.52,
            "requested_size": 10.0, "execution_candidate": "TAKER_FOK", "fok_price_cap": 0.52,
            "order_type": "FOK", "post_only": False}
    payload = sub.build_place_order_payload({**base, "side": "NO", "order_action": "BUY"})
    assert payload["outcome_side"] == "NO" and payload["side"] == "BUY"
    with pytest.raises(sub.OrderSubmissionError, match="no_leg_sell_invalid"):
        sub.build_place_order_payload({**base, "side": "NO", "order_action": "SELL"})
    with pytest.raises(sub.OrderSubmissionError, match="outcome_side_invalid"):
        sub.build_place_order_payload({**base, "side": "MAYBE", "order_action": "BUY"})


def test_the_retired_no_side_trigger_is_gone_from_the_schema(tmp_path) -> None:
    conn = sqlite3.connect(tmp_path / "s.db", isolation_level=None)
    apply_production_ssot_schema(conn)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
    assert "basket_target_intents_no_no_side_insert" not in names


def test_the_lane_is_switched_on_and_the_basket_off() -> None:
    assert strategy_lanes.LANE_PROPER_BET in strategy_lanes.KNOWN_LANES
    defaults = strategy_lanes.PROPER_BET_DEFAULT_SETTINGS
    assert defaults["rule"] == lane.RULE_LOG_V1 and defaults["opening_stages"] == ["P0"]
    shipped = strategy_lanes.load_strategy_lanes(ROOT / "config" / "trading" / "strategy_lanes_v1.json")
    assert shipped["source"] == "file"
    assert shipped["lanes"]["proper_bet"]["enabled"] is True
    assert shipped["lanes"]["basket"]["enabled"] is False


def test_the_charter_and_agents_carry_the_ruling() -> None:
    charter = (ROOT / "docs" / "constitution" / "SYSTEM_CHARTER.md").read_text(encoding="utf-8")
    assert "\n231. **Proper betting" in charter
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert "禁止任何自动买 NO 策略" not in agents
    assert "宪章 231" in agents
