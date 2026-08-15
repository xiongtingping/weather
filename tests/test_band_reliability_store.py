import sqlite3

import pytest

from bayes_basket_target.band_reliability_store import (
    band_table_sha256,
    load_band_table,
    persist_and_activate_band_table,
    pinned_band_table_sha,
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
        conn, factors=_factors(), evidence={"n": 1}, serving_day_count=15
    )
    assert first["state"] == "ACTIVATED"
    factors, sha = load_band_table(conn)
    assert sha == first["band_table_sha256"]
    assert dict(factors)[(0.02, 0.12)] == 0.86

    stronger = tuple(
        (band, 0.80 if band == (0.02, 0.12) else factor)
        for band, factor in _factors()
    )
    second = persist_and_activate_band_table(
        conn, factors=stronger, evidence={"n": 2}, serving_day_count=30
    )
    assert second["state"] == "ACTIVATED"
    assert second["previous"] == sha
    _active, active_sha = load_band_table(conn)
    assert active_sha == second["band_table_sha256"]
    # The old vintage stays loadable by pin: an artifact gated against it
    # must be served exactly it, refits notwithstanding.
    pinned, pinned_sha = load_band_table(conn, table_sha256=sha)
    assert pinned_sha == sha
    assert dict(pinned)[(0.02, 0.12)] == 0.86


def test_identical_refit_is_idempotent() -> None:
    conn = sqlite3.connect(":memory:")
    persist_and_activate_band_table(
        conn, factors=_factors(), evidence={}, serving_day_count=15
    )
    again = persist_and_activate_band_table(
        conn, factors=_factors(), evidence={}, serving_day_count=15
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
