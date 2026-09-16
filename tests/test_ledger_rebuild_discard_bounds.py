"""A rebuild deletes only what it is going to re-derive, and says when it has not.

Two defects found on 2026-09-08 while re-running the book under charter 110.

``--rebuild`` opened by deleting all three ledger tables outright, with no
date bound, and only THEN honoured ``--through-date`` when choosing which
days to walk.  Both the option's own help text and ``backfill``'s docstring
promise the opposite -- "rows above the bound are left exactly as they are"
-- and the workflow the option exists for (a rebuild taken oldest-first in
slices, so each seat is sized off a book whose earlier days are already
re-derived) therefore wiped every day above the bound on each pass.  The
test that was meant to hold this, ``test_a_rebuild_can_be_taken_oldest_
first_in_slices``, greps the source for the WALK's bound
(``AND (?3 IS NULL OR event_date <= ?3)``) and passes on the broken discard.
So this one runs the discard.

The second defect is what the discard leaves behind while it is running.
It commits, and the re-walk then takes about forty minutes; the dashboard
render is on its own half-hourly timer and the two lanes do not exclude
each other.  A render at 2026-09-08 10:20 landed inside the window and
published position_count=0, day_count=0 and every hit rate at zero, as
measurements.  The page shrank from 6.95MB to 4.25MB and read as "命中率
都这么低".  The book cannot stop being briefly absent without holding a
forty-minute write lock against production, so the page has to be able to
tell absent from empty: history with no seat at all is the signature.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

LEDGER = ROOT / "scripts" / "paper_trading_ledger.py"


def _ledger_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE paper_trade_position ("
        " position_id TEXT PRIMARY KEY, station_id TEXT, event_date TEXT)"
    )
    conn.execute(
        "CREATE TABLE paper_trade_settlement ("
        " settlement_id TEXT PRIMARY KEY, position_id TEXT, event_date TEXT)"
    )
    conn.execute(
        "CREATE TABLE paper_trade_refusal ("
        " refusal_id TEXT PRIMARY KEY, event_date TEXT)"
    )


def _seed(conn: sqlite3.Connection, dates: list[str]) -> None:
    for index, date in enumerate(dates):
        conn.execute(
            "INSERT INTO paper_trade_position VALUES (?,?,?)",
            (f"p{index}", "KMIA", date),
        )
        conn.execute(
            "INSERT INTO paper_trade_settlement VALUES (?,?,?)",
            (f"s{index}", f"p{index}", date),
        )
        conn.execute(
            "INSERT INTO paper_trade_refusal VALUES (?,?)", (f"r{index}", date)
        )
    conn.commit()


def _discard(conn: sqlite3.Connection, bound: str | None) -> None:
    """The discard exactly as ``main`` runs it, read out of the shipped file.

    Extracted rather than restated: a copy here would keep passing after the
    real one drifted, which is how the first defect survived a test written
    for it.
    """

    source = LEDGER.read_text(encoding="utf-8")
    start = source.index('        if args.rebuild:\n')
    end = source.index("            conn.commit()\n", start) + len(
        "            conn.commit()\n"
    )
    body = source[start:end]
    assert "DELETE FROM paper_trade_settlement" in body
    # Drop the guard line and the schema call, dedent, and run what is left.
    lines = [
        line[12:] if line.startswith(" " * 12) else line
        for line in body.splitlines()
        if line.strip() not in {"if args.rebuild:", "ensure_schema(conn)"}
        and not line.strip().startswith("#")
    ]
    namespace = {"conn": conn, "args": type("A", (), {"through_date": bound})()}
    exec(  # noqa: S102 -- the shipped discard, run against a fixture
        "\n".join(lines).replace(
            'bound = (args.through_date or "").strip() or None',
            'bound = (args.through_date or "").strip() or None',
        ),
        namespace,
    )


def _dates(conn: sqlite3.Connection, table: str) -> list[str]:
    return sorted(
        str(r[0])
        for r in conn.execute(f"SELECT event_date FROM {table}")
    )


def test_a_bounded_rebuild_keeps_the_days_above_its_bound() -> None:
    """The promise the help text makes, run rather than grepped."""

    conn = sqlite3.connect(":memory:")
    _ledger_schema(conn)
    _seed(conn, ["2026-08-15", "2026-08-31", "2026-09-01", "2026-09-08"])
    _discard(conn, "2026-08-31")
    for table in (
        "paper_trade_position", "paper_trade_settlement", "paper_trade_refusal",
    ):
        assert _dates(conn, table) == ["2026-09-01", "2026-09-08"], table


def test_an_unbounded_rebuild_still_discards_everything() -> None:
    """No bound means the whole book, which is what a plain --rebuild is."""

    conn = sqlite3.connect(":memory:")
    _ledger_schema(conn)
    _seed(conn, ["2026-08-15", "2026-09-08"])
    _discard(conn, None)
    for table in (
        "paper_trade_position", "paper_trade_settlement", "paper_trade_refusal",
    ):
        assert _dates(conn, table) == [], table


def test_the_settlements_of_kept_days_survive_a_bounded_discard() -> None:
    """Settlements are deleted through their position, so the join has to
    carry the bound too -- deleting them all would leave the kept days
    holding seats that no longer settle."""

    conn = sqlite3.connect(":memory:")
    _ledger_schema(conn)
    _seed(conn, ["2026-08-20", "2026-09-05"])
    _discard(conn, "2026-08-20")
    assert _dates(conn, "paper_trade_settlement") == ["2026-09-05"]
    assert conn.execute(
        "SELECT COUNT(*) FROM paper_trade_settlement s"
        " JOIN paper_trade_position p ON p.position_id = s.position_id"
    ).fetchone()[0] == 1


def test_the_help_text_and_the_discard_now_agree() -> None:
    """The two statements that disagreed.  Kept as a pair so a future edit
    to either one has to look at the other."""

    source = LEDGER.read_text(encoding="utf-8")
    assert "Rows above the bound are left untouched." in source
    discard = source[source.index("        if args.rebuild:"):]
    discard = discard[: discard.index("        counts = backfill(")]
    assert "WHERE ?1 IS NULL OR event_date <= ?1" in discard
    assert "DELETE FROM paper_trade_position\"\n" not in discard


def test_an_absent_ledger_is_not_reported_as_an_empty_one() -> None:
    """The page's half of it.  A book with history and no seat at all is
    a book that is not there; zeros would be read as measurements."""

    import selection_market_dashboard as S

    source = (
        ROOT / "scripts" / "selection_market_dashboard.py"
    ).read_text(encoding="utf-8")
    assert '"ledger_absent": absent,' in source
    assert "FROM basket_probability_bridge_history" in source

    page = (ROOT / "scripts" / "calibration_dashboard.py").read_text(
        encoding="utf-8"
    )
    # The board's half left with the paper readings (user ruling,
    # 2026-09-16: the paper ledger is a rule comparator).  The strip's
    # 选桶 row reads the live book, which no rebuild empties, so the banner
    # that suppressed it while the paper tables were empty has nothing left
    # to guard -- and a banner telling a reader not to read live numbers
    # would be wrong.
    overview = page[page.index("function overviewLamps(){"):]
    overview = overview[: overview.index("\n}\n")]
    assert "S.ledger_absent" not in overview
    assert "D.live_selection" in overview
    assert str(S.STATS_START_EVENT_DATE)
