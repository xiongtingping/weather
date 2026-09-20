"""「秤砣线」-- the tiebreak lane (charters 146 and 163).

At a stage's first decision, when the market's normalised mid puts at least
0.40 on the model's mode bucket and that bucket is one rung from the
market's own mode, the lane BIDS for the model's bucket: one resting
post-only GTD rung at the best bid plus one tick, the stake's worth of
shares, once per station-day, through the shared buy sender, with a
decision row the live book can count it under (charter 163, user ruling
2026-09-20 "秤砣线从吃单改为post only，挂best买一+1"; it crossed at the
walked ask from 09-18 to then).  Every first decision is scored into
``tiebreak_lane_evaluation`` whether or not it fires.
"""

from __future__ import annotations

import json
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
from bayes_basket_target import tiebreak_lane as lane  # noqa: E402
from bayes_basket_target.account_equity import ensure_account_equity_table  # noqa: E402
from bayes_basket_target.market_registry import apply_target_market_registry_schema  # noqa: E402
from bayes_basket_target.ssot import apply_production_ssot_schema  # noqa: E402

ST, ED, TZ = "ZSPD", "2026-09-19", "Asia/Shanghai"
NOW = "2026-09-18T12:10:00.000Z"
DECIDED = "2026-09-18T12:02:00.000Z"
ORDS = {"27°C": 27, "28°C": 28, "29°C": 29}
TOKENS = {"27°C": "t27", "28°C": "t28", "29°C": "t29"}
SHA = "b" * 64
ADMIT = lambda station, conn: {"admitted": True, "reason": "test"}  # noqa: E731


def _insert(conn: sqlite3.Connection, table: str, values: dict) -> None:
    """Insert ``values``, filling every other NOT NULL column with a
    type-appropriate placeholder (the charter 144 fixture's helper)."""

    row = dict(values)
    for _cid, name, ctype, notnull, default, _pk in conn.execute(f"PRAGMA table_info({table})"):
        if name in row or not notnull or default is not None:
            continue
        lname = str(name)
        if "sha256" in lname:
            row[name] = SHA
        elif lname.endswith("_json"):
            row[name] = "{}"
        elif str(ctype).upper().startswith(("INT", "REAL", "NUM")):
            row[name] = 1
        else:
            row[name] = "x"
    columns = list(row)
    conn.execute(
        f"INSERT INTO {table}({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
        [row[c] for c in columns],
    )


def _book(asks_28=((0.46, 30.0), (0.47, 100.0)), bid_28=0.44) -> list[dict]:
    """A near-tie ladder: the market leans 27°C over 28°C, 84% on the two;
    28°C is quoted 0.44/0.46, a two-cent spread (v2.1 admits up to three)."""

    def row(bucket, ask, bid, asks=None):
        levels = [{"price": p, "shares": s} for p, s in (asks or ((ask, 200.0),))]
        return {"bucket": bucket, "ask_levels": levels,
                "bid_levels": ([{"price": bid, "shares": 200.0}] if bid is not None else []),
                "ask_available": True}
    return [row("27°C", 0.50, 0.48), row("28°C", 0.46, bid_28, asks_28), row("29°C", 0.09, 0.07)]


@pytest.fixture
def store(tmp_path, monkeypatch) -> sqlite3.Connection:
    conn = sqlite3.connect(tmp_path / "target.db", isolation_level=None)
    apply_production_ssot_schema(conn)
    apply_target_market_registry_schema(conn)
    armed = tmp_path / "intent.json"
    armed.write_text(json.dumps({"live_enabled": True}), encoding="utf-8")
    monkeypatch.setattr(sub, "LIVE_INTENT_PATH", armed)
    return conn


def _envelope_sha(fact_id: str) -> str:
    return (fact_id * 64)[:64]


def _seed(conn, *, stage="P1", probabilities=None, book=None, decided=DECIDED, fact_id="f1", accepting=1, event_date=ED):
    probabilities = probabilities or {"27°C": 0.35, "28°C": 0.45, "29°C": 0.20}
    _insert(conn, "basket_probability_envelope", {
        "envelope_id": f"env_{fact_id}", "stage": stage, "checkpoint_id": f"cp_{fact_id}", "station_id": ST, "event_date": event_date,
        "settlement_timezone": TZ, "decision_at_utc": decided, "fresh_until_utc": "2026-09-18T13:00:00Z",
        "bucket_ordinals_json": json.dumps(ORDS), "probabilities_json": json.dumps(probabilities),
        "content_sha256": _envelope_sha(fact_id), "state": "COMMITTED", "created_at": decided, "lineage_json": "{}",
    })
    _insert(conn, "basket_official_ws_snapshot", {
        "snapshot_id": f"snap_{fact_id}", "market_id": "m", "station_id": ST, "event_date": event_date,
        "settlement_timezone": TZ, "sequence": 1, "observed_at": decided, "quote_version": "v",
        "quote_observed_at": decided, "quote_expires_at": "2026-09-18T13:00:00Z",
        "quote_source": "OFFICIAL_CLOB_WS", "remaining_budget_usdc": 100.0,
        "buckets_json": json.dumps(book if book is not None else _book()), "state": "COMMITTED",
        "created_at": decided,
    })
    _insert(conn, "basket_policy_training_fact", {
        "fact_id": fact_id, "station_id": ST, "event_date": event_date, "settlement_timezone": TZ,
        "decision_at_utc": decided, "decision_scope_key": f"scope_{fact_id}", "captured_at_utc": decided,
        "source_sequence": 1, "quote_observed_at": decided, "quote_expires_at": "2026-09-18T13:00:00Z",
        "stage": stage, "probability_envelope_id": f"env_{fact_id}", "ws_snapshot_id": f"snap_{fact_id}",
        "intent_count": 0, "authority_scope": "PRODUCTION_NO_LIVE_FORWARD", "created_at_utc": decided,
        "cycle_id": f"cyc_{fact_id}", "decision_id": f"dec_{fact_id}",
    })
    registry_sha = f"reg{fact_id}".ljust(64, "0")[:64]
    _insert(conn, "basket_target_market_registry_snapshot", {"snapshot_sha256": registry_sha, "source_scope": "shared_live_registry_export_only"})
    for bucket, token in TOKENS.items():
        _insert(conn, "basket_target_market_registry", {
            "snapshot_sha256": registry_sha, "slug": f"{ST}-{bucket}", "event_date": event_date,
            "outcome": bucket, "city_slug": "shanghai", "station_id": ST, "settlement_timezone": TZ,
            "yes_token": token, "market_active": 1, "market_accepting_orders": accepting,
            "registry_json": json.dumps({"order_price_min_tick_size": 0.01, "order_min_size": 5.0}),
            "refreshed_at": decided, "source_eligible": 1, "source_lane": "awc",
        })


def _resting_transport(sent: list):
    """The venue takes the post-only rung and holds it: POST_ACK, no match."""

    def transport(path, payload):
        sent.append((path, payload))
        return {"ok": True, "accepted": True, "exchange_order_id": "0xrest", "status": "live"}
    return transport


def _expire(conn, intent_id: str, *, at: str) -> None:
    """What the resting lifecycle writes when a rung's GTD horizon runs out
    or it is cancelled off its anchor: the order existed and stopped
    existing without filling."""

    from bayes_basket_target.execution_reconciliation import record_order_event  # noqa: PLC0415

    record_order_event(conn, order_intent_id=intent_id, event={
        "event_type": "EXPIRED", "exchange_order_id": "0xrest", "observed_at": at,
        "matched_size": 0.0, "matched_notional_usdc": 0.0, "event_payload": {"reason": "gtd_expired"},
    })


def _fill_transport(sent: list):
    def transport(path, payload):
        sent.append((path, payload))
        return {"ok": True, "accepted": True, "exchange_order_id": "0xfill",
                "status": "matched", "shares_matched": payload["size"],
                "exchange": {"size_matched": payload["size"], "makingAmount": payload["size"] * payload["price"]}}
    return transport


# ---------------------------------------------------------------------------
# The rule.


def test_v1_fires_only_on_an_adjacent_near_tie() -> None:
    m = {"27°C": 0.50, "28°C": 0.42, "29°C": 0.08}
    p = {"27°C": 0.35, "28°C": 0.45, "29°C": 0.20}
    V1 = dict(min_market_share=0.40, rule=lane.RULE_V1)
    fired = lane.evaluate_rule(p, ORDS, m, **V1)
    assert fired["fired"] is True and fired["rule"] == "tiebreak_adjacent_mode_market_share_v1"
    assert (fired["model_mode"], fired["market_mode"], fired["mode_distance"]) == ("28°C", "27°C", 1)
    assert fired["market_share_on_model_mode"] == pytest.approx(0.42)
    # Modes agree: nothing to break.
    agree = lane.evaluate_rule({"27°C": 0.6, "28°C": 0.3, "29°C": 0.1}, ORDS, m, **V1)
    assert agree["fired"] is False and agree["reason"] == "modes_agree"
    # Two rungs apart is not a tie the market is having.
    far = lane.evaluate_rule({"27°C": 0.2, "28°C": 0.3, "29°C": 0.5}, ORDS, m, **V1)
    assert far["fired"] is False and far["reason"].startswith("modes_not_adjacent")
    # The market must actually be torn: 0.39 on the model's bucket is not.
    torn = lane.evaluate_rule(p, ORDS, {"27°C": 0.53, "28°C": 0.39, "29°C": 0.08}, **V1)
    assert torn["fired"] is False and torn["reason"] == "market_share_below_threshold"
    # Renormalised over the buckets in common, as the reading was.
    partial = lane.evaluate_rule(p, ORDS, {"27°C": 0.25, "28°C": 0.21}, **V1)
    assert partial["fired"] is True and partial["market_share_on_model_mode"] == pytest.approx(0.21 / 0.46)


