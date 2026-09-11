"""The simulator may know the outcome; the strategy may not.

Modelling an informed counterparty requires the fill model to know how the
day settled -- adverse selection IS the statement that the fill probability
depends on the outcome -- and a strategy that knows how the day settled is
worthless.  So the two have to be separated by construction rather than by
care, and these tests hold that construction:

* the settled bucket is closed over in ``adverse_fill_predicate``, outside
  the walk, and what the walk receives is a function of (bucket, rung);
* ``_resting_ladder_fill`` therefore has no name for the truth in its whole
  body, which is asserted off the syntax tree rather than by reading;
* every other fill model passes ``None`` and is byte-identical.

Measured inputs, for the record: live maker legs from 2026-09-05, 12/32 on
the leg that won against 58/101 on the legs that lost; this book's own
crossing rates over 2026-08-28..09-10, 106/153 and 371/517.
"""
from __future__ import annotations

import ast
import importlib
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import paper_trading_ledger as ledger  # noqa: E402

SOURCE = (ROOT / "scripts" / "paper_trading_ledger.py").read_text(
    encoding="utf-8"
)


def _function(name: str) -> ast.FunctionDef:
    for node in ast.parse(SOURCE).body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError("not a module-level function: " + name)


def test_the_walk_has_no_name_for_the_truth() -> None:
    """The firewall, read off the syntax tree.

    Not "the walk must not misuse the outcome" -- the walk must not be able
    to.  Any of these names appearing inside it is a leak waiting for one
    careless line, so the test fails on the name, not on the misuse.
    """

    walk = _function("_resting_ladder_fill")
    forbidden = {
        "truth",
        "settled_bucket",
        "_settled_bucket",
        "adverse_fill_predicate",
        "tmax_c",
        "ADVERSE_FILL_P_WIN",
        "ADVERSE_FILL_P_LOSE",
    }
    seen = set()
    for node in ast.walk(walk):
        if isinstance(node, ast.Name) and node.id in forbidden:
            seen.add(node.id)
        if isinstance(node, ast.Attribute) and node.attr in forbidden:
            seen.add(node.attr)
        if isinstance(node, ast.arg) and node.arg in forbidden:
            seen.add(node.arg)
    assert not seen, (
        "the resting walk can see the outcome through: " + str(sorted(seen))
    )


def test_the_predicate_is_the_only_reader_of_the_calibration() -> None:
    predicate = _function("adverse_fill_predicate")
    body = ast.unparse(predicate)
    assert "ADVERSE_FILL_P_WIN" in body and "ADVERSE_FILL_P_LOSE" in body
    # And nothing else in the file reads them except the fingerprint, which
    # records what ran.
    readers = set()
    for node in ast.walk(ast.parse(SOURCE)):
        if isinstance(node, ast.FunctionDef):
            if any(
                isinstance(inner, ast.Name)
                and inner.id.startswith("ADVERSE_FILL_P_")
                for inner in ast.walk(node)
            ):
                readers.add(node.name)
    assert readers == {"adverse_fill_predicate"}, readers


def test_the_draw_is_deterministic_and_survives_a_replay() -> None:
    first = ledger.adverse_fill_predicate(
        station_id="KORD", event_date="2026-09-10", settled_bucket="78-79°F",
    )
    second = ledger.adverse_fill_predicate(
        station_id="KORD", event_date="2026-09-10", settled_bucket="78-79°F",
    )
    for bucket in ("76-77°F", "78-79°F", "80-81°F"):
        for rung in (0.07, 0.30, 0.32):
            assert first(bucket, rung) is second(bucket, rung)
    # Asked twice in one order's life, the same answer: a rung the market
    # refused at a price stays refused at that price.
    admits = first("78-79°F", 0.07)
    for _ in range(5):
        assert first("78-79°F", 0.07) is admits


def test_a_reprice_is_a_new_question() -> None:
    """A new price is a new order, so it gets its own draw."""

    admits = ledger.adverse_fill_predicate(
        station_id="KORD", event_date="2026-09-10", settled_bucket="78-79°F",
        p_win=0.5, p_lose=0.5,
    )
    answers = {admits("78-79°F", rung) for rung in (
        0.05, 0.06, 0.07, 0.08, 0.09, 0.10, 0.11, 0.12
    )}
    assert answers == {True, False}


