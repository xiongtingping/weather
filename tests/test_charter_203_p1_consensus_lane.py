"""同心线 (the P1 consensus lane) -- the four centres agree, in the afternoon, on a bucket still cheap.

Charter 203 (user ruling 2026-09-23 「上线，一个站日可以买多次！」, rule A, no
yielding to the tiebreak lane, 5 USDC a take).  On a P1 decision 6-10 h from
the settlement clock where the model's and the market's mode and mean buckets
are one bucket and its best ask is in [0.50, 0.70), the lane crosses FOK at
the ask -- on every such decision, up to four per station-day.
"""

from __future__ import annotations

import ast
import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target import order_submission as sub  # noqa: E402
from bayes_basket_target import p1_consensus_lane as lane  # noqa: E402
from bayes_basket_target.lane_orders import lane_decision_id  # noqa: E402
from bayes_basket_target.market_registry import (  # noqa: E402
    apply_target_market_registry_schema,
)
from bayes_basket_target.ssot import apply_production_ssot_schema  # noqa: E402
from bayes_basket_target.strategy_lanes import P1_CONSENSUS_DEFAULT_SETTINGS  # noqa: E402

#: Shanghai is UTC+8, so the settlement clock (local midnight ending
#: 2026-09-19) is 2026-09-19T16:00Z; a decision at 08:00Z sits 8 h from it.
ST, ED, TZ = "ZSPD", "2026-09-19", "Asia/Shanghai"
NOW = "2026-09-19T08:02:00.000Z"
DECIDED = "2026-09-19T08:00:00.000Z"
OBSERVED = "2026-09-19T08:01:40.000Z"
BUCKETS = ("26°C", "27°C", "28°C", "29°C")
ORDINALS = {b: 26 + i for i, b in enumerate(BUCKETS)}
TOKENS = {b: f"t{i}" for i, b in enumerate(BUCKETS)}
SHA = "b" * 64
ADMIT = lambda station, conn: {"admitted": True, "reason": "test"}  # noqa: E731

#: The model agrees with the market: 27°C is both vectors' mode and mean.
AGREE = {"26°C": 0.15, "27°C": 0.60, "28°C": 0.20, "29°C": 0.05}
#: The model's mode is 28°C while the market's is 27°C.
SPLIT = {"26°C": 0.05, "27°C": 0.30, "28°C": 0.55, "29°C": 0.10}


def _insert(conn: sqlite3.Connection, table: str, values: dict) -> None:
    row = dict(values)
    for _cid, name, ctype, notnull, default, _pk in conn.execute(
        f"PRAGMA table_info({table})"
    ):
        if name in row or not notnull or default is not None:
            continue
        if "sha256" in str(name):
            row[name] = SHA
        elif str(name).endswith("_json"):
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


def _book(ask_27: float = 0.60) -> list[dict]:
    """The market's mode and mean are 27°C, sold at ``ask_27``."""

    def row(bucket: str, ask: float, bid: float) -> dict:
        return {
            "bucket": bucket,
            "ask_levels": [{"price": ask, "shares": 200.0}],
            "bid_levels": [{"price": bid, "shares": 200.0}],
            "ask_available": True,
        }

    return [
        row("26°C", 0.14, 0.12), row("27°C", ask_27, ask_27 - 0.02),
        row("28°C", 0.20, 0.18), row("29°C", 0.05, 0.03),
    ]


@pytest.fixture
def store(tmp_path, monkeypatch) -> sqlite3.Connection:
    conn = sqlite3.connect(tmp_path / "target.db", isolation_level=None)
    apply_production_ssot_schema(conn)
    apply_target_market_registry_schema(conn)
    armed = tmp_path / "intent.json"
    armed.write_text(json.dumps({"live_enabled": True}), encoding="utf-8")
    monkeypatch.setattr(sub, "LIVE_INTENT_PATH", armed)
    monkeypatch.setattr(lane, "settle_stuck_lane_cycles", lambda conn: 0)
    monkeypatch.setenv("PM_TARGET_MARKET_MANIFEST", str(tmp_path / "no_manifest.json"))
    _seed_registry(conn)
    return conn


