"""Charter 132, stage A: the evidence contract the claim lanes fit under.

Every counterexample in the 2026-09-13 audit (docs/research/EDGE_REALIZATION_
OPTIMIZATION_2026-09-13.md, section 2) is a test here, written so that it
fails on the arithmetic the audit found and passes on the arithmetic that
replaced it:

* charter 128's served belief drops the fitted intercept (item 42);
* the same belief moves when only the cost moves (item 43);
* a lower bound on beta times a negative disagreement flips sign (item 44);
* lambda's as-of fit read settlements after its own as-of date (item 46);
* beta read labels that were not yet knowable, and ``hit or 0`` turned a
  missing label into a loss (item 47);
* an anchor that failed to read became "no bound" (item 48);
* a lambda table that never recorded its seat shape was treated as current
  (item 49) -- here for evidence readers only; see
  ``lambda_vintage_status`` for why serving is not flipped.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target import beta_edge_store as beta_store  # noqa: E402
from bayes_basket_target import effective_edge_store as lambda_store  # noqa: E402
from tests.claim_lane_tables import add_label  # noqa: E402
from bayes_basket_target.basket_kernel.delta_v2 import (  # noqa: E402
    ECONOMIC_HURDLE_V1,
    SEAT_SHAPE_RULE_VERSION,
    beta_belief_v1,
    beta_conditional_edge_v2,
    beta_conditional_edge_v3,
    clears_economic_hurdle_v1,
)


# --------------------------------------------------------------------------
# Items 42-44: the served form
# --------------------------------------------------------------------------

def test_the_served_belief_drops_the_intercept_and_the_prediction_keeps_it():
    """p=.8, q=.6, beta=.3, alpha=-.08 (audit item 42)."""

    p, q = 0.8, 0.6
    served = beta_belief_v1(p, q, 0.3)
    assert served - q == pytest.approx(0.06)
    assert clears_economic_hurdle_v1(expected_payout=served, cost=q)
    fitted = beta_conditional_edge_v2(p, q, [(-0.08, 0.3)] * 50)
    assert fitted["edge_point"] == pytest.approx(-0.02)
    assert fitted["edge_lcb"] < ECONOMIC_HURDLE_V1 * q


def test_charter_128s_belief_moves_with_the_cost_alone():
    """Same claim, same beta, only the cost moves (audit item 43).

    The recorded failure, kept as a statement of what the old form does: a
    dearer price raises the belief in the event.  The valuation lane's vector
    does not take a price at all -- see ``test_charter_132_valuation``.
    """

    assert beta_belief_v1(0.8, 0.6, 0.3) == pytest.approx(0.66)
    assert beta_belief_v1(0.8, 0.7, 0.3) == pytest.approx(0.73)


def test_a_negative_disagreement_does_not_flip_the_bound():
    """d = -.2, beta in [-.8, .4] (audit item 44)."""

    q = 0.6
    p = q - 0.2
    served = beta_belief_v1(p, q, -0.8)
    assert served - q == pytest.approx(0.16)
    draws = [(0.0, -0.8 + 1.2 * k / 99) for k in range(100)]
    # The audit's -.08 is the interval's worst case: quantile 0.
    worst = beta_conditional_edge_v2(p, q, draws, quantile=0.0)
    assert worst["edge_lcb"] == pytest.approx(-0.08)
    # The served 5% quantile sits just inside it and keeps the sign.
    fitted = beta_conditional_edge_v2(p, q, draws)
    assert -0.08 <= fitted["edge_lcb"] < 0.0
    assert fitted["edge_ucb"] <= 0.16 + 1e-9


@pytest.mark.parametrize("d", [-0.3, -0.1, 0.0, 0.1, 0.3])
@pytest.mark.parametrize("alpha", [-0.05, 0.0, 0.05])
def test_the_bound_is_a_quantile_of_the_prediction_for_every_sign(d, alpha):
    """The lower bound never exceeds the point, whatever the sign of d."""

    draws = [(alpha + 0.01 * (k % 7 - 3), 0.1 * (k % 11 - 5)) for k in range(200)]
    got = beta_conditional_edge_v2(0.5 + d, 0.5, draws)
    assert got["edge_lcb"] <= got["edge_point"] <= got["edge_ucb"]


def test_no_draws_serves_nothing():
    assert beta_conditional_edge_v2(0.8, 0.6, []) is None
    assert beta_conditional_edge_v3(0.8, 0.6, []) is None


# --------------------------------------------------------------------------
# v3 (user ruling, 2026-09-17): the market's own slope
# --------------------------------------------------------------------------

def test_a_pair_reads_gamma_zero_and_is_the_v2_prediction():
    draws = [(-0.05 + 0.01 * (k % 5), 0.3 - 0.02 * (k % 7)) for k in range(120)]
    v2 = beta_conditional_edge_v2(0.8, 0.6, draws)
    v3 = beta_conditional_edge_v3(0.8, 0.6, draws, market_centre=0.4)
    for key in ("edge_point", "edge_lcb", "edge_ucb", "disagreement"):
        assert v3[key] == pytest.approx(v2[key])


def test_gamma_moves_the_prediction_by_the_markets_offset_from_the_centre():
    """alpha -0.02, beta 0.3, gamma 0.25, m 0.6 against a centre of 0.4:
    the market sits 0.2 above the centre, so the line adds 0.05."""
    draws = [(-0.02, 0.3, 0.25)] * 50
    got = beta_conditional_edge_v3(0.8, 0.6, draws, market_centre=0.4)
    assert got["market_offset"] == pytest.approx(0.2)
    assert got["edge_point"] == pytest.approx(-0.02 + 0.3 * 0.2 + 0.25 * 0.2)
    below = beta_conditional_edge_v3(0.8, 0.2, draws, market_centre=0.4)
    assert below["edge_point"] == pytest.approx(-0.02 + 0.3 * 0.6 - 0.25 * 0.2)


@pytest.mark.parametrize("d", [-0.3, 0.0, 0.3])
@pytest.mark.parametrize("z", [-0.2, 0.0, 0.2])
def test_the_v3_bound_is_a_quantile_of_the_whole_prediction(d, z):
    draws = [(0.01 * (k % 7 - 3), 0.1 * (k % 11 - 5), 0.05 * (k % 5 - 2)) for k in range(200)]
    got = beta_conditional_edge_v3(0.5 + d, 0.5, draws, market_centre=0.5 - z)
    assert got["edge_lcb"] <= got["edge_point"] <= got["edge_ucb"]
    assert got["market_offset"] == pytest.approx(z)


# --------------------------------------------------------------------------
# Items 47-48: beta's population
# --------------------------------------------------------------------------

def _beta_db(*, with_shape: bool = False) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE basket_candidate_window_scored("
        " candidate_id TEXT PRIMARY KEY, station_id TEXT, event_date TEXT,"
        " stage TEXT, decision_at_utc TEXT, raw_win_probability REAL,"
        " all_in_price REAL, hit INTEGER, settled_bucket TEXT,"
        # Charter 128 v2: the population regresses on the market's belief,
        # so the fixture carries the column the ledger writes it to.
        " settled_at_utc TEXT, market_mid REAL"
        + (", seat_shape_rule TEXT" if with_shape else "")
        + ")"
    )
    beta_store.ensure_schema(conn)
    return conn


def _row(conn, cid, *, day, settled_at, hit=1, decided=None, shape=None):
    values = [
        cid, "S1", day, "P0", decided or f"{day}T06:00:00Z", 0.7, 0.6, hit,
        "20°C", settled_at, 0.55,
    ]
    if shape is not None or _has(conn, "seat_shape_rule"):
        values.append(shape)
    conn.execute(
        "INSERT INTO basket_candidate_window_scored VALUES("
        + ",".join("?" for _ in values) + ")",
        values,
    )
    add_label(conn, "S1", day, settled_bucket="20°C", known_at_utc=settled_at)


def _has(conn, column):
    return any(
        r[1] == column
        for r in conn.execute("PRAGMA table_info(basket_candidate_window_scored)")
    )


def test_a_label_not_yet_knowable_at_the_cutoff_is_not_read():
    conn = _beta_db()
    _row(conn, "a", day="2026-09-04", settled_at="2026-09-05T01:00:00Z")
    _row(conn, "b", day="2026-09-05", settled_at="2026-09-12T01:00:00Z")
    rows, meta = beta_store.population(
        conn, stage_scope="P0", as_of="2026-09-05",
        cutoff_utc="2026-09-06T00:00:00Z", anchored=False,
    )
    assert [r["date"] for r in rows] == ["2026-09-04"]
    assert meta["excluded_label_not_yet_knowable"] == 1


def test_a_decision_after_the_cutoff_is_not_read():
    conn = _beta_db()
    _row(conn, "a", day="2026-09-05", settled_at="2026-09-05T20:00:00Z",
         decided="2026-09-05T19:00:00Z")
    rows, meta = beta_store.population(
        conn, stage_scope="P0", as_of="2026-09-05",
        cutoff_utc="2026-09-05T18:00:00Z", anchored=False,
    )
    assert rows == []
    # The label was not knowable either; one of the two counters must say so.
    assert (
        meta["excluded_label_not_yet_knowable"]
        + meta["excluded_decided_after_cutoff"]
    ) == 1


def test_a_missing_label_is_rejected_not_counted_as_a_loss():
    conn = _beta_db()
    _row(conn, "a", day="2026-09-04", settled_at="2026-09-05T01:00:00Z", hit=None)
    _row(conn, "b", day="2026-09-04", settled_at="2026-09-05T01:00:00Z", hit=1)
    rows, meta = beta_store.population(
        conn, stage_scope="P0", cutoff_utc="2026-09-10T00:00:00Z", anchored=False,
    )
    assert len(rows) == 1 and rows[0]["hit"] == 1.0
    assert meta["rejected_invalid_label"] == 1


def test_a_table_that_cannot_say_when_labels_became_knowable_is_refused():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE basket_candidate_window_scored("
        " candidate_id TEXT, station_id TEXT, event_date TEXT, stage TEXT,"
        " decision_at_utc TEXT, raw_win_probability REAL, all_in_price REAL,"
        " hit INTEGER, settled_bucket TEXT)"
    )
    rows, meta = beta_store.population(conn, stage_scope="P0", anchored=False)
    assert rows == [] and meta["error"] == "no_label_knowable_column"


def test_the_seat_shape_partition_excludes_other_and_unrecorded_shapes():
    conn = _beta_db(with_shape=True)
    _row(conn, "a", day="2026-09-04", settled_at="2026-09-05T01:00:00Z",
         shape=SEAT_SHAPE_RULE_VERSION)
    _row(conn, "b", day="2026-09-04", settled_at="2026-09-05T01:00:00Z",
         shape="old_rule")
    _row(conn, "c", day="2026-09-04", settled_at="2026-09-05T01:00:00Z",
         shape=None)
    rows, meta = beta_store.population(
        conn, stage_scope="P0", cutoff_utc="2026-09-10T00:00:00Z", anchored=False,
    )
    assert len(rows) == 1
    assert meta["excluded_other_shape"] == 2


def test_an_anchor_store_that_fails_to_read_refuses_the_fit(monkeypatch):
    conn = _beta_db()
    conn.execute("CREATE TABLE basket_probability_bridge_history(x)")
    conn.execute("CREATE TABLE basket_probability_envelope(x)")
    _row(conn, "a", day="2026-09-04", settled_at="2026-09-05T01:00:00Z")
    from bayes_basket_target import center_width_store

    def boom(*_a, **_k):
        raise RuntimeError("transient")

    monkeypatch.setattr(center_width_store, "served_arithmetic_anchor", boom)
    rows, meta = beta_store.population(
        conn, stage_scope="P0", cutoff_utc="2026-09-10T00:00:00Z",
    )
    assert rows == [] and meta["error"] == "anchor_unreadable"


def test_an_absent_anchor_store_is_no_bound_and_says_so():
    conn = _beta_db()
    _row(conn, "a", day="2026-09-04", settled_at="2026-09-05T01:00:00Z")
    rows, meta = beta_store.population(
        conn, stage_scope="P0", cutoff_utc="2026-09-10T00:00:00Z",
    )
    assert len(rows) == 1
    assert meta["anchor_states"] == {"P0": "absent"}


def test_the_fit_keeps_joint_draws_only_when_it_serves(monkeypatch):
    import random

    monkeypatch.setattr(beta_store, "_utc_now_iso", lambda: "2026-12-31T00:00:00Z")
    conn = _beta_db()
    rng = random.Random(3)
    n = 0
    for d in range(1, 13):
        day = f"2026-09-{d:02d}"
        for _ in range(12):
            price = rng.uniform(0.4, 0.8)
            mid = price - 0.04
            raw = min(0.99, max(0.01, mid + rng.uniform(-0.2, 0.2)))
            hit = 1 if rng.random() < mid + 0.4 * (raw - mid) else 0
            conn.execute(
                "INSERT INTO basket_candidate_window_scored VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (f"c{n}", f"S{n % 7}", day, "P0", f"{day}T06:00:00Z", raw, price,
                 hit, "20°C", f"{day}T23:00:00Z", mid),
            )
            add_label(conn, f"S{n % 7}", day, settled_bucket="20°C",
                      known_at_utc=f"{day}T23:00:00Z")
            n += 1
    fit = beta_store.fit_beta(conn, stage_scope="P0", anchored=False)
    ev = fit["evidence"]
    assert not fit["accumulating"]
    assert len(ev["joint_draws"]) == beta_store.BOOTSTRAP_DRAWS
    assert ev["intercept_lcb"] <= ev["intercept"] <= ev["intercept_ucb"]
    beta_store.persist_and_activate(conn, fit, now="2026-12-31T00:00:00Z")
    served = beta_store.load_beta_as_of(conn, stage_scope="P0")
    assert len(served["joint_draws"]) == beta_store.BOOTSTRAP_DRAWS
    thin = beta_store.fit_beta(
        conn, stage_scope="P0", anchored=False, window_days=2,
    )
    assert thin["accumulating"] and thin["evidence"]["joint_draws"] == []


# --------------------------------------------------------------------------
# Item 46: lambda's as-of fit has a ceiling
# --------------------------------------------------------------------------

def _lambda_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE basket_candidate_window_scored("
        " candidate_id TEXT PRIMARY KEY, station_id TEXT, event_date TEXT,"
        " stage TEXT, decision_at_utc TEXT, buckets_json TEXT,"
        " win_probability REAL, all_in_price REAL, hit INTEGER,"
        " admissible INTEGER, seated INTEGER, settled_at_utc TEXT,"
        " settled_bucket TEXT)"
    )
    add_label(conn, "P", "2026-09-12")
    return conn


def _lambda_row(conn, cid, *, day, settled_at, station="S1"):
    conn.execute(
        "INSERT INTO basket_candidate_window_scored VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (cid, station, day, "P0", f"{day}T06:00:00Z", '["b"]', 0.7, 0.6, 1, 1, 0,
         settled_at, "b"),
    )
    add_label(conn, station, day, settled_bucket="b", known_at_utc=settled_at)


def test_a_lambda_fit_as_of_a_day_reads_nothing_after_it():
    """The audit's reproduction: as-of 09-05 read a 09-12 settlement."""

    conn = _lambda_db()
    _lambda_row(conn, "a", day="2026-09-04", settled_at="2026-09-05T01:00:00Z")
    _lambda_row(conn, "b", day="2026-09-05", settled_at="2026-09-05T22:00:00Z",
                station="S2")
    _lambda_row(conn, "c", day="2026-09-12", settled_at="2026-09-13T01:00:00Z",
                station="S3")
    got = lambda_store._candidate_population(
        conn, ("P0",), "2026-08-01",
        ceiling_day="2026-09-05", cutoff_utc="2026-09-06T00:00:00Z",
    )
    assert sorted(str(r[4]) for r in got["rows"]) == ["2026-09-04", "2026-09-05"]


