"""The live book's own arithmetic, before there is a live book.

Every number in the board's 实盘订单 panel is unexercised code: the
reconciliation table is empty, so the first time this arithmetic runs for
real is the first time real money is on it.  Synthetic fills are the only
way to know it works at all, and the three things asserted here are the
three ways a live PnL panel lies: it counts the paper book, it scores an
unsettled day as a loss, or it reports 0% when it means "no sample".
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

_spec = importlib.util.spec_from_file_location(
    "calibration_dashboard", ROOT / "scripts" / "calibration_dashboard.py"
)
board = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(board)


def _book(rows: list[tuple], fills: list[tuple],
          settled: list[tuple],
          truth: list[tuple] | None = None,
          ladder: list[tuple] | None = None) -> sqlite3.Connection:
    """A book with just the columns the panel reads.

    ``truth`` and ``ladder`` populate the second source the panel resolves a
    settled bucket from: the truth lane and the station-day's own committed
    rungs.  Leaving them empty is the pre-2026-09-12 world, where a live
    basket had a verdict only if the paper book happened to seat the same
    station-day.
    """

    conn = sqlite3.connect(":memory:")
    conn.executescript(
        "CREATE TABLE basket_target_order_intents("
        " order_intent_id TEXT PRIMARY KEY, decision_id TEXT, cycle_id TEXT,"
        " bucket TEXT, side TEXT, limit_price REAL, requested_size REAL,"
        " requested_notional_usdc REAL, state TEXT,"
        " live_submit_authorized INTEGER, created_at TEXT,"
        " target_execution_hard_deadline_at TEXT);"
        "CREATE TABLE auto_trade_decisions("
        " decision_id TEXT PRIMARY KEY, station_id TEXT, event_date TEXT);"
        "CREATE TABLE basket_target_order_reconciliations("
        " order_intent_id TEXT PRIMARY KEY, state TEXT, filled_size REAL,"
        " filled_notional_usdc REAL, blocking_reason TEXT);"
        "CREATE TABLE paper_trade_settlement("
        " settlement_id INTEGER PRIMARY KEY, station_id TEXT,"
        " event_date TEXT, settled_bucket TEXT);"
        "CREATE TABLE calibration_station_daily_high_truth("
        " truth_id TEXT PRIMARY KEY, station_id TEXT, local_date TEXT,"
        " official_tmax_c REAL, revision_no INTEGER, content_sha256 TEXT);"
        "CREATE TABLE basket_probability_bridge_history("
        " history_id TEXT PRIMARY KEY, station_id TEXT, event_date TEXT,"
        " state TEXT, stage TEXT, decision_at_utc TEXT);"
        "CREATE TABLE basket_probability_bridge_bucket_probability("
        " history_id TEXT, bucket TEXT, ordinal INTEGER, lower_c REAL,"
        " upper_c REAL, probability REAL);"
    )
    for intent, decision, station, day, bucket, live in rows:
        conn.execute(
            "INSERT INTO basket_target_order_intents VALUES"
            " (?,?,?,?, 'YES', 0.3, 10, 3.0, 'prepared', ?,"
            " '2026-08-31T00:00:00Z', '2026-08-31T00:00:03.000Z')",
            (intent, decision, "cycle", bucket, live),
        )
        conn.execute(
            "INSERT OR IGNORE INTO auto_trade_decisions VALUES (?,?,?)",
            (decision, station, day),
        )
    for intent, size, notional in fills:
        conn.execute(
            "INSERT INTO basket_target_order_reconciliations VALUES"
            " (?, 'resolved_filled', ?, ?, NULL)",
            (intent, size, notional),
        )
    for index, (station, day, bucket) in enumerate(settled):
        conn.execute(
            "INSERT INTO paper_trade_settlement VALUES (?,?,?,?)",
            (index + 1, station, day, bucket),
        )
    for index, (station, day, tmax_c, revision) in enumerate(truth or []):
        conn.execute(
            "INSERT INTO calibration_station_daily_high_truth VALUES"
            " (?,?,?,?,?,?)",
            (f"t{index}", station, day, tmax_c, revision, f"{index:064d}"),
        )
    for index, (station, day, rungs) in enumerate(ladder or []):
        history = f"h{index}"
        conn.execute(
            "INSERT INTO basket_probability_bridge_history VALUES"
            " (?,?,?, 'COMMITTED', 'P0', ?)",
            (history, station, day, f"2026-08-30T0{index}:00:00Z"),
        )
        for ordinal, (bucket, low, high) in enumerate(rungs):
            conn.execute(
                "INSERT INTO basket_probability_bridge_bucket_probability"
                " VALUES (?,?,?,?,?, 0.1)",
                (history, bucket, ordinal, low, high),
            )
    conn.commit()
    return conn


# A three-rung Celsius day: below 30.5, 31, above 31.5.
_RUNGS = [("30°C or below", None, 30.5), ("31°C", 30.5, 31.5),
          ("32°C or above", 31.5, None)]


def test_no_fills_reports_no_sample_rather_than_zero_percent() -> None:
    """An empty live book has no ROI -- 0% would be a measured claim."""

    conn = _book(
        rows=[("i1", "d1", "KMIA", "2026-08-30", "31°C", 1)],
        fills=[], settled=[("KMIA", "2026-08-30", "31°C")],
    )
    pnl = board._collect_live_pnl(conn)
    assert pnl["available"] is False
    assert pnl["roi"] is None
    assert pnl["filled_legs"] == 0
    assert pnl["pnl_usdc"] == 0.0


def test_the_paper_book_never_reaches_the_live_numbers() -> None:
    """A filled row with live_submit_authorized=0 is not a live fill.

    Both books are settled winners here, so a collector that ignored the
    authorisation column would report a healthy live ROI off paper rows.
    """

    conn = _book(
        rows=[("live", "d1", "KMIA", "2026-08-30", "31°C", 1),
              ("paper", "d2", "KLGA", "2026-08-30", "24°C", 0)],
        fills=[("live", 10.0, 3.0), ("paper", 40.0, 12.0)],
        settled=[("KMIA", "2026-08-30", "31°C"),
                 ("KLGA", "2026-08-30", "24°C")],
    )
    pnl = board._collect_live_pnl(conn)
    assert pnl["filled_legs"] == 1
    assert pnl["cost_usdc"] == 3.0
    # Ten shares on the bucket truth landed in: ten USDC, less the three paid.
    assert pnl["payout_usdc"] == 10.0
    assert pnl["pnl_usdc"] == 7.0
    assert pnl["roi"] == round(7.0 / 3.0, 6)
    assert [r["station_id"] for r in pnl["by_station"]] == ["KMIA"]


def test_a_miss_is_a_loss_and_an_unsettled_day_is_neither() -> None:
    """Pending legs stay out of the ratio instead of reading as zero payout.

    Counting them as losses is what would make the panel look worse every
    afternoon and recover overnight, which is not a thing the book did.
    """

    conn = _book(
        rows=[("hit", "d1", "KMIA", "2026-08-30", "31°C", 1),
              ("miss", "d2", "KLGA", "2026-08-30", "24°C", 1),
              ("open", "d3", "EGLL", "2026-08-31", "19°C", 1)],
        fills=[("hit", 10.0, 3.0), ("miss", 10.0, 4.0), ("open", 10.0, 5.0)],
        settled=[("KMIA", "2026-08-30", "31°C"),
                 ("KLGA", "2026-08-30", "25°C")],
    )
    pnl = board._collect_live_pnl(conn)
    assert pnl["filled_legs"] == 2
    assert pnl["won_legs"] == 1
    assert pnl["pending_legs"] == 1
    # The unsettled five USDC is in neither the cost nor the payout.
    assert pnl["cost_usdc"] == 7.0
    assert pnl["payout_usdc"] == 10.0
    assert pnl["pnl_usdc"] == 3.0
    assert pnl["station_days"] == 2
    assert pnl["available"] is True


def test_the_intent_book_counts_only_the_live_rows() -> None:
    """Paper intents are a different book and stay out of every cut.

    Two of the three rows here are paper, and they are the two that would
    dominate every total if the filter were dropped -- so a regression
    shows up as numbers that are too big, not as an empty panel.
    """

    conn = _book(
        rows=[("live", "d1", "KMIA", "2026-08-30", "31°C", 1),
              ("p1", "d2", "KLGA", "2026-08-30", "24°C", 0),
              ("p2", "d3", "KLGA", "2026-08-29", "23°C", 0)],
        fills=[], settled=[],
    )
    book = board._collect_order_book(conn)
    assert book["total"] == 1
    assert book["paper_excluded"] == 2
    assert book["stations"] == 1
    assert book["notional_usdc"] == 3.0
    assert [r["event_date"] for r in book["by_day"]] == ["2026-08-30"]
    assert book["by_day"][0]["count"] == 1
    assert [r["station_id"] for r in book["by_station"]] == ["KMIA"]
    assert [r["state"] for r in book["by_state"]] == ["prepared"]
    assert book["by_state"][0]["count"] == 1


def test_an_all_paper_book_reports_zero_and_says_what_it_excluded() -> None:
    """The state the board is in today: nothing live, 1406 paper rows."""

    conn = _book(
        rows=[("p1", "d1", "KLGA", "2026-08-30", "24°C", 0),
              ("p2", "d2", "KLGA", "2026-08-29", "23°C", 0)],
        fills=[], settled=[],
    )
    book = board._collect_order_book(conn)
    assert book["total"] == 0
    assert book["paper_excluded"] == 2
    # No aggregates are invented for an empty book.
    assert "notional_usdc" not in book


def test_a_fail_closed_receipt_turns_the_production_light_red() -> None:
    """The shape that got past every check: no lanes, because it never ran.

    A worker that cannot open the SSOT publishes a receipt with no lane
    maps at all, no ``worker_checked_at``, and a ``cycle_total`` of a
    fraction of a millisecond -- so the lane scan found nothing blocked,
    the staleness check had nothing to read, and the slow-cycle check
    passed.  The card printed "周期正常产出" while the pipeline had
    produced no decision for nineteen hours.
    """

    receipt = {
        "production": {
            "ok": False,
            "state": "blocked_fail_closed",
            "reason": "target_ssot_read_only_open_failed:ResourceBoundError:"
                      "target_database_wal_checkpoint_busy",
            "execution_mode": "production_live",
            "orders_submitted": 0,
            "stage_timings_ms": {"cycle_total": 0.1},
        }
    }
    health = board._production_health(receipt)
    assert health["production_health"] == "red"
    assert health["production_state"] == "blocked_fail_closed"
    assert "fail-closed" in health["production_note"]
    assert "wal_checkpoint_busy" in health["production_note"]


def test_a_producing_receipt_is_still_green() -> None:
    """The fail-closed check must not swallow the ordinary healthy case."""

    receipt = {
        "production": {
            "ok": True,
            "state": "produced",
            "stage_timings_ms": {"cycle_total": 240_000},
        }
    }
    health = board._production_health(receipt)
    assert health["production_health"] == "green"
    assert health["production_note"] == "周期正常产出"


def test_cycle_verdict_counts_the_ledger_not_the_worst_scope() -> None:
    """One blocked scope must not paint a committed cycle fail-closed.

    The old aggregation was all-or-nothing: state went blocked_fail_closed
    unless every candidate was clean, over a fleet where some scopes are
    always in a soft per-station refusal (support_empty is HOLD semantics).
    On 2026-08-31 the first cycle ever to carry nine fully committed
    live-authority decisions rendered as an outage banner.

    The verdict now counts what the ledger says.  This drives the health
    function with the three shapes it distinguishes -- all committed, part
    committed, nothing committed -- through the real dashboard code.
    """
    import importlib.util as _ilu
    from pathlib import Path as _P

    spec = _ilu.spec_from_file_location(
        "calibration_dashboard_module",
        _P(__file__).resolve().parents[1] / "scripts" / "calibration_dashboard.py",
    )
    dash = _ilu.module_from_spec(spec)
    spec.loader.exec_module(dash)

    # Relative to the running clock, not a literal: worker_checked_at is
    # aged against "now", so a fixed stamp turns this test red the moment
    # real time moves three hours past it -- which it did, hours after the
    # test was written.
    from datetime import datetime as _dt, timezone as _tz

    now = (
        _dt.now(_tz.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )

    partial = dash._production_health({"production": {
        "state": "cycle_committed_partial",
        "ok": True,
        "committed_scope_count": 9,
        "blocked_scope_count": 5,
        "blocked_scope_reasons": {"support_empty": 3, "resident_p1_replay_required": 2},
        "worker_checked_at": now,
        "stage_timings_ms": {"cycle_total": 300000.0},
    }})
    assert partial["production_health"] == "amber"
    assert "9 个 scope 落账" in partial["production_note"]
    assert "support_empty×3" in partial["production_note"]

    clean = dash._production_health({"production": {
        "state": "cycle_committed",
        "ok": True,
        "committed_scope_count": 12,
        "blocked_scope_count": 0,
        "blocked_scope_reasons": {},
        "worker_checked_at": now,
        "stage_timings_ms": {"cycle_total": 300000.0},
    }})
    assert clean["production_health"] == "green"

    # Nothing committed is still an outage, and still red.
    dead = dash._production_health({"production": {
        "state": "blocked_fail_closed",
        "ok": False,
        "reason": "target_activation_invalid:RuntimeError:target_activation_state_drift",
        "worker_checked_at": now,
    }})
    assert dead["production_health"] == "red"
    assert "fail-closed" in dead["production_note"]


def test_the_legs_of_one_basket_carry_their_basket_identity() -> None:
    """Two legs of one decision must be reassemblable into one basket.

    The panel groups by decision because the decision *is* the basket: the
    solver sizes the whole support set as one joint action and the selector
    refuses any proper subset.  On 2026-08-31 a WMKK basket went out as
    31°C and 32°C -- one decision, two legs, one deadline -- and the flat
    leg table rendered them as two unrelated single-bucket orders, which
    reads as a system that buys buckets one at a time.  Carrying the
    station, the day and the window on every row is what lets the board
    put them back together.
    """

    conn = _book(
        rows=[("leg31", "d1", "WMKK", "2026-09-02", "31°C", 1),
              ("leg32", "d1", "WMKK", "2026-09-02", "32°C", 1),
              ("other", "d2", "KATL", "2026-08-31", "90-91°F", 1)],
        fills=[], settled=[],
    )
    live = board._collect_live_orders(conn)
    by_decision: dict[str, list[dict]] = {}
    for row in live["rows"]:
        by_decision.setdefault(row["decision_id"], []).append(row)
    assert sorted(by_decision) == ["d1", "d2"]
    assert len(by_decision["d1"]) == 2
    assert {r["bucket"] for r in by_decision["d1"]} == {"31°C", "32°C"}
    assert {r["station_id"] for r in by_decision["d1"]} == {"WMKK"}
    assert {r["event_date"] for r in by_decision["d1"]} == {"2026-09-02"}
    # The window the whole basket shares, not a per-leg allowance: both
    # legs go out serially inside this one.
    assert [r["execution_window_seconds"] for r in by_decision["d1"]] == [3.0, 3.0]


def test_an_intent_without_its_decision_row_still_renders() -> None:
    """A leg whose decision row is missing must not vanish or crash.

    The join is LEFT for this reason.  An intent that outlived its decision
    is exactly the row an operator needs to see, so it degrades to an
    unnamed basket rather than being filtered out of the count.
    """

    conn = _book(rows=[], fills=[], settled=[])
    conn.execute(
        "INSERT INTO basket_target_order_intents VALUES"
        " ('orphan','gone','cycle','31°C','YES',0.3,10,3.0,'prepared',1,"
        " '2026-08-31T00:00:00Z',NULL)"
    )
    conn.commit()
    live = board._collect_live_orders(conn)
    assert live["intent_count"] == 1
    row = live["rows"][0]
    assert row["station_id"] == ""
    assert row["event_date"] == ""
    # No deadline is unknown, not unlimited.
    assert row["execution_window_seconds"] is None


def test_a_day_the_paper_book_never_seated_still_settles() -> None:
    """The live verdict stops depending on a different book's seating rule.

    The paper ledger seats the station-days its own rule admits; the live
    lane mints intents for days it never seated.  On 2026-09-08 that was
    eleven live station-days against three paper settlements, while the
    truth lane held an ACCEPTED value for forty-eight stations -- so eight
    live baskets read 未结算 four days after the fact, and every one of
    their filled legs sat in ``pending`` keeping itself out of the ROI.
    """

    conn = _book(
        rows=[("hit", "d1", "KMIA", "2026-08-30", "31°C", 1)],
        fills=[("hit", 10.0, 3.0)],
        settled=[],
        truth=[("KMIA", "2026-08-30", 31.2, 0)],
        ladder=[("KMIA", "2026-08-30", _RUNGS)],
    )
    resolved = board._live_settled_buckets(conn, {("KMIA", "2026-08-30")})
    assert resolved == {("KMIA", "2026-08-30"): ("31°C", "truth_lane")}

    pnl = board._collect_live_pnl(conn)
    assert pnl["pending_legs"] == 0
    assert pnl["filled_legs"] == 1
    assert pnl["won_legs"] == 1
    assert pnl["roi"] == round(7.0 / 3.0, 6)


def test_the_settler_own_row_wins_and_says_so() -> None:
    """A day the paper book did seat is not re-derived, only reported.

    The label the production settler wrote is the one that stands, and the
    source travels with it: "the paper book settled this seat" and "the
    truth lane settled this day" are different claims and the panel has to
    be able to tell a reader which one it is showing.  The truth here would
    land on a different rung on purpose -- if precedence ever inverted,
    this row changes rather than staying quietly equal.
    """

    conn = _book(
        rows=[("hit", "d1", "KMIA", "2026-08-30", "31°C", 1)],
        fills=[("hit", 10.0, 3.0)],
        settled=[("KMIA", "2026-08-30", "31°C")],
        truth=[("KMIA", "2026-08-30", 35.0, 0)],
        ladder=[("KMIA", "2026-08-30", _RUNGS)],
    )
    resolved = board._live_settled_buckets(conn, {("KMIA", "2026-08-30")})
    assert resolved == {("KMIA", "2026-08-30"): ("31°C", "paper_ledger")}


def test_the_latest_truth_revision_is_the_one_that_settles() -> None:
    """Truth is revisable by design (charter 44) and the newest one stands."""

    conn = _book(
        rows=[("hit", "d1", "KMIA", "2026-08-30", "31°C", 1)],
        fills=[("hit", 10.0, 3.0)],
        settled=[],
        truth=[("KMIA", "2026-08-30", 29.0, 0),
               ("KMIA", "2026-08-30", 31.2, 1)],
        ladder=[("KMIA", "2026-08-30", _RUNGS)],
    )
    resolved = board._live_settled_buckets(conn, {("KMIA", "2026-08-30")})
    assert resolved[("KMIA", "2026-08-30")][0] == "31°C"


def test_a_truth_outside_the_ladder_leaves_the_day_open() -> None:
    """No rung contains it, so there is no bucket to name.

    The ladder here is bounded at both ends on purpose.  Naming a bucket
    the day was never priced against would be the audit inventing its own
    finding, which is the whole reason the grid rule is not restated here.
    """

    bounded = [("30°C", 29.5, 30.5), ("31°C", 30.5, 31.5)]
    conn = _book(
        rows=[("hit", "d1", "KMIA", "2026-08-30", "31°C", 1)],
        fills=[("hit", 10.0, 3.0)],
        settled=[],
        truth=[("KMIA", "2026-08-30", 40.0, 0)],
        ladder=[("KMIA", "2026-08-30", bounded)],
    )
    assert board._live_settled_buckets(conn, {("KMIA", "2026-08-30")}) == {}
    assert board._collect_live_pnl(conn)["pending_legs"] == 1


def test_a_day_with_no_truth_yet_is_still_pending() -> None:
    """Today's baskets have no value to score and must not acquire one."""

    conn = _book(
        rows=[("open", "d1", "KMIA", "2026-08-31", "31°C", 1)],
        fills=[("open", 10.0, 3.0)],
        settled=[],
        truth=[("KMIA", "2026-08-30", 31.2, 0)],
        ladder=[("KMIA", "2026-08-30", _RUNGS)],
    )
    assert board._live_settled_buckets(conn, {("KMIA", "2026-08-31")}) == {}
    assert board._collect_live_pnl(conn)["pending_legs"] == 1


