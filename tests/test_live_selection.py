"""The board's selection readings off production's decisions and live fills.

User ruling 2026-09-16: the paper ledger is a rule comparator, so the top
strip's 选桶 row, 逐站汇总（跨日）, 没建仓是因为什么, 覆盖 · 价格 · EV and
篮子命中率 read the live book.  These pin what each reading counts, on a
synthetic book, and that the board no longer hands those sections the paper
selection payload.
"""

from __future__ import annotations

import ast
import json
import re
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import live_book  # noqa: E402
import live_selection as LS  # noqa: E402


def _leg(intent, decision, station, day, stage, bucket, *, filled=0.0,
         notional=0.0, state="cancelled", claim=0.3, at="2026-09-10T01:00Z"):
    return {
        "order_intent_id": intent, "decision_id": decision,
        "station_id": station, "event_date": day, "stage": stage,
        "decision_at_utc": at, "bucket": bucket, "limit_price": 0.2,
        "requested_size": 5.0, "requested_notional_usdc": 1.0,
        "state": state, "created_at": at,
        "filled_size": filled, "filled_notional_usdc": notional,
        "claimed_probability": claim, "center_c": None,
    }


def _book(legs, settled):
    return {
        "legs": legs,
        "settled": settled,
        "settlements": live_book.settlement_rows(legs, settled),
    }


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE basket_policy_training_fact (station_id TEXT,"
        " event_date TEXT, stage TEXT, decision_at_utc TEXT, decision_id TEXT,"
        " probability_envelope_id TEXT, feature_json TEXT)"
    )
    conn.execute(
        "CREATE TABLE basket_probability_envelope (envelope_id TEXT,"
        " bucket_ordinals_json TEXT)"
    )
    conn.execute(
        "CREATE TABLE basket_target_order_events (order_intent_id TEXT,"
        " event_type TEXT, event_json TEXT)"
    )
    return conn


def _fact(conn, station, day, stage, at, decision, *, state="HOLD",
          codes=(), seated=None, ladder=None, market=None, envelope=""):
    plan = {"state": state, "reason_codes": list(codes),
            "basket_support_selection": {"seated": seated,
                                         "ladder_center_bucket": ladder},
            "market_center_ordinal": market}
    conn.execute(
        "INSERT INTO basket_policy_training_fact VALUES (?,?,?,?,?,?,?)",
        (station, day, stage, at, decision, envelope,
         json.dumps({"plan": plan})),
    )


def test_a_day_the_send_gate_refused_whole_is_not_a_chosen_basket() -> None:
    legs = [
        # KMIA: posted two, one filled, the other bucket won.
        _leg("i1", "d1", "KMIA", "2026-09-10", "P0", "30", filled=5, notional=1,
             state="filled", claim=0.4),
        _leg("i2", "d1", "KMIA", "2026-09-10", "P0", "31", claim=0.2),
        # EGLC: every intent refused before any market saw it.
        _leg("i3", "d2", "EGLC", "2026-09-10", "P0", "20", state="failed"),
    ]
    settled = {("KMIA", "2026-09-10"): ("31", "truth"),
               ("EGLC", "2026-09-10"): ("20", "truth")}
    chains = LS.settled_chains(_book(legs, settled))
    assert [c["station_id"] for c in chains] == ["KMIA"]
    chain = chains[0]
    assert chain["chosen"] == {"30", "31"}
    assert chain["bought"] == {"30"}
    assert chain["landed"] == "31"
    assert abs(chain["claimed"] - 0.6) < 1e-9


def test_the_chosen_claim_reads_the_newest_decisions_envelope() -> None:
    """Newest-leg-per-bucket added two envelopes and passed 1.

    d1 posted 30 (its envelope: 30 at 0.6, 31 at 0.3); d2 later posted 31
    (30 at 0.3, 31 at 0.65).  Added bucket by bucket that is 0.6 + 0.65 =
    1.25; on d2's envelope, the newest, the chosen pair is 0.95.
    """

    e1 = {"30": 0.6, "31": 0.3}
    e2 = {"30": 0.3, "31": 0.65}
    # The later decision's id sorts first: only its instant says it is newer.
    legs = [
        dict(_leg("i1", "d9", "KMIA", "2026-09-10", "P0", "30", filled=5,
                  notional=1, state="filled", claim=0.6),
             decision_probabilities=e1),
        dict(_leg("i2", "d1", "KMIA", "2026-09-10", "P0", "31", claim=0.65,
                  at="2026-09-10T03:00Z"),
             decision_probabilities=e2),
    ]
    (chain,) = LS.settled_chains(
        _book(legs, {("KMIA", "2026-09-10"): ("31", "truth")})
    )
    assert chain["claimed"] == pytest.approx(0.95)


