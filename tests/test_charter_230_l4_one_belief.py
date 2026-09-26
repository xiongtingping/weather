"""Charter 230: the L4 posterior is the basket's one belief.

The fit is charter 211's training rule (the multiclass log loss of the
renormalised ladder); a row serves its parameters only on its own walk-forward
verdict against the market on both of charter 211's scores; under ``on`` the
window gate, the completions, the incumbent and the solver's partition read
that one vector, the effective-edge lambda is not read, a row without a
verdict serves ``b = m``, and a snapshot without a complete book buys nothing.
"""
from __future__ import annotations

import math
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target import posterior_store as store  # noqa: E402
from bayes_basket_target.basket_kernel import posterior_v1 as kernel  # noqa: E402
from bayes_basket_target.basket_kernel.valuation_v1 import market_reference_v1  # noqa: E402

from tests.test_posterior_lane import _snapshot, _windows  # noqa: E402


# --------------------------------------------------------------------------
# The fit
# --------------------------------------------------------------------------

def _rows(*, days: int, stations: int, informative: str, seed: int = 7) -> list[dict]:
    """One decision per station-day; truth drawn from ``p`` (the model knows
    something the market does not) or from ``m`` (the model is noise)."""

    rng = np.random.default_rng(seed)
    out = []
    start = date(2026, 8, 1)
    for d in range(days):
        day = (start + timedelta(days=d)).isoformat()
        for s in range(stations):
            k = 6
            truth_p = rng.dirichlet(np.ones(k) * 1.5)
            m = 0.5 * truth_p + 0.5 * rng.dirichlet(np.ones(k) * 1.5)
            p = truth_p if informative == "p" else rng.dirichlet(np.ones(k) * 1.5)
            source = truth_p if informative == "p" else m
            label = int(rng.choice(k, p=source / source.sum()))
            out.append({
                "station": f"S{s:02d}", "date": day,
                "at": f"{day}T06:00:00Z",
                "label_known_at": f"{day}T23:00:00Z",
                "lead_band": ">=36h", "weight": 1.0,
                "label": str(label),
                "m": {str(i): float(v) for i, v in enumerate(m)},
                "p": {str(i): float(v) for i, v in enumerate(p)},
            })
    return out


def test_multiclass_fit_maximises_the_renormalised_likelihood() -> None:
    rows = _rows(days=12, stations=10, informative="p")
    design = store._design(rows)
    theta = store._fit_multiclass(design, row_weight=1.0, prior_mean=(0.0, 0.0, 0.0))
    dec_weight = design["w"][design["starts"]]
    prec = 1.0 / np.asarray(store.PRIOR_SD) ** 2
    mu = np.zeros(3)
    loss, grad, _hess = store._multiclass_terms(
        design, theta, dec_weight=dec_weight, prior_mean=mu, prec=prec,
    )
    assert float(np.max(np.abs(grad))) < 1e-6
    binary = store._fit_logistic(design, row_weight=1.0, prior_mean=(0.0, 0.0, 0.0))
    binary_loss, _g, _h = store._multiclass_terms(
        design, binary, dec_weight=dec_weight, prior_mean=mu, prec=prec, want_hessian=False,
    )
    assert loss <= binary_loss + 1e-9
    # The model carries the truth here, so the fit leans on it.
    assert theta[2] > 0.3


def test_verdict_admits_a_row_whose_model_beats_the_market() -> None:
    rows = _rows(days=24, stations=15, informative="p")
    verdicts = store.walk_forward_verdict(rows)
    band = verdicts[">=36h"]
    assert band["rule"] == store.VERDICT_RULE
    assert band["test_days"] >= store.VERDICT_MIN_TEST_DAYS
    assert band["d_logscore"][2] < 0 and band["d_brier"][2] < 0
    assert band["admitted"] is True
    # Every decision read the band row, so the stage row judged nothing.
    assert verdicts[store.WHOLE_STAGE]["admitted"] is False


def test_verdict_holds_a_row_that_does_not_beat_the_market() -> None:
    rows = _rows(days=24, stations=15, informative="m")
    band = store.walk_forward_verdict(rows)[">=36h"]
    assert band["test_days"] >= store.VERDICT_MIN_TEST_DAYS
    assert band["admitted"] is False
    assert band["reason"] == "does_not_beat_market"