def test_v2_fires_on_the_model_mode_inside_the_market_mid_band() -> None:
    """v2 (user ruling 2026-09-18 升级到v2): the model's mode bucket priced
    0.40-0.60 by the market, agreeing or not; v1's tie is a subset."""

    V2 = dict(min_market_share=0.40, max_market_share=0.60, rule=lane.RULE_V2)
    p = {"27°C": 0.35, "28°C": 0.45, "29°C": 0.20}
    # Disagreeing modes, market 0.42 on the model's bucket: fires (v1's case).
    tie = lane.evaluate_rule(p, ORDS, {"27°C": 0.50, "28°C": 0.42, "29°C": 0.08}, **V2)
    assert tie["fired"] is True and tie["rule"] == "tiebreak_model_mode_market_midband_v2"
    default = lane.evaluate_rule(p, ORDS, {"27°C": 0.50, "28°C": 0.42, "29°C": 0.08}, min_market_share=0.40, spread=0.02, max_spread=0.031)
    assert default["rule"] == "tiebreak_model_mode_market_midband_spread_maker_v2_2" and default["fired"] is True, "v2.2 is the default"
    # v2.2 is v2.1's predicate: the same book, the same verdict, every field
    # but the version identical.  What charter 163 changed is the order.
    v21 = lane.evaluate_rule(p, ORDS, {"27°C": 0.50, "28°C": 0.42, "29°C": 0.08},
                             min_market_share=0.40, rule=lane.RULE_V21, spread=0.02, max_spread=0.031)
    assert {k: v for k, v in v21.items() if k not in ("rule", "rule_name")} == {
        k: v for k, v in default.items() if k not in ("rule", "rule_name")}
    assert lane.evaluate_rule(p, ORDS, {"27°C": 0.50, "28°C": 0.42, "29°C": 0.08}, min_market_share=0.40, spread=0.05, max_spread=0.031)["reason"] == "spread_too_wide"
    assert lane.evaluate_rule(p, ORDS, {"27°C": 0.50, "28°C": 0.42, "29°C": 0.08}, min_market_share=0.40, spread=None, max_spread=0.031)["reason"] == "spread_unquoted"
    # Agreeing modes, market 0.55 on the shared bucket: fires under v2, not v1.
    agree = lane.evaluate_rule({"27°C": 0.6, "28°C": 0.3, "29°C": 0.1}, ORDS, {"27°C": 0.55, "28°C": 0.35, "29°C": 0.10}, **V2)
    assert agree["fired"] is True and agree["model_mode"] == agree["market_mode"] == "27°C"
    assert lane.evaluate_rule({"27°C": 0.6, "28°C": 0.3, "29°C": 0.1}, ORDS, {"27°C": 0.55, "28°C": 0.35, "29°C": 0.10}, min_market_share=0.40, rule=lane.RULE_V1)["fired"] is False
    # Two rungs apart no longer matters: only the market's price on the model's bucket does.
    far = lane.evaluate_rule({"27°C": 0.2, "28°C": 0.3, "29°C": 0.5}, ORDS, {"27°C": 0.45, "28°C": 0.10, "29°C": 0.45}, **V2)
    assert far["fired"] is True and far["mode_distance"] == 2
    # Below the floor and at or above the ceiling both refuse, by name.
    low = lane.evaluate_rule(p, ORDS, {"27°C": 0.53, "28°C": 0.39, "29°C": 0.08}, **V2)
    assert low["fired"] is False and low["reason"] == "market_share_below_threshold"
    high = lane.evaluate_rule({"27°C": 0.6, "28°C": 0.3, "29°C": 0.1}, ORDS, {"27°C": 0.62, "28°C": 0.30, "29°C": 0.08}, **V2)
    assert high["fired"] is False and high["reason"] == "market_share_above_ceiling"
    with pytest.raises(ValueError):
        lane.evaluate_rule(p, ORDS, {"27°C": 0.5, "28°C": 0.4, "29°C": 0.1}, min_market_share=0.4, rule="no_such_rule")


def test_the_rung_stands_one_tick_over_the_bid_and_spends_at_most_the_stake() -> None:
    """Charter 163: bid+1t, clamped under the ask, and never more than the
    stake.  The price is production's own ladder, so the resting lifecycle
    reads this rung as standing on its anchor rather than off it."""

    row = _book()[1]  # 28C quoted 0.44 / 0.46
    rung = lane.maker_rung(row, 20.0, tick_size=0.01, min_shares=5.0)
    assert rung["refusal"] is None
    assert rung["limit_price"] == 0.45 and rung["best_bid"] == 0.44 and rung["best_ask"] == 0.46
    assert rung["offset_ticks"] == 1.0 and rung["clamped_to_post_only_ceiling"] is False
    assert rung["shares"] == pytest.approx(44.44) and rung["cost_usdc"] <= 20.0
    # A one-tick spread: bid+1t IS the ask, so post-only clamps the rung to
    # the highest price it may rest at -- the bid itself, first in the queue.
    tight = lane.maker_rung(
        {"bucket": "x", "ask_levels": [{"price": 0.45, "shares": 200.0}],
         "bid_levels": [{"price": 0.44, "shares": 200.0}], "ask_available": True},
        20.0, tick_size=0.01, min_shares=5.0)
    assert tight["limit_price"] == 0.44 and tight["clamped_to_post_only_ceiling"] is True
    # No bid is no anchor; no tick is no grid.  Neither is a rung.
    assert lane.maker_rung(
        {"bucket": "x", "ask_levels": [{"price": 0.45, "shares": 200.0}], "bid_levels": [],
         "ask_available": True}, 20.0, tick_size=0.01)["refusal"] == "maker_anchor_unavailable"
    assert lane.maker_rung(_book()[1], 20.0, tick_size=None)["refusal"] == "maker_anchor_unavailable"
    assert lane.maker_rung(None, 20.0, tick_size=0.01)["refusal"] == "maker_anchor_unavailable"
    # A stake too small to reach the venue's minimum size at that price.
    assert lane.maker_rung(_book()[1], 1.0, tick_size=0.01, min_shares=5.0)["refusal"].startswith(
        "order_below_venue_minimum:")


def test_the_gtd_horizon_is_the_belief_that_authorized_the_rung() -> None:
    """Charter 127 (二), read through production's own field builder: the
    rung lives as long as the belief, never under the venue's floor."""

    belief = lane.resting_order_fields(
        now_utc="2026-09-18T12:10:00Z", limit_price=0.45,
        belief_fresh_until_utc="2026-09-18T13:00:00Z")
    assert belief["order_type"] == "GTD" and belief["post_only"] is True
    assert belief["expiration_basis"] == "belief_fresh_until"
    assert belief["expiration"] == int(
        __import__("datetime").datetime.fromisoformat("2026-09-18T13:00:00+00:00").timestamp())
    # A belief expiring inside the venue's three-minute floor is lifted to it.
    floored = lane.resting_order_fields(
        now_utc="2026-09-18T12:10:00Z", limit_price=0.45,
        belief_fresh_until_utc="2026-09-18T12:11:00Z")
    assert floored["expiration_basis"] == "venue_minimum_horizon"
    assert floored["expiration"] > belief["expiration"] - 3600


# ---------------------------------------------------------------------------
# End to end.


