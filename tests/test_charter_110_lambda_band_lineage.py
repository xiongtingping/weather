"""Lambda is fitted on one band vintage at a time.

The band table changes every claim, and the claim is the denominator of
``a = price / claim`` -- the axis lambda itself is keyed on.  Measured
2026-09-08 on 757 live candidate windows, moving P0 to the distance axis
shifted ``a`` on 100% of them and carried 6.1% across a band boundary into
a different lambda value.  A fit pooling both vintages fits one curve to two
different x axes.

The partition key is the AXIS, not the table.  Band tables are content
addressed and refitted daily, so the sha moves whenever a factor shifts in
its third decimal: P0's 662 scored windows carry 28 distinct shas, the
largest holding 61 -- fewer than the eight lambda bands need at twelve seats
each -- and P1's 77 carry 21 shas of which exactly one clears the threshold.
Partitioning on the sha would not make the fit purer, it would end it.  The
sha is kept for audit so drift inside an axis stays visible.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for path in (str(ROOT), str(ROOT / "scripts")):
    if path not in sys.path:
        sys.path.insert(0, path)

from bayes_basket_target.basket_kernel.delta_v2 import SEAT_SHAPE_RULE_VERSION  # noqa: E402
from bayes_basket_target.effective_edge_store import _fit_axis  # noqa: E402
from tests.claim_lane_tables import add_label  # noqa: E402


def _db() -> sqlite3.Connection:
    """Production's candidate relation, in the columns the fit reads, and
    the one station-day's label that says where the window ends."""

    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE basket_candidate_window_scored ("
        " candidate_id TEXT PRIMARY KEY, station_id TEXT, event_date TEXT,"
        " stage TEXT, decision_at_utc TEXT, buckets_json TEXT,"
        " window_size INTEGER, win_probability REAL, raw_win_probability REAL,"
        " all_in_price REAL, admissible INTEGER, seated INTEGER,"
        " band_axis TEXT, band_table_sha256 TEXT, claim_transform_sha256 TEXT,"
        " leg_claim_cost_json TEXT, seat_shape_rule TEXT, settled_bucket TEXT,"
        " hit INTEGER, settled_at_utc TEXT)"
    )
    add_label(conn, "ZSQD", "2026-09-05", settled_bucket="30°C",
              known_at_utc="2026-09-05T12:00:00Z")
    return conn


def _candidate(conn, cid, *, axis=None, sha=None, stage="P0", bucket="30°C",
               shape=SEAT_SHAPE_RULE_VERSION):
    # ``shape`` defaults to the current rule (charter 131): a fixture row is
    # a row production would record today.  ``None`` is a row from before.
    conn.execute(
        "INSERT INTO basket_candidate_window_scored("
        " candidate_id, station_id, event_date, stage, decision_at_utc,"
        " buckets_json, window_size, win_probability, all_in_price,"
        " admissible, seated, band_axis, band_table_sha256, hit,"
        " seat_shape_rule)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (cid, "ZSQD", "2026-09-05", stage, "2026-09-05T00:00:00Z",
         json.dumps([bucket]), 1, 0.4, 0.3, 1, 0, axis, sha, 1, shape),
    )


def test_the_fit_reads_one_axis_and_counts_what_it_left_out():
    """Both halves matter.  Excluding the other vintage is the point; saying
    how much was excluded is what stops a halved sample looking like a
    sample that was always that size."""

    import inspect

    from bayes_basket_target import effective_edge_store as store

    # The selection moved into ``_candidate_population`` on 2026-09-09 so
    # the fit and the activation guard read one implementation; the tallies
    # still surface on the fit's own receipt.
    source = inspect.getsource(store._candidate_population)
    assert "band_axis IS ?" in source
    assert "band_axis IS NOT ?" in source
    assert '"excluded_other_band_axis"' in source
    assert '"band_axis": axis' in source
    fit_source = inspect.getsource(store.fit_lambda_table)
    assert "_candidate_population(" in fit_source
    assert 'population["excluded_other_band_axis"]' in fit_source