def test_a_ledger_rebuild_no_longer_blanks_the_live_book() -> None:
    """Both paper tables sit empty for the better part of an hour.

    Every rule change re-derives the ledger's whole history, and while it
    runs ``paper_trade_position`` and ``paper_trade_settlement`` are empty.
    Reading the settlement table alone, the panel answered 未结算 for every
    date on the book and the ROI tile was computed over nothing -- a state
    it entered on 2026-09-12 within minutes of charter 131 landing.
    """

    conn = _book(
        rows=[("hit", "d1", "KMIA", "2026-08-30", "31°C", 1),
              ("miss", "d2", "KLGA", "2026-08-30", "31°C", 1)],
        fills=[("hit", 10.0, 3.0), ("miss", 10.0, 4.0)],
        settled=[],
        truth=[("KMIA", "2026-08-30", 31.2, 0),
               ("KLGA", "2026-08-30", 33.0, 0)],
        ladder=[("KMIA", "2026-08-30", _RUNGS),
                ("KLGA", "2026-08-30", _RUNGS)],
    )
    conn.execute("DELETE FROM paper_trade_settlement")
    pnl = board._collect_live_pnl(conn)
    assert pnl["pending_legs"] == 0
    assert pnl["filled_legs"] == 2
    assert pnl["won_legs"] == 1
    assert pnl["station_days"] == 2