def test_it_bids_for_the_model_bucket_post_only_at_bid_plus_one_tick(store) -> None:
    _seed(store)
    sent: list = []
    receipt = lane.execute_tiebreak_entries(
        store, transport=_fill_transport(sent), now_utc=NOW, live_submit_authorized=True,
        settings={"stake_usdc": 20.0}, admission=ADMIT,
    )
    assert receipt["evaluated"] == 1 and receipt["fired"] == 1 and receipt["planned"] == 1
    assert receipt["submitted"] == 1 and receipt["filled"] == 1
    (path, payload), = sent
    assert path == sub.PLACE_ORDER_PATH
    assert payload["token_id"] == "t28" and payload["side"] == "BUY"
    # Charter 163: a resting post-only GTD rung, never a crossing order, and
    # never a price cap (that field belongs to the take this lane stopped
    # sending).  28C is quoted 0.44 / 0.46, so the rung stands at 0.45.
    assert payload["order_type"] == "GTD" and payload["post_only"] is True
    assert payload["price"] == 0.45 and "fok_price_cap" not in payload
    assert payload["expiration"] > 0
    assert payload["size"] * payload["price"] <= 20.0 + 1e-6 and payload["size"] >= 44.0
    intent = store.execute(
        "SELECT decision_id, bucket, order_action, limit_price, requested_size, state, live_submit_authorized,"
        " order_intent_id FROM basket_target_order_intents"
    ).fetchone()
    assert intent[0] == f"tiebreak_decision_{ST}_{ED}" and intent[1] == "28°C" and intent[2] == "BUY"
    assert intent[3] == 0.45 and intent[5] == "filled" and intent[6] == 1
    body = json.loads(store.execute(
        "SELECT intent_json FROM basket_target_order_intents").fetchone()[0])
    assert body["execution_candidate"] == "MAKER" and body["post_only"] is True
    assert body["order_type"] == "GTD" and body["expiration_basis"] == "belief_fresh_until"
    decision = store.execute(
        "SELECT strategy_lane, decided_state, will_order, details_json FROM auto_trade_decisions WHERE decision_id=?",
        (intent[0],),
    ).fetchone()
    # Quiescent from birth, as the basket's own rows are: prepared, will_order 0
    # (the order-state ingress refuses a scope with a live-authorised decision).
    assert decision[:3] == ("basket", "prepared", 0)
    details = json.loads(decision[3])
    assert details["decision_signature"]["stage"] == "P1"
    assert details["forward_evidence_binding"]["probability_artifact_sha256"] == _envelope_sha("f1")
    evaluation = store.execute(
        f"SELECT fired, refusal, order_intent_id, market_share_on_model_mode FROM {lane.EVALUATION_TABLE}"
    ).fetchone()
    # The market's share is read off the normalised MIDS (28°C mid 0.45 of a
    # 1.02 ladder), never the bid or the ask alone.
    assert evaluation[0] == 1 and evaluation[1] is None and evaluation[2] == intent[7]
    assert evaluation[3] == pytest.approx(0.45 / 1.02)
    # v2.1 (charter 146): the row carries the model bucket's book.
    book = store.execute(
        f"SELECT best_ask_on_model_mode, best_bid_on_model_mode, spread_on_model_mode, ask_depth_shares,"
        f" rule_version FROM {lane.EVALUATION_TABLE}"
    ).fetchone()
    assert book[0] == 0.46 and book[1] == 0.44 and book[2] == pytest.approx(0.02) and book[3] == 30.0
    assert book[4] == "tiebreak_model_mode_market_midband_spread_maker_v2_2"
    reconciled = store.execute(
        "SELECT filled_size FROM basket_target_order_reconciliations WHERE order_intent_id=?",
        (evaluation[2],),
    ).fetchone()
    assert reconciled is not None and reconciled[0] == pytest.approx(payload["size"])


def test_the_live_book_counts_the_leg_under_its_decision_with_the_claim(store) -> None:
    _seed(store)
    lane.execute_tiebreak_entries(
        store, transport=_fill_transport([]), now_utc=NOW, live_submit_authorized=True,
        settings={"stake_usdc": 20.0}, admission=ADMIT,
    )
    import live_book  # noqa: PLC0415 -- scripts/ is on the path for this

    legs = live_book.load_legs(store)
    assert len(legs) == 1
    leg = legs[0]
    assert (leg["station_id"], leg["event_date"], leg["stage"], leg["bucket"]) == (ST, ED, "P1", "28°C")
    assert leg["filled_size"] > 44.0 and leg["claimed_probability"] == pytest.approx(0.45)


def test_one_attempt_per_station_day_and_once_per_stage(store) -> None:
    _seed(store)
    sent: list = []
    first = lane.execute_tiebreak_entries(store, transport=_fill_transport(sent), now_utc=NOW,
                                          live_submit_authorized=True, settings={"stake_usdc": 20.0}, admission=ADMIT)
    assert first["submitted"] == 1
    # The same first decision on the next cycle: already evaluated, nothing new.
    again = lane.execute_tiebreak_entries(store, transport=_fill_transport(sent), now_utc=NOW,
                                          live_submit_authorized=True, settings={"stake_usdc": 20.0}, admission=ADMIT)
    assert again["evaluated"] == 0 and again["planned"] == 0 and len(sent) == 1
    # A later stage's first decision on the same station-day fires but is not bought twice.
    _seed(store, stage="P0-M", fact_id="f2", decided="2026-09-18T12:05:00.000Z")
    third = lane.execute_tiebreak_entries(store, transport=_fill_transport(sent), now_utc=NOW,
                                          live_submit_authorized=True, settings={"stake_usdc": 20.0}, admission=ADMIT)
    assert third["evaluated"] == 1 and third["fired"] == 1 and third["planned"] == 0
    assert third["refusals"] == ["station_day_already_attempted"] and len(sent) == 1
    # And the second decision of a stage is never the reading: only the earliest.
    _seed(store, stage="P1", fact_id="f3", decided="2026-09-18T12:08:00.000Z")
    fourth = lane.execute_tiebreak_entries(store, transport=_fill_transport(sent), now_utc=NOW,
                                           live_submit_authorized=True, settings={"stake_usdc": 20.0}, admission=ADMIT)
    assert fourth["evaluated"] == 0


def test_a_stale_first_decision_is_scored_not_bought(store) -> None:
    _seed(store, decided="2026-09-18T09:00:00.000Z")
    sent: list = []
    receipt = lane.execute_tiebreak_entries(store, transport=_fill_transport(sent), now_utc=NOW,
                                            live_submit_authorized=True, settings={"stake_usdc": 20.0}, admission=ADMIT)
    assert receipt["evaluated"] == 1 and receipt["fired"] == 0 and sent == []
    assert receipt["refusals"] == ["first_decision_stale"]


def test_a_decision_that_does_not_fire_is_scored_with_its_reason(store) -> None:
    # The model's mode is 29°C, which the market prices at 0.08: below the band.
    _seed(store, probabilities={"27°C": 0.2, "28°C": 0.3, "29°C": 0.5})
    receipt = lane.execute_tiebreak_entries(store, transport=_fill_transport([]), now_utc=NOW,
                                            live_submit_authorized=True, settings={"stake_usdc": 20.0}, admission=ADMIT)
    assert receipt["evaluated"] == 1 and receipt["fired"] == 0
    assert receipt["refusals"] == ["rule_not_met:market_share_below_threshold"]
    assert receipt["rule"] == "tiebreak_model_mode_market_midband_spread_maker_v2_2"
    row = store.execute(f"SELECT model_mode, market_mode, fired, rule_version FROM {lane.EVALUATION_TABLE}").fetchone()
    assert row == ("29°C", "27°C", 0, "tiebreak_model_mode_market_midband_spread_maker_v2_2")


def test_v2_1_refuses_a_wide_or_one_sided_book_on_the_model_bucket(store) -> None:
    """v2.1 (user ruling 2026-09-18): the model's bucket must be quoted both
    sides at most three cents wide; the reason is on the row, and v2 is
    still one setting away."""

    _seed(store, book=_book(bid_28=0.40))          # 0.40 / 0.46: six cents
    wide = lane.execute_tiebreak_entries(store, transport=_fill_transport([]), now_utc=NOW,
                                         live_submit_authorized=True, settings={"stake_usdc": 20.0}, admission=ADMIT)
    assert wide["fired"] == 0 and wide["refusals"] == ["rule_not_met:spread_too_wide"]
    assert wide["rule"] == "tiebreak_model_mode_market_midband_spread_maker_v2_2"
    row = store.execute(f"SELECT spread_on_model_mode, fired FROM {lane.EVALUATION_TABLE}").fetchone()
    assert row[0] == pytest.approx(0.06) and row[1] == 0
    # The same book under v2 fires: the spread is v2.1's condition alone.
    store.execute(f"DELETE FROM {lane.EVALUATION_TABLE}")
    v2 = lane.execute_tiebreak_entries(store, transport=_fill_transport([]), now_utc=NOW,
                                       live_submit_authorized=True,
                                       settings={"stake_usdc": 20.0, "rule": "model_mode_midband_v2"}, admission=ADMIT)
    assert v2["fired"] == 1 and v2["submitted"] == 1
    # One-sided: no bid on the model's bucket (another station-day, so the
    # first buy above does not shadow it).
    store.execute(f"DELETE FROM {lane.EVALUATION_TABLE}")
    _seed(store, stage="P0-M", fact_id="f2", decided="2026-09-18T12:05:00.000Z", book=_book(bid_28=None), event_date="2026-09-20")
    one_sided = lane.execute_tiebreak_entries(store, transport=_fill_transport([]), now_utc=NOW,
                                              live_submit_authorized=True, settings={"stake_usdc": 20.0}, admission=ADMIT)
    # A bucket priced 0.46 with no bid is refused before the rule is asked --
    # the market vector itself is incomplete on a one-sided ask above the
    # tail cap -- and the rule would refuse it as unquoted if it got there.
    reason = store.execute(
        f"SELECT refusal FROM {lane.EVALUATION_TABLE} WHERE event_date='2026-09-20'"
    ).fetchone()[0]
    assert reason in {"market_incomplete:one_sided_ask:28°C", "rule_not_met:spread_unquoted"}
    assert store.execute("SELECT COUNT(*) FROM basket_target_order_intents WHERE decision_id LIKE '%2026-09-20'").fetchone()[0] == 0
    # A wider ceiling in the lane file admits a six-cent book on a third station-day.
    store.execute(f"DELETE FROM {lane.EVALUATION_TABLE}")
    _seed(store, stage="P1", fact_id="f3", decided="2026-09-18T12:06:00.000Z", book=_book(bid_28=0.40), event_date="2026-09-21")
    sent: list = []
    loose = lane.execute_tiebreak_entries(store, transport=_fill_transport(sent), now_utc=NOW,
                                          live_submit_authorized=True,
                                          settings={"stake_usdc": 20.0, "max_spread_on_model_mode": 0.10}, admission=ADMIT)
    assert loose["submitted"] == 1 and sent[0][1]["token_id"] == "t28"
    assert store.execute(
        f"SELECT fired, refusal FROM {lane.EVALUATION_TABLE} WHERE event_date='2026-09-21'"
    ).fetchone() == (1, None)


