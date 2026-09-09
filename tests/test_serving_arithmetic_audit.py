"""The daily question: does any stage owe the version bump its evidence keys off.

The watchdog has always been able to say "the code moved after the version
did".  What it could not say is whether that mattered, so it said it every day
about every stage and nobody read it -- which is how charter 86 changed P0's
served width on 2026-09-06 and left its posterior_version behind.  These tests
pin the part that makes the line worth reading: the judgement is made on what
was SERVED across the moment the fingerprint changed, not on the fact that
some file was edited.
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "acknowledge_serving_arithmetic",
    ROOT / "scripts" / "acknowledge_serving_arithmetic.py",
)
audit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(audit)


def _plans(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE basket_p0_preselection_plan ("
        " station_id TEXT, event_date TEXT, created_at TEXT, plan_json TEXT)"
    )


def _row(conn, *, created, stage, sigma, centre):
    payload = {
        "compiled_plan": {
            "stage": stage,
            "plan": {"skew_reading": {
                "sigma_buckets": sigma, "mean_ordinal": centre,
            }},
        }
    }
    conn.execute(
        "INSERT INTO basket_p0_preselection_plan VALUES(?,?,?,?)",
        ("X", "2026-09-09", created, json.dumps(payload)),
    )


def _side(conn, *, day, hour_from, hour_to, sigma, centre, stage="P0"):
    for hour in range(hour_from, hour_to):
        for minute in (0, 20, 40):
            _row(conn, created=f"2026-09-{day:02d}T{hour:02d}:{minute:02d}:00Z",
                 stage=stage, sigma=sigma, centre=centre)


def test_an_edit_that_moved_nothing_is_judged_inert() -> None:
    conn = sqlite3.connect(":memory:")
    _plans(conn)
    _side(conn, day=9, hour_from=0, hour_to=12, sigma=1.20, centre=5.0)
    _side(conn, day=9, hour_from=12, hour_to=23, sigma=1.21, centre=5.02)
    shift = audit._shape_shift(conn, stage="P0", since="2026-09-09T12:00:00Z")
    assert shift is not None
    assert shift["moved"] is False
    assert shift["sigma_fraction"] < audit.SHAPE_SHIFT_SIGMA_FRACTION
    conn.close()


def test_a_change_the_size_of_charter_86_is_judged_a_real_move() -> None:
    """The case the mechanism failed on: P0's served width fell 30%."""
    conn = sqlite3.connect(":memory:")
    _plans(conn)
    _side(conn, day=9, hour_from=0, hour_to=12, sigma=1.13, centre=5.0)
    _side(conn, day=9, hour_from=12, hour_to=23, sigma=0.72, centre=5.0)
    shift = audit._shape_shift(conn, stage="P0", since="2026-09-09T12:00:00Z")
    assert shift is not None and shift["moved"] is True
    assert shift["sigma_fraction"] > audit.SHAPE_SHIFT_SIGMA_FRACTION
    conn.close()


def test_a_centre_that_walks_counts_even_when_the_width_holds() -> None:
    """Width is not the only shape: a shifted centre is a served change too."""
    conn = sqlite3.connect(":memory:")
    _plans(conn)
    _side(conn, day=9, hour_from=0, hour_to=12, sigma=1.20, centre=5.0)
    _side(conn, day=9, hour_from=12, hour_to=23, sigma=1.20, centre=5.4)
    shift = audit._shape_shift(conn, stage="P0", since="2026-09-09T12:00:00Z")
    assert shift is not None and shift["moved"] is True
    assert shift["centre_shift"] > audit.SHAPE_SHIFT_CENTRE_BUCKETS
    conn.close()


def test_a_thin_side_is_unjudgeable_rather_than_judged() -> None:
    """``sigma_buckets`` reaches plans from 2026-09-08.  A fingerprint older
    than that has nothing to compare, and saying so is the honest answer --
    a default of "inert" would auto-acknowledge exactly the changes this
    lane exists to catch."""
    conn = sqlite3.connect(":memory:")
    _plans(conn)
    _side(conn, day=9, hour_from=12, hour_to=23, sigma=0.72, centre=5.0)
    assert audit._shape_shift(
        conn, stage="P0", since="2026-09-09T12:00:00Z"
    ) is None
    conn.close()


