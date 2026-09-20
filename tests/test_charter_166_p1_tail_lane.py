"""P1 尾段线 -- the market's ladder times our observation likelihood.

Charter 166 (user ruling 2026-09-20 「上线尾段车道 p1_tail 策略」).  On a P1
envelope whose report sits 6 to 10 hours from the station's settlement clock,
the belief is ``norm(norm(market_mid x alive) x L)`` and the lane crosses FOK
at the ask where that belief clears it by the hurdle.  Outside the bin, past
the freshness window, on a bucket already held or behind another lane's
resting BUY, it does not trade -- and says which.
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

from bayes_basket_target import order_submission as sub  # noqa: E402
from bayes_basket_target import p1_tail_lane as lane  # noqa: E402
from bayes_basket_target.strategy_lanes import P1_TAIL_DEFAULT_SETTINGS  # noqa: E402
from bayes_basket_target.lane_orders import lane_decision_id  # noqa: E402
from bayes_basket_target.market_registry import (  # noqa: E402
    apply_target_market_registry_schema,
)
from bayes_basket_target.ssot import apply_production_ssot_schema  # noqa: E402

ST, ED, TZ = "ZSPD", "2026-09-19", "Asia/Shanghai"
NOW = "2026-09-19T04:00:00.000Z"
DECIDED = "2026-09-19T03:58:00.000Z"
#: The book a send may be priced on is much younger than the belief
#: (charter 166, 2026-09-21): the envelope may be five minutes old, the
#: ladder sixty seconds.
OBSERVED = "2026-09-19T03:59:40.000Z"
BUCKETS = ("26°C", "27°C", "28°C", "29°C")
TOKENS = {b: f"t{i}" for i, b in enumerate(BUCKETS)}
SHA = "b" * 64
ADMIT = lambda station, conn: {"admitted": True, "reason": "test"}  # noqa: E731

#: The likelihood one report leaves: the day has printed 27 already, so the
#: two dead rungs are masked out and the rest slope down from 27.
LIKELIHOOD = {"26°C": 0.0, "27°C": 1.0, "28°C": 0.62, "29°C": 0.11}
EXCLUDED = ["26°C"]


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


def _book(ask_28: float = 0.30) -> list[dict]:
    """A ladder the market prices flat across 27/28 while the report has
    already put the day on 27: 28°C at ``ask_28`` is the cheap rung."""

    def row(bucket: str, ask: float, bid: float) -> dict:
        return {
            "bucket": bucket,
            "ask_levels": [{"price": ask, "shares": 200.0}],
            "bid_levels": [{"price": bid, "shares": 200.0}],
            "ask_available": True,
        }

    return [
        row("26°C", 0.05, 0.03), row("27°C", 0.46, 0.44),
        row("28°C", ask_28, ask_28 - 0.02), row("29°C", 0.10, 0.08),
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
    return conn


def _seed(
    conn: sqlite3.Connection,
    *,
    hours: float = 7.5,
    state: str = "APPLIED",
    decided: str = DECIDED,
    book: list[dict] | None = None,
    observed: str = OBSERVED,
    envelope_id: str = "env1",
    accepting: int = 1,
    likelihood: dict | None = None,
) -> None:
    receipt = {
        "schema": "p1_observation_likelihood_receipt_v1",
        "state": state,
        "clock_hours": hours,
        "lambda": 1.0,
        "rule": "p1_root_x_mask_x_exp_direct_log_update_v1",
        "running_high_c": 27.0,
        "rise_probability": 0.21,
        "likelihood_by_bucket": dict(LIKELIHOOD if likelihood is None else likelihood),
    }
    _insert(conn, "basket_probability_envelope", {
        "envelope_id": envelope_id, "stage": "P1", "checkpoint_id": f"cp_{envelope_id}",
        "station_id": ST, "event_date": ED, "settlement_timezone": TZ,
        "decision_at_utc": decided, "fresh_until_utc": "2026-09-19T05:00:00Z",
        "bucket_ordinals_json": json.dumps({b: 26 + i for i, b in enumerate(BUCKETS)}),
        "probabilities_json": json.dumps({b: 0.25 for b in BUCKETS}),
        "content_sha256": (envelope_id * 64)[:64], "state": "COMMITTED",
        "created_at": decided,
        "lineage_json": json.dumps({
            "p1_observation_likelihood_json": json.dumps(receipt),
            "hard_excluded_buckets": ",".join(EXCLUDED),
        }),
    })
    _insert(conn, "basket_official_ws_snapshot", {
        "snapshot_id": f"snap_{envelope_id}", "market_id": "m", "station_id": ST,
        "event_date": ED, "settlement_timezone": TZ, "sequence": 1,
        "observed_at": observed, "quote_version": "v", "quote_observed_at": observed,
        "quote_expires_at": "2026-09-19T05:00:00Z", "quote_source": "OFFICIAL_CLOB_WS",
        "remaining_budget_usdc": 100.0,
        "buckets_json": json.dumps(book if book is not None else _book()),
        "state": "COMMITTED", "created_at": decided,
    })
    registry_sha = f"reg{envelope_id}".ljust(64, "0")[:64]
    _insert(conn, "basket_target_market_registry_snapshot", {
        "snapshot_sha256": registry_sha,
        "source_scope": "shared_live_registry_export_only",
    })
    for bucket, token in TOKENS.items():
        _insert(conn, "basket_target_market_registry", {
            "snapshot_sha256": registry_sha, "slug": f"{ST}-{bucket}", "event_date": ED,
            "outcome": bucket, "city_slug": "shanghai", "station_id": ST,
            "settlement_timezone": TZ, "yes_token": token, "market_active": 1,
            "market_accepting_orders": accepting,
            "registry_json": json.dumps({
                "order_price_min_tick_size": 0.01, "order_min_size": 5.0,
            }),
            "refreshed_at": decided, "source_eligible": 1, "source_lane": "awc",
        })


def _plan(conn: sqlite3.Connection, **kwargs):
    return lane.plan_p1_tail_entries(conn, None, now_utc=NOW, admission=ADMIT, **kwargs)


# ---------------------------------------------------------------------------
# The belief.


def test_the_belief_is_the_market_masked_then_tilted_by_the_likelihood() -> None:
    market = {"26°C": 0.05, "27°C": 0.45, "28°C": 0.35, "29°C": 0.15}
    asks = {"26°C": 0.05, "27°C": 0.46, "28°C": 0.30, "29°C": 0.10}
    read = lane.evaluate_rule(market, LIKELIHOOD, EXCLUDED, asks, hurdle=0.08)
    assert read["state"] == "read"
    # The dead rung leaves the prior entirely; the rest renormalise.
    assert "26°C" not in read["prior"]
    assert read["prior"]["27°C"] == pytest.approx(0.45 / 0.95, abs=1e-6)
    # ... then the likelihood, and renormalise again.
    weights = {b: read["prior"][b] * LIKELIHOOD[b] for b in read["prior"]}
    total = sum(weights.values())
    for bucket, weight in weights.items():
        assert read["belief"][bucket] == pytest.approx(weight / total, abs=1e-6)
    assert sum(read["belief"].values()) == pytest.approx(1.0, abs=1e-9)
    assert read["gaps"]["28°C"] == pytest.approx(read["belief"]["28°C"] - 0.30, abs=1e-6)


def test_only_a_gap_at_or_over_the_hurdle_fires() -> None:
    market = {"26°C": 0.05, "27°C": 0.45, "28°C": 0.35, "29°C": 0.15}
    asks = {"26°C": 0.05, "27°C": 0.46, "28°C": 0.30, "29°C": 0.10}
    assert lane.evaluate_rule(market, LIKELIHOOD, EXCLUDED, asks, hurdle=0.08)["fired"]
    assert lane.evaluate_rule(
        market, LIKELIHOOD, EXCLUDED, asks, hurdle=0.90
    )["fired"] == []


def test_a_dead_bucket_is_never_bought_however_cheap() -> None:
    market = {"26°C": 0.40, "27°C": 0.40, "28°C": 0.15, "29°C": 0.05}
    asks = {"26°C": 0.01, "27°C": 0.46, "28°C": 0.30, "29°C": 0.10}
    read = lane.evaluate_rule(market, LIKELIHOOD, EXCLUDED, asks, hurdle=0.08)
    assert "26°C" not in read["fired"]
    assert "26°C" not in read["belief"]


def test_a_live_bucket_with_no_likelihood_stops_the_reading() -> None:
    """Assuming 1.0 for a rung the receipt never scored would be inventing
    the belief this lane trades on."""

    read = lane.evaluate_rule(
        {"27°C": 0.5, "28°C": 0.5}, {"27°C": 1.0}, [], {"27°C": 0.4, "28°C": 0.4},
        hurdle=0.08,
    )
    assert read["state"] == "likelihood_incomplete" and read["missing"] == ["28°C"]


# ---------------------------------------------------------------------------
# The take.


def test_the_take_is_priced_at_the_ask_not_walked() -> None:
    row = {"bucket": "28°C", "ask_available": True,
           "ask_levels": [{"price": 0.30, "shares": 200.0}, {"price": 0.35, "shares": 500.0}],
           "bid_levels": [{"price": 0.28, "shares": 50.0}]}
    take = lane.taker_take(row, 5.0, tick_size=0.01, min_shares=5.0)
    assert take["limit_price"] == 0.30 and take["refusal"] is None
    assert take["shares"] == pytest.approx(16.0)
    assert take["cost_usdc"] == pytest.approx(4.80)


@pytest.mark.parametrize("price", [0.01, 0.07, 0.13, 0.29, 0.30, 0.46, 0.72, 0.83, 0.99])
def test_both_venue_amounts_land_on_the_grid(price) -> None:
    """The refusal that taught this: SAEZ 2026-09-20 25°C, 6.94 shares at
    0.72 -> a cost of 4.9968, and the venue takes two decimals of cost and
    four of shares."""

    row = {"bucket": "x", "ask_available": True,
           "ask_levels": [{"price": price, "shares": 5000.0}], "bid_levels": []}
    take = lane.taker_take(row, 5.0, tick_size=0.01, min_shares=1.0)
    assert take["refusal"] is None, take
    cost, shares = take["cost_usdc"], take["shares"]
    assert cost == pytest.approx(round(cost, 2), abs=1e-9), (price, cost)
    assert shares == pytest.approx(round(shares, 4), abs=1e-9), (price, shares)
    assert cost <= 5.0 + 1e-9
    # ... and it does not give away more than one share of the stake.
    assert cost > 5.0 - price - 1e-9


def test_a_thin_top_level_is_taken_at_its_own_size_or_refused() -> None:
    thin = {"bucket": "28°C", "ask_available": True,
            "ask_levels": [{"price": 0.30, "shares": 9.0}], "bid_levels": []}
    # Nine shares on the level, one left behind (charter 166, 2026-09-21).
    assert lane.taker_take(thin, 5.0, tick_size=0.01, min_shares=5.0)["shares"] == 7.0
    too_thin = {"bucket": "28°C", "ask_available": True,
                "ask_levels": [{"price": 0.30, "shares": 2.0}], "bid_levels": []}
    assert lane.taker_take(
        too_thin, 5.0, tick_size=0.01, min_shares=5.0
    )["refusal"].startswith("thin_ask:")


def test_the_lane_crosses_fok_with_a_price_cap() -> None:
    from bayes_basket_target.production import venue_order_fields  # noqa: PLC0415

    fields = venue_order_fields(
        lane.EXECUTION_CANDIDATE, created_at_utc=NOW, limit_price=0.30,
    )
    assert fields["order_type"] == "FOK" and fields["post_only"] is False
    assert fields["fok_price_cap"] == 0.30 and "expiration" not in fields


# ---------------------------------------------------------------------------
# The window.


@pytest.mark.parametrize(
    ("hours", "is_candidate"),
    [
        (11.0, False),   # the market has all day to read the same report
        (10.0, False),   # the bin is half-open: ten hours is outside it
        (9.99, True),
        (7.5, True),
        (6.0, True),     # ... and six is inside
        (5.9, False),    # under six the market is already on the answer
        (4.0, False),
    ],
)
def test_only_the_six_to_ten_hour_bin_is_a_candidate(store, hours, is_candidate) -> None:
    _seed(store, hours=hours)
    seen = lane.tail_envelopes(
        store, now_utc=NOW, min_remaining_hours=6.0, max_remaining_hours=10.0,
        fresh_seconds=300.0,
    )
    assert [c["envelope_id"] for c in seen] == (["env1"] if is_candidate else [])


def test_a_likelihood_that_was_not_applied_is_not_a_candidate(store) -> None:
    _seed(store, state="IDENTITY_NO_EVIDENCE")
    assert lane.tail_envelopes(
        store, now_utc=NOW, min_remaining_hours=6.0, max_remaining_hours=10.0,
        fresh_seconds=300.0,
    ) == []


def test_an_envelope_past_the_freshness_window_is_refused_not_traded(store) -> None:
    _seed(store, decided="2026-09-19T03:50:00.000Z")
    planned = _plan(store)
    assert planned["plans"] == []
    [row] = planned["evaluations"]
    assert row["refusal"].startswith("envelope_stale:")
    assert row["bucket"] == lane.NO_BUCKET


# ---------------------------------------------------------------------------
# Firing, and the row it leaves.


def test_a_cheap_live_bucket_is_planned_as_a_take(store) -> None:
    _seed(store)
    planned = _plan(store)
    [plan] = planned["plans"]
    # 27°C is the rung: the report has already printed it, so the likelihood
    # leaves it whole while the market still prices it at 0.46 -- and the
    # limit is authorised one tick above that (charter 166, 2026-09-21).
    assert plan["bucket"] == "27°C"
    assert plan["best_ask"] == 0.46 and plan["limit_price"] == pytest.approx(0.47)
    assert plan["walked_gap"] == pytest.approx(plan["belief"] - 0.47, abs=1e-6)
    assert plan["order_type"] == "FOK" and plan["post_only"] is False
    assert plan["fok_price_cap"] == pytest.approx(0.47)
    assert plan["remaining_hours"] == 7.5
    assert plan["gap"] >= 0.08
    assert plan["order_intent_id"].startswith("p1tail_")
    row = store.execute(
        f"SELECT bucket, fired, refusal, market_ask, belief, gap, remaining_hours"
        f" FROM {lane.EVALUATION_TABLE} WHERE order_intent_id=?",
        (plan["order_intent_id"],),
    ).fetchone()
    assert row[0] == "27°C" and row[1] == 1 and row[2] is None
    # The ROW still records the ask the reading was taken at, so it stays
    # comparable to the measurement; the walk lives on the plan.
    assert row[3] == pytest.approx(0.46) and row[5] == pytest.approx(plan["gap"])
    assert row[6] == pytest.approx(7.5)


def test_a_reading_that_clears_nothing_is_still_written(store) -> None:
    """The evidence a later fit re-asks the question on is the row that did
    not fire, not only the row that did."""

    _seed(store)
    planned = _plan(store, settings={**P1_TAIL_DEFAULT_SETTINGS, "hurdle": 0.9})
    assert planned["plans"] == []
    [row] = planned["evaluations"]
    assert row["refusal"] == "rule_not_met:no_bucket_over_hurdle"
    stored = store.execute(
        f"SELECT best_gap, remaining_hours FROM {lane.EVALUATION_TABLE}"
    ).fetchone()
    assert stored[0] is not None and stored[1] == pytest.approx(7.5)


def test_one_envelope_is_scored_once(store) -> None:
    _seed(store)
    first = _plan(store)
    again = _plan(store)
    assert len(first["plans"]) == 1 and again["plans"] == []


# ---------------------------------------------------------------------------
# Charter 166's three rules against the tiebreak lane.


def test_a_bucket_already_held_is_not_bought_again(store) -> None:
    _seed(store)
    _insert(store, "basket_confirmed_inventory_snapshot", {
        "inventory_snapshot_id": "inv1", "station_id": ST, "event_date": ED,
        "inventory_version": "v1", "observed_at": DECIDED, "complete": 1,
        "inventory_json": json.dumps({"filled_by_bucket": {"27°C": 12.0}}),
        "state": "COMMITTED", "created_at": DECIDED,
    })
    planned = _plan(store)
    assert planned["plans"] == []
    refusals = {e["bucket"]: e["refusal"] for e in planned["evaluations"]}
    assert refusals["27°C"] == "bucket_already_held"
    held = store.execute(
        f"SELECT held_buckets FROM {lane.EVALUATION_TABLE} WHERE bucket='27°C'"
    ).fetchone()[0]
    assert held == "27°C"


def test_the_lane_never_queues_behind_another_lanes_resting_buy(store) -> None:
    _seed(store)
    decision = lane_decision_id("tiebreak", ST, ED)
    _insert(store, "auto_trade_decisions", {
        "decision_id": decision, "station_id": ST, "event_date": ED,
        # The decisions table's own lane column is fenced to 'basket' by the
        # hard-NO trigger; which lane minted the order lives on the intent.
        "settlement_timezone": TZ, "strategy_lane": "basket",
        "decided_state": "submitted", "will_order": 1, "created_at": DECIDED,
    })
    _insert(store, "basket_target_order_intents", {
        "order_intent_id": "tiebreak_open", "decision_id": decision,
        "cycle_id": "tiebreak_cycle_1", "bucket": "26°C", "token_id": TOKENS["26°C"],
        "side": "YES", "order_action": "BUY", "limit_price": 0.05,
        "requested_size": 20.0, "requested_notional_usdc": 1.0, "state": "submitted",
        "live_submit_authorized": 1, "created_at": DECIDED,
        "intent_json": json.dumps({"lane": "tiebreak"}),
    })
    planned = _plan(store)
    assert planned["plans"] == []
    [row] = planned["evaluations"]
    assert row["refusal"].startswith("open_order:tiebreak:")
    lane_name, bucket, dead = store.execute(
        f"SELECT open_order_lane, open_order_bucket, open_order_bucket_dead"
        f" FROM {lane.EVALUATION_TABLE}"
    ).fetchone()
    # The rung the tiebreak lane is resting on is one THIS report has killed:
    # the count that would justify a cancel-then-take version.
    assert (lane_name, bucket, dead) == ("tiebreak", "26°C", 1)


def test_a_resting_sell_does_not_block_the_take(store) -> None:
    """The recycle lane's 0.99 offer rests on every held leg; it neither
    freezes the station-day's quotes nor shuts its token (charter 100)."""

    _seed(store)
    decision = lane_decision_id("recycle998", ST, ED)
    _insert(store, "auto_trade_decisions", {
        "decision_id": decision, "station_id": ST, "event_date": ED,
        "settlement_timezone": TZ, "strategy_lane": "basket",
        "decided_state": "submitted", "will_order": 1, "created_at": DECIDED,
    })
    _insert(store, "basket_target_order_intents", {
        "order_intent_id": "recycle_open", "decision_id": decision,
        "cycle_id": "recycle998_cycle_1", "bucket": "27°C", "token_id": TOKENS["27°C"],
        "side": "YES", "order_action": "SELL", "limit_price": 0.99,
        "requested_size": 10.0, "requested_notional_usdc": 9.9, "state": "submitted",
        "live_submit_authorized": 1, "created_at": DECIDED,
        "intent_json": json.dumps({"lane": "certain_win_recycle"}),
    })
    assert len(_plan(store)["plans"]) == 1


