"""The off-centre claim correction refits itself, on the axis it serves.

Two hand fits stood before this lane: 2026-08-14, by code that was never
written down (no script until three weeks later, no n recorded on the
off-centre cells, and taken the day before charter 49 removed the population
it scored), and 2026-09-08, on claim-axis samples days after P0 had moved to
the distance axis.  A constant only a person can move does not follow the
band table underneath it, and that table refits nightly.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target import off_centre_claim_store as store  # noqa: E402
from bayes_basket_target.basket_kernel.delta_v2 import (  # noqa: E402
    OFF_CENTER_CLAIM_BANDS,
    off_center_claim_factor,
    window_claim_factor,
)


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    store.ensure_schema(conn)
    from bayes_basket_target.band_reliability_store import (
        ensure_band_table_schema,
    )
    ensure_band_table_schema(conn)
    return conn


def _band_table(conn, *, stage="P0", axis="centre_distance", sha=None):
    conn.execute(
        "INSERT INTO calibration_band_reliability_table("
        " band_table_sha256, factors_json, evidence_json, fitted_at_utc,"
        " serving_day_count, activated, activated_at_utc, stage, axis)"
        " VALUES(?,?,?,?,?,1,?,?,?)",
        (sha or ("b" * 64), json.dumps([[[0.0, 1.0], 1.0]]), "{}",
         "2026-09-09T00:00:00Z", 30, "2026-09-09T00:00:00Z", stage, axis),
    )


def _activate(conn, factors, *, stage="P0", axis="centre_distance", sha="f" * 64):
    store.persist_and_activate(conn, {
        "stage": stage, "band_axis": axis, "content_sha256": sha,
        "serving_day_count": 30, "evidence": [],
        "payload": {"factors": factors},
    }, now="2026-09-09T00:00:00Z")


def test_the_kernel_constant_is_the_bootstrap_not_the_rule() -> None:
    assert store.bootstrap_bands() == tuple(
        (float(b), float(f)) for b, f in OFF_CENTER_CLAIM_BANDS
    )
    # And the kernel still applies the constant when handed nothing.
    assert off_center_claim_factor(0.15) == 0.88
    assert off_center_claim_factor(
        0.15, bands=((0.05, 1.0), (0.2, 0.50), (1.01, 1.0))
    ) == 0.50


def test_a_table_fitted_on_another_axis_is_not_loaded() -> None:
    """Charter 110's reason, one layer over: the band table moves every
    claim, this correction is measured on claims, so a table fitted on one
    axis describes a quantity the other does not compute."""
    conn = _db()
    _band_table(conn, axis="centre_distance")
    _activate(conn, [[0.05, 1.0], [0.2, 0.70], [1.01, 1.0]], axis="claim")
    assert store.load_off_centre_bands(conn, stage="P0") is None
    _activate(conn, [[0.05, 1.0], [0.2, 0.75], [1.01, 1.0]],
              axis="centre_distance", sha="a" * 64)
    loaded = store.load_off_centre_bands(conn, stage="P0")
    assert loaded is not None and dict(loaded[0])[0.2] == 0.75
    conn.close()


def test_absence_serves_the_bootstrap_rather_than_failing() -> None:
    conn = _db()
    assert store.load_off_centre_bands(conn, stage="P0") is None
    assert store.load_off_centre_tables(conn) == {
        "P0": None, "P0-M": None, "P1": None
    }
    conn.close()


def test_the_lane_deflates_and_never_lifts() -> None:
    """Charter 81 owns the lift, for centre-holding windows, where it was
    measured.  Lifting an off-centre claim raises what the gate lets us PAY,
    and off-centre positions are the ones losing money -- measured
    2026-09-09: 70 of them, -15.8%, against -0.85% for centre-holding.
    """
    source = __import__("inspect").getsource(store.fit_off_centre_claim_table)
    assert 'value, reason = 1.0, "lift_refused_deflation_only"' in source
    assert "elif shrunk > 1.0:" in source


def test_a_cell_without_evidence_keeps_what_it_serves() -> None:
    source = __import__("inspect").getsource(store.fit_off_centre_claim_table)
    assert "count < MIN_CELL_ROWS or len(by_day) < MIN_CELL_DAYS" in source
    assert store.MIN_CELL_ROWS >= 200 and store.MIN_CELL_DAYS >= 10


def test_the_served_table_reaches_the_enumeration() -> None:
    """The kernel is pure, so the store is read by the caller and handed
    down; this is the wire, asserted by name."""
    import inspect

    from bayes_basket_target import delta_v2_runtime, production_io

    runtime_source = inspect.getsource(delta_v2_runtime)
    assert "off_centre_bands=(off_centre_claim_bands or {}).get(" in runtime_source
    io_source = inspect.getsource(production_io)
    assert "off_centre_claim_bands=load_off_centre_tables(conn)" in io_source
    helper = inspect.getsource(window_claim_factor)
    assert "bands=off_centre_bands" in helper


def test_the_fingerprint_carries_the_served_table_not_just_the_constant() -> None:
    """The gap this lane opened, and closed the same day.

    The off-centre table moved into the database while
    ``claim_transform_fingerprint`` -- charter 110's partition key for the
    lambda fit -- still hashed the kernel constant, so the served table could
    move under lambda's own sample unseen.  It was left as a sentinel on the
    reasoning that charter 49's ladder floor keeps off-centre windows out of
    the candidate population entirely.

    The sentinel fired on its first run: 84 replay rows were already on the
    off-centre path, because re-deriving a claim on the distance axis can
    move the peak and take a window off centre.  The floor governs what
    PRODUCTION enumerates; it does not govern what a replay recomputes.
    """
    from bayes_basket_target.basket_kernel.delta_v2 import (
        claim_transform_fingerprint,
    )

    bare = claim_transform_fingerprint(holds_centre=False)
    served = claim_transform_fingerprint(
        holds_centre=False, off_centre_table_sha="a" * 64
    )
    other = claim_transform_fingerprint(
        holds_centre=False, off_centre_table_sha="b" * 64
    )
    assert bare != served != other and bare != other
    # A centre-holding window never reads the off-centre table, so its
    # fingerprint must not move when that table does -- otherwise every
    # centre-holding row in the sample retires on a nightly refit.
    assert claim_transform_fingerprint(
        holds_centre=True
    ) == claim_transform_fingerprint(
        holds_centre=True, off_centre_table_sha="a" * 64
    )


def test_the_fit_partitions_and_counts_the_off_centre_rows() -> None:
    """Both halves: the key that excludes them, and the number that says how
    many there were.  The count has been zero and non-zero inside one day."""
    import inspect

    from bayes_basket_target import effective_edge_store as store

    source = inspect.getsource(store._candidate_population)
    assert "off_centre_table_sha=off_centre_sha" in source
    assert '"off_centre_row_count": off_centre_rows,' in source
    fit_source = inspect.getsource(store.fit_lambda_table)
    assert 'population["off_centre_row_count"]' in fit_source


def test_the_replay_lane_prices_on_the_served_table() -> None:
    """Before 2026-09-09 it used the kernel bootstrap while production read
    the store, so a replayed claim could differ from the served one by the
    very table this lane exists to keep current."""
    source = (
        ROOT / "scripts" / "replay_candidate_windows_on_current_band.py"
    ).read_text(encoding="utf-8")
    assert "load_off_centre_bands" in source
    assert "off_centre_bands=off_centre_bands" in source
    assert "off_centre_table_sha=off_centre_sha" in source
