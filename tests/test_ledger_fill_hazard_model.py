"""The fill model's other half: were we filled when the market reached us.

``through`` asks only whether the book's ask came down to the rung.  It
never asks whether we were the one filled when it did, and that omission is
the whole gap: the ledger kept 62.3% of intended legs against 37.5% for
live maker legs that reached the exchange.

``hazard`` keeps the depth walk as the NECESSARY condition and adds the
sufficient one as a draw against the shadow lane's measured shape, levelled
on the live arm.  Measured on the same three-day replay: 39.8% against the
live arm's 37.5%, from 71.4%.

The first two tests are the ones that matter most.  A research arm that
moves the shipped default's rows, or that moves the shipped default's
fingerprint, has retired the recorded book to say nothing.
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import paper_trading_ledger as ledger  # noqa: E402


def test_default_is_through_and_the_arm_is_inert():
    assert ledger.MAKER_FILL_MODEL == "through"


def test_the_shipped_fingerprint_does_not_carry_the_artifact():
    """Adding a key that is always present retires every recorded row.

    The artifact hash has to be in the identity when the model reads it --
    a refit moves the fill rate and the two books must not pool -- but a
    key present under every model would change the fingerprint of rows
    written by models that never open the file.  Same trap the seat-rule
    key was left out of.
    """

    assert "maker_fill_hazard_sha256" not in ledger._SELECTION_RULE_IDENTITY


def test_the_armed_fingerprint_does_carry_it(monkeypatch):
    monkeypatch.setenv("PAPER_LEDGER_FILL_MODEL", "hazard")
    reloaded = importlib.reload(ledger)
    try:
        assert reloaded.MAKER_FILL_MODEL == "hazard"
        carried = reloaded._SELECTION_RULE_IDENTITY["maker_fill_hazard_sha256"]
        assert carried == (reloaded._fill_hazard().get("content_sha256")
                           or "MISSING")
    finally:
        monkeypatch.delenv("PAPER_LEDGER_FILL_MODEL", raising=False)
        importlib.reload(ledger)


def test_a_missing_artifact_leaves_every_rung_alone(monkeypatch):
    """Absent fit means inert, not zero.

    A model that silently refuses every rung because its table could not be
    read would report an empty book as a strategy result.
    """

    monkeypatch.setattr(ledger, "_FILL_HAZARD", {})
    assert ledger._hazard_keep_probability(
        rule="bid+1t", mid=0.45, lead_hours=40.0) == 1.0


def test_a_pooled_cell_is_exactly_the_level_scalar(monkeypatch):
    """Shape and level are separable, and the shape is one on average.

    That separation is what lets a refit move the level without silently
    re-ranking the cells.
    """

    monkeypatch.setattr(ledger, "_FILL_HAZARD", {
        "level_scalar": 0.5,
        "shape_pooled": {"bid+1t": 0.20},
        "shape_60min": {"bid+1t": {"centre": {"D-2": 0.20}}},
    })
    assert ledger._hazard_keep_probability(
        rule="bid+1t", mid=0.45, lead_hours=40.0) == pytest.approx(0.5)


def test_an_unwritten_cell_falls_back_to_a_tilt_of_one(monkeypatch):
    """A cell too thin to have been fitted is not a guess.

    ``--fit`` drops any cell under 500 orders.  The reader must then use
    the level alone rather than borrow a neighbouring tier's shape.
    """

    monkeypatch.setattr(ledger, "_FILL_HAZARD", {
        "level_scalar": 0.6, "shape_pooled": {"bid+1t": 0.20},
        "shape_60min": {"bid+1t": {"centre": {"D-2": 0.20}}},
    })
    assert ledger._hazard_keep_probability(
        rule="bid+1t", mid=0.06, lead_hours=40.0) == pytest.approx(0.6)


def test_the_shape_follows_the_measured_direction(monkeypatch):
    """Far from settlement a tail rung keeps less than a centre rung.

    Measured on the shadow lane at sixty minutes: tail 6.1% against centre
    12.3% at D-2, and both converge near settlement.  A fit that inverted
    that would be describing a market nobody observed.
    """

    monkeypatch.setattr(ledger, "_FILL_HAZARD", {
        "level_scalar": 0.6, "shape_pooled": {"bid+1t": 0.19},
        "shape_60min": {"bid+1t": {
            "centre": {"D-2": 0.123, "<3h": 0.332},
            "tail": {"D-2": 0.061, "<3h": 0.409},
        }},
    })
    far_centre = ledger._hazard_keep_probability(
        rule="bid+1t", mid=0.45, lead_hours=40.0)
    far_tail = ledger._hazard_keep_probability(
        rule="bid+1t", mid=0.06, lead_hours=40.0)
    near_tail = ledger._hazard_keep_probability(
        rule="bid+1t", mid=0.06, lead_hours=1.0)
    assert far_tail < far_centre
    assert near_tail > far_tail


def test_probability_is_clipped_to_a_probability(monkeypatch):
    monkeypatch.setattr(ledger, "_FILL_HAZARD", {
        "level_scalar": 3.0, "shape_pooled": {"bid+1t": 0.05},
        "shape_60min": {"bid+1t": {"centre": {"D-2": 0.40}}},
    })
    assert ledger._hazard_keep_probability(
        rule="bid+1t", mid=0.45, lead_hours=40.0) == 1.0


def test_the_draw_is_a_pure_function_of_the_leg():
    """A replay has to give the same book every time it is run.

    Two arms that differ only by a switch must differ only by that switch,
    and a draw seeded on the wall clock would make every rebuild its own
    strategy.
    """

    key = "ZSQD|2026-09-07|24°C|2026-09-05T18:00:00Z|0.310000"
    first = ledger._hazard_admits(key, 0.6)
    assert all(ledger._hazard_admits(key, 0.6) is first for _ in range(50))
    # And the draws are spread, not all one answer.
    outcomes = {
        ledger._hazard_admits(f"{key}|{index}", 0.5) for index in range(64)
    }
    assert outcomes == {True, False}


def test_certainty_needs_no_draw():
    assert ledger._hazard_admits("anything", 1.0) is True
    assert ledger._hazard_admits("anything", 0.0) is False


def test_the_fitted_artifact_is_present_and_shaped():
    """The file the arm reads is in the repo, with the halves named apart."""

    body = json.loads(ledger.FILL_HAZARD_ARTIFACT.read_text(encoding="utf-8"))
    assert body["schema"] == "maker_fill_hazard_v1"
    assert body["horizon_minutes"] == 60
    assert 0.0 < float(body["level_scalar"]) <= 1.5
    assert body["shape_60min"]["bid+1t"]["centre"]["D-2"] > 0
    # The level says what it was fitted against, because 144 live legs is
    # not a number to hide inside a model.
    assert body["level_fitted_against"]["live_legs_at_venue"] > 0
