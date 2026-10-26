"""The centre/width layer refits, and cannot be outvoted by retired math.

The store held the search, the forward admission rule, the persistence and
the journal, and nothing anywhere called any of it: the scalars in production
were one hand fit that could never refresh.  A layer whose entire job is to
say how wrong the served distribution is has to run again when the served
distribution changes, and it has to be able to ignore the days it no longer
describes -- otherwise it re-derives whatever it had been compensating for.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import unittest

import pytest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.center_width_store import (  # noqa: E402
    CENTER_WIDTH_SERVED_STAGES,
    CENTER_WIDTH_STAGES,
    FORWARD_WINDOW_FRACTION,
    _mean_nll,
    _truth,
    ensure_center_width_schema,
    fit_center_width,
    load_center_width,
    persist_and_activate_center_width,
    refit_all_center_width,
    serving_cells_by_day,
)


def _connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    ensure_center_width_schema(conn)
    conn.execute(
        "CREATE TABLE basket_probability_bridge_history ("
        " stage TEXT, station_id TEXT, event_date TEXT,"
        " decision_at_utc TEXT, envelope_json TEXT,"
        " settlement_timezone TEXT,"
        " checkpoint_id TEXT DEFAULT '',"
        " envelope_content_sha256 TEXT DEFAULT '')"
    )
    conn.execute(
        "CREATE TABLE calibration_station_daily_high_truth ("
        " station_id TEXT, local_date TEXT, official_tmax_c REAL,"
        " revision_no INTEGER, qc_status TEXT)"
    )
    return conn


def _envelope() -> str:
    """The shape the store actually scores: probabilities, ordinals, and the
    bucket intervals its lineage carries.  A cell missing any of the three is
    skipped, so a thinner fixture would test the skip and not the window."""

    keys = ["20", "21", "22"]
    intervals = {
        "20": [None, 20.5],
        "21": [20.5, 21.5],
        "22": [21.5, None],
    }
    return json.dumps({
        "probabilities": {"20": 0.3, "21": 0.4, "22": 0.3},
        "bucket_ordinals": {key: index for index, key in enumerate(keys)},
        "lineage": {"bucket_intervals_c_json": json.dumps(intervals)},
    })


class EvidenceWindowTest(unittest.TestCase):
    def test_the_window_excludes_days_below_the_bound(self) -> None:
        """A day the served arithmetic no longer describes must not vote."""

        conn = _connection()
        rows = [
            (
                "P1", "KLGA", day, f"{day}T12:00:00Z",
                _envelope(), "America/New_York",
            )
            for day in ("2026-08-10", "2026-08-11", "2026-08-17")
        ]
        conn.executemany(
            "INSERT INTO basket_probability_bridge_history VALUES"
            " (?,?,?,?,?,?,'','')",
            rows,
        )
        truth = {
            ("KLGA", "2026-08-10"): 20.0,
            ("KLGA", "2026-08-11"): 21.0,
            ("KLGA", "2026-08-17"): 20.0,
        }
        unbounded = serving_cells_by_day(conn, truth, stage="P1")
        bounded = serving_cells_by_day(
            conn, truth, stage="P1", min_decision_at_utc="2026-08-17T00:00:00Z"
        )
        self.assertEqual(len(unbounded), 3)
        self.assertEqual(sorted(bounded), ["2026-08-17"])

    def test_the_bound_is_on_the_decision_not_the_event_date(self) -> None:
        """A P0 decision for an event two days out is taken two days early.

        Bounding on event_date would admit exactly the decisions the bound
        exists to exclude: on the live table, event date 2026-08-19 carries
        decisions reaching back to 2026-08-17.
        """

        conn = _connection()
        conn.execute(
            "INSERT INTO basket_probability_bridge_history VALUES"
            " (?,?,?,?,?,?,'','')",
            (
                "P1", "KLGA", "2026-08-19", "2026-08-17T06:00:00Z",
                _envelope(), "America/New_York",
            ),
        )
        truth = {("KLGA", "2026-08-19"): 20.0}
        # Late event date, early decision: the decision bound must exclude it.
        self.assertEqual(
            serving_cells_by_day(
                conn, truth, stage="P1",
                min_decision_at_utc="2026-08-18T00:00:00Z",
            ),
            {},
        )
        self.assertEqual(
            sorted(serving_cells_by_day(
                conn, truth, stage="P1",
                min_decision_at_utc="2026-08-17T00:00:00Z",
            )),
            ["2026-08-19"],
        )

    def test_the_bound_is_absent_by_default(self) -> None:
        """Existing callers keep scoring every day on record."""

        conn = _connection()
        conn.execute(
            "INSERT INTO basket_probability_bridge_history VALUES"
            " (?,?,?,?,?,?,'','')",
            (
                "P1", "KLGA", "2026-08-01", "2026-08-01T12:00:00Z",
                _envelope(), "America/New_York",
            ),
        )
        cells = serving_cells_by_day(
            conn, {("KLGA", "2026-08-01"): 20.0}, stage="P1"
        )
        self.assertEqual(sorted(cells), ["2026-08-01"])

    def test_the_bound_reaches_the_fit_and_its_evidence(self) -> None:
        conn = _connection()
        fitted = fit_center_width(
            conn, stage="P1", min_decision_at_utc="2026-08-18T00:00:00Z"
        )
        self.assertEqual(
            fitted["evidence"]["min_decision_at_utc"],
            "2026-08-18T00:00:00Z",
        )
        # No days at all is the accumulating case, not an error: the layer
        # says it has nothing to go on rather than inventing a scalar.
        self.assertTrue(fitted["accumulating"])
        self.assertEqual(fitted["delta_c"], 0.0)
        self.assertEqual(fitted["width_lambda"], 1.0)


class RefitAllStagesTest(unittest.TestCase):
    def test_only_p0_is_fitted_and_served(self) -> None:
        """Charter 150: P0 is the only stage with a centre/width correction.

        P0-M and P1 are one likelihood update each on P0's calibrated root;
        the pass reports them as not served and fits nothing for them.
        """

        conn = _connection()
        stages = refit_all_center_width(conn, commit=False)
        self.assertEqual(sorted(stages), sorted(CENTER_WIDTH_STAGES))
        self.assertTrue(stages["P0"]["served_stage"])
        for stage in ("P0-M", "P1"):
            self.assertFalse(stages[stage]["served_stage"])
            self.assertNotIn("delta_c", stages[stage])
        self.assertEqual(CENTER_WIDTH_SERVED_STAGES, ("P0",))

    def test_report_only_activates_nothing(self) -> None:
        conn = _connection()
        refit_all_center_width(conn, commit=False)
        for stage in CENTER_WIDTH_STAGES:
            self.assertIsNone(load_center_width(conn, stage=stage))

    def test_a_starved_p0_accumulates_rather_than_activating(self) -> None:
        """The pin makes P0 servable; evidence still decides whether it is."""

        conn = _connection()
        stages = refit_all_center_width(conn, commit=True)
        self.assertEqual(stages["P0"]["outcome"], "ACCUMULATING")
        self.assertIsNone(load_center_width(conn, stage="P0"))

    def test_a_retired_stage_is_retired_not_fitted(self) -> None:
        conn = _connection()
        stages = refit_all_center_width(conn, commit=True)
        self.assertIn(stages["P1"]["commit"]["state"], {"RETIRED", "UNCHANGED"})
        self.assertIsNone(load_center_width(conn, stage="P1"))



class P0PinContractTest(unittest.TestCase):
    """What the gate scored is what serving reproduces, or nothing is served.

    P0's distribution is compiled against an OOS-gated artifact.  The stage
    scalars could only join it by becoming part of the gated math: the
    holdout scores them, the artifact records the version scored, and serving
    applies that version or fails closed.  These pin the three halves of that
    contract against each other.
    """

    def test_the_tag_and_the_pin_are_read_back_from_one_string(self) -> None:
        from bayes_basket_target.center_width_store import (
            CENTER_WIDTH_METHOD_TAG,
            pinned_center_width_sha,
        )

        method = (
            "train_rmse_inverse_family_capped_v1::PRODUCTION_NO_LIVE"
            "::band_reliability_v1::band_table_sha256=" + "a" * 64
            + "::" + CENTER_WIDTH_METHOD_TAG
            + "::center_width_sha256=" + "b" * 64
        )
        self.assertIn(CENTER_WIDTH_METHOD_TAG, method)
        self.assertEqual(pinned_center_width_sha(method), "b" * 64)

    def test_an_untagged_artifact_names_no_version(self) -> None:
        """Artifacts built before this existed keep serving their own math."""

        from bayes_basket_target.center_width_store import (
            CENTER_WIDTH_METHOD_TAG,
            pinned_center_width_sha,
        )

        legacy = (
            "train_rmse_inverse_family_capped_v1::PRODUCTION_NO_LIVE"
            "::band_reliability_v1"
        )
        self.assertNotIn(CENTER_WIDTH_METHOD_TAG, legacy)
        self.assertIsNone(pinned_center_width_sha(legacy))

    def test_a_pinned_version_that_is_gone_fails_closed(self) -> None:
        """Serving an approximation of gated math is what the pin forbids."""

        from bayes_basket_target.contracts import ContractError

        conn = _connection()
        with self.assertRaises(ContractError):
            load_center_width(conn, stage="P0", table_sha256="c" * 64)

    def test_the_serving_path_reads_the_tag_and_the_pin(self) -> None:
        """Both halves must be present in the compiler, not just the tag.

        The transform itself is reached through ``served_shape`` since
        2026-09-13, which is the one place the fitted table layers are
        applied; the compiler holding that function object is what makes it
        apply the same arithmetic the gate scores.
        """

        from bayes_basket_target import native_ensemble_p0, served_shape

        source = (
            ROOT / "bayes_basket_target" / "native_ensemble_p0.py"
        ).read_text()
        self.assertIn("CENTER_WIDTH_METHOD_TAG", source)
        self.assertIn("pinned_center_width_sha", source)
        self.assertIs(
            native_ensemble_p0.apply_served_table_layers,
            served_shape.apply_served_table_layers,
        )

    def test_the_gate_pins_what_it_scored(self) -> None:
        from bayes_basket_target import native_ensemble_oos, served_shape

        source = (
            ROOT / "bayes_basket_target" / "native_ensemble_oos.py"
        ).read_text()
        self.assertIn("center_width_sha256", source)
        self.assertIn("CENTER_WIDTH_METHOD_TAG", source)
        self.assertIs(
            native_ensemble_oos.apply_served_table_layers,
            served_shape.apply_served_table_layers,
        )



class StageIsolationTest(unittest.TestCase):
    """One lane's lock loss must not cost the other lanes their fits.

    Mirrors the band store's 2026-08-20 lesson: P0 losing the write-lock
    race to the resident unwound the whole loop and P0-M and P1 lost even
    their journal rows.  This store does two units per stage -- the stage
    scalars and the per-station widths -- so the isolation is finer: the
    two lanes of one stage must also fail independently.
    """

    def test_one_failing_lane_does_not_cost_the_others(self) -> None:
        from unittest import mock

        import bayes_basket_target.center_width_store as store

        conn = _connection()
        real_scalar = store.fit_center_width
        real_station = store.fit_station_width

        def scalar_boom(inner, *, stage, **kwargs):
            if stage == "P0":
                # Die with a transaction open, as a mid-write failure
                # would: an un-rolled-back connection would fail the next
                # lane's lock take for an unrelated reason.
                inner.execute("BEGIN IMMEDIATE")
                raise sqlite3.OperationalError("database is locked")
            return real_scalar(inner, stage=stage, **kwargs)

        def station_boom(inner, *, stage, **kwargs):
            return real_station(inner, stage=stage, **kwargs)

        with mock.patch.object(store, "fit_center_width", scalar_boom), \
                mock.patch.object(store, "fit_station_width", station_boom):
            report = store.refit_all_center_width(conn, commit=True)

        self.assertEqual(sorted(report), sorted(CENTER_WIDTH_STAGES))
        # P0: scalar lane lost the lock; its station lane still fitted.
        self.assertIn("database is locked", report["P0"]["error"])
        self.assertNotIn("error", report["P0"]["station_width"])
        self.assertEqual(
            report["P0"]["station_width"]["outcome"], "ACCUMULATING"
        )
        # P0-M and P1 (charter 150): retired, not fitted, and P0's lost
        # lock does not cost them the retirement.
        for stage in ("P0-M", "P1"):
            self.assertNotIn("error", report[stage])
            self.assertFalse(report[stage]["served_stage"])
        journalled = {
            row[0]
            for row in conn.execute(
                "SELECT stage FROM calibration_center_width_fit_journal"
            )
        }
        # P0's scalar lane lost its lock before journalling, and the two
        # retired stages fit nothing to journal.
        self.assertEqual(journalled, set())

    def test_the_cli_reports_part_success_as_failure(self) -> None:
        """Exit code carries the verdict; the landed lanes stay reported."""

        import io
        import tempfile
        from contextlib import redirect_stdout
        from unittest import mock

        if str(ROOT / "scripts") not in sys.path:
            sys.path.insert(0, str(ROOT / "scripts"))
        import refit_center_width as cli

        with tempfile.NamedTemporaryFile(suffix=".db") as handle:
            seed = sqlite3.connect(handle.name)
            ensure_center_width_schema(seed)
            seed.commit()
            seed.close()

            # The shapes charter 150 returns: P0 is the one stage fitted;
            # the other two are retired and carry no fit at all, which the
            # summary read as one on 2026-09-18T20:16Z and died on.
            def broken(conn, *, commit, min_decision_at_utc=None):
                return {
                    "P0": {
                        "stage": "P0",
                        "outcome": "ACCUMULATING",
                        "served_stage": True,
                        "accumulating": True,
                        "admitted": False,
                        "delta_c": 0.0,
                        "width_lambda": 1.0,
                        "forward_nll_gain": 0.0,
                        "serving_day_count": 0,
                        "station_width": {},
                    },
                    "P0-M": {
                        "stage": "P0-M",
                        "served_stage": False,
                        "commit": {"state": "RETIRED"},
                    },
                    "P1": {
                        "stage": "P1",
                        "served_stage": False,
                        "error": "OperationalError: database is locked",
                    },
                }

            out = io.StringIO()
            with mock.patch.object(
                cli, "refit_all_center_width", broken
            ), mock.patch.object(
                sys, "argv",
                ["refit_center_width.py", "--db", handle.name,
                 "--commit", "--summary"],
            ), redirect_stdout(out):
                code = cli.main()

        self.assertEqual(code, 1)
        text = out.getvalue()
        self.assertIn('"failed_stages": ["P1"]', text)
        self.assertIn("SUMMARY P1: FAILED", text)
        self.assertIn("SUMMARY P0: ACCUMULATING", text)
        self.assertIn("SUMMARY P0-M: 宪章150起不服务，不拟合 [RETIRED]", text)


class BlockSamplingTest(unittest.TestCase):
    """One decision per three-hour block, chosen without reading the rest.

    ``serving_cells_by_day`` used to build a cell for every history row and
    then keep the median of each block.  Since 2026-08-26 no history row
    carries its body inline, so building a cell is a per-row query into
    ``basket_probability_envelope`` plus a rebuild and a full re-hash --
    profiled 2026-09-09 at 154,723 calls and 115.7s inside one render, which
    is 77% of the largest section on the board and one the cache may not
    hold.  The block a row belongs to is a function of three of its own
    columns, so the sampling now happens before any body is read.

    Equivalence rests on one measured fact: on the live store all 157,132
    rows that pass the truth/timezone filter produce a cell, none fail.  With
    no failures the walk stops on its first index, and that index is the
    median -- the same row the old loop kept.  These tests pin both halves:
    the first index IS the old median, and the walk only moves off it when a
    row genuinely yields nothing.
    """

    def test_the_first_index_tried_is_the_median_the_old_loop_kept(self):
        from bayes_basket_target.center_width_store import (
            _outward_from_middle,
        )

        for count in range(1, 40):
            order = _outward_from_middle(count)
            self.assertEqual(
                order[0], count // 2,
                f"{count} rows: first try must be the old median index",
            )
            self.assertEqual(
                sorted(order), list(range(count)),
                f"{count} rows: every index reachable, none twice",
            )

    def test_a_block_keeps_its_median_decision(self):
        conn = _connection()
        rows = [
            ("P1", "KLGA", "2026-08-10", f"2026-08-10T{stamp}Z",
             _envelope(), "America/New_York")
            # 08:00, 08:20 and 08:40 local -- one three-hour block.
            for stamp in ("12:00:00", "12:20:00", "12:40:00")
        ]
        conn.executemany(
            "INSERT INTO basket_probability_bridge_history VALUES"
            " (?,?,?,?,?,?,'','')",
            rows,
        )
        cells = serving_cells_by_day(
            conn, {("KLGA", "2026-08-10"): 20.0}, stage="P1"
        )
        self.assertEqual(len(cells["2026-08-10"]), 1, "one cell per block")
        self.assertEqual(
            cells["2026-08-10"][0]["decision_at_utc"],
            "2026-08-10T12:20:00Z",
        )

    def test_blocks_are_split_by_the_local_clock_not_the_utc_one(self):
        """Two rows three hours apart in UTC can share a local block or not;
        the boundary the sampler uses is the station's own midnight."""

        conn = _connection()
        conn.executemany(
            "INSERT INTO basket_probability_bridge_history VALUES"
            " (?,?,?,?,?,?,'','')",
            [
                # 08:00 and 11:00 local: blocks 2 and 3, so two cells.
                ("P1", "KLGA", "2026-08-10", "2026-08-10T12:00:00Z",
                 _envelope(), "America/New_York"),
                ("P1", "KLGA", "2026-08-10", "2026-08-10T15:00:00Z",
                 _envelope(), "America/New_York"),
            ],
        )
        cells = serving_cells_by_day(
            conn, {("KLGA", "2026-08-10"): 20.0}, stage="P1"
        )
        self.assertEqual(len(cells["2026-08-10"]), 2)
        self.assertEqual(
            sorted(c["decision_at_utc"] for c in cells["2026-08-10"]),
            ["2026-08-10T12:00:00Z", "2026-08-10T15:00:00Z"],
        )

    def test_a_block_whose_median_row_is_unusable_is_not_dropped(self):
        """The walk exists for this case.

        Returning only the median row would lose the whole block when that
        one row cannot be rebuilt; the old loop would have kept a neighbour,
        because its median was taken over the rows that produced cells.
        """

        conn = _connection()
        conn.executemany(
            "INSERT INTO basket_probability_bridge_history VALUES"
            " (?,?,?,?,?,?,'','')",
            [
                ("P1", "KLGA", "2026-08-10", "2026-08-10T12:00:00Z",
                 _envelope(), "America/New_York"),
                # The median by time, and unreadable.
                ("P1", "KLGA", "2026-08-10", "2026-08-10T12:20:00Z",
                 "{ not json", "America/New_York"),
                ("P1", "KLGA", "2026-08-10", "2026-08-10T12:40:00Z",
                 _envelope(), "America/New_York"),
            ],
        )
        cells = serving_cells_by_day(
            conn, {("KLGA", "2026-08-10"): 20.0}, stage="P1"
        )
        self.assertEqual(len(cells["2026-08-10"]), 1)
        self.assertEqual(
            cells["2026-08-10"][0]["decision_at_utc"],
            "2026-08-10T12:00:00Z",
            "ties go to the lower index, as they did before",
        )


