"""The guard that watches outcomes, where the rule binding watches source.

``test_ledger_production_rule_binding`` compares the two sides' RULES and was
green on 2026-09-16 while the ledger and the live book were buying different
legs on the same station-day.  Rules can be shared and still be applied to
different books, so the guard measures what came out rather than what went
in, and this pins the three properties that make its answer worth reading:

1. the layers do not cover for each other -- a book that matches on how MANY
   legs filled while disagreeing on WHICH must show green on one and red on
   the other, because that is exactly the shape the binding test missed;
2. the universe is the orders that reached the exchange, so a station the
   operator barred cannot be scored as a ledger fault (the ledger is
   *supposed* to keep seating it);
3. a layer below its minimum sample says 样本不足 and never green.

The fixtures are built from production's own DDL -- ``apply_production_ssot_schema``
for the live side and the ledger's own ``ensure_schema`` for its own -- rather
than from trimmed CREATE TABLEs, because a hand-written fixture is free to
drift from the columns the guard actually reads and would then pass while
production fails.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target import ledger_alignment as alignment  # noqa: E402
from bayes_basket_target.ssot import apply_production_ssot_schema  # noqa: E402


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


ledger_module = _load("_ptl_for_guard", "scripts/paper_trading_ledger.py")
guard = _load("_guard_cli", "scripts/ledger_production_alignment_guard.py")

NOW = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)
DAY = "2026-09-15"


def _sha(text: str) -> str:
    """Production's tables CHECK that every sha column is 64 hex characters.

    Built from the real DDL rather than a trimmed fixture precisely so that
    constraints like this one apply here too: a placeholder that passes the
    fixture and fails production is a test that certifies nothing.
    """

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


RULE_SHA = _sha("selection-rule-under-test")


def _book() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    apply_production_ssot_schema(conn)
    # The ledger's own schema entrypoint, not just its CREATE TABLE: half
    # the columns the guard reads (``posted_buckets_json``,
    # ``resting_fill_json``, ``selection_rule_sha256``) arrive as ALTERs
    # applied here, so a fixture built from the DDL string alone would be
    # missing exactly the fields this measures.
    ledger_module.ensure_schema(conn)
    return conn


def _live(
    conn: sqlite3.Connection,
    station: str,
    buckets: dict[str, bool],
    *,
    stage: str = "P0",
    day: str = DAY,
    reached: bool = True,
    placed_at: str = "2026-09-14T12:00:00Z",
) -> None:
    """One live basket: ``buckets`` maps bucket -> did it fill."""

    decision = f"dec-{station}-{day}-{stage}"
    # A plain INSERT, deliberately.  ``INSERT OR IGNORE`` swallowed a CHECK
    # violation here while this was being written and the test reported
    # "zero live cells" instead of "your fixture is invalid" -- a fixture
    # that hides its own rejections certifies nothing.  ``cycle_id`` and
    # ``plan_id`` are UNIQUE in production, so they are per-decision.
    conn.execute(
        "INSERT INTO auto_trade_decisions"
        " (decision_id, cycle_id, plan_id, station_id, event_date,"
        "  settlement_timezone, strategy_lane, decided_state, will_order,"
        "  reason_codes_json, details_json, decision_sha256, created_at,"
        "  target_execution_hard_deadline_at, decision_scope_key)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            decision, f"cycle-{decision}", f"plan-{decision}", station, day,
            "UTC", "basket", "prepared", 1, "[]",
            json.dumps({"decision_signature": {"stage": stage}}),
            _sha(decision), placed_at, placed_at,
            f"{station}|{day}|{stage}",
        ),
    )
    for index, (bucket, filled) in enumerate(buckets.items()):
        intent = f"int-{station}-{day}-{stage}-{index}"
        conn.execute(
            "INSERT INTO basket_target_order_intents"
            " (order_intent_id, decision_id, cycle_id, bucket, token_id,"
            # side='YES'/action='BUY' are the only pair production's own
            # trigger (``no_order_side_retired``) admits -- the pure-YES
            # basket is the only live strategy.
            "  side, order_action, limit_price, requested_size,"
            "  requested_notional_usdc, state, live_submit_authorized,"
            "  idempotency_key, intent_json, intent_sha256, created_at,"
            "  target_execution_hard_deadline_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                intent, decision, f"cycle-{decision}", bucket, "token", "YES", "BUY",
                0.3, 10.0, 3.0, "filled" if filled else "cancelled", 1,
                intent, "{}", _sha(intent), placed_at, placed_at,
            ),
        )
        conn.execute(
            "INSERT INTO basket_target_order_reconciliations"
            " (order_intent_id, exchange_order_id, state, filled_size,"
            "  filled_notional_usdc, last_event_id, blocking_reason,"
            "  updated_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (
                intent, "x",
                "resolved_filled" if filled else "resolved_cancelled",
                10.0 if filled else 0.0, 3.0 if filled else 0.0, None,
                None if reached else "order_never_reached_exchange",
                placed_at,
            ),
        )


def _ledger(
    conn: sqlite3.Connection,
    station: str,
    posted: list[str],
    filled: list[str],
    *,
    stage: str = "P0",
    day: str = DAY,
    decided_at: str = "2026-09-14T12:00:00Z",
) -> None:
    rungs = {b: ("1/1@0.3000" if b in filled else "0/1@0.0000")
             for b in posted}
    conn.execute(
        "INSERT INTO paper_trade_position"
        " (position_id, station_id, event_date, stage, decision_at_utc,"
        "  quote_observed_at_utc, ladder_center_bucket, window_buckets_json,"
        "  window_size, win_probability, all_in_cost, edge, kelly_fraction,"
        "  stake_usdc, shares, probabilities_json, prices_json,"
        "  refused_window_count, content_sha256, resting_fill_json,"
        "  posted_buckets_json, selection_rule_sha256)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            f"pos-{station}-{day}-{stage}", station, day, stage, decided_at,
            decided_at, posted[0], json.dumps(filled), len(filled), 0.5, 0.4,
            0.1, 0.25, 3.0, 10.0, "{}", "{}", 0, _sha(station + day),
            json.dumps(rungs), json.dumps(posted), RULE_SHA,
        ),
    )


def _measure(conn: sqlite3.Connection) -> dict:
    return alignment.measure(conn, window_days=14, now=NOW)


# --- the closed-form baseline -------------------------------------------

@pytest.mark.parametrize(
    "pool,left,right",
    [(3, 1, 2), (4, 2, 2), (5, 3, 2), (6, 1, 1), (5, 5, 5), (4, 1, 4)],
)
def test_chance_jaccard_matches_exhaustive_enumeration(pool, left, right):
    """The hypergeometric shortcut must equal enumerating every pairing.

    The baseline is what the fill-identity verdict is measured AGAINST, so a
    baseline that drifted would move the verdict without anything about the
    book changing.  Enumeration is the definition; the closed form is only an
    optimisation, and this is the proof it stayed one.
    """

    from itertools import combinations

    universe = list(range(pool))
    totals = []
    for a in combinations(universe, left):
        for b in combinations(universe, right):
            sa, sb = set(a), set(b)
            totals.append(len(sa & sb) / len(sa | sb))
    assert alignment.chance_jaccard(pool, left, right) == pytest.approx(
        sum(totals) / len(totals), abs=1e-12
    )


def test_chance_jaccard_is_undefined_when_a_side_bought_nothing():
    assert alignment.chance_jaccard(3, 0, 2) is None
    assert alignment.chance_jaccard(0, 1, 1) is None


# --- the layers do not cover for each other -----------------------------

def test_matching_book_is_green_on_every_layer():
    conn = _book()
    for index in range(22):
        station = "ST%02d" % index
        _live(conn, station, {"a": True, "b": False, "c": True})
        _ledger(conn, station, ["a", "b", "c"], ["a", "c"])
    reading = _measure(conn)
    assert reading["verdict"] == alignment.GREEN
    for name, layer in reading["layers"].items():
        assert layer["verdict"] == alignment.GREEN, (name, layer)
    assert reading["layers"]["fill_identity"]["jaccard_mean"] == 1.0


def test_same_fill_rate_different_legs_is_caught_by_identity_alone():
    """The 2026-09-16 shape, which the rule binding could not see.

    Both sides post three legs and fill two of them, so every aggregate
    agrees -- the seat is the same basket and the fill RATE is identical --
    and the legs are disjoint.  If the identity layer ever stops separating
    from the rate layer, this guard has become the test it was built to
    complement rather than replace.
    """

    conn = _book()
    for index in range(22):
        station = "ST%02d" % index
        _live(conn, station, {"a": True, "b": True, "c": False, "d": False})
        _ledger(conn, station, ["a", "b", "c", "d"], ["c", "d"])
    reading = _measure(conn)
    layers = reading["layers"]
    assert layers["seat_choice"]["verdict"] == alignment.GREEN
    assert layers["fill_rate"]["verdict"] == alignment.GREEN
    assert layers["fill_rate"]["live_rate"] == layers["fill_rate"][
        "ledger_rate"
    ]
    assert layers["fill_identity"]["verdict"] == alignment.RED
    assert layers["fill_identity"]["jaccard_mean"] == 0.0
    assert layers["fill_identity"]["lift"] < 0
    assert reading["verdict"] == alignment.RED


def test_disjoint_baskets_are_caught_by_seat_choice():
    conn = _book()
    for index in range(22):
        station = "ST%02d" % index
        _live(conn, station, {"a": True, "b": True})
        _ledger(conn, station, ["y", "z"], ["y", "z"])
    layers = _measure(conn)["layers"]
    assert layers["seat_choice"]["verdict"] == alignment.RED
    assert layers["seat_choice"]["jaccard_mean"] == 0.0
    assert layers["seat_choice"]["disjoint_cells"] == 22


def test_clock_layer_reads_the_gap_to_the_nearest_live_order():
    conn = _book()
    for index in range(12):
        station = "ST%02d" % index
        _live(conn, station, {"a": True, "b": True},
              placed_at="2026-09-14T12:00:00Z")
        _ledger(conn, station, ["a", "b"], ["a", "b"],
                decided_at="2026-09-14T18:00:00Z")
    layers = _measure(conn)["layers"]
    assert layers["clock"]["median_hours"] == pytest.approx(6.0)
    assert layers["clock"]["within_quarter_hour"] == 0
    assert layers["clock"]["verdict"] == alignment.RED


# --- the universe rule ---------------------------------------------------

def test_orders_stopped_at_the_door_are_not_scored_against_the_ledger():
    """A barred station is not a ledger fault.

    1,550 of 1,928 live orders never reached the exchange on the day this
    was written, every one refused on station policy, and the ledger is
    meant to keep seating exactly those stations -- that is where a barred
    station's evidence comes from.  Counting them would make the guard a
    reading of the operator's admission list.
    """

    conn = _book()
    for index in range(22):
        station = "ST%02d" % index
        # Three posted, two filled: a pool with room to disagree, so the
        # identity layer has something to score.  Two-for-two would make
        # agreement the only possible outcome and the cell would carry no
        # evidence either way.
        _live(conn, station, {"a": True, "b": True, "c": False})
        _ledger(conn, station, ["a", "b", "c"], ["a", "b"])
    # A whole basket production authorized and the door refused, on a
    # station the ledger seated somewhere else entirely.
    _live(conn, "BARRED", {"q": False, "r": False}, reached=False)
    _ledger(conn, "BARRED", ["x", "y"], ["x"])
    reading = _measure(conn)
    assert reading["verdict"] == alignment.GREEN
    assert reading["cells_shared"] == 22
    assert reading["live_orders_authorized"] == 68
    assert reading["live_orders_reached_exchange"] == 66
    assert reading["reach_rate"] == pytest.approx(66 / 68, abs=1e-4)


def test_stage_separates_cells_on_the_same_station_day():
    conn = _book()
    _live(conn, "KLGA", {"a": True}, stage="P0")
    _live(conn, "KLGA", {"z": True}, stage="P1")
    _ledger(conn, "KLGA", ["a"], ["a"], stage="P0")
    reading = _measure(conn)
    assert reading["cells_live"] == 2
    assert reading["cells_shared"] == 1


# --- sample floors -------------------------------------------------------

def test_a_layer_below_its_minimum_never_reports_green():
    """样本不足 is not a pass.

    On 2026-09-16 the identity layer had n=14 against a floor of 20, and a
    guard that had called that red would have asserted more than it knew --
    while one that called it green would have cleared a book nobody had
    measured.  Both are wrong in the same way.
    """

    conn = _book()
    for index in range(3):
        station = "ST%02d" % index
        _live(conn, station, {"a": True, "b": True})
        _ledger(conn, station, ["a", "b"], ["a", "b"])
    reading = _measure(conn)
    for name, layer in reading["layers"].items():
        assert layer["n"] < layer["minimum_n"], (name, layer)
        assert layer["verdict"] == alignment.INSUFFICIENT, (name, layer)
    assert reading["verdict"] == alignment.INSUFFICIENT


def test_an_empty_book_says_insufficient_rather_than_aligned():
    reading = _measure(_book())
    assert reading["verdict"] == alignment.INSUFFICIENT
    assert reading["cells_shared"] == 0
    assert reading["reach_rate"] is None


# --- the row the sweep writes ------------------------------------------------

def test_reading_round_trips_through_the_guard_table():
    """What the sweep writes carries the fingerprint it measured under.

    A reader can date a divergence against the commit that caused it only if
    the row says which rule the rows it measured were seated under.  (The
    board section that drew these rows left the page on 2026-09-16.)
    """

    conn = _book()
    for index in range(22):
        station = "ST%02d" % index
        _live(conn, station, {"a": True, "b": True, "c": False})
        _ledger(conn, station, ["a", "b", "c"], ["a", "b"])
    reading = _measure(conn)
    guard.write_reading(conn, reading, rule_sha=guard._selection_rule_sha(conn))
    conn.commit()

    row = conn.execute(
        "SELECT verdict, selection_rule_sha256, fill_identity_n"
        f" FROM {guard.TABLE}"
    ).fetchone()
    assert row[0] == reading["verdict"]
    # Read off the book, not imported from the ledger module: the row has to
    # say which rule the rows it measured were seated under.
    assert row[1] == RULE_SHA
    assert row[2] == reading["layers"]["fill_identity"]["n"]



def test_a_cell_with_no_room_to_disagree_is_not_evidence_of_agreement():
    """Both sides bought the whole pool, so agreeing was the only outcome.

    Scored naively this reads lift 0.0 -- "no better than chance" -- for two
    books that matched perfectly, and an early build of this guard reported
    exactly that.  The cell is dropped instead: no possible disagreement, no
    evidence of agreement.
    """

    conn = _book()
    for index in range(22):
        station = "ST%02d" % index
        _live(conn, station, {"a": True, "b": True})
        _ledger(conn, station, ["a", "b"], ["a", "b"])
    layers = _measure(conn)["layers"]
    assert layers["seat_choice"]["verdict"] == alignment.GREEN
    assert layers["fill_identity"]["n"] == 0
    assert layers["fill_identity"]["verdict"] == alignment.INSUFFICIENT