def test_the_station_day_send_cap_bounds_the_lane(store) -> None:
    _seed(store)
    planned = _plan(store, settings={**P1_TAIL_DEFAULT_SETTINGS, "max_sends_per_station_day": 0})
    assert planned["plans"] == []
    assert any(
        str(e["refusal"]).startswith("station_day_send_cap:")
        for e in planned["evaluations"]
    )


# ---------------------------------------------------------------------------
# Registration.


def test_the_lane_is_registered_and_shipped_on(tmp_path) -> None:
    from bayes_basket_target import strategy_lanes  # noqa: PLC0415

    assert strategy_lanes.LANE_P1_TAIL in strategy_lanes.KNOWN_LANES
    served = strategy_lanes.lane_settings(strategy_lanes.LANE_P1_TAIL)
    assert served["enabled"] is True
    assert served["rule"] == lane.RULE_V1
    assert (served["min_remaining_hours"], served["max_remaining_hours"]) == (6.0, 10.0)
    assert served["hurdle"] == 0.08
    # The freshness window is the edge's own half-life, not a round number.
    assert served["envelope_fresh_seconds"] <= 300.0


def test_the_daemon_runs_the_lane_at_both_ends_of_the_cycle() -> None:
    import ast  # noqa: PLC0415

    source = (ROOT / "scripts" / "weather_basket_yes_daemon.py").read_text()
    tree = ast.parse(source)
    called = [
        node.func.id for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    ]
    assert called.count("execute_p1_tail_entries") == 1
    assert called.count("_tail_head_execute") == 1
    assert '"p1_tail_lane": p1_tail_receipt' in source
    assert '"p1_tail_lane_head": p1_tail_head_receipt' in source