def test_the_served_row_carries_its_verdict_and_accumulating_is_never_admitted() -> None:
    rows = _rows(days=24, stations=15, informative="p")
    for row in rows:
        row["lead_band"] = ">=36h"
    fits = []
    verdicts = store.walk_forward_verdict(rows)
    whole = store.fit_posterior(
        None, stage_scope="P0", rows=rows, meta={}, draws=5,
        verdict=verdicts[store.WHOLE_STAGE],
    )
    fits.append(whole)
    band = store.fit_posterior(
        None, stage_scope="P0", rows=rows, meta={}, draws=5, lead_band=">=36h",
        parent=whole["evidence"]["point"], verdict=verdicts[">=36h"],
    )
    assert band["table"]["verdict"]["admitted"] is True
    assert band["evidence"]["verdict"]["admitted"] is True
    thin = store.fit_posterior(
        None, stage_scope="P0", rows=rows[:30], meta={}, draws=5, lead_band=">=36h",
        verdict=verdicts[">=36h"],
    )
    assert thin["accumulating"] is True
    assert thin["table"]["verdict"]["admitted"] is False


# --------------------------------------------------------------------------
# The runtime under ``on``
# --------------------------------------------------------------------------

_PARAMS = {"alpha": 0.0, "gamma": 0.0, "w": 0.5}


def _served(*, admitted: bool) -> dict:
    return {kernel.POSTERIOR_TABLE_KEY: {"P0": {
        **_PARAMS, "w_lcb": 0.3, "w_ucb": 0.7, "sha256": "x" * 64,
        "accumulating": False, "lead_band": "", "admitted": admitted,
        "verdict": {"admitted": admitted, "reason": "admitted" if admitted else "does_not_beat_market"},
        "draws": tuple((0.0, 0.0, 0.3 + 0.02 * i) for i in range(21)),
        "lead_bands": {},
    }}}


@pytest.fixture
def on(monkeypatch):
    import bayes_basket_target.delta_v2_runtime as R

    monkeypatch.setattr(R, "POSTERIOR_MODE", "on")
    return R


def test_on_values_every_window_on_the_admitted_posterior(on) -> None:
    windows, audit = _windows(_served(admitted=True))
    snapshot, probabilities = _snapshot()
    market = market_reference_v1(snapshot.buckets)
    belief = kernel.posterior_vector_v1(probabilities, market["vector"], _PARAMS)
    assert audit["belief_source"] == "l4_posterior"
    assert audit["posterior_state"] == "admitted"
    priced = 0
    for w in windows:
        mass = kernel.window_mass(belief, w["buckets"])
        assert w["win_probability"] == pytest.approx(mass)
        assert w["effective_win_probability"] == pytest.approx(mass)
        # One belief: lambda is not read.
        assert w["effective_edge_lambda"] is None
        if w["all_in_price"] is None:
            continue
        priced += 1
        if w.get("completion") is None and not w.get("structurally_refused"):
            if w["admissible"]:
                assert w["posterior_edge_lcb"] >= 0.05 * w["all_in_price"] - 1e-9
    assert priced > 0


def test_on_a_row_without_a_verdict_serves_the_market(on) -> None:
    windows, audit = _windows(_served(admitted=False))
    snapshot, _probabilities = _snapshot()
    market = market_reference_v1(snapshot.buckets)
    assert audit["belief_source"] == "l4_market_prior"
    assert audit["posterior_state"] == "not_admitted"
    for w in windows:
        assert w["win_probability"] == pytest.approx(
            kernel.window_mass(market["vector"], w["buckets"]), abs=1e-5,
        )
        # b = m is the lane's failure direction: nothing is bought on it,
        # even where this book's offers sum under one and the normalised mid
        # clears an ask.
        assert w["admissible"] is False
    assert audit["posterior_blocked_refused_window_count"] > 0


def test_on_without_a_complete_book_buys_nothing(on, monkeypatch) -> None:
    import bayes_basket_target.delta_v2_runtime as R

    monkeypatch.setattr(
        R, "market_reference_v1",
        lambda buckets: {"state": "incomplete", "reason": "missing_quote:29°C"},
    )
    windows, audit = _windows(_served(admitted=True))
    assert audit["belief_source"] is None
    assert audit["posterior_state"] == "market_missing_quote"
    assert all(w["admissible"] is False for w in windows)


