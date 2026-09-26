"""Charter 202: a version bump no longer restarts the lanes' evidence.

User ruling 2026-09-23: 「证据时钟不要清零，用旧的样本」.  The served
version's start stays what the watchdog and the board's version comparison
read; the calibration lanes read the stage's evidence epoch, and an old row
that recorded the width layer's input is re-served through today's function
before any lane scores it.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from bayes_basket_target import center_width_store as cws
from bayes_basket_target import serving_arithmetic as sa
from bayes_basket_target.center_width_calibration import (
    apply_center_width_calibration,
)

ROOT = Path(__file__).resolve().parents[1]


def test_a_served_version_after_the_epoch_reads_the_epoch() -> None:
    assert sa.evidence_effective_from(
        "P0", "calibrated_native_cross_ensemble_p0_v9"
    ) == sa.STAGE_EVIDENCE_EPOCH["P0"]
    assert sa.evidence_effective_from(
        "P0-M", "p0m_taf_likelihood_v11"
    ) == sa.STAGE_EVIDENCE_EPOCH["P0-M"]
    # The version's own start is untouched: the watchdog reads it.
    assert sa.version_effective_from(
        "P0", "calibrated_native_cross_ensemble_p0_v9"
    ) == "2026-09-22T12:00:00Z"


def test_a_version_older_than_the_epoch_keeps_its_own_start() -> None:
    assert sa.evidence_effective_from(
        "P0", "calibrated_native_cross_ensemble_p0_v4"
    ) == "2026-09-10T15:27:09Z"
    assert sa.evidence_effective_from("P0", "not_a_declared_version") is None


def test_the_epoch_is_compared_as_an_instant_not_as_text() -> None:
    # The P1 epoch carries microseconds; as text "...09Z" sorts after
    # "...09.000001Z" and the version starting at the epoch would be missed.
    assert sa.evidence_effective_from(
        "P1", "p1_joint_likelihood_v12"
    ) == sa.STAGE_EVIDENCE_EPOCH["P1"]
    assert sa.evidence_effective_from(
        "P1", "p1_metar_trajectory_target_v11"
    ) == "2026-09-18T09:18:16Z"


def _served(version: str) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE basket_probability_bridge_history("
        " stage TEXT, state TEXT, decision_at_utc TEXT, checkpoint_id TEXT)"
    )
    conn.execute(
        "CREATE TABLE basket_probability_envelope("
        " envelope_id TEXT, posterior_version TEXT)"
    )
    conn.execute(
        "INSERT INTO basket_probability_bridge_history VALUES"
        " ('P0','COMMITTED','2026-09-23T01:00:00Z','e1')"
    )
    conn.execute("INSERT INTO basket_probability_envelope VALUES ('e1', ?)", (version,))
    return conn


def test_the_lanes_floor_is_the_epoch_and_the_version_start_is_not_moved() -> None:
    conn = _served("calibrated_native_cross_ensemble_p0_v9")
    assert cws.served_arithmetic_anchor(conn, stage="P0") == "2026-09-22T12:00:00Z"
    assert cws.evidence_anchor(conn, stage="P0") == sa.STAGE_EVIDENCE_EPOCH["P0"]
    assert cws._evidence_floor(
        conn, stage="P0", caller_bound=None
    ) == sa.STAGE_EVIDENCE_EPOCH["P0"]
    # A caller's bound still wins when it is later.
    assert cws._evidence_floor(
        conn, stage="P0", caller_bound="2026-09-20T00:00:00Z"
    ) == "2026-09-20T00:00:00Z"


def test_every_lane_reads_the_evidence_anchor() -> None:
    from bayes_basket_target import beta_edge_store, valuation_store

    conn = _served("calibrated_native_cross_ensemble_p0_v9")
    for lane in (beta_edge_store, valuation_store):
        assert lane._anchor(conn, "P0") == (sa.STAGE_EVIDENCE_EPOCH["P0"], "ok")
    for module in (
        "band_reliability_store", "band_shadow_store", "beta_edge_store",
        "off_centre_claim_store", "valuation_store",
    ):
        source = (ROOT / "bayes_basket_target" / f"{module}.py").read_text()
        assert "served_arithmetic_anchor" not in source, module
        assert "evidence_anchor" in source, module


def test_the_nightly_sweep_passes_no_per_stage_clock() -> None:
    sweep = (ROOT / "scripts" / "calibration_refit_sweep.sh").read_text()
    call = sweep[sweep.index('"$PY" scripts/refit_center_width.py'):]
    call = call[:call.index("RC=$?")]
    assert "--since-stage" not in call


def _receipt(probability, *, state="APPLIED", tier=None, lam=0.75):
    return {
        "state": state, "delta_c": 0.0, "width_lambda": lam,
        "sigma_floor_rungs": 0.0, "width_tier_lambda": tier,
        "input_probability": dict(probability),
    }


KEYS = ["20°C or below", "21°C", "22°C", "23°C", "24°C or higher"]
CENTERS = [19.5, 21.0, 22.0, 23.0, 24.5]
OPEN = (20.0, 24.0)
INPUT = dict(zip(KEYS, [0.05, 0.2, 0.5, 0.2, 0.05]))


def test_an_old_row_is_re_served_through_todays_function() -> None:
    served = cws._reserved_under_current_width(
        _receipt(INPUT), keys=KEYS, centers=CENTERS,
        open_edge_bounds=OPEN, excluded=(),
    )
    today, _ = apply_center_width_calibration(
        INPUT, delta_c=0.0, width_lambda=0.75, ordered_keys=KEYS,
        key_centers_c=CENTERS, open_edge_bounds_c=OPEN,
    )
    total = sum(today.values())
    assert served == {k: v / total for k, v in today.items()}
    # The open tails keep mass: the pre-charter-190 transport emptied them.
    assert served["20°C or below"] > 0.0 and served["24°C or higher"] > 0.0


@pytest.mark.parametrize("receipt", [
    None,
    _receipt(INPUT, state="IDENTITY"),
    _receipt(INPUT, tier=0.8),
    {**_receipt(INPUT), "input_probability": None},
])
def test_a_row_with_nothing_exact_to_re_serve_keeps_its_served_vector(receipt) -> None:
    assert cws._reserved_under_current_width(
        receipt, keys=KEYS, centers=CENTERS, open_edge_bounds=OPEN, excluded=(),
    ) is None
