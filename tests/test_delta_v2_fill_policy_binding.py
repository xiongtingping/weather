"""The fill-policy binding check across the placeholder/measured divide.

The check was born when every fill cell in a scope carried the same
placeholder distribution -- one sha per scope, so a policy could pin "the
fill model" by equalling the finalist cell's hash.  The 2026-08-22 refit
made distributions per-cell (151 hashes per scope) and no single value can
equal them cell by cell, so every resolution reaching the check failed and
no pin could have passed.  The check now accepts the legacy exact form OR
the fingerprint of the finalist's fitting batch.
"""

import sqlite3

import pytest

from bayes_basket_target.basket_kernel.contracts import (
    ContractError,
    content_sha256,
)
from bayes_basket_target.delta_v2_fill_artifact import (
    _fill_batch_fingerprint,
    _BATCH_FINGERPRINT_CACHE,
)


SCOPE = dict(
    station_id="*", stage="*", season_bin="summer",
    maturity_bin="d1", liquidity_bin="liquid",
)


def _db(rows):
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE basket_delta_v2_fill_artifact ("
        " station_id TEXT, stage TEXT, season_bin TEXT, maturity_bin TEXT,"
        " liquidity_bin TEXT, valid_from_utc TEXT, valid_until_utc TEXT,"
        " authority_scope TEXT, fill_distribution_artifact_sha256 TEXT)"
    )
    conn.executemany(
        "INSERT INTO basket_delta_v2_fill_artifact VALUES (?,?,?,?,?,?,?,?,?)",
        rows,
    )
    return conn


def _row(sha, station="*", valid_from="2026-08-22T04:38:38Z"):
    return (
        station, "*", "summer", "d1", "liquid",
        valid_from, "2026-09-21T04:37:38Z", "PRODUCTION_NO_LIVE", sha,
    )


def setup_function(_fn):
    # The cache is process-lifetime by design (rows are append-only in
    # production); tests use throwaway in-memory tables, so it must not
    # bleed between them.
    _BATCH_FINGERPRINT_CACHE.clear()


def test_fingerprint_is_over_the_sorted_distinct_batch_hashes():
    conn = _db([_row("b" * 64), _row("a" * 64), _row("a" * 64)])
    fingerprint = _fill_batch_fingerprint(
        conn, **SCOPE,
        valid_from_utc="2026-08-22T04:38:38Z",
        authority_scope="PRODUCTION_NO_LIVE",
    )
    assert fingerprint == content_sha256({
        "schema": "delta_v2_fill_batch_fingerprint_v1",
        "distribution_sha256s": ["a" * 64, "b" * 64],
    })


def test_single_cell_batch_fingerprint_differs_from_the_bare_hash():
    """The two pin vocabularies must never collide by accident.

    A placeholder-era batch has one hash; its fingerprint is the hash of a
    one-element list, not the bare hash itself, so an old exact pin can
    never accidentally satisfy the batch path or vice versa.
    """

    conn = _db([_row("c" * 64)])
    fingerprint = _fill_batch_fingerprint(
        conn, **SCOPE,
        valid_from_utc="2026-08-22T04:38:38Z",
        authority_scope="PRODUCTION_NO_LIVE",
    )
    assert fingerprint != "c" * 64


def test_batches_are_separated_by_valid_from_and_station():
    conn = _db([
        _row("a" * 64, valid_from="2026-07-29T01:15:00Z"),
        _row("b" * 64, valid_from="2026-08-22T04:38:38Z"),
        _row("d" * 64, station="RKPK", valid_from="2026-07-29T01:15:00Z"),
    ])
    old = _fill_batch_fingerprint(
        conn, **SCOPE,
        valid_from_utc="2026-07-29T01:15:00Z",
        authority_scope="PRODUCTION_NO_LIVE",
    )
    new = _fill_batch_fingerprint(
        conn, **SCOPE,
        valid_from_utc="2026-08-22T04:38:38Z",
        authority_scope="PRODUCTION_NO_LIVE",
    )
    rkpk = _fill_batch_fingerprint(
        conn, **{**SCOPE, "station_id": "RKPK"},
        valid_from_utc="2026-07-29T01:15:00Z",
        authority_scope="PRODUCTION_NO_LIVE",
    )
    assert len({old, new, rkpk}) == 3


def test_fingerprint_is_cached_per_batch():
    conn = _db([_row("a" * 64)])
    first = _fill_batch_fingerprint(
        conn, **SCOPE,
        valid_from_utc="2026-08-22T04:38:38Z",
        authority_scope="PRODUCTION_NO_LIVE",
    )
    # Same key must not re-query: drop the table and ask again.
    conn.execute("DROP TABLE basket_delta_v2_fill_artifact")
    again = _fill_batch_fingerprint(
        conn, **SCOPE,
        valid_from_utc="2026-08-22T04:38:38Z",
        authority_scope="PRODUCTION_NO_LIVE",
    )
    assert again == first


def test_binding_check_shape_exact_or_batch_never_silent():
    """The check's decision table, pinned as data.

    Exact match passes (placeholder-era pin against its own cell), batch
    fingerprint passes (re-pinned policy against the measured batch), and
    anything else -- including a stale placeholder pin against the measured
    batch, which is exactly the 2026-08-22..28 outage -- still refuses.
    """

    conn = _db([_row("a" * 64), _row("b" * 64)])
    batch = _fill_batch_fingerprint(
        conn, **SCOPE,
        valid_from_utc="2026-08-22T04:38:38Z",
        authority_scope="PRODUCTION_NO_LIVE",
    )
    finalist_sha = "a" * 64

    def check(pin):
        return pin == finalist_sha or pin == batch

    assert check(finalist_sha)          # legacy exact
    assert check(batch)                 # re-pinned batch
    assert not check("e" * 64)          # stale pin: still loud
