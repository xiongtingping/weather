"""Charter 132 stage E: the valuation lane inside the enumeration.

Shadow changes no verdict and records the pooled reading on every priced
window; ``on`` holds a stage without an admitted walk-forward verdict and,
with one, gates on the action's lower bound and sizes on the pooled mass.
The same production function the paper ledger calls, so the two lanes read
one valuation.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT, ROOT / "tests"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from test_beta_edge_lane import _snapshot, _windows  # noqa: E402

from bayes_basket_target.basket_kernel.valuation_v1 import (  # noqa: E402
    VALUATION_TABLE_KEY,
    market_reference_v1,
    valuation_vector_v1,
    window_mass,
)


def _served(verdict, *, beta=0.5, draws=None, accumulating=False):
    return {VALUATION_TABLE_KEY: {"P0": {
        "beta": beta, "beta_lcb": beta, "beta_ucb": beta,
        "accumulating": accumulating, "verdict": verdict, "sha256": "v" * 64,
        "beta_draws": tuple(draws if draws is not None else [beta] * 40),
    }}}


def test_shadow_records_the_pooled_reading_and_changes_no_verdict():
    import bayes_basket_target.delta_v2_runtime as R

    assert R.VALUATION_MODE == "shadow"
    plain, _ = _windows(None)
    shadow, audit = _windows(_served("hold"))
    by_plain = {tuple(w["buckets"]): w for w in plain}
    snapshot, probabilities = _snapshot()
    market = market_reference_v1(snapshot.buckets)
    assert market["state"] == "complete"
    vector = valuation_vector_v1(probabilities, market["vector"], 0.5)
    priced = 0
    for w in shadow:
        before = by_plain[tuple(w["buckets"])]
        assert w["admissible"] == before["admissible"]
        assert w["effective_win_probability"] == pytest.approx(
            before["effective_win_probability"]
        )
        if w["all_in_price"] is None:
            continue
        priced += 1
        assert w["valuation_mass"] == pytest.approx(window_mass(vector, w["buckets"]))
        assert w["valuation_market_mass"] == pytest.approx(
            window_mass(market["vector"], w["buckets"])
        )
        assert w["valuation_edge_point"] == pytest.approx(
            w["valuation_mass"] - w["all_in_price"]
        )
        assert w["valuation_edge_lcb"] <= w["valuation_edge_point"] + 1e-12
        assert w["valuation_capacity_shares"] >= 0.0
    assert priced > 0
    assert audit["valuation_mode"] == "shadow"
    assert audit["valuation_state"] == "unverified_hold"
    assert audit["valuation_served"]["verdict"] == "hold"
    assert audit["valuation_market"]["state"] == "complete"


def test_no_fit_and_an_accumulating_fit_record_nothing():
    for tables, state in (
        ({VALUATION_TABLE_KEY: {"P0": None}}, "no_fit"),
        (_served("admitted", accumulating=True), "accumulating"),
    ):
        windows, audit = _windows(tables)
        assert audit["valuation_state"] == state
        assert all(w["valuation_mass"] is None for w in windows)


# Charter 230 retired the lambda / second-belief arm this test pinned.




# Charter 230 retired the lambda / second-belief arm this test pinned.




def test_on_with_a_market_only_pool_is_the_market_and_gates_on_its_bound(monkeypatch):
    """beta 0: the pool is the book's reference, and a window is admitted only
    when that reference clears the hurdle at the ask.  The fixture's ladder is
    under-round (asks sum to 0.80), so a normalised mid can clear an ask here;
    on a real, over-round ladder it almost never does."""

    import bayes_basket_target.delta_v2_runtime as R
    from bayes_basket_target.basket_kernel.delta_v2 import ECONOMIC_HURDLE_V1

    monkeypatch.setattr(R, "VALUATION_MODE", "on")
    windows, _audit = _windows(_served("admitted", beta=0.0))
    priced = [w for w in windows if w["all_in_price"] is not None]
    assert priced
    for w in priced:
        assert w["valuation_mass"] == pytest.approx(w["valuation_market_mass"])
        if w["admissible"]:
            assert w["valuation_edge_lcb"] >= ECONOMIC_HURDLE_V1 * w["all_in_price"] - 1e-9