def test_the_market_move_since_the_previous_decision_is_recorded_not_gated(store) -> None:
    """The feature the ruling asked to record: the market's share on the
    model's bucket now minus at the station-day's previous decision (any
    stage), the model's mode then, and the gap.  None without a previous
    decision.  It never changes what fires."""

    # A lone first decision: no previous decision on the station-day.
    _seed(store)
    lone = lane.execute_tiebreak_entries(store, transport=_fill_transport([]), now_utc=NOW,
                                         live_submit_authorized=True, settings={"stake_usdc": 20.0}, admission=ADMIT)
    assert lone["fired"] == 1
    row = store.execute(
        f"SELECT market_move_on_model_mode, prev_decision_at_utc, prev_gap_minutes, prev_model_mode"
        f" FROM {lane.EVALUATION_TABLE}"
    ).fetchone()
    assert row == (None, None, None, None)
    # An earlier P0 decision on the same station-day, 100 minutes before
    # (stale for its own evaluation), with 28°C quoted lower (0.38/0.40) and
    # the model then leaning 27°C: the P0-M first decision reads the move.
    store.execute(f"DELETE FROM {lane.EVALUATION_TABLE}")
    D2 = "2026-09-20"
    _seed(store, stage="P0", fact_id="f0", decided="2026-09-18T10:30:00.000Z", event_date=D2,
          probabilities={"27°C": 0.6, "28°C": 0.3, "29°C": 0.1}, book=_book(asks_28=((0.40, 100.0),), bid_28=0.38))
    _seed(store, stage="P0-M", fact_id="f2", decided="2026-09-18T12:05:00.000Z", event_date=D2)
    receipt = lane.execute_tiebreak_entries(store, transport=_fill_transport([]), now_utc=NOW,
                                            live_submit_authorized=True, settings={"stake_usdc": 20.0}, admission=ADMIT)
    rows = {r[0]: r for r in store.execute(
        f"SELECT stage, fired, refusal, market_move_on_model_mode, prev_decision_at_utc, prev_gap_minutes,"
        f" prev_model_mode, prev_market_share_on_model_mode FROM {lane.EVALUATION_TABLE} WHERE event_date=?", (D2,)
    )}
    assert rows["P0"][2] == "first_decision_stale"
    p0m = rows["P0-M"]
    assert p0m[1] == 1 and p0m[2] is None
    # 28°C mid now 0.45 of 1.02; then 0.39 of 0.96: the market moved toward the model's bucket.
    assert p0m[4] == "2026-09-18T10:30:00.000Z" and p0m[5] == pytest.approx(95.0)
    assert p0m[6] == "27°C" and p0m[7] == pytest.approx(0.39 / 0.96)
    assert p0m[3] == pytest.approx(0.45 / 1.02 - 0.39 / 0.96, abs=1e-6)
    # And the move changes nothing about firing: it is a feature.  (The first
    # station-day is re-scored too and refused as already attempted.)
    assert receipt["submitted"] == 1


def test_the_reference_point_is_always_the_previous_stage_never_the_same_one(store) -> None:
    """What the recorded move is measured against.

    The lane only ever scores a stage's FIRST decision, so the decision
    before it cannot belong to that stage -- it is the last decision of an
    earlier one.  Seed a P0 and then TWO P0-M decisions: the scored row is
    the earlier P0-M, and its reference is the P0, not its own stage's
    later decision, and not any same-stage row.  This is why the pooled
    momentum bin is confounded with the stage schedule (2026-09-20: all 207
    live rows carrying the feature cross a stage boundary, P0-M rows a
    median 47 minutes and P1 rows a median 739 minutes), and why the board
    also splits the feature with the transition held fixed.
    """

    day = "2026-09-20"
    _seed(store, stage="P0", fact_id="r0", decided="2026-09-18T09:00:00.000Z", event_date=day,
          probabilities={"27°C": 0.6, "28°C": 0.3, "29°C": 0.1},
          book=_book(asks_28=((0.40, 100.0),), bid_28=0.38))
    _seed(store, stage="P0-M", fact_id="r1", decided="2026-09-18T12:02:00.000Z", event_date=day)
    _seed(store, stage="P0-M", fact_id="r2", decided="2026-09-18T12:06:00.000Z", event_date=day)
    lane.execute_tiebreak_entries(store, transport=_fill_transport([]), now_utc=NOW,
                                  live_submit_authorized=True, settings={"stake_usdc": 20.0},
                                  admission=ADMIT)
    scored = {
        r[0]: r for r in store.execute(
            f"SELECT stage, fact_id, decision_at_utc, prev_decision_at_utc, prev_gap_minutes"
            f" FROM {lane.EVALUATION_TABLE} WHERE event_date=?", (day,),
        )
    }
    # One row per stage, and the P0-M row is the stage's FIRST decision.
    assert scored["P0-M"][1] == "r1" and scored["P0-M"][2] == "2026-09-18T12:02:00.000Z"
    # Its reference is the P0 -- not the later same-stage decision, and not itself.
    assert scored["P0-M"][3] == "2026-09-18T09:00:00.000Z"
    assert scored["P0-M"][4] == pytest.approx(182.0)
    same_stage = {r[0] for r in store.execute(
        f"SELECT f.stage FROM {lane.EVALUATION_TABLE} e"
        f" JOIN basket_policy_training_fact f"
        f"  ON f.station_id=e.station_id AND f.event_date=e.event_date"
        f"  AND f.decision_at_utc=e.prev_decision_at_utc"
        f" WHERE e.prev_decision_at_utc IS NOT NULL AND f.stage=e.stage",
    )}
    assert same_stage == set()


def test_the_lane_file_selects_v1_and_the_row_says_which_rule_scored_it(store) -> None:
    """A stage scored under v1 keeps v1's version on its row; v2 rows sit
    beside it (user ruling: v1's forward sample is not overwritten)."""

    _seed(store, probabilities={"27°C": 0.6, "28°C": 0.3, "29°C": 0.1})
    receipt = lane.execute_tiebreak_entries(store, transport=_fill_transport([]), now_utc=NOW,
                                            live_submit_authorized=True,
                                            settings={"stake_usdc": 20.0, "rule": "adjacent_tie_v1"}, admission=ADMIT)
    assert receipt["rule"] == "tiebreak_adjacent_mode_market_share_v1"
    assert receipt["refusals"] == ["rule_not_met:modes_agree"]
    assert store.execute(f"SELECT rule_version FROM {lane.EVALUATION_TABLE}").fetchone()[0] == "tiebreak_adjacent_mode_market_share_v1"
    # The same envelope under v2 (a later stage) fires and is recorded as v2.
    _seed(store, stage="P0-M", fact_id="f2", decided="2026-09-18T12:05:00.000Z", probabilities={"27°C": 0.6, "28°C": 0.3, "29°C": 0.1})
    sent: list = []
    second = lane.execute_tiebreak_entries(store, transport=_fill_transport(sent), now_utc=NOW,
                                           live_submit_authorized=True, settings={"stake_usdc": 20.0}, admission=ADMIT)
    assert second["fired"] == 1 and second["submitted"] == 1 and sent[0][1]["token_id"] == "t27"
    versions = sorted(r[0] for r in store.execute(f"SELECT rule_version FROM {lane.EVALUATION_TABLE}"))
    assert versions == ["tiebreak_adjacent_mode_market_share_v1", "tiebreak_model_mode_market_midband_spread_maker_v2_2"]
    plan_rule = store.execute("SELECT json_extract(intent_json, '$.rule') FROM basket_target_order_intents").fetchone()[0]
    assert plan_rule == "tiebreak_model_mode_market_midband_spread_maker_v2_2"