def test_an_unknown_vintage_is_excluded_rather_than_assumed_current():
    """Charter 104 forbids silently borrowing a basis for a claim; a
    partition key gets the same rule.  ``IS ?`` rather than ``= ?`` so the
    NULL case is explicit either way."""

    conn = _db()
    # Different buckets: the row's identity is (station, date, stage,
    # decision, buckets), so two rows on one decision have to differ there.
    _candidate(conn, "known", axis="claim", sha="a" * 64, bucket="30°C")
    _candidate(conn, "unknown", bucket="31°C")
    conn.commit()
    got = conn.execute(
        "SELECT COUNT(*) FROM basket_candidate_window_scored"
        " WHERE hit IS NOT NULL AND band_axis IS ?", ("claim",),
    ).fetchone()[0]
    left_out = conn.execute(
        "SELECT COUNT(*) FROM basket_candidate_window_scored"
        " WHERE hit IS NOT NULL AND band_axis IS NOT ?", ("claim",),
    ).fetchone()[0]
    assert (got, left_out) == (1, 1)


def test_the_axis_comes_from_what_the_stages_serve_not_from_the_caller():
    from bayes_basket_target.band_reliability_store import (
        ensure_band_table_schema,
        persist_and_activate_band_table,
    )

    conn = sqlite3.connect(":memory:")
    ensure_band_table_schema(conn)
    # No table at all is not a vintage: partitioning on the store's
    # historical ``claim`` default would exclude every row in a fixture
    # that never had band lineage to record.
    assert _fit_axis(conn, ("P0",)) is None, "no table, no partition"
    persist_and_activate_band_table(
        conn, stage="P0", factors=[((0.0, 1.0), 1.1)],
        evidence={}, serving_day_count=20, axis="centre_distance",
    )
    assert _fit_axis(conn, ("P0",)) == "centre_distance"
    # Stages on two different axes cannot be pooled into one fit, and the
    # honest answer is no axis rather than one of them.
    persist_and_activate_band_table(
        conn, stage="P1", factors=[((0.0, 0.5), 0.9)],
        evidence={}, serving_day_count=20, axis="claim",
    )
    assert _fit_axis(conn, ("P0", "P1")) is None


# --- Charter 110 extended (2026-09-08): the claim transform is a second key --


def test_the_two_claim_paths_carry_different_fingerprints() -> None:
    """A centre-holding window and an off-centre one are not one vintage.

    They go through different tables -- charter 81's lift or the off-centre
    deflation -- so one fingerprint over the union would retire a row for a
    change to a table it never read.
    """
    from bayes_basket_target.basket_kernel.delta_v2 import (
        claim_transform_fingerprint,
    )

    assert claim_transform_fingerprint(
        holds_centre=True
    ) != claim_transform_fingerprint(holds_centre=False)