def _seed_registry(conn: sqlite3.Connection) -> None:
    registry_sha = "reg".ljust(64, "0")
    _insert(conn, "basket_target_market_registry_snapshot", {
        "snapshot_sha256": registry_sha,
        "source_scope": "shared_live_registry_export_only",
    })
    for bucket, token in TOKENS.items():
        _insert(conn, "basket_target_market_registry", {
            "snapshot_sha256": registry_sha, "slug": f"{ST}-{bucket}", "event_date": ED,
            "outcome": bucket, "city_slug": "shanghai", "station_id": ST,
            "settlement_timezone": TZ, "yes_token": token, "market_active": 1,
            "market_accepting_orders": 1,
            "registry_json": json.dumps({
                "order_price_min_tick_size": 0.01, "order_min_size": 5.0,
            }),
            "refreshed_at": DECIDED, "source_eligible": 1, "source_lane": "awc",
        })


def _seed(
    conn: sqlite3.Connection,
    *,
    fact_id: str = "fact1",
    decided: str = DECIDED,
    probabilities: dict | None = None,
    book: list[dict] | None = None,
    observed: str = OBSERVED,
    sequence: int = 1,
    created: str | None = None,
) -> None:
    envelope_id = f"env_{fact_id}"
    _insert(conn, "basket_probability_envelope", {
        "envelope_id": envelope_id, "stage": "P1", "checkpoint_id": f"cp_{envelope_id}",
        "station_id": ST, "event_date": ED, "settlement_timezone": TZ,
        "decision_at_utc": decided, "fresh_until_utc": "2026-09-19T12:00:00Z",
        "bucket_ordinals_json": json.dumps(ORDINALS),
        "probabilities_json": json.dumps(AGREE if probabilities is None else probabilities),
        "content_sha256": (envelope_id * 64)[:64], "state": "COMMITTED",
        "created_at": decided,
    })
    snapshot_id = f"snap_{fact_id}"
    _insert(conn, "basket_official_ws_snapshot", {
        "snapshot_id": snapshot_id, "market_id": "m", "station_id": ST,
        "event_date": ED, "settlement_timezone": TZ, "sequence": sequence,
        "observed_at": observed, "quote_version": "v", "quote_observed_at": observed,
        "quote_expires_at": "2026-09-19T12:00:00Z", "quote_source": "OFFICIAL_CLOB_WS",
        "remaining_budget_usdc": 100.0,
        "buckets_json": json.dumps(book if book is not None else _book()),
        "state": "COMMITTED", "created_at": decided,
    })
    _insert(conn, "basket_policy_training_fact", {
        "fact_id": fact_id, "station_id": ST, "event_date": ED,
        "settlement_timezone": TZ, "decision_at_utc": decided,
        "decision_scope_key": f"scope_{fact_id}", "captured_at_utc": decided,
        "source_sequence": 1, "quote_observed_at": observed, "quote_expires_at": observed,
        "stage": "P1", "probability_envelope_id": envelope_id,
        "ws_snapshot_id": snapshot_id, "authority_scope": "PRODUCTION_NO_LIVE_FORWARD",
        "created_at_utc": created or decided,
    })


def _plan(conn: sqlite3.Connection, now: str = NOW, **kwargs):
    return lane.plan_p1_consensus_entries(conn, None, now_utc=now, admission=ADMIT, **kwargs)


def _settings(**overrides) -> dict:
    return {**P1_CONSENSUS_DEFAULT_SETTINGS, **overrides}


# ---------------------------------------------------------------------------
# The four centres.