def test_a_lambda_label_settled_after_the_cutoff_is_not_read():
    conn = _lambda_db()
    _lambda_row(conn, "a", day="2026-09-05", settled_at="2026-09-09T00:00:00Z")
    got = lambda_store._candidate_population(
        conn, ("P0",), "2026-08-01",
        ceiling_day="2026-09-05", cutoff_utc="2026-09-06T00:00:00Z",
    )
    assert got["rows"] == []
    assert got["excluded_label_not_yet_knowable"] == 1


def test_the_nightly_lambda_population_is_unchanged_without_a_ceiling():
    conn = _lambda_db()
    _lambda_row(conn, "a", day="2026-09-04", settled_at="2026-09-05T01:00:00Z")
    _lambda_row(conn, "c", day="2026-09-12", settled_at="2026-09-13T01:00:00Z",
                station="S3")
    got = lambda_store._candidate_population(
        conn, ("P0",), "2026-08-01",
    )
    assert len(got["rows"]) == 2


# --------------------------------------------------------------------------
# Item 49: an unrecorded seat shape is not the current one, for evidence
# --------------------------------------------------------------------------

def _lambda_table(conn, *, sha, shape):
    lambda_store.ensure_schema(conn)
    payload = {"bands": [{"upper_bound": None, "lambda": 0.9}]}
    if shape is not None:
        payload["seat_shape_rule"] = shape
    conn.execute(
        "INSERT INTO calibration_effective_edge_lambda("
        " lambda_table_sha256, bands_json, evidence_json, fitted_at_utc,"
        " window_days, seat_count, activated, activated_at_utc, stage_scope)"
        " VALUES(?,?,?,?,?,?,?,?,?)",
        (sha, json.dumps(payload), "{}", "2026-09-11T00:00:00Z", 28, 10, 1,
         "2026-09-11T00:00:00Z", "P0"),
    )