def test_moving_the_off_centre_table_leaves_centre_holding_rows_current(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reason the key is split, stated as a test.

    Measured 2026-09-08: all 1,493 candidate windows in the book verify
    against the centre-holding path and none against the off-centre one --
    charter 49's ladder floor has left the fit no off-centre rows at all.  A
    union key would therefore have taken the whole lambda sample to zero to
    record a change to a table none of those rows read.
    """
    import bayes_basket_target.basket_kernel.delta_v2 as kernel

    # The deflation is retired (identity) by default, and on that path the
    # table is not part of the claim, so moving it must NOT move the key.
    off_retired = kernel.claim_transform_fingerprint(holds_centre=False)
    monkeypatch.setattr(
        kernel,
        "OFF_CENTER_CLAIM_BANDS",
        ((0.05, 1.15), (0.10, 0.85), (0.20, 0.88), (1.01, 0.5)),
    )
    assert kernel.claim_transform_fingerprint(holds_centre=False) == off_retired
    # With the arm on, the table IS the claim's arithmetic and the key moves.
    monkeypatch.setattr(kernel, "OFF_CENTRE_CLAIM_MODE", "on")
    monkeypatch.setattr(
        kernel, "OFF_CENTER_CLAIM_BANDS", kernel.OFF_CENTER_CLAIM_BANDS,
    )
    before_centre = kernel.claim_transform_fingerprint(holds_centre=True)
    monkeypatch.setattr(
        kernel,
        "OFF_CENTER_CLAIM_BANDS",
        ((0.05, 1.15), (0.10, 0.85), (0.20, 0.88), (1.01, 0.6)),
    )
    before_off = kernel.claim_transform_fingerprint(holds_centre=False)
    monkeypatch.setattr(
        kernel,
        "OFF_CENTER_CLAIM_BANDS",
        ((0.05, 1.15), (0.10, 0.85), (0.20, 0.88), (1.01, 0.5)),
    )
    assert kernel.claim_transform_fingerprint(holds_centre=True) == before_centre
    assert kernel.claim_transform_fingerprint(holds_centre=False) != before_off


def test_moving_charter_81_retires_the_centre_holding_sample(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """And it still fails closed where it must: 1,321 of those rows read
    charter 81's lift, and moving it really does change their claims."""
    import bayes_basket_target.basket_kernel.delta_v2 as kernel

    before = kernel.claim_transform_fingerprint(holds_centre=True)
    monkeypatch.setattr(
        kernel, "CENTER_WINDOW_CLAIM_BANDS", ((0.35, 1.0), (1.01, 1.5)),
    )
    assert kernel.claim_transform_fingerprint(holds_centre=True) != before


def test_the_fit_excludes_rows_from_another_claim_vintage() -> None:
    """A row whose claim came from other tables is not evidence about a."""
    from bayes_basket_target.basket_kernel.delta_v2 import (
        claim_transform_fingerprint,
    )
    from bayes_basket_target.effective_edge_store import fit_lambda_table

    conn = _db()
    current = claim_transform_fingerprint(holds_centre=True)
    for index in range(6):
        _candidate(
            conn, f"cand-current-{index}", bucket=f"3{index}°C",
        )
        conn.execute(
            "UPDATE basket_candidate_window_scored"
            " SET claim_transform_sha256=? WHERE candidate_id=?",
            (current, f"cand-current-{index}"),
        )
    for index in range(6):
        _candidate(
            conn, f"cand-stale-{index}", bucket=f"4{index}°C",
        )
        conn.execute(
            "UPDATE basket_candidate_window_scored"
            " SET claim_transform_sha256=? WHERE candidate_id=?",
            ("0" * 64, f"cand-stale-{index}"),
        )
    fit = fit_lambda_table(conn, stage_scope="P0")
    assert fit["claim_transform_sha256"] is not None
    assert fit["excluded_other_claim_transform"] == 6
    conn.close()


def test_activation_closes_the_outgoing_interval_and_opens_one() -> None:
    """``activated_at_utc`` said when a table started and never when one
    stopped, so there was no window to score anything over: 42 tables
    fitted by 2026-09-09, three carrying a stamp, all three stamped the
    same minute, no history behind them."""

    from bayes_basket_target.effective_edge_store import ensure_schema

    conn = sqlite3.connect(":memory:")
    ensure_schema(conn)
    # The shape the activation path writes: close the open one, open the new.
    conn.execute(
        "INSERT INTO calibration_effective_edge_serving_interval VALUES"
        " ('P0','old','2026-09-01T00:00:00+00:00',NULL)"
    )
    now = "2026-09-05T00:00:00+00:00"
    conn.execute(
        "UPDATE calibration_effective_edge_serving_interval"
        " SET served_until_utc=? WHERE stage_scope=? AND"
        " served_until_utc IS NULL",
        (now, "P0"),
    )
    conn.execute(
        "INSERT INTO calibration_effective_edge_serving_interval("
        " stage_scope, lambda_table_sha256, served_from_utc,"
        " served_until_utc) VALUES(?,?,?,NULL)",
        ("P0", "new", now),
    )
    # P1 activating must not close P0's interval.
    conn.execute(
        "INSERT INTO calibration_effective_edge_serving_interval VALUES"
        " ('P1','p1a','2026-09-06T00:00:00+00:00',NULL)"
    )
    conn.commit()
    rows = conn.execute(
        "SELECT stage_scope, lambda_table_sha256, served_until_utc"
        " FROM calibration_effective_edge_serving_interval"
        " ORDER BY stage_scope, served_from_utc"
    ).fetchall()
    assert rows == [
        ("P0", "old", now),
        ("P0", "new", None),
        ("P1", "p1a", None),
    ]


def _activate_band_table(conn, sha, *, stage="P0", axis="centre_distance"):
    from bayes_basket_target.band_reliability_store import (
        ensure_band_table_schema,
    )
    ensure_band_table_schema(conn)
    conn.execute(
        "INSERT INTO calibration_band_reliability_table("
        " band_table_sha256, factors_json, evidence_json, fitted_at_utc,"
        " serving_day_count, activated, activated_at_utc, stage, axis)"
        " VALUES(?,?,?,?,?,?,?,?,?)",
        (sha, json.dumps([[[0.0, 1.0], 1.0]]), "{}",
         "2026-09-09T00:00:00Z", 30, 1, "2026-09-09T00:00:00Z", stage, axis),
    )


def test_a_table_fitted_on_another_band_axis_is_not_served() -> None:
    """``a = price / claim`` and the band axis moves every claim, so a table
    fitted on one axis describes a different quantity on the other.  The
    population was already partitioned on the axis (charter 110); the table
    that came out recorded none, so nothing downstream could tell.

    Three states, not two: a payload recording NO axis is carried as
    matching, because "did not record a vintage" is not "the vintage
    changed" -- treating it as one deadlocks the lane against its own
    activation gate, which is the 2026-09-08 judgement on the claim vintage.
    """

    import json
    import sqlite3

    from bayes_basket_target import effective_edge_store as store
    from bayes_basket_target.band_reliability_store import (
        ensure_band_table_schema,
    )

    conn = sqlite3.connect(":memory:")
    store.ensure_schema(conn)
    ensure_band_table_schema(conn)

    def _band(axis: str) -> None:
        conn.execute(
            "DELETE FROM calibration_band_reliability_table WHERE stage='P0'"
        )
        conn.execute(
            "INSERT INTO calibration_band_reliability_table("
            " band_table_sha256, factors_json, evidence_json, fitted_at_utc,"
            " serving_day_count, activated, activated_at_utc, stage, axis)"
            " VALUES(?,?,?,?,?,1,?,?,?)",
            ("b" * 64, json.dumps([[[0.0, 1.0], 1.0]]), "{}",
             "2026-09-09T00:00:00Z", 30, "2026-09-09T00:00:00Z", "P0", axis),
        )

    def _table(sha: str, axis, lam: float) -> None:
        payload = {
            "schema": "effective_edge_lambda_table_v1",
            "stage_scope": "P0", "claim_scope": "window", "stages": ["P0"],
            "bands": [{"upper_bound": None, "lambda": lam}],
            "window_days": 28, "as_of_event_date": "2026-09-10",
            "fit_floor_event_date": "2026-08-13",
            "band_partition_cells": store.BAND_PARTITION_CELLS,
            "claim_transform_sha256": None,
        }
        if axis is not None:
            payload["band_axis"] = axis
        conn.execute(
            "UPDATE calibration_effective_edge_lambda SET activated=0"
            " WHERE stage_scope='P0'"
        )
        conn.execute(
            "INSERT INTO calibration_effective_edge_lambda("
            " lambda_table_sha256, bands_json, evidence_json, fitted_at_utc,"
            " window_days, seat_count, activated, activated_at_utc,"
            " stage_scope, claim_scope) VALUES(?,?,?,?,?,?,1,?,?,?)",
            (sha, json.dumps(payload), "{}", "2026-09-10T00:00:00Z", 28, 50,
             "2026-09-10T00:00:00Z", "P0", "window"),
        )
        conn.commit()

    _band("centre_distance")
    # Same axis: served.
    _table("a" * 64, "centre_distance", 0.31)
    assert store.load_active_lambda(conn, stage_scope="P0") == ((float("inf"), 0.31),)
    # Another axis: not served, and the incumbent falls back to the kernel
    # constant rather than to a table about a different quantity.
    _table("c" * 64, "claim", 0.31)
    assert store.load_active_lambda(conn, stage_scope="P0") is None
    assert store._incumbent_bands(conn, "P0", "window") == list(
        store._bootstrap_bands("P0", "window")
    )
    # No axis recorded: carried, not refused.
    _table("d" * 64, None, 0.29)
    assert store.load_active_lambda(conn, stage_scope="P0") == ((float("inf"), 0.29),)
    # No band table at all: no partition, so no mismatch is possible.
    conn.execute("DELETE FROM calibration_band_reliability_table")
    conn.commit()
    _table("e" * 64, "claim", 0.27)
    assert store.load_active_lambda(conn, stage_scope="P0") == ((float("inf"), 0.27),)


def test_the_fit_records_the_axis_it_was_drawn_on(tmp_path) -> None:
    """The payload must carry the axis, or the guard above has nothing to
    read and the next fit inherits across an axis move in silence."""

    import json
    import sqlite3

    from bayes_basket_target import effective_edge_store as store

    conn = sqlite3.connect(tmp_path / "e.db", isolation_level=None)
    conn.execute(
        "CREATE TABLE basket_candidate_window_scored ("
        " candidate_id TEXT PRIMARY KEY, station_id TEXT, event_date TEXT,"
        " stage TEXT, decision_at_utc TEXT, buckets_json TEXT,"
        " win_probability REAL, all_in_price REAL, hit INTEGER)"
    )
    for i in range(20):
        conn.execute(
            "INSERT INTO basket_candidate_window_scored VALUES(?,?,?,?,?,?,?,?,?)",
            (f"c{i}", f"S{i}", "2026-09-01", "P0", "2026-09-01T00:00:00Z",
             '["b"]', 0.60, 0.18, i % 2),
        )
        add_label(conn, f"S{i}", "2026-09-01")
    fit = store.fit_lambda_table(conn, window_days=28)
    assert "band_axis" in fit["table"]
    assert fit["table"]["band_axis"] == fit["band_axis"]
    conn.close()


def test_the_axis_column_is_read_not_the_candidate_tables_column() -> None:
    """The lambda table's own migration decides whether the column is there.
    Keying off ``basket_candidate_window_scored``'s band_axis -- a different
    table on a different migration -- would be right only by coincidence."""

    import sqlite3

    from bayes_basket_target import effective_edge_store as store

    conn = sqlite3.connect(":memory:")
    store.ensure_schema(conn)
    assert store._has_lambda_band_axis_column(conn)
    # No candidate table at all: the lambda column is still there and still
    # read.
    assert not store._has_band_axis_column(conn)
    columns = {
        str(r[1])
        for r in conn.execute(
            "PRAGMA table_info(calibration_effective_edge_lambda)"
        )
    }
    assert "band_axis" in columns
    # The journal deliberately does NOT gain the column: it records fits,
    # and the fit's axis travels on the table it points at.
    journal = {
        str(r[1])
        for r in conn.execute(
            "PRAGMA table_info(calibration_effective_edge_fit_journal)"
        )
    }
    assert "band_axis" not in journal


def test_the_axis_is_read_from_the_column_when_the_payload_has_none() -> None:
    """Every row that existed before 2026-09-11 records its axis ONLY in the
    column: ``lambda_table_sha256`` is ``content_sha256(payload)``, so the
    backfill could not touch the payload without breaking each row against
    its own hash.  A guard that reads only the payload therefore passes
    every one of them -- which is how the first version of this check
    shipped reading a different function's query and looked fine.
    """

    import json
    import sqlite3

    from bayes_basket_target import effective_edge_store as store
    from bayes_basket_target.band_reliability_store import (
        ensure_band_table_schema,
    )

    conn = sqlite3.connect(":memory:")
    store.ensure_schema(conn)
    ensure_band_table_schema(conn)
    conn.execute(
        "INSERT INTO calibration_band_reliability_table("
        " band_table_sha256, factors_json, evidence_json, fitted_at_utc,"
        " serving_day_count, activated, activated_at_utc, stage, axis)"
        " VALUES(?,?,?,?,?,1,?,?,?)",
        ("b" * 64, json.dumps([[[0.0, 1.0], 1.0]]), "{}",
         "2026-09-11T00:00:00Z", 30, "2026-09-11T00:00:00Z", "P0-M",
         "centre_distance"),
    )
    payload = {
        "schema": "effective_edge_lambda_table_v1",
        "stage_scope": "P0-M", "claim_scope": "window", "stages": ["P0-M"],
        "bands": [{"upper_bound": None, "lambda": 0.31}],
        "window_days": 28, "as_of_event_date": "2026-09-10",
        "fit_floor_event_date": "2026-08-13",
        "band_partition_cells": store.BAND_PARTITION_CELLS,
        "claim_transform_sha256": None,
    }
    assert "band_axis" not in payload
    conn.execute(
        "INSERT INTO calibration_effective_edge_lambda("
        " lambda_table_sha256, bands_json, evidence_json, fitted_at_utc,"
        " window_days, seat_count, activated, activated_at_utc, stage_scope,"
        " claim_scope, band_axis) VALUES(?,?,?,?,?,?,1,?,?,?,?)",
        ("a" * 64, json.dumps(payload), "{}", "2026-09-10T00:00:00Z", 28, 50,
         "2026-09-10T00:00:00Z", "P0-M", "window", "claim"),
    )
    conn.commit()
    # The column says claim, the stage serves centre_distance: no table.
    assert store.load_active_lambda(conn, stage_scope="P0-M") is None
    assert store._incumbent_bands(conn, "P0-M", "window") == list(
        store._bootstrap_bands("P0-M", "window")
    )
    # Same row, column agreeing with the served axis: served.
    conn.execute(
        "UPDATE calibration_effective_edge_lambda"
        " SET band_axis='centre_distance' WHERE lambda_table_sha256=?",
        ("a" * 64,),
    )
    conn.commit()
    assert store.load_active_lambda(conn, stage_scope="P0-M") == (
        (float("inf"), 0.31),
    )


def test_the_fit_partitions_on_the_seat_shape_too():
    """Charter 131 (2026-09-12): the third partition key.  A window's shape
    decides whether the mode bucket is inside it, and rungs realise by their
    claim size, so rows enumerated under another shape rule are not
    evidence about today's lambda.  NULL -- written before the rule -- is
    excluded like a NULL axis, and the fit says how many it left out."""

    import inspect

    from bayes_basket_target import effective_edge_store as store
    from bayes_basket_target.basket_kernel.delta_v2 import (
        claim_transform_fingerprint as kernel_claim_transform_fingerprint,
    )

    conn = _db()
    store.ensure_schema(conn)
    columns = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(basket_candidate_window_scored)")
    }
    assert "seat_shape_rule" in columns
    lambda_columns = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(calibration_effective_edge_lambda)")
    }
    assert "seat_shape_rule" in lambda_columns
    source = inspect.getsource(store._candidate_population)
    assert "seat_shape_rule IS ?" in source
    assert "seat_shape_rule IS NOT ?" in source
    assert '"excluded_other_seat_shape"' in source

    _candidate(conn, "current", bucket="30°C")
    _candidate(conn, "older", bucket="31°C", shape=None)
    conn.execute(
        "UPDATE basket_candidate_window_scored SET claim_transform_sha256=?",
        (kernel_claim_transform_fingerprint(holds_centre=True),),
    )
    conn.commit()
    population = store._candidate_population(conn, ("P0",), "2026-09-01")
    assert len(population["rows"]) == 1
    assert population["seat_shape_rule"] == SEAT_SHAPE_RULE_VERSION
    assert population["excluded_other_seat_shape"] == 1