def test_the_gates_a_fired_decision_must_still_pass(store) -> None:
    _seed(store)
    # The station bar refuses the entry, and the row says so.
    barred = lane.execute_tiebreak_entries(
        store, transport=_fill_transport([]), now_utc=NOW, live_submit_authorized=True,
        settings={"stake_usdc": 20.0}, admission=lambda s, c: {"admitted": False, "reason": "live_station_barred"},
    )
    assert barred["fired"] == 1 and barred["planned"] == 0
    assert barred["refusals"] == ["station_not_admitted:live_station_barred"]


def test_an_unpriceable_rung_and_a_closed_market_are_refused(store) -> None:
    # No tick on the registry row: there is no grid to place a rung on.  (A
    # missing BID cannot reach here -- v2.1/v2.2 refuse an unquoted side at
    # the rule, before any order is priced.)
    _seed(store)
    store.execute(
        "UPDATE basket_target_market_registry SET registry_json=?",
        (json.dumps({"order_min_size": 5.0}),),
    )
    thin = lane.execute_tiebreak_entries(store, transport=_fill_transport([]), now_utc=NOW,
                                         live_submit_authorized=True, settings={"stake_usdc": 20.0}, admission=ADMIT)
    assert thin["refusals"] == ["maker_anchor_unavailable"]
    store.execute(f"DELETE FROM {lane.EVALUATION_TABLE}")
    store.execute("DELETE FROM basket_target_market_registry")
    _insert(store, "basket_target_market_registry_snapshot", {"snapshot_sha256": "1" * 64, "source_scope": "shared_live_registry_export_only"})
    for bucket, token in TOKENS.items():
        _insert(store, "basket_target_market_registry", {
            "snapshot_sha256": "1" * 64, "slug": f"{ST}-{bucket}-2", "event_date": ED,
            "outcome": bucket, "city_slug": "shanghai", "station_id": ST, "settlement_timezone": TZ,
            "yes_token": token, "market_active": 1, "market_accepting_orders": 0,
            "registry_json": json.dumps({"order_price_min_tick_size": 0.01, "order_min_size": 5.0}),
            "refreshed_at": DECIDED, "source_eligible": 1, "source_lane": "awc",
        })
    closed = lane.execute_tiebreak_entries(store, transport=_fill_transport([]), now_utc=NOW,
                                           live_submit_authorized=True, settings={"stake_usdc": 20.0}, admission=ADMIT)
    assert closed["refusals"] == ["market_not_accepting_orders"]


def test_charter_141_cash_is_read_when_the_account_table_exists(store, monkeypatch) -> None:
    # The lane re-reads the account once before refusing a stale row.  That
    # re-read is a live capture; here it must find nothing, or the refusal
    # this test pins depends on whatever an earlier test left reachable.
    monkeypatch.setattr(lane, "capture_account_equity", lambda *a, **k: None)
    _seed(store)
    ensure_account_equity_table(store)
    # A stale read is a refusal, never a guess.
    _insert(store, "basket_target_account_equity", {
        "equity_id": "e1", "observed_at_utc": "2026-09-18T09:00:00Z", "cash_usdc": 100.0,
        "positions_value_usdc": 0.0, "equity_usdc": 100.0, "resting_buy_usdc": 0.0, "created_at_utc": NOW,
        "content_sha256": "d" * 64,
    })
    stale = lane.execute_tiebreak_entries(store, transport=_fill_transport([]), now_utc=NOW,
                                          live_submit_authorized=True, settings={"stake_usdc": 20.0}, admission=ADMIT)
    assert stale["refusals"] == ["account_equity_stale"]
    store.execute(f"DELETE FROM {lane.EVALUATION_TABLE}")
    _insert(store, "basket_target_account_equity", {
        "equity_id": "e2", "observed_at_utc": "2026-09-18T12:09:00Z", "cash_usdc": 5.0,
        "positions_value_usdc": 0.0, "equity_usdc": 5.0, "resting_buy_usdc": 0.0, "created_at_utc": NOW,
        "content_sha256": "e" * 64,
    })
    poor = lane.execute_tiebreak_entries(store, transport=_fill_transport([]), now_utc=NOW,
                                         live_submit_authorized=True, settings={"stake_usdc": 20.0}, admission=ADMIT)
    assert poor["refusals"] and poor["refusals"][0].startswith("insufficient_cash:5.00<")


def _newer_snapshot(conn, *, observed_at: str, snapshot_id: str, book=None, event_date=ED) -> None:
    """A fresher committed book of the station-day, written by a later cycle
    without a new decision (the market ingress runs every cycle)."""

    _insert(conn, "basket_official_ws_snapshot", {
        "snapshot_id": snapshot_id, "market_id": "m", "station_id": ST, "event_date": event_date,
        "settlement_timezone": TZ, "sequence": 9, "observed_at": observed_at, "quote_version": "v",
        "quote_observed_at": observed_at, "quote_expires_at": "2026-09-18T14:00:00Z",
        "quote_source": "OFFICIAL_CLOB_WS", "remaining_budget_usdc": 100.0,
        "buckets_json": json.dumps(book if book is not None else _book()), "state": "COMMITTED",
        "created_at": observed_at,
    })


def test_a_rung_that_ended_without_filling_is_retried_on_a_newer_book_and_capped(store) -> None:
    """Charter 163 in place of the killed FOK (user ruling 2026-09-18 "加重试").

    A rung the venue is HOLDING ends the station-day while it stands -- it is
    the order this lane meant to place, not a refusal, and nothing doubles it.
    Once it has ended without filling (its own GTD horizon, or the resting
    lifecycle cancelling it off its anchor) the lane re-judges on a NEWER
    committed book, re-posts at the anchor that stands there, and does so at
    most ``max_sends_per_station_day`` times."""

    _seed(store)
    sent: list = []
    receipt = lane.execute_tiebreak_entries(store, transport=_resting_transport(sent), now_utc=NOW,
                                            live_submit_authorized=True, settings={"stake_usdc": 20.0}, admission=ADMIT)
    assert receipt["submitted"] == 1 and receipt["filled"] == 0
    first_id, state = store.execute(
        "SELECT order_intent_id, state FROM basket_target_order_intents").fetchone()
    assert state == "submitted", "a post-only rung the venue took is resting, not answered"
    assert store.execute(f"SELECT refusal, order_intent_id FROM {lane.EVALUATION_TABLE}").fetchone() == (
        None, first_id), "a resting rung is not a refusal"
    # Nothing is re-asked while it stands: the row is not a refusal at all.
    standing = lane.execute_tiebreak_entries(store, transport=_resting_transport(sent), now_utc=NOW,
                                             live_submit_authorized=True, settings={"stake_usdc": 20.0}, admission=ADMIT)
    assert standing["retried"] == 0 and len(sent) == 1
    # The venue's clock runs out.  Now the row is a ``send:`` refusal again,
    # and it waits for a book newer than the one the dead rung was priced on.
    _expire(store, first_id, at="2026-09-18T12:08:30.000Z")
    ended = lane.execute_tiebreak_entries(store, transport=_resting_transport(sent), now_utc=NOW,
                                          live_submit_authorized=True, settings={"stake_usdc": 20.0}, admission=ADMIT)
    assert ended["retry_planned"] == 0 and len(sent) == 1
    assert ended["retries"][0].get("waiting_for_newer_book") is True
    assert store.execute(f"SELECT refusal FROM {lane.EVALUATION_TABLE}").fetchone()[0] == "send:resolved_cancelled"
    # A newer book, still inside the rule: re-posted at ITS anchor (0.45/0.47
    # quotes a rung at 0.46), not at the price the dead rung stood on.
    _newer_snapshot(store, observed_at="2026-09-18T12:09:00.000Z", snapshot_id="snap_new1",
                    book=_book(asks_28=((0.47, 100.0),), bid_28=0.45))
    second = lane.execute_tiebreak_entries(store, transport=_resting_transport(sent), now_utc=NOW,
                                           live_submit_authorized=True, settings={"stake_usdc": 20.0}, admission=ADMIT)
    assert second["retry_planned"] == 1 and len(sent) == 2 and sent[1][1]["price"] == 0.46
    # Waiting for a newer book is not a retry; the count moves only when one is made.
    assert store.execute(f"SELECT retry_snapshot_id, retry_count FROM {lane.EVALUATION_TABLE}").fetchone() == ("snap_new1", 1)
    second_id = store.execute(
        "SELECT order_intent_id FROM basket_target_order_intents ORDER BY created_at DESC LIMIT 1").fetchone()[0]
    _expire(store, second_id, at="2026-09-18T12:09:20.000Z")
    # A newer book that has left the band is not bought.
    _newer_snapshot(store, observed_at="2026-09-18T12:09:30.000Z", snapshot_id="snap_new2",
                    book=[{"bucket": "27°C", "ask_levels": [{"price": 0.20, "shares": 200.0}], "bid_levels": [{"price": 0.18, "shares": 200.0}], "ask_available": True},
                          {"bucket": "28°C", "ask_levels": [{"price": 0.75, "shares": 200.0}], "bid_levels": [{"price": 0.73, "shares": 200.0}], "ask_available": True},
                          {"bucket": "29°C", "ask_levels": [{"price": 0.05, "shares": 200.0}], "bid_levels": [{"price": 0.04, "shares": 200.0}], "ask_available": True}])
    third = lane.execute_tiebreak_entries(store, transport=_resting_transport(sent), now_utc=NOW,
                                          live_submit_authorized=True, settings={"stake_usdc": 20.0}, admission=ADMIT)
    assert third["retry_planned"] == 0 and len(sent) == 2
    assert third["retries"][0]["refusal_after"] == "retry_rule_not_met:market_share_above_ceiling"
    # Back inside the band on a newer book: the third and last send.
    _newer_snapshot(store, observed_at="2026-09-18T12:10:00.000Z", snapshot_id="snap_new3")
    fourth = lane.execute_tiebreak_entries(store, transport=_resting_transport(sent), now_utc=NOW,
                                           live_submit_authorized=True, settings={"stake_usdc": 20.0}, admission=ADMIT)
    assert fourth["retry_planned"] == 1 and len(sent) == 3
    third_id = store.execute(
        "SELECT order_intent_id FROM basket_target_order_intents ORDER BY created_at DESC LIMIT 1").fetchone()[0]
    _expire(store, third_id, at="2026-09-18T12:10:20.000Z")
    _newer_snapshot(store, observed_at="2026-09-18T12:11:00.000Z", snapshot_id="snap_new4")
    fifth = lane.execute_tiebreak_entries(store, transport=_resting_transport(sent), now_utc=NOW,
                                          live_submit_authorized=True, settings={"stake_usdc": 20.0}, admission=ADMIT)
    assert fifth["retry_planned"] == 0 and len(sent) == 3
    assert fifth["retries"][0]["refusal_after"] == "station_day_send_cap:3"