def test_the_mean_bucket_rounds_half_up_to_the_nearest_rung() -> None:
    ords = {"a": 1, "b": 2, "c": 3}
    assert lane.mean_bucket({"a": 0.5, "b": 0.0, "c": 0.5}, ords)[0] == "b"
    # sum = 2.5 -> 3 (half up)
    assert lane.mean_bucket({"a": 0.25, "b": 0.0, "c": 0.75}, ords)[0] == "c"
    assert lane.mean_bucket({"a": 0.75, "b": 0.0, "c": 0.25}, ords)[0] == "b"
    # A gap in the ladder: 2 is not a rung, so the tie goes to the lower one.
    gap = {"a": 1, "c": 3}
    assert lane.mean_bucket({"a": 0.5, "c": 0.5}, gap)[0] == "a"


def test_the_four_centres_are_read_on_the_buckets_both_vectors_share() -> None:
    market = {"26°C": 0.14, "27°C": 0.59, "28°C": 0.20, "29°C": 0.05, "30°C": 0.02}
    read = lane.evaluate_consensus(AGREE, ORDINALS, market)
    assert read["state"] == "read" and read["four_same"] is True
    assert {read[k] for k in ("model_mode", "model_mean", "market_mode", "market_mean")} == {"27°C"}
    # 30°C is not on the model's ladder, so the market is renormalised
    # without it.
    assert read["market_share"] == pytest.approx(0.59 / 0.98, abs=1e-6)
    assert read["buckets_in_common"] == 4


def test_a_split_on_any_one_centre_is_not_consensus() -> None:
    market = {"26°C": 0.14, "27°C": 0.59, "28°C": 0.20, "29°C": 0.05}
    split = lane.evaluate_consensus(SPLIT, ORDINALS, market)
    assert split["model_mode"] == "28°C" and split["market_mode"] == "27°C"
    assert split["four_same"] is False
    # Modes agree but the model's mean sits a rung warmer.
    skewed = {"26°C": 0.0, "27°C": 0.40, "28°C": 0.30, "29°C": 0.30}
    read = lane.evaluate_consensus(skewed, ORDINALS, market)
    assert read["model_mode"] == "27°C" and read["model_mean"] == "28°C"
    assert read["four_same"] is False


# ---------------------------------------------------------------------------
# The window.


@pytest.mark.parametrize(
    ("decided", "is_candidate"),
    [
        ("2026-09-19T05:30:00.000Z", False),  # 10.5 h left
        ("2026-09-19T06:00:00.000Z", False),  # exactly 10 h: the bin is half-open
        ("2026-09-19T06:00:36.000Z", True),   # 9.99 h
        ("2026-09-19T08:00:00.000Z", True),   # 8 h
        ("2026-09-19T10:00:00.000Z", True),   # exactly 6 h is inside
        ("2026-09-19T10:06:00.000Z", False),  # 5.9 h
    ],
)
def test_only_six_to_ten_hours_from_the_settlement_clock_is_a_candidate(
    store, decided, is_candidate
) -> None:
    _seed(store, decided=decided)
    seen = lane.candidate_facts(
        store, now_utc=decided, min_remaining_hours=6.0, max_remaining_hours=10.0,
        fresh_seconds=1200.0,
    )
    assert [c["fact_id"] for c in seen] == (["fact1"] if is_candidate else [])


def test_one_decision_second_is_the_fact_committed_last(store) -> None:
    _seed(store, fact_id="early", created="2026-09-19T08:00:01.000Z")
    _seed(store, fact_id="late", created="2026-09-19T08:00:05.000Z", sequence=2)
    seen = lane.candidate_facts(
        store, now_utc=NOW, min_remaining_hours=6.0, max_remaining_hours=10.0,
        fresh_seconds=1200.0,
    )
    assert [c["fact_id"] for c in seen] == ["late"]


def test_a_decision_past_the_freshness_window_is_recorded_not_traded(store) -> None:
    _seed(store, decided="2026-09-19T07:30:00.000Z")
    planned = _plan(store)
    assert planned["plans"] == []
    [row] = planned["evaluations"]
    assert row["refusal"].startswith("fact_stale:") and row["fired"] is False


# ---------------------------------------------------------------------------
# Firing, and the row it leaves.


