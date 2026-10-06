"""Charter 251: the 远尾做空线 takes a far tail only where the model, too, calls the NO cheap.

User ruling 2026-10-04 「改规则，把模型当过滤器」: on top of charter 234's
market-only rule, a leg is taken only where the station-day's newest P0
envelope -- inside its own validity, floored as the Proper lane floors it --
puts the bucket under the YES best bid.  No readable p refuses the leg.
Rule v1 (the market-only lane) stays selectable.
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

from bayes_basket_target import proper_bet_lane  # noqa: E402
from bayes_basket_target import strategy_lanes  # noqa: E402
from bayes_basket_target import tail_short_lane as lane  # noqa: E402
import tests.test_charter_234_tail_short_lane as tails  # noqa: E402
from tests.test_charter_234_tail_short_lane import _cash, _fill_transport, _intents, _seed  # noqa: E402,F401
from tests.test_tiebreak_lane import ADMIT, ST, TZ, _insert, store  # noqa: E402,F401

ED = tails.ED

V2 = {"live_ws_book": False, "rule": lane.RULE_V2}
#: The model over charter 234's test ladder: the far-tail legs are 24°C (bid
#: 2c), 25°C (bid 3c) and 31°C (bid 3c).
MODEL = {"24°C": 0.01, "25°C": 0.02, "26°C": 0.05, "27°C": 0.20, "28°C": 0.35,
         "29°C": 0.22, "30°C": 0.08, "31°C": 0.05, "32°C": 0.02}


def _envelope(conn, *, envelope_id="env_f1", model=None, fresh_until="2026-09-18T13:00:00Z",
              decided=tails.DECIDED):
    _insert(conn, "basket_probability_envelope", {
        "envelope_id": envelope_id, "stage": "P0", "checkpoint_id": f"cp_{envelope_id}", "station_id": ST,
        "event_date": ED, "settlement_timezone": TZ, "decision_at_utc": decided,
        "fresh_until_utc": fresh_until, "bucket_ordinals_json": json.dumps({b: i for i, b in enumerate(MODEL)}),
        "probabilities_json": json.dumps(model or MODEL), "content_sha256": (envelope_id * 64)[:64],
        "state": "COMMITTED", "created_at": decided, "lineage_json": "{}",
    })


def _run(conn, sent, *, now=tails.NOW, **overrides):
    return lane.execute_tail_short(
        conn, transport=_fill_transport(sent), now_utc=now, live_submit_authorized=True,
        settings={**strategy_lanes.TAIL_SHORT_DEFAULT_SETTINGS, **V2, **overrides}, admission=ADMIT,
    )


def _evaluations(conn):
    return {
        bucket: (refusal, model_p, envelope)
        for bucket, refusal, model_p, envelope in conn.execute(
            f"SELECT bucket, refusal, model_p, model_envelope_id FROM {lane.EVALUATION_TABLE} ORDER BY evaluated_at_utc"
        )
    }


# ---------------------------------------------------------------------------
# The filter on its own.


def test_the_filter_passes_only_p_under_the_bid() -> None:
    served = {"state": "ok", "probabilities": {"a": 0.01, "b": 0.03, "c": 0.05}}
    assert lane.model_filter(served, "a", 0.02) == {"p": 0.01, "refusal": None}
    # Equal is not under: the model must call the NO cheap, not fair.
    assert lane.model_filter(served, "b", 0.03)["refusal"] == lane.MODEL_NOT_BELOW_BID
    assert lane.model_filter(served, "c", 0.03)["refusal"] == lane.MODEL_NOT_BELOW_BID
    assert lane.model_filter(served, "z", 0.03)["refusal"] == "model_unavailable:bucket_missing"
    assert lane.model_filter({"state": "envelope_expired"}, "a", 0.02)["refusal"] == "model_unavailable:envelope_expired"


# ---------------------------------------------------------------------------
# The lane end to end.


def test_v2_takes_only_the_legs_the_model_calls_cheap(store) -> None:
    _seed(store)
    _envelope(store)
    sent: list = []
    receipt = _run(store, sent)
    assert receipt["rule"] == "tail_short_lane_v2"
    # 24°C: p 1% under a 2c bid; 25°C: 2% under 3c; 31°C: 5% over 3c.
    assert [r[0] for r in _intents(store)] == ["24°C", "25°C"]
    assert receipt["submitted"] == 2 and receipt["filled"] == 2
    rows = _evaluations(store)
    assert rows["31°C"][0] == lane.MODEL_NOT_BELOW_BID and rows["31°C"][1] == pytest.approx(0.05)
    assert rows["24°C"][0] is None and rows["24°C"][1] == pytest.approx(0.01) and rows["24°C"][2] == "env_f1"


def test_the_send_is_bound_to_the_envelope_it_read(store) -> None:
    _seed(store)
    _envelope(store)
    _run(store, [])
    intent = json.loads(_intents(store)[0][6])
    plan = intent["plan"]
    assert plan["model_envelope_id"] == "env_f1" and plan["model_p"] == pytest.approx(0.01)
    details = json.loads(store.execute(
        "SELECT details_json FROM auto_trade_decisions WHERE decision_id IN"
        " (SELECT decision_id FROM basket_target_order_intents LIMIT 1)"
    ).fetchone()[0])
    binding = details["forward_evidence_binding"]
    assert binding["probability_envelope_id"] == "env_f1"
    assert binding["probability_artifact_sha256"] == ("env_f1" * 64)[:64]
    assert binding["probability_decision_at_utc"] == tails.DECIDED


@pytest.mark.parametrize("case, why", [
    ("missing", "envelope_unreadable"),
    ("expired", "envelope_expired"),
    ("bucket", "bucket_missing"),
])
def test_no_served_p_refuses_every_leg(store, case, why) -> None:
    _seed(store)
    if case == "expired":
        _envelope(store, fresh_until="2026-09-18T12:00:00Z")
    elif case == "bucket":
        _envelope(store, model={b: v for b, v in MODEL.items() if b not in {"24°C", "25°C", "31°C"}})
    sent: list = []
    receipt = _run(store, sent)
    assert receipt["planned"] == 0 and sent == []
    assert {r[0] for r in _evaluations(store).values()} == {f"{lane.MODEL_UNAVAILABLE}:{why}"}


def _newer_p0(store, model, *, fresh_until="2026-09-18T13:00:00Z"):
    later, seen = "2026-09-18T12:30:00.000Z", "2026-09-18T12:30:30.000Z"
    _envelope(store, envelope_id="env_f2", model=model, decided=later, fresh_until=fresh_until)
    _insert(store, "basket_official_ws_snapshot", {
        "snapshot_id": "snap2", "market_id": "m", "station_id": ST, "event_date": ED,
        "settlement_timezone": TZ, "sequence": 2, "observed_at": seen, "quote_version": "v",
        "quote_observed_at": seen, "quote_expires_at": "2026-09-18T13:00:00Z",
        "quote_source": "OFFICIAL_CLOB_WS", "remaining_budget_usdc": 100.0,
        "buckets_json": json.dumps(tails._book()), "state": "COMMITTED", "created_at": seen,
    })
    _insert(store, "basket_policy_training_fact", {
        "fact_id": "f2", "station_id": ST, "event_date": ED, "settlement_timezone": TZ,
        "decision_at_utc": later, "decision_scope_key": "scope_f2", "captured_at_utc": later,
        "source_sequence": 2, "quote_observed_at": later, "quote_expires_at": "2026-09-18T13:00:00Z",
        "stage": "P0", "probability_envelope_id": "env_f2", "ws_snapshot_id": "snap2",
        "intent_count": 0, "authority_scope": "PRODUCTION_NO_LIVE_FORWARD", "created_at_utc": later,
        "cycle_id": "cyc_f2", "decision_id": "dec_f2",
    })
    return "2026-09-18T12:30:40.000Z"


def test_a_bucket_the_model_refused_stays_out_for_the_day(store) -> None:
    # ZSPD 10-03 22°C: 3.26% against a 3c bid when first sendable, 1.6% a run
    # later; the filter judges once, so the dip does not admit it.
    _seed(store)
    _envelope(store)
    _run(store, [])
    assert [r[0] for r in _intents(store)] == ["24°C", "25°C"]
    now = _newer_p0(store, {**MODEL, "31°C": 0.01, "28°C": 0.39})
    receipt = _run(store, [], now=now)
    assert [r[0] for r in _intents(store)] == ["24°C", "25°C"]
    assert receipt["skipped"].get("model_barred") == 1


def test_an_unreadable_model_refuses_without_barring(store) -> None:
    _seed(store)
    _envelope(store, fresh_until="2026-09-18T12:00:00Z")      # expired at the first look
    _run(store, [])
    assert _intents(store) == []
    now = _newer_p0(store, MODEL)
    _run(store, [], now=now)
    assert [r[0] for r in _intents(store)] == ["24°C", "25°C"]


def test_the_model_is_judged_where_the_leg_is_first_sendable(store) -> None:
    # A bid too thin to send is refused on depth before the model is asked, so
    # a model "no" read then does not bar the bucket.
    _seed(store, book=tails._book(bid_size=3.0))
    _envelope(store, model={**MODEL, "24°C": 0.05, "28°C": 0.31})
    _run(store, [])
    first = {b: r for b, (r, _p, _e) in _evaluations(store).items()}
    assert first["24°C"].startswith("thin_bid") and lane.MODEL_NOT_BELOW_BID not in first.values()
    now = _newer_p0(store, MODEL)
    _run(store, [], now=now)
    assert [r[0] for r in _intents(store)] == ["24°C", "25°C"]


def test_v1_does_not_read_the_model(store) -> None:
    _seed(store)  # no envelope at all
    sent: list = []
    receipt = _run(store, sent, rule=lane.RULE_V1)
    assert receipt["rule"] == "tail_short_lane_v1" and receipt["filled"] == 3
    assert all(r[1] is None for r in _evaluations(store).values())


def test_an_older_table_gains_the_model_columns() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(lane.EVALUATION_DDL.replace(", model_p REAL, model_envelope_id TEXT", "")
                 .replace(" send_json TEXT, model_p REAL, model_envelope_id TEXT", " send_json TEXT"))
    old = {r[1] for r in conn.execute(f"PRAGMA table_info({lane.EVALUATION_TABLE})")}
    assert "model_p" not in old
    lane.ensure_lane_schema(conn)
    lane.ensure_lane_schema(conn)
    new = {r[1] for r in conn.execute(f"PRAGMA table_info({lane.EVALUATION_TABLE})")}
    assert {"model_p", "model_envelope_id"} <= new


def test_proper_reads_the_same_served_vector(store) -> None:
    _seed(store)
    _envelope(store)
    served = proper_bet_lane.served_p0_vector(store, ST, ED)
    assert served["state"] == "ok" and served["envelope_id"] == "env_f1"
    value, read = proper_bet_lane._latest_p0_probability(store, ST, ED, "31°C")
    assert value == pytest.approx(served["probabilities"]["31°C"])
    assert read["p_envelope_id"] == "env_f1"


# ---------------------------------------------------------------------------
# Shipped and written down.


def test_v2_stays_selectable() -> None:
    # Charter 251 (六) ships v3 (tests/test_charter_251_tail_short_centred_filter.py);
    # v2 stays the setting that restores this filter as it was.
    assert lane.RULE_VERSIONS[lane.RULE_V1] == "tail_short_lane_v1"
    assert lane.RULE_VERSIONS[lane.RULE_V2] == "tail_short_lane_v2"
    assert lane.RULE_V2 in lane.MODEL_FILTER_RULES and lane.RULE_V2 not in lane.CENTRED_FILTER_RULES


def test_the_charter_and_agents_carry_the_filter() -> None:
    charter = (ROOT / "docs" / "constitution" / "SYSTEM_CHARTER.md").read_text()
    clause = next(line for line in charter.splitlines() if line.startswith("251. "))
    assert "把模型当过滤器" in clause and lane.RULE_V2 in clause and "占号" not in clause
    agents = (ROOT / "AGENTS.md").read_text()
    assert "宪章 251" in agents