if __name__ == "__main__":
    unittest.main()


def test_round_memo_asks_each_loading_question_once_per_stage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One round, one answer per distinct question.

    The two lanes inside a stage pass identical arguments, so before the memo
    a three-stage round resolved the evidence floor six times, the truth table
    six times and the serving cells six times -- 608 of a measured 885-second
    round went to the floor alone.
    """

    import bayes_basket_target.center_width_store as store

    calls = {"anchor": 0, "truth": 0, "cells": 0}

    def _anchor(_conn, *, stage, known_floor=None):  # noqa: ANN001, ARG001
        calls["anchor"] += 1
        return None

    def _truth(_conn):  # noqa: ANN001
        calls["truth"] += 1
        return {}

    def _cells(_conn, _truth_map, *, stage, min_decision_at_utc=None):  # noqa: ANN001, ARG001
        calls["cells"] += 1
        return {}

    monkeypatch.setattr(store, "served_arithmetic_anchor", _anchor)
    monkeypatch.setattr(store, "_truth", _truth)
    monkeypatch.setattr(store, "serving_cells_by_day", _cells)

    conn = sqlite3.connect(":memory:")
    try:
        store.ensure_center_width_schema(conn)
        store.refit_all_center_width(conn, commit=False)
    finally:
        conn.close()

    # Charter 150: one fitted stage (P0) since P0-M and P1 carry no table.
    assert calls == {"anchor": 1, "truth": 1, "cells": 1}


def test_round_memo_keeps_different_caller_bounds_apart() -> None:
    """Two lanes given different bounds are asking different questions."""

    import bayes_basket_target.center_width_store as store

    with store._round_memo():
        first = store._memoised(("evidence_floor", "P0", "A"), lambda: "a")
        second = store._memoised(("evidence_floor", "P0", "B"), lambda: "b")
        repeat = store._memoised(("evidence_floor", "P0", "A"), lambda: "z")
    assert (first, second, repeat) == ("a", "b", "a")
    # Outside a round there is no memo at all, so direct callers are unchanged.
    assert store._memoised(("evidence_floor", "P0", "A"), lambda: "fresh") == "fresh"


def _journal_fixture() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE calibration_center_width_fit_journal("
        "stage TEXT,fitted_at_utc TEXT)"
    )
    conn.execute(
        "CREATE TABLE calibration_center_width_station_journal("
        "stage TEXT,fitted_at_utc TEXT)"
    )
    return conn


def test_stage_order_serves_the_least_recently_fitted_first() -> None:
    """A truncated round used to cut the same stage every time.

    P1 sat last in the tuple and was the most expensive, so it was what never
    got fitted: sixteen journal rows for P0 against eleven for P1, whose
    newest was a full day behind.
    """

    from bayes_basket_target.center_width_store import stage_order_by_staleness

    conn = _journal_fixture()
    try:
        conn.executemany(
            "INSERT INTO calibration_center_width_fit_journal VALUES(?,?)",
            [
                ("P0", "2026-08-26T10:00:00Z"),
                ("P0-M", "2026-08-26T09:00:00Z"),
                ("P1", "2026-08-25T17:11:00Z"),
            ],
        )
        conn.executemany(
            "INSERT INTO calibration_center_width_station_journal VALUES(?,?)",
            [
                ("P0", "2026-08-26T10:00:05Z"),
                ("P0-M", "2026-08-26T09:00:05Z"),
                ("P1", "2026-08-25T17:13:07Z"),
            ],
        )
        assert stage_order_by_staleness(
            conn, ("P0", "P0-M", "P1")
        ) == ("P1", "P0-M", "P0")
    finally:
        conn.close()


def test_stage_order_ranks_a_stage_by_its_most_neglected_lane() -> None:
    """SQL's MIN drops the missing lane and would invert this exactly.

    The two lanes are separately isolated, so a stage can carry a row for one
    and not the other. Ranking it by the lane that did run puts the stage that
    should go first dead last.
    """

    from bayes_basket_target.center_width_store import stage_order_by_staleness

    conn = _journal_fixture()
    try:
        conn.executemany(
            "INSERT INTO calibration_center_width_fit_journal VALUES(?,?)",
            [
                ("P0", "2026-08-26T01:00:00Z"),
                ("P0-M", "2026-08-26T02:00:00Z"),
                ("P1", "2026-08-26T03:00:00Z"),
            ],
        )
        # P1's scalar lane is the newest of the three, but its station lane
        # has never run at all.
        conn.executemany(
            "INSERT INTO calibration_center_width_station_journal VALUES(?,?)",
            [("P0", "2026-08-26T01:05:00Z"), ("P0-M", "2026-08-26T02:05:00Z")],
        )
        assert stage_order_by_staleness(
            conn, ("P0", "P0-M", "P1")
        ) == ("P1", "P0", "P0-M")
    finally:
        conn.close()


def test_stage_order_is_canonical_when_nothing_has_been_fitted() -> None:
    """No journal at all is no reason to be non-deterministic."""

    from bayes_basket_target.center_width_store import stage_order_by_staleness

    conn = _journal_fixture()
    try:
        assert stage_order_by_staleness(
            conn, ("P0", "P0-M", "P1")
        ) == ("P0", "P0-M", "P1")
    finally:
        conn.close()

    missing = sqlite3.connect(":memory:")
    try:
        assert stage_order_by_staleness(
            missing, ("P0", "P0-M", "P1")
        ) == ("P0", "P0-M", "P1")
    finally:
        missing.close()


def test_explicit_stage_list_keeps_the_order_the_caller_gave() -> None:
    """Rotation is for the default sweep, not for a caller who named an order."""

    import bayes_basket_target.center_width_store as store

    seen: list[str] = []

    def _record_order(conn, wanted, results, commit, bound):  # noqa: ANN001, ARG001
        seen.extend(wanted)

    original = store._refit_stages
    store._refit_stages = _record_order
    conn = _journal_fixture()
    try:
        conn.execute(
            "INSERT INTO calibration_center_width_fit_journal VALUES(?,?)",
            ("P1", "2026-08-20T00:00:00Z"),
        )
        store.refit_all_center_width(conn, commit=False, stages=("P0", "P0-M"))
    finally:
        store._refit_stages = original
        conn.close()

    # Charter 150: a named stage that serves no table is retired, not fitted,
    # so only P0 reaches the fit -- in the caller's order.
    assert seen == ["P0"]


def _anchor_fixture() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE basket_probability_bridge_history("
        "stage TEXT,state TEXT,checkpoint_id TEXT,decision_at_utc TEXT)"
    )
    conn.execute(
        "CREATE TABLE basket_probability_envelope("
        "envelope_id TEXT PRIMARY KEY,posterior_version TEXT)"
    )
    return conn


def _add_decision(
    conn: sqlite3.Connection, *, at: str, version: str, stage: str = "P1"
) -> None:
    envelope_id = f"env-{at}-{version}"
    conn.execute(
        "INSERT OR IGNORE INTO basket_probability_envelope VALUES(?,?)",
        (envelope_id, version),
    )
    conn.execute(
        "INSERT INTO basket_probability_bridge_history VALUES(?,?,?,?)",
        (stage, "COMMITTED", envelope_id, at),
    )


def test_served_anchor_skips_the_scan_when_it_cannot_move_the_maximum() -> None:
    """The caller takes a maximum, so a candidate under it contributes nothing.

    One descending seek answers whether that is the case; the scan it replaces
    walks every row of the stage older than the served version's first
    appearance -- 147,870 of them for P1 in production, 27.0s against 1.5ms.
    """

    from bayes_basket_target.center_width_store import served_arithmetic_anchor

    conn = _anchor_fixture()
    try:
        _add_decision(conn, at="2026-08-20T00:00:00Z", version="v_now")
        _add_decision(conn, at="2026-08-24T00:00:00Z", version="v_now")

        # Unbounded, the anchor is the earliest decision carrying v_now.
        assert served_arithmetic_anchor(
            conn, stage="P1"
        ) == "2026-08-20T00:00:00Z"

        # A floor at or above that earliest decision cannot be raised by it.
        assert served_arithmetic_anchor(
            conn, stage="P1", known_floor="2026-08-22T00:00:00Z"
        ) is None
        assert served_arithmetic_anchor(
            conn, stage="P1", known_floor="2026-08-20T00:00:00Z"
        ) is None
    finally:
        conn.close()


def test_served_anchor_still_answers_when_it_would_win() -> None:
    """The skip must not fire on the one case the anchor exists for.

    A version bumped by hand in a constant the AST fingerprint does not hash
    -- P0's POSTERIOR_VERSION lives outside the hashed function -- moves this
    bound and nothing else.
    """

    from bayes_basket_target.center_width_store import served_arithmetic_anchor

    conn = _anchor_fixture()
    try:
        _add_decision(conn, at="2026-08-01T00:00:00Z", version="v_old")
        _add_decision(conn, at="2026-08-26T00:00:00Z", version="v_now")

        # Every decision carrying the served version is above the floor, so
        # the anchor genuinely raises it and must be computed.
        assert served_arithmetic_anchor(
            conn, stage="P1", known_floor="2026-08-10T00:00:00Z"
        ) == "2026-08-26T00:00:00Z"
    finally:
        conn.close()


def test_evidence_floor_takes_the_latest_bound_either_way() -> None:
    """Reordering the candidates may not change which bound wins."""

    import bayes_basket_target.center_width_store as store

    conn = _anchor_fixture()
    try:
        _add_decision(conn, at="2026-08-01T00:00:00Z", version="v_old")
        _add_decision(conn, at="2026-08-26T00:00:00Z", version="v_now")

        assert store._evidence_floor(
            conn, stage="P1", caller_bound="2026-08-02T00:00:00Z"
        ) == "2026-08-26T00:00:00Z"
        assert store._evidence_floor(
            conn, stage="P1", caller_bound="2026-08-27T00:00:00Z"
        ) == "2026-08-27T00:00:00Z"
    finally:
        conn.close()


def test_a_code_change_alone_does_not_spend_the_evidence() -> None:
    """The AST fingerprint is a watchdog now, not a boundary.

    It hashes the serving functions, so it moves for a commit that only
    records a receipt exactly as for a real change of arithmetic.  Measured
    2026-08-27, that cost P1 every clock it ever started: longest window
    held was 1.5 days against the eight the layer needs.
    """

    import bayes_basket_target.center_width_store as store

    conn = _anchor_fixture()
    try:
        _add_decision(conn, at="2026-08-26T00:00:00Z", version="v_now")
        original = store.serving_arithmetic.anchor
        store.serving_arithmetic.anchor = lambda conn, *, stage: (  # noqa: ARG005
            "2026-08-27T12:00:00Z"
        )
        try:
            assert store._evidence_floor(
                conn, stage="P1", caller_bound="2026-08-02T00:00:00Z"
            ) == "2026-08-26T00:00:00Z"
        finally:
            store.serving_arithmetic.anchor = original
    finally:
        conn.close()


def test_fingerprint_covers_the_training_arithmetic_a_stage_serves_from() -> None:
    """Charter 86 moved P0's served width by editing ``_fit_parameters`` --
    a function no fingerprint root named and none calls at serving time,
    because its output reaches serving baked into artifacts -- and the
    fingerprint did not move.  The closure names it now, for both stages
    built on P0's root; P1 serves from another lane and stays out."""

    from bayes_basket_target import serving_arithmetic

    # P0-M, which moved with P0's root, left with charter 283.
    assert serving_arithmetic.closure("P0-M") is None
    for stage in ("P0",):
        members = serving_arithmetic.closure(stage)
        assert members is not None
        names = {function for _module, function in members}
        assert "_fit_parameters" in names, stage
        assert "_resolved_bias_c" in names, stage
        assert "apply_center_width_calibration" in names, stage
        # Sorted and duplicate-free, so the hash cannot depend on traversal.
        assert members == sorted(set(members))
    p1 = {function for _m, function in serving_arithmetic.closure("P1")}
    assert "_fit_parameters" not in p1
    assert serving_arithmetic.fingerprint("P0") != serving_arithmetic.fingerprint("P1")


