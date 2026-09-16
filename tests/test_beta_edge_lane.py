"""Charter 128: the beta lane -- one slope per stage, walk-forward, by-day
lower bound, shadow by default.

What is pinned: the fit recovers a planted slope and reports a lower bound
below it; below the hurdles it serves 0 and says so; the fit as of a day
reads nothing after that day; the enumeration records the beta belief on
every priced window and, in shadow, changes no verdict; under ``on`` the
gate reads ``beta_lcb * (p - q)`` and a stage with no fit refuses; the
ledger hashes the mode; the nightly sweep runs the refit.
"""
from __future__ import annotations

import random
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target import beta_edge_store as store  # noqa: E402
from bayes_basket_target.basket_kernel.delta_v2 import (  # noqa: E402
    BETA_TABLE_KEY,
    beta_belief_v1,
    beta_lead_band,
    clears_economic_hurdle_v1,
)

# Charter 140: these fixtures build the paper candidate table, and the estimator
# under test does not depend on the source; see ``ledger_evidence_source`` in
# conftest.
pytestmark = pytest.mark.usefixtures("ledger_evidence_source")


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch):
    """The fixture's settlements are dated September 2026; a population cut
    at the wall clock would read a different number of them on every day the
    suite runs.  The clock is the test's, not the calendar's."""

    monkeypatch.setattr(store, "_utc_now_iso", lambda: "2026-12-31T00:00:00Z")


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE paper_trade_candidate_window("
        " candidate_id TEXT PRIMARY KEY, station_id TEXT, event_date TEXT,"
        " stage TEXT, decision_at_utc TEXT, raw_win_probability REAL,"
        # Charter 132: the population reads when a label became knowable,
        # so the fixture carries the production column that says so.
        " all_in_price REAL, hit INTEGER, settled_bucket TEXT,"
        # Charter 128 v2: the market's belief and the clock, the lane's two
        # regressors, as the ledger writes them.
        " settled_at_utc TEXT, market_mid REAL, hours_to_cutoff REAL)"
    )
    store.ensure_schema(conn)
    return conn


def _plant(conn, *, days: int, stations: int = 20, beta: float = 0.5,
           intercept: float = -0.05, noise: float = 0.1, seed: int = 1,
           stage: str = "P0", first_day: int = 1,
           hours: float | None = None, spread: float = 0.04,
           prefix: str = "c") -> None:
    """Rows whose outcome follows ``hit - m = intercept + beta * (p - m)``
    on the market's belief ``m``; the all-in price sits ``spread`` above it,
    the way an ask sits above a mid, and plays no part in the outcome."""

    rng = random.Random(seed)
    n = 0
    for d in range(first_day, first_day + days):
        day = f"2026-09-{d:02d}"
        for s in range(stations):
            mid = rng.uniform(0.4, 0.9)
            price = min(0.99, mid + spread)
            x = rng.uniform(-0.3, 0.3)
            raw = min(0.99, max(0.01, mid + x))
            y = intercept + beta * (raw - mid) + rng.gauss(0.0, noise)
            hit = 1 if rng.random() < max(0.0, min(1.0, mid + y)) else 0
            conn.execute(
                "INSERT INTO paper_trade_candidate_window VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"{prefix}{n}", f"S{s:02d}", day, stage, f"{day}T00:00:00Z",
                 raw, price, hit, "29°C", f"{day}T23:30:00Z", mid, hours),
            )
            n += 1
    conn.commit()


def test_the_fit_recovers_a_planted_slope_with_a_bound_below_it() -> None:
    conn = _db()
    # Bernoulli outcomes: the slope's standard error is about
    # sqrt(Var(hit) / (n Var(x))) ~ 0.06 at n = 1680, so the tolerance is
    # three of those, not a claim about the estimator's precision.
    _plant(conn, days=14, stations=120, beta=0.5, noise=0.05)
    fit = store.fit_beta(conn, stage_scope="P0")
    ev = fit["evidence"]
    assert not fit["accumulating"]
    assert fit["row_count"] == 14 * 120 and fit["day_count"] == 14
    assert abs(ev["beta"] - 0.5) < 0.2
    assert ev["beta_lcb"] < ev["beta"] < ev["beta_ucb"]
    assert ev["served_beta_lcb"] == ev["beta_lcb"]
    assert abs(ev["intercept"] - (-0.05)) < 0.06
    assert fit["table"]["beta_lcb"] == ev["served_beta_lcb"]


