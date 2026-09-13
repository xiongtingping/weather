"""A registered point-forecast system must also have a scope contract.

Registration happens in two places that look interchangeable and are not.
``POINT_FORECAST_SYSTEMS`` is the identity roster -- it lets P0 recognise a
system id.  ``POINT_FORECAST_SCOPE_CONTRACTS`` is what lets the OOS reader
SEE the rows, and the transport allowlist in the reader's SQL is derived
from it, so a lane with no contract has its rows discarded before the
per-row check even runs.

Measured 2026-08-30: JMA MSM, NCEP HRRR, NCEP NBM and DWD ICON-EU had all
been captured for days (MSM 37 rows over 8 days, verified against truth by
its own lane and reported healthy every day), were all on the identity
roster, and every one of them had zero artifacts and zero OOS evaluations
because none had a contract.  The capture reports were true and measured
the wrong half of the pipeline.
"""

from __future__ import annotations

import pytest

from bayes_basket_target import native_ensemble_oos as oos
from bayes_basket_target.official_native_forward_packet import (
    POINT_FORECAST_SYSTEMS,
    RETIRED_POINT_FORECAST_SYSTEM_IDS,
)

# The roster keeps a retired system so what was written before still reads;
# a retired system is exactly one whose rows the reader must NOT see.
LIVE_POINT_SYSTEMS = tuple(
    system_id for system_id in POINT_FORECAST_SYSTEMS
    if system_id not in RETIRED_POINT_FORECAST_SYSTEM_IDS
)


def test_every_registered_point_system_has_a_scope_contract() -> None:
    unadmitted = sorted(
        system_id for system_id in LIVE_POINT_SYSTEMS
        if oos._admitted_scope_contract(system_id) is None
    )
    assert not unadmitted, (
        "these systems are on the identity roster but the OOS reader cannot"
        " see their rows -- give each a POINT_FORECAST_SCOPE_CONTRACTS"
        f" entry: {unadmitted}"
    )


def test_every_contract_transport_reaches_the_sql_allowlist() -> None:
    # The allowlist is derived, so this cannot drift -- but the derivation
    # is the thing being relied on, so it is asserted rather than assumed.
    for system_id in LIVE_POINT_SYSTEMS:
        contract = oos._admitted_scope_contract(system_id)
        assert contract is not None
        transport = str(contract["transport_provider_id"])
        assert transport in oos.OFFICIAL_ARCHIVE_TRANSPORT_PROVIDER_IDS
        assert f"'{transport}'" in oos._OFFICIAL_TRANSPORT_SQL_LIST


# What the collectors were observed to write on 2026-08-30.  Stated here so
# a collector that changes its extraction convention breaks this test rather
# than silently emptying its own training set: the reader drops superseded
# extractions by design, so a mismatch costs the accumulated history.
OBSERVED_ROWS = (
    ("JMA_MSM_POINT", "RISH_KYOTO_JMA_ARCHIVE", "h12_24", "00Z",
     "jma_msm_surface_nearest_cell_v1"),
    ("JMA_MSM_POINT", "RISH_KYOTO_JMA_ARCHIVE", "h24_48", "00Z",
     "jma_msm_surface_nearest_cell_v1"),
    ("NCEP_HRRR_POINT", "NOAA_HRRR_PDS_S3", "h12_24", "12Z",
     "ncep_hrrr_surface_nearest_cell_v1"),
    ("NCEP_HRRR_POINT", "NOAA_HRRR_PDS_S3", "h24_48", "00Z",
     "ncep_hrrr_surface_nearest_cell_v1"),
    ("NCEP_NBM_POINT", "NOAA_NBM_PDS_S3", "h12_24", "12Z",
     "ncep_nbm_core_nearest_cell_v1"),
    ("DWD_ICON_EU_POINT", "DWD_OPENDATA_ICON_EU", "h12_24", "00Z",
     "dwd_icon_eu_surface_nearest_cell_v1"),
    ("DWD_ICON_EU_POINT", "DWD_OPENDATA_ICON_EU", "h24_48", "00Z",
     "dwd_icon_eu_surface_nearest_cell_v1"),
    ("DWD_ICON_EU_POINT", "DWD_OPENDATA_ICON_EU", "h24_48", "12Z",
     "dwd_icon_eu_surface_nearest_cell_v1"),
)


@pytest.mark.parametrize(
    "system_id,transport,lead_bucket,run_cycle,extraction", OBSERVED_ROWS
)
def test_the_rows_the_collectors_write_are_admitted(
    system_id: str,
    transport: str,
    lead_bucket: str,
    run_cycle: str,
    extraction: str,
) -> None:
    assert oos._historical_transport_scope_allowed(
        system_id=system_id,
        transport_provider_id=transport,
        lead_bucket=lead_bucket,
        run_cycle=run_cycle,
        extraction_version=extraction,
    )


def test_the_roster_blurb_is_not_the_extraction_version() -> None:
    # The JMA entry in POINT_FORECAST_SYSTEMS says "single_cell" while its
    # rows say "nearest_cell".  A contract written from the blurb passes the
    # transport filter and then rejects every row on the extraction version
    # -- a failure that looks like "no data yet" rather than a mismatch.
    assert not oos._historical_transport_scope_allowed(
        system_id="JMA_MSM_POINT",
        transport_provider_id="RISH_KYOTO_JMA_ARCHIVE",
        lead_bucket="h12_24",
        run_cycle="00Z",
        extraction_version="jma_msm_surface_single_cell_v1",
    )


def test_an_unserved_cycle_stays_out() -> None:
    assert not oos._historical_transport_scope_allowed(
        system_id="JMA_MSM_POINT",
        transport_provider_id="RISH_KYOTO_JMA_ARCHIVE",
        lead_bucket="h12_24",
        run_cycle="06Z",
        extraction_version="jma_msm_surface_nearest_cell_v1",
    )


def test_a_foreign_transport_cannot_borrow_a_contract() -> None:
    assert not oos._historical_transport_scope_allowed(
        system_id="JMA_MSM_POINT",
        transport_provider_id="OPEN_METEO",
        lead_bucket="h12_24",
        run_cycle="00Z",
        extraction_version="jma_msm_surface_nearest_cell_v1",
    )


def test_a_retired_point_system_is_read_as_excluded_and_never_admitted() -> None:
    """Charter 133 retired the TAF TX member lane: no contract, so its
    transport leaves the SQL allowlist, yet an activation or packet written
    before still names an excluded point system rather than an unknown one."""

    from bayes_basket_target.official_native_forward_packet import (
        SUPPORTED_ARCHIVE_SCHEMAS,
        is_fusion_excluded_point_system,
    )

    for system_id in RETIRED_POINT_FORECAST_SYSTEM_IDS:
        assert system_id in POINT_FORECAST_SYSTEMS
        assert oos._admitted_scope_contract(system_id) is None
        assert is_fusion_excluded_point_system(system_id) is True
    assert "AWC_TAF_DATA_API" not in oos.OFFICIAL_ARCHIVE_TRANSPORT_PROVIDER_IDS
    assert "AWC_TAF_DATA_API" not in SUPPORTED_ARCHIVE_SCHEMAS.values()