def test_consensus_in_the_band_is_planned_as_a_rung_at_bid_plus_one(store) -> None:
    """v2 (「吃单改为post only。best买一+1，挂单周期改为最短」): a post-only GTD
    rung one tick over the best bid, signed at the venue's minimum horizon."""

    _seed(store)
    planned = _plan(store)
    [plan] = planned["plans"]
    assert plan["bucket"] == "27°C"
    assert (plan["best_bid"], plan["best_ask"]) == (0.58, 0.60)
    assert plan["limit_price"] == pytest.approx(0.59)
    assert plan["order_type"] == "GTD" and plan["post_only"] is True
    assert "fok_price_cap" not in plan
    # The shortest horizon the venue takes: its floor (240 s) plus the signing
    # margin, not the belief's own end.
    from bayes_basket_target.production import (  # noqa: PLC0415
        GTD_SIGNING_MARGIN_SECONDS, VENUE_GTD_MINIMUM_HORIZON_SECONDS,
    )
    horizon = plan["expiration"] - lane._utc(NOW).timestamp()
    assert horizon == pytest.approx(
        VENUE_GTD_MINIMUM_HORIZON_SECONDS + GTD_SIGNING_MARGIN_SECONDS, abs=1.0
    )
    assert plan["expiration_basis"] == "venue_minimum_horizon"
    assert plan["cost_usdc"] <= 5.0 + 1e-9
    assert plan["remaining_hours"] == pytest.approx(8.0)
    assert plan["order_intent_id"].startswith(lane.INTENT_PREFIX)
    row = store.execute(
        f"SELECT fired, refusal, four_same, model_mode, model_mean, market_mode,"
        f" market_mean, best_ask, remaining_hours FROM {lane.EVALUATION_TABLE}"
        " WHERE order_intent_id=?",
        (plan["order_intent_id"],),
    ).fetchone()
    assert row[:3] == (1, None, 1)
    assert set(row[3:7]) == {"27°C"}
    assert row[7] == pytest.approx(0.60) and row[8] == pytest.approx(8.0)


@pytest.mark.parametrize(("ask", "fires"), [(0.49, False), (0.50, True), (0.69, True), (0.70, False)])
def test_the_band_is_half_open_on_the_best_ask(store, ask, fires) -> None:
    _seed(store, book=_book(ask_27=ask))
    planned = _plan(store)
    assert bool(planned["plans"]) is fires
    if not fires:
        [row] = planned["evaluations"]
        assert row["refusal"].startswith("rule_not_met:ask_outside_band:")


def test_the_rung_never_pays_over_the_cap(store) -> None:
    _seed(store, book=_book(ask_27=0.69))
    planned = _plan(store, settings=_settings(max_paid_price=0.67))
    assert planned["plans"] == []
    [row] = planned["evaluations"]
    assert row["refusal"].startswith("paid_price_above_cap:") and row["fired"] is True


def test_a_split_decision_is_written_and_not_traded(store) -> None:
    """The row that did not fire is the evidence the rule is re-asked on."""

    _seed(store, probabilities=SPLIT)
    planned = _plan(store)
    assert planned["plans"] == []
    [row] = planned["evaluations"]
    assert row["refusal"] == "rule_not_met:centres_split" and row["fired"] is False
    stored = store.execute(
        f"SELECT four_same, model_mode, market_mode FROM {lane.EVALUATION_TABLE}"
    ).fetchone()
    assert stored == (0, "28°C", "27°C")


def test_one_decision_is_judged_once(store) -> None:
    _seed(store)
    assert len(_plan(store)["plans"]) == 1
    assert _plan(store)["plans"] == []


def test_a_stale_book_is_scored_but_never_bought(store) -> None:
    _seed(store, observed="2026-09-19T07:59:00.000Z")
    planned = _plan(store)
    assert planned["plans"] == []
    [row] = planned["evaluations"]
    assert row["refusal"].startswith("send_book_stale:") and row["fired"] is True


# ---------------------------------------------------------------------------
# A station-day may be bought several times (the user's ruling).