def test_a_zero_slope_serves_a_bound_at_or_below_zero() -> None:
    conn = _db()
    _plant(conn, days=14, stations=120, beta=0.0, noise=0.05, seed=3)
    fit = store.fit_beta(conn, stage_scope="P0")
    assert abs(fit["evidence"]["beta"]) < 0.2
    assert fit["evidence"]["beta_lcb"] <= 0.05


def test_below_the_hurdles_it_serves_zero_and_says_so() -> None:
    conn = _db()
    _plant(conn, days=5, stations=40)
    fit = store.fit_beta(conn, stage_scope="P0")
    assert fit["accumulating"] is True
    assert fit["table"]["beta_lcb"] == 0.0
    assert fit["evidence"]["beta"] is None


def test_the_fit_as_of_a_day_reads_nothing_after_it() -> None:
    conn = _db()
    _plant(conn, days=12, stations=10)
    fit = store.fit_beta(conn, stage_scope="P0", as_of="2026-09-06")
    assert fit["row_count"] == 6 * 10
    assert fit["evidence"]["as_of_event_date"] == "2026-09-06"


def test_persist_activate_and_read_as_of_the_journal() -> None:
    conn = _db()
    _plant(conn, days=14, stations=30, beta=0.6, seed=5)
    first = store.fit_beta(conn, stage_scope="P0", as_of="2026-09-10")
    store.persist_and_activate(conn, first, now="2026-09-10T06:00:00Z")
    store.journal_fit(conn, fit=first, outcome="ACTIVATED",
                      fitted_at_utc="2026-09-10T06:00:00Z")
    second = store.fit_beta(conn, stage_scope="P0", as_of="2026-09-14")
    store.persist_and_activate(conn, second, now="2026-09-14T06:00:00Z")
    store.journal_fit(conn, fit=second, outcome="ACTIVATED",
                      fitted_at_utc="2026-09-14T06:00:00Z")
    assert first["content_sha256"] != second["content_sha256"]
    active = store.load_beta_as_of(conn, stage_scope="P0")
    assert active["sha256"] == second["content_sha256"]
    then = store.load_beta_as_of(
        conn, stage_scope="P0", as_of_utc="2026-09-12T00:00:00Z"
    )
    assert then["sha256"] == first["content_sha256"]
    assert store.load_beta_as_of(conn, stage_scope="P1") is None
    tables = store.load_beta_tables(conn)
    assert set(tables) == {"P0", "P0-M", "P1"}
    assert tables["P0"]["beta_lcb"] == second["table"]["beta_lcb"]
    assert conn.execute(
        "SELECT COUNT(*) FROM calibration_beta_edge_table WHERE activated=1"
    ).fetchone()[0] == 1


def test_the_served_tables_mapping_carries_the_lane_under_its_own_key() -> None:
    from bayes_basket_target.effective_edge_store import load_effective_edge_tables

    conn = _db()
    tables = load_effective_edge_tables(conn)
    assert BETA_TABLE_KEY in tables
    assert set(tables[BETA_TABLE_KEY]) == {"P0", "P0-M", "P1"}
    assert all(v is None for v in tables[BETA_TABLE_KEY].values())


def test_the_belief_is_the_price_at_zero_and_the_claim_at_one() -> None:
    assert beta_belief_v1(0.9, 0.8, 0.0) == pytest.approx(0.8)
    assert beta_belief_v1(0.9, 0.8, 1.0) == pytest.approx(0.9)
    assert beta_belief_v1(0.9, 0.8, 0.5) == pytest.approx(0.85)
    assert beta_belief_v1(0.2, 0.8, 0.5) == pytest.approx(0.5)