def test_the_basket_listing_carries_the_source_of_its_verdict() -> None:
    """Each leg says which book settled its day, so the page can say so."""

    conn = _book(
        rows=[("borrowed", "d1", "KMIA", "2026-08-30", "31°C", 1),
              ("derived", "d2", "KLGA", "2026-08-30", "31°C", 1)],
        fills=[("borrowed", 10.0, 3.0), ("derived", 10.0, 3.0)],
        settled=[("KMIA", "2026-08-30", "31°C")],
        truth=[("KMIA", "2026-08-30", 31.2, 0),
               ("KLGA", "2026-08-30", 31.2, 0)],
        ladder=[("KMIA", "2026-08-30", _RUNGS),
                ("KLGA", "2026-08-30", _RUNGS)],
    )
    rows = {r["station_id"]: r for r in board._collect_live_orders(conn)["rows"]}
    assert rows["KMIA"]["settled_bucket"] == "31°C"
    assert rows["KMIA"]["settled_bucket_source"] == "paper_ledger"
    assert rows["KLGA"]["settled_bucket"] == "31°C"
    assert rows["KLGA"]["settled_bucket_source"] == "truth_lane"


def test_neither_live_reader_is_left_on_the_old_table() -> None:
    """One resolver, two callers, and no private copy of the query.

    The listing and the PnL used to hold a ``paper_trade_settlement`` query
    each.  Moving one and not the other is the failure this asserts against
    -- a panel whose basket shows 命中 while the tile beside it counts the
    same leg as pending has two answers for one fact.  Asserted on the
    parse tree rather than on the source text: what matters is that these
    functions call the resolver and query nothing themselves, not how the
    lines happen to be written.
    """

    import ast

    tree = ast.parse(
        (ROOT / "scripts" / "calibration_dashboard.py").read_text("utf-8")
    )
    wanted = {"_collect_live_orders", "_collect_live_pnl"}
    seen = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name not in wanted:
            continue
        seen.add(node.name)
        calls = {
            child.func.id for child in ast.walk(node)
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Name)
        }
        assert "_live_settled_buckets" in calls, node.name
        literals = [
            child.value for child in ast.walk(node)
            if isinstance(child, ast.Constant) and isinstance(child.value, str)
        ]
        assert not [s for s in literals if "paper_trade_settlement" in s], (
            node.name
        )
    assert seen == wanted


