"""The second ingress into the product registry currently has no members.

``model_product_identity`` says in its first paragraph that coverage rows
and retained native decode evidence are two ingress sources for the same
receipt.  Only one of them has ever been populated here: all five registered
products -- the ECMWF IFS and AIFS ensembles, DWD ICON EPS, NOAA GEFS and
ECCC GEM -- carry ``ingress_kind=COVERAGE_MATRIX_ROW``, so
``NATIVE_PRODUCT_IDS`` is empty and the registered-product receipt builder,
which serves that ingress alone, refuses everything put to it.

That is a fact worth a test rather than a surprise worth a debugging session,
because the lane built on it reads as broken from the inside.  Its
``NATIVE_PRODUCT_BY_MODEL`` names ECMWF_IFS_0P25, NOAA_GFS_0P25 and
DWD_ICON_GLOBAL: deterministic quarter-degree products, none of which is in
the registry under any name.  The registry asserts at import that every row
is an ensemble (``canonical_active_product_registry_native_ensemble_only``),
so those three cannot be added without that assertion firing.  Both halves
arrived together in the 2026-08-06 bulk import and have never agreed.

Nothing in the resident cycle reaches it: the only caller is the one-off
``scripts/build_pr10_native_daily_max_truth_binding.py``, which has never
written an audit output.  So this is dormant rather than broken, and this
test is what will say so if either half moves.
"""

from __future__ import annotations

import pytest

from bayes_basket_target.contracts import ContractError
from bayes_basket_target.model_product_identity import (
    COVERAGE_PRODUCT_IDS,
    NATIVE_PRODUCT_IDS,
    PRODUCT_BY_ID,
    build_registered_model_product_identity_receipt,
)
from bayes_basket_target.native_daily_max_manifest import (
    NATIVE_PRODUCT_BY_MODEL,
)


def test_every_registered_product_arrives_through_the_coverage_ingress() -> None:
    assert NATIVE_PRODUCT_IDS == frozenset()
    assert COVERAGE_PRODUCT_IDS == frozenset(PRODUCT_BY_ID)
    assert len(COVERAGE_PRODUCT_IDS) == 5


def test_the_registered_product_receipt_refuses_while_that_ingress_is_empty() -> None:
    """Not a bug: the builder gates on the native ingress, and there is
    nothing in it.  A product that IS registered is refused just the same,
    which is what distinguishes this from a stale product id."""

    with pytest.raises(ContractError, match="registered_product_invalid"):
        build_registered_model_product_identity_receipt(
            product_id="DWD_ICON_GLOBAL_EPS", station_id="KATL",
            lead_bucket="d2", run_cycle="18Z",
            identity_source_receipt_sha256="a" * 64,
        )


def test_the_daily_max_lane_names_products_the_registry_does_not_hold() -> None:
    """The other half of the mismatch, pinned so that fixing one half
    without the other cannot pass quietly."""

    named = set(NATIVE_PRODUCT_BY_MODEL.values())
    assert named == {"ECMWF_IFS_0P25", "NOAA_GFS_0P25", "DWD_ICON_GLOBAL"}
    assert not (named & set(PRODUCT_BY_ID)), (
        "none of these is registered; the registry holds ensembles only"
    )
