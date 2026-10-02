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


# Charter 160: the instant P0 v7, P0-M v9 and P1 v16 declare.
CHARTER_160_START = "2026-09-19T15:02:00Z"
# Charter 190: the instant P0 v8, P0-M v10 and P1 v18 declare.
CHARTER_190_START = sa.version_effective_from(
    "P0", "calibrated_native_cross_ensemble_p0_v8"
)
# Charter 198: the instant P0 v9, P0-M v11 and P1 v19 declare.
CHARTER_198_START = sa.version_effective_from(
    "P0", "calibrated_native_cross_ensemble_p0_v9"
)
# Charter 204: the instant P0 v10, P0-M v12 and P1 v21 declare.
CHARTER_204_START = sa.version_effective_from(
    "P0", "calibrated_native_cross_ensemble_p0_v10"
)
# Charter 210: the instant P0 v11, P0-M v13 and P1 v22 declare.
CHARTER_210_START = sa.version_effective_from(
    "P0", "calibrated_native_cross_ensemble_p0_v11"
)
# Charter 212: the instant P0 v12, P0-M v14 and P1 v23 declare.
CHARTER_212_START = sa.version_effective_from(
    "P0", "calibrated_native_cross_ensemble_p0_v12"
)
# Charter 199 (the blend serves): the instant P0 v13, P0-M v15 and P1 v24 declare.
CHARTER_199_START = sa.version_effective_from(
    "P0", "calibrated_native_cross_ensemble_p0_v13"
)
# Charter 216: the instant P0 v14, P0-M v16 and P1 v25 declare.
CHARTER_216_START = sa.version_effective_from(
    "P0", "calibrated_native_cross_ensemble_p0_v14"
)
# Charter 217: the instant P0 v15, P0-M v17 and P1 v26 declare.
CHARTER_217_START = sa.version_effective_from(
    "P0", "calibrated_native_cross_ensemble_p0_v15"
)
# Charter 222: the instant P0 v16, P0-M v18 and P1 v27 declare.
CHARTER_222_START = sa.version_effective_from(
    "P0", "calibrated_native_cross_ensemble_p0_v16"
)
# Charter 224: the instant P0 v17, P0-M v19 and P1 v28 declare.
CHARTER_224_START = sa.version_effective_from(
    "P0", "calibrated_native_cross_ensemble_p0_v17"
)
# Charter 222's ruling (0.18 / 0.06): the instant P0 v18, P0-M v20 and P1 v29 declare.
CHARTER_222_PAIR_START = sa.version_effective_from(
    "P0", "calibrated_native_cross_ensemble_p0_v18"
)
# Charter 199 (the US ASOS stations blended): the instant P0 v19, P0-M v21 and P1 v30 declare.
CHARTER_199_ASOS_START = sa.version_effective_from(
    "P0", "calibrated_native_cross_ensemble_p0_v19"
)
# Charter 236 (pool seats rank breadth before the mint clock): the instant P0
# v20, P0-M v22 and P1 v31 declare.
CHARTER_236_START = sa.version_effective_from(
    "P0", "calibrated_native_cross_ensemble_p0_v20"
)
# Charter 241 (P0-M's observed bound censors): P0-M's own change, declared
# after v22; P1 is not bumped (it roots on the proportional shape).
CHARTER_241_P0M_START = "2026-10-01T16:30:00Z"
# Charter 242 (P0 fusion station x model weights): the instant P0 v21, P0-M
# v24 and P1 v32 declare.
CHARTER_242_START = sa.version_effective_from(
    "P0", "calibrated_native_cross_ensemble_p0_v21"
)
# Charter 243 (pool seats rank breadth before riders): P0 v22, P0-M v25, P1 v33.
CHARTER_243_START = sa.version_effective_from(
    "P0", "calibrated_native_cross_ensemble_p0_v22"
)
# Charter 244 (stable-pool seat kappa 0.25): P0 v23, P0-M v26, P1 v34.
CHARTER_244_START = sa.version_effective_from(
    "P0", "calibrated_native_cross_ensemble_p0_v23"
)
# Charter 246 (consensus bridge ramp 1.0-1.5C): P0 v24, P0-M v27, P1 v35.
CHARTER_246_START = sa.version_effective_from(
    "P0", "calibrated_native_cross_ensemble_p0_v24"
)
# Charter 247 (single-system width by widening the kernel): P0 v25, P0-M v28, P1 v36.
CHARTER_247_START = sa.version_effective_from(
    "P0", "calibrated_native_cross_ensemble_p0_v25"
)
# Charter 248 (thin-far slice is a tail mix on the stage pair): P0 v26, P0-M v29, P1 v37.
CHARTER_248_START = sa.version_effective_from(
    "P0", "calibrated_native_cross_ensemble_p0_v26"
)


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
    }
    # Charter 150: P1 applies no table; P0-M applies none of its own but is
    # built on P0's root, whose tables it therefore moves with.
    for stage in ("P0", "P0-M"):
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

    # Charter 198 (P0 v9) is declared past its push and the restart that
    # deploys it; v8 keeps charter 190's moment, v7 charter 160's and v6
    # charter 149's.
    assert POSTERIOR_VERSION == "calibrated_native_cross_ensemble_p0_v26"
    assert CHARTER_190_START and CHARTER_190_START > CHARTER_160_START
    assert CHARTER_198_START and CHARTER_198_START > CHARTER_190_START
    assert CHARTER_204_START and CHARTER_204_START > CHARTER_198_START
    assert CHARTER_210_START and CHARTER_210_START > CHARTER_204_START
    assert CHARTER_212_START and CHARTER_212_START > CHARTER_210_START
    assert CHARTER_199_START and CHARTER_199_START > CHARTER_212_START
    assert CHARTER_216_START and CHARTER_216_START > CHARTER_199_START
    assert CHARTER_217_START and CHARTER_217_START > CHARTER_216_START
    assert CHARTER_222_START and CHARTER_222_START > CHARTER_217_START
    assert CHARTER_224_START and CHARTER_224_START > CHARTER_222_START
    assert CHARTER_222_PAIR_START and CHARTER_222_PAIR_START > CHARTER_224_START
    assert CHARTER_199_ASOS_START and CHARTER_199_ASOS_START > CHARTER_222_PAIR_START
    assert CHARTER_236_START and CHARTER_236_START > CHARTER_199_ASOS_START
    assert CHARTER_242_START and CHARTER_242_START > CHARTER_241_P0M_START > CHARTER_236_START
    assert CHARTER_243_START and CHARTER_243_START > CHARTER_242_START
    assert CHARTER_244_START and CHARTER_244_START > CHARTER_243_START
    assert CHARTER_246_START and CHARTER_246_START > CHARTER_244_START
    assert CHARTER_247_START and CHARTER_247_START > CHARTER_246_START
    assert CHARTER_248_START and CHARTER_248_START > CHARTER_247_START
    assert sa.version_effective_from("P0", POSTERIOR_VERSION) == CHARTER_248_START
    assert sa.version_effective_from(
        "P0", "calibrated_native_cross_ensemble_p0_v23"
    ) == CHARTER_244_START
    assert sa.version_effective_from(
        "P0", "calibrated_native_cross_ensemble_p0_v22"
    ) == CHARTER_243_START
    assert sa.version_effective_from(
        "P0", "calibrated_native_cross_ensemble_p0_v21"
    ) == CHARTER_242_START
    assert sa.version_effective_from(
        "P0", "calibrated_native_cross_ensemble_p0_v20"
    ) == CHARTER_236_START
    assert sa.version_effective_from(
        "P0", "calibrated_native_cross_ensemble_p0_v19"
    ) == CHARTER_199_ASOS_START
    assert sa.version_effective_from(
        "P0", "calibrated_native_cross_ensemble_p0_v18"
    ) == CHARTER_222_PAIR_START
    assert sa.version_effective_from(
        "P0", "calibrated_native_cross_ensemble_p0_v17"
    ) == CHARTER_224_START
    assert sa.version_effective_from(
        "P0", "calibrated_native_cross_ensemble_p0_v16"
    ) == CHARTER_222_START
    assert sa.version_effective_from(
        "P0", "calibrated_native_cross_ensemble_p0_v15"
    ) == CHARTER_217_START
    assert sa.version_effective_from(
        "P0", "calibrated_native_cross_ensemble_p0_v14"
    ) == CHARTER_216_START
    assert sa.version_effective_from(
        "P0", "calibrated_native_cross_ensemble_p0_v13"
    ) == CHARTER_199_START
    assert sa.version_effective_from(
        "P0", "calibrated_native_cross_ensemble_p0_v12"
    ) == CHARTER_212_START
    assert sa.version_effective_from(
        "P0", "calibrated_native_cross_ensemble_p0_v11"
    ) == CHARTER_210_START
    assert sa.version_effective_from(
        "P0", "calibrated_native_cross_ensemble_p0_v10"
    ) == CHARTER_204_START
    assert sa.version_effective_from(
        "P0", "calibrated_native_cross_ensemble_p0_v9"
    ) == CHARTER_198_START
    assert sa.version_effective_from(
        "P0", "calibrated_native_cross_ensemble_p0_v8"
    ) == CHARTER_190_START
    assert sa.version_effective_from(
        "P0", "calibrated_native_cross_ensemble_p0_v7"
    ) == CHARTER_160_START
    assert sa.version_effective_from(
        "P0", "calibrated_native_cross_ensemble_p0_v6"
    ) == "2026-09-18T09:18:16Z"
    p0m = inspect.signature(
        materialize_p0m_observed
    ).parameters["posterior_version"].default
    # Charter 158's v8 is declared at the restart that deployed it, after
    # the tempered artifact was written.  Charter 150's v7 was this stage's
    # own change and declared its own first decision -- the value the
    # inference returned, written down because inferring it scans the
    # stage's whole history; v6 keeps the declaration of the P0 bump it
    # followed.
    # Charter 160's v9 is a root bump at P0 v7's moment; v8 keeps its own.
    # Charter 190's v10 is a root bump at P0 v8's moment, charter 198's v11
    # at P0 v9's.
    # Charter 204's v12 and charter 210's v13 are root bumps at P0 v10's and
    # P0 v11's moments.
    assert p0m == "p0m_taf_likelihood_v29"
    assert sa.version_effective_from("P0-M", p0m) == CHARTER_248_START
    assert sa.version_effective_from("P0-M", "p0m_taf_likelihood_v28") == CHARTER_247_START
    assert sa.version_effective_from("P0-M", "p0m_taf_likelihood_v27") == CHARTER_246_START
    assert sa.version_effective_from("P0-M", "p0m_taf_likelihood_v26") == CHARTER_244_START
    assert sa.version_effective_from("P0-M", "p0m_taf_likelihood_v25") == CHARTER_243_START
    assert sa.version_effective_from("P0-M", "p0m_taf_likelihood_v24") == CHARTER_242_START
    assert sa.version_effective_from("P0-M", "p0m_taf_likelihood_v23") == CHARTER_241_P0M_START
    assert sa.version_effective_from("P0-M", "p0m_taf_likelihood_v22") == CHARTER_236_START
    assert sa.version_effective_from("P0-M", "p0m_taf_likelihood_v21") == CHARTER_199_ASOS_START
    assert sa.version_effective_from("P0-M", "p0m_taf_likelihood_v20") == CHARTER_222_PAIR_START
    assert sa.version_effective_from("P0-M", "p0m_taf_likelihood_v19") == CHARTER_224_START
    assert sa.version_effective_from("P0-M", "p0m_taf_likelihood_v18") == CHARTER_222_START
    assert sa.version_effective_from(
        "P0-M", "p0m_taf_likelihood_v17"
    ) == CHARTER_217_START
    assert sa.version_effective_from(
        "P0-M", "p0m_taf_likelihood_v16"
    ) == CHARTER_216_START
    assert sa.version_effective_from(
        "P0-M", "p0m_taf_likelihood_v15"
    ) == CHARTER_199_START
    assert sa.version_effective_from(
        "P0-M", "p0m_taf_likelihood_v14"
    ) == CHARTER_212_START
    assert sa.version_effective_from(
        "P0-M", "p0m_taf_likelihood_v13"
    ) == CHARTER_210_START
    assert sa.version_effective_from(
        "P0-M", "p0m_taf_likelihood_v12"
    ) == CHARTER_204_START
    assert sa.version_effective_from(
        "P0-M", "p0m_taf_likelihood_v11"
    ) == CHARTER_198_START
    assert sa.version_effective_from(
        "P0-M", "p0m_taf_likelihood_v10"
    ) == CHARTER_190_START
    assert sa.version_effective_from(
        "P0-M", "p0m_taf_likelihood_v9"
    ) == CHARTER_160_START
    assert sa.version_effective_from(
        "P0-M", "p0m_taf_likelihood_v8"
    ) == "2026-09-19T10:15:00Z"
    assert sa.version_effective_from(
        "P0-M", "p0m_taf_likelihood_v7"
    ) == "2026-09-18T19:10:09Z"
    assert sa.version_effective_from(
        "P0-M", "p0m_taf_observed_v6"
    ) == "2026-09-18T09:18:16Z"
    assert sa.version_effective_from(
        "P0", "calibrated_native_cross_ensemble_p0_v5"
    ) == "2026-09-14T05:52:13Z"
    assert sa.version_effective_from(
        "P0", "calibrated_native_cross_ensemble_p0_v4"
    ) == "2026-09-10T15:27:09Z"
    # The superseded declarations stay: inert for the versions nobody
    # serves, and the record of when v3's arithmetic began.
    assert sa.version_effective_from(
        "P0", "calibrated_native_cross_ensemble_p0_v3"
    ) == "2026-09-06T04:18:00Z"