def test_each_station_carries_its_own_settled_days_and_five_day_window() -> None:
    """The go-live table reads live ROI per station, beside the paper one.

    Six settled dates across the book, so the five-day window has to drop
    the oldest -- and it drops it for every station at the same date, the
    way the paper column next to it is cut.  The pending leg is in the leg
    count and nowhere in the money.
    """

    days = ["2026-09-01", "2026-09-02", "2026-09-03",
            "2026-09-04", "2026-09-05", "2026-09-06"]
    rows, fills, settled = [], [], []
    for index, day in enumerate(days):
        # KMIA loses three USDC on the oldest day and wins on the other five.
        bucket = "30°C or below" if index == 0 else "31°C"
        rows.append((f"m{index}", f"dm{index}", "KMIA", day, bucket, 1))
        fills.append((f"m{index}", 10.0, 3.0))
        settled.append(("KMIA", day, "31°C"))
    # KLGA trades only the oldest day and one unsettled day.
    rows += [("l0", "dl0", "KLGA", days[0], "24°C", 1),
             ("l1", "dl1", "KLGA", "2026-09-07", "24°C", 1)]
    fills += [("l0", 10.0, 4.0), ("l1", 10.0, 5.0)]
    settled.append(("KLGA", days[0], "24°C"))

    conn = _book(rows=rows, fills=fills, settled=settled)
    pnl = board._collect_live_pnl(conn)
    assert pnl["recent_window_dates"] == days[1:]
    by = {r["station_id"]: r for r in pnl["by_station"]}

    kmia = by["KMIA"]
    assert kmia["settled_days"] == 6
    assert kmia["settled_legs"] == 6 and kmia["won"] == 5
    assert kmia["pnl_usdc"] == round(5 * 10.0 - 6 * 3.0, 4)
    assert kmia["recent_days"] == 5
    assert kmia["recent_pnl_usdc"] == 35.0
    assert kmia["recent_roi"] == round(35.0 / 15.0, 6)

    klga = by["KLGA"]
    assert klga["legs"] == 2 and klga["pending"] == 1
    assert klga["settled_legs"] == 1 and klga["settled_days"] == 1
    assert klga["cost_usdc"] == 4.0
    assert klga["roi"] == round(6.0 / 4.0, 6)
    # Its only settled day is outside the window: no sample, not 0%.
    assert klga["recent_days"] == 0
    assert klga["recent_roi"] is None


