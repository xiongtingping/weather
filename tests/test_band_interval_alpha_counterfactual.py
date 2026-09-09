"""The interval width is measurable through the fit, and only measurable.

``band_reliability_store`` serves the end of a by-day bootstrap interval
nearest one, so the width of that interval IS how hard a table is allowed to
pull.  Its own comment says the width is a parameter precisely so the
counterfactual runs through this code path rather than a second copy of it --
which matters because a hand-rolled replay does not have production's centre
coordinate, and on 2026-09-09 a replay that guessed it put the prize at
+0.0497 nat where the shipped end had banked +0.0155.

Two properties keep that measurable without making it a back door:

  the width reaches the fit, so a different alpha actually changes the served
  factor rather than being accepted and ignored;

  a non-default width cannot be committed, because the constants are a ruling
  and a flag must not activate a table nobody ruled on.

The forward-window number is asserted too.  Every stage but P0 earns its
cells against the forward window one at a time, so P0 -- the stage whose
table moves the most -- had no forward number anywhere; the fit now reports
one for the whole table, gating nothing.
"""

import inspect

import pytest

from bayes_basket_target.band_reliability_store import (
    DEFAULT_INTERVAL_ALPHA,
    fit_band_reliability_table,
    refit_all_stages,
)
from bayes_basket_target.contracts import ContractError


def test_interval_alpha_reaches_the_fit() -> None:
    # Not a style check: an alpha the caller accepts and drops would make
    # every counterfactual read exactly like the shipped fit.
    assert "interval_alpha" in inspect.signature(refit_all_stages).parameters
    assert (
        "interval_alpha"
        in inspect.signature(fit_band_reliability_table).parameters
    )
    source = inspect.getsource(refit_all_stages)
    assert "interval_alpha=interval_alpha" in source


def test_a_counterfactual_width_cannot_be_committed() -> None:
    with pytest.raises(ContractError) as excinfo:
        refit_all_stages(
            None,  # never reached: the guard runs before any query
            commit=True,
            interval_alpha=0.5,
        )
    assert "not_committable" in str(excinfo.value)


def test_the_shipped_width_still_commits() -> None:
    # The guard must not have closed the ordinary path.  A connection of
    # None makes the first real query raise, which is far enough: reaching
    # a database error proves the width guard let this through.
    with pytest.raises(Exception) as excinfo:
        refit_all_stages(None, commit=True, interval_alpha=DEFAULT_INTERVAL_ALPHA)
    assert "not_committable" not in str(excinfo.value)


def test_fit_reports_its_width_and_a_forward_number() -> None:
    source = inspect.getsource(fit_band_reliability_table)
    assert '"interval_alpha": float(interval_alpha)' in source
    assert '"forward_nll_delta_vs_identity"' in source
    # Reported, not gating, and deliberately outside ``evidence`` so a new
    # diagnostic cannot churn a served table's sha.
    evidence_block = source.split('"evidence": {', 1)[1]
    assert "forward_nll_delta_vs_identity" not in evidence_block