def test_an_acknowledged_fingerprint_puts_the_lamp_out_until_the_code_moves_again() -> None:
    """The watchdog's sentence has two halves and only had one place to say
    the first.  Measured 2026-09-08: discrepancy true for every stage on
    every day since the mechanism landed (P0: 17 fingerprints, 1 version).
    An acknowledgement quiets exactly the fingerprint it names."""

    from bayes_basket_target import serving_arithmetic

    conn = _anchor_fixture()
    try:
        original = serving_arithmetic.anchor
        original_fingerprint = serving_arithmetic.fingerprint
        serving_arithmetic.fingerprint = lambda stage: "deadbeef"  # noqa: ARG005
        try:
            _add_decision(conn, at="2026-08-26T00:00:00Z", version="v_now")
            serving_arithmetic.anchor = lambda conn, *, stage: (  # noqa: ARG005
                "2026-08-27T12:00:00Z"
            )
            assert serving_arithmetic.watchdog(conn, stage="P1")["discrepancy"] is True
            # No reason, no acknowledgement: the audit's record is the point.
            with pytest.raises(ValueError):
                serving_arithmetic.acknowledge(conn, stage="P1", reason="  ")
            receipt = serving_arithmetic.acknowledge(
                conn, stage="P1", reason="receipt-only refactor",
                now_utc="2026-08-28T00:00:00Z",
            )
            assert receipt["fingerprint"] == "deadbeef"
            after = serving_arithmetic.watchdog(conn, stage="P1")
            assert after["discrepancy"] is False
            assert after["acknowledged"]["reason"] == "receipt-only refactor"
            # The code moves again: a new fingerprint, and the lamp is back.
            serving_arithmetic.fingerprint = lambda stage: "cafebabe"  # noqa: ARG005
            assert serving_arithmetic.watchdog(conn, stage="P1")["discrepancy"] is True
        finally:
            serving_arithmetic.anchor = original
            serving_arithmetic.fingerprint = original_fingerprint
    finally:
        conn.close()