def _readiness_book(monkeypatch) -> dict:
    """The go-live collector over a small book, with the control plane stubbed.

    Two barred paper stations that differ only in their centre bias, named
    so the clean one sorts first; and one live station the paper book never
    seated.
    """

    conn = _book(
        rows=[("n1", "dn1", "NZWN", "2026-09-03", "31°C", 1)],
        fills=[("n1", 10.0, 4.0)],
        settled=[],
        truth=[("NZWN", "2026-09-03", 31.1, 0)],
        ladder=[("NZWN", "2026-09-03", _RUNGS)],
    )
    conn.executescript(
        "DROP TABLE paper_trade_settlement;"
        "CREATE TABLE paper_trade_settlement("
        " settlement_id INTEGER PRIMARY KEY, station_id TEXT,"
        " event_date TEXT, settled_bucket TEXT, hit INTEGER,"
        " stake_usdc REAL, pnl_usdc REAL);"
        "CREATE TABLE paper_trade_position("
        " station_id TEXT, event_date TEXT, center_c REAL);"
    )
    for station, bias_c in (("AAAA", 0.1), ("ZZZZ", 2.0)):
        for day in ("2026-09-01", "2026-09-02", "2026-09-03"):
            conn.execute(
                "INSERT INTO paper_trade_settlement(station_id, event_date,"
                " settled_bucket, hit, stake_usdc, pnl_usdc)"
                " VALUES (?,?, '31°C', 1, 1.0, 0.5)", (station, day),
            )
            conn.execute(
                "INSERT INTO calibration_station_daily_high_truth VALUES"
                " (?,?,?, 30.0, 0, ?)",
                (f"{station}{day}", station, day, "0" * 64),
            )
            conn.execute(
                "INSERT INTO paper_trade_position VALUES (?,?,?)",
                (station, day, 30.0 + bias_c),
            )
    conn.commit()
    monkeypatch.setattr(board, "barred_station_set",
                        lambda: {"AAAA", "ZZZZ"})
    monkeypatch.setattr(board, "_live_scope", lambda: {
        "scope": "all_except_barred", "stations": ["NZWN"],
        "probation": [], "barred": ["AAAA", "ZZZZ"],
        "minimum_settlements": 5,
    })
    band = [{"station": "AAAA", "ratio": 1.0},
            {"station": "ZZZZ", "ratio": 1.0}]
    return board.collect_live_readiness(conn, band)