def test_stuck_cycles_of_this_lane_are_settled_with_the_others() -> None:
    from bayes_basket_target import tiebreak_lane  # noqa: PLC0415

    assert "p1_tail_cycle_" in tiebreak_lane.LANE_CYCLE_PREFIXES


# ---------------------------------------------------------------------------
# Charter 166 (2026-09-21): the lane looks where P1 is written.


def test_the_lane_runs_at_every_moment_p1_is_written() -> None:
    """Run from the daemon alone the lane looked twice a cycle, and a cycle
    is five to ten minutes: 80% of its first 56 evaluations were refused
    ``envelope_stale`` against a five-minute window.  The three places P1
    is committed each call it."""

    import ast  # noqa: PLC0415

    import bayes_basket_target.production_worker as pw  # noqa: PLC0415

    tree = ast.parse(Path(pw.__file__).read_text())
    reasons = sorted(
        node.args[0].value for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_p1_tail_after_p1" and node.args
        and isinstance(node.args[0], ast.Constant)
    )
    assert reasons == ["early_p1", "probability_chain"]
    # ... and the checkpoint's, which names the label it ran under.
    templated = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_p1_tail_after_p1" and node.args
        and isinstance(node.args[0], ast.JoinedStr)
    ]
    assert len(templated) == 1
    for stage in ("p1_tail_after_early_p1", "p1_tail_after_chain"):
        assert pw.STAGE_TIER[stage] == pw.STAGE_TIER_R0_DECISION