def test_the_same_bucket_is_bought_once_a_second_take_needs_a_new_bucket(store) -> None:
    """User ruling 2026-09-23 「每站日上限 2 笔，单桶上限 1 笔」."""

    _seed(store, fact_id="f1", decided="2026-09-19T07:35:00.000Z")
    _seed(store, fact_id="f2", decided="2026-09-19T08:00:00.000Z", sequence=2)
    planned = _plan(store, settings=_settings(fact_fresh_seconds=3600.0))
    assert [p["bucket"] for p in planned["plans"]] == ["27°C"]
    refused = {e["fact_id"]: e["refusal"] for e in planned["evaluations"] if e["refusal"]}
    assert refused["f2"] == "bucket_buy_cap:1"
    # The four centres move to 28°C: that is a new bucket and a second take.
    moved = {"26°C": 0.05, "27°C": 0.20, "28°C": 0.60, "29°C": 0.15}
    book = [
        {"bucket": "26°C", "ask_levels": [{"price": 0.06, "shares": 200.0}],
         "bid_levels": [{"price": 0.04, "shares": 200.0}], "ask_available": True},
        {"bucket": "27°C", "ask_levels": [{"price": 0.20, "shares": 200.0}],
         "bid_levels": [{"price": 0.18, "shares": 200.0}], "ask_available": True},
        {"bucket": "28°C", "ask_levels": [{"price": 0.60, "shares": 200.0}],
         "bid_levels": [{"price": 0.58, "shares": 200.0}], "ask_available": True},
        {"bucket": "29°C", "ask_levels": [{"price": 0.15, "shares": 200.0}],
         "bid_levels": [{"price": 0.13, "shares": 200.0}], "ask_available": True},
    ]
    _seed(store, fact_id="f3", decided="2026-09-19T08:01:00.000Z", sequence=3,
          probabilities=moved, book=book)
    again = _plan(store, now="2026-09-19T08:02:00.000Z",
                  settings=_settings(fact_fresh_seconds=3600.0, min_minutes_between_buys=0.0))
    assert [(p["fact_id"], p["bucket"]) for p in again["plans"]] == [("f3", "28°C")]


def test_the_station_day_buy_cap_bounds_the_takes(store) -> None:
    for i in range(3):
        _seed(store, fact_id=f"f{i}", decided=f"2026-09-19T07:5{i}:00.000Z", sequence=i + 1)
    planned = _plan(store, settings=_settings(
        max_buys_per_station_day=2, min_minutes_between_buys=0.0, max_buys_per_bucket=9,
    ))
    assert len(planned["plans"]) == 2
    assert any(
        str(e["refusal"]).startswith("station_day_buy_cap:2") for e in planned["evaluations"]
    )


def test_takes_on_one_station_day_are_spaced_twenty_minutes(store) -> None:
    """User ruling 2026-09-23 「加 20 分钟间隔」: LTAC bought four times in
    four minutes at one price; the replay's takes sat 6-91 minutes apart."""

    fresh = "2026-09-19T08:05:40.000Z"
    _seed(store, fact_id="f1", decided="2026-09-19T07:45:00.000Z", observed=fresh)
    _seed(store, fact_id="f2", decided="2026-09-19T07:46:00.000Z", sequence=2, observed=fresh)
    _seed(store, fact_id="f3", decided="2026-09-19T08:05:00.000Z", sequence=3, observed=fresh)
    planned = _plan(store, now="2026-09-19T08:06:00.000Z",
                    settings=_settings(fact_fresh_seconds=3600.0, max_buys_per_bucket=9))
    assert [p["fact_id"] for p in planned["plans"]] == ["f1", "f3"]
    refused = {e["fact_id"]: e["refusal"] for e in planned["evaluations"] if e["refusal"]}
    assert refused["f2"].startswith("station_day_buy_spacing:1.0m<20m")