def _snapshot():
    from bayes_basket_target.basket_kernel.contracts import TargetBasketSnapshot

    labels = ["29°C", "30°C", "31°C", "32°C", "33°C", "34°C"]
    probs = [0.06, 0.20, 0.30, 0.20, 0.16, 0.08]
    asks = [0.05, 0.15, 0.30, 0.15, 0.10, 0.05]
    rows = [
        {
            "bucket": b, "ordinal": i + 2, "probability": p,
            "bid_price": max(0.0, a - 0.01),
            "bid_levels": [{"price": max(0.001, a - 0.01), "shares": 50.0}],
            "ask_levels": [{"price": a, "shares": 100.0}],
            "price_tick_size": 0.01,
            "maker_queue_ahead_shares": 0.0, "maker_queue_known": True,
        }
        for i, (b, p, a) in enumerate(zip(labels, probs, asks))
    ]
    snapshot = TargetBasketSnapshot.from_mapping({
        "schema": "target_basket_snapshot_v6", "snapshot_id": "c128", "sequence": 1,
        "observed_at": "2026-09-12T00:00:00Z", "market_id": "m", "station_id": "X",
        "event_date": "2026-09-13", "quote_version": "q",
        "quote_observed_at": "2026-09-12T00:00:00Z", "quote_source": "OFFICIAL_CLOB_WS",
        "inventory_version": "i", "order_state_version": "o",
        "evidence": {"probability_stage": "P0", "posterior_version": "p", "posterior_sha256": "a" * 64},
        "buckets": rows, "checkpoint_changed": True, "quote_changed": True,
        "fill_changed": True, "time_gate_changed": True, "evidence_fresh": True,
        "contract_matches": True, "order_state_known": True, "risk_inputs_complete": True,
        "time_left_minutes": 600.0,
    })
    return snapshot, dict(zip(labels, probs))


def _windows(beta_tables, **overrides):
    from bayes_basket_target.delta_v2_runtime import _support_windows

    snapshot, probabilities = _snapshot()
    kwargs = dict(
        snapshot=snapshot, desired={"30°C", "31°C", "32°C"},
        probabilities=probabilities, fee_rate=0.0,
        maximum_support_buckets=4, minimum_support_buckets=3,
        center_bucket="31°C", probability_stage="P0",
        effective_edge_tables=beta_tables,
    )
    kwargs.update(overrides)
    return _support_windows(**kwargs)


def test_shadow_records_the_beta_reading_and_changes_no_verdict() -> None:
    import bayes_basket_target.delta_v2_runtime as R

    assert R.BETA_EDGE_MODE == "shadow"
    plain, _ = _windows(None)
    served = {BETA_TABLE_KEY: {"P0": {"beta": 0.5, "beta_lcb": 0.2, "sha256": "x" * 64, "accumulating": False}}}
    shadow, audit = _windows(served)
    by_plain = {tuple(w["buckets"]): w for w in plain}
    priced = 0
    for w in shadow:
        before = by_plain[tuple(w["buckets"])]
        assert w["admissible"] == before["admissible"]
        assert w["effective_win_probability"] == pytest.approx(before["effective_win_probability"])
        if w["all_in_price"] is None:
            assert w["beta_edge"] is None
            continue
        priced += 1
        raw = w["raw_win_probability"]
        # v2 basis: the disagreement is read against the market's belief
        # for the window, and the belief is judged against the all-in ask.
        assert 0.0 < w["beta_market_mid"] < 1.0
        expected = beta_belief_v1(raw, w["beta_market_mid"], 0.2)
        assert w["beta_lcb"] == pytest.approx(0.2)
        assert w["beta_point"] == pytest.approx(0.5)
        assert w["beta_belief"] == pytest.approx(expected)
        assert w["beta_edge"] == pytest.approx(expected - w["all_in_price"])
        assert w["beta_admissible"] == (0.2 > 1e-9 and clears_economic_hurdle_v1(
            expected_payout=expected, cost=w["all_in_price"],
        ))
        assert w["beta_table_sha256"] == "x" * 64
        assert w["beta_lead_band"] is None
    assert priced > 0
    assert audit["beta_edge_mode"] == "shadow"
    assert audit["beta_price_basis"] == store.PRICE_BASIS
    assert audit["beta_market_state"] == "complete"
    assert audit["beta_served"]["P0"]["beta_lcb"] == pytest.approx(0.2)
    assert audit["beta_would_refuse_count"] + audit["beta_would_admit_count"] >= 0
    # No fit for the stage: shadow records nothing and changes nothing.
    none, audit_none = _windows({BETA_TABLE_KEY: {"P0": None}})
    assert all(w["beta_edge"] is None for w in none)
    assert audit_none["beta_would_refuse_count"] == 0