def test_p1_declares_only_the_bump_its_root_caused() -> None:
    """P1 bumps on time for its own changes (v2 to v9, none declared).

    v10 was caused upstream: charter 134 changed the P0 root P1 composes on,
    so it declares the root's moment, the instant P0 and P0-M v5 declare.
    v11 is the second such bump (charter 149) and declares P0 v6's moment.
    v12 (charter 150) is P1's own again, and declares its own first decision
    verbatim: inferring it walked every older P1 row, 62s per reader.
    """

    from bayes_basket_target.materialization import materialize_p1
    from bayes_basket_target.native_ensemble_p0 import POSTERIOR_VERSION
    import inspect

    p1 = inspect.signature(
        materialize_p1
    ).parameters["posterior_version"].default
    # v15 (charter 158) is the third root bump: P0-M's TAF likelihood is
    # tempered and every P1 composes on a P0-M, so it declares P0-M v8's
    # moment.  v14 (charter 157) and v13 (charter 156) are P1's own changes,
    # each declared at the restart that deployed it -- after the resident's
    # start, so no older envelope falls in the newer version's window.
    # v16 (charter 160) is the fourth root bump, at P0 v7's moment; v15
    # keeps P0-M v8's.  v17 (charter 161) is P1's own change, declared ahead
    # of its restart like v14.  v18 (charter 190) is the fifth root bump, at
    # P0 v8's moment; v19 (charter 198) the sixth, at P0 v9's.  v20
    # (charter 200) is P1's own change, declared past its push and restart.
    # v21 (charter 204) and v22 (charter 210) are the seventh and eighth
    # root bumps, at P0 v10's and P0 v11's moments.
    assert p1 == "p1_joint_likelihood_v37"
    assert sa.version_effective_from("P1", p1) == CHARTER_248_START
    assert sa.version_effective_from("P1", "p1_joint_likelihood_v36") == CHARTER_247_START
    assert sa.version_effective_from("P1", "p1_joint_likelihood_v35") == CHARTER_246_START
    assert sa.version_effective_from("P1", "p1_joint_likelihood_v34") == CHARTER_244_START
    assert sa.version_effective_from("P1", "p1_joint_likelihood_v33") == CHARTER_243_START
    assert sa.version_effective_from("P1", "p1_joint_likelihood_v32") == CHARTER_242_START
    assert sa.version_effective_from("P1", "p1_joint_likelihood_v31") == CHARTER_236_START
    assert sa.version_effective_from("P1", "p1_joint_likelihood_v30") == CHARTER_199_ASOS_START
    assert sa.version_effective_from("P1", "p1_joint_likelihood_v29") == CHARTER_222_PAIR_START
    assert sa.version_effective_from("P1", "p1_joint_likelihood_v28") == CHARTER_224_START
    assert sa.version_effective_from("P1", "p1_joint_likelihood_v27") == CHARTER_222_START
    assert sa.version_effective_from(
        "P1", "p1_joint_likelihood_v26"
    ) == CHARTER_217_START
    assert sa.version_effective_from(
        "P1", "p1_joint_likelihood_v25"
    ) == CHARTER_216_START
    assert sa.version_effective_from(
        "P1", "p1_joint_likelihood_v24"
    ) == CHARTER_199_START
    assert sa.version_effective_from(
        "P1", "p1_joint_likelihood_v23"
    ) == CHARTER_212_START
    assert sa.version_effective_from(
        "P1", "p1_joint_likelihood_v22"
    ) == CHARTER_210_START
    assert sa.version_effective_from(
        "P1", "p1_joint_likelihood_v21"
    ) == CHARTER_204_START
    assert sa.version_effective_from("P1", "p1_joint_likelihood_v20") > CHARTER_198_START
    assert sa.version_effective_from(
        "P1", "p1_joint_likelihood_v19"
    ) == CHARTER_198_START
    assert sa.version_effective_from(
        "P1", "p1_joint_likelihood_v18"
    ) == CHARTER_190_START
    assert sa.version_effective_from(
        "P1", "p1_joint_likelihood_v17"
    ) == "2026-09-19T17:20:00Z"
    assert sa.version_effective_from("P1", "p1_joint_likelihood_v16") == CHARTER_160_START
    assert sa.version_effective_from(
        "P1", "p1_joint_likelihood_v15"
    ) == sa.version_effective_from("P0-M", "p0m_taf_likelihood_v8")
    assert sa.version_effective_from(
        "P1", "p1_joint_likelihood_v14"
    ) == "2026-09-19T09:05:00Z"
    # v13 (charter 156) keeps the moment its restart declared.
    assert sa.version_effective_from("P1", "p1_joint_likelihood_v13") == "2026-09-19T07:27:00Z"
    # v12 (charter 150): its first decision, verbatim.  A string bound, so
    # the fraction matters -- rounded to 19:10:09Z it would sort above the
    # first v12 row and drop it.
    first_v12_decision = "2026-09-18T19:10:09.000001Z"
    assert sa.version_effective_from("P1", "p1_joint_likelihood_v12") == first_v12_decision
    assert "2026-09-18T19:10:09Z" > first_v12_decision
    assert sa.version_effective_from("P1", p1) > first_v12_decision
    assert sa.version_effective_from(
        "P1", "p1_metar_trajectory_target_v11"
    ) == sa.version_effective_from("P0", "calibrated_native_cross_ensemble_p0_v6")
    # v10 was charter 134's root bump and keeps its own declaration.
    assert sa.version_effective_from(
        "P1", "p1_metar_trajectory_target_v10"
    ) == sa.version_effective_from(
        "P0", "calibrated_native_cross_ensemble_p0_v5"
    )
    assert sa.version_effective_from("P1", "p1_metar_trajectory_target_v9") is None


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