def test_a_killed_take_does_not_start_the_spacing_clock(store) -> None:
    _seed(store, fact_id="f1", decided="2026-09-19T07:59:00.000Z")
    [first] = _plan(store, now="2026-09-19T07:59:30.000Z")["plans"]
    # The venue killed it: the send writes a ``send:*`` refusal on the row.
    store.execute(
        f"UPDATE {lane.EVALUATION_TABLE} SET refusal='send:killed' WHERE order_intent_id=?",
        (first["order_intent_id"],),
    )
    assert lane.last_buy_decision_at(store, ST, ED) is None
    _seed(store, fact_id="f2", decided="2026-09-19T08:00:00.000Z", sequence=2)
    assert len(_plan(store)["plans"]) == 1


def _mint(store: sqlite3.Connection, intent_id: str, state: str) -> None:
    decision = lane_decision_id(lane.LANE, ST, ED)
    if store.execute("SELECT 1 FROM auto_trade_decisions WHERE decision_id=?", (decision,)).fetchone() is None:
        _insert(store, "auto_trade_decisions", {
            "decision_id": decision, "station_id": ST, "event_date": ED,
            "settlement_timezone": TZ, "strategy_lane": "basket",
            "decided_state": "prepared", "will_order": 0, "created_at": DECIDED,
        })
    _insert(store, "basket_target_order_intents", {
        "order_intent_id": intent_id, "decision_id": decision,
        "cycle_id": f"p1_consensus_cycle_{intent_id}", "bucket": "27°C",
        "token_id": TOKENS["27°C"], "side": "YES", "order_action": "BUY",
        "limit_price": 0.61, "requested_size": 8.0, "requested_notional_usdc": 4.88,
        "state": state, "live_submit_authorized": 1, "created_at": DECIDED,
        "idempotency_key": f"idem_{intent_id}",
        "intent_json": json.dumps({"lane": lane.LANE}),
    })


def test_a_killed_take_is_not_a_buy_but_a_filled_one_is(store) -> None:
    _mint(store, "p1cons_killed", "failed")
    assert lane.station_day_buys(store, ST, ED) == 0
    assert lane.station_day_sends(store, ST, ED) == 1
    _mint(store, "p1cons_filled", "filled")
    assert lane.station_day_buys(store, ST, ED) == 1


def test_the_lane_does_not_yield_to_the_tiebreak_lane(store) -> None:
    """User ruling 2026-09-23 「照买，不避让」: a tiebreak rung resting on the
    station-day, and a tiebreak position on the very same bucket, stop
    nothing -- both are recorded on the row."""

    _seed(store)
    decision = lane_decision_id("tiebreak", ST, ED)
    _insert(store, "auto_trade_decisions", {
        "decision_id": decision, "station_id": ST, "event_date": ED,
        "settlement_timezone": TZ, "strategy_lane": "basket",
        "decided_state": "prepared", "will_order": 0, "created_at": DECIDED,
    })
    _insert(store, "basket_target_order_intents", {
        "order_intent_id": "tiebreak_open", "decision_id": decision,
        "cycle_id": "tiebreak_cycle_1", "bucket": "28°C", "token_id": TOKENS["28°C"],
        "side": "YES", "order_action": "BUY", "limit_price": 0.19,
        "requested_size": 30.0, "requested_notional_usdc": 5.7, "state": "submitted",
        "live_submit_authorized": 1, "created_at": DECIDED,
        "intent_json": json.dumps({"lane": "tiebreak"}),
    })
    _insert(store, "basket_confirmed_inventory_snapshot", {
        "inventory_snapshot_id": "inv1", "station_id": ST, "event_date": ED,
        "inventory_version": "v1", "observed_at": DECIDED, "complete": 1,
        "inventory_json": json.dumps({"filled_by_bucket": {"27°C": 10.0}}),
        "state": "COMMITTED", "created_at": DECIDED,
    })
    planned = _plan(store)
    assert len(planned["plans"]) == 1
    stored = store.execute(
        f"SELECT held_buckets, open_order_lane, open_order_bucket FROM {lane.EVALUATION_TABLE}"
    ).fetchone()
    assert stored == ("27°C", "tiebreak", "28°C")


# ---------------------------------------------------------------------------
# Registration.