def test_basket_width_is_the_buckets_that_filled() -> None:
    legs = [
        _leg("i1", "d1", "KMIA", "2026-09-10", "P0", "30", filled=5, notional=1,
             state="filled", claim=0.4),
        _leg("i2", "d1", "KMIA", "2026-09-10", "P0", "31", filled=5, notional=1,
             state="filled", claim=0.3),
        _leg("i3", "d2", "KLGA", "2026-09-10", "P0", "70", filled=5, notional=1,
             state="filled", claim=0.3),
    ]
    settled = {("KMIA", "2026-09-10"): ("31", "truth"),
               ("KLGA", "2026-09-10"): ("72", "truth")}
    rows = {r["window_size"]: r for r in LS.basket_hit_by_width(_book(legs, settled))}
    assert rows[2]["settled_count"] == 1 and rows[2]["hit_count"] == 1
    assert rows[1]["settled_count"] == 1 and rows[1]["hit_count"] == 0
    assert abs(rows[2]["pnl_usdc"] - (5 - 2)) < 1e-9


def test_the_refusal_funnel_attributes_in_order_at_p0_station_days() -> None:
    conn = _conn()
    day = "2026-09-10"
    legs = [
        _leg("a", "dA", "AAAA", day, "P0", "1", filled=5, notional=1,
             state="filled"),
        _leg("b", "dB", "BBBB", day, "P0", "1"),                  # reached
        _leg("c", "dC", "CCCC", day, "P0", "1", state="failed"),  # refused
    ]
    conn.execute(
        "INSERT INTO basket_target_order_events VALUES (?,?,?)",
        ("c", "NEVER_REACHED_EXCHANGE", json.dumps({"reason": "live_station_barred"})),
    )
    for station in ("AAAA", "BBBB", "CCCC"):
        _fact(conn, station, day, "P0", "2026-09-09T01:00Z", "d" + station[0],
              state="TRADE")
    _fact(conn, "DDDD", day, "P0", "2026-09-09T01:00Z", "dD", state="TRADE")
    # EEEE held twice; the newest plan names the floor and the edge -- the
    # floor wins -- while the older one only said "not a decision moment".
    _fact(conn, "EEEE", day, "P0", "2026-09-09T01:00Z", "dE1",
          codes=["no_decision_moment"])
    _fact(conn, "EEEE", day, "P0", "2026-09-09T02:00Z", "dE2",
          codes=["support_edge_after_lift", "support_floor_after_lift"])
    _fact(conn, "FFFF", day, "P0", "2026-09-09T01:00Z", "dF",
          codes=["delta_v2_complete_basket_hold_selected"])
    # A P1 decision on an unopened day is not a second station-day.
    _fact(conn, "GGGG", day, "P1", "2026-09-10T01:00Z", "dG",
          codes=["chain_not_opened_by_p0"])
    # Before the live book's first day: out of the window.
    _fact(conn, "HHHH", "2026-09-01", "P0", "2026-08-31T01:00Z", "dH",
          codes=["support_edge_after_lift"])
    funnel = LS.refusal_funnel(conn, _book(legs, {}))
    assert funnel["since_event_date"] == day
    stage = funnel["stages"][0]
    assert stage["stage"] == "P0"
    assert stage["scope_count"] == 6
    assert stage["seated_count"] == 1
    reasons = {r["reason"]: r["count"] for r in stage["reasons"]}
    assert reasons == {
        "live_unfilled": 1,
        "live_station_barred": 1,
        "traded_without_live_intent": 1,
        "hold_support_floor": 1,
        "hold_solver_stood_still": 1,
    }
    assert stage["scope_count"] == stage["seated_count"] + sum(reasons.values())


def test_the_seat_side_is_productions_two_centres_on_envelope_ordinals() -> None:
    conn = _conn()
    conn.execute(
        "INSERT INTO basket_probability_envelope VALUES (?,?)",
        ("env1", json.dumps({"20": 0, "21": 1, "22": 2})),
    )
    seated = {"buckets": ["21", "22"], "all_in_price": 0.8,
              "win_probability": 0.9}
    _fact(conn, "EPWA", "2026-09-18", "P0", "2026-09-17T01:00Z", "d1",
          state="TRADE", seated=seated, ladder="22", market=1, envelope="env1")
    legs = [_leg("i1", "d1", "EPWA", "2026-09-18", "P0", "21")]
    seats = LS.decision_seats(conn, _book(legs, {}))
    assert seats["d1"]["side"] == "above"
    assert seats["d1"]["buckets"] == ["21", "22"]
    account = LS._seat_account(list(seats.values()))
    assert abs(account["ev_per_usdc_mean"] - (0.9 / 0.8 - 1)) < 1e-9


def test_the_board_counts_station_day_verdicts_from_the_live_book_only() -> None:
    """每日统计 reads one decision of a day's verdict.

    It used to be free to compute 篮子命中/持仓命中 on its own; the verdict
    lives in ``live_book.station_days`` and the caller may not restate the
    membership test.  (The go-live table's per-station hit columns left the
    board on 2026-10-05.)
    """

    source = (ROOT / "scripts" / "calibration_dashboard.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    days = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "collect_live_days"
    )
    calls = {
        ast.unparse(node.func) for node in ast.walk(days)
        if isinstance(node, ast.Call)
    }
    assert "live_book.station_days" in calls
    assert "live_book.stage_summary" not in calls