def test_a_standing_rung_holds_the_station_day_even_on_a_newer_book(store) -> None:
    """Charter 163: while the venue holds the rung there is a live order on
    that token, so the station-day is spent -- a later stage's first decision
    fires and is refused, and a newer book buys nothing."""

    _seed(store)
    sent: list = []
    lane.execute_tiebreak_entries(store, transport=_resting_transport(sent), now_utc=NOW,
                                  live_submit_authorized=True, settings={"stake_usdc": 20.0}, admission=ADMIT)
    assert len(sent) == 1
    _newer_snapshot(store, observed_at="2026-09-18T12:09:00.000Z", snapshot_id="snap_standing")
    _seed(store, stage="P0-M", fact_id="f9", decided="2026-09-18T12:09:00.000Z")
    later = lane.execute_tiebreak_entries(store, transport=_resting_transport(sent), now_utc=NOW,
                                          live_submit_authorized=True, settings={"stake_usdc": 20.0}, admission=ADMIT)
    assert later["fired"] == 1 and later["planned"] == 0 and len(sent) == 1
    assert later["refusals"] == ["station_day_already_attempted"]


def test_a_fill_ends_the_station_day_for_good(store) -> None:
    _seed(store)
    sent: list = []
    lane.execute_tiebreak_entries(store, transport=_fill_transport(sent), now_utc=NOW, live_submit_authorized=True,
                                  settings={"stake_usdc": 20.0}, admission=ADMIT)
    assert len(sent) == 1
    _newer_snapshot(store, observed_at="2026-09-18T12:09:00.000Z", snapshot_id="snap_after_fill")
    again = lane.execute_tiebreak_entries(store, transport=_fill_transport(sent), now_utc=NOW, live_submit_authorized=True,
                                          settings={"stake_usdc": 20.0}, admission=ADMIT)
    assert again["retried"] == 0 and len(sent) == 1


def test_a_lowered_arming_flag_holds_the_send(store, tmp_path, monkeypatch) -> None:
    _seed(store)
    lowered = tmp_path / "lowered.json"
    lowered.write_text(json.dumps({"live_enabled": False}), encoding="utf-8")
    monkeypatch.setattr(sub, "LIVE_INTENT_PATH", lowered)
    sent: list = []
    receipt = lane.execute_tiebreak_entries(store, transport=_fill_transport(sent), now_utc=NOW,
                                            live_submit_authorized=True, settings={"stake_usdc": 20.0}, admission=ADMIT)
    assert sent == [] and receipt["submitted"] == 0
    assert receipt["receipts"][0]["reason"] == "live_intent_disarmed"
    assert store.execute("SELECT state FROM basket_target_order_intents").fetchone()[0] == "failed"


def test_a_cash_refusal_is_retried_on_the_latest_decision_once_cash_is_there(store) -> None:
    """User ruling 2026-09-18 "加重试": RJTT 2026-09-18 P1 fired at 02:11Z and
    was refused on a 1.70 USDC balance; the deposit landed minutes later.
    A fired row refused for cash (or a stale read) is re-asked on the
    station-day-stage's latest decision and bought if the rule still holds
    there; rule refusals are never retried."""

    from bayes_basket_target.account_equity import ensure_account_equity_table  # noqa: PLC0415

    _seed(store)
    ensure_account_equity_table(store)
    _insert(store, "basket_target_account_equity", {
        "equity_id": "e_poor", "observed_at_utc": "2026-09-18T12:09:00Z", "cash_usdc": 1.7,
        "positions_value_usdc": 70.0, "equity_usdc": 71.7, "resting_buy_usdc": 0.0, "created_at_utc": NOW,
        "content_sha256": "f" * 64,
    })
    poor = lane.execute_tiebreak_entries(store, transport=_fill_transport([]), now_utc=NOW,
                                         live_submit_authorized=True, settings={"stake_usdc": 6.0}, admission=ADMIT)
    assert poor["fired"] == 1 and poor["submitted"] == 0 and poor["refusals"][0].startswith("insufficient_cash:1.70")
    assert poor["retried"] == 0
    # Same cycle again, still poor: the row is retried and refused again, counted.
    again = lane.execute_tiebreak_entries(store, transport=_fill_transport([]), now_utc=NOW,
                                          live_submit_authorized=True, settings={"stake_usdc": 6.0}, admission=ADMIT)
    assert again["retried"] == 1 and again["retry_planned"] == 0
    assert again["retries"][0]["refusal_after"].startswith("insufficient_cash:")
    row = store.execute(f"SELECT refusal, retry_of_refusal, retry_count, order_intent_id FROM {lane.EVALUATION_TABLE}").fetchone()
    assert row[0].startswith("insufficient_cash:") and row[1].startswith("insufficient_cash:1.70") and row[2] == 1 and row[3] is None
    # The deposit lands, and a later decision of the same stage is written.
    _insert(store, "basket_target_account_equity", {
        "equity_id": "e_rich", "observed_at_utc": "2026-09-18T12:12:00Z", "cash_usdc": 100.13,
        "positions_value_usdc": 120.0, "equity_usdc": 220.13, "resting_buy_usdc": 0.0, "created_at_utc": NOW,
        "content_sha256": "0" * 64,
    })
    _seed(store, stage="P1", fact_id="f9", decided="2026-09-18T12:08:00.000Z")
    sent: list = []
    later = "2026-09-18T12:14:00.000Z"
    retried = lane.execute_tiebreak_entries(store, transport=_fill_transport(sent), now_utc=later,
                                            live_submit_authorized=True, settings={"stake_usdc": 6.0}, admission=ADMIT)
    assert retried["evaluated"] == 0, "the first decision is not re-scored"
    assert retried["retried"] == 1 and retried["retry_planned"] == 1 and retried["submitted"] == 1
    (path, payload), = sent
    assert payload["token_id"] == "t28" and payload["order_type"] == "GTD" and payload["post_only"] is True
    assert payload["size"] * payload["price"] <= 6.0 + 1e-6
    row = store.execute(
        f"SELECT refusal, retry_of_refusal, retry_count, order_intent_id, retry_fact_id, fact_id FROM {lane.EVALUATION_TABLE}"
    ).fetchone()
    assert row[0] is None and row[1].startswith("insufficient_cash:1.70") and row[2] == 2
    assert row[3] == payload["order_intent_id"] and row[4] == "f9" and row[5] == "f1", "the row keeps its first decision and names the retry's"
    plan = json.loads(store.execute("SELECT plan_json FROM basket_target_order_intents").fetchone()[0] if False else store.execute(f"SELECT plan_json FROM {lane.EVALUATION_TABLE}").fetchone()[0])
    assert plan["retry_of_refusal"].startswith("insufficient_cash:") and plan["fact_id"] == "f9"
    # Once bought, nothing retries it again.
    third = lane.execute_tiebreak_entries(store, transport=_fill_transport(sent), now_utc=later,
                                          live_submit_authorized=True, settings={"stake_usdc": 6.0}, admission=ADMIT)
    assert third["retried"] == 0 and len(sent) == 1