def test_the_lane_is_registered_and_shipped_on() -> None:
    from bayes_basket_target import strategy_lanes  # noqa: PLC0415

    assert strategy_lanes.LANE_P1_CONSENSUS in strategy_lanes.KNOWN_LANES
    served = dict(strategy_lanes.P1_CONSENSUS_DEFAULT_SETTINGS)
    assert served["enabled"] is True
    assert served["rule"] == lane.RULE_V2
    # The operator file names the same rule (its switch is the operator's).
    assert strategy_lanes.lane_settings(strategy_lanes.LANE_P1_CONSENSUS)["rule"] == lane.RULE_V2
    assert lane.EXECUTION_CANDIDATE == "MAKER"
    assert served["stake_usdc"] == 5.0
    assert (served["min_remaining_hours"], served["max_remaining_hours"]) == (6.0, 10.0)
    assert (served["min_ask"], served["max_ask"]) == (0.5, 0.7)
    assert served["max_buys_per_station_day"] == 2
    assert served["max_buys_per_bucket"] == 1
    assert served["min_minutes_between_buys"] == 20.0
    assert served["send_book_fresh_seconds"] <= 60.0


def test_the_taker_rule_is_retired() -> None:
    with pytest.raises(ValueError, match="p1_consensus_rule_retired"):
        lane.rule_version_for(lane.RULE_V1)


# ---------------------------------------------------------------------------
# 「没成交继续挂」: the rung's fate, and the re-post.


def _intent_row(store, plan: dict, state: str, filled: float = 0.0) -> None:
    decision = lane_decision_id(lane.LANE, ST, ED)
    if store.execute("SELECT 1 FROM auto_trade_decisions WHERE decision_id=?", (decision,)).fetchone() is None:
        _insert(store, "auto_trade_decisions", {
            "decision_id": decision, "station_id": ST, "event_date": ED,
            "settlement_timezone": TZ, "strategy_lane": "basket",
            "decided_state": "prepared", "will_order": 0, "created_at": DECIDED,
        })
    _insert(store, "basket_target_order_intents", {
        "order_intent_id": plan["order_intent_id"], "decision_id": decision,
        "cycle_id": f"p1_consensus_cycle_{plan['order_intent_id']}", "bucket": plan["bucket"],
        "token_id": plan["token_id"], "side": "YES", "order_action": "BUY",
        "limit_price": plan["limit_price"], "requested_size": plan["shares"],
        "requested_notional_usdc": plan["cost_usdc"], "state": state,
        "live_submit_authorized": 1, "created_at": NOW,
        "idempotency_key": f"idem_{plan['order_intent_id']}",
        "intent_json": json.dumps({"lane": lane.LANE}),
    })
    if filled:
        _insert(store, "basket_target_order_reconciliations", {
            "order_intent_id": plan["order_intent_id"], "filled_size": filled,
            "state": "resolved_filled",
        })


def test_an_unfilled_rung_that_lapsed_is_not_a_take_and_is_reposted(store) -> None:
    _seed(store)
    [plan] = _plan(store)["plans"]
    _intent_row(store, plan, "cancelled")          # the GTD lapsed, nothing filled
    later = "2026-09-19T08:06:00.000Z"
    # A fresh book at the later instant, still in the band.
    _insert(store, "basket_official_ws_snapshot", {
        "snapshot_id": "snap_later", "market_id": "m", "station_id": ST, "event_date": ED,
        "settlement_timezone": TZ, "sequence": 9, "observed_at": "2026-09-19T08:05:40.000Z",
        "quote_version": "v", "quote_observed_at": "2026-09-19T08:05:40.000Z",
        "quote_expires_at": "2026-09-19T12:00:00Z", "quote_source": "OFFICIAL_CLOB_WS",
        "remaining_budget_usdc": 100.0, "buckets_json": json.dumps(_book(ask_27=0.61)),
        "state": "COMMITTED", "created_at": "2026-09-19T08:05:40.000Z",
    })
    # The sync reads the lapse: the row is no longer a take, the bucket is free.
    lane.sync_rungs(store, floor_event_date=ED)
    assert lane.bucket_buys(store, ST, ED, "27°C") == 0
    assert store.execute(f"SELECT refusal FROM {lane.EVALUATION_TABLE}").fetchone()[0] == "send:cancelled"
    again = _plan(store, now=later)
    [repost] = again["plans"]
    assert repost["order_intent_id"] != plan["order_intent_id"]
    assert repost["limit_price"] == pytest.approx(0.60)   # bid 0.59 + one tick
    row = store.execute(
        f"SELECT order_intent_id, refusal, retry_count FROM {lane.EVALUATION_TABLE}"
    ).fetchone()
    assert row == (repost["order_intent_id"], None, 1)


