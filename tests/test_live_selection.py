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


def test_the_refusal_funnel_is_gone() -> None:
    # User 2026-10-22 「不要“拒单漏斗”，删掉」: no front-end read it since the
    # 10-05 merge into 盈亏日历, yet every render computed it and kept its
    # closed-day cache.
    for name in ("refusal_funnel", "closed_plan_summaries", "_plan_summaries",
                 "_PLAN_FACTS_SQL", "_gate_refusals", "HOLD_REASONS"):
        assert not hasattr(LS, name), name
    source = (ROOT / "scripts" / "calibration_dashboard.py").read_text(encoding="utf-8")
    assert "refusal_history" not in source.replace("refusal_history (since retired)", "")