def test_a_lane_failure_is_a_receipt_and_never_stops_the_cycle(monkeypatch) -> None:
    import bayes_basket_target.production_worker as pw  # noqa: PLC0415

    worker = object.__new__(pw.TargetProductionWorker)
    worker.commit_conn = object()
    worker._stage_timings_ms = {}
    monkeypatch.setattr(pw, "PRODUCTION_EXECUTION_MODE", "production_live")
    monkeypatch.setattr(pw.TargetProductionWorker, "_add_span",
                        lambda self, name, ms, label="": None, raising=False)
    monkeypatch.setattr(lane, "execute_p1_tail_entries",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("venue down")))
    receipt = worker._p1_tail_after_p1("checkpoint:after_regime")
    assert receipt["ok"] is False and "venue down" in receipt["error"]
    assert receipt["reason"] == "checkpoint:after_regime"
    assert worker._p1_tail_receipts == [receipt]


def test_a_paused_lane_or_a_no_live_mode_costs_nothing(monkeypatch) -> None:
    import bayes_basket_target.production_worker as pw  # noqa: PLC0415

    worker = object.__new__(pw.TargetProductionWorker)
    worker.commit_conn = object()
    monkeypatch.setattr(lane, "execute_p1_tail_entries",
                        lambda *a, **k: pytest.fail("the lane must not run"))
    monkeypatch.setattr(pw, "PRODUCTION_EXECUTION_MODE", "production_no_live")
    assert worker._p1_tail_after_p1("early_p1") is None
    monkeypatch.setattr(pw, "PRODUCTION_EXECUTION_MODE", "production_live")
    from bayes_basket_target import strategy_lanes  # noqa: PLC0415

    monkeypatch.setattr(strategy_lanes, "lane_enabled", lambda name, path=None: False)
    assert worker._p1_tail_after_p1("early_p1") is None


