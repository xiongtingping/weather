import sqlite3

import pytest

from bayes_basket_target import band_reliability_store
from bayes_basket_target.band_reliability_store import (
    BAND_STAGES,
    band_table_sha256,
    journal_band_fit,
    load_band_table,
    persist_and_activate_band_table,
    pinned_band_table_sha,
    refit_all_stages,
    _begin_immediate,
    _invert_band_receipt,
)
from bayes_basket_target.contracts import ContractError


def _factors():
    return (
        ((0.00, 0.02), 1.0),
        ((0.02, 0.12), 0.86),
        ((0.12, 0.20), 1.08),
        ((0.20, 0.35), 1.10),
        ((0.35, 1.01), 1.0),
    )


def test_persist_load_and_pin_roundtrip() -> None:
    """A refit activates the new table without touching pinned history."""

    conn = sqlite3.connect(":memory:")
    first = persist_and_activate_band_table(
        conn, stage="P0", factors=_factors(), evidence={"n": 1}, serving_day_count=15
    )
    assert first["state"] == "ACTIVATED"
    factors, sha = load_band_table(conn, stage="P0")
    assert sha == first["band_table_sha256"]
    assert dict(factors)[(0.02, 0.12)] == 0.86

    stronger = tuple(
        (band, 0.80 if band == (0.02, 0.12) else factor)
        for band, factor in _factors()
    )
    second = persist_and_activate_band_table(
        conn, stage="P0", factors=stronger, evidence={"n": 2}, serving_day_count=30
    )
    assert second["state"] == "ACTIVATED"
    assert second["previous"] == sha
    _active, active_sha = load_band_table(conn, stage="P0")
    assert active_sha == second["band_table_sha256"]
    # The old vintage stays loadable by pin: an artifact gated against it
    # must be served exactly it, refits notwithstanding.
    pinned, pinned_sha = load_band_table(conn, table_sha256=sha)
    assert pinned_sha == sha
    assert dict(pinned)[(0.02, 0.12)] == 0.86


def test_identical_refit_is_idempotent() -> None:
    conn = sqlite3.connect(":memory:")
    persist_and_activate_band_table(
        conn, stage="P0", factors=_factors(), evidence={}, serving_day_count=15
    )
    again = persist_and_activate_band_table(
        conn, stage="P0", factors=_factors(), evidence={}, serving_day_count=15
    )
    assert again["state"] == "UNCHANGED"
    count = conn.execute(
        "SELECT COUNT(*) FROM calibration_band_reliability_table"
    ).fetchone()[0]
    assert count == 1


def test_pinned_but_missing_table_fails_closed() -> None:
    conn = sqlite3.connect(":memory:")
    with pytest.raises(ContractError):
        load_band_table(conn, table_sha256="0" * 64)


def test_pinned_sha_parses_out_of_the_combination_method() -> None:
    sha = band_table_sha256(_factors())
    method = (
        "train_rmse_inverse_family_capped_v1::PROFILE"
        "::band_reliability_v1::band_table_sha256=" + sha
    )
    assert pinned_band_table_sha(method) == sha
    assert pinned_band_table_sha(
        "train_rmse_inverse_family_capped_v1::band_reliability_v1"
    ) is None


def test_receipt_inversion_recovers_the_preband_claim() -> None:
    """Post-band rows keep feeding the fit through exact inversion."""

    raw = [(0.06, "34"), (0.15, "35"), (0.79, "36")]
    factors = {"34": 0.86, "35": 1.08}
    served_mass = sum(p * factors.get(k, 1.0) for p, k in raw)
    served = [
        (p * factors.get(k, 1.0) / served_mass, k) for p, k in raw
    ]
    receipt = {
        "state": "APPLIED",
        "applied_factors": factors,
        "pre_normalisation_mass": served_mass,
    }
    recovered = _invert_band_receipt(served, receipt)
    for (probability, key), (expected, expected_key) in zip(recovered, raw):
        assert key == expected_key
        assert abs(probability - expected) < 1e-12


def test_stage_tables_are_independent_and_never_alias() -> None:
    """Identical factors in two stages are two tables, not one.

    Each stage's factors are fitted on that stage's own distribution, so an
    identity table for P1 must never be loadable as if it were P0's -- the
    stage is part of the content hash for exactly that reason.
    """

    conn = sqlite3.connect(":memory:")
    identity = tuple((band, 1.0) for band, _f in _factors())
    p0 = persist_and_activate_band_table(
        conn, stage="P0", factors=_factors(), evidence={},
        serving_day_count=15,
    )
    p1 = persist_and_activate_band_table(
        conn, stage="P1", factors=identity, evidence={},
        serving_day_count=11,
    )
    assert p0["band_table_sha256"] != p1["band_table_sha256"]
    assert band_table_sha256(identity, stage="P1") != band_table_sha256(
        identity, stage="P0"
    )
    # Activating P1 must not have deactivated P0.
    assert load_band_table(conn, stage="P0")[1] == p0["band_table_sha256"]
    assert load_band_table(conn, stage="P1")[1] == p1["band_table_sha256"]
    assert load_band_table(conn, stage="P0-M") is None


