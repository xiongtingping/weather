#!/usr/bin/env python3
"""Measure the paper ledger against the live book, and write the reading down.

``tests/test_ledger_production_rule_binding.py`` catches a rule that forked.
It cannot catch what happened by 2026-09-16: the rules were shared, the test
was green, and the two sides were still buying different legs on the same
station-day.  A rule binding compares source; this compares outcomes, and the
two failures need different instruments.

Why a table rather than a printout.  The four readings move slowly and the
question asked of them is "since when" -- a drift that shows up as one amber
render is unreadable, and the same drift as a row per day next to the commits
that landed is a diagnosis.  The row is small (one per pass), so the cost of
keeping every one of them is nothing against the cost of not being able to
date a divergence.

Each pass records the selection fingerprint the ledger was carrying.  Every
rule change mints a new one and re-derives the book underneath it, so a
reading that moved at the same time as the fingerprint is a different
question from one that moved on its own -- and without the fingerprint on the
row there is no way to tell those apart afterwards.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target import ledger_alignment as alignment  # noqa: E402

DB = ROOT / "data" / "bayes_basket_target_ssot.db"

TABLE = "paper_ledger_alignment_guard"
DDL = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
  measured_at_utc TEXT NOT NULL,
  definition_version INTEGER NOT NULL,
  window_days INTEGER NOT NULL,
  window_from TEXT NOT NULL,
  selection_rule_sha256 TEXT NOT NULL,
  verdict TEXT NOT NULL,
  seat_choice_verdict TEXT NOT NULL,
  seat_choice_n INTEGER NOT NULL,
  seat_choice_jaccard REAL,
  seat_choice_disjoint INTEGER NOT NULL,
  fill_rate_verdict TEXT NOT NULL,
  fill_rate_n INTEGER NOT NULL,
  fill_rate_live REAL,
  fill_rate_ledger REAL,
  fill_identity_verdict TEXT NOT NULL,
  fill_identity_n INTEGER NOT NULL,
  fill_identity_jaccard REAL,
  fill_identity_chance REAL,
  clock_verdict TEXT NOT NULL,
  clock_n INTEGER NOT NULL,
  clock_median_hours REAL,
  reach_rate REAL,
  cells_shared INTEGER NOT NULL,
  reading_json TEXT NOT NULL,
  PRIMARY KEY (measured_at_utc)
)
"""
INDEX = (
    f"CREATE INDEX IF NOT EXISTS {TABLE}_verdict_idx"
    f" ON {TABLE}(verdict, measured_at_utc)"
)


def _selection_rule_sha(conn: sqlite3.Connection) -> str:
    """The fingerprint the book is currently carrying.

    Read off the book rather than imported from the ledger module: importing
    it would report the rule THIS process computes, and the row has to say
    which rule the rows it measured were seated under.  A book mid-repair
    carries more than one; the newest is the one the reading describes.
    """

    try:
        row = conn.execute(
            "SELECT selection_rule_sha256 FROM paper_trade_position"
            " GROUP BY selection_rule_sha256"
            " ORDER BY MAX(event_date) DESC LIMIT 1"
        ).fetchone()
    except sqlite3.Error:
        return ""
    return str(row[0]) if row and row[0] else ""


def write_reading(
    conn: sqlite3.Connection, reading: dict, *, rule_sha: str
) -> None:
    conn.execute(DDL)
    conn.execute(INDEX)
    layers = reading["layers"]
    seat, rate = layers["seat_choice"], layers["fill_rate"]
    identity, clock = layers["fill_identity"], layers["clock"]
    conn.execute(
        f"INSERT OR REPLACE INTO {TABLE} ("
        " measured_at_utc, definition_version, window_days, window_from,"
        " selection_rule_sha256,"
        " verdict, seat_choice_verdict, seat_choice_n, seat_choice_jaccard,"
        " seat_choice_disjoint, fill_rate_verdict, fill_rate_n,"
        " fill_rate_live, fill_rate_ledger, fill_identity_verdict,"
        " fill_identity_n, fill_identity_jaccard, fill_identity_chance,"
        " clock_verdict, clock_n, clock_median_hours, reach_rate,"
        " cells_shared, reading_json"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            reading["measured_at_utc"], reading["definition_version"],
            reading["window_days"],
            reading["window_from"], rule_sha, reading["verdict"],
            seat["verdict"], seat["n"], seat["jaccard_mean"],
            seat["disjoint_cells"],
            rate["verdict"], rate["n"], rate["live_rate"],
            rate["ledger_rate"],
            identity["verdict"], identity["n"], identity["jaccard_mean"],
            identity["chance_mean"],
            clock["verdict"], clock["n"], clock["median_hours"],
            reading["reach_rate"], reading["cells_shared"],
            json.dumps(reading, ensure_ascii=False, sort_keys=True),
        ),
    )


