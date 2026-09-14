"""A book that holds two rule sets repairs itself (charter 129, ruled 09-12).

``_drop_superseded_positions`` is bounded by the window a pass can re-derive,
which is right -- never delete what you will not rebuild -- and the price is
that the incremental window can never reach an older rule set.  Three identity
changes landed on 2026-09-12 and the standing book became 224 settled chains
of one strategy and 83 of another, published as a single -28.3% while the
served rule set alone read -34.5%.

The repair is the same shape as the one already beside it for the refusal
vocabulary: a pass that finds foreign rows walks the whole history instead of
the recent window.
"""
from __future__ import annotations

import ast
import importlib
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import paper_trading_ledger as ledger  # noqa: E402

OTHER = "0" * 64


def _book(*shas: str) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    ledger.ensure_schema(conn)
    columns = [
        row[1] for row in conn.execute("PRAGMA table_info(paper_trade_position)")
    ]
    for index, sha in enumerate(shas):
        values = {
            "position_id": f"p{index}", "station_id": "KMIA",
            "event_date": "2026-09-0%d" % (index + 1), "stage": "P0",
            "decision_at_utc": "2026-09-01T00:00:00Z",
            "quote_observed_at_utc": "2026-09-01T00:00:00Z",
            "ladder_center_bucket": "30C", "window_buckets_json": '["30C"]',
            "window_size": 1, "win_probability": 0.5, "all_in_cost": 0.5,
            "edge": 0.0, "kelly_fraction": 0.25, "stake_usdc": 1.0,
            "shares": 1.0, "probabilities_json": "{}", "prices_json": "{}",
            "refused_window_count": 0, "content_sha256": f"c{index}",
            "selection_rule_sha256": sha,
        }
        present = {k: v for k, v in values.items() if k in columns}
        conn.execute(
            "INSERT INTO paper_trade_position (%s) VALUES (%s)"
            % (",".join(present), ",".join("?" * len(present))),
            tuple(present.values()),
        )
    conn.commit()
    return conn


def test_the_detector_counts_only_foreign_rule_sets() -> None:
    conn = _book(ledger.SELECTION_RULE_SHA256, OTHER, OTHER)
    assert ledger._positions_under_other_rule_sets(conn) == 2
    clean = _book(ledger.SELECTION_RULE_SHA256)
    assert ledger._positions_under_other_rule_sets(clean) == 0
    # A database without the table answers zero rather than raising: the
    # detector runs before the walk on every pass.
    assert ledger._positions_under_other_rule_sets(
        sqlite3.connect(":memory:")
    ) == 0


def test_the_repair_converges_in_one_pass() -> None:
    """A full-window drop can leave nothing foreign behind, so no loop."""

    conn = _book(ledger.SELECTION_RULE_SHA256, OTHER, OTHER)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS basket_probability_bridge_history ("
        " station_id TEXT, event_date TEXT, stage TEXT, state TEXT)"
    )
    conn.execute(
        "INSERT INTO basket_probability_bridge_history"
        " VALUES ('KMIA','2026-09-03','P0','COMMITTED')"
    )
    conn.commit()

    ledger._drop_superseded_positions(conn, recent_days=None)
    assert ledger._positions_under_other_rule_sets(conn) == 0
    # And the served rows are untouched.
    assert conn.execute(
        "SELECT COUNT(*) FROM paper_trade_position"
    ).fetchone()[0] == 1


def test_an_incremental_window_cannot_reach_the_older_rule_set() -> None:
    """Why the trigger has to widen the window rather than trust the drop."""

    conn = _book(ledger.SELECTION_RULE_SHA256, OTHER)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS basket_probability_bridge_history ("
        " station_id TEXT, event_date TEXT, stage TEXT, state TEXT)"
    )
    # Newest event day is 09-20, so a four-day window starts at 09-16 and
    # the foreign row on 09-02 is outside it.
    conn.execute(
        "INSERT INTO basket_probability_bridge_history"
        " VALUES ('KMIA','2026-09-20','P0','COMMITTED')"
    )
    conn.commit()

    ledger._drop_superseded_positions(conn, recent_days=4)
    assert ledger._positions_under_other_rule_sets(conn) == 1