def test_a_station_that_traded_live_has_a_row_with_its_live_money(
    monkeypatch,
) -> None:
    """LTAC and NZWN traded real money and had no row on the go-live table.

    The rows were the union of three paper-side sources, so a station the
    paper book never seated could not be seen -- nor could its switch.
    """

    ready = _readiness_book(monkeypatch)
    by = {r["station"]: r for r in ready["stations"]}
    assert "NZWN" in by
    live = by["NZWN"]["live"]
    assert live["settled_legs"] == 1 and live["won"] == 1
    assert live["roi"] == round(6.0 / 4.0, 6)
    # Paper-only stations carry no live row rather than a zero one.
    assert by["AAAA"]["live"] is None
    # And the row reads the same collector the money card does.
    assert ready["live_orders"]["pnl"]["by_station"][0] == live


def test_release_candidacy_reads_the_rows_own_gates(monkeypatch) -> None:
    """Each barred station is judged on its own centre, not the last row's.

    The criterion read ``gates`` and ``sid`` left over from the loop that
    built the rows, so AAAA -- clean centre, positive five days -- was held
    to ZZZZ's 2°C bias and never offered for release.
    """

    ready = _readiness_book(monkeypatch)
    by = {r["station"]: r for r in ready["stations"]}
    assert by["AAAA"]["gates"]["centre_bias"] is True
    assert by["ZZZZ"]["gates"]["centre_bias"] is False
    assert by["AAAA"]["release_candidate"] is True
    assert by["ZZZZ"]["release_candidate"] is False
    assert ready["release_candidates"] == ["AAAA"]