def _summary(reading: dict) -> str:
    word = {
        alignment.GREEN: "对齐", alignment.AMBER: "偏离",
        alignment.RED: "分叉", alignment.INSUFFICIENT: "样本不足",
    }
    lines = [
        "账本 ↔ 生产对齐守卫  窗口 %s 起 %d 天  总判定 %s" % (
            reading["window_from"], reading["window_days"],
            word.get(reading["verdict"], reading["verdict"]),
        ),
        "  可比样本：实盘 %d 单里 %d 单到过交易所（%s），共同站日阶段 %d 个" % (
            reading["live_orders_authorized"],
            reading["live_orders_reached_exchange"],
            "—" if reading["reach_rate"] is None
            else "%.1f%%" % (100 * reading["reach_rate"]),
            reading["cells_shared"],
        ),
    ]
    layers = reading["layers"]
    lines.append("  %-12s %-6s n=%d/%d  Jaccard %s（完全不重合 %d 格）" % (
        layers["seat_choice"]["label"],
        word.get(layers["seat_choice"]["verdict"]),
        layers["seat_choice"]["n"], layers["seat_choice"]["minimum_n"],
        layers["seat_choice"]["jaccard_mean"],
        layers["seat_choice"]["disjoint_cells"],
    ))
    lines.append("  %-12s %-6s n=%d/%d  实盘 %s ↔ 账本 %s" % (
        layers["fill_rate"]["label"],
        word.get(layers["fill_rate"]["verdict"]),
        layers["fill_rate"]["n"], layers["fill_rate"]["minimum_n"],
        layers["fill_rate"]["live_rate"], layers["fill_rate"]["ledger_rate"],
    ))
    lines.append("  %-12s %-6s n=%d/%d  Jaccard %s 对同池随机 %s（差 %s）" % (
        layers["fill_identity"]["label"],
        word.get(layers["fill_identity"]["verdict"]),
        layers["fill_identity"]["n"], layers["fill_identity"]["minimum_n"],
        layers["fill_identity"]["jaccard_mean"],
        layers["fill_identity"]["chance_mean"],
        layers["fill_identity"]["lift"],
    ))
    lines.append("  %-12s %-6s n=%d/%d  中位差 %s 小时，%s 在 15 分钟内" % (
        layers["clock"]["label"], word.get(layers["clock"]["verdict"]),
        layers["clock"]["n"], layers["clock"]["minimum_n"],
        layers["clock"]["median_hours"],
        "—" if layers["clock"]["within_quarter_share"] is None
        else "%.0f%%" % (100 * layers["clock"]["within_quarter_share"]),
    ))
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--window-days", type=int, default=alignment.DEFAULT_WINDOW_DAYS
    )
    parser.add_argument("--db", default=str(DB))
    parser.add_argument(
        "--apply", action="store_true",
        help="write the reading to the guard table; default prints only",
    )
    parser.add_argument("--json", action="store_true", help="emit the reading")
    args = parser.parse_args()

    conn = sqlite3.connect(str(args.db), timeout=30.0)
    # The resident owns the single write domain and fail-closes P0/P1 for a
    # whole cycle rather than stall behind a maintenance writer, so this one
    # waits for its turn instead of taking it: the insert is a single small
    # row and has no reason to be the thing holding the lock.
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        reading = alignment.measure(
            conn, window_days=args.window_days,
            now=datetime.now(timezone.utc),
        )
        if args.apply:
            write_reading(
                conn, reading, rule_sha=_selection_rule_sha(conn)
            )
            conn.commit()
        print(
            json.dumps(reading, ensure_ascii=False, indent=2)
            if args.json else _summary(reading)
        )
    finally:
        conn.close()
    # A guard that exits non-zero on its own finding would read as a broken
    # sweep in every log that watches these, and the finding is not that the
    # pass failed.  The verdict goes in the row and onto the board.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