def test_a_served_table_from_another_seat_shape_yields_to_the_constant():
    """Same three states as the axis: another shape is None (the kernel
    constant serves), the same shape serves, no record is carried."""

    import json as _json

    from bayes_basket_target import effective_edge_store as store

    conn = _db()
    store.ensure_schema(conn)
    payload = {"bands": [{"upper_bound": None, "lambda": 0.9}], "window_days": 14}

    def _activate(sha: str, shape: str | None, fitted_at: str) -> None:
        conn.execute(
            "INSERT INTO calibration_effective_edge_lambda("
            " lambda_table_sha256, bands_json, evidence_json, fitted_at_utc,"
            " window_days, seat_count, activated, activated_at_utc,"
            " stage_scope, claim_scope, band_axis, seat_shape_rule)"
            " VALUES(?,?,?,?,?,?,1,?,'P0','window',NULL,?)",
            (sha, _json.dumps(payload), "{}", fitted_at, 14, 30, fitted_at, shape),
        )
        conn.commit()

    _activate("a" * 64, "some_older_shape_rule", "2026-09-10T00:00:00Z")
    assert store.load_active_lambda(conn, stage_scope="P0") is None
    _activate("b" * 64, SEAT_SHAPE_RULE_VERSION, "2026-09-12T00:00:00Z")
    assert store.load_active_lambda(conn, stage_scope="P0") == ((float("inf"), 0.9),)
    _activate("c" * 64, None, "2026-09-13T00:00:00Z")
    assert store.load_active_lambda(conn, stage_scope="P0") == ((float("inf"), 0.9),)
