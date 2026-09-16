"""A vetoed band table stops rescaling: what is left is identity.

2026-09-15T18:26Z the veto lane retired P0's table (undo_gain +0.0077 nat per
station-day over 64 station-days, hurdle 0.005).  The retirement worked for
every artifact that pinned the table's sha -- the loader hands those an empty
table.  The builds made AFTER it did not: with no activated table the OOS
build minted a bare ``band_reliability_v1`` tag, and a bare tag used to mean
the pre-store bootstrap constants.  So 284 P0 decisions kept being rescaled
by hand-written 2026-08 constants, keyed on the CLAIM axis while the lane has
been on ``centre_distance`` since 2026-09-07 -- by the very layer the veto had
just measured as harmful, in a shape no holdout ever scored.

Two ways the hole is shut: serving reads a bare tag as identity, and the
build's own gate scores identity under the same condition (it also stops
minting a bare tag, which the serving test makes harmless either way).
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.band_reliability_store import (  # noqa: E402
    BAND_AXIS_CENTRE_DISTANCE,
    band_table_axis,
    band_table_sha256,
    ensure_band_table_schema,
    persist_and_activate_band_table,
    retire_band_table,
)
from bayes_basket_target.cross_consensus import (  # noqa: E402
    BAND_RELIABILITY_FACTORS,
    BAND_RELIABILITY_METHOD_TAG,
    apply_band_reliability,
)
from bayes_basket_target.native_ensemble_p0 import (  # noqa: E402
    resolve_pinned_band_layer,
)

FACTORS = (
    ((0.0, 1.0), 1.0),
    ((1.0, 2.0), 1.0475153905280654),
    ((2.0, 3.0), 1.0),
    ((3.0, 4.0), 0.9840320314626928),
    ((4.0, 99.0), 0.6943378762404834),
)


def _store_with_table() -> tuple[sqlite3.Connection, str]:
    conn = sqlite3.connect(":memory:")
    ensure_band_table_schema(conn)
    persist_and_activate_band_table(
        conn, stage="P0", factors=FACTORS, evidence={"schema": "t"},
        serving_day_count=41, axis=BAND_AXIS_CENTRE_DISTANCE,
    )
    return conn, band_table_sha256(
        FACTORS, stage="P0", axis=BAND_AXIS_CENTRE_DISTANCE
    )


def test_a_bare_tag_is_identity_not_the_bootstrap_constants() -> None:
    """The 2026-09-15 defect: no table, so no rescaling of any kind."""

    conn, _sha = _store_with_table()
    retire_band_table(conn, stage="P0")
    layer, sha, _axis = resolve_pinned_band_layer(
        conn, f"blend::{BAND_RELIABILITY_METHOD_TAG}"
    )
    assert layer is None and sha is None

    # And what identity means downstream: the distribution is untouched.
    probability = {"18°C": 0.1, "19°C": 0.15, "20°C": 0.25, "21°C": 0.5}
    rescaled, _receipt = apply_band_reliability(
        probability, factors=BAND_RELIABILITY_FACTORS
    )
    assert rescaled != probability, "the bootstrap table does move the vector"


def test_a_pinned_table_is_still_served_through_its_own_axis() -> None:
    conn, sha = _store_with_table()
    layer, pinned, axis = resolve_pinned_band_layer(
        conn, f"blend::{BAND_RELIABILITY_METHOD_TAG}::band_table_sha256={sha}"
    )
    assert layer is not None and pinned == sha and axis == BAND_AXIS_CENTRE_DISTANCE
    assert layer.axis == BAND_AXIS_CENTRE_DISTANCE
    assert tuple(layer.factors) == FACTORS


def test_a_pin_to_a_vetoed_table_serves_identity() -> None:
    """Charter: a veto stops the pinned readers too, via the empty table."""

    conn, sha = _store_with_table()
    retire_band_table(conn, stage="P0")
    layer, pinned, _axis = resolve_pinned_band_layer(
        conn, f"blend::{BAND_RELIABILITY_METHOD_TAG}::band_table_sha256={sha}"
    )
    # the sha is kept so the receipt can name the version retired out from
    # under this artifact, while the factors it serves are empty
    assert layer is not None and tuple(layer.factors) == () and pinned == sha
    probability = {"18°C": 0.2, "19°C": 0.3, "20°C": 0.5}
    rescaled, receipt = apply_band_reliability(probability, factors=())
    assert rescaled == probability
    assert receipt["state"] == "NO_BAND_IN_SUPPORT"


def test_no_tag_at_all_means_no_band_layer() -> None:
    conn, _sha = _store_with_table()
    assert resolve_pinned_band_layer(conn, "blend::something_else")[0] is None


def test_the_holdout_scores_identity_when_the_store_has_no_table() -> None:
    """The build's own gate must score what serving serves.

    Before the fix both sides agreed on the WRONG thing: ``factors=None``
    reached the bootstrap constants in each.  The gate is asked directly
    here, on a store whose table has been vetoed.
    """

    from bayes_basket_target.native_ensemble_oos import p0_gate_tables

    conn, _sha = _store_with_table()
    tables = p0_gate_tables(conn, station_id="KLGA")
    assert tables["band_factors"] is not None
    assert tables["band_layer"] is not None

    retire_band_table(conn, stage="P0")
    vetoed = p0_gate_tables(conn, station_id="KLGA")
    assert vetoed["band_factors"] is None
    assert vetoed["band_table_sha256"] is None
    assert vetoed["band_layer"] is None, (
        "no active table must score identity, not the bootstrap constants"
    )


def test_the_receipt_of_the_incident_is_reproducible() -> None:
    """The shape that was served on 2026-09-15: claim-axis hand constants.

    Kept as a regression witness: if a bare tag ever again resolves to a
    layer, this is the vector it would produce.
    """

    probability = {"18°C": 0.05, "19°C": 0.15, "20°C": 0.25, "21°C": 0.55}
    _rescaled, receipt = apply_band_reliability(
        probability, factors=BAND_RELIABILITY_FACTORS
    )
    assert receipt["axis"] == "claim"
    assert set(json.loads(json.dumps(receipt))["applied_factors"].values()) <= {
        0.86, 1.08, 1.1
    }


def test_a_vetoed_stage_still_knows_which_axis_it_fits_on() -> None:
    """Otherwise the next refit moves the stage back to the claim axis.

    ``refit_all_stages(axis=None)`` asks this for the axis to fit on, and the
    nightly sweep passes no axis.  With P0's table retired on 2026-09-15 this
    answered ``claim`` -- so the next fit would have undone charter 105's
    2026-09-07 move to ``centre_distance`` without anybody deciding it.
    """

    conn, _sha = _store_with_table()
    assert band_table_axis(conn, stage="P0") == BAND_AXIS_CENTRE_DISTANCE
    retire_band_table(conn, stage="P0")
    assert band_table_axis(conn, stage="P0") == BAND_AXIS_CENTRE_DISTANCE

    # A stage that never had a table is still claim: that is what every
    # table was fitted on before the column existed.
    assert band_table_axis(conn, stage="P1") == "claim"