def test_the_pass_widens_its_window_when_the_book_is_mixed() -> None:
    """The trigger, read off the statements it is made of."""

    source = (ROOT / "scripts" / "paper_trading_ledger.py").read_text(
        encoding="utf-8"
    )
    backfill = next(
        node for node in ast.parse(source).body
        if isinstance(node, ast.FunctionDef) and node.name == "backfill"
    )
    trigger = next(
        node for node in ast.walk(backfill)
        if isinstance(node, ast.If)
        and "foreign_rule_set_rows" in ast.unparse(node.test)
    )
    body = ast.unparse(trigger)
    assert "recent_days = None" in body
    assert "AUTO_REDERIVE_ON_RULE_CHANGE == 'on'" in ast.unparse(trigger.test)

    def _line(needle: str) -> int:
        for node in ast.walk(backfill):
            if isinstance(node, ast.Call) and needle in ast.unparse(node):
                return node.lineno
        raise AssertionError(needle)

    # Detected and decided before the drop reads the window, or the widened
    # window arrives too late to matter.
    assert _line("_positions_under_other_rule_sets(") < _line(
        "_drop_superseded_positions("
    )


def test_the_switch_is_operational_and_not_a_rule() -> None:
    """Two books made by the same rules must carry the same digest."""

    def _sha(mode: str | None) -> str:
        previous = os.environ.get("PAPER_LEDGER_AUTO_REDERIVE")
        if mode is None:
            os.environ.pop("PAPER_LEDGER_AUTO_REDERIVE", None)
        else:
            os.environ["PAPER_LEDGER_AUTO_REDERIVE"] = mode
        try:
            return importlib.reload(ledger).SELECTION_RULE_SHA256
        finally:
            if previous is None:
                os.environ.pop("PAPER_LEDGER_AUTO_REDERIVE", None)
            else:
                os.environ["PAPER_LEDGER_AUTO_REDERIVE"] = previous
            importlib.reload(ledger)

    assert _sha(None) == _sha("on") == _sha("off")
    assert _sha(None) == ledger.SELECTION_RULE_SHA256


def test_an_unknown_mode_is_refused() -> None:
    import pytest  # noqa: PLC0415

    previous = os.environ.get("PAPER_LEDGER_AUTO_REDERIVE")
    os.environ["PAPER_LEDGER_AUTO_REDERIVE"] = "sometimes"
    try:
        with pytest.raises(SystemExit):
            importlib.reload(ledger)
    finally:
        if previous is None:
            os.environ.pop("PAPER_LEDGER_AUTO_REDERIVE", None)
        else:
            os.environ["PAPER_LEDGER_AUTO_REDERIVE"] = previous
        importlib.reload(ledger)


def test_the_repair_is_counted_so_a_thrash_is_visible() -> None:
    """A rule change buys one slow pass; a storm of them buys many.

    The counter is how that shows up in the sweep log instead of as an
    unexplained fourteen-minute pass.
    """

    source = (ROOT / "scripts" / "paper_trading_ledger.py").read_text(
        encoding="utf-8"
    )
    assert 'counts["positions_under_other_rule_sets"]' in source
    assert 'counts["rule_change_full_rederive"]' in source


# ---------------------------------------------------------------- bounded
#
# 2026-09-14: the whole-history re-derive needs about 55 minutes and the
# hosted sweep that runs it is killed at 1800 seconds.  The 11:32Z pass
# dropped every foreign row, was killed before its closing write, and left
# a book with no positions and nothing foreign to trigger a repair -- the
# next four-day incremental pass would have called four days the book.  A
# repair now takes the oldest ``REDERIVE_SLICE_DAYS`` days that need it and
# the book's own newest answer is the cursor it resumes from.


def _history(conn: sqlite3.Connection, *days: str) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS basket_probability_bridge_history ("
        " station_id TEXT, event_date TEXT, stage TEXT, state TEXT)"
    )
    conn.executemany(
        "INSERT INTO basket_probability_bridge_history VALUES ('KMIA',?,'P0','COMMITTED')",
        [(day,) for day in days],
    )
    conn.commit()


def _refusal(conn: sqlite3.Connection, day: str, sha: str) -> None:
    conn.execute(
        "INSERT INTO paper_trade_refusal (station_id, event_date, stage, reason,"
        " detail, cycles_examined, last_decision_at_utc, recorded_at_utc,"
        " selection_rule_sha256) VALUES ('KMIA', ?, 'P0', 'no_book_at_decision', '', 0, '', '', ?)",
        (day, sha),
    )
    conn.commit()


def _after(day: str, days: int) -> str:
    from datetime import date, timedelta  # noqa: PLC0415

    return (date.fromisoformat(day) + timedelta(days=days)).isoformat()


