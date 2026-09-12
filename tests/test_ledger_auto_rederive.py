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