def test_on_the_solver_is_handed_the_belief(on, monkeypatch) -> None:
    import bayes_basket_target.delta_v2_runtime as R

    snapshot, probabilities = _snapshot()
    l4 = R.posterior_belief_v1(
        snapshot=snapshot, probabilities=probabilities, probability_stage="P0",
        hours_to_cutoff=40.0, effective_edge_tables=_served(admitted=True),
    )
    assert l4["belief_source"] == "l4_posterior"
    assert l4["belief"] is not None and l4["belief"] != probabilities
    assert sum(l4["belief"].values()) == pytest.approx(1.0)
    assert len(l4["belief_draws"]) == 21


def test_a_second_belief_is_refused_at_import() -> None:
    code = "import bayes_basket_target.delta_v2_runtime"
    for flag in ("PM_BETA_EDGE", "PM_VALUATION_MODE", "PM_CHARTER_124_ROBUST_CLAIM", "PM_CONVERGENCE_CLAIM"):
        result = subprocess.run(
            [sys.executable, "-c", code], cwd=str(ROOT), capture_output=True, text=True,
            env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(ROOT),
                 "PM_POSTERIOR_LANE": "on", flag: "on"},
            timeout=120,
        )
        assert result.returncode != 0, flag
        assert "charter 230" in result.stderr, (flag, result.stderr[-400:])


def test_production_ships_the_lane_on() -> None:
    """The suite pins ``shadow`` (conftest); production reads the code default."""

    result = subprocess.run(
        [sys.executable, "-c",
         "import bayes_basket_target.basket_kernel.posterior_v1 as k;"
         " import bayes_basket_target.delta_v2_runtime as r;"
         " print(k.POSTERIOR_MODE, r.POSTERIOR_MODE, k.POSTERIOR_RULE)"],
        cwd=str(ROOT), capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(ROOT)}, timeout=120,
    )
    assert result.returncode == 0, result.stderr[-400:]
    assert result.stdout.split() == ["on", "on", "logit_market_prior_multiclass_v2"]



# --------------------------------------------------------------------------
# The production chain under ``on``
# --------------------------------------------------------------------------

def _find_all(obj, key):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key:
                yield v
            yield from _find_all(v, key)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _find_all(v, key)


def _identity_posterior(*, admitted: bool) -> dict:
    """w = 1, c = 0: the posterior IS the served vector, renormalised."""

    return {kernel.POSTERIOR_TABLE_KEY: {"P0": {
        "alpha": 0.0, "gamma": 0.0, "w": 1.0, "w_lcb": 1.0, "w_ucb": 1.0,
        "accumulating": False, "admitted": admitted, "sha256": "z" * 64,
        "lead_band": "", "lead_bands": {},
        "verdict": {"admitted": admitted},
        "draws": tuple((0.0, 0.0, 1.0) for _ in range(5)),
    }}}


def test_on_the_production_chain_opens_only_on_an_admitted_posterior(monkeypatch) -> None:
    import bayes_basket_target.delta_v2_runtime as R
    import bayes_basket_target.production as production
    from tests import test_bayes_basket_target_production_chain_e2e as chain

    monkeypatch.setattr(production, "PRODUCTION_EXECUTION_MODE", "production_no_live")
    monkeypatch.setattr(R, "POSTERIOR_MODE", "on")

    compiled, *_ = chain._p0_opening_compile(
        effective_edge_tables=_identity_posterior(admitted=True),
    )
    assert compiled["status"] == "ready_no_live"
    assert "l4_posterior" in set(_find_all(compiled, "belief_source"))
    assert set(_find_all(compiled, "effective_edge_lambda")) <= {None}
    opened = [
        row for row in compiled["order_sizing_decisions"]
        if row["eligible"] and row["candidate_present"]
    ]
    assert opened

    held, *_ = chain._p0_opening_compile(
        effective_edge_tables=_identity_posterior(admitted=False),
    )
    assert "l4_market_prior" in set(_find_all(held, "belief_source"))
    assert not [
        row for row in held.get("order_sizing_decisions") or ()
        if row["eligible"] and row["candidate_present"]
    ]