def test_a_filled_rung_is_a_take_and_is_never_reposted(store) -> None:
    _seed(store)
    [plan] = _plan(store)["plans"]
    _intent_row(store, plan, "cancelled", filled=3.0)      # partly filled, then lapsed
    assert lane.sync_rungs(store, floor_event_date=ED) == 0
    assert lane.bucket_buys(store, ST, ED, "27°C") == 1
    assert _plan(store, now="2026-09-19T08:03:00.000Z")["plans"] == []


def test_one_rung_at_a_time(store) -> None:
    _seed(store, fact_id="f1", decided="2026-09-19T07:59:00.000Z")
    [plan] = _plan(store)["plans"]
    _intent_row(store, plan, "submitted")                   # resting at the venue
    _seed(store, fact_id="f2", decided="2026-09-19T08:01:00.000Z", sequence=2)
    planned = _plan(store)
    assert planned["plans"] == []
    [row] = planned["evaluations"]
    assert row["refusal"] == "rung_resting:27°C" and row["fired"] is True


def test_no_repost_once_the_day_is_under_six_hours(store) -> None:
    _seed(store, decided="2026-09-19T09:55:00.000Z", observed="2026-09-19T09:55:30.000Z")
    [plan] = _plan(store, now="2026-09-19T09:56:00.000Z")["plans"]
    _intent_row(store, plan, "cancelled")
    assert _plan(store, now="2026-09-19T10:05:00.000Z")["plans"] == []
    refusal = store.execute(f"SELECT refusal FROM {lane.EVALUATION_TABLE}").fetchone()[0]
    assert refusal == "window_closed"


def test_the_send_is_a_post_only_gtd_rung(store, monkeypatch) -> None:
    _seed(store)
    sent = {}

    def fake_send(conn, **kwargs):
        sent.update(kwargs)
        return {"exchange_post_started": True, "event_type": "POST_ACK", "matched_size": 0.0}

    monkeypatch.setattr(lane, "send_lane_buy", fake_send)
    receipt = lane.execute_p1_consensus_entries(
        store, None, transport=object(), now_utc=NOW, live_submit_authorized=True,
        admission=ADMIT,
    )
    assert receipt["submitted"] == 1
    assert sent["execution_candidate"] == "MAKER"
    assert isinstance(sent["expiration"], int) and "fok_price_cap" not in sent
    assert sent["intent_body"]["post_only"] is True
    # A rung the venue took is resting, not refused.
    assert store.execute(f"SELECT refusal FROM {lane.EVALUATION_TABLE}").fetchone()[0] is None


def test_the_daemon_runs_the_lane_at_both_ends_of_the_cycle() -> None:
    source = (ROOT / "scripts" / "weather_basket_yes_daemon.py").read_text()
    tree = ast.parse(source)
    called = [
        node.func.id for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    ]
    assert called.count("execute_p1_consensus_entries") == 1
    assert called.count("_consensus_head_execute") == 1
    assert '"p1_consensus_lane": p1_consensus_receipt' in source
    assert '"p1_consensus_lane_head": p1_consensus_head_receipt' in source


def test_stuck_cycles_of_this_lane_are_settled_with_the_others() -> None:
    from bayes_basket_target import tiebreak_lane  # noqa: PLC0415

    assert "p1_consensus_cycle_" in tiebreak_lane.LANE_CYCLE_PREFIXES