def test_a_drop_bounded_above_keeps_the_foreign_rows_it_will_not_rederive() -> None:
    conn = _book(OTHER, OTHER, OTHER)  # 09-01, 09-02, 09-03
    _history(conn, "2026-09-03")

    ledger._drop_superseded_positions(conn, recent_days=None, through_date="2026-09-02")

    assert [r[0] for r in conn.execute(
        "SELECT event_date FROM paper_trade_position ORDER BY event_date"
    )] == ["2026-09-03"]


def test_an_empty_book_resumes_from_the_floor_one_slice_at_a_time() -> None:
    """The state the killed pass left: history, and no answer at all."""

    floor = ledger.LEDGER_EVENT_DATE_FLOOR
    conn = _book()
    _history(conn, floor, _after(floor, 40))

    assert ledger._repair_slice_through(conn, recent_days=4) == _after(
        floor, ledger.REDERIVE_SLICE_DAYS - 1
    )


def test_a_partial_book_resumes_after_its_newest_answer() -> None:
    floor = ledger.LEDGER_EVENT_DATE_FLOOR
    conn = _book()
    _history(conn, floor, _after(floor, 40))
    _refusal(conn, _after(floor, 9), ledger.SELECTION_RULE_SHA256)
    # A refusal under another rule set is not an answer this rule set gave.
    _refusal(conn, _after(floor, 20), OTHER)

    assert ledger._repair_slice_through(conn, recent_days=4) == _after(
        floor, 10 + ledger.REDERIVE_SLICE_DAYS - 1
    )


def test_a_book_that_reaches_the_incremental_window_needs_no_repair() -> None:
    floor = ledger.LEDGER_EVENT_DATE_FLOOR
    conn = _book()
    newest = _after(floor, 40)
    _history(conn, floor, newest)
    _refusal(conn, _after(floor, 37), ledger.SELECTION_RULE_SHA256)

    assert ledger._repair_slice_through(conn, recent_days=4) is None
    # A rebuild and an explicitly bounded walk plan their own coverage.
    assert ledger._repair_slice_through(conn, recent_days=None) is None


def test_foreign_rows_start_the_slice_at_the_oldest_of_them() -> None:
    conn = _book(ledger.SELECTION_RULE_SHA256, OTHER, OTHER)  # 09-01 served
    _history(conn, "2026-09-01", "2026-09-30")

    assert ledger._repair_slice_through(conn, recent_days=4) == _after(
        "2026-09-02", ledger.REDERIVE_SLICE_DAYS - 1
    )


def test_the_slice_never_runs_past_the_history() -> None:
    conn = _book(OTHER)  # 09-01
    _history(conn, "2026-09-01", "2026-09-02")

    assert ledger._repair_slice_through(conn, recent_days=4) == "2026-09-02"


def _run_main(tmp_path, monkeypatch, results, *, tick: float, budget: float) -> list:
    database = tmp_path / "book.db"
    sqlite3.connect(database).close()
    calls: list = []
    clock = {"now": 0.0}

    def fake_backfill(conn, **kwargs):
        calls.append(kwargs)
        clock["now"] += tick
        return dict(results[min(len(calls), len(results)) - 1])

    monkeypatch.setattr(ledger, "backfill", fake_backfill)
    monkeypatch.setattr(ledger.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(ledger, "REDERIVE_PASS_BUDGET_SEC", budget)
    monkeypatch.setattr(sys, "argv", ["paper_trading_ledger.py", "--db", str(database), "--backfill"])
    assert ledger.main() == 0
    return calls


def test_a_pass_takes_the_next_slice_while_it_has_budget(tmp_path, monkeypatch) -> None:
    slices = [{"repair_slice_through": "2026-08-19"}, {"repair_slice_through": "2026-08-24"}, {}]

    calls = _run_main(tmp_path, monkeypatch, slices, tick=100.0, budget=1500.0)

    assert len(calls) == 3
    # The first call is the scheduled incremental pass; the ones after it
    # hand the planner the same window and no bound of their own.
    assert all(c.get("through_date") is None for c in calls)


def test_a_pass_stops_before_a_slice_it_could_not_finish(tmp_path, monkeypatch) -> None:
    slices = [{"repair_slice_through": "2026-08-19"}, {"repair_slice_through": "2026-08-24"}]

    calls = _run_main(tmp_path, monkeypatch, slices, tick=700.0, budget=1500.0)

    assert len(calls) == 1  # 700 spent + 1.5 x 700 expected > 1500


def test_a_slice_that_does_not_move_the_cursor_stops_the_loop(tmp_path, monkeypatch) -> None:
    slices = [{"repair_slice_through": "2026-08-19"}]

    calls = _run_main(tmp_path, monkeypatch, slices, tick=10.0, budget=1500.0)

    assert len(calls) == 2