def test_rule_refusals_and_stale_latest_decisions_are_not_retried(store) -> None:
    from bayes_basket_target.account_equity import ensure_account_equity_table  # noqa: PLC0415

    # A rule refusal (market share above the ceiling) is never retried.
    _seed(store, book=[{"bucket": "27°C", "ask_levels": [{"price": 0.20, "shares": 200.0}], "bid_levels": [{"price": 0.18, "shares": 200.0}], "ask_available": True},
                       {"bucket": "28°C", "ask_levels": [{"price": 0.75, "shares": 200.0}], "bid_levels": [{"price": 0.73, "shares": 200.0}], "ask_available": True},
                       {"bucket": "29°C", "ask_levels": [{"price": 0.05, "shares": 200.0}], "bid_levels": [{"price": 0.04, "shares": 200.0}], "ask_available": True}])
    first = lane.execute_tiebreak_entries(store, transport=_fill_transport([]), now_utc=NOW,
                                          live_submit_authorized=True, settings={"stake_usdc": 6.0}, admission=ADMIT)
    assert first["fired"] == 0 and first["refusals"] == ["rule_not_met:market_share_above_ceiling"]
    again = lane.execute_tiebreak_entries(store, transport=_fill_transport([]), now_utc=NOW,
                                          live_submit_authorized=True, settings={"stake_usdc": 6.0}, admission=ADMIT)
    assert again["retried"] == 0
    # A cash refusal whose latest decision is stale waits, unchanged.
    D2 = "2026-09-20"
    ensure_account_equity_table(store)
    _insert(store, "basket_target_account_equity", {
        "equity_id": "e_poor2", "observed_at_utc": "2026-09-18T12:09:00Z", "cash_usdc": 1.0,
        "positions_value_usdc": 70.0, "equity_usdc": 71.0, "resting_buy_usdc": 0.0, "created_at_utc": NOW,
        "content_sha256": "e" * 64,
    })
    _seed(store, fact_id="f2", event_date=D2)
    poor = lane.execute_tiebreak_entries(store, transport=_fill_transport([]), now_utc=NOW,
                                         live_submit_authorized=True, settings={"stake_usdc": 6.0}, admission=ADMIT)
    assert poor["fired"] == 1 and poor["submitted"] == 0
    much_later = "2026-09-18T14:00:00.000Z"
    waiting = lane.execute_tiebreak_entries(store, transport=_fill_transport([]), now_utc=much_later,
                                            live_submit_authorized=True, settings={"stake_usdc": 6.0}, admission=ADMIT)
    assert waiting["retried"] == 1 and waiting["retry_planned"] == 0
    assert waiting["retries"][0].get("waiting_for_fresh_decision") is True
    row = store.execute(f"SELECT refusal, retry_count, order_intent_id FROM {lane.EVALUATION_TABLE} WHERE event_date=?", (D2,)).fetchone()
    assert row[0].startswith("insufficient_cash:") and (row[1] or 0) == 0 and row[2] is None
    # The switch in the lane file turns retries off.
    off = lane.execute_tiebreak_entries(store, transport=_fill_transport([]), now_utc=NOW,
                                        live_submit_authorized=True,
                                        settings={"stake_usdc": 6.0, "retry_operational_refusals": False}, admission=ADMIT)
    assert off["retried"] == 0


def test_an_ambiguous_send_waits_for_the_reconciliation_and_follows_it(store) -> None:
    """OPKC 2026-09-18 06:04Z: the gateway's proxy timed out, the order may or
    may not have reached the exchange.  The row says ``send:UNKNOWN_POST``;
    while the reconciliation is unknown the retry pass waits and writes
    nothing; a fill the reconciliation lane finds makes the row a success,
    and "never placed" makes it retryable on a newer book."""

    from bayes_basket_target.execution_reconciliation import record_order_event  # noqa: PLC0415

    _seed(store)
    sent: list = []

    def ambiguous(path, payload):
        sent.append(payload)
        return {"ok": False, "reconcile_required": True, "error": "clob_proxy_submit_failed:curl: (28) timeout"}

    first = lane.execute_tiebreak_entries(store, transport=ambiguous, now_utc=NOW, live_submit_authorized=True,
                                          settings={"stake_usdc": 20.0}, admission=ADMIT)
    assert first["submitted"] == 1 and first["filled"] == 0
    intent_id = sent[0]["order_intent_id"]
    row = store.execute(f"SELECT refusal, order_intent_id FROM {lane.EVALUATION_TABLE}").fetchone()
    assert row[0].startswith("send:UNKNOWN_POST") and row[1] == intent_id
    # Unknown at the exchange: a newer book changes nothing, the row is untouched.
    _newer_snapshot(store, observed_at="2026-09-18T12:09:00.000Z", snapshot_id="snap_amb1")
    waiting = lane.execute_tiebreak_entries(store, transport=ambiguous, now_utc=NOW, live_submit_authorized=True,
                                            settings={"stake_usdc": 20.0}, admission=ADMIT)
    assert waiting["retry_planned"] == 0 and len(sent) == 1
    assert waiting["retries"][0].get("waiting_for_open_order") is True
    assert store.execute(f"SELECT refusal, COALESCE(retry_count,0) FROM {lane.EVALUATION_TABLE}").fetchone() == (row[0], 0)
    # The reconciliation lane finds it never reached the exchange: retryable again, and retried.
    record_order_event(store, order_intent_id=intent_id, event={
        "event_type": "SUBMIT_REFUSED", "exchange_order_id": "", "observed_at": "2026-09-18T12:09:30.000Z",
        "matched_size": 0.0, "matched_notional_usdc": 0.0, "event_payload": {"reason": "lookup_not_found"},
    })
    retried = lane.execute_tiebreak_entries(store, transport=_fill_transport(sent), now_utc=NOW, live_submit_authorized=True,
                                            settings={"stake_usdc": 20.0}, admission=ADMIT)
    assert retried["retry_planned"] == 1 and len(sent) == 2
    assert store.execute(f"SELECT refusal FROM {lane.EVALUATION_TABLE}").fetchone()[0] is None


def test_a_row_re_judged_after_its_send_failed_keeps_the_later_judgement(store) -> None:
    """RJTT/OPKC 2026-09-18 07:55Z: a row whose dead send had been re-judged
    ``retry_rule_not_met`` on a newer book read ``send:resolved_not_placed``
    again one cycle later -- the sent-row sync rewrote it from the dead order
    on every pass, and the board named the order as the reason when the
    market was.  The later judgement stands until a newer book is judged."""

    _seed(store)
    sent: list = []
    lane.execute_tiebreak_entries(store, transport=_resting_transport(sent), now_utc=NOW,
                                  live_submit_authorized=True, settings={"stake_usdc": 20.0}, admission=ADMIT)
    intent_id = store.execute("SELECT order_intent_id FROM basket_target_order_intents").fetchone()[0]
    _expire(store, intent_id, at="2026-09-18T12:09:00.000Z")
    _newer_snapshot(store, observed_at="2026-09-18T12:09:30.000Z", snapshot_id="snap_out",
                    book=[{"bucket": "27°C", "ask_levels": [{"price": 0.20, "shares": 200.0}], "bid_levels": [{"price": 0.18, "shares": 200.0}], "ask_available": True},
                          {"bucket": "28°C", "ask_levels": [{"price": 0.75, "shares": 200.0}], "bid_levels": [{"price": 0.73, "shares": 200.0}], "ask_available": True},
                          {"bucket": "29°C", "ask_levels": [{"price": 0.05, "shares": 200.0}], "bid_levels": [{"price": 0.04, "shares": 200.0}], "ask_available": True}])
    judged = lane.execute_tiebreak_entries(store, transport=_resting_transport(sent), now_utc=NOW,
                                           live_submit_authorized=True, settings={"stake_usdc": 20.0}, admission=ADMIT)
    assert judged["retries"][0]["refusal_after"] == "retry_rule_not_met:market_share_above_ceiling"
    assert store.execute(f"SELECT refusal FROM {lane.EVALUATION_TABLE}").fetchone()[0] == "retry_rule_not_met:market_share_above_ceiling"
    # No newer book: the next pass judges nothing, and the row keeps its reason.
    again = lane.execute_tiebreak_entries(store, transport=_resting_transport(sent), now_utc=NOW,
                                          live_submit_authorized=True, settings={"stake_usdc": 20.0}, admission=ADMIT)
    assert again["retry_planned"] == 0 and len(sent) == 1
    assert store.execute(f"SELECT refusal, order_intent_id FROM {lane.EVALUATION_TABLE}").fetchone() == (
        "retry_rule_not_met:market_share_above_ceiling", sent[0][1]["order_intent_id"],
    )