def test_on_reads_the_beta_belief_and_a_missing_fit_refuses(monkeypatch) -> None:
    import bayes_basket_target.delta_v2_runtime as R

    monkeypatch.setattr(R, "BETA_EDGE_MODE", "on")
    # No fit at all: beta_lcb 0, the belief is the market's own, and the
    # lane buys nothing on it -- whatever the book's arithmetic says (this
    # fixture's asks sum under one, so the normalised mid clears the ask on
    # every window; that is the book's quirk, not the model's evidence).
    windows, audit = _windows({BETA_TABLE_KEY: {"P0": None}})
    assert audit["beta_edge_mode"] == "on"
    assert not any(w["admissible"] for w in windows if w["all_in_price"] is not None)
    for w in windows:
        if w["all_in_price"] is not None:
            assert w["beta_lcb"] == 0.0
            assert w["beta_belief"] == pytest.approx(w["beta_market_mid"], abs=1e-6)
            assert w["beta_admissible"] is False
            assert w["effective_win_probability"] <= 1e-6
            assert w["effective_edge_lambda_source"] == "beta_edge_lcb_zero"
    # beta_lcb 1: the belief is the raw claim, so a window is admissible
    # exactly when its raw claim clears its price (and the structural gates).
    served = {BETA_TABLE_KEY: {"P0": {"beta": 1.0, "beta_lcb": 1.0, "sha256": "y" * 64, "accumulating": False}}}
    windows, _ = _windows(served)
    for w in windows:
        if w["all_in_price"] is None:
            continue
        assert w["effective_win_probability"] == pytest.approx(w["raw_win_probability"], abs=1e-6)
        # The solver's lambda is the ratio that reproduces the belief.
        assert w["effective_edge_lambda"] * w["win_probability"] == pytest.approx(w["raw_win_probability"], abs=1e-6)
    seated = [w for w in windows if w["admissible"]]
    assert seated, "a raw claim above its price must seat under beta_lcb = 1"
    assert all(w["raw_win_probability"] > w["all_in_price"] for w in seated)


def test_a_band_row_serves_only_when_it_clears_the_hurdles() -> None:
    """The second axis: the decision's lead band picks a band row when that
    row is past the hurdles; an accumulating band, or no clock, reads the
    stage's whole row."""

    served = {BETA_TABLE_KEY: {"P0": {
        "beta": 0.3, "beta_lcb": 0.2, "sha256": "w" * 64, "accumulating": False,
        "lead_bands": {
            "<6h": {"beta": 0.9, "beta_lcb": 0.8, "sha256": "b" * 64, "accumulating": False},
            "12-24h": {"beta": 0.9, "beta_lcb": 0.9, "sha256": "c" * 64, "accumulating": True},
        },
    }}}
    assert beta_lead_band(3.0) == "<6h" and beta_lead_band(15.0) == "12-24h"
    assert beta_lead_band(None) is None
    near, audit = _windows(served, hours_to_cutoff=3.0)
    priced = [w for w in near if w["all_in_price"] is not None]
    assert priced and all(w["beta_lead_band"] == "<6h" for w in priced)
    assert all(w["beta_lcb"] == pytest.approx(0.8) for w in priced)
    assert all(w["beta_table_sha256"] == "b" * 64 for w in priced)
    assert audit["beta_lead_band"] == "<6h" and audit["beta_hours_to_cutoff"] == 3.0
    assert audit["beta_served"]["P0"]["lead_bands"]["<6h"]["beta_lcb"] == pytest.approx(0.8)
    mid, _ = _windows(served, hours_to_cutoff=15.0)
    priced = [w for w in mid if w["all_in_price"] is not None]
    assert all(w["beta_lead_band"] is None and w["beta_lcb"] == pytest.approx(0.2) for w in priced)
    unclocked, _ = _windows(served)
    priced = [w for w in unclocked if w["all_in_price"] is not None]
    assert all(w["beta_lead_band"] is None and w["beta_lcb"] == pytest.approx(0.2) for w in priced)