def test_arithmetic_moving_after_its_version_raises_a_discrepancy() -> None:
    """What the demoted bound still owes: naming the skipped version bump."""

    from bayes_basket_target import serving_arithmetic

    conn = _anchor_fixture()
    try:
        original = serving_arithmetic.anchor
        original_fingerprint = serving_arithmetic.fingerprint
        serving_arithmetic.fingerprint = lambda stage: "deadbeef"  # noqa: ARG005
        try:
            # The version was already serving when the code moved: the bump
            # is owed.
            _add_decision(conn, at="2026-08-26T00:00:00Z", version="v_now")
            serving_arithmetic.anchor = lambda conn, *, stage: (  # noqa: ARG005
                "2026-08-27T12:00:00Z"
            )
            late = serving_arithmetic.watchdog(conn, stage="P1")
            assert late["served_version"] == "v_now"
            assert late["discrepancy"] is True

            # The version started after the code moved: nothing is owed.
            serving_arithmetic.anchor = lambda conn, *, stage: (  # noqa: ARG005
                "2026-08-25T00:00:00Z"
            )
            assert serving_arithmetic.watchdog(
                conn, stage="P1"
            )["discrepancy"] is False
        finally:
            serving_arithmetic.anchor = original
            serving_arithmetic.fingerprint = original_fingerprint
    finally:
        conn.close()