def test_a_fill_the_reconciliation_finds_later_makes_the_row_a_success(store) -> None:
    from bayes_basket_target.execution_reconciliation import record_order_event  # noqa: PLC0415

    _seed(store)
    sent: list = []
    lane.execute_tiebreak_entries(store, transport=lambda p, payload: (sent.append(payload) or {"ok": False, "reconcile_required": True, "error": "timeout"}),
                                  now_utc=NOW, live_submit_authorized=True, settings={"stake_usdc": 20.0}, admission=ADMIT)
    intent_id = sent[0]["order_intent_id"]
    record_order_event(store, order_intent_id=intent_id, event={
        "event_type": "FILL", "exchange_order_id": "0xlate", "observed_at": "2026-09-18T12:09:30.000Z",
        "matched_size": float(sent[0]["size"]), "matched_notional_usdc": float(sent[0]["size"]) * float(sent[0]["price"]),
        "event_payload": {},
    })
    _newer_snapshot(store, observed_at="2026-09-18T12:09:40.000Z", snapshot_id="snap_late")
    after = lane.execute_tiebreak_entries(store, transport=_fill_transport(sent), now_utc=NOW, live_submit_authorized=True,
                                          settings={"stake_usdc": 20.0}, admission=ADMIT)
    assert after["retry_planned"] == 0 and len(sent) == 1
    assert store.execute(f"SELECT refusal FROM {lane.EVALUATION_TABLE}").fetchone()[0] is None


def test_a_send_leaves_its_cycle_settled_so_the_days_quotes_keep_refreshing(store) -> None:
    """The market ingress refuses to refresh a station-day while an execution
    cycle sits at submitting/submitted (2026-09-18: RJTT, WMKK, OPKC, EDDM
    and nine charter-144 station-days froze this way).  A lane cycle is one
    answered order: settled."""

    _seed(store)
    lane.execute_tiebreak_entries(store, transport=_fill_transport([]), now_utc=NOW, live_submit_authorized=True,
                                  settings={"stake_usdc": 20.0}, admission=ADMIT)
    assert store.execute("SELECT state FROM basket_target_execution_cycles WHERE cycle_id LIKE 'tiebreak_cycle_%'").fetchall() == [("settled",)]
    # The decision row is append-only and quiescent from birth: ``prepared``.
    assert store.execute("SELECT decided_state FROM auto_trade_decisions WHERE decision_id LIKE 'tiebreak_decision_%'").fetchall() == [("prepared",)]
    # A refused send too.
    _seed(store, fact_id="f2", event_date="2026-09-20")
    lane.execute_tiebreak_entries(store, transport=lambda p, payload: {"ok": False, "error": "refused"}, now_utc=NOW,
                                  live_submit_authorized=True, settings={"stake_usdc": 20.0}, admission=ADMIT)
    assert set(r[0] for r in store.execute("SELECT state FROM basket_target_execution_cycles WHERE cycle_id LIKE 'tiebreak_cycle_%'")) == {"settled"}


def test_a_fok_matched_under_the_size_sent_is_closed_as_filled(store) -> None:
    """EDDM 2026-09-19: sent 9.67 at 0.62, the venue matched 9.5.  A FOK the
    venue matched is the whole order; the request is restated as the venue's
    size and the ledger closes it filled, never ``reconcile_required``.

    The tiebreak lane stopped crossing at charter 163 and this branch has no
    caller in production today; it is kept on the shared sender, and pinned
    here, because it is a ledger-correctness rule about the FOK family rather
    than about one lane -- the next lane to ask for a take must not have to
    rediscover it.  Driven through ``send_lane_buy`` directly for that
    reason."""

    from bayes_basket_target.lane_orders import send_lane_buy  # noqa: PLC0415

    _seed(store)
    sent: list = []

    def short_fill(path, payload):
        sent.append(payload)
        matched = round(payload["size"] * 0.982, 2)
        return {"ok": True, "accepted": True, "exchange_order_id": "0xshort", "status": "matched",
                "shares_matched": matched, "exchange": {"size_matched": matched, "makingAmount": matched * payload["price"]}}

    entry = send_lane_buy(
        store, lane="tiebreak", intent_id="fok_short_i", station_id=ST, event_date=ED,
        settlement_timezone=TZ, bucket="28°C", token_id="t28", price=0.62, shares=9.67,
        execution_candidate="TAKER_FOK", fok_price_cap=0.62,
        intent_body={"schema": "x", "rule": "r", "order_type": "FOK", "post_only": False},
        transport=short_fill, live_submit_authorized=True,
        decision_details={"schema": "x"},
    )
    assert entry["matched_size"] == pytest.approx(round(9.67 * 0.982, 2))
    row = store.execute(
        "SELECT i.state, i.requested_size, r.state, r.filled_size FROM basket_target_order_intents i"
        " JOIN basket_target_order_reconciliations r ON r.order_intent_id=i.order_intent_id"
    ).fetchone()
    assert row[0] == "filled" and row[2] == "resolved_filled" and row[1] == pytest.approx(row[3])
    assert row[3] == pytest.approx(round(sent[0]["size"] * 0.982, 2))
    ev = store.execute("SELECT event_json FROM basket_target_order_events").fetchone()[0]
    assert "fok_short_fill" in ev and "requested_size_at_send" in ev


def test_stuck_lane_cycles_and_partial_foks_left_by_earlier_code_are_repaired(store) -> None:
    from bayes_basket_target.execution_reconciliation import record_order_event  # noqa: PLC0415

    # A charter-144 exit cycle left at submitted with its one intent terminal.
    _insert(store, "basket_target_execution_cycles", {"cycle_id": "misbuyexit_cycle_x", "plan_id": "mbx_p", "station_id": ST, "event_date": ED, "settlement_timezone": TZ, "strategy_lane": "basket", "requested_reserve_usdc": 0.0, "state": "submitted", "created_at": DECIDED, "updated_at": DECIDED})
    _insert(store, "basket_target_order_intents", {"order_intent_id": "mbx_i", "decision_id": "d_x", "cycle_id": "misbuyexit_cycle_x", "bucket": "27°C", "token_id": "t27", "side": "YES", "order_action": "SELL", "limit_price": 0.3, "requested_size": 5.0, "requested_notional_usdc": 1.5, "state": "filled", "live_submit_authorized": 1, "idempotency_key": "mbx_i", "intent_json": "{}", "created_at": DECIDED})
    # A tiebreak FOK recorded as a partial fill by the earlier sender.
    _insert(store, "basket_target_execution_cycles", {"cycle_id": "tiebreak_cycle_y", "plan_id": "tb_p", "station_id": ST, "event_date": ED, "settlement_timezone": TZ, "strategy_lane": "basket", "requested_reserve_usdc": 0.0, "state": "submitted", "created_at": DECIDED, "updated_at": DECIDED})
    _insert(store, "basket_target_order_intents", {"order_intent_id": "tb_i", "decision_id": f"tiebreak_decision_{ST}_{ED}", "cycle_id": "tiebreak_cycle_y", "bucket": "28°C", "token_id": "t28", "side": "YES", "order_action": "BUY", "limit_price": 0.62, "requested_size": 9.67, "requested_notional_usdc": 5.9954, "state": "submitted", "live_submit_authorized": 1, "idempotency_key": "tb_i", "intent_json": json.dumps({"order_type": "FOK", "execution_candidate": "TAKER_FOK"}), "created_at": DECIDED})
    record_order_event(store, order_intent_id="tb_i", event={"event_type": "FILL", "exchange_order_id": "0xe", "observed_at": DECIDED, "matched_size": 9.5, "matched_notional_usdc": 5.89, "event_payload": {}})
    assert store.execute("SELECT state FROM basket_target_order_intents WHERE order_intent_id='tb_i'").fetchone()[0] == "reconcile_required"
    # A still-open basket cycle is not touched.
    _insert(store, "basket_target_execution_cycles", {"cycle_id": "basket_cycle_z", "plan_id": "bk_p", "station_id": ST, "event_date": ED, "settlement_timezone": TZ, "strategy_lane": "basket", "requested_reserve_usdc": 1.0, "state": "submitted", "created_at": DECIDED, "updated_at": DECIDED})
    assert lane.settle_stuck_lane_cycles(store) == 1
    assert lane.repair_fok_short_fills(store) == 1
    assert lane.settle_stuck_lane_cycles(store) == 1, "the tiebreak cycle settles once its intent is filled"
    states = dict(store.execute("SELECT cycle_id, state FROM basket_target_execution_cycles").fetchall())
    assert states["misbuyexit_cycle_x"] == "settled" and states["tiebreak_cycle_y"] == "settled" and states["basket_cycle_z"] == "submitted"
    row = store.execute("SELECT i.state, i.requested_size, r.state, r.filled_size FROM basket_target_order_intents i JOIN basket_target_order_reconciliations r ON r.order_intent_id=i.order_intent_id WHERE i.order_intent_id='tb_i'").fetchone()
    assert row == ("filled", 9.5, "resolved_filled", 9.5)
    # And the pass itself reports what it repaired.
    _seed(store, fact_id="f3", event_date="2026-09-20")
    receipt = lane.execute_tiebreak_entries(store, transport=_fill_transport([]), now_utc=NOW, live_submit_authorized=True,
                                            settings={"stake_usdc": 20.0}, admission=ADMIT)
    assert receipt["repairs"] == {"cycles_settled": 0, "fok_short_fills_closed": 0}