# ---------------------------------------------------------------------------
# Charter 166 (2026-09-21): what the venue taught, in two sends.


def test_a_take_never_asks_for_the_whole_top_level() -> None:
    """KMIA 2026-09-20 88-89°F: the snapshot showed exactly 8 shares, the
    plan asked for all 8, and the FOK was killed -- the book was 171
    seconds old and part of the level had gone."""

    row = {"bucket": "x", "ask_available": True,
           "ask_levels": [{"price": 0.59, "shares": 8.0}], "bid_levels": []}
    take = lane.taker_take(row, 5.0, tick_size=0.01, min_shares=1.0)
    assert take["shares"] < 8.0 and take["refusal"] is None
    # A deep level gives up a fifth, a thin one a whole share.
    deep = lane.taker_take(
        {**row, "ask_levels": [{"price": 0.59, "shares": 40.0}]},
        5.0, tick_size=0.01, min_shares=1.0,
    )
    assert deep["shares"] == pytest.approx(8.0)   # the stake binds, not depth
    thin = lane.taker_take(
        {**row, "ask_levels": [{"price": 0.59, "shares": 5.5}]},
        5.0, tick_size=0.01, min_shares=1.0,
    )
    assert thin["shares"] == pytest.approx(4.0)


def test_a_stale_book_is_scored_but_never_bought(store) -> None:
    """The belief was 24 seconds old and the ladder 171.  One of those is
    allowed to be old."""

    _seed(store)
    planned = _plan(store, settings={
        **P1_TAIL_DEFAULT_SETTINGS, "send_book_fresh_seconds": 1.0,
    })
    assert planned["plans"] == []
    [row] = planned["evaluations"]
    assert row["refusal"].startswith("send_book_stale:")
    # The reading is still on the row: the evidence a later fit reads does
    # not depend on whether the order could be sent.
    stored = store.execute(
        f"SELECT best_gap, fired FROM {lane.EVALUATION_TABLE}"
    ).fetchone()
    assert stored[0] is not None and stored[1] == 0


