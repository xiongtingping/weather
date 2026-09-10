"""The set a scope serves is refitted beside the contest's own pick.

The serving sort prefers, before anything else, an activation whose system
set equals the packet's and whose fitted cycles need no rider.  A new fit can
only satisfy both by being fitted on exactly today's packet, and the contest
fits what it chooses -- a pair 92-97% of the time.  So an older row that
happens to match today's packet exactly outranks every newer fit for its
thirty-day window, and whatever only newer fits carry never reaches that
scope.  Measured 2026-09-10: 21 of 142 P0 scopes serving 08-25..09-03 rows
without the width layer while tagged successors were minted daily.

The refit gives the served identity a tagged twin that ties the old row on
set and riders and beats it on freshness.  Nothing about the sort, the
contest or the membership changes.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target import native_ensemble_oos as oos  # noqa: E402
from bayes_basket_target.contracts import ContractError  # noqa: E402


def _envelope_store(rows) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        "CREATE TABLE basket_probability_envelope ("
        " station_id TEXT, stage TEXT, state TEXT, decision_at_utc TEXT,"
        " lineage_json TEXT);"
        "CREATE TABLE calibration_cross_native_ensemble_artifact ("
        " content_sha256 TEXT, ensemble_system_ids_json TEXT,"
        " run_cycle_by_system_json TEXT);"
    )
    for station, decided, season, lead, sha in rows:
        conn.execute(
            "INSERT INTO basket_probability_envelope VALUES (?,?,?,?,?)",
            (station, "P0", "COMMITTED", decided, json.dumps({
                "season_bin": season,
                "training_lead_bucket": lead,
                "native_cross_ensemble_artifact_sha256": sha,
            })),
        )
    return conn


def _artifact(conn, sha, ids, cycles) -> None:
    conn.execute(
        "INSERT INTO calibration_cross_native_ensemble_artifact VALUES (?,?,?)",
        (sha, json.dumps(sorted(ids)), json.dumps(cycles)),
    )


NOW = "2026-09-10T12:00:00Z"


def test_the_served_identity_is_read_from_the_pinned_artifact() -> None:
    """The artifact's fitted cycles, not the packet's -- the sort compares those."""

    conn = _envelope_store([
        ("KAUS", "2026-09-10T05:30:00Z", "DESCENT", "h00_12", "old"),
        ("KAUS", "2026-09-09T05:30:00Z", "DESCENT", "h00_12", "older"),
    ])
    _artifact(conn, "old", ["B", "A"], {"A": "00Z", "B": "00Z"})
    _artifact(conn, "older", ["A", "C"], {"A": "00Z", "C": "12Z"})
    served = oos._served_cross_identities(conn, station_id="kaus", as_of_utc=NOW)
    assert served == {("DESCENT", "h00_12"): (("A", "B"), {"A": "00Z", "B": "00Z"})}


def test_a_scope_that_has_not_decided_recently_names_no_set() -> None:
    conn = _envelope_store([
        ("KAUS", "2026-09-01T05:30:00Z", "DESCENT", "h00_12", "old"),
    ])
    _artifact(conn, "old", ["A", "B"], {"A": "00Z", "B": "00Z"})
    assert oos._served_cross_identities(conn, station_id="KAUS", as_of_utc=NOW) == {}


def test_a_store_without_the_tables_is_not_a_station_with_a_served_set() -> None:
    conn = sqlite3.connect(":memory:")
    assert oos._served_cross_identities(conn, station_id="KAUS", as_of_utc=NOW) == {}


def test_a_single_system_or_mismatched_cycle_map_is_refused() -> None:
    conn = _envelope_store([
        ("KAUS", "2026-09-10T05:30:00Z", "DESCENT", "h00_12", "lone"),
        ("KAUS", "2026-09-10T05:30:00Z", "DESCENT", "h12_24", "torn"),
    ])
    _artifact(conn, "lone", ["A"], {"A": "00Z"})
    _artifact(conn, "torn", ["A", "B"], {"A": "00Z"})
    assert oos._served_cross_identities(conn, station_id="KAUS", as_of_utc=NOW) == {}


def test_the_lookup_seeks_the_recency_index() -> None:
    """A station's whole history is 150k rows; the bound is what keeps this cheap."""

    import inspect

    source = inspect.getsource(oos._served_cross_identities)
    assert "decision_at_utc>=?" in source
    assert "SERVED_SET_LOOKBACK_DAYS" in source


class PinnedSubsetTest:
    """``_select_minimum_crps_subset`` scores exactly the pinned set."""


def _selector_inputs():
    days = ("2026-09-01", "2026-09-02")
    probability = {"20": 0.2, "21": 0.6, "22": 0.2}
    predictions = {
        s: {d: {"probability": probability, "calibrated_center_c": 21.0,
                "actual_tmax_c": 21.0} for d in days}
        for s in ("A", "B", "C")
    }
    return dict(
        ready_system_ids=["A", "B", "C"],
        predictions_by_system=predictions,
        common_holdout=days,
        family_by_system={"A": "fa", "B": "fb", "C": "fc"},
        configured_caps={},
        train_error_scale={"A": 1.0, "B": 1.0, "C": 1.0},
        train_n_by_system={"A": 20, "B": 20, "C": 20},
    )


def test_a_pinned_subset_is_the_only_subset_scored() -> None:
    weights, receipt = oos._select_minimum_crps_subset(
        **_selector_inputs(), pinned_subset=["C", "A"]
    )
    assert sorted(weights) == ["A", "C"]
    assert receipt["rule"] == "SERVED_SET_REFIT_V1"
    assert receipt["evaluated_subset_count"] == 1
    assert receipt["winner"]["system_ids"] == ["A", "C"]


def test_without_a_pin_the_contest_is_unchanged() -> None:
    _weights, receipt = oos._select_minimum_crps_subset(**_selector_inputs())
    assert receipt["rule"] == "MIN_CRPS_EXACT_SHARED_HOLDOUT_SUBSET_V1"
    # 3 systems, 3 families: every subset of two or more.
    assert receipt["evaluated_subset_count"] == 4


def test_a_pin_outside_the_ready_set_fails_closed() -> None:
    with pytest.raises(ContractError) as excinfo:
        oos._select_minimum_crps_subset(
            **_selector_inputs(), pinned_subset=["A", "Z"]
        )
    assert "pinned_subset_not_ready" in str(excinfo.value)


def test_the_twin_is_labelled_outside_the_selection_identity() -> None:
    """The label must not perturb the identity a pointer was bound under."""

    import inspect

    identity = inspect.getsource(oos._selection_identity)
    assert "cross_selection_rule" not in identity
    assert "subset_selection" not in identity
    build = inspect.getsource(oos.build_native_ensemble_oos)
    assert "::SERVED_SET_REFIT" in build
    assert "pinned_subset=pinned_subset" in build
