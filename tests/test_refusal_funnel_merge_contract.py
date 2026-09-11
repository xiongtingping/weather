"""What the merged refusal card on the board needs from the payload.

The three per-stage cards became one card (user ruling, 2026-09-11), and a
merge changes what the rows have to carry.  Per stage the denominators were
separate, so ``chain_never_opened`` could sit in P0-M's and P1's tally
harmlessly; merged, it is the same station-day's refusal counted a second
and a third time, and it would be the largest "reason" on the board while
naming none.  The page subtracts it from both sides -- which it can only do
because every row states its reason as a key, not only as a Chinese label.

So this pins the handle, not the arithmetic: the page owns the rule, and an
assertion here that recomputed it would be a second implementation to keep
in step with the first.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import selection_market_dashboard as S  # noqa: E402


def _book() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE paper_trade_refusal"
        " (stage TEXT, reason TEXT, station_id TEXT, detail TEXT)"
    )
    conn.execute(
        "CREATE TABLE paper_trade_position (stage TEXT, station_id TEXT)"
    )
    rows = [
        ("P0", "negative_edge_window", "KMIA", "edge 0.01"),
        ("P0", "negative_edge_window", "KLGA", "edge 0.00"),
        ("P0", "no_book_at_decision", "KJFK", ""),
        # The same two station-days reappear downstream carrying the
        # pass-through, which is what the merge has to take back out.
        ("P0-M", "chain_never_opened", "KMIA", ""),
        ("P1", "chain_never_opened", "KMIA", ""),
        ("P1", "chain_never_opened", "KLGA", ""),
    ]
    conn.executemany("INSERT INTO paper_trade_refusal VALUES (?,?,?,?)", rows)
    conn.execute("INSERT INTO paper_trade_position VALUES ('P0','KBOS')")
    conn.commit()
    return conn


def test_every_reason_row_states_its_reason_as_a_key() -> None:
    stages = S._refusal_stats(_book())["stages"]
    assert stages, "no stages came back"
    for stage in stages:
        for row in stage["reasons"]:
            assert row["reason"], f"{stage['stage']} row has no reason key"
            assert row["label"], f"{row['reason']} has no label"


def test_the_pass_through_is_counted_per_stage_so_it_can_be_taken_back_out(
) -> None:
    """It has to be visible AND separable.

    Visible because the per-stage funnel only balances with it in; separable
    because the merged one only balances with it out.
    """

    stages = {s["stage"]: s for s in S._refusal_stats(_book())["stages"]}
    inherited = {
        stage: sum(
            r["count"] for r in s["reasons"] if r["reason"] == "chain_never_opened"
        )
        for stage, s in stages.items()
    }
    assert inherited["P0"] == 0, "P0 opens the chain; nothing is inherited there"
    assert inherited["P0-M"] == 1
    assert inherited["P1"] == 2
    # And the per-stage funnel still closes with it in.
    for s in stages.values():
        assert s["scope_count"] == s["seated_count"] + sum(
            r["count"] for r in s["reasons"]
        )
