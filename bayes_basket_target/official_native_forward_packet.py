"""Build a decision snapshot from official native ensemble archives.

The archives in one snapshot must agree on station-day, lead and the target
bucket contract, but they do not have to share a model run cycle.  A model
family's cycle is part of its own lineage and is never collapsed into a
synthetic common cycle.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from .contracts import ContractError, content_sha256
from .native_archive_lead import capture_lead_bucket
from .metar_physical_tmax import (
    EXPECTED_MEMBER_COUNT as METAR_PHYSICAL_EXPECTED_MEMBER_COUNT,
    INDEPENDENCE_FAMILY_ID as METAR_PHYSICAL_FAMILY_ID,
    PROVIDER_ID as METAR_PHYSICAL_PROVIDER_ID,
    SYSTEM_ID as METAR_PHYSICAL_SYSTEM_ID,
)


SCHEMA = "official_native_ensemble_forward_packet_v1"
NOAA_GEFS_MULTISTATION_SCHEMA = (
    "noaa_gefs_official_historical_multistation_members_v2"
)
ECMWF_MULTISTATION_SCHEMA = (
    "ecmwf_ifs_open_pf50_historical_multistation_members_v1"
)
ECCC_GEPS_MULTISTATION_SCHEMA = (
    "eccc_geps_official_historical_multistation_members_v1"
)
DWD_ICON_EPS_MULTISTATION_SCHEMA = (
    "dwd_icon_eps_official_historical_multistation_members_v1"
)
# Charter 204: AIFS ENS reaches the packet only as a per-station follow
# archive (its own newest certified run), never as a rung archive.  The
# schema string is spelled here rather than imported so this module keeps
# its import graph (``ecmwf_aifs_ens_archive`` asserts the two agree).
ECMWF_AIFS_ENS_MULTISTATION_SCHEMA = (
    "ecmwf_aifs_ens_official_historical_multistation_members_v1"
)
# Charter 212: NOAA AIGEFS, a follow-only family like AIFS ENS.
NOAA_AIGEFS_MULTISTATION_SCHEMA = (
    "noaa_aigefs_ops_multistation_members_v1"
)
# Charter 95 (2026-09-06): the regional deterministic models' forward runs
# reach the packet through a single-member point-archive shape, one
# schema per system so the transport is read off the archive as for every
# other family.
JMA_MSM_POINT_SCHEMA = "official_jma_msm_point_members_v1"
NCEP_HRRR_POINT_SCHEMA = "official_ncep_hrrr_point_members_v1"
NCEP_NBM_POINT_SCHEMA = "official_ncep_nbm_point_members_v1"
DWD_ICON_EU_POINT_SCHEMA = "official_dwd_icon_eu_point_members_v1"
CMA_GRAPES_GLOBAL_POINT_SCHEMA = "official_cma_grapes_global_point_members_v1"
SUPPORTED_ARCHIVE_SCHEMAS = {
    NOAA_GEFS_MULTISTATION_SCHEMA: "NOAA_GEFS_PDS_S3",
    ECMWF_MULTISTATION_SCHEMA: "ECMWF_OPEN_DATA_S3",
    ECCC_GEPS_MULTISTATION_SCHEMA: "ECCC_DD_WEATHER",
    DWD_ICON_EPS_MULTISTATION_SCHEMA: "DWD_OPENDATA_ICON_EPS",
    ECMWF_AIFS_ENS_MULTISTATION_SCHEMA: "ECMWF_OPEN_DATA_S3",
    NOAA_AIGEFS_MULTISTATION_SCHEMA: "NCEP_NOMADS",
    JMA_MSM_POINT_SCHEMA: "RISH_KYOTO_JMA_ARCHIVE",
    NCEP_HRRR_POINT_SCHEMA: "NOAA_HRRR_PDS_S3",
    NCEP_NBM_POINT_SCHEMA: "NOAA_NBM_PDS_S3",
    DWD_ICON_EU_POINT_SCHEMA: "DWD_OPENDATA_ICON_EU",
}
# Point systems whose ``run_cycle`` is a scope label over an issuance instant
# rather than a run's init hour, so the builder waives the hour-equality check
# for them and keeps only the ordering against the query time.
POINT_FORECAST_SYSTEM_IDS = frozenset({
    METAR_PHYSICAL_SYSTEM_ID,
})
# The forecaster's TAF TX as a P0 point member, retired 2026-09-13 (user
# ruling, charter 133: "TAF 作 P0 点成员那条车道下线").  Charters 42/95 had
# kept it out of every P0 seat since 2026-08-15 while the lane went on
# projecting member rows, minting per-system OOS artifacts and carrying TAF
# archives into forward packets; nothing produces it any more.  The id stays
# on the identity roster below only so what was written before -- packets
# still inside their TTL, activations and selection rows that name it, the
# 5,582 member rows -- keeps reading as an excluded point system instead of
# as an unknown one.
RETIRED_POINT_FORECAST_SYSTEM_IDS = frozenset({"OFFICIAL_AIRPORT_TAF_TX"})
# The one regional model this fleet has evidence for.  RCSS sits on the floor
# of the Taipei basin at five metres inside a ring of 500-1000m hills, and a
# 25 km global cell averages those hills into the airfield: the bias layer has
# been applying +5.75C there all month and still leaves +1.4C standing, which
# is not a fit that needs tuning but a resolution the source does not have.
# MSM's 5 km cell for the same point sits at four metres.  Measured day-ahead
# over sixty settled days it beat the served posterior on CRPS, NLL and Brier
# and kept beating it with the three best days removed -- the only one of
# twenty-six candidate station-model pairs that did.
#
# Registered as a point system because a deterministic run is one number, the
# same way a TAF TX is: there is no member spread to characterise before its
# centre can be trusted, which is what the twenty-day profile exists to buy.
# Its width comes from its own residual record, as the single-member path
# already handles.
#
# Deliberately NOT in POINT_FORECAST_SYSTEM_IDS.  That set waives the check
# that a run's init hour equals its cycle, and it exists for point systems
# whose cycle is only a scope label over a jittering issuance instant.  MSM
# runs on a strict three-hourly grid and its init hour *is* its cycle, so it
# keeps the check: a waiver granted to something that does not need one is a
# check silently lost.
JMA_MSM_POINT_SYSTEM_ID = "JMA_MSM_POINT"
JMA_MSM_FAMILY_ID = "JMA_MSM"

# The CONUS pair, ruled in 2026-08-28 from the 46-pair sweep: HRRR and the
# NBM cleared the three admission gates at more roster stations than every
# other regional source combined.  Both are single numbers per run --
# HRRR a 3km deterministic, the NBM a calibrated blend -- so both take the
# point-forecast path MSM defined: capture builds the corpus, the OOS gates
# race each station-pair, and what a win buys is envelope evidence, never a
# membership seat.  Neither goes in POINT_FORECAST_SYSTEM_IDS: both run on
# strict hourly grids where init hour IS the cycle, so they keep the check
# for MSM's reason.  The NBM blends other centres' output, including
# families already seated; its family id names the blend, and any seat race
# that puts it beside what it blends must ask the overlap question openly.
NCEP_HRRR_POINT_SYSTEM_ID = "NCEP_HRRR_POINT"
NCEP_HRRR_FAMILY_ID = "NCEP_HRRR"
NCEP_NBM_POINT_SYSTEM_ID = "NCEP_NBM_POINT"
NCEP_NBM_FAMILY_ID = "NCEP_NBM"
# ICON-EU deterministic, same sweep and day.  Unlike MSM and the CONUS
# pair it does NOT get its own family: it is the same ICON physics from
# the same centre that already seats three EPS systems, and a second
# family for one model's physics would let it spend two families' caps.
DWD_ICON_EU_POINT_SYSTEM_ID = "DWD_ICON_EU_POINT"
CMA_GRAPES_GLOBAL_POINT_SYSTEM_ID = "CMA_GRAPES_GLOBAL_POINT"
CMA_GRAPES_FAMILY_ID = "CMA_GRAPES"

POINT_FORECAST_SYSTEMS = {
    JMA_MSM_POINT_SYSTEM_ID: {
        "ensemble_system_id": JMA_MSM_POINT_SYSTEM_ID,
        "canonical_name": "JMA MSM 5km Regional Deterministic Point",
        # Its own family, like every other centre's.  The weight machinery
        # aggregates within a family before capping across families, so a
        # regional model filed under a global family would spend that
        # family's cap while sharing neither its grid nor its physics.
        "independence_family_id": JMA_MSM_FAMILY_ID,
        "provider_id": "JMA_RJTD",
        "expected_member_count": 1,
        "content_sha256": content_sha256({
            "ensemble_system_id": JMA_MSM_POINT_SYSTEM_ID,
            "member_identity_contract_version": (
                "jma_msm_surface_single_cell_v1"
            ),
            "publication_contract_version": (
                "three_hourly_run_grid_nearest_cell_v1"
            ),
        }),
    },
    NCEP_HRRR_POINT_SYSTEM_ID: {
        "ensemble_system_id": NCEP_HRRR_POINT_SYSTEM_ID,
        "canonical_name": "NCEP HRRR 3km CONUS Deterministic Point",
        "independence_family_id": NCEP_HRRR_FAMILY_ID,
        "provider_id": "NCEP_NOAA",
        "expected_member_count": 1,
        "content_sha256": content_sha256({
            "ensemble_system_id": NCEP_HRRR_POINT_SYSTEM_ID,
            "member_identity_contract_version": (
                "ncep_hrrr_surface_nearest_cell_v1"
            ),
            "publication_contract_version": (
                "hourly_run_grid_idx_byte_range_v1"
            ),
        }),
    },
    NCEP_NBM_POINT_SYSTEM_ID: {
        "ensemble_system_id": NCEP_NBM_POINT_SYSTEM_ID,
        "canonical_name": "NCEP National Blend of Models Core Point",
        "independence_family_id": NCEP_NBM_FAMILY_ID,
        "provider_id": "NCEP_NOAA",
        "expected_member_count": 1,
        "content_sha256": content_sha256({
            "ensemble_system_id": NCEP_NBM_POINT_SYSTEM_ID,
            "member_identity_contract_version": (
                "ncep_nbm_core_nearest_cell_v1"
            ),
            "publication_contract_version": (
                "hourly_run_grid_idx_byte_range_v1"
            ),
        }),
    },
    DWD_ICON_EU_POINT_SYSTEM_ID: {
        "ensemble_system_id": DWD_ICON_EU_POINT_SYSTEM_ID,
        "canonical_name": "DWD ICON-EU 7km Deterministic Point",
        "independence_family_id": "DWD_ICON_EPS",
        "provider_id": "DWD",
        "expected_member_count": 1,
        "content_sha256": content_sha256({
            "ensemble_system_id": DWD_ICON_EU_POINT_SYSTEM_ID,
            "member_identity_contract_version": (
                "dwd_icon_eu_surface_nearest_cell_v1"
            ),
            "publication_contract_version": (
                "six_hourly_run_grid_whole_domain_no_history_v1"
            ),
        }),
    },
    CMA_GRAPES_GLOBAL_POINT_SYSTEM_ID: {
        "ensemble_system_id": CMA_GRAPES_GLOBAL_POINT_SYSTEM_ID,
        "canonical_name": "CMA GRAPES Global 12km Deterministic Point",
        # Its own family: no seated system carries CMA physics.  CMA has
        # been a declared P0 candidate since the pool was written and has
        # never served an envelope, so nothing here shares a cap with it.
        "independence_family_id": CMA_GRAPES_FAMILY_ID,
        "provider_id": "CMA",
        "expected_member_count": 1,
        "content_sha256": content_sha256({
            "ensemble_system_id": CMA_GRAPES_GLOBAL_POINT_SYSTEM_ID,
            "member_identity_contract_version": (
                "cma_grapes_global_open_meteo_derived_cycle_v1"
            ),
            "publication_contract_version": (
                "six_hourly_cycle_aggregator_no_run_stamp_v1"
            ),
        }),
    },
    # Retired (see ``RETIRED_POINT_FORECAST_SYSTEM_IDS``): kept for reading
    # what was written before, never produced.
    "OFFICIAL_AIRPORT_TAF_TX": {
        "ensemble_system_id": "OFFICIAL_AIRPORT_TAF_TX",
        "canonical_name": "Official Airport TAF TX Point",
        "independence_family_id": "HUMAN_FORECASTER",
        "provider_id": "ICAO_MET_OFFICE",
        "expected_member_count": 1,
        "content_sha256": content_sha256({
            "ensemble_system_id": "OFFICIAL_AIRPORT_TAF_TX",
            "member_identity_contract_version": "taf_tx_single_point_v1",
            "publication_contract_version": (
                "issue_time_known_amendments_supersede_v1"
            ),
        }),
    },
    # The day's own observations, forecasting the day's maximum without
    # consulting a model.  It sits in its own independence family for the
    # reason the family exists: the weight machinery aggregates within a
    # family before capping across families, so filing this under any NWP
    # family would let a source that shares none of their physics dilute the
    # cap that keeps them honest.  It is not the human forecaster either --
    # the TAF and this system disagree in useful ways, and collapsing them
    # would hide exactly the disagreement worth seeing.
    METAR_PHYSICAL_SYSTEM_ID: {
        "ensemble_system_id": METAR_PHYSICAL_SYSTEM_ID,
        "canonical_name": "METAR Physical Path Daily Maximum",
        "independence_family_id": METAR_PHYSICAL_FAMILY_ID,
        "provider_id": METAR_PHYSICAL_PROVIDER_ID,
        # One, like the TAF.  METAR is a single observation stream producing a
        # single forecast; it has no exchangeable draws to contribute, and its
        # uncertainty is measured from its record rather than declared here.
        "expected_member_count": METAR_PHYSICAL_EXPECTED_MEMBER_COUNT,
        "content_sha256": content_sha256({
            "ensemble_system_id": METAR_PHYSICAL_SYSTEM_ID,
            "member_identity_contract_version": (
                "metar_physical_tmax_single_point_v1"
            ),
            "publication_contract_version": (
                "recomputed_on_each_canonical_observation_v1"
            ),
        }),
    },
}
# Only a fallback, for archives written before collectors stated their lead.
# Every current archive carries ``archive_lead_bucket`` derived from the run it
# was actually drawn from, and that always wins.
ARCHIVE_LEAD_BY_SCHEMA = {
    schema: "d2" for schema in SUPPORTED_ARCHIVE_SCHEMAS
}
# Charter 95 (user ruling, 2026-09-06: "接入"): the regional deterministic
# models -- JMA MSM, NCEP HRRR, NCEP NBM, DWD ICON-EU -- are numerical models
# and may contest a P0 seat once they win the OOS gate, served as
# single-member systems whose bandwidth is their fitted residual sigma.  The
# 2026-08-15 exclusion of point forecasts from the P0 combination was written
# for the forecaster's TAF TX and the METAR physical path, which are not
# model runs; those two stay out.  ``is_fusion_excluded_point_system`` is the
# one predicate every fusion site reads.
REGIONAL_DETERMINISTIC_SYSTEM_IDS = frozenset({
    JMA_MSM_POINT_SYSTEM_ID,
    CMA_GRAPES_GLOBAL_POINT_SYSTEM_ID,
    NCEP_HRRR_POINT_SYSTEM_ID,
    NCEP_NBM_POINT_SYSTEM_ID,
    DWD_ICON_EU_POINT_SYSTEM_ID,
})
REGIONAL_POINT_ARCHIVE_SCHEMA_BY_SYSTEM = {
    JMA_MSM_POINT_SYSTEM_ID: JMA_MSM_POINT_SCHEMA,
    CMA_GRAPES_GLOBAL_POINT_SYSTEM_ID: CMA_GRAPES_GLOBAL_POINT_SCHEMA,
    NCEP_HRRR_POINT_SYSTEM_ID: NCEP_HRRR_POINT_SCHEMA,
    NCEP_NBM_POINT_SYSTEM_ID: NCEP_NBM_POINT_SCHEMA,
    DWD_ICON_EU_POINT_SYSTEM_ID: DWD_ICON_EU_POINT_SCHEMA,
}
REGIONAL_POINT_CONTRACT_VERSIONS = {
    CMA_GRAPES_GLOBAL_POINT_SYSTEM_ID: (
        "cma_grapes_global_open_meteo_derived_cycle_v1",
        # The publication contract names what this transport cannot do:
        # the aggregator serves the newest run without stamping which run
        # it is, so the cycle is derived and only knowable_at_utc is read.
        "six_hourly_cycle_aggregator_no_run_stamp_v1",
    ),
    JMA_MSM_POINT_SYSTEM_ID: (
        "jma_msm_surface_single_cell_v1",
        "three_hourly_run_grid_nearest_cell_v1",
    ),
    NCEP_HRRR_POINT_SYSTEM_ID: (
        "ncep_hrrr_surface_nearest_cell_v1",
        "hourly_run_grid_idx_byte_range_v1",
    ),
    NCEP_NBM_POINT_SYSTEM_ID: (
        "ncep_nbm_core_nearest_cell_v1",
        "hourly_run_grid_idx_byte_range_v1",
    ),
    DWD_ICON_EU_POINT_SYSTEM_ID: (
        "dwd_icon_eu_surface_nearest_cell_v1",
        "six_hourly_run_grid_whole_domain_no_history_v1",
    ),
}


# Charter 95 as re-ruled the same day ("把区域模型移动到 P1，不是 P0…不要和 P0
# 的全球模型混淆"): the regional deterministic models are P1-stage evidence,
# fused with the observed temperature path, never P0 fusion members.  The
# stage is named here so the P0 side stays a global-model combination and the
# P1 lane reads its own table.
REGIONAL_MODELS_STAGE = "P1"


def is_fusion_excluded_point_system(system_id: str) -> bool:
    """A point system that may never be a P0 fusion member.

    True for every point system: the retired TAF TX, the METAR physical
    path, and -- by the 2026-09-06 re-ruling -- the regional deterministic
    models, which belong to the P1 stage.  False for every ensemble.
    """

    key = str(system_id or "").strip()
    if key in REGIONAL_DETERMINISTIC_SYSTEM_IDS:
        return REGIONAL_MODELS_STAGE != "P0"
    return key in POINT_FORECAST_SYSTEMS
# P0 is recomputed as the models update, so the same station-day is captured at
# d2, again at d1, and again at d0.  This builder used to accept d2 alone: the
# collectors were parameterised, the scheduler emitted all three leads, the
# worker passed the lead through and the archives came back correctly labelled
# -- and then every shorter-lead capture died here with
# ``official_native_forward_scope_mismatch`` after doing all the downloading.
# Nothing but d2 had ever reached a packet.
# Hours ahead of the station's local day.  The calendar ``d`` labels remain
# admissible only so archives captured before the switch stay readable; nothing
# writes them any more, and the builder re-derives the lead from the run
# whenever the archive states one, so a stated ``d`` label survives only for
# archives with no readable run.  ``d3`` is the ECMWF archive-level label of
# the charter 193 amendment's D-3 18Z run (its captures state hour buckets like
# every other), which a D+2 pass may hold before D-2 00Z lands.
SUPPORTED_ARCHIVE_LEAD_BUCKETS = frozenset({
    "h00_12", "h12_24", "h24_48", "h48_plus", "d0", "d1", "d2", "d3",
})
# Freshest first, for choosing the packet-level label out of a mixed-cycle
# set.  Calendar labels sort after the hour buckets: they survive only on
# archives whose run cannot be read, and a stated claim without a verifiable
# run should never outrank a derived one.
_LEAD_BUCKET_FRESHNESS_ORDER = {
    "h00_12": 0,
    "h12_24": 1,
    "h24_48": 2,
    "h48_plus": 3,
    "d0": 10,
    "d1": 11,
    "d2": 12,
    "d3": 13,
}
VALID_RUN_CYCLES = frozenset({"00Z", "06Z", "12Z", "18Z"})
MIXED_NATIVE_CYCLES = "MIXED_NATIVE_CYCLES"


def _verified_utc_timestamp(value: Any) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise ContractError(
            "official_native_forward_lineage_timestamp_missing"
        )
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ContractError(
            "official_native_forward_lineage_timestamp_invalid"
        ) from exc
    if parsed.tzinfo is None:
        raise ContractError(
            "official_native_forward_lineage_timestamp_invalid"
        )
    return parsed.astimezone(timezone.utc)


def _verified_archive(raw: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ContractError("official_native_forward_archive_invalid")
    archive = dict(raw)
    identity = {
        key: value
        for key, value in archive.items()
        if key != "content_sha256"
    }
    if (
        archive.get("schema") not in SUPPORTED_ARCHIVE_SCHEMAS
        or archive.get("content_sha256") != content_sha256(identity)
        or archive.get("training_only") is not True
        or archive.get("holdout_eligible") is not False
        or not isinstance(archive.get("captures"), list)
    ):
        raise ContractError(
            "official_native_forward_archive_invalid"
        )
    return archive


def _verified_target_bucket_contract(
    raw: Mapping[str, Any],
    *,
    station_id: str,
    event_date: str,
) -> dict[str, Any]:
    contract = dict(raw)
    intervals = contract.get("bucket_intervals")
    authority_source_class = str(
        contract.get("authority_source_class")
        or "AUTHORITATIVE_MARKET_REGISTRY_STATION_DAY"
    )
    if (
        contract.get("schema")
        != "official_native_forward_target_bucket_contract_v1"
        or str(contract.get("station_id") or "").strip().upper()
        != station_id
        or str(contract.get("event_date") or "") != event_date
        or not str(contract.get("settlement_timezone") or "")
        or not isinstance(intervals, Mapping)
        or not intervals
        or len(str(contract.get("authority_content_sha256") or ""))
        != 64
        or authority_source_class not in {
            "AUTHORITATIVE_MARKET_REGISTRY_STATION_DAY",
            "SOURCE_ARCHIVE_OFFLINE_CONTRACT_BRIDGE",
        }
    ):
        raise ContractError(
            "official_native_forward_target_contract_invalid"
        )
    frozen: dict[str, list[float | None]] = {}
    ordered: list[tuple[str, float | None, float | None]] = []
    for bucket, raw_bounds in intervals.items():
        if (
            not str(bucket)
            or not isinstance(raw_bounds, Sequence)
            or isinstance(raw_bounds, (str, bytes, bytearray))
            or len(raw_bounds) != 2
        ):
            raise ContractError(
                "official_native_forward_target_intervals_invalid"
            )
        lower = (
            None
            if raw_bounds[0] is None
            else float(raw_bounds[0])
        )
        upper = (
            None
            if raw_bounds[1] is None
            else float(raw_bounds[1])
        )
        if lower is not None and upper is not None and lower >= upper:
            raise ContractError(
                "official_native_forward_target_intervals_invalid"
            )
        frozen[str(bucket)] = [lower, upper]
        ordered.append((str(bucket), lower, upper))
    ordered.sort(key=lambda row: (
        float("-inf") if row[1] is None else row[1],
        float("inf") if row[2] is None else row[2],
    ))
    if (
        ordered[0][1] is not None
        or ordered[-1][2] is not None
        or any(
            left[2] is None
            or right[1] is None
            or left[2] != right[1]
            for left, right in zip(ordered, ordered[1:])
        )
        or content_sha256(frozen)
        != str(contract.get("bucket_contract_sha256") or "")
    ):
        raise ContractError(
            "official_native_forward_target_intervals_invalid"
        )
    return {
        **contract,
        "station_id": station_id,
        "bucket_intervals": frozen,
        "authority_source_class": authority_source_class,
    }


def _station_capture_lead(
    archive: Mapping[str, Any], station_id: str
) -> str:
    """The lead this archive states for one station, or empty if it has none.

    Empty rather than raising: the caller falls back to the archive-level value
    and then to the schema default, which is what keeps archives captured
    before the per-station vocabulary readable.
    """

    captures = archive.get("captures")
    if not isinstance(captures, Sequence):
        return ""
    for capture in captures:
        if not isinstance(capture, Mapping):
            continue
        if str(capture.get("station_id") or "").strip().upper() == station_id:
            return str(capture.get("archive_lead_bucket") or "")
    return ""


def build_official_native_forward_packet(
    *,
    archives: Sequence[Mapping[str, Any]],
    station_id: str,
    target_bucket_contract: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Merge official member captures for one station-day."""

    if (
        not isinstance(archives, Sequence)
        or isinstance(archives, (str, bytes, bytearray))
        or len(archives) < 2
        or len(archives) > 8
    ):
        raise ContractError(
            "official_native_forward_archive_set_invalid"
        )
    station = str(station_id or "").strip().upper()
    if not station:
        raise ContractError(
            "official_native_forward_station_invalid"
        )
    verified = [_verified_archive(row) for row in archives]
    event_dates = {str(row.get("event_date") or "") for row in verified}
    # The lead is derived from each archive's run against this station's own
    # local day, not read from the stamped label.  The stamp's vocabulary
    # changed underneath this check -- new captures state per-station hour
    # buckets while archives from before the switch state calendar labels --
    # and a packet legitimately mixes both vintages for hours after a deploy,
    # which made a label-equality check fail on spelling.  ``run_init_utc`` is
    # the underlying fact and every vintage carries it.
    #
    # Derived per system, the leads are no longer required to agree: ECMWF's
    # 06Z run and ECCC's 00Z run stand at genuinely different hours from the
    # same station-day, and requiring one shared value was an artifact of the
    # calendar era, when all four collectors stamped whatever lead the caller
    # asked for.  What still must hold: every archive names a run this station
    # can be measured against.  Archives without a readable run keep their
    # stated label, so pre-switch fixtures and replays stay buildable.
    lead_by_system: dict[str, str] = {}
    for row in verified:
        derived = ""
        run_init_raw = str(row.get("run_init_utc") or "").strip()
        capture_row = next(
            (
                item
                for item in row.get("captures") or []
                if isinstance(item, Mapping)
                and str(item.get("station_id") or "").strip().upper()
                == station
            ),
            None,
        )
        if run_init_raw and capture_row is not None:
            timezone_name = str(
                capture_row.get("settlement_timezone") or ""
            ).strip()
            try:
                derived = capture_lead_bucket(
                    event_date=str(row.get("event_date") or ""),
                    settlement_timezone=timezone_name,
                    run_init=datetime.fromisoformat(
                        run_init_raw.replace("Z", "+00:00")
                    ),
                )
            except (ContractError, ValueError, TypeError):
                derived = ""
        lead_by_system[str(row.get("ensemble_system_id") or "")] = str(
            derived
            or _station_capture_lead(row, station)
            or row.get("archive_lead_bucket")
            or ARCHIVE_LEAD_BY_SCHEMA.get(str(row.get("schema")), "")
            or ""
        )
    lead_buckets = set(lead_by_system.values())
    system_ids = {
        str(row.get("ensemble_system_id") or "") for row in verified
    }
    family_ids = {
        str(row.get("independence_family_id") or "")
        for row in verified
    }
    archive_metadata_by_system: dict[str, dict[str, Any]] = {}
    for archive in verified:
        system_id = str(archive.get("ensemble_system_id") or "")
        run_cycle = str(archive.get("run_cycle") or "")
        archive_lead = str(
            archive.get("archive_lead_bucket")
            or (
                archive["captures"][0].get("archive_lead_bucket")
                if archive["captures"]
                and isinstance(archive["captures"][0], Mapping)
                else ""
            )
            or ARCHIVE_LEAD_BY_SCHEMA.get(str(archive.get("schema")), "")
            or ""
        )
        run_init_utc = str(archive.get("run_init_utc") or "").strip()
        # The station's own capture says when its members were read.  A
        # follow file read again for stations listed after its first read
        # holds captures from both reads, and the file-level instant is the
        # newest; each station's arrival is its own capture's, never earlier
        # nor later than the read that produced it.  Captures without the
        # field (pre-vocabulary fixtures) keep the file-level value.
        station_query = next(
            (
                str(item.get("archive_query_at_utc") or "").strip()
                for item in archive.get("captures") or []
                if isinstance(item, Mapping)
                and str(item.get("station_id") or "").strip().upper()
                == station
            ),
            "",
        )
        archive_query_at_utc = station_query or str(
            archive.get("archive_query_at_utc") or ""
        ).strip()
        if (
            not system_id
            or system_id in archive_metadata_by_system
            or run_cycle not in VALID_RUN_CYCLES
            or archive_lead not in SUPPORTED_ARCHIVE_LEAD_BUCKETS
            or not run_init_utc
            or not archive_query_at_utc
        ):
            raise ContractError(
                "official_native_forward_scope_mismatch"
            )
        run_init = _verified_utc_timestamp(run_init_utc)
        archive_query = _verified_utc_timestamp(archive_query_at_utc)
        if (
            (
                system_id not in POINT_FORECAST_SYSTEM_IDS
                and run_init.hour != int(run_cycle[:2])
            )
            or archive_query < run_init
        ):
            # A model run's init hour is its cycle by definition.  A point
            # forecast's cycle is a scope label over a jittering issuance
            # instant, so only the temporal ordering is checked for those.
            raise ContractError(
                "official_native_forward_run_cycle_lineage_mismatch"
            )
        archive_metadata_by_system[system_id] = {
            "run_cycle": run_cycle,
            "archive_lead_bucket": archive_lead,
            "run_init_utc": run_init_utc,
            "archive_query_at_utc": archive_query_at_utc,
            "run_init": run_init,
            "archive_query": archive_query,
        }
    run_cycles = {
        str(metadata["run_cycle"])
        for metadata in archive_metadata_by_system.values()
    }
    if (
        len(event_dates) != 1
        or "" in event_dates
        # Every system must state a supported lead for this station; the leads
        # are not required to agree with each other.  Under the hour
        # vocabulary a mixed-cycle packet legitimately carries different
        # per-system leads -- ECMWF's 06Z run and ECCC's 00Z run stand at
        # different hours from the same station-day -- and the old "one lead
        # per packet" rule was the calendar era's spelling of "one event date
        # per packet", which the date check above still enforces.  The compile
        # side derives its own training key from the per-system runs and
        # treats a genuinely split packet as unkeyed rather than guessing.
        or not lead_buckets
        or "" in lead_buckets
        or not lead_buckets.issubset(SUPPORTED_ARCHIVE_LEAD_BUCKETS)
        or not run_cycles.issubset(VALID_RUN_CYCLES)
        or len(system_ids) != len(verified)
        or "" in system_ids
        or len(family_ids) < 2
        or "" in family_ids
        or set(archive_metadata_by_system) != system_ids
    ):
        raise ContractError(
            "official_native_forward_scope_mismatch"
        )
    event_date = next(iter(event_dates))
    target_contract = (
        None
        if target_bucket_contract is None
        else _verified_target_bucket_contract(
            target_bucket_contract,
            station_id=station,
            event_date=event_date,
        )
    )
    captures: list[dict[str, Any]] = []
    source_rows: list[dict[str, Any]] = []
    for archive in verified:
        matches = [
            dict(row)
            for row in archive["captures"]
            if isinstance(row, Mapping)
            and str(row.get("station_id") or "").strip().upper()
            == station
        ]
        if len(matches) != 1:
            raise ContractError(
                "official_native_forward_station_capture_missing"
            )
        capture = matches[0]
        system_id = str(archive["ensemble_system_id"])
        archive_metadata = archive_metadata_by_system[system_id]
        archive_cycle = str(archive_metadata["run_cycle"])
        capture_lead = str(capture.get("archive_lead_bucket") or "")
        capture_identity = {
            key: value
            for key, value in capture.items()
            if key != "content_sha256"
        }
        if (
            capture.get("content_sha256")
            != content_sha256(capture_identity)
            or str(capture.get("ensemble_system_id") or "")
            != str(archive["ensemble_system_id"])
            or str(capture.get("independence_family_id") or "")
            != str(archive["independence_family_id"])
            or str(capture.get("event_date") or "")
            != event_date
            or str(capture.get("run_cycle") or "")
            != archive_cycle
            # The capture's lead is a per-station fact -- one run stands a
            # different number of hours from each station's local day -- so it
            # is checked for admissibility, not for equality with an
            # archive-level value.  Requiring equality was the calendar era's
            # "one label per archive" restated per capture, and it rejected
            # every multi-timezone hour-bucket archive at the first station
            # whose bucket differed from the first capture's.
            or (
                capture_lead
                and capture_lead not in SUPPORTED_ARCHIVE_LEAD_BUCKETS
            )
            or not isinstance(capture.get("members"), list)
            or len(capture["members"])
            != int(
                archive.get("member_count")
                or capture.get("member_count")
                or 0
            )
        ):
            raise ContractError(
                "official_native_forward_station_capture_invalid"
            )
        captures.append(capture)
        source_rows.append({
            "ensemble_system_id": str(
                archive["ensemble_system_id"]
            ),
            "independence_family_id": str(
                archive["independence_family_id"]
            ),
            "transport_provider_id": SUPPORTED_ARCHIVE_SCHEMAS[
                str(archive["schema"])
            ],
            "run_cycle": archive_cycle,
            "run_init_utc": str(archive_metadata["run_init_utc"]),
            "archive_query_at_utc": str(
                archive_metadata["archive_query_at_utc"]
            ),
            "archive_content_sha256": str(
                archive["content_sha256"]
            ),
            "station_capture_content_sha256": str(
                capture["content_sha256"]
            ),
            "source_bucket_contract_sha256": str(
                capture["bucket_contract_sha256"]
            ),
            **(
                {
                    "family_temperature_curve_sha256": str(
                        capture["family_temperature_curve"][
                            "content_sha256"
                        ]
                    )
                }
                if isinstance(
                    capture.get("family_temperature_curve"), Mapping
                )
                else {}
            ),
        })
    timezones = {
        str(row.get("settlement_timezone") or "") for row in captures
    }
    if len(timezones) != 1 or "" in timezones:
        raise ContractError(
            "official_native_forward_station_contract_mismatch"
        )
    if target_contract is None:
        interval_hashes = {
            str(row.get("bucket_contract_sha256") or "")
            for row in captures
        }
        if (
            len(interval_hashes) != 1
            or "" in interval_hashes
            or any(
                row.get("bucket_intervals")
                != captures[0].get("bucket_intervals")
                for row in captures[1:]
            )
        ):
            raise ContractError(
                "official_native_forward_station_contract_mismatch"
            )
        bucket_intervals = dict(captures[0]["bucket_intervals"])
        bucket_contract_sha = next(iter(interval_hashes))
        bucket_contract_authority = {
            "source_class": "SOURCE_ARCHIVES_EXACT_CONTRACT",
            "authority_content_sha256": bucket_contract_sha,
        }
    else:
        if (
            str(target_contract["settlement_timezone"])
            != next(iter(timezones))
        ):
            raise ContractError(
                "official_native_forward_station_contract_mismatch"
            )
        bucket_intervals = dict(target_contract["bucket_intervals"])
        bucket_contract_sha = str(
            target_contract["bucket_contract_sha256"]
        )
        bucket_contract_authority = {
            "source_class": str(
                target_contract["authority_source_class"]
            ),
            "authority_content_sha256": str(
                target_contract["authority_content_sha256"]
            ),
        }
    received_at = max(
        _verified_utc_timestamp(row["archive_query_at_utc"])
        for row in source_rows
    )
    source_rows.sort(key=lambda row: row["ensemble_system_id"])
    source_by_system = {
        str(row["ensemble_system_id"]): row for row in source_rows
    }
    run_cycle_by_system = {
        system_id: str(row["run_cycle"])
        for system_id, row in source_by_system.items()
    }
    run_init_utc_by_system = {
        system_id: str(row["run_init_utc"])
        for system_id, row in source_by_system.items()
    }
    archive_query_at_utc_by_system = {
        system_id: str(row["archive_query_at_utc"])
        for system_id, row in source_by_system.items()
    }
    availability_identity = {
        "source_class": "OFFICIAL_NATIVE_ARCHIVE_QUERY_RECEIPT",
        "available_at_definition": "MAX_ARCHIVE_QUERY_AT_UTC",
        "available_at_utc": received_at.isoformat().replace(
            "+00:00", "Z"
        ),
        "by_system": {
            system_id: {
                "run_cycle": str(row["run_cycle"]),
                "run_init_utc": str(row["run_init_utc"]),
                "archive_query_at_utc": str(
                    row["archive_query_at_utc"]
                ),
                "archive_content_sha256": str(
                    row["archive_content_sha256"]
                ),
                "station_capture_content_sha256": str(
                    row["station_capture_content_sha256"]
                ),
            }
            for system_id, row in source_by_system.items()
        },
    }
    availability_evidence = {
        **availability_identity,
        "content_sha256": content_sha256(availability_identity),
    }
    run_inits = {
        str(row["run_init_utc"]) for row in source_rows
    }
    same_native_cycle = len(run_cycles) == 1 and len(run_inits) == 1
    packet_run_cycle = (
        next(iter(run_cycles)) if same_native_cycle else MIXED_NATIVE_CYCLES
    )
    run_cycle_scope = (
        "SAME_NATIVE_CYCLE"
        if same_native_cycle
        else "PER_SYSTEM_NATIVE_CYCLE"
    )
    source_class = (
        "OFFICIAL_NATIVE_SAME_CYCLE_SNAPSHOT"
        if same_native_cycle
        else "OFFICIAL_NATIVE_MIXED_CYCLE_SNAPSHOT"
    )
    systems = [
        {
            "ensemble_system_id": str(
                capture["ensemble_system_id"]
            ),
            "independence_family_id": str(
                capture["independence_family_id"]
            ),
            "run_cycle": str(
                source_by_system[str(capture["ensemble_system_id"])]
                ["run_cycle"]
            ),
            "run_init_utc": str(
                source_by_system[str(capture["ensemble_system_id"])]
                ["run_init_utc"]
            ),
            "archive_query_at_utc": str(
                source_by_system[str(capture["ensemble_system_id"])]
                ["archive_query_at_utc"]
            ),
            "member_count": int(capture["member_count"]),
            "member_set_sha256": str(
                capture["member_set_sha256"]
            ),
            "members": list(capture["members"]),
            **(
                {
                    "family_temperature_curve": dict(
                        capture["family_temperature_curve"]
                    )
                }
                if isinstance(
                    capture.get("family_temperature_curve"), Mapping
                )
                else {}
            ),
        }
        for capture in sorted(
            captures,
            key=lambda row: str(row["ensemble_system_id"]),
        )
    ]
    identity = {
        "schema": SCHEMA,
        "station_id": station,
        "event_date": event_date,
        "settlement_timezone": next(iter(timezones)),
        # The freshest system's lead.  A mixed-cycle packet carries several
        # per-system leads; the packet-level label answers "what is the best
        # this packet can serve", which is the newest run's distance -- the
        # same convention ``run_init_utc_by_system`` readers use when they
        # take the max.  ``lead_by_system`` alongside keeps every system's own
        # lead readable.
        "lead_bucket": min(
            lead_buckets,
            key=lambda name: _LEAD_BUCKET_FRESHNESS_ORDER.get(name, 99),
        ),
        "lead_bucket_by_system": dict(sorted(lead_by_system.items())),
        "run_cycle": packet_run_cycle,
        "run_cycle_scope": run_cycle_scope,
        "run_cycle_by_system": run_cycle_by_system,
        "run_init_utc_by_system": run_init_utc_by_system,
        "archive_query_at_utc_by_system": (
            archive_query_at_utc_by_system
        ),
        "received_at_utc": received_at.isoformat().replace(
            "+00:00", "Z"
        ),
        "availability_evidence": availability_evidence,
        "bucket_intervals": bucket_intervals,
        "bucket_contract_sha256": bucket_contract_sha,
        "bucket_contract_authority": bucket_contract_authority,
        "systems": systems,
        "source_archives": source_rows,
        "source_archive_set_sha256": content_sha256(source_rows),
        "source_class": source_class,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "live_submit_authorized": False,
    }
    return {
        **identity,
        "snapshot_content_sha256": content_sha256(identity),
    }


__all__ = [
    "SCHEMA",
    "ARCHIVE_LEAD_BY_SCHEMA",
    "SUPPORTED_ARCHIVE_LEAD_BUCKETS",
    "VALID_RUN_CYCLES",
    "MIXED_NATIVE_CYCLES",
    "SUPPORTED_ARCHIVE_SCHEMAS",
    "RETIRED_POINT_FORECAST_SYSTEM_IDS",
    "build_official_native_forward_packet",
]