class ResidualAndAbsoluteSpaceTest(unittest.TestCase):
    """The fit reads served cells; the server writes to uncorrected ones.

    ``serving_cells_by_day`` returns what production committed, so whatever
    the incumbent pair did is already inside every cell.  Two consequences the
    lane got wrong until 2026-09-10, in opposite directions:

    * scoring the incumbent by applying it a second time reads 0.75 as
      0.5625, so the incumbent looks worse than the distribution actually
      served and hands that margin to every candidate as free gain;
    * the grid's answer is a *residual* on top of that width, and storing it
      as the absolute the server applies is a different number entirely.

    On P0's own cells for 2026-09-03..09-09 the residual was exactly 1.000 --
    "today's width is right" -- and storing it would have widened every served
    P0 distribution by 33%, undoing the narrowing that took served sigma from
    1.36x the realised error to 1.16x.
    """

    @staticmethod
    def _stocked(stage: str, *, incumbent: tuple[float, float]) -> sqlite3.Connection:
        conn = _connection()
        conn.execute(
            "CREATE TABLE calibration_forward_taf_capture ("
            " station_id TEXT, event_date TEXT, tx_c REAL,"
            " tx_available INTEGER, issued_at_utc TEXT)"
        )
        stations = [f"ST{index:02d}" for index in range(15)]
        days = [f"2026-08-{day:02d}" for day in range(10, 22)]
        truth: dict[tuple[str, str], float] = {}
        rows = []
        for offset, day in enumerate(days):
            for index, station in enumerate(stations):
                rows.append((
                    stage, station, day, f"{day}T12:00:00Z",
                    _envelope(), "America/New_York",
                ))
                truth[(station, day)] = 20.0 + ((offset + index) % 3)
        conn.executemany(
            "INSERT INTO basket_probability_bridge_history VALUES"
            " (?,?,?,?,?,?,'','')",
            rows,
        )
        conn.executemany(
            "INSERT INTO calibration_station_daily_high_truth"
            " (station_id, local_date, official_tmax_c, revision_no, qc_status)"
            " VALUES (?,?,?,1,'PASS')",
            [(station, day, value) for (station, day), value in truth.items()],
        )
        conn.commit()
        persist_and_activate_center_width(
            conn,
            stage=stage,
            delta_c=incumbent[0],
            width_lambda=incumbent[1],
            evidence={"schema": "center_width_evidence_v1", "basis": "TEST"},
            serving_day_count=len(days),
        )
        assert load_center_width(conn, stage=stage) is not None
        return conn

    def test_the_incumbent_is_scored_as_the_cells_already_carry_it(self) -> None:
        conn = self._stocked("P1", incumbent=(0.0, 0.75))
        fitted = fit_center_width(conn, stage="P1")
        # The label moved when the gate did (2026-09-10, rolling origin);
        # what this test pins is the baseline the incumbent is scored at,
        # which is identity either way because the cells already carry it.
        self.assertEqual(fitted["evidence"]["basis"], "ROLLING_ORIGIN")
        truth = _truth(conn)
        by_day = serving_cells_by_day(conn, truth, stage="P1")
        days = sorted(by_day)
        cut = max(1, int(len(days) * (1.0 - FORWARD_WINDOW_FRACTION)))
        forward = [
            cell for day in (days[cut:] or days[-1:]) for cell in by_day[day]
        ]
        untransformed = _mean_nll(forward, delta_c=0.0, width_lambda=1.0)
        self.assertAlmostEqual(
            fitted["evidence"]["forward_nll_incumbent"], untransformed, places=9,
        )
        # And the reading the bug produced is a different number, so this
        # assertion cannot pass by both arms agreeing.
        self.assertNotAlmostEqual(
            untransformed,
            _mean_nll(forward, delta_c=0.0, width_lambda=0.75),
            places=6,
        )

    def test_what_is_stored_is_the_residual_composed_onto_the_incumbent(self) -> None:
        conn = self._stocked("P1", incumbent=(0.1, 0.75))
        fitted = fit_center_width(conn, stage="P1")
        evidence = fitted["evidence"]
        self.assertEqual(evidence["basis"], "ROLLING_ORIGIN")
        self.assertAlmostEqual(
            evidence["absolute_width_lambda"],
            round(0.75 * evidence["residual_width_lambda"], 4),
            places=9,
        )
        self.assertAlmostEqual(
            evidence["absolute_delta_c"],
            round(0.1 + evidence["residual_delta_c"], 4),
            places=9,
        )
        if fitted["admitted"]:
            self.assertEqual(
                fitted["width_lambda"], evidence["absolute_width_lambda"]
            )
            self.assertEqual(fitted["delta_c"], evidence["absolute_delta_c"])
        else:
            self.assertEqual(fitted["width_lambda"], 0.75)
            self.assertEqual(fitted["delta_c"], 0.1)

    def test_a_width_that_is_already_right_leaves_the_incumbent_alone(self) -> None:
        """The regression this class exists for, stated as behaviour.

        A residual of identity means the served width needs no further
        change.  Storing it as the absolute would throw the incumbent away.
        """

        conn = self._stocked("P1", incumbent=(0.0, 0.75))
        fitted = fit_center_width(conn, stage="P1")
        if fitted["evidence"]["residual_width_lambda"] == 1.0:
            self.assertNotEqual(fitted["width_lambda"], 1.0)
        self.assertGreaterEqual(fitted["width_lambda"], 0.5)
        self.assertLessEqual(fitted["width_lambda"], 0.75 * 1.5 + 1e-9)

    def test_an_incumbent_floor_refuses_rather_than_composing(self) -> None:
        """No composition rule exists for a floor, so none is invented."""

        conn = self._stocked("P1", incumbent=(0.0, 0.75))
        conn.execute(
            "UPDATE calibration_center_width_table SET sigma_floor_rungs=0.5"
            " WHERE stage='P1' AND activated=1"
        )
        fitted = fit_center_width(conn, stage="P1")
        self.assertEqual(
            fitted["evidence"]["basis"],
            "COMPOSITION_UNDEFINED_UNDER_FLOOR_OR_TIERS",
        )
        self.assertFalse(fitted["admitted"])
        self.assertEqual(fitted["width_lambda"], 0.75)