def test_journal_records_every_fit_including_non_promoting_ones() -> None:
    """The trail must show what was known, not only what was served."""

    conn = sqlite3.connect(":memory:")
    identity = tuple((band, 1.0) for band, _f in _factors())
    fitted = {
        "factors": identity,
        "serving_day_count": 4,
        "evidence": {
            "accumulating": True,
            "serving_station_day_count": 52,
            "basis": {"[0.02, 0.12]": "accumulating_evidence"},
        },
    }
    fit_id = journal_band_fit(
        conn, stage="P1", fitted=fitted, outcome="ACCUMULATING",
        fitted_at_utc="2026-08-15T02:00:00Z",
    )
    row = conn.execute(
        "SELECT stage, outcome, serving_day_count,"
        " serving_station_day_count, qualifying_cell_count"
        " FROM calibration_band_reliability_fit_journal WHERE fit_id=?",
        (fit_id,),
    ).fetchone()
    assert row == ("P1", "ACCUMULATING", 4, 52, 0)


def _evidence_lanes(conn: sqlite3.Connection) -> None:
    """The tables the fit reads.

    Empty is a legitimate state, not a broken one: a stage with no settled
    history fits to identity and journals itself as accumulating, which is
    exactly the path a refit takes for a stage that has just arrived.
    """

    conn.executescript(
        "CREATE TABLE calibration_station_daily_high_truth ("
        " station_id TEXT, local_date TEXT, official_tmax_c REAL);"
        "CREATE TABLE basket_probability_bridge_history ("
        " station_id TEXT, event_date TEXT, history_id TEXT,"
        " envelope_json TEXT, decision_at_utc TEXT, stage TEXT);"
        "CREATE TABLE basket_probability_bridge_bucket_probability ("
        " history_id TEXT, bucket TEXT, lower_c REAL, upper_c REAL,"
        " probability REAL);"
        "CREATE TABLE calibration_cross_native_ensemble_oos_prediction ("
        " station_id TEXT, event_date TEXT, probabilities_json TEXT,"
        " bucket_centers_c_json TEXT, cross_ensemble_artifact_id TEXT,"
        " trained_through_event_date TEXT, decision_at_utc TEXT,"
        " lead_bucket TEXT);"
        "CREATE TABLE calibration_cross_native_ensemble_artifact ("
        " cross_ensemble_artifact_id TEXT, combination_method TEXT);"
    )


def test_one_failing_stage_does_not_cost_the_other_stages(monkeypatch) -> None:
    """A stage that cannot persist loses itself and nothing else.

    Reproduces 2026-08-20: P0's ``BEGIN IMMEDIATE`` lost the race to the
    resident, the exception unwound the whole loop, and P0-M and P1 were
    never fitted -- not even journalled.  Their evidence had nothing to do
    with P0's lock.
    """

    conn = sqlite3.connect(":memory:")
    _evidence_lanes(conn)
    persist = band_reliability_store.persist_and_activate_band_table

    def contended(inner, *, stage, **kwargs):
        if stage != "P0":
            return persist(inner, stage=stage, **kwargs)
        # Die with a transaction open, as a failure inside the write would:
        # a poisoned connection would fail the next stage's BEGIN for an
        # unrelated reason and turn one lost stage back into three.
        inner.execute("BEGIN IMMEDIATE")
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(
        band_reliability_store, "persist_and_activate_band_table", contended
    )
    report = refit_all_stages(conn, bootstrap_samples=8, commit=True)

    assert set(report) == set(BAND_STAGES)
    assert "database is locked" in report["P0"]["error"]
    assert "fit_id" not in report["P0"]
    assert load_band_table(conn, stage="P0") is None
    for stage in ("P0-M", "P1"):
        entry = report[stage]
        assert "error" not in entry, entry["error"]
        assert entry["commit"]["state"] == "ACTIVATED"
        assert entry["accumulating"] is True
        assert load_band_table(conn, stage=stage)[1] == (
            entry["fitted_table_sha256"]
        )
    journalled = {
        (row[0], row[1])
        for row in conn.execute(
            "SELECT stage, outcome"
            " FROM calibration_band_reliability_fit_journal"
        )
    }
    assert journalled == {("P0-M", "ACCUMULATING"), ("P1", "ACCUMULATING")}


def test_write_lock_is_retried_for_contention_only(monkeypatch) -> None:
    """Wait out the resident; surface anything else on the first try."""

    slept: list[float] = []
    monkeypatch.setattr(band_reliability_store.time, "sleep", slept.append)

    class _Writer:
        def __init__(self, failures: int, message: str) -> None:
            self.remaining = failures
            self.message = message
            self.attempts = 0

        def execute(self, _sql: str) -> None:
            self.attempts += 1
            if self.remaining > 0:
                self.remaining -= 1
                raise sqlite3.OperationalError(self.message)

    busy = _Writer(2, "database is locked")
    assert _begin_immediate(busy) == 3
    assert len(slept) == 2

    wedged = _Writer(99, "database is locked")
    with pytest.raises(sqlite3.OperationalError):
        _begin_immediate(wedged)
    assert wedged.attempts == band_reliability_store.WRITE_LOCK_ATTEMPTS

    # A missing table is a fault, not contention: retrying it would spend
    # four busy_timeouts to learn what the first attempt already said.
    broken = _Writer(99, "no such table: calibration_band_reliability_table")
    with pytest.raises(sqlite3.OperationalError):
        _begin_immediate(broken)
    assert broken.attempts == 1
