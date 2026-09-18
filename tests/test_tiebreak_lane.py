"""「秤砣线」-- the tiebreak lane (charter 146, user ruling 2026-09-18).

At a stage's first decision, when the market's normalised mid puts at least
0.40 on the model's mode bucket and that bucket is one rung from the
market's own mode, the lane buys the model's bucket: 20 USDC, FOK at the
walked ask, once per station-day, through the shared buy sender, with a
decision row the live book can count it under.  Every first decision is
scored into ``tiebreak_lane_evaluation`` whether or not it fires.
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
    assert default["rule"] == "tiebreak_model_mode_market_midband_spread_v2_1" and default["fired"] is True, "v2.1 is the default"
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


def test_the_walk_spends_at_most_the_stake_and_prices_the_deepest_level() -> None:
    walk = lane.walk_ask_book([{"price": 0.47, "shares": 100.0}, {"price": 0.46, "shares": 30.0}], 20.0)
    assert walk["best_ask"] == 0.46 and walk["limit_price"] == 0.47 and walk["levels_touched"] == 2
    # Capped at what the stake buys at the limit: 20 / 0.47, floored to a cent of a share.
    assert walk["walked_cost_usdc"] <= 20.0 and walk["shares"] == pytest.approx(42.55)
    assert walk["shares"] * walk["limit_price"] <= 20.0
    # One level deep enough: limit is the best ask.
    one = lane.walk_ask_book([{"price": 0.46, "shares": 100.0}], 20.0)
    assert one["limit_price"] == 0.46 and one["shares"] == pytest.approx(43.47)
    # Too thin for the venue minimum: no plan.
    assert lane.walk_ask_book([{"price": 0.46, "shares": 3.0}], 20.0) is None
    assert lane.walk_ask_book([], 20.0) is None


# ---------------------------------------------------------------------------
# End to end.


def test_it_buys_the_model_bucket_fok_at_the_walked_ask_and_records_everything(store) -> None:
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
    assert payload["order_type"] == "FOK" and payload["post_only"] is False
    assert payload["price"] == 0.47 and payload["fok_price_cap"] == 0.47
    assert payload["size"] * payload["price"] <= 20.0 + 1e-6 and payload["size"] >= 40.0
    intent = store.execute(
        "SELECT decision_id, bucket, order_action, limit_price, requested_size, state, live_submit_authorized,"
        " order_intent_id FROM basket_target_order_intents"
    ).fetchone()
    assert intent[0] == f"tiebreak_decision_{ST}_{ED}" and intent[1] == "28°C" and intent[2] == "BUY"
    assert intent[3] == 0.47 and intent[5] == "filled" and intent[6] == 1
    decision = store.execute(
        "SELECT strategy_lane, decided_state, will_order, details_json FROM auto_trade_decisions WHERE decision_id=?",
        (intent[0],),
    ).fetchone()
    assert decision[:3] == ("basket", "submitted", 1)
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
    assert book[4] == "tiebreak_model_mode_market_midband_spread_v2_1"
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
    assert leg["filled_size"] > 40.0 and leg["claimed_probability"] == pytest.approx(0.45)


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
    assert receipt["rule"] == "tiebreak_model_mode_market_midband_spread_v2_1"
    row = store.execute(f"SELECT model_mode, market_mode, fired, rule_version FROM {lane.EVALUATION_TABLE}").fetchone()
    assert row == ("29°C", "27°C", 0, "tiebreak_model_mode_market_midband_spread_v2_1")


def test_v2_1_refuses_a_wide_or_one_sided_book_on_the_model_bucket(store) -> None:
    """v2.1 (user ruling 2026-09-18): the model's bucket must be quoted both
    sides at most three cents wide; the reason is on the row, and v2 is
    still one setting away."""

    _seed(store, book=_book(bid_28=0.40))          # 0.40 / 0.46: six cents
    wide = lane.execute_tiebreak_entries(store, transport=_fill_transport([]), now_utc=NOW,
                                         live_submit_authorized=True, settings={"stake_usdc": 20.0}, admission=ADMIT)
    assert wide["fired"] == 0 and wide["refusals"] == ["rule_not_met:spread_too_wide"]
    assert wide["rule"] == "tiebreak_model_mode_market_midband_spread_v2_1"
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
    assert versions == ["tiebreak_adjacent_mode_market_share_v1", "tiebreak_model_mode_market_midband_spread_v2_1"]
    plan_rule = store.execute("SELECT json_extract(intent_json, '$.rule') FROM basket_target_order_intents").fetchone()[0]
    assert plan_rule == "tiebreak_model_mode_market_midband_spread_v2_1"


def test_the_gates_a_fired_decision_must_still_pass(store) -> None:
    _seed(store)
    # The station bar refuses the entry, and the row says so.
    barred = lane.execute_tiebreak_entries(
        store, transport=_fill_transport([]), now_utc=NOW, live_submit_authorized=True,
        settings={"stake_usdc": 20.0}, admission=lambda s, c: {"admitted": False, "reason": "live_station_barred"},
    )
    assert barred["fired"] == 1 and barred["planned"] == 0
    assert barred["refusals"] == ["station_not_admitted:live_station_barred"]


def test_a_thin_ask_book_and_a_closed_market_are_refused(store) -> None:
    _seed(store, book=_book(asks_28=((0.46, 3.0),)))
    thin = lane.execute_tiebreak_entries(store, transport=_fill_transport([]), now_utc=NOW,
                                         live_submit_authorized=True, settings={"stake_usdc": 20.0}, admission=ADMIT)
    assert thin["refusals"] == ["ask_book_too_thin"]
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


def test_charter_141_cash_is_read_when_the_account_table_exists(store) -> None:
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


def test_a_killed_fok_is_recorded_and_not_retried(store) -> None:
    _seed(store)
    sent: list = []

    def killed(path, payload):
        sent.append(payload)
        return {"ok": True, "accepted": True, "exchange_order_id": "0xkill", "status": "live"}

    receipt = lane.execute_tiebreak_entries(store, transport=killed, now_utc=NOW, live_submit_authorized=True,
                                            settings={"stake_usdc": 20.0}, admission=ADMIT)
    assert receipt["submitted"] == 1 and receipt["filled"] == 0
    state = store.execute("SELECT state FROM basket_target_order_intents").fetchone()[0]
    assert state in {"cancelled", "failed"}
    again = lane.execute_tiebreak_entries(store, transport=killed, now_utc=NOW, live_submit_authorized=True,
                                          settings={"stake_usdc": 20.0}, admission=ADMIT)
    assert again["planned"] == 0 and len(sent) == 1


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