def test_without_a_market_belief_shadow_records_nothing_and_on_refuses(monkeypatch) -> None:
    import bayes_basket_target.delta_v2_runtime as R

    monkeypatch.setattr(
        R, "market_reference_v1",
        lambda buckets: {"state": "incomplete", "reason": "one_sided_ask:31°C"},
    )
    served = {BETA_TABLE_KEY: {"P0": {"beta": 0.5, "beta_lcb": 0.4, "sha256": "x" * 64, "accumulating": False}}}
    shadow, audit = _windows(served)
    assert audit["beta_market_state"] == "incomplete"
    assert all(w["beta_belief"] is None and w["beta_market_mid"] is None for w in shadow)
    monkeypatch.setattr(R, "BETA_EDGE_MODE", "on")
    on, _ = _windows(served)
    priced = [w for w in on if w["all_in_price"] is not None]
    assert priced and not any(w["admissible"] for w in priced)
    assert all(w["effective_edge_lambda_source"] == "beta_edge_no_market" for w in priced)


def test_a_row_fitted_on_another_basis_is_never_served() -> None:
    conn = _db()
    _plant(conn, days=14, stations=30, beta=0.6, seed=5)
    fit = store.fit_beta(conn, stage_scope="P0")
    store.persist_and_activate(conn, fit, now="2026-09-15T06:00:00Z")
    store.journal_fit(conn, fit=fit, outcome="ACTIVATED", fitted_at_utc="2026-09-15T06:00:00Z")
    assert store.load_beta_as_of(conn, stage_scope="P0") is not None
    conn.execute(
        "UPDATE calibration_beta_edge_table SET price_basis='ledger_all_in_price'"
    )
    conn.commit()
    assert store.load_beta_as_of(conn, stage_scope="P0") is None
    assert store.load_beta_as_of(
        conn, stage_scope="P0", as_of_utc="2026-09-16T00:00:00Z"
    ) is None
    assert store.load_beta_tables(conn)["P0"] is None


def test_the_band_fits_are_cut_from_one_population_and_serve_per_band() -> None:
    conn = _db()
    # Two bands with different slopes: the near band realises most of the
    # disagreement, the far band almost none.
    _plant(conn, days=14, stations=60, beta=0.8, noise=0.05, seed=11, hours=3.0)
    _plant(conn, days=14, stations=60, beta=0.1, noise=0.05, seed=12, hours=15.0,
           prefix="d")
    fits = store.fit_stage_and_bands(conn, stage_scope="P0")
    by_band = {f["lead_band"]: f for f in fits}
    assert set(by_band) == {store.WHOLE_STAGE, *store.LEAD_BAND_LABELS}
    near, far = by_band["<6h"], by_band["12-24h"]
    assert not near["accumulating"] and not far["accumulating"]
    assert abs(near["evidence"]["beta"] - 0.8) < 0.25
    assert abs(far["evidence"]["beta"] - 0.1) < 0.25
    assert near["evidence"]["beta_lcb"] > far["evidence"]["beta"]
    assert by_band["<6h"]["evidence"]["layers"] == {}
    assert by_band[store.WHOLE_STAGE]["evidence"]["layers"]["by_lead"].keys() >= {"<6h", "12-24h"}
    assert by_band["6-12h"]["accumulating"] and by_band["6-12h"]["row_count"] == 0
    for fit in fits:
        store.persist_and_activate(conn, fit, now="2026-09-15T06:00:00Z")
    tables = store.load_beta_tables(conn)
    served = tables["P0"]
    assert served["lead_band"] == store.WHOLE_STAGE
    assert set(served["lead_bands"]) == set(store.LEAD_BAND_LABELS)
    assert served["lead_bands"]["<6h"]["beta_lcb"] == near["table"]["beta_lcb"]
    assert served["lead_bands"]["6-12h"]["accumulating"] is True
    assert conn.execute(
        "SELECT COUNT(*) FROM calibration_beta_edge_table WHERE activated=1"
    ).fetchone()[0] == 1 + len(store.LEAD_BAND_LABELS)


def test_the_ledger_hashes_the_mode_and_the_sweep_runs_the_refit() -> None:
    ledger = (ROOT / "scripts" / "paper_trading_ledger.py").read_text(encoding="utf-8")
    assert '"beta_edge_mode": BETA_EDGE_MODE' in ledger
    assert '"beta_edge_rule": BETA_EDGE_RULE_VERSION' in ledger
    sweep = (ROOT / "scripts" / "calibration_refit_sweep.sh").read_text(encoding="utf-8")
    assert "refit_beta_edge.py --commit" in sweep
    cli = (ROOT / "scripts" / "refit_beta_edge.py").read_text(encoding="utf-8")
    assert "persist_and_activate" in cli and "journal_fit" in cli