def test_the_two_book_bounds_are_separate_dials() -> None:
    from bayes_basket_target import strategy_lanes  # noqa: PLC0415

    served = strategy_lanes.lane_settings(strategy_lanes.LANE_P1_TAIL)
    assert served["send_book_fresh_seconds"] <= 60.0
    assert served["book_fresh_seconds"] > served["send_book_fresh_seconds"]


def test_the_walk_is_paid_for_by_a_stricter_hurdle(store) -> None:
    """The reading is on the ask; the SEND has to clear the price it may
    actually pay.  Three live FOKs died on a top level too thin to fill."""

    _seed(store)
    # Belief on 27°C is ~0.70 against an ask of 0.46: a gap of 0.24 on the
    # ask and 0.23 one tick up.  A hurdle between the two fires on the ask
    # and refuses on the walk.
    planned = _plan(store, settings={**P1_TAIL_DEFAULT_SETTINGS, "hurdle": 0.235})
    assert planned["plans"] == []
    refusals = [e["refusal"] for e in planned["evaluations"] if e["bucket"] == "27°C"]
    assert refusals and refusals[0].startswith("hurdle_after_walk:")


def test_the_size_is_the_depth_up_to_the_authorised_limit() -> None:
    """KLGA 2026-09-20 68-69°F: 7.68 shares at 0.72 and 12.56 at 0.73.  Held
    to the ask the take asks for 6 of 7.68 and dies; one tick up it is 6 of
    20.24."""

    row = {"bucket": "x", "ask_available": True, "bid_levels": [],
           "ask_levels": [{"price": 0.72, "shares": 7.68},
                          {"price": 0.73, "shares": 12.56},
                          {"price": 0.74, "shares": 149.41}]}
    held = lane.taker_take(row, 5.0, tick_size=0.01, min_shares=1.0, walk_ticks=0)
    walked = lane.taker_take(row, 5.0, tick_size=0.01, min_shares=1.0, walk_ticks=1)
    assert held["walked_depth_shares"] == pytest.approx(7.68)
    assert walked["walked_depth_shares"] == pytest.approx(20.24)
    assert walked["limit_price"] == pytest.approx(0.73)
    assert walked["cost_usdc"] <= 5.0 + 1e-9
    # A walk that would price the take at or above a dollar is not taken.
    top = {"bucket": "x", "ask_available": True, "bid_levels": [],
           "ask_levels": [{"price": 0.99, "shares": 50.0}]}
    assert lane.taker_take(
        top, 5.0, tick_size=0.01, min_shares=1.0, walk_ticks=1
    )["limit_price"] == pytest.approx(0.99)