@pytest.mark.parametrize(
    "shape, verdict",
    [(None, "unrecorded"), ("old_rule", "other_shape"),
     (SEAT_SHAPE_RULE_VERSION, "current")],
)
def test_the_strict_loader_serves_evidence_only_on_the_current_shape(shape, verdict):
    conn = sqlite3.connect(":memory:")
    _lambda_table(conn, sha="a" * 64, shape=shape)
    bands, got = lambda_store.load_lambda_as_of_strict(conn, stage_scope="P0")
    assert got == verdict
    assert (bands is not None) == (verdict == "current")
    status = lambda_store.lambda_vintage_status(conn)
    assert status["P0/window"]["verdict"] == verdict


def test_the_truth_revision_commit_is_when_a_label_became_knowable():
    """The standing revision's label, knowable at that revision's commit."""

    from bayes_basket_target.label_knowability import (
        SOURCE_TRUTH_COMMIT,
        knowable_labels,
    )

    conn = sqlite3.connect(":memory:")
    add_label(conn, "S1", "2026-08-20", settled_bucket="20°C",
              known_at_utc="2026-08-21T01:00:00Z", truth_revision_no=0)
    add_label(conn, "S1", "2026-08-20", settled_bucket="21°C",
              known_at_utc="2026-08-21T04:15:13Z", truth_revision_no=1)
    got = knowable_labels(conn)
    # Revision 1 moved the bucket: its label, at its own commit, not earlier.
    assert got[("S1", "2026-08-20")]["bucket"] == "21°C"
    assert got[("S1", "2026-08-20")]["known_at"] == "2026-08-21T04:15:13Z"
    assert got[("S1", "2026-08-20")]["source"] == SOURCE_TRUTH_COMMIT