def test_the_winning_leg_is_refused_more_often_than_the_losing_legs() -> None:
    """The whole point, over enough legs to see it."""

    wins = loses = 0
    win_n = lose_n = 0
    for index in range(4000):
        admits = ledger.adverse_fill_predicate(
            station_id="S%04d" % index, event_date="2026-09-10",
            settled_bucket="30C",
        )
        wins += admits("30C", 0.30)
        win_n += 1
        loses += admits("31C", 0.30)
        lose_n += 1
    win_rate, lose_rate = wins / win_n, loses / lose_n
    # Within a couple of points of the served calibration, and ordered.
    assert abs(win_rate - ledger.ADVERSE_FILL_P_WIN) < 0.03, win_rate
    assert abs(lose_rate - ledger.ADVERSE_FILL_P_LOSE) < 0.03, lose_rate
    assert win_rate < lose_rate


def test_an_unsettled_day_is_unconditioned_rather_than_refused() -> None:
    """No truth is not "never fills"; it is the model we had before."""

    admits = ledger.adverse_fill_predicate(
        station_id="KORD", event_date="2026-09-12", settled_bucket=None,
    )
    for bucket in ("30C", "31C", "32C"):
        assert admits(bucket, 0.30) is True


def test_the_degenerate_calibrations_do_not_consult_the_hash() -> None:
    always = ledger.adverse_fill_predicate(
        station_id="KORD", event_date="2026-09-10", settled_bucket="30C",
        p_win=1.0, p_lose=1.0,
    )
    never = ledger.adverse_fill_predicate(
        station_id="KORD", event_date="2026-09-10", settled_bucket="30C",
        p_win=0.0, p_lose=0.0,
    )
    assert always("30C", 0.3) is True and always("31C", 0.3) is True
    assert never("30C", 0.3) is False and never("31C", 0.3) is False


def test_the_crossing_stays_necessary_under_every_model() -> None:
    """The predicate can only ever refuse a fill, never create one."""

    walk = ast.unparse(_function("_resting_ladder_fill"))
    # Both fill loops keep their price condition and gain a break.
    assert "price <= rungs[0] + 1e-09" in walk
    assert "price < rungs[0] - 1e-09" in walk
    assert walk.count("_admitted(rungs[0])") == 2


def test_only_the_adverse_model_builds_a_predicate() -> None:
    backfill = ast.unparse(_function("backfill"))
    assert "_fill_admits = None" in backfill
    assert "if MAKER_FILL_MODEL == 'adverse':" in backfill
    assert "fill_admits=_fill_admits" in backfill


def test_the_calibration_enters_the_fingerprint_only_when_it_runs() -> None:
    def _sha(model: str | None) -> str:
        previous = os.environ.get("PAPER_LEDGER_FILL_MODEL")
        if model is None:
            os.environ.pop("PAPER_LEDGER_FILL_MODEL", None)
        else:
            os.environ["PAPER_LEDGER_FILL_MODEL"] = model
        try:
            return importlib.reload(ledger).SELECTION_RULE_SHA256
        finally:
            if previous is None:
                os.environ.pop("PAPER_LEDGER_FILL_MODEL", None)
            else:
                os.environ["PAPER_LEDGER_FILL_MODEL"] = previous
            importlib.reload(ledger)

    default = _sha(None)
    adverse = _sha("adverse")
    assert default != adverse
    assert _sha(None) == default


def test_an_unknown_fill_model_is_refused() -> None:
    import pytest  # noqa: PLC0415

    previous = os.environ.get("PAPER_LEDGER_FILL_MODEL")
    os.environ["PAPER_LEDGER_FILL_MODEL"] = "hopeful"
    try:
        with pytest.raises(SystemExit):
            importlib.reload(ledger)
    finally:
        if previous is None:
            os.environ.pop("PAPER_LEDGER_FILL_MODEL", None)
        else:
            os.environ["PAPER_LEDGER_FILL_MODEL"] = previous
        importlib.reload(ledger)


def test_the_served_calibration_is_the_measured_one() -> None:
    """Pinned by the arithmetic, so a typo cannot pass as a refit.

    Two steps, both recorded: the live rate over this book's own crossing
    rate, then scaled by what the first arm actually produced -- a rung
    refused at one price is asked again when it is repriced, so a leg gets
    more than one draw and the first pair came back high.
    """

    first_win = 0.3750 / (106 / 153)
    first_lose = 58 / 101 / (371 / 517)
    assert round(first_win, 4) == 0.5413
    assert round(first_lose, 4) == 0.8002
    assert round(first_win * 0.375 / (70 / 158), 4) == (
        ledger.ADVERSE_FILL_P_WIN
    )
    assert round(first_lose * (58 / 101) / (369 / 534), 4) == (
        ledger.ADVERSE_FILL_P_LOSE
    )
    # Ordered, or the model would be saying the winner is easier to buy.
    assert ledger.ADVERSE_FILL_P_WIN < ledger.ADVERSE_FILL_P_LOSE