def test_one_stage_is_not_judged_on_another_stages_vectors() -> None:
    conn = sqlite3.connect(":memory:")
    _plans(conn)
    _side(conn, day=9, hour_from=0, hour_to=12, sigma=1.20, centre=5.0)
    _side(conn, day=9, hour_from=12, hour_to=23, sigma=1.20, centre=5.0)
    _side(conn, day=9, hour_from=0, hour_to=12, sigma=3.00, centre=9.0, stage="P1")
    _side(conn, day=9, hour_from=12, hour_to=23, sigma=0.30, centre=1.0, stage="P1")
    assert audit._shape_shift(
        conn, stage="P0", since="2026-09-09T12:00:00Z"
    )["moved"] is False
    assert audit._shape_shift(
        conn, stage="P1", since="2026-09-09T12:00:00Z"
    )["moved"] is True
    conn.close()


def test_an_unparsable_stamp_is_no_judgement() -> None:
    conn = sqlite3.connect(":memory:")
    _plans(conn)
    assert audit._shape_shift(conn, stage="P0", since="") is None
    assert audit._shape_shift(conn, stage="P0", since="not-a-time") is None
    conn.close()


def test_the_thresholds_are_the_measurement_they_were_set_from() -> None:
    """Charter 86 moved the width 30% and the centre a fifth of a bucket; the
    thresholds sit below both on purpose, so the change that motivated this
    would have tripped it with room to spare."""
    assert audit.SHAPE_SHIFT_SIGMA_FRACTION < 0.30
    assert audit.SHAPE_SHIFT_CENTRE_BUCKETS <= 0.20
    assert audit.SHAPE_SHIFT_WINDOW_HOURS <= 24


def test_acknowledging_still_demands_a_reason() -> None:
    """Only the finding that no bump is owed is automatic; a bump never is,
    and an acknowledgement without a reason is an unsigned judgement."""
    from bayes_basket_target import serving_arithmetic

    conn = sqlite3.connect(":memory:")
    with pytest.raises(ValueError):
        serving_arithmetic.acknowledge(
            conn, stage="P0", reason="   ", fingerprint_sha256="a" * 64
        )
    receipt = serving_arithmetic.acknowledge(
        conn, stage="P0", reason="served shape unchanged",
        fingerprint_sha256="a" * 64, now_utc="2026-09-09T00:00:00Z",
    )
    assert receipt["reason"] == "served shape unchanged"
    assert serving_arithmetic.acknowledged(
        conn, stage="P0", fingerprint_sha256="a" * 64
    )["reason"] == "served shape unchanged"
    conn.close()


def test_the_window_query_seeks_instead_of_reading_every_plan_body() -> None:
    """The audit asks "what was compiled in this window" across stations, and
    every other index on this table seeks on station_id first, so it had
    nothing to seek on: measured 2026-09-09, a one-day COUNT read all 68,933
    plan bodies and took 38 seconds.  Pinned as a PLAN, not a duration --
    a timing test on a shared machine measures the machine.
    """
    from bayes_basket_target import ssot

    conn = sqlite3.connect(":memory:")
    # Production's own statements for this one table, sliced out rather than
    # restated: the whole DDL has cross-table dependencies this test does not
    # need, and a hand-written copy of the schema would drift from the thing
    # it claims to pin.
    for statement in ssot.PRODUCTION_TARGET_DDL.split(";"):
        if "basket_p0_preselection_plan" in statement and (
            "CREATE TABLE" in statement or "CREATE INDEX" in statement
        ):
            conn.execute(statement)
    plan = "\n".join(
        str(row[3]) for row in conn.execute(
            "EXPLAIN QUERY PLAN"
            " SELECT plan_json FROM basket_p0_preselection_plan"
            " WHERE created_at>=? AND created_at<?",
            ("2026-09-08T00:00:00Z", "2026-09-09T00:00:00Z"),
        )
    )
    assert "basket_p0_preselection_created_idx" in plan, plan
    assert "SCAN" not in plan, plan
    conn.close()
