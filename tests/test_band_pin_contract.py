"""A pinned band table is held to what the pin claims (review 2026-09-13, R6).

The pinned loader used to trust the sha string: it never checked the row
belonged to the caller's stage, and a retired table read as identity on every
date, so replaying a decision made before the veto served different math
from the decision itself.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.band_reliability_store import (  # noqa: E402
    BAND_AXIS_CENTRE_DISTANCE,
    BAND_AXIS_CLAIM,
    ensure_band_table_schema,
    load_band_table,
    load_band_table_with_axis,
    persist_and_activate_band_table,
    retire_band_table,
)
from bayes_basket_target.contracts import ContractError, content_sha256  # noqa: E402

DISTANCE = (
    ((0.0, 1.0), 1.1), ((1.0, 2.0), 1.02), ((2.0, 3.0), 0.95),
    ((3.0, 4.0), 0.9), ((4.0, 99.0), 0.7),
)


def _store() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    ensure_band_table_schema(conn)
    return conn


def _persist(conn, stage, factors=DISTANCE, axis=BAND_AXIS_CENTRE_DISTANCE):
    return persist_and_activate_band_table(
        conn, stage=stage, factors=factors, evidence={"schema": "test"},
        serving_day_count=20, axis=axis,
        fitted_at_utc="2026-09-01T00:00:00Z",
    )["band_table_sha256"]


def test_a_pin_from_another_stage_fails_closed() -> None:
    conn = _store()
    p1_sha = _persist(conn, "P1")
    loaded = load_band_table_with_axis(conn, stage="P1", table_sha256=p1_sha)
    assert loaded is not None and loaded[2] == BAND_AXIS_CENTRE_DISTANCE
    with pytest.raises(ContractError, match="stage_mismatch"):
        load_band_table(conn, stage="P0", table_sha256=p1_sha)


def test_bytes_that_do_not_hash_to_the_pin_fail_closed() -> None:
    conn = _store()
    sha = _persist(conn, "P0")
    conn.execute(
        "UPDATE calibration_band_reliability_table SET factors_json=?"
        " WHERE band_table_sha256=?",
        ("[[[0.0, 1.0], 1.3], [[1.0, 99.0], 1.0]]", sha),
    )
    with pytest.raises(ContractError, match="content_mismatch"):
        load_band_table(conn, stage="P0", table_sha256=sha)


def test_an_illegal_factor_fails_closed_before_it_is_served() -> None:
    conn = _store()
    sha = _persist(conn, "P0")
    conn.execute(
        "UPDATE calibration_band_reliability_table SET factors_json=?"
        " WHERE band_table_sha256=?",
        ("[[[0.0, 1.0], -1.0], [[1.0, 99.0], 1.0]]", sha),
    )
    with pytest.raises(ContractError, match="factors_invalid"):
        load_band_table(conn, stage="P0", table_sha256=sha)


def test_the_pre_stage_bootstrap_identity_is_still_the_p0_claim_table() -> None:
    """The one row on the book that predates the stage column (9963e3fe)."""

    conn = _store()
    factors = (((0.0, 0.02), 1.0), ((0.02, 0.12), 0.86), ((0.12, 1.01), 1.0))
    canonical = [[[lo, hi], f] for (lo, hi), f in factors]
    legacy_sha = content_sha256({
        "schema": "band_reliability_table_v1", "factors": canonical,
    })
    conn.execute(
        "INSERT INTO calibration_band_reliability_table"
        " (band_table_sha256, stage, factors_json, evidence_json,"
        "  fitted_at_utc, serving_day_count, activated)"
        " VALUES (?, 'P0', ?, '{}', '2026-08-15T10:55:09Z', 20, 0)",
        (legacy_sha, str(canonical).replace("(", "[").replace(")", "]")),
    )
    loaded = load_band_table_with_axis(conn, stage="P0", table_sha256=legacy_sha)
    assert loaded is not None
    assert loaded[2] == BAND_AXIS_CLAIM
    assert loaded[0] == factors
    # The older identity is P0's alone: the same bytes filed under P1 are not
    # the table that sha names.
    conn.execute(
        "UPDATE calibration_band_reliability_table SET stage='P1'"
        " WHERE band_table_sha256=?",
        (legacy_sha,),
    )
    with pytest.raises(ContractError, match="content_mismatch"):
        load_band_table(conn, stage="P1", table_sha256=legacy_sha)


def test_retirement_is_read_at_the_moment_asked_about() -> None:
    conn = _store()
    sha = _persist(conn, "P0")
    retire_band_table(conn, stage="P0", retired_at_utc="2026-09-10T00:00:00Z")
    # Serving: retired now, so identity now.
    assert load_band_table(conn, stage="P0", table_sha256=sha) == ((), sha)
    # A replay of a decision made before the veto reads the table it served.
    before = load_band_table(
        conn, stage="P0", table_sha256=sha, as_of_utc="2026-09-09T12:00:00Z"
    )
    assert before == (DISTANCE, sha)
    after = load_band_table_with_axis(
        conn, stage="P0", table_sha256=sha, as_of_utc="2026-09-10T00:00:01Z"
    )
    assert after == ((), sha, BAND_AXIS_CENTRE_DISTANCE)
