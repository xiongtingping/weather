"""A version bump may say when its arithmetic began, and the lanes' roots.

Two halves of the same failure, both visible on 2026-09-10.

The anchor a calibration lane keys its evidence off was inferred: the earliest
decision still carrying the version being served now.  That makes a bump cost
the lane every day it accumulated, so the bump stops being made -- charter 122
wrote the trap down and paid a hand-set flag in the sweep instead: "A version
bump today would anchor at today and throw away the de-blended days already
served."  A version may now state the moment instead of having it reconstructed.

And the fingerprint could not see the lanes at all.  Charter 122 made the
training entry a root because "what P0 serves is decided as much when the
artifact is fitted as when it is compiled"; the three table lanes decide
numbers that reach serving the same way, through an activated row rather than
a call, and were never added.  Two commits that day changed what both lanes
will store and moved no stage's fingerprint, because only the LOADERS of those
tables were reachable from the serving roots.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target import serving_arithmetic as sa  # noqa: E402
from bayes_basket_target.center_width_store import (  # noqa: E402
    served_arithmetic_anchor,
)

STAGES = ("P0", "P0-M", "P1")


def _served(version: str) -> sqlite3.Connection:
    """One committed decision per stage, all carrying ``version``."""

    conn = sqlite3.connect(":memory:")
    conn.executescript(
        "CREATE TABLE basket_probability_bridge_history ("
        " stage TEXT, state TEXT, decision_at_utc TEXT, checkpoint_id TEXT);"
        "CREATE TABLE basket_probability_envelope ("
        " envelope_id TEXT, posterior_version TEXT);"
    )
    for index, moment in enumerate(
        ("2026-08-27T11:24:30Z", "2026-09-04T00:00:00Z", "2026-09-10T00:00:00Z")
    ):
        conn.execute(
            "INSERT INTO basket_probability_bridge_history VALUES (?,?,?,?)",
            ("P0", "COMMITTED", moment, f"env{index}"),
        )
        conn.execute(
            "INSERT INTO basket_probability_envelope VALUES (?,?)",
            (f"env{index}", version),
        )
    conn.commit()
    return conn


def test_without_a_declaration_the_anchor_is_the_first_decision() -> None:
    conn = _served("some_version_v2")
    assert served_arithmetic_anchor(conn, stage="P0") == "2026-08-27T11:24:30Z"


def test_a_declared_start_is_preferred_over_the_inference(monkeypatch) -> None:
    """The point of the mechanism: a bump typed on the 10th may name the 6th.

    Inferred, this version anchors at 2026-08-27 -- which is the bug the
    hand-set flag in the sweep was compensating for.
    """

    conn = _served("some_version_v3")
    monkeypatch.setattr(
        sa, "STAGE_VERSION_EFFECTIVE_FROM",
        {"P0": {"some_version_v3": "2026-09-06T04:18:00Z"}},
    )
    assert served_arithmetic_anchor(conn, stage="P0") == "2026-09-06T04:18:00Z"


def test_a_declaration_for_another_version_is_inert(monkeypatch) -> None:
    conn = _served("some_version_v2")
    monkeypatch.setattr(
        sa, "STAGE_VERSION_EFFECTIVE_FROM",
        {"P0": {"some_version_v3": "2026-09-06T04:18:00Z"}},
    )
    assert served_arithmetic_anchor(conn, stage="P0") == "2026-08-27T11:24:30Z"


def test_a_declaration_beats_the_callers_own_bound_shortcut(monkeypatch) -> None:
    """The known_floor shortcut must not answer before the declaration does.

    Called with a bound at or above the inferred minimum the anchor returns
    None ("cannot move your maximum"), which for a declared version would
    silently discard a boundary that is later than the caller's.
    """

    conn = _served("some_version_v3")
    monkeypatch.setattr(
        sa, "STAGE_VERSION_EFFECTIVE_FROM",
        {"P0": {"some_version_v3": "2026-09-06T04:18:00Z"}},
    )
    assert served_arithmetic_anchor(
        conn, stage="P0", known_floor="2026-09-01T00:00:00Z"
    ) == "2026-09-06T04:18:00Z"


def test_every_stage_hashes_the_lanes_that_decide_its_tables() -> None:
    """The gap: a change to what a lane will store moved no fingerprint."""

    wanted = {
        ("bayes_basket_target.center_width_store", "fit_center_width"),
        ("bayes_basket_target.center_width_store", "fit_station_width"),
        ("bayes_basket_target.band_reliability_store",
         "fit_band_reliability_table"),
    }
    for stage in STAGES:
        members = set(sa.closure(stage) or [])
        assert wanted <= members, stage
        # And the search inside them, not just the entry: the grid is where
        # the served scalar is actually chosen.
        assert ("bayes_basket_target.center_width_store", "_search") in members, stage


def test_the_instrument_is_not_part_of_the_arithmetic() -> None:
    """A lamp that reports its own maintenance is a lamp nobody reads.

    The lanes call ``served_arithmetic_anchor``, which calls back into this
    module, so without the carve-out every watchdog edit lights the watchdog.
    """

    for stage in STAGES:
        members = sa.closure(stage) or []
        assert not [m for m, _ in members if m in sa.EXCLUDED_MODULES], stage
    assert "bayes_basket_target.serving_arithmetic" in sa.EXCLUDED_MODULES


def test_a_root_is_never_excluded() -> None:
    """Exclusion stops the expansion walking back in; it is not a veto."""

    entries = [("bayes_basket_target.serving_arithmetic", "version_effective_from")]
    assert sa._closure(entries) == entries


def test_every_stage_still_has_a_fingerprint() -> None:
    seen = {stage: sa.fingerprint(stage) for stage in STAGES}
    assert all(value for value in seen.values())
    # Three stages, three different closures, three different hashes.
    assert len(set(seen.values())) == 3


def test_the_shipped_declarations_name_the_versions_that_are_served() -> None:
    """A declaration for a version nobody serves is inert and silently so.

    So the pairing is asserted here instead: the two stages that owe the
    2026-09-06 width backlog declare it, and they declare it for the exact
    string their own module emits.
    """

    from bayes_basket_target.materialization import materialize_p0m_observed
    from bayes_basket_target.native_ensemble_p0 import POSTERIOR_VERSION
    import inspect

    assert sa.version_effective_from("P0", POSTERIOR_VERSION) == (
        "2026-09-10T15:27:09Z"
    )
    p0m = inspect.signature(
        materialize_p0m_observed
    ).parameters["posterior_version"].default
    assert sa.version_effective_from("P0-M", p0m) == "2026-09-10T15:27:11Z"
    # The superseded declarations stay: inert for the versions nobody
    # serves, and the record of when v3's arithmetic began.
    assert sa.version_effective_from(
        "P0", "calibrated_native_cross_ensemble_p0_v3"
    ) == "2026-09-06T04:18:00Z"


def test_p1_declares_nothing_because_p1_bumps_on_time() -> None:
    """P1 went v2 to v9 while P0 sat on v2; its anchor is already honest."""

    from bayes_basket_target.materialization import materialize_p1
    import inspect

    p1 = inspect.signature(
        materialize_p1
    ).parameters["posterior_version"].default
    assert sa.version_effective_from("P1", p1) is None


def _watchdog_conn(version: str, *, first_decision: str) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        "CREATE TABLE basket_probability_bridge_history ("
        " stage TEXT, state TEXT, decision_at_utc TEXT, checkpoint_id TEXT);"
        "CREATE TABLE basket_probability_envelope ("
        " envelope_id TEXT, posterior_version TEXT);"
    )
    conn.execute(
        "INSERT INTO basket_probability_bridge_history VALUES (?,?,?,?)",
        ("P0", "COMMITTED", first_decision, "env0"),
    )
    conn.execute(
        "INSERT INTO basket_probability_envelope VALUES (?,?)",
        ("env0", version),
    )
    sa.ensure_schema(conn)
    conn.commit()
    return conn


def test_a_bump_does_not_clear_the_lamp_by_being_new(monkeypatch) -> None:
    """The hole the declaration would otherwise open.

    A brand new version string has no decision older than anything, so the
    inference answers "no discrepancy" for every bump, on merit or not.  Now
    that a bump is cheap the clearing must not be: a declared version is
    judged against the moment it says it began.
    """

    monkeypatch.setattr(
        sa, "STAGE_VERSION_EFFECTIVE_FROM",
        {"P0": {"declared_v3": "2026-09-06T04:18:00Z"}},
    )
    monkeypatch.setattr(sa, "fingerprint", lambda stage: "f" * 64)
    conn = _watchdog_conn("declared_v3", first_decision="2026-09-10T12:00:00Z")
    conn.execute(
        "INSERT INTO calibration_serving_arithmetic VALUES (?,?,?)",
        ("P0", "f" * 64, "2026-09-10T09:00:00Z"),
    )
    reading = sa.watchdog(conn, stage="P0")
    assert reading["version_effective_from_utc"] == "2026-09-06T04:18:00Z"
    # Code moved on the 10th, the version says it began on the 6th: owed.
    assert reading["discrepancy"] is True


def test_a_declared_version_whose_code_predates_it_is_clear(monkeypatch) -> None:
    monkeypatch.setattr(
        sa, "STAGE_VERSION_EFFECTIVE_FROM",
        {"P0": {"declared_v3": "2026-09-06T04:18:00Z"}},
    )
    monkeypatch.setattr(sa, "fingerprint", lambda stage: "e" * 64)
    conn = _watchdog_conn("declared_v3", first_decision="2026-09-10T12:00:00Z")
    conn.execute(
        "INSERT INTO calibration_serving_arithmetic VALUES (?,?,?)",
        ("P0", "e" * 64, "2026-09-02T09:00:00Z"),
    )
    assert sa.watchdog(conn, stage="P0")["discrepancy"] is False