def test_the_funnel_names_each_station_day_it_counted_as_not_built() -> None:
    conn = _conn()
    day = "2026-09-10"
    legs = [_leg("a", "dA", "AAAA", day, "P0", "1", filled=5, notional=1,
                 state="filled")]
    _fact(conn, "AAAA", day, "P0", "2026-09-09T01:00Z", "dA", state="TRADE")
    _fact(conn, "BBBB", day, "P0", "2026-09-09T01:00Z", "dB",
          codes=["support_edge_after_lift"])
    funnel = LS.refusal_funnel(conn, _book(legs, {}))
    assert funnel["station_days"] == [{
        "station_id": "BBBB", "event_date": day,
        "reason": "hold_edge_after_lift",
        "label": "抬到一手后整篮边际不正（宪章 123）",
    }]


def _template_function(template: str, name: str) -> str | None:
    match = re.search(
        r"\nfunction " + name + r"\(\)\{(.*?)\n\}\n", template, re.S
    )
    return match.group(1) if match else None


def test_the_board_hands_these_sections_the_live_payload_only() -> None:
    source = (ROOT / "scripts" / "calibration_dashboard.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    fn = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_build_payload"
    )
    returned = next(
        node for node in reversed(fn.body) if isinstance(node, ast.Return)
    )
    keys = {k.value for k in returned.value.keys if isinstance(k, ast.Constant)}
    assert "live_selection" in keys
    # The alignment guard left the page (user ruling, 2026-09-16).
    assert "ledger_alignment" not in keys
    # The paper selection payload is no longer built for the page.
    assert "build_selection_payload" not in source
    template = source[source.index('HTML_TEMPLATE = r"""'):]
    for name in ("overviewLamps", "liveReadiness"):
        body = _template_function(template, name)
        assert body is not None, name
        assert "D.selection" not in body, name
    # liveReadiness reads no selection payload since 2026-10-05 (its lanes'
    # money is the readiness rows'); overviewLamps still reads the live one.
    assert "D.live_selection" in _template_function(template, "overviewLamps")
    # 逐站汇总（跨日） merged into the go-live table, 没建仓是因为什么 moved
    # into 逐站逐日明细: neither is a section of its own any more.  The
    # basket's refusal, coverage and hit-rate sections -- and the roster's
    # why-not-built rows -- left the page on 2026-09-26 with the basket's
    # selection readings (user: 「全面检查旧的内容」「整区删除」).
    for gone in ("stationSummary", "ledgerAlignment", "refusalStats",
                 "coverage", "basketHit"):
        assert _template_function(template, gone) is None, gone
    assert "D.live_selection" not in (_template_function(template, "roster") or "")
    top = template[template.index("const TOP = "):]
    top = top[: top.index("];") + 2]
    assert "refusalStats" not in top and "stationSummary" not in top
    assert "paper_roi" not in template and "paper_chains" not in template


def test_the_funnel_on_kept_closed_days_is_the_funnel_on_every_fact() -> None:
    """Closed days from ``closed_plan_summaries``, later ones read: same funnel.

    The board keeps the summaries of days that take no more opening
    decisions, so a render reads feature_json (~63 KB a fact) for the open
    days only.  Whatever it keeps, the attribution must not move.
    """

    conn = _conn()
    closed_day, open_day = "2026-09-10", "2026-09-11"
    legs = [
        _leg("a", "dA", "AAAA", closed_day, "P0", "1", filled=5, notional=1,
             state="filled"),
        _leg("c", "dC", "CCCC", open_day, "P0", "1", state="failed"),
    ]
    conn.execute(
        "INSERT INTO basket_target_order_events VALUES (?,?,?)",
        ("c", "NEVER_REACHED_EXCHANGE", json.dumps({"reason": "live_station_barred"})),
    )
    for day in (closed_day, open_day):
        _fact(conn, "AAAA", day, "P0", day + "T00:00Z", "dA", state="TRADE")
        _fact(conn, "CCCC", day, "P0", day + "T00:00Z", "dC", state="TRADE")
        _fact(conn, "DDDD", day, "P0", day + "T00:00Z", "dD", state="TRADE")
        _fact(conn, "EEEE", day, "P0", day + "T01:00Z", "dE1",
              codes=["no_decision_moment"])
        _fact(conn, "EEEE", day, "P0", day + "T02:00Z", "dE2",
              codes=["support_edge_after_lift", "support_floor_after_lift"])
        _fact(conn, "FFFF", day, "P0", day + "T01:00Z", "dF",
              codes=["delta_v2_complete_basket_hold_selected"])
    book = _book(legs, {})
    whole = LS.refusal_funnel(conn, book)
    kept = LS.closed_plan_summaries(conn, closed_day, closed_day)
    assert LS.refusal_funnel(conn, book, kept) == whole
    # Kept summaries that start after the book's first day are not used.
    late = LS.closed_plan_summaries(conn, open_day, open_day)
    assert LS.refusal_funnel(conn, book, late) == whole
    # And the kept days are really not read again.
    conn.execute(
        "UPDATE basket_policy_training_fact SET feature_json='{}'"
        " WHERE event_date=?", (closed_day,),
    )
    assert LS.refusal_funnel(conn, book, kept) == whole
