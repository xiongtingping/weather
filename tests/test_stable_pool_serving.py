"""Charter 134: the stable pool seats P0 end to end.

Build, persist and compile on the three-system pipeline fixture: two ECMWF
products (one family) and GEFS.  Under the stable pool every cohort mints its
whole pool and its missing-member views with shrunk skill weights, nothing is
chosen on the holdout, and the compile serves a pool seat ahead of any contest
artifact still in window.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target import native_ensemble_oos, native_ensemble_p0  # noqa: E402
from bayes_basket_target.native_ensemble_oos import (  # noqa: E402
    build_native_ensemble_oos,
    persist_native_ensemble_oos,
)
from bayes_basket_target.stable_pool import (  # noqa: E402
    STABLE_POOL_KAPPA,
    STABLE_POOL_WEIGHT_RULE,
    pool_subsets,
)

CYCLES = {
    "ECMWF_IFS_TEST": "18Z",
    "ECMWF_AIFS_TEST": "00Z",
    "NOAA_GEFS_TEST": "18Z",
}
PROVIDERS = {
    "ECMWF_IFS_TEST": "ECMWF_OPEN_DATA_S3",
    "ECMWF_AIFS_TEST": "ECMWF_OPEN_DATA_S3",
    "NOAA_GEFS_TEST": "NOAA_GEFS_PDS_S3",
}
FAMILIES = {
    "ECMWF_IFS_TEST": "ECMWF",
    "ECMWF_AIFS_TEST": "ECMWF",
    "NOAA_GEFS_TEST": "NOAA_GEFS",
}


@pytest.fixture()
def store(monkeypatch: pytest.MonkeyPatch) -> sqlite3.Connection:
    from tests.test_native_ensemble_oos_p0_pipeline import _insert_fixture
    from tests.test_native_ensemble_p0 import (
        _insert_official_native_mixed_cycle_archive,
    )

    conn = sqlite3.connect(":memory:")
    _insert_fixture(conn, cycle_by_system=CYCLES, lead_bucket="d2")
    _insert_official_native_mixed_cycle_archive(conn, CYCLES)
    for system_id, cycle in CYCLES.items():
        monkeypatch.setitem(
            native_ensemble_oos.OFFICIAL_ARCHIVE_SCOPE_CONTRACTS,
            system_id,
            {
                "transport_provider_id": PROVIDERS[system_id],
                "lead_bucket": "d2",
                "run_cycle": cycle,
            },
        )
    return conn


def _official(report):
    return [
        c for c in report["cross_candidates"]
        if isinstance(c, dict) and c.get("republisher_fallback_used") is False
    ]


def _build(conn, mode):
    return build_native_ensemble_oos(
        conn, station_id="TEST", as_of_utc="2026-07-01T00:00:00Z",
        membership_mode=mode,
    )


def test_the_pool_seats_every_view_and_contests_none(store) -> None:
    report = _build(store, "stable_pool")
    candidates = _official(report)
    assert candidates
    seated = {tuple(sorted(c["selected_system_ids"])) for c in candidates}
    whole = tuple(sorted(CYCLES))
    assert seated <= set(pool_subsets(whole, FAMILIES))
    assert whole in seated
    for candidate in candidates:
        receipt = candidate["subset_selection"]
        assert receipt["rule"] == "STABLE_POOL_SEAT_V1"
        assert receipt["weight_rule"] == STABLE_POOL_WEIGHT_RULE.upper()
        assert receipt["stable_pool_kappa"] == STABLE_POOL_KAPPA
        assert receipt["evaluated_subset_count"] == 1
        assert candidate["cross_selection_rule"].endswith("::STABLE_POOL_SEAT")
        # Nothing was picked on the holdout, so the score the seat was ranked
        # on and the score it is certified on are one number.
        for metric in ("crps", "nll", "brier"):
            assert receipt["winner"][metric] == pytest.approx(
                candidate["oos_metrics"][metric], abs=1e-12
            )
        weights = candidate["system_weights"]
        assert sum(weights.values()) == pytest.approx(1.0)
        # Shrinkage keeps every member at least kappa times its family share.
        families = {FAMILIES[s] for s in weights}
        for system_id, weight in weights.items():
            family_members = [s for s in weights if FAMILIES[s] == FAMILIES[system_id]]
            floor = STABLE_POOL_KAPPA / len(families) / len(family_members)
            assert weight >= floor - 1e-12, (system_id, weight, floor)
    contest = _official(_build(store, "subset_contest"))
    assert {c["subset_selection"]["rule"] for c in contest} == {
        "MIN_CRPS_EXACT_SHARED_HOLDOUT_SUBSET_V1"
    }


def test_a_pool_artifact_names_its_rule_and_its_shrinkage(store) -> None:
    report = _build(store, "stable_pool")
    identity = {k: v for k, v in report.items() if k != "content_sha256"}
    identity["cross_candidates"] = _official(report)
    from bayes_basket_target.contracts import content_sha256

    report = {**identity, "content_sha256": content_sha256(identity)}
    persisted = persist_native_ensemble_oos(
        store, report, activated_at_utc="2026-07-01T00:00:00Z"
    )
    assert persisted["state"] == "COMMITTED"
    methods = [
        row[0] for row in store.execute(
            "SELECT combination_method FROM calibration_cross_native_ensemble_artifact"
        )
    ]
    assert methods
    for method in methods:
        assert method.startswith(STABLE_POOL_WEIGHT_RULE + "::stable_pool_kappa=0.5::")


def _activation(activation_id, *, valid_from, method, generation=3):
    row = [None] * 16
    row[0] = activation_id
    row[3] = generation
    row[4] = valid_from
    row[15] = method
    return tuple(row)


def test_the_compile_ranks_a_pool_seat_ahead_of_a_newer_contest_row() -> None:
    """A contest row for a set the pool never mints, still in its window.

    An evaluation is superseded by a later one for the same identity, so a
    pool seat retires the contest row for its own set without help (see
    ``test_the_pool_seat_supersedes_the_contest_row_for_its_own_set``).  The
    rank's first key is for the rest: here a contest pair activated a day
    after a pool pair of different systems, equal on every other key.
    """

    observed = {"ECMWF_IFS_TEST", "ECMWF_AIFS_TEST", "NOAA_GEFS_TEST"}
    contest = (
        _activation("contest", valid_from="2026-07-01T00:00:00Z",
                    method="inverse_train_rmse_maturity_ramp_v1::x"),
        ["ECMWF_IFS_TEST", "NOAA_GEFS_TEST"], [], 1,
    )
    pool = (
        _activation("pool", valid_from="2026-06-30T00:00:00Z",
                    method=STABLE_POOL_WEIGHT_RULE + "::stable_pool_kappa=0.5::x"),
        ["ECMWF_AIFS_TEST", "NOAA_GEFS_TEST"], [], 1,
    )
    rank = native_ensemble_p0._activation_rank
    legacy = sorted([pool, contest], key=lambda i: rank(i, observed_set=observed, pool_first=False), reverse=True)
    pooled = sorted([contest, pool], key=lambda i: rank(i, observed_set=observed, pool_first=True), reverse=True)
    assert legacy[0][0][0] == "contest"
    assert pooled[0][0][0] == "pool"
    # An exact packet match does not outrank the pool key either.
    exact_contest = (contest[0], sorted(observed), [], 1)
    pooled = sorted([exact_contest, pool], key=lambda i: rank(i, observed_set=observed, pool_first=True), reverse=True)
    assert pooled[0][0][0] == "pool"


def test_the_pool_seat_supersedes_the_contest_row_for_its_own_set(store) -> None:
    """Minted after the switch, a pool seat is the only live row for its set."""

    from bayes_basket_target.contracts import content_sha256
    from tests.test_native_ensemble_p0 import _mixed_packet

    def _persist(mode, at):
        report = _build(store, mode)
        identity = {k: v for k, v in report.items() if k != "content_sha256"}
        identity["cross_candidates"] = _official(report)
        report = {**identity, "content_sha256": content_sha256(identity)}
        assert persist_native_ensemble_oos(store, report, activated_at_utc=at)["state"] == "COMMITTED"

    _persist("subset_contest", "2026-06-30T00:00:00Z")
    _persist("stable_pool", "2026-07-01T00:00:00Z")
    for mode in ("subset_contest", "stable_pool"):
        native_ensemble_p0.P0_MEMBERSHIP_MODE = mode
        try:
            compiled = native_ensemble_p0.compile_native_ensemble_p0(
                store, forward_packet=_mixed_packet(),
                decision_at_utc="2026-07-01T13:00:00Z",
            )
        finally:
            native_ensemble_p0.P0_MEMBERSHIP_MODE = "stable_pool"
        lineage = compiled["packet"]["native_probability_envelope"]["lineage"]
        method = store.execute(
            "SELECT combination_method FROM calibration_cross_native_ensemble_artifact"
            " WHERE content_sha256=?", (lineage["native_cross_ensemble_artifact_sha256"],),
        ).fetchone()[0]
        assert STABLE_POOL_WEIGHT_RULE in method, mode


def test_charter_134_names_the_rule_the_code_serves() -> None:
    from bayes_basket_target import stable_pool

    charter = (ROOT / "docs" / "constitution" / "SYSTEM_CHARTER.md").read_text(encoding="utf-8")
    clause = charter.split("\n134. ", 1)[1]
    assert "稳定合格池" in clause
    assert f"κ={stable_pool.STABLE_POOL_KAPPA:g}" in clause
    assert "stable_pool_shrunk_skill_v1::stable_pool_kappa=0.5" in clause
    assert "P0_MEMBERSHIP_MODE=subset_contest" in clause
    assert stable_pool.STABLE_POOL_WEIGHT_RULE == "stable_pool_shrunk_skill_v1"
    # The default is the ruling's, absent an explicit rollback in the env.
    import os

    if not os.environ.get("P0_MEMBERSHIP_MODE"):
        assert stable_pool.P0_MEMBERSHIP_MODE == "stable_pool"
    clause_42 = charter.split("\n42. ", 1)[1].split("\n43. ", 1)[0]
    assert "第 134 条退役本款「由子集赛唯一授予」一句" in clause_42
