"""Strict historical OOS training for native-member ensemble P0.

The module reads only append-only member runs and canonical daily-high truth.
Open-Meteo rows remain ``PRODUCTION_NO_LIVE_CANDIDATE`` evidence.  They may
earn a ``PRODUCTION_NO_LIVE_ONLY`` activation after a real, closed-day
strict-as-of holdout, but can never create ``FORMAL_PROMOTED`` authority.
"""

from __future__ import annotations

import copy
import json
import math
import os
import sqlite3
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
from itertools import product
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .band_reliability_store import (
    BAND_AXIS_CENTRE_DISTANCE,
    BAND_AXIS_CLAIM,
    load_band_table,
    load_band_table_with_axis,
)
from .center_width_store import (
    CENTER_WIDTH_METHOD_TAG,
    STATION_WIDTH_METHOD_TAG,
    load_center_width,
    load_center_width_floor,
    load_center_width_tiers,
    load_station_width,
)
from .contracts import ContractError, content_sha256
from .forecast_physics_asof import resolve_radiation_as_of
from .station_registry import station_coordinates
from .station_season import season_bins_for_window, station_season_bin
from .center_width_calibration import served_bucket_axis
from .station_day_ladder import settled_rung, station_day_ladder
from .cross_consensus import (
    BAND_RELIABILITY_METHOD_TAG,
    CONSENSUS_BRIDGE_METHOD_TAG,
    SYNOPTIC_COOLING_METHOD_TAG,
    apply_neighbour_direction_tilt,
    neighbour_direction_method_tag_suffix,
    RADIATION_ASOF_METHOD_TAG,
    RADIATION_CLEARNESS_METHOD_TAG,
    RADIATION_SHIFT_SIGMA,
    distribution_sigma_c,
    forecast_clearness_shift_sigma,
    forecast_radiation_shift_sigma,
    radiation_shift_sigma_for_lead,
    bridged_cross_probability,
)
from .table_as_of import (
    band_table_as_of,
    center_width_as_of,
    station_width_as_of,
)
from .stable_pool import (
    P0_MEMBERSHIP_MODE,
    P0_MEMBERSHIP_MODE_STABLE_POOL,
    STABLE_POOL_KAPPA,
    STABLE_POOL_WEIGHT_RULE,
    pool_subsets,
    stable_pool_weights,
)
from .served_shape import (
    SERVED_TABLE_LAYER_ORDER,
    BandLayer,
    CenterWidthLayer,
    apply_served_table_layers,
    model_center_c as served_model_center_c,
)
from .dwd_icon_eps_archive import (
    ARCHIVE_RUN_CYCLE_HOURS_BY_LEAD as _ICON_ARCHIVE_CYCLES_BY_LEAD,
    RUN_CYCLE_HOUR_BY_LEAD as _ICON_CYCLE_BY_LEAD,
    SUPPORTED_LEAD_DAYS as _ICON_LEADS,
    extraction_version_for_lead as _icon_extraction_version,
)
from .eccc_geps_archive import (
    ARCHIVE_RUN_CYCLE_HOURS_BY_LEAD as _GEPS_ARCHIVE_CYCLES_BY_LEAD,
    SUPPORTED_LEAD_DAYS as _GEPS_LEADS,
    extraction_version_for_lead as _geps_extraction_version,
)
from .ecmwf_ifs_archive import (
    FOLLOW_ONLY_LEAD_CYCLES as _IFS_FOLLOW_ONLY_LEAD_CYCLES,
    RUN_CYCLE_HOUR_BY_LEAD as _IFS_CYCLE_BY_LEAD,
    SUPPORTED_RUN_CYCLE_HOURS as _IFS_SUPPORTED_CYCLES,
    SUPPORTED_LEAD_DAYS as _IFS_LEADS,
    extraction_version_for_lead as _ifs_extraction_version,
)
from .ecmwf_aifs_ens_archive import (
    SUPPORTED_LEAD_DAYS as _AIFS_LEADS,
    SUPPORTED_RUN_CYCLE_HOURS as _AIFS_SUPPORTED_CYCLES,
    extraction_version_for_lead as _aifs_extraction_version,
)
from .noaa_aigefs_archive import (
    SUPPORTED_LEAD_DAYS as _AIGEFS_LEADS,
    SUPPORTED_RUN_CYCLE_HOURS as _AIGEFS_SUPPORTED_CYCLES,
    extraction_version_for_lead as _aigefs_extraction_version,
)
from .native_archive_lead import (
    LEAD_HOUR_BUCKETS,
    LONGEST_LEAD_HOUR_BUCKET,
    lead_hour_bucket_for_run,
)
from .native_ensemble_contract import (
    P0_PROBABILITY_PATHWAY,
    native_ensemble_family_set_hash,
    validate_cross_native_ensemble_weights,
)
from .noaa_gefs_archive import (
    RUN_CYCLE_HOUR_BY_LEAD as _GEFS_CYCLE_BY_LEAD,
    SUPPORTED_RUN_CYCLE_HOURS as _GEFS_SUPPORTED_CYCLES,
    SUPPORTED_LEAD_DAYS as _GEFS_LEADS,
    extraction_version_for_lead as _gefs_extraction_version,
)
from .metar_physical_tmax import (
    METHOD_VERSION as METAR_PHYSICAL_METHOD_VERSION,
    PROVIDER_ID as METAR_PHYSICAL_PROVIDER_ID,
    SYSTEM_ID as METAR_PHYSICAL_SYSTEM_ID,
)
from .official_native_forward_packet import (
    DWD_ICON_EU_POINT_SYSTEM_ID,
    JMA_MSM_POINT_SYSTEM_ID,
    MIXED_NATIVE_CYCLES,
    NCEP_HRRR_POINT_SYSTEM_ID,
    NCEP_NBM_POINT_SYSTEM_ID,
    POINT_FORECAST_SYSTEMS,
    is_fusion_excluded_point_system,
)
from .ssot import native_ensemble_foundation_schema_status
from .truth_exclusions import is_excluded


SCHEMA = "native_ensemble_strict_forward_oos_v1"
DEFAULT_MIN_TRAIN_DAYS = 20
DEFAULT_MIN_HOLDOUT_DAYS = 8
# Where a system's weight stops being discounted for thin evidence.
WEIGHT_MATURITY_FULL_TRAIN_DAYS = 28
# The live evaluation window (user decision, 2026-08-13): minimum 5 training
# days, 3-day exact shared holdout.  Training is an expanding window, so this
# floors nothing for the established families -- ECMWF and GEFS keep every
# settled day they have; only the gate's holdout shrinks.  What the shorter
# window buys is admission: the cross cohort requires every member to share
# the exact latest settled days, and an 8-day window ejected any family with
# one hole in it -- ECCC (capture stalled 8/04-8/06) could sit on thirty days
# of history and still never pair, and ICON (unfillable by design, capturing
# since 8/08) was three weeks out.  A 3-day window both can share today.
# The quality controls that matter do not live in this number: thin training
# is priced by the sqrt(n/28) weight ramp, and the combo winner still has to
# clear the CRPS/Brier/NLL selection gates.  Judged on 3 days those gates are
# noisier -- that is the accepted cost of admitting evidence forward instead
# of waiting for it.
FORWARD_ACCUMULATION_MIN_TRAIN_DAYS = 5
FORWARD_ACCUMULATION_MIN_HOLDOUT_DAYS = 3
FORWARD_ACCUMULATION_THRESHOLD_PROFILE = (
    "FORWARD_ACCUMULATION_5_TRAIN_3_HOLDOUT"
)
# Charter 62 (user ruling, 2026-08-25): the fit window is a rolling tail
# window, cross-season by construction.  The expanding window it replaces
# averaged the whole record equally, so a regime whose residual climbs --
# the tropical stations measured 0.4-1.3C of surviving under-correction in
# late August 2026 -- was always chased by an estimate diluted with months
# of quieter history, and the dilution grew with every settled day.  Sixty
# days keeps 97% of the half-life-15 weight mass below, and effectively all
# of it once the half-life shortened to 5 (see below) -- the window stopped
# binding on the decay and is now only the ceiling on how far back a case may
# be read at all.
ROLLING_TRAIN_WINDOW_DAYS = 60
# Same ruling: within the window, residual evidence decays with a half-life.
# Charter 62 set it to 15 days as a compromise -- its own measurement read
# "monotonically prefers 10" and 15 was chosen for steadiness.
#
# Shortened to 5 on 2026-08-27 (user ruling) after the sweep was re-run on
# production's own cases.  Two earlier attempts that session measured the
# wrong rows -- one pooled the retired d0/d1/d2 lead vocabulary with the hour
# buckets, halving every scope until a minimum-training filter dropped it;
# the other omitted the authority, holdout and transport filters the real
# loader applies.  Both ended up describing the TAF and METAR lanes while
# appearing to describe the ensemble families.  The measurement below calls
# ``_load_cases`` and ``_fit_parameters`` and varies nothing but this number,
# over 11,075 scope-days, 706 scopes, 7 families, all 45 stations, walking
# forward and scoring only days the fit had not seen:
#
#     H2 1.0877   H3 1.0758   H5 1.0739   H7 1.0768   H10 1.0832
#     H15 1.0927 (incumbent)  H25 1.1030          -- mean absolute error, C
#
# The curve is single-troughed at 5 and every cut agrees: RMSE -1.9%, the
# p99 tail -2.5% (so this is not mean-for-tail), all seven families and all
# five lead buckets negative, thin and thick training alike, and a held-out
# second half that reproduces the ordering more strongly than the first
# (-2.2% against -1.2%).
#
# Two limits belong here with it.  The gain is concentrated: dropping the
# five stations it helps most leaves -0.6%, and 16 of 45 stations get worse,
# EDDM by 7%.  And it has never been scored on the book -- this constant is
# upstream of the compiled envelope, and the paper ledger reads envelopes
# rather than producing them, so the ledger A/B that caught the fleet regime
# shift costing seven points cannot reach this.  H15 was never book-scored
# either; what the change buys is the better-evidenced of two values that
# stand on residuals alone.
#
# A per-station half-life was measured and refused on the same data: the
# per-station optimum is bimodal (12 stations want 2 days, 12 want 15) but
# it does not persist -- the first half's pick matches the second half's for
# only 14 of 45 stations, log-optimum correlates +0.31, and selecting per
# station on the first half scores 1.0434 on the second against 1.0397 for
# a flat H5.  The station-level spread is sampling noise, not character.
BIAS_HALF_LIFE_DAYS = 5.0
# The bias shift, split by the scope's own forecast level (user ruling,
# 2026-08-27).  Not the removed affine form and not the refused scope
# dimensions: the affine slope re-measured dead the same day at production's
# one-step horizon (CRPS +0.46% n.s. with ridge shrinkage), and the refused
# bins cut ACROSS scopes on weather-type and month, starving every cell.
# These bins sit INSIDE one scope, split its training days into forecast-
# level terciles, and shrink each tercile's shift toward the pooled one by
# ``BIAS_BIN_SHRINKAGE_CASES`` case-equivalents -- a thin or degenerate bin
# degrades to exactly the constant form.  Measured rolling one-step-ahead
# over 507 scopes x 4916 predictions: CRPS and NLL both -0.52% with the 95%
# bootstrap interval clear of zero, 32 of 45 stations improved, and the sign
# survives dropping the three largest station contributions.  RKPK, the
# band table's worst overclaimer, gains most; RCSS gains nothing -- its
# misses sit on days its own forecast level cannot see coming.
BIAS_CORRECTION_FORM = "level_terciles_v1"
BIAS_BIN_COUNT = 3
BIAS_BIN_SHRINKAGE_CASES = 10.0
# The half-life the model-versus-climatology blend is priced at, held at the
# value that blend was calibrated under while the bias half-life moved.  See
# the note beside ``reliability_weight``: these answer different questions and
# were only ever the same number by accident of sharing one weight vector.
# Retired as a serving quantity by charter 86 (the blend share is zero, so
# nothing is priced at this half-life any more); the decay is still computed
# and persisted as ``train_n_effective``, which is evidence, not a share.
RELIABILITY_HALF_LIFE_DAYS = 15.0
# Charter 86 (user ruling, 2026-09-06): the served distribution is the
# calibrated members and nothing else.  This is the pseudo-count that used to
# decide how much of each system's own training-window OUTCOMES were mixed
# into its forecast -- ``n_eff / (n_eff + K)`` model, the rest climatology --
# and it was a literal 10 written in 2026-07-31 (c7213176) with no ruling, no
# fit and no measurement behind it.  Measured 2026-09-06 by rebuilding every
# served P0 vector from its own members and lineage receipts
# (``scripts/measure_climate_blend.py``, 946 station-days, 45 stations,
# 08-15..09-05, 1,646 of 1,745 envelopes reproduced bucket-exact): dropping
# the blend improves NLL by 0.070 [95% 0.061, 0.080], Brier by 0.0125 and
# CRPS by 0.020, and is the better arm at 39 of 45 stations.  The harm is
# concentrated exactly where the design meant to help -- thin history, where
# the pseudo-count dominates: the share-under-0.72 slice improves by 0.153 of
# NLL.  Zero means no blend; the old behaviour is reachable as a research arm
# (charter 80 (8)) by setting the environment variable to 10.
CLIMATE_BLEND_PSEUDO_DAYS = float(
    os.environ.get("PM_CLIMATE_BLEND_PSEUDO_DAYS", "").strip() or 0.0
)
if not math.isfinite(CLIMATE_BLEND_PSEUDO_DAYS) or (
    CLIMATE_BLEND_PSEUDO_DAYS < 0.0
):
    raise RuntimeError(
        "invalid climate blend pseudo-count: "
        f"{CLIMATE_BLEND_PSEUDO_DAYS}"
    )
# A ready scope whose newest settled case is older than this stops being
# emitted and re-activated.  Sized above the observed truth lag (2-4 days)
# plus the worst routine capture stall (2-7 days, the IEM one-minute lane),
# so a lane merely limping never trips it -- only one that has actually
# died, like ECCC's 00Z long-lead lanes after the 2026-08-12 vocabulary
# switch.
SCOPE_SUPPLY_MAX_AGE_DAYS = 10
# Point forecast systems clear a lower bar than an ensemble family.  Written
# for the aerodrome TAF's TX group, whose member lane charter 133 retired on
# 2026-09-13; the METAR path and the regional point models use it now, and the
# reasoning below is the TAF measurement it was first argued from.
#
# The 20+8 profile is sized for a fifty-member ensemble whose spread must be
# characterised before its centre can be trusted.  A TAF TX is one number a
# human committed to; there is no spread to fit, so the same history buys far
# more certainty about it.  Measured over 303 settled station-days its mean
# error is -0.012C against -0.5 to -1.9C for every model family, and it is the
# only source in the system that is independent of NWP -- precisely the thing
# a blend of mutually-correlated models is short of.  Holding it out until it
# has 28 days at each station meant 9 of the 17 TX-issuing stations still had
# no access to it, while the models it would have corrected went on picking
# buckets unchallenged.
#
# Lowering the bar is safe only because it is paired with the weight ramp
# above: an early TAF enters at a fraction of its earned share rather than at
# full strength.  The holdout still has to exist and still has to be strictly
# out of sample -- this shortens the wait, it does not remove the test.
POINT_FORECAST_MIN_TRAIN_DAYS = 5
POINT_FORECAST_MIN_HOLDOUT_DAYS = 3
NO_LIVE_BOOTSTRAP_MIN_TRAIN_DAYS = 2
NO_LIVE_BOOTSTRAP_MIN_HOLDOUT_DAYS = 1
FORMAL_PROMOTION_THRESHOLD_PROFILE = "FORMAL_PROMOTION_20_TRAIN_8_HOLDOUT"
NO_LIVE_BOOTSTRAP_THRESHOLD_PROFILE = (
    "PRODUCTION_NO_LIVE_BOOTSTRAP_2_TRAIN_1_HISTORICAL_HOLDOUT"
)
# Open-Meteo fallback samples are allowed only after the station day is
# closed and the corresponding AWC truth is visible at the strict-as-of
# cutoff.  Keep this explicit class distinct from a prospective receipt so a
# future event can never be counted as an OOS holdout.
HISTORICAL_REPUBLISHER_RECEIPT = "HISTORICAL_ASOF_REPUBLISHER_RECEIPT"
HISTORICAL_OOS_TEMPORAL_SCOPE = "CLOSED_HISTORICAL_AWC_TRUTH"
DEFAULT_VALID_DAYS = 30
# Extra settled days each system exposes, beyond its own holdout, so the
# cross cohort can find ``min_holdout_days`` common to every member when their
# latest windows are shifted against each other by a missing capture.  Three
# on a three-day window doubles the reach; measured 2026-09-10 it recovers 88
# of the 88 four-system groups the exact rule was refusing.  The extra days
# are predicted from a fit trained before all of them, never from the gate's
# own fit, which has seen them.
CROSS_HOLDOUT_SLACK_DAYS = 3
# Family weights come from measured skill: each family's share is its summed
# system score over the total, and a cap only ever binds when that share would
# otherwise exceed it.  The cap therefore exists to preserve ensemble diversity
# -- no single family may dominate -- which is a property of the *combination*,
# not of any particular provider.
#
# ECMWF previously carried a hardcoded 0.45 here while every other family used
# the 0.75 default.  ECMWF's measured share exceeded 0.45 at ZUUU, so it was
# clipped to exactly the cap and the remainder fell to GEFS: the resulting
# 0.45/0.55 split described the cap, not the two systems' relative skill.  With
# only two candidate systems available that asymmetry silently set the weights
# outright.  Leaving this empty restores the symmetric 0.75 diversity bound and
# lets skill decide.
DEFAULT_FAMILY_WEIGHT_CAPS: dict[str, float] = {}
# There is no provisional cross-ensemble seat.  The 2026-08-15 ruling
# clarified the earlier "ICON must not sit at zero weight" as *eligibility*,
# not a guarantee: every seat in the P0 combination is won on the exact
# shared holdout, and a system without one accumulates its forward record at
# zero weight until it can compete.  The equal-family-share graft this
# replaced had left its fingerprint on every selection row it touched --
# ICON at exactly 1/n while its peers carried fitted weights -- which is a
# mandate, not eligibility.  ICON's archive still cannot be backfilled (DWD
# publishes one run and keeps it for hours), so its window fills forward
# only; that makes the wait real, and it does not change who awards seats.


def _every_supported_cycle(
    *, supported_lead_days, supported_cycle_hours, default_by_lead,
) -> dict[int, tuple[int, ...]]:
    """Charter 101 (2026-09-06 ruling "training catches up"): contract every
    cycle the collector may serve, not the one it serves by default.

    The two lanes each carried a constant and they stopped agreeing.  Capture
    widened to ``SUPPORTED_RUN_CYCLE_HOURS`` on 2026-08-14 and began serving
    06Z and 12Z in earnest in early September; the archive contracts kept
    reading ``RUN_CYCLE_HOUR_BY_LEAD``, one cycle per lead, so those cycles
    could never be fitted -- 111 of 111 06Z members went provisional, and 95%
    of those were then served a raw centre for want of three certified peers.
    ECCC was the control: its two constants agree, and it appears twelve times
    in the provisional receipts against the 229 of the two families whose do
    not.

    Nothing is loosened by this.  The cycle stays part of the extraction
    identity and of the training scope key, so each (lead, cycle) remains its
    own candidate that cohort selection either partners or leaves unused, and
    the grid coverage check downstream still refuses outright a cycle that
    genuinely comes up short for a station -- which is what keeps a contracted
    cycle from becoming a served one on its own.  A pair that is never
    captured simply never fits, which is exactly the state it is in today; the
    difference is that it is now named rather than invisible.

    ``default_by_lead`` is kept as the collector's own default and as the
    answer for a family that exports no supported set.
    """

    return {
        lead: tuple(sorted(supported_cycle_hours))
        for lead in supported_lead_days
    } if supported_cycle_hours else dict(default_by_lead)


def _lead_contracts(
    *,
    supported_lead_days,
    cycle_hour_by_lead,
    extraction_version_for_lead,
    follow_cycle_hours_by_lead=None,
) -> dict[str, list[dict[str, str]]]:
    """One lead's cycles and extraction identities, taken from its collector.

    Restating these by hand is what let the table drift from the code: the
    collector was changed to read 00Z at the shorter leads while the table --
    and the extraction version it named -- still said 18Z, so the scope check
    passed on rows whose object had come from a different run entirely.  A
    contract that is derived cannot disagree with the collector; it can only be
    wrong in the same way, which the collector's own tests can catch.

    A lead maps to one or more cycles.  Most families publish one usable run per
    lead and give a bare hour; ECCC gives a tuple, because reading it at a
    single cycle put its thickest scope in a lead-hour bucket no other family
    occupies -- see ``ARCHIVE_RUN_CYCLE_HOURS_BY_LEAD`` there.  Admitting both
    does not blur them: the cycle is part of the extraction identity and of the
    training scope key, so each stays a separate candidate that the cohort
    selection either finds a partner for or leaves unused.
    """

    contracts: dict[str, list[dict[str, str]]] = {}
    for lead in sorted(supported_lead_days):
        cycle_hours = cycle_hour_by_lead[lead]
        if isinstance(cycle_hours, int):
            cycle_hours = (cycle_hours,)
        # Charter 192: a cycle captured only to follow the newest run is
        # admitted for training like any other, and flagged so the capture
        # scheduler's readiness -- which reads the latest contracted cycle as
        # "the lead is on the store" -- keeps asking what it asked before.
        follow_hours = {
            int(hour)
            for hour in (follow_cycle_hours_by_lead or {}).get(lead, ())
        }
        contracts[f"d{lead}"] = [
            {
                "run_cycle": f"{int(cycle_hour):02d}Z",
                "extraction_version": extraction_version_for_lead(
                    lead, cycle_hour
                ),
                **(
                    {"serving_follow": True}
                    if int(cycle_hour) in follow_hours
                    else {}
                ),
            }
            for cycle_hour in cycle_hours
        ]
    return contracts


# Registered at all three leads, because P0 follows the freshest run: d2 two
# days out, d1 the day before, d0 on the event day.  Each lead keeps its own
# extraction version so the OOS scope check cannot accept one lead against
# another's contract, and training stays split by (season_bin, lead_bucket).
#
# The cycle differs by family and, within a family, by lead.  The reasons live
# with each collector -- ECCC's two admitted cycles, DWD's hourly t_2m only on
# its primary cycles, the shorter leads moving to the day's first cycle so the
# local-day grid still fits -- and are read from there rather than repeated
# here.
OFFICIAL_ARCHIVE_SCOPE_CONTRACTS = {
    "NOAA_GEFS_05": {
        "transport_provider_id": "NOAA_GEFS_PDS_S3",
        "leads": _lead_contracts(
            supported_lead_days=_GEFS_LEADS,
            cycle_hour_by_lead=_every_supported_cycle(
                supported_lead_days=_GEFS_LEADS,
                supported_cycle_hours=_GEFS_SUPPORTED_CYCLES,
                default_by_lead=_GEFS_CYCLE_BY_LEAD,
            ),
            extraction_version_for_lead=_gefs_extraction_version,
        ),
    },
    "ECMWF_IFS_ENS_OPEN_PF50": {
        "transport_provider_id": "ECMWF_OPEN_DATA_S3",
        "leads": {
            **_lead_contracts(
                supported_lead_days=_IFS_LEADS,
                cycle_hour_by_lead=_every_supported_cycle(
                    supported_lead_days=_IFS_LEADS,
                    supported_cycle_hours=_IFS_SUPPORTED_CYCLES,
                    default_by_lead=_IFS_CYCLE_BY_LEAD,
                ),
                extraction_version_for_lead=_ifs_extraction_version,
            ),
            # Charter 193 amendment: D-3 18Z for a D+2 day, captured on the
            # follow lane only -- flagged so the scheduler, which reads only
            # its own leads, is told nothing new either way.
            **_lead_contracts(
                supported_lead_days=tuple(_IFS_FOLLOW_ONLY_LEAD_CYCLES),
                cycle_hour_by_lead=_IFS_FOLLOW_ONLY_LEAD_CYCLES,
                extraction_version_for_lead=_ifs_extraction_version,
                follow_cycle_hours_by_lead=_IFS_FOLLOW_ONLY_LEAD_CYCLES,
            ),
        },
    },
    "ECCC_GEM_GLOBAL_ENS": {
        "transport_provider_id": "ECCC_DD_WEATHER",
        "leads": _lead_contracts(
            supported_lead_days=_GEPS_LEADS,
            # The archive map, not the collector's single production cycle:
            # ECCC is the one family admitted at two cycles, because at 00Z
            # alone its d2 sits in a lead-hour bucket no other family reaches.
            cycle_hour_by_lead=_GEPS_ARCHIVE_CYCLES_BY_LEAD,
            extraction_version_for_lead=_geps_extraction_version,
        ),
    },
    # DWD retains only the newest run per cycle, so ICON's history cannot be
    # backfilled at all: the run captured today qualifies two days later, and a
    # run not taken before it is overwritten is gone permanently.  Registering
    # the scope now means the accumulating archive is admitted as soon as it
    # covers a holdout cohort, rather than needing a second change then.
    "DWD_ICON_GLOBAL_EPS": {
        "transport_provider_id": "DWD_OPENDATA_ICON_EPS",
        "leads": _lead_contracts(
            supported_lead_days=_ICON_LEADS,
            # Charter 192: every cycle DWD publishes at every lead, the lead's
            # own cycle unchanged among them.  The identity now names the cycle,
            # and for the lead's own cycle it is byte-identical to the
            # lead-only string it replaced, so no trained row changes scope.
            cycle_hour_by_lead=_ICON_ARCHIVE_CYCLES_BY_LEAD,
            extraction_version_for_lead=_icon_extraction_version,
            follow_cycle_hours_by_lead={
                lead: tuple(
                    hour
                    for hour in _ICON_ARCHIVE_CYCLES_BY_LEAD[lead]
                    if hour != _ICON_CYCLE_BY_LEAD[lead]
                )
                for lead in _ICON_LEADS
            },
        ),
    },
    # Charter 204: AIFS ENS, charter L0's fifth formal P0 product.  Every
    # cycle at every lead, and every one of them a follow cycle: the family
    # is read on the follow lane at its own publication (init + ~6h10m,
    # 1-1.7 h before the IFS run of the same cycle), never on the rung, so
    # the capture scheduler's readiness -- which reads only non-follow
    # cycles -- is told nothing and no lead waits for it.  Measured before
    # registration (research replay, 2026-08-16..09-19, 1,574 station-days):
    # served P0 CRPS -4.6%, 39/45 stations.
    "ECMWF_AIFS_ENS_GLOBAL": {
        "transport_provider_id": "ECMWF_OPEN_DATA_S3",
        "leads": _lead_contracts(
            supported_lead_days=_AIFS_LEADS,
            cycle_hour_by_lead={
                lead: tuple(_AIFS_SUPPORTED_CYCLES) for lead in _AIFS_LEADS
            },
            extraction_version_for_lead=_aifs_extraction_version,
            follow_cycle_hours_by_lead={
                lead: tuple(_AIFS_SUPPORTED_CYCLES) for lead in _AIFS_LEADS
            },
        ),
    },
    # Charter 212: NOAA AIGEFS, the sixth formal product and the fifth
    # independent family, read the same way as AIFS ENS -- every cycle at
    # every lead on the follow lane at its own publication, no rung, so the
    # scheduler's readiness is told nothing.  Transport id is the NOAA NODD
    # S3 programme's (see ``noaa_aigefs_archive``).  Measured before
    # registration: served P0 CRPS -3.0% alone, -1.1% on top of AIFS.
    "NOAA_AIGEFS_025": {
        "transport_provider_id": "NOAA_GEFS_PDS_S3",
        "leads": _lead_contracts(
            supported_lead_days=_AIGEFS_LEADS,
            cycle_hour_by_lead={
                lead: tuple(_AIGEFS_SUPPORTED_CYCLES) for lead in _AIGEFS_LEADS
            },
            extraction_version_for_lead=_aigefs_extraction_version,
            follow_cycle_hours_by_lead={
                lead: tuple(_AIGEFS_SUPPORTED_CYCLES) for lead in _AIGEFS_LEADS
            },
        ),
    },
}
# Single-member point systems.  They live in their own dict rather than in
# ``OFFICIAL_ARCHIVE_SCOPE_CONTRACTS`` because the NWP capture scheduler
# iterates that dict to advertise archive runs, and these are captured by
# their own lanes -- merging them would emit capture scopes no NWP collector
# can fetch.  For OOS admission the two dicts are one set: same scope check,
# same transport allowlist, same gates.
#
# The forecaster's TAF TX had the first entry here and was retired
# 2026-09-13 (charter 133, "TAF 作 P0 点成员那条车道下线"): it minted ~40
# per-system artifacts a day for a system charters 42/95 never let seat.
# Without a contract its transport leaves the SQL allowlist, so the 5,582
# member rows already written are no longer read.
POINT_FORECAST_SCOPE_CONTRACTS = {
    # Lead 0 only, and that is the whole character of this system: it forecasts
    # from what the airport has already reported today, so there is nothing for
    # it to say about tomorrow.  The four cycle labels are carried to match the
    # scope vocabulary its cross-candidates use -- it recomputes on every
    # arriving observation rather than on a run cycle, and pairing it to a
    # cycle it does not observe is how a system ends up in a lead-hour bucket
    # no other family occupies and never finds a partner to be scored against.
    METAR_PHYSICAL_SYSTEM_ID: {
        "transport_provider_id": METAR_PHYSICAL_PROVIDER_ID,
        "leads": _lead_contracts(
            supported_lead_days=(0,),
            cycle_hour_by_lead={0: (0, 6, 12, 18)},
            extraction_version_for_lead=(
                lambda _lead, _cycle_hour: METAR_PHYSICAL_METHOD_VERSION
            ),
        ),
    },
    # The four regional point lanes (2026-08-30).  They were registered in
    # POINT_FORECAST_SYSTEMS when their collectors shipped, which is what
    # lets P0 recognise the system id -- but a scope contract is what lets
    # the OOS reader see the rows, and none was written.  The transport
    # allowlist below is DERIVED from these two dicts, so a lane with no
    # contract is not merely unadmitted: its transport never reaches the
    # SQL IN-list and every row is discarded before the per-row check runs.
    # Measured that day: MSM 37 rows over 8 days and 3 stations, HRRR 24,
    # NBM 18, ICON-EU 35 -- all captured, all verified against truth by
    # their own lanes, none ever read.  Same shape as the ICON omission the
    # derivation above was introduced to end, one layer further out.
    #
    # Every field here is taken from what the collectors actually wrote,
    # not from the registration blurbs: the JMA identity string in
    # POINT_FORECAST_SYSTEMS says ``single_cell`` while the rows say
    # ``nearest_cell``, and a contract restating the blurb would pass the
    # transport filter and then reject every row on the extraction version.
    # These lanes stamp hour buckets, so the lead label is not compared --
    # the extraction version is the lead statement (see
    # ``_historical_transport_scope_allowed``) -- and the cycles below are
    # the ones the archives were observed to serve.
    JMA_MSM_POINT_SYSTEM_ID: {
        "transport_provider_id": "RISH_KYOTO_JMA_ARCHIVE",
        "leads": {
            "h12_24": {
                "run_cycle": "00Z",
                "extraction_version": "jma_msm_surface_nearest_cell_v1",
            },
            "h24_48": {
                "run_cycle": "00Z",
                "extraction_version": "jma_msm_surface_nearest_cell_v1",
            },
        },
    },
    NCEP_HRRR_POINT_SYSTEM_ID: {
        "transport_provider_id": "NOAA_HRRR_PDS_S3",
        "leads": {
            "h12_24": {
                "run_cycle": "12Z",
                "extraction_version": "ncep_hrrr_surface_nearest_cell_v1",
            },
            "h24_48": {
                "run_cycle": "00Z",
                "extraction_version": "ncep_hrrr_surface_nearest_cell_v1",
            },
        },
    },
    NCEP_NBM_POINT_SYSTEM_ID: {
        "transport_provider_id": "NOAA_NBM_PDS_S3",
        "leads": {
            "h12_24": {
                "run_cycle": "12Z",
                "extraction_version": "ncep_nbm_core_nearest_cell_v1",
            },
        },
    },
    DWD_ICON_EU_POINT_SYSTEM_ID: {
        "transport_provider_id": "DWD_OPENDATA_ICON_EU",
        "leads": {
            "h12_24": {
                "run_cycle": "00Z",
                "extraction_version": "dwd_icon_eu_surface_nearest_cell_v1",
            },
            # Both cycles are served at the longer lead, so both are
            # admitted; the cycle is part of the training scope key, so
            # they stay separate candidates rather than being merged.
            "h24_48": [
                {
                    "run_cycle": "00Z",
                    "extraction_version": (
                        "dwd_icon_eu_surface_nearest_cell_v1"
                    ),
                },
                {
                    "run_cycle": "12Z",
                    "extraction_version": (
                        "dwd_icon_eu_surface_nearest_cell_v1"
                    ),
                },
            ],
        },
    },
}
def _admitted_scope_contract(system_id: str) -> Mapping[str, Any] | None:
    """Live lookup across both contract dicts, official lane first.

    Deliberately not a merged snapshot: tests (and any future runtime
    registration) mutate ``OFFICIAL_ARCHIVE_SCOPE_CONTRACTS`` in place, and a
    copy taken at import time would silently stop seeing them.
    """

    official = OFFICIAL_ARCHIVE_SCOPE_CONTRACTS.get(system_id)
    if official is not None:
        return official
    return POINT_FORECAST_SCOPE_CONTRACTS.get(system_id)
# Derived rather than restated, because the two SQL filters that used to spell
# this set out by hand had silently fallen behind: ICON was registered above but
# missing from both, so every official ICON row was discarded before it reached
# the scope check.  Deriving it means registering a system is a single edit.
OFFICIAL_ARCHIVE_TRANSPORT_PROVIDER_IDS = frozenset(
    str(contract["transport_provider_id"])
    for contract in (
        *OFFICIAL_ARCHIVE_SCOPE_CONTRACTS.values(),
        *POINT_FORECAST_SCOPE_CONTRACTS.values(),
    )
)
# Inlined into SQL rather than bound as parameters because the set is a
# compile-time constant of this module, never caller input.  Values are
# identifiers from the contract above, so there is nothing to escape.
_OFFICIAL_TRANSPORT_SQL_LIST = ",".join(
    f"'{provider}'"
    for provider in sorted(OFFICIAL_ARCHIVE_TRANSPORT_PROVIDER_IDS)
)
# The forward packet still requires the two globally available anchor feeds
# (ECMWF PF50 + NOAA GEFS), but the strict-OOS selector must not hard-pin the
# calibrated P0 system set to that pair.  ECCC GEPS (and any future registered
# official family) is included whenever it has the same exact holdout cohort;
# a pair remains valid when a third family is unavailable.  This keeps model
# selection availability-only and preserves the >=2 independent-family floor.
OFFICIAL_FORWARD_REQUIRED_SYSTEM_IDS = frozenset({
    "ECMWF_IFS_ENS_OPEN_PF50",
    "NOAA_GEFS_05",
})
EPS = 1e-12
# Brier only scores the bucket that settled, so a candidate that is sharper
# across the whole distribution can still lose it by a rounding-scale margin
# while winning the log score outright.  Requiring the other scoring rule
# to improve by a real margin keeps that from becoming a way in for
# genuinely worse candidates.  A station whose candidates keep losing both
# still stays rejected and keeps serving its last passing weights.
#
# Charter 211 (user ruling 2026-09-24): the model's only objective is the
# discrete bucket distribution P(Y=k|X); RMSE and CRPS are diagnostics, not
# targets.  The gate therefore reads the two proper scores of that
# distribution -- bucket Brier and the log score (NLL) -- and CRPS is still
# computed and recorded on every evaluation but decides nothing.  Before
# this the strict path was Brier-and-CRPS and the tolerance path demanded a
# CRPS margin beside the NLL margin.  Re-judged read-only from the stored
# window means of the 68,080 stable-pool evaluations since 2026-09-14
# (docs/research/bucket_objective_20260924/measure_gate_flip.py): 481
# REJECTED -> PASS, 393 PASS -> REJECTED; at the newest evaluation per key
# 36 admitted and 21 withdrawn of 6,252 keys (0.9%).
BRIER_TOLERANCE_MAX_RELATIVE_LOSS = 0.02
BRIER_TOLERANCE_MIN_RELATIVE_GAIN = 0.05
GATE_SCORING_RULE = "BRIER_LOGSCORE_V2"
VALID_NATIVE_RUN_CYCLES = frozenset({
    "00Z",
    "06Z",
    "12Z",
    "18Z",
    "AGGREGATOR_UNKNOWN_NATIVE_CYCLE",
})


def _json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _season_bin(day: str) -> str:
    # Legacy calendar vocabulary.  Charter 62 replaced it with the
    # station-local labels in ``station_season`` for every new scope; this
    # stays only so audit scripts can still read the rows written before
    # the switch.  Do not key anything new with it.
    month = int(day[5:7])
    if month in (12, 1, 2):
        return "DJF"
    if month in (3, 4, 5):
        return "MAM"
    if month in (6, 7, 8):
        return "JJA"
    return "SON"


def _temperature_bucket(value: float) -> str:
    return str(math.floor(float(value) + 0.5))


# Half a degree: the discreteness scale of the sample, not a smoothing
# choice.  Dressing exists to stop a few dozen discrete values from serving
# zeros between and beside themselves; Silverman-sized bandwidths were tried
# and rejected by the holdout (CRPS worsened on 16 of 24 scopes) because
# they also widened the distribution the calibration had already sized.
KERNEL_BANDWIDTH_C = 0.5


def _probabilities(
    values: Sequence[float],
    bandwidth_c: float = KERNEL_BANDWIDTH_C,
) -> dict[str, float]:
    """Bucket probabilities from a kernel-dressed sample, not a histogram.

    A histogram of a few dozen discrete values serves zeros even beside its
    own mean -- settled truth landed at a calibrated center of 30.0C while
    the 30C bucket held no member and was served as impossible.  Each value
    is dressed with a normal kernel instead, so mass decays with distance
    the way the sample's own shape says it should; a bucket far from every
    member is small because the model puts little there, not zero because
    the sample is finite.
    """

    if not values:
        raise ContractError("native_ensemble_oos_values_missing")
    current = []
    for value in values:
        if not math.isfinite(float(value)):
            raise ContractError("native_ensemble_oos_value_invalid")
        current.append(float(value))
    bandwidth = float(bandwidth_c)
    # Variance-preserving: shrink deviations so sample variance plus kernel
    # variance reproduces the original.  The calibration already chose the
    # distribution's width; dressing may not change it, only make it smooth.
    mean = sum(current) / len(current)
    std = math.sqrt(
        sum((v - mean) ** 2 for v in current) / len(current)
    )
    if std > bandwidth:
        shrink = math.sqrt(1.0 - (bandwidth / std) ** 2)
        current = [mean + shrink * (v - mean) for v in current]
    lo = math.floor(min(current) - 4.0 * bandwidth)
    hi = math.ceil(max(current) + 4.0 * bandwidth)
    def _cdf(x: float, mu: float) -> float:
        return 0.5 * (1.0 + math.erf((x - mu) / (bandwidth * math.sqrt(2.0))))
    result: dict[str, float] = {}
    for bucket in range(int(lo), int(hi) + 1):
        mass = sum(
            _cdf(bucket + 0.5, mu) - _cdf(bucket - 0.5, mu)
            for mu in current
        ) / len(current)
        if mass > 0.0:
            result[str(bucket)] = mass
    total = sum(result.values())
    return {
        bucket: value / total
        for bucket, value in sorted(
            result.items(), key=lambda item: int(item[0])
        )
    }


# Charter 211 (六) -> the gate scores the station's market ladder.  The
# holdout used to dress, bridge, shape and score every day on a 1C integer
# grid, while serving does all four on the day's market ladder: for a
# Celsius market that differs only in the two open end buckets, for a
# Fahrenheit market (2F = 1.11C a rung, edges on the half-degree F) it is a
# different grid altogether, and the band table's distance axis counts
# rungs.  ``market_ladder`` puts the holdout on the ladder the station-day
# was priced against; ``integer_c`` is the grid it replaced.  A day with no
# committed ladder stays on the integer grid and says so on its record.
HOLDOUT_GRID_INTEGER_C = "integer_c"
HOLDOUT_GRID_MARKET_LADDER = "market_ladder"
HOLDOUT_GRIDS = (HOLDOUT_GRID_INTEGER_C, HOLDOUT_GRID_MARKET_LADDER)
HOLDOUT_GRID = (
    os.environ.get("PM_OOS_HOLDOUT_GRID", HOLDOUT_GRID_INTEGER_C).strip()
    or HOLDOUT_GRID_INTEGER_C
)
if HOLDOUT_GRID not in HOLDOUT_GRIDS:
    raise RuntimeError(f"unknown PM_OOS_HOLDOUT_GRID: {HOLDOUT_GRID}")


def _station_day_ladder(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    event_date: str,
    as_of_utc: str,
) -> dict[str, Any] | None:
    """The ladder a settled station-day was priced against, as known then.

    ``station_day_ladder`` is the one rule (the settlement labels read it
    too): the newest committed decision of any stage, here held to the
    as-of the build runs at.  None when nothing was committed, or when the
    rungs do not make a served axis.
    """

    found = station_day_ladder(
        conn, station_id=station_id, event_date=event_date,
        as_of_utc=as_of_utc,
    )
    if found is None or len(found[2]) < 2:
        return None
    history_id, _decided, rows = found
    intervals = {bucket: (lower, upper) for bucket, _o, lower, upper in rows}
    ordered = [row[0] for row in rows]
    try:
        centers, open_edges = served_bucket_axis(intervals, ordered)
    except ValueError:
        return None
    return {
        "history_id": history_id,
        "rows": rows,
        "ordered": ordered,
        "intervals": intervals,
        "centers_c": [float(value) for value in centers],
        "open_edge_bounds_c": open_edges,
    }


def _ladder_bucket(ladder: Mapping[str, Any], value: float) -> str:
    """The rung a value lands in, lower edge inclusive -- the settler's rule."""

    rung = settled_rung(ladder["rows"], float(value))
    if rung is None:
        raise ContractError("native_ensemble_oos_value_outside_ladder")
    return rung


def _metrics(
    probability: Mapping[str, float],
    actual: float,
    ladder: Mapping[str, Any] | None = None,
) -> dict[str, float]:
    if ladder is not None:
        return _ladder_metrics(probability, actual, ladder)
    actual_bucket = _temperature_bucket(actual)
    support = sorted({
        *(int(bucket) for bucket in probability),
        int(actual_bucket),
    })
    brier = sum(
        (
            float(probability.get(str(bucket), 0.0))
            - (1.0 if str(bucket) == actual_bucket else 0.0)
        ) ** 2
        for bucket in support
    )
    nll = -math.log(
        max(float(probability.get(actual_bucket, 0.0)), EPS)
    )
    crps = 0.0
    for bucket in range(min(support), max(support) + 1):
        forecast_cdf = sum(
            float(value)
            for key, value in probability.items()
            if int(key) <= bucket
        )
        observed_cdf = 1.0 if int(actual_bucket) <= bucket else 0.0
        crps += (forecast_cdf - observed_cdf) ** 2
    return {"brier": brier, "nll": nll, "crps": crps}


def _ladder_metrics(
    probability: Mapping[str, float],
    actual: float,
    ladder: Mapping[str, Any],
) -> dict[str, float]:
    """Brier, log score and CRPS on the market ladder.

    Brier sums every rung of the ladder; the log score reads the rung the
    value settles in; CRPS (a diagnostic since charter 211) is the step
    CDF's on the served axis, each rung weighted by the distance to the
    next rung's centre so it reads in degrees like the integer grid's.
    """

    ordered = list(ladder["ordered"])
    if set(str(key) for key in probability) - set(ordered):
        raise ContractError("native_ensemble_oos_ladder_key_mismatch")
    hit = _ladder_bucket(ladder, actual)
    values = [float(probability.get(key, 0.0)) for key in ordered]
    brier = sum(
        (value - (1.0 if key == hit else 0.0)) ** 2
        for key, value in zip(ordered, values)
    )
    nll = -math.log(max(values[ordered.index(hit)], EPS))
    centers = list(ladder["centers_c"])
    hit_index = ordered.index(hit)
    crps = 0.0
    cumulative = 0.0
    for index in range(len(ordered) - 1):
        cumulative += values[index]
        observed = 1.0 if index >= hit_index else 0.0
        crps += (cumulative - observed) ** 2 * (
            centers[index + 1] - centers[index]
        )
    return {"brier": brier, "nll": nll, "crps": crps}


def _cross_oos_gate(
    cross_metrics: Mapping[str, float],
    climate_metrics: Mapping[str, float],
) -> dict[str, Any]:
    """Decide the gate and record which path decided it.

    Charter 211: the strict path beats climatology on bucket Brier and the
    log score (NLL).  The tolerance path exists because Brier can be lost by
    a rounding-scale margin on a candidate that is better everywhere else,
    and it demands a real margin on the log score in exchange.  CRPS is
    recorded beside both paths as a diagnostic and decides neither.  The
    returned record is stored with the evaluation so a later review can
    separate tolerance admissions from strict ones without re-deriving them.
    """

    strict_pass = (
        float(cross_metrics["brier"])
        <= float(climate_metrics["brier"]) + EPS
        and float(cross_metrics["nll"])
        <= float(climate_metrics["nll"]) + EPS
    )
    climate_crps = float(climate_metrics.get("crps", 0.0))
    crps_relative_gain = (
        (climate_crps - float(cross_metrics.get("crps", climate_crps)))
        / climate_crps
        if climate_crps > EPS else None
    )
    gate: dict[str, Any] = {
        "path": "STRICT" if strict_pass else "REJECTED",
        "pass": strict_pass,
        "scoring": GATE_SCORING_RULE,
        "brier_tolerance_max_relative_loss": (
            BRIER_TOLERANCE_MAX_RELATIVE_LOSS
        ),
        "tolerance_min_relative_gain": BRIER_TOLERANCE_MIN_RELATIVE_GAIN,
        # Diagnostic only (charter 211): never read by the verdict.
        "crps_relative_gain": crps_relative_gain,
    }
    if strict_pass:
        return gate

    climate_brier = float(climate_metrics["brier"])
    climate_nll = float(climate_metrics["nll"])
    if climate_brier <= EPS or climate_nll <= EPS:
        # Relative margins are meaningless against a zero baseline, and a
        # perfect climatology is not something to be admitted around.
        gate["tolerance_available"] = False
        return gate

    brier_relative_loss = (
        float(cross_metrics["brier"]) - climate_brier
    ) / climate_brier
    nll_relative_gain = (
        climate_nll - float(cross_metrics["nll"])
    ) / climate_nll
    tolerance_pass = (
        brier_relative_loss <= BRIER_TOLERANCE_MAX_RELATIVE_LOSS
        and nll_relative_gain >= BRIER_TOLERANCE_MIN_RELATIVE_GAIN
    )
    gate.update({
        "tolerance_available": True,
        "brier_relative_loss": brier_relative_loss,
        "nll_relative_gain": nll_relative_gain,
        "path": "NLL_TOLERANCE" if tolerance_pass else "REJECTED",
        "pass": tolerance_pass,
    })
    return gate


# Charter 134 (七) (user ruling 2026-09-22, 「合成件（稳定池件）：让评估门稳下来」):
# a stable-pool seat is certified on its own rolling record, not on three days.
#
# The pool seats a fixed set with nothing chosen on the holdout, so the same
# key (station, season, lead, fitted cycle map, set) is re-scored every scan
# and each scan scores its three newest shared days strictly out of sample.
# Judged on those three days alone the verdict flipped on noise: over the
# pool's first eight days (171,066 evaluations, 3,866 keys) 15.0% of
# consecutive holdout windows changed verdict and 7.1% went PASS -> REJECTED,
# and the rejected seats were not bad ones -- on the event day they would
# have served they still beat climatology by RPS 0.67 (95% [0.45, 0.93]) and
# NLL 0.62 nat.  A rejected seat is dark to the compile, so P0 fell back to
# contest artifacts charter 134 no longer re-mints, and failed closed where
# none were left (4.7% of scope-hours with all four families in hand).
#
# The gate now reads the key's own newest ``STABLE_POOL_GATE_WINDOW_DAYS``
# scored days -- each day from the newest evaluation that scored it, so every
# day is out of sample for the fit that scored it -- and applies the same
# ``_cross_oos_gate`` to their means.  A key with only its own three days is
# judged exactly as before.  One guard is added, because a longer window
# must not let a seat that has just broken hide behind its older days: if
# its own newest holdout scores the log score more than
# ``STABLE_POOL_GATE_BREAKAGE_MAX_RELATIVE_NLL_LOSS`` above climatology's,
# it is rejected whatever the window says.  The floor was set on CRPS at
# 1.0 (twice climatology's) -- the one bin of the replay where forward skill
# over climatology vanished; charter 211 moved it to the log score and the
# threshold 0.5 was chosen to keep the same ejection rate: on the 68,080
# stable-pool evaluations since 09-14 the CRPS floor hit 439 (0.64%), an
# NLL floor at 0.5 hits 489 (0.72%) and at 1.0 only 124 (0.18%).  The CRPS
# reading stays on the record as a diagnostic.
# Measured on the same evaluations: window flips 15.0% -> 5.5%, PASS ->
# REJECTED 7.1% -> 2.1%, dark scope-hours 4.8% -> 1.4% (all four in hand,
# from 09-18) and 8.2% -> 3.4% (ECMWF+GEFS only); the seats admitted that the
# three-day gate refused beat climatology on their served day by RPS 1.01
# [0.67, 1.36] and NLL 0.83 [0.58, 1.08] (164 station-days).
STABLE_POOL_GATE_RULE = "STABLE_POOL_ROLLING_WINDOW_GATE_V2"
STABLE_POOL_GATE_WINDOW_DAYS = 10
STABLE_POOL_GATE_BREAKAGE_MAX_RELATIVE_NLL_LOSS = 0.5
# The window reads only the pool's own record: evaluations written before
# charter 134's first deploy scored contest-weighted blends under the same
# key, and a row whose gate names another membership mode (a rollback) is
# skipped for the same reason.
STABLE_POOL_GATE_HISTORY_EPOCH_UTC = "2026-09-14T04:00:00Z"
# How far back an evaluation, and a scored day, may be read at all.  A key
# whose cohort stopped forming keeps no old days alive past this.
STABLE_POOL_GATE_LOOKBACK_DAYS = 21


def _stable_pool_gate_history(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    season_bin: str,
    lead_bucket: str,
    run_cycle: str,
    run_cycle_by_system_json: str,
    run_cycle_by_system_sha256: str,
    settlement_definition: str,
    system_ids: Sequence[str],
    threshold_profile: str,
    as_of_utc: str,
) -> dict[str, tuple[dict[str, float], dict[str, float]]]:
    """The key's scored days on record: event date -> (seat, climatology).

    Each day is taken from the newest evaluation that scored it.  The read
    seeks the evaluation table's scope index (station, season, lead, cycle,
    set, evaluated_at) and pulls only the per-day scores out of the JSON.
    """

    as_of = datetime.fromisoformat(as_of_utc.replace("Z", "+00:00"))
    lookback = (
        as_of - timedelta(days=STABLE_POOL_GATE_LOOKBACK_DAYS)
    ).isoformat().replace("+00:00", "Z")
    since = max(lookback, STABLE_POOL_GATE_HISTORY_EPOCH_UTC)
    ids = sorted(str(value) for value in system_ids)
    rows = conn.execute(
        "SELECT json_extract(oos_metrics_json,"
        "'$.equal_weight_diagnostic.scheme_daily_scores'),"
        "json_extract(oos_metrics_json,'$.gate.membership_mode') "
        "FROM calibration_cross_native_ensemble_oos_evaluation "
        "WHERE station_id=? AND season_bin=? AND lead_bucket=? "
        "AND run_cycle=? AND available_system_set_hash=? "
        "AND evaluated_at_utc>=? AND evaluated_at_utc<=? "
        "AND run_cycle_by_system_json=? AND run_cycle_by_system_sha256=? "
        "AND settlement_definition=? AND ensemble_system_ids_json=? "
        "AND threshold_profile=? "
        "ORDER BY evaluated_at_utc,evaluation_id",
        (
            station_id,
            season_bin,
            lead_bucket,
            run_cycle,
            content_sha256(ids),
            since,
            as_of_utc,
            run_cycle_by_system_json,
            run_cycle_by_system_sha256,
            settlement_definition,
            _json(ids),
            threshold_profile,
        ),
    ).fetchall()
    days: dict[str, tuple[dict[str, float], dict[str, float]]] = {}
    for raw_scores, membership in rows:
        if membership not in (None, P0_MEMBERSHIP_MODE_STABLE_POOL):
            continue
        try:
            scores = json.loads(raw_scores) if raw_scores else []
        except (TypeError, ValueError):
            continue
        for row in scores if isinstance(scores, list) else []:
            try:
                day = str(row["event_date"])
                learned = {m: float(row["learned"][m]) for m in ("brier", "crps", "nll")}
                climate = {m: float(row["climate"][m]) for m in ("brier", "crps", "nll")}
            except (KeyError, TypeError, ValueError):
                continue
            if all(math.isfinite(v) for v in (*learned.values(), *climate.values())):
                days[day] = (learned, climate)
    return days


def _stable_pool_window_gate(
    *,
    own_learned_metrics: Mapping[str, float],
    own_climate_metrics: Mapping[str, float],
    own_daily_scores: Sequence[Mapping[str, Any]],
    history: Mapping[str, tuple[Mapping[str, float], Mapping[str, float]]],
    holdout_end_date: str,
) -> dict[str, Any]:
    """Charter 134 (七): the pool seat's gate on its rolling window.

    ``own_*`` are this evaluation's holdout: its means and its per-day
    scores, which override any older scoring of the same day.  The returned
    record carries the three-day verdict it replaced as ``own_holdout_gate``
    so a review can still read the raw verdict sequence.
    """

    own_gate = _cross_oos_gate(own_learned_metrics, own_climate_metrics)
    days: dict[str, tuple[Mapping[str, float], Mapping[str, float]]] = {
        str(day): (learned, climate)
        for day, (learned, climate) in history.items()
    }
    for row in own_daily_scores:
        days[str(row["event_date"])] = (row["learned"], row["climate"])
    end = str(holdout_end_date)[:10]
    horizon = (
        date.fromisoformat(end)
        - timedelta(days=STABLE_POOL_GATE_LOOKBACK_DAYS)
    ).isoformat()
    window = sorted(
        day for day in days if horizon < day[:10] <= end
    )[-STABLE_POOL_GATE_WINDOW_DAYS:]
    if not window:
        raise ContractError("native_ensemble_oos_pool_gate_window_empty")
    window_learned = _mean_metrics([days[day][0] for day in window])
    window_climate = _mean_metrics([days[day][1] for day in window])
    window_gate = _cross_oos_gate(window_learned, window_climate)
    own_nll = float(own_learned_metrics["nll"])
    climate_nll = float(own_climate_metrics["nll"])
    # Against a perfect climatology any loss is unbounded; recorded as None
    # (JSON has no infinity) and treated as a breach.
    own_relative_nll_loss = (
        (own_nll - climate_nll) / climate_nll
        if climate_nll > EPS
        else None
    )
    breakage = (
        own_nll > EPS
        if own_relative_nll_loss is None
        else own_relative_nll_loss
        > STABLE_POOL_GATE_BREAKAGE_MAX_RELATIVE_NLL_LOSS
    )
    # Diagnostic only (charter 211): the CRPS reading the floor used to read.
    own_crps = float(own_learned_metrics.get("crps", 0.0))
    climate_crps = float(own_climate_metrics.get("crps", 0.0))
    own_relative_crps_loss = (
        (own_crps - climate_crps) / climate_crps
        if climate_crps > EPS
        else None
    )
    passed = bool(window_gate["pass"]) and not breakage
    return {
        **window_gate,
        "path": (
            "REJECTED_BREAKAGE_FLOOR" if breakage and window_gate["pass"]
            else ("WINDOW_" + str(window_gate["path"])
                  if window_gate["pass"] else "REJECTED")
        ),
        "pass": passed,
        "rule": STABLE_POOL_GATE_RULE,
        "membership_mode": P0_MEMBERSHIP_MODE_STABLE_POOL,
        "window_max_days": STABLE_POOL_GATE_WINDOW_DAYS,
        "window_day_count": len(window),
        "window_event_dates": window,
        "window_learned_metrics": window_learned,
        "window_climate_metrics": window_climate,
        "breakage_floor": {
            "max_relative_nll_loss":
                STABLE_POOL_GATE_BREAKAGE_MAX_RELATIVE_NLL_LOSS,
            "own_relative_nll_loss": own_relative_nll_loss,
            "own_relative_crps_loss": own_relative_crps_loss,
            "hit": breakage,
        },
        "own_holdout_gate": own_gate,
    }


def _mean_metrics(rows: Sequence[Mapping[str, float]]) -> dict[str, float]:
    if not rows:
        raise ContractError("native_ensemble_oos_metric_rows_missing")
    return {
        metric: sum(float(row[metric]) for row in rows) / len(rows)
        for metric in ("brier", "nll", "crps")
    }


def _pairwise_skill(
    worse: Mapping[str, float],
    better: Mapping[str, float],
) -> dict[str, Any]:
    """How much ``better`` improves on ``worse``, in the gate's own units.

    Sign convention is fixed by the argument names, not by the metric: every
    score here is a loss, so a positive number always means the second
    argument won.  ``bits`` restates the log score as information because it
    is the one figure that is comparable across scopes with different bucket
    counts -- a percentage of NLL is not.
    """

    out: dict[str, Any] = {}
    for metric in ("brier", "nll", "crps"):
        base = float(worse[metric])
        out[metric] = float(better[metric]) - base
        out[f"{metric}_relative_gain"] = (
            (base - float(better[metric])) / base if base else 0.0
        )
    out["bits"] = (
        float(worse["nll"]) - float(better["nll"])
    ) / math.log(2.0)
    out["verdict"] = (
        "BETTER" if out["bits"] > 0.0
        else ("WORSE" if out["bits"] < 0.0 else "TIED")
    )
    return out


def _equal_weight_diagnostic(
    *,
    climate_metrics: Mapping[str, float],
    equal_weight_metrics: Mapping[str, float] | None,
    learned_metrics: Mapping[str, float],
    available_system_ids: Sequence[str],
    seated_system_ids: Sequence[str],
    seated_weights: Mapping[str, float],
    equal_cohort_metrics: Mapping[str, float] | None = None,
    daily_scores: Sequence[Mapping[str, Any]] = (),
    shadow_weight_schemes: Mapping[str, Mapping[str, float]] = {},
    shadow_metrics: Mapping[str, Mapping[str, float]] = {},
) -> dict[str, Any]:
    """Three-way skill matrix: climatology, equal weight, learned weights.

    The gate asks one question -- does the assembled distribution beat
    climatology -- and a pass answers two others by implication without ever
    testing either: that the models carry information, and that our weighting
    of them carries information beyond averaging.  Both can be false while the
    gate still passes, and one of them measurably is: as of 2026-08 the seated
    weights sit at an effective system count of 1.95 out of 2 and the subset
    contest drops nobody in 82% of scopes, so "learned" and "equal" are
    largely the same forecast wearing two labels.  This records the
    decomposition so that question stops being unanswerable.

    The candidate set is held fixed across the schemes being compared: equal
    weight is taken over the seated systems, so comparison B moves only the
    weights.  Where the contest did drop someone, the full-cohort blend is
    carried alongside as its own leg rather than folded into B, because
    "averaging would have been better" and "we should not have dropped that
    system" are different findings with different fixes.

    Recorded, not enforced, exactly as the shape-layer ablation is: one
    scope's holdout is three or four days and cannot retire a design, and no
    interval is estimable inside it -- ``scheme_daily_scores`` carries the
    paired samples so a pooled bootstrap over days can be run downstream.
    """

    matrix: dict[str, Any] = {
        "available_system_ids": list(available_system_ids),
        "available_system_count": len(available_system_ids),
        "seated_system_ids": list(seated_system_ids),
        "seated_system_count": len(seated_system_ids),
        # Named so a reader never has to infer which systems the equal-weight
        # leg averaged: it is this list, the same one the learned weights
        # were fitted over.
        "fixed_candidate_system_ids": list(seated_system_ids),
        # Both facts the comparison hinges on, carried beside it so a reader
        # never has to reconstruct them: whether the contest actually dropped
        # anyone, and how far the surviving weights sit from flat.  When the
        # answer is "nobody, and not far", comparison B is near-tautological
        # and its own smallness is the finding.
        "dropped_system_ids": sorted(
            set(available_system_ids) - set(seated_system_ids)
        ),
        "weight_l1_distance_from_equal": (
            sum(
                abs(
                    float(weight)
                    - 1.0 / float(len(seated_weights))
                )
                for weight in seated_weights.values()
            )
            if seated_weights else 0.0
        ),
        "effective_system_count": (
            1.0 / sum(
                float(weight) ** 2 for weight in seated_weights.values()
            )
            if seated_weights
            and any(float(w) for w in seated_weights.values())
            else 0.0
        ),
        "climatology_metrics": dict(climate_metrics),
        "learned_metrics": dict(learned_metrics),
    }
    if equal_weight_metrics is None:
        matrix["state"] = "EQUAL_WEIGHT_UNAVAILABLE"
        return matrix
    matrix["state"] = "MEASURED"
    matrix["equal_weight_metrics"] = dict(equal_weight_metrics)
    matrix["comparisons"] = {
        # A: do the weather models carry anything over climatology at all?
        "a_climate_vs_equal_weight": _pairwise_skill(
            climate_metrics, equal_weight_metrics
        ),
        # B: on one fixed candidate set, does learning the weights beat
        # weighting them alike?
        "b_equal_weight_vs_learned": _pairwise_skill(
            equal_weight_metrics, learned_metrics
        ),
        # C: the gate's own question, restated here so all three sit in one
        # place and A + B is visibly reconcilable with C.
        "c_climate_vs_learned": _pairwise_skill(
            climate_metrics, learned_metrics
        ),
    }
    if equal_cohort_metrics is not None:
        matrix["equal_cohort_metrics"] = dict(equal_cohort_metrics)
        matrix["comparisons"]["d_equal_cohort_vs_learned"] = _pairwise_skill(
            equal_cohort_metrics, learned_metrics
        )
    # Candidate weightings, scored and serving nothing.  Each is the seated
    # systems under a different rule, so its gap to the learned blend is a
    # gap in weighting alone -- the same contrast B draws, with equal weight
    # swapped for a covariance-aware one.
    for label in sorted(shadow_metrics):
        matrix.setdefault("shadow_schemes", {})[label] = {
            "metrics": dict(shadow_metrics[label]),
            "weights": dict(shadow_weight_schemes.get(label) or {}),
            "vs_learned": _pairwise_skill(
                shadow_metrics[label], learned_metrics
            ),
            "vs_equal_weight": _pairwise_skill(
                shadow_metrics[label], equal_weight_metrics
            ),
        }
    if daily_scores:
        matrix["scheme_daily_scores"] = [
            dict(row) for row in daily_scores
        ]
    return matrix


# The bias correction is a single constant per scope.  A level-dependent
# (affine) alternative was fitted here and chosen per scope on a six-day
# validation tail; it was removed on 2026-08-18 after it was measured, not
# because of the plumbing bug that kept it from ever reaching production.
#
# Measured on a 60% chronological split over 479 scopes (all 45 stations),
# scoring the holdout with this module's own metrics:
#   * on the scopes the tail test picked, affine lost in the gate's currency
#     -- CRPS +4.3%, NLL +4.6%, both bootstrap intervals clear of zero.
#   * with the selector removed entirely, always-affine still lost across all
#     444 estimable scopes (CRPS +2.7%, NLL +2.0%), so the form was the
#     problem, not the six-day tail.  Per scope it won 187 of 444.
#   * a selector sweep over tail lengths 6-20 and margins 0.5-0.8 found one
#     favourable cell (tail 14, margin 0.6) carried by a single scope; drop
#     that scope and it reverses.
#   * centre MSE alone could not see this -- its sign flipped with the split
#     point (+12%, +2%, -7% at 50/60/70%) while CRPS stayed negative
#     throughout.  That is why the form survived earlier MSE-only looks.
#
# It also distorted the cross-ensemble weights, which are 1/train_rmse_c.
# Refitting under the affine form overwrote train_rmse_c with a two-parameter
# in-sample residual and compared it against one-parameter residuals from
# every other system: it came out lower in 89 of 89 affine scopes, a median
# 1.44x weight-score advantage, while delivering 1.05x on unseen days.
# Any future level-dependent form has to be judged out of sample and has to
# report a weight metric that is comparable across forms.


def _fit_parameters(
    cases: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    if not cases:
        raise ContractError("native_ensemble_oos_train_cases_missing")
    errors: list[float] = []
    spreads: list[float] = []
    climate_samples: list[float] = []
    centers: list[float] = []
    actuals: list[float] = []
    event_days: list[date] = []
    for case in cases:
        values = [float(value) for value in case["values"]]
        mean = sum(values) / len(values)
        centers.append(mean)
        actuals.append(float(case["actual_tmax_c"]))
        errors.append(float(case["actual_tmax_c"]) - mean)
        spreads.append(math.sqrt(
            sum((value - mean) ** 2 for value in values) / len(values)
        ))
        climate_samples.append(float(case["actual_tmax_c"]))
        try:
            event_days.append(
                date.fromisoformat(str(case["event_date"])[:10])
            )
        except (KeyError, ValueError) as exc:
            # Weighted estimation needs every case dated.  Falling back to
            # equal weights here would fit a different form from the one the
            # OOS gate scores -- the exact silent split the affine-form
            # incident taught -- so an undated case refuses the whole fit.
            raise ContractError(
                "native_ensemble_oos_train_case_event_date_missing"
            ) from exc
    # Charter 62: residual evidence decays with a half-life, so the estimate
    # tracks the current regime instead of the whole record's average.  Ages
    # are taken from the newest training day, never the wall clock, so a
    # replayed fit weighs its cases exactly as the original did.
    anchor_day = max(event_days)
    weights = [
        0.5 ** ((anchor_day - event_day).days / BIAS_HALF_LIFE_DAYS)
        for event_day in event_days
    ]
    weight_total = sum(weights)
    bias = sum(
        weight * error for weight, error in zip(weights, errors)
    ) / weight_total
    # The tercile shifts.  Cuts come from the scope's own training levels;
    # each bin's decayed-mean error is shrunk toward the pooled shift, so a
    # bin holding little weight -- or a degenerate scope whose levels never
    # move -- returns the pooled number rather than its own noise.
    ordered_levels = sorted(centers)
    bias_bin_cuts = [
        ordered_levels[
            max(0, (len(ordered_levels) * (index + 1)) // BIAS_BIN_COUNT - 1)
        ]
        for index in range(BIAS_BIN_COUNT - 1)
    ]
    bin_weight_totals = [0.0] * BIAS_BIN_COUNT
    bin_error_sums = [0.0] * BIAS_BIN_COUNT
    for weight, center, error in zip(weights, centers, errors):
        index = _level_bin(center, bias_bin_cuts)
        bin_weight_totals[index] += weight
        bin_error_sums[index] += weight * error
    bias_bin_shifts = [
        (bin_error_sums[index] + BIAS_BIN_SHRINKAGE_CASES * bias)
        / (bin_weight_totals[index] + BIAS_BIN_SHRINKAGE_CASES)
        for index in range(BIAS_BIN_COUNT)
    ]
    rmse = math.sqrt(sum(
        weight * error * error
        for weight, error in zip(weights, errors)
    ) / weight_total)
    # ``rmse`` and ``residual_sigma`` stay on the POOLED shift deliberately,
    # not the tercile one: ``train_rmse_c`` is the cross weight, and a form
    # that lowers its own in-sample residual with extra parameters buys
    # weight it has not earned -- the affine form banked a median 1.44x
    # this way and delivered 1.05x out of sample.  One basis for every
    # form keeps the weights comparable.
    #
    # The scale the served distribution actually has to cover.  ``rmse`` is
    # taken about zero and so carries the bias, but the bias is exactly what
    # the centre shift above removes: sizing a width by it charges the
    # distribution twice for an offset that has already been corrected.
    # Measured over 231 scopes, rmse/sigma runs 1.26 at the median and 2.43
    # at the ninetieth percentile -- a per-scope inflation that a single
    # global width factor downstream can centre but cannot flatten.
    residual_sigma = math.sqrt(sum(
        weight * (error - bias) ** 2
        for weight, error in zip(weights, errors)
    ) / weight_total)
    mean_spread = sum(
        weight * spread for weight, spread in zip(weights, spreads)
    ) / weight_total
    # The cap was 3.0 and it bound: 199 of 614 fitted parameter sets sat on
    # it, and that group measured a z standard deviation of 1.66 -- still
    # 66% too narrow after the largest inflation the cap allowed -- while the
    # sets that landed near 2.0 measured 1.03 with 95% interval coverage of
    # exactly 95%.  The mechanism works; it was being truncated.  Measured
    # requirements per system run 2.5x to 4.7x (GEFS_SEAMLESS 4.7x, UKMO
    # 4.0x, GEFS 3.4x), so 5.0 clears every observed case while still
    # refusing a runaway fit on a degenerate spread.
    spread_factor = min(
        5.0,
        max(0.5, residual_sigma / max(mean_spread, 0.1)),
    )
    # Decayed cases are fractional evidence, so the day count overstates
    # what the fit actually rests on.  Kish's effective sample size prices
    # it honestly: equal weights give back exactly len(cases), and a full
    # 60-day window at half-life 15 carries about 43 effective days -- close
    # enough to the old counts that the climate blend barely moves.
    train_n_effective = weight_total ** 2 / sum(
        weight * weight for weight in weights
    )
    # Charter 86: the share of this scope's own training-window outcomes that
    # is mixed into its forecast.  Zero by default, which is to say the served
    # distribution is the calibrated members and nothing else; see
    # ``CLIMATE_BLEND_PSEUDO_DAYS`` for what the mixture cost when measured.
    #
    # Two things the retired blend was standing in for, both of which already
    # have an owner, which is why nothing replaces it here.  A model too new
    # to trust is refused a certificate by the OOS gate and served through the
    # provisional path, which borrows its peers' parameters and has never
    # blended.  A model whose width is understated is widened by
    # ``spread_factor`` against its own residual, and again downstream by the
    # centre/width layer.  Mixing in a second distribution was a third answer
    # to the same question, and the only one that could move mass to a bucket
    # no member named -- which on an open-ended edge bucket means the whole
    # of a warm spell landing on tomorrow.
    #
    # The decay itself stays: ``train_n_effective`` is how much evidence this
    # fit rests on, which readers and gates use, and it is not a share.
    reliability_weights = [
        0.5 ** ((anchor_day - event_day).days / RELIABILITY_HALF_LIFE_DAYS)
        for event_day in event_days
    ]
    reliability_total = sum(reliability_weights)
    reliability_n_effective = reliability_total ** 2 / sum(
        weight * weight for weight in reliability_weights
    )
    reliability_weight = reliability_n_effective / (
        reliability_n_effective + CLIMATE_BLEND_PSEUDO_DAYS
    ) if CLIMATE_BLEND_PSEUDO_DAYS > 0.0 else 1.0
    # Every key returned here has to reach an artifact column, or serving
    # silently runs a different fit from the one the OOS gate scored.  That is
    # not hypothetical: the removed affine form was fitted, gated and folded
    # into the cross weights while never being persisted, so production served
    # the constant form for months against a certificate earned by another.
    # ``test_fit_parameter_keys_are_all_persisted`` freezes this set against
    # the three parameter columns; adding a key means deciding where it lands.
    return {
        "bias_c": bias,
        "correction_form": BIAS_CORRECTION_FORM,
        "bias_bin_cuts_c": bias_bin_cuts,
        "bias_bin_shifts_c": bias_bin_shifts,
        "spread_factor": spread_factor,
        "reliability_weight": reliability_weight,
        "climate_samples_c": climate_samples[-180:],
        "train_rmse_c": rmse,
        "train_residual_sigma_c": residual_sigma,
        "train_mean_spread_c": mean_spread,
        "train_n_effective": train_n_effective,
        # Provenance of this fit -- what it saw -- so a served row can say
        # whether it was trained through yesterday or through a week ago.
        # Measured 2026-09-02: the served bias sat a median six days behind
        # the event because the gate's holdout was withheld from the fit.
        "fit_through_date": max(
            str(case["event_date"])[:10] for case in cases
        ),
        "fit_case_count": len(cases),
    }


def _level_bin(mean: float, cuts: Sequence[float]) -> int:
    """Which forecast-level bin a centre falls in, lowest first."""

    for index, cut in enumerate(cuts):
        if float(mean) <= float(cut):
            return index
    return len(cuts)


def _resolved_bias_c(
    mean: float,
    parameters: Mapping[str, Any],
) -> float:
    """The shift this centre actually earns, whatever form fitted it.

    One resolver for the evaluator and the server -- the affine incident was
    exactly these two disagreeing -- so the form gate lives here: a
    parameters payload naming a form this build cannot run is refused, never
    silently served as the constant.
    """

    form = str(parameters.get("correction_form") or "constant")
    if form == "constant":
        return float(parameters["bias_c"])
    if form == BIAS_CORRECTION_FORM:
        cuts = [float(cut) for cut in parameters["bias_bin_cuts_c"]]
        shifts = [
            float(shift) for shift in parameters["bias_bin_shifts_c"]
        ]
        if not shifts or len(shifts) != len(cuts) + 1:
            raise ContractError(
                "native_ensemble_oos_binned_bias_malformed"
            )
        return shifts[_level_bin(float(mean), cuts)]
    raise ContractError(
        "native_ensemble_oos_correction_form_unsupported"
    )


def _corrected_center(
    mean: float,
    parameters: Mapping[str, Any],
) -> float:
    """Where the calibrated distribution centers.

    Routed through ``_resolved_bias_c``, the same resolver
    ``native_ensemble_p0._calibrated_system_probability`` runs at serving
    time.  Keep the two on one function -- an evaluator that centers
    differently from the server hands out certificates it did not earn.
    """

    return mean + _resolved_bias_c(mean, parameters)


def _apply_parameters(
    values: Sequence[float],
    parameters: Mapping[str, Any],
    intervals: Mapping[str, Sequence[float | None]] | None = None,
) -> dict[str, float]:
    if intervals is not None:
        return _apply_parameters_on_ladder(values, parameters, intervals)
    current = [float(value) for value in values]
    mean = sum(current) / len(current)
    center = _corrected_center(mean, parameters)
    spread_factor = float(parameters["spread_factor"])
    corrected = [
        center + spread_factor * (value - mean)
        for value in current
    ]
    # A single-member system (a point forecast) has no member spread for the
    # spread factor to inflate, so the default kernel would serve a delta of
    # width 0.5C regardless of how wrong the point tends to be.  Its honest
    # width is the training residual: dress the lone member with the fitted
    # RMSE instead.  Strictly one member -- a plural ensemble that happens to
    # collapse to identical values keeps the plain kernel, exactly as before.
    member_bandwidth = (
        max(
            KERNEL_BANDWIDTH_C,
            float(
                parameters.get("train_residual_sigma_c")
                or parameters.get("train_rmse_c")
                or 0.0
            ),
        )
        if len(current) == 1
        else KERNEL_BANDWIDTH_C
    )
    member_probability = _probabilities(
        corrected, bandwidth_c=member_bandwidth
    )
    climate_probability = _probabilities([
        float(value)
        for value in parameters["climate_samples_c"]
    ])
    reliability = float(parameters["reliability_weight"])
    keys = set(member_probability) | set(climate_probability)
    result = {
        key: (
            reliability * member_probability.get(key, 0.0)
            + (1.0 - reliability)
            * climate_probability.get(key, 0.0)
        )
        for key in keys
    }
    total = sum(result.values())
    if total <= 0:
        raise ContractError("native_ensemble_oos_probability_empty")
    return {
        key: value / total
        for key, value in sorted(
            result.items(),
            key=lambda item: int(item[0]),
        )
    }


def _apply_parameters_on_ladder(
    values: Sequence[float],
    parameters: Mapping[str, Any],
    intervals: Mapping[str, Sequence[float | None]],
) -> dict[str, float]:
    """One system's calibrated distribution on the day's market ladder.

    The serving function itself (``native_ensemble_p0.
    _calibrated_system_probability``), handed this artifact's flat
    parameters in the three parts it reads, so the holdout certifies the
    arithmetic P0 serves on that ladder.
    """

    from .native_ensemble_p0 import _calibrated_system_probability  # noqa: PLC0415

    return _calibrated_system_probability(
        values=[float(value) for value in values],
        intervals=intervals,
        bias_parameters=parameters,
        spread_parameters=parameters,
        reliability_parameters=parameters,
    )


def _ladder_climate_probability(
    climate_samples_c: Sequence[float],
    ladder: Mapping[str, Any],
) -> dict[str, float]:
    """Climatology on the ladder, dressed by the kernel serving uses."""

    from .native_ensemble_p0 import _interval_probability  # noqa: PLC0415

    return _interval_probability(
        [float(value) for value in climate_samples_c], ladder["intervals"]
    )


def _cycle_mapping_for_rows(
    rows: Sequence[tuple[str, str, Mapping[str, Any]]],
) -> tuple[dict[str, str], str, str, str]:
    """Resolve one deterministic cycle identity for a cross candidate.

    A system may contribute only one native cycle to a candidate.  The
    returned mapping is the identity used by downstream activation lookup;
    it is never replaced by the cross label.
    """

    by_system: dict[str, set[str]] = defaultdict(set)
    for system_id, run_cycle, _evaluation in rows:
        system = str(system_id or "").strip()
        cycle = str(run_cycle or "").strip()
        if not system or cycle not in VALID_NATIVE_RUN_CYCLES:
            raise ContractError(
                "native_ensemble_oos_run_cycle_mapping_missing"
            )
        by_system[system].add(cycle)
    conflicts = sorted(
        system_id
        for system_id, cycles in by_system.items()
        if len(cycles) != 1
    )
    if conflicts:
        raise ContractError(
            "native_ensemble_oos_system_cycle_conflict"
        )
    mapping = {
        system_id: next(iter(cycles))
        for system_id, cycles in sorted(by_system.items())
    }
    if not mapping:
        raise ContractError(
            "native_ensemble_oos_run_cycle_mapping_missing"
        )
    mapping_sha = content_sha256(mapping)
    cycles = set(mapping.values())
    if len(cycles) == 1:
        run_cycle = next(iter(cycles))
        run_cycle_scope = "SAME_NATIVE_CYCLE"
    else:
        run_cycle = MIXED_NATIVE_CYCLES
        run_cycle_scope = "PER_SYSTEM_NATIVE_CYCLE"
    return mapping, mapping_sha, run_cycle, run_cycle_scope


def _candidate_cycle_mapping(candidate: Mapping[str, Any]) -> tuple[
    dict[str, str], str, str, str
]:
    raw_mapping = candidate.get("run_cycle_by_system")
    if not isinstance(raw_mapping, Mapping):
        raise ContractError(
            "native_ensemble_oos_run_cycle_mapping_missing"
        )
    rows = [
        (
            str(system_id),
            str(run_cycle),
            {},
        )
        for system_id, run_cycle in raw_mapping.items()
    ]
    mapping, mapping_sha, run_cycle, run_cycle_scope = (
        _cycle_mapping_for_rows(rows)
    )
    selected = sorted(
        str(system_id)
        for system_id in candidate.get("selected_system_ids") or []
    )
    if selected != sorted(mapping):
        raise ContractError(
            "native_ensemble_oos_run_cycle_mapping_system_mismatch"
        )
    if str(candidate.get("run_cycle") or "") != run_cycle:
        raise ContractError(
            "native_ensemble_oos_run_cycle_mapping_label_mismatch"
        )
    if str(candidate.get("run_cycle_scope") or "") != run_cycle_scope:
        raise ContractError(
            "native_ensemble_oos_run_cycle_scope_mismatch"
        )
    if str(candidate.get("run_cycle_by_system_sha256") or "") != (
        mapping_sha
    ):
        raise ContractError(
            "native_ensemble_oos_run_cycle_mapping_hash_mismatch"
        )
    return mapping, mapping_sha, run_cycle, run_cycle_scope


def _correlation(
    left: Sequence[float],
    right: Sequence[float],
) -> float:
    if len(left) != len(right) or len(left) < 2:
        return 0.0
    left_mean = sum(left) / len(left)
    right_mean = sum(right) / len(right)
    numerator = sum(
        (a - left_mean) * (b - right_mean)
        for a, b in zip(left, right)
    )
    left_scale = math.sqrt(sum(
        (value - left_mean) ** 2 for value in left
    ))
    right_scale = math.sqrt(sum(
        (value - right_mean) ** 2 for value in right
    ))
    if left_scale <= EPS or right_scale <= EPS:
        return 0.0
    return max(-1.0, min(1.0, numerator / (left_scale * right_scale)))


def _weight_maturity(train_n: int | None) -> float:
    """How much of its earned weight a system may hold at ``train_n`` days.

    ``sqrt(n / n_full)``, capped at one, because the standard error of the
    fitted RMSE shrinks as ``1/sqrt(n)`` -- the share we can justify tracks how
    well the error is pinned down.  ``None`` means the caller could not report
    a count, which is treated as mature so this never silently demotes the
    established systems on a path that has not been taught to pass it.
    """

    if train_n is None:
        return 1.0
    if train_n <= 0:
        return 0.0
    return math.sqrt(
        min(1.0, float(train_n) / float(WEIGHT_MATURITY_FULL_TRAIN_DAYS))
    )


def _nearest_key(ordered_keys: Sequence[str], value: float) -> str:
    """The integer-labelled bucket a temperature rounds to."""

    labels = [float(int(key)) for key in ordered_keys]
    best = min(range(len(labels)), key=lambda i: abs(labels[i] - value))
    return str(ordered_keys[best])


def _shift_bucketed_probability(
    probability: Mapping[str, float],
    *,
    ordered_keys: Sequence[str],
    shift_c: float,
) -> dict[str, float]:
    """Move a bucketed distribution along the temperature axis.

    The holdout's keys are integer degree labels, so a bucket's centre after
    the shift falls between two of them and its mass splits linearly.  Same
    transform the serving path applies; the edges absorb what leaves them.
    """

    labels = [float(int(key)) for key in ordered_keys]
    shifted = {str(key): 0.0 for key in ordered_keys}
    for index, key in enumerate(ordered_keys):
        mass = float(probability[str(key)])
        if mass <= 0.0:
            continue
        target = labels[index] + shift_c
        if target <= labels[0]:
            shifted[str(ordered_keys[0])] += mass
            continue
        if target >= labels[-1]:
            shifted[str(ordered_keys[-1])] += mass
            continue
        position = 0
        while (
            position + 1 < len(labels) and labels[position + 1] <= target
        ):
            position += 1
        span = labels[position + 1] - labels[position]
        weight = (target - labels[position]) / span if span > 0 else 0.0
        shifted[str(ordered_keys[position])] += mass * (1.0 - weight)
        shifted[str(ordered_keys[position + 1])] += mass * weight
    total = sum(shifted.values())
    if total <= 0.0:
        raise ContractError("native_ensemble_oos_shift_probability_empty")
    return {key: value / total for key, value in shifted.items()}


def _holdout_conditioning_signals(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    holdout_days: Sequence[str],
    predictions_by_system: Mapping[str, Mapping[str, Mapping[str, Any]]],
    radiation_shift_sigma: float = RADIATION_SHIFT_SIGMA,
) -> dict[str, dict[str, float | None]]:
    """Per-day shift signal the holdout's shape stack scores.

    One signal: the radiation shift, with the airport forecaster's cooling
    index as its fallback, resolved here from the archives so the holdout
    scores the same transform serving will apply, day for day.  The
    conditional tail skew's two signals -- same-calendar-day climatology
    headroom and the TAF TX gap -- were read here until 2026-09-22 and fed
    nothing: the skew's tag left new artifacts on 2026-08-18 and the layer
    was deleted (charter 43 amendment), so the ten-year climatology window
    and the station's whole truth record are no longer read per call.

    Charter 172: the radiation shift is returned in sigmas (``synoptic_cooling_shift_sigma``)
    and scaled by the blended distribution's own spread inside
    ``_holdout_shape_stack``, exactly as serving scales it.  The day's radiation
    is the version known at the day's cutoff -- the latest ``decision_at_utc``
    among the systems' predictions, when their members were in hand -- read
    from the version table (2026-09-21: the physics table itself is rewritten
    until each day's own evening).  A day whose radiation versions all came
    after its cutoff gets no shift and no TAF fallback: serving would have
    shifted it by radiation, so the holdout does not stand in another signal.
    """

    centers_by_day: dict[str, list[float]] = {}
    cutoff_by_day: dict[str, datetime] = {}
    for day_rows in predictions_by_system.values():
        for day, row in day_rows.items():
            center = row.get("calibrated_center_c")
            if center is None:
                continue
            centers_by_day.setdefault(str(day), []).append(float(center))
            try:
                decided = datetime.fromisoformat(
                    str(row.get("decision_at_utc") or "").replace("Z", "+00:00")
                )
            except ValueError:
                continue
            if decided.tzinfo is None:
                continue
            decided = decided.astimezone(timezone.utc)
            if str(day) not in cutoff_by_day or decided > cutoff_by_day[str(day)]:
                cutoff_by_day[str(day)] = decided
    try:
        station_latitude = float(
            station_coordinates(station_id)["latitude"]
        )
    except Exception:  # noqa: BLE001 -- unknown latitude is neutral
        station_latitude = None
    # Radiation only, the same precedence the serving path applies: the
    # clearness index, then the absolute form, then no shift.  The
    # forecaster's fields are no stand-in (charter 43, 2026-09-23).
    radiation_by_day: dict[str, float] = {}
    radiation_history: list[float] = []
    try:
        for local_date, radiation in conn.execute(
            "SELECT local_date,shortwave_radiation_sum "
            "FROM calibration_station_forecast_physics "
            "WHERE station_id=? AND reference_only=1 "
            "AND shortwave_radiation_sum IS NOT NULL",
            (station_id,),
        ):
            radiation_by_day[str(local_date)] = float(radiation)
            radiation_history.append(float(radiation))
    except sqlite3.Error:
        radiation_by_day = {}
        radiation_history = []
    signals: dict[str, dict[str, float | None]] = {}
    for day in holdout_days:
        key = str(day)
        centers = centers_by_day.get(key) or []
        if not centers:
            continue
        cutoff = cutoff_by_day.get(key)
        day_radiation, as_of_receipt = resolve_radiation_as_of(
            conn,
            station_id=station_id,
            local_date=key,
            cutoff_utc=(
                cutoff.isoformat().replace("+00:00", "Z") if cutoff else None
            ),
        )
        radiation_state = str(as_of_receipt.get("state"))
        # Explicit rather than defaulted: this is the coefficient the
        # artifact will pin and serving will read back, and a default here
        # would let the two drift the moment the constant moves.
        # Charter 160: scored on the clearness index over the station's days
        # before this one, falling back to the absolute form exactly where
        # serving does (no latitude, or under thirty days of history).
        shift_sigma, _radiation_receipt = forecast_clearness_shift_sigma(
            shift_sigma_per_unit=radiation_shift_sigma,
            radiation_sum=day_radiation,
            event_date=key,
            latitude_deg=station_latitude,
            station_history=[
                (other, value)
                for other, value in radiation_by_day.items()
                if other < key
            ],
        )
        if shift_sigma is None:
            shift_sigma, _radiation_receipt = forecast_radiation_shift_sigma(
                shift_sigma_per_unit=radiation_shift_sigma,
                radiation_sum=day_radiation,
                station_history=[
                    value
                    for other, value in radiation_by_day.items()
                    if other != key
                ],
            )
        # A day with neither radiation form is left unshifted, exactly as
        # serving leaves it.
        if shift_sigma is None:
            continue
        signals[key] = {
            "synoptic_cooling_shift_sigma": shift_sigma,
            "radiation_as_of_state": radiation_state,
        }
    return signals


def _holdout_shape_stack(
    base: Mapping[str, float],
    *,
    grid: Sequence[str],
    cooling_shift_sigma: float | None,
    station_latitude: float | None,
    neighbour_center_c: float,
    center_width_layer: CenterWidthLayer | None,
    band_layer: BandLayer | None,
    skip: str | None = None,
    ladder: Mapping[str, Any] | None = None,
) -> dict[str, float]:
    """The P0 shape stack on the holdout's grid.

    With ``ladder`` (charter 211 (六)) the grid is the day's market ladder
    and each layer runs the way P0 serving runs it there: the radiation
    shift moves mass along the served axis (``_shift_probability_c``), the
    neighbour lean centres on the rung the models' centre falls in, and the
    width layer is handed the open ends' bounds (charter 190).  Without it,
    the integer-degree grid below.

    One function for the subset contest, the gate and its ablation, so the
    blend that wins a seat, the blend that is certified and the blend that is
    served are shaped by the same layers in the same order.  Position first
    (radiation shift), then the neighbour lean around the models' own centre,
    then the two fitted tables in ``served_shape``'s order: centre/width,
    band.

    Until 2026-09-13 the contest shaped without centre/width, the gate
    applied band before centre/width, and both anchored the lean on the
    shifted histogram's mean while serving anchored it on the weighted
    calibrated centres.  Each was a different arithmetic from the one an
    artifact is served under.
    """

    shaped = {str(key): float(value) for key, value in base.items()}
    if ladder is not None:
        return _ladder_shape_stack(
            shaped,
            ladder=ladder,
            cooling_shift_sigma=cooling_shift_sigma,
            station_latitude=station_latitude,
            neighbour_center_c=neighbour_center_c,
            center_width_layer=center_width_layer,
            band_layer=band_layer,
            skip=skip,
        )
    if cooling_shift_sigma and skip != "synoptic_cooling":
        # In the blended distribution's own spread, as serving scales it
        # (``distribution_sigma_c`` is the one function both call).
        spread_c = distribution_sigma_c(
            [shaped[str(key)] for key in grid],
            [float(int(key)) for key in grid],
        )
        if spread_c > 0.0:
            shaped = _shift_bucketed_probability(
                shaped,
                ordered_keys=grid,
                shift_c=float(cooling_shift_sigma) * spread_c,
            )
    if skip != "neighbour_direction":
        shaped, _direction_receipt = apply_neighbour_direction_tilt(
            shaped,
            ordinals={str(key): index for index, key in enumerate(grid)},
            center_bucket=_nearest_key(grid, float(neighbour_center_c)),
            latitude=station_latitude,
        )
    shaped, _cw_receipt, _band_receipt = apply_served_table_layers(
        shaped,
        ordered_keys=grid,
        key_centers_c=[float(int(key)) for key in grid],
        center_width=center_width_layer,
        band=band_layer,
        skip=skip if skip in SERVED_TABLE_LAYER_ORDER else None,
    )
    return shaped


def _ladder_shape_stack(
    shaped: Mapping[str, float],
    *,
    ladder: Mapping[str, Any],
    cooling_shift_sigma: float | None,
    station_latitude: float | None,
    neighbour_center_c: float,
    center_width_layer: CenterWidthLayer | None,
    band_layer: BandLayer | None,
    skip: str | None,
) -> dict[str, float]:
    """``_holdout_shape_stack`` on the market ladder, in serving's order."""

    from .native_ensemble_p0 import _shift_probability_c  # noqa: PLC0415

    grid = list(ladder["ordered"])
    centers = list(ladder["centers_c"])
    shaped = {key: float(shaped.get(key, 0.0)) for key in grid}
    if cooling_shift_sigma and skip != "synoptic_cooling":
        spread_c = distribution_sigma_c(
            [shaped[key] for key in grid], centers,
        )
        shift_c = float(cooling_shift_sigma) * spread_c
        if spread_c > 0.0 and abs(shift_c) > EPS:
            shaped = _shift_probability_c(
                shaped,
                ordered=grid,
                bucket_centers_c=[value + shift_c for value in centers],
                intervals=ladder["intervals"],
            )
    if skip != "neighbour_direction":
        shaped, _direction_receipt = apply_neighbour_direction_tilt(
            shaped,
            ordinals={key: index for index, key in enumerate(grid)},
            center_bucket=_ladder_bucket(ladder, float(neighbour_center_c)),
            latitude=station_latitude,
        )
    shaped, _cw_receipt, _band_receipt = apply_served_table_layers(
        shaped,
        ordered_keys=grid,
        key_centers_c=centers,
        center_width=center_width_layer,
        band=band_layer,
        open_edge_bounds_c=ladder["open_edge_bounds_c"],
        skip=skip if skip in SERVED_TABLE_LAYER_ORDER else None,
    )
    return shaped


def _day_grid(
    predictions_by_system: Mapping[str, Mapping[str, Mapping[str, Any]]],
    systems: Sequence[str],
    day: str,
    ladder: Mapping[str, Any] | None,
) -> tuple[list[str], list[float]]:
    """The grid a holdout day is blended on, and each key's centre."""

    if ladder is not None:
        return list(ladder["ordered"]), list(ladder["centers_c"])
    keys = sorted(
        set().union(*(
            set(predictions_by_system[system][day]["probability"])
            for system in systems
        )),
        key=lambda key: int(key),
    )
    return keys, [float(int(key)) for key in keys]


def _day_ladder(
    predictions_by_system: Mapping[str, Mapping[str, Mapping[str, Any]]],
    systems: Sequence[str],
    day: str,
    ladder_for: Callable[[str], Mapping[str, Any] | None] | None,
) -> Mapping[str, Any] | None:
    """The day's ladder when every system's prediction was dressed on it."""

    if ladder_for is None:
        return None
    if not all(
        predictions_by_system[system][day].get("holdout_grid")
        == HOLDOUT_GRID_MARKET_LADDER
        for system in systems
    ):
        return None
    return ladder_for(str(day))


def _select_minimum_crps_subset(
    *,
    ready_system_ids: Sequence[str],
    predictions_by_system: Mapping[str, Mapping[str, Mapping[str, Any]]],
    common_holdout: Sequence[str],
    family_by_system: Mapping[str, str],
    configured_caps: Mapping[str, float],
    train_error_scale: Mapping[str, float],
    train_n_by_system: Mapping[str, int],
    residuals_by_system: Mapping[str, Mapping[str, float]] = {},
    conditioning_by_day: Mapping[str, Mapping[str, float | None]] = {},
    station_latitude: float | None = None,
    band_factors: Sequence[tuple[tuple[float, float], float]] | None = None,
    # Charter 105: the axis travels with the factors.  A holdout that scores
    # a distance-fitted table through the claim lookup gates it on math
    # production will not serve.
    band_axis: str = BAND_AXIS_CLAIM,
    # The centre/width layer the gate scores and serving applies.  The
    # contest ranked blends without it until 2026-09-13, so the seat went to
    # the blend that scored best under an arithmetic nobody serves.
    center_width_layer: CenterWidthLayer | None = None,
    ladder_for: Callable[[str], Mapping[str, Any] | None] | None = None,
    # Charter 134: ``stable_pool`` divides a pinned set's weight by the skill
    # rule shrunk toward the family baseline, and the receipt says so.  Only
    # meaningful with ``pinned_subset``: a pool is seated, never contested.
    weight_mode: str = "skill",
    # Score exactly this subset and no other.  The contest still runs the
    # same holdout through the same served blend; only the enumeration is
    # narrowed, so a pinned subset is gated on precisely the math a contested
    # one is.  See ``_served_cross_identities`` for why a caller pins.
    pinned_subset: Sequence[str] | None = None,
) -> tuple[dict[str, float], dict[str, Any]]:
    """Choose the subset of ready systems whose blend scores best out of sample.

    Every ready system used to enter the blend unconditionally, weighted by
    inverse RMSE -- admission was the only selection there was.  That is the
    wrong default for a blend: a mediocre correlated system dilutes a sharp
    pair even at a small weight, and admitting a family (which the forward
    window now does early, on thin records) should not be the same act as
    granting it a seat.  Here every combination spanning at least two
    independence families is scored on the exact shared holdout with exactly
    the bridged blend P0 would serve, and the seat goes to the combination
    with the best CRPS -- NLL, then Brier break ties, then fewer systems,
    because when the evidence cannot separate two blends the one carrying
    fewer moving parts is the one to trust.

    The contest READS the holdout it is certified on.  Parameters are never
    fitted on these days, but the choice of subset is, and the winner's
    scores are the gate's scores (``oos_metrics`` equals ``winner`` since
    the shape stacks were unified).  Measured 2026-09-13 over 1,148
    enumerating contests at three non-overlapping replay origins, 45
    stations (tmp/model_layer_fix_20260913/selection_bias): picked on the
    three shared days before the holdout instead, the winner changes 80% of
    the time and the certified CRPS is 35% optimistic [30.5%, 40.9%]
    station-clustered; 88 of 1,146 cohorts pass the gate only because the
    pick saw its labels; and seating the whole cohort scores within -1.0%
    [-3.2%, +1.4%] of the honest pick.  Whether the seat keeps being won
    this way is the 2026-08-15 membership ruling's to change, not this
    function's -- see the model-layer review, R2.
    """

    ordered = sorted(ready_system_ids)
    if len(ordered) > 8:
        raise ContractError("cross_native_ensemble_subset_space_too_large")
    if weight_mode not in {"skill", P0_MEMBERSHIP_MODE_STABLE_POOL}:
        raise ContractError("cross_native_ensemble_weight_mode_unknown")
    if weight_mode == P0_MEMBERSHIP_MODE_STABLE_POOL and pinned_subset is None:
        raise ContractError("cross_native_ensemble_stable_pool_requires_pinned_set")
    if pinned_subset is not None:
        pinned = sorted(str(value) for value in pinned_subset)
        if not pinned or not set(pinned).issubset(ordered):
            raise ContractError("cross_native_ensemble_pinned_subset_not_ready")
        masks: Sequence[int] = [
            sum(1 << index for index, system_id in enumerate(ordered)
                if system_id in set(pinned))
        ]
    else:
        masks = range(1, 1 << len(ordered))
    scored: list[dict[str, Any]] = []
    best: tuple[Any, ...] | None = None
    best_weights: dict[str, float] | None = None
    best_weight_rule = SEAT_WEIGHT_RULE_INVERSE_RMSE
    best_skill_rule = SEAT_WEIGHT_RULE_INVERSE_RMSE
    best_entry: dict[str, Any] | None = None
    for mask in masks:
        subset = [
            ordered[index]
            for index in range(len(ordered))
            if mask & (1 << index)
        ]
        if len({family_by_system[s] for s in subset}) < 2:
            continue
        try:
            # The contest must weigh each candidate the way that candidate
            # would actually be served, or it is ranking blends nobody runs.
            weights, weight_rule = _seat_weights(
                residuals_by_system={
                    s: residuals_by_system.get(s, {}) for s in subset
                },
                train_error_scale_by_system={
                    s: train_error_scale[s] for s in subset
                },
                family_by_system={
                    s: family_by_system[s] for s in subset
                },
                family_caps=configured_caps,
                train_n_by_system={
                    s: train_n_by_system.get(s, 0) for s in subset
                },
            )
        except ContractError:
            continue
        skill_rule = weight_rule
        if weight_mode == P0_MEMBERSHIP_MODE_STABLE_POOL:
            weights = stable_pool_weights(
                {s: float(weights.get(s, 0.0)) for s in subset},
                {s: family_by_system[s] for s in subset},
                kappa=STABLE_POOL_KAPPA,
            )
            weight_rule = STABLE_POOL_WEIGHT_RULE.upper()
        day_metrics = []
        for day in common_holdout:
            day_ladder = _day_ladder(
                predictions_by_system, subset, day, ladder_for
            )
            ordered_keys, key_centers = _day_grid(
                predictions_by_system, subset, day, day_ladder
            )
            probability, _receipt = bridged_cross_probability(
                system_probabilities={
                    s: predictions_by_system[s][day]["probability"]
                    for s in subset
                },
                system_weights=weights,
                system_centers_c={
                    s: float(
                        predictions_by_system[s][day]["calibrated_center_c"]
                    )
                    for s in subset
                },
                ordered_keys=ordered_keys,
                key_centers_c=key_centers,
            )
            # Only the radiation shift reads these now.  The conditional tail
            # skew was withdrawn 2026-08-18: measured against its own absence
            # over 1712 evaluations it cost 3.0% CRPS, won on 17% of scopes,
            # and was net negative at every lead and at 36 of 45 stations --
            # no pocket of the network it was helping.  The mechanism was not
            # the problem; overlapping with the band table was.  It lifted
            # buckets below 5% into the 2-12% band the band table then
            # multiplies by 0.84, so two thirds of every lift was undone, and
            # each layer had been fitted with the other held still as
            # background.  Serving kept applying it to artifacts gated with the
            # tag until the last one's window closed (2026-09-17); the branch
            # was deleted 2026-09-22 (charter 43 amendment).
            # Charter 105: the band lookup follows the table's own axis, and
            # the holdout scores it through the lookup production serves.
            probability = _holdout_shape_stack(
                probability,
                grid=ordered_keys,
                cooling_shift_sigma=(
                    conditioning_by_day.get(str(day)) or {}
                ).get("synoptic_cooling_shift_sigma"),
                station_latitude=station_latitude,
                neighbour_center_c=served_model_center_c(
                    weights,
                    {
                        s: float(
                            predictions_by_system[s][day][
                                "calibrated_center_c"
                            ]
                        )
                        for s in subset
                    },
                ),
                center_width_layer=center_width_layer,
                band_layer=(
                    BandLayer(factors=band_factors, axis=band_axis)
                    if band_factors is not None else None
                ),
                ladder=day_ladder,
            )
            actual = float(
                predictions_by_system[subset[0]][day]["actual_tmax_c"]
            )
            day_metrics.append(_metrics(probability, actual, day_ladder))
        mean = _mean_metrics(day_metrics)
        entry = {
            "system_ids": subset,
            "crps": mean["crps"],
            "nll": mean["nll"],
            "brier": mean["brier"],
        }
        scored.append(entry)
        rank = (
            round(float(mean["crps"]), 12),
            round(float(mean["nll"]), 12),
            round(float(mean["brier"]), 12),
            len(subset),
            tuple(subset),
        )
        if best is None or rank < best:
            best = rank
            best_weights = weights
            best_weight_rule = weight_rule
            best_skill_rule = skill_rule
            best_entry = entry
    if best_weights is None or best_entry is None:
        raise ContractError("cross_native_ensemble_no_scorable_subset")
    scored.sort(key=lambda row: (
        round(float(row["crps"]), 12),
        round(float(row["nll"]), 12),
        len(row["system_ids"]),
    ))
    receipt = {
        "rule": (
            "STABLE_POOL_SEAT_V1"
            if weight_mode == P0_MEMBERSHIP_MODE_STABLE_POOL
            else "SERVED_SET_REFIT_V1" if pinned_subset is not None
            else "MIN_CRPS_EXACT_SHARED_HOLDOUT_SUBSET_V1"
        ),
        # Which rule divided the winner's weight.  A scope that fell back to
        # inverse RMSE looks identical in the weights alone, and the reason it
        # fell back -- too few shared days for a covariance -- is exactly what
        # a reader chasing a stale-looking blend needs to see.
        "weight_rule": best_weight_rule,
        **(
            {
                "skill_weight_rule": best_skill_rule,
                "stable_pool_kappa": STABLE_POOL_KAPPA,
            }
            if weight_mode == P0_MEMBERSHIP_MODE_STABLE_POOL else {}
        ),
        "evaluated_subset_count": len(scored),
        "winner": best_entry,
        "leaderboard": scored[:8],
    }
    return best_weights, receipt


# A covariance wants many more days than systems.  Twelve is the floor at
# which the diagonal is worth trusting at all; below it the scheme declines to
# have an opinion rather than returning a confident wrong one.
MINIMUM_VARIANCE_MIN_DAYS = 12
# Half weight on the measured correlations.  The diagonal of a residual
# covariance is pinned down by a few dozen days; the off-diagonal is not, and
# an unshrunk inverse turns that noise into large offsetting weights -- the
# classic failure of a sample minimum-variance portfolio.  Shrinking toward
# the diagonal degrades gracefully to inverse-variance weighting, so the
# shadow can also be run at 1.0 to ask whether the correlations earned
# anything the variances did not.
MINIMUM_VARIANCE_SHRINKAGE = 0.5


def _solve_symmetric(
    matrix: list[list[float]],
    vector: list[float],
) -> list[float] | None:
    """Gaussian elimination with partial pivoting; None if near-singular."""

    size = len(vector)
    augmented = [list(matrix[row]) + [vector[row]] for row in range(size)]
    for column in range(size):
        pivot = max(
            range(column, size),
            key=lambda row: abs(augmented[row][column]),
        )
        if abs(augmented[pivot][column]) < 1e-12:
            return None
        augmented[column], augmented[pivot] = (
            augmented[pivot], augmented[column]
        )
        for row in range(size):
            if row == column:
                continue
            factor = augmented[row][column] / augmented[column][column]
            for term in range(column, size + 1):
                augmented[row][term] -= factor * augmented[column][term]
    return [
        augmented[row][size] / augmented[row][row] for row in range(size)
    ]


def _minimum_variance_weights(
    *,
    residuals_by_system: Mapping[str, Mapping[str, float]],
    family_by_system: Mapping[str, str],
    family_caps: Mapping[str, float],
    shrinkage: float = MINIMUM_VARIANCE_SHRINKAGE,
) -> dict[str, float] | None:
    """Weights minimising the combined residual variance, not each system's own.

    Inverse RMSE asks only how good each system is alone, so two systems that
    make the same mistake are paid twice for one piece of information.  The
    combination that actually minimises error variance is ``inv(S) 1``
    normalised, and the whole of its advantage lives in the off-diagonal --
    which is also the part a month of days estimates worst, hence the
    shrinkage above.

    Negative weights are refused rather than clipped after the fact: a blend
    is a mixture of distributions and a negative share has no meaning in it.
    The constrained optimum is found by enumerating which systems sit at
    zero -- sound because the contract already bounds the cohort at eight
    systems, so the active set is at most 255 candidates.

    Returns None when the sample cannot support the estimate, which the
    caller reads as "this scheme has no opinion here" rather than as a
    failure.
    """

    systems = sorted(residuals_by_system)
    if len(systems) < 2:
        return None
    days = sorted(set.intersection(*(
        set(residuals_by_system[system]) for system in systems
    )))
    if len(days) < MINIMUM_VARIANCE_MIN_DAYS:
        return None
    # Centered: the per-system bias is corrected upstream, so what the blend
    # still has to fight is the variation around each system's own mean.
    means = {
        system: sum(
            residuals_by_system[system][day] for day in days
        ) / len(days)
        for system in systems
    }
    centered = {
        system: [
            residuals_by_system[system][day] - means[system]
            for day in days
        ]
        for system in systems
    }
    size = len(systems)
    covariance = [[0.0] * size for _ in range(size)]
    for row, left in enumerate(systems):
        for column, right in enumerate(systems):
            covariance[row][column] = sum(
                centered[left][index] * centered[right][index]
                for index in range(len(days))
            ) / float(len(days))
    if any(covariance[index][index] <= 1e-9 for index in range(size)):
        return None
    for row in range(size):
        for column in range(size):
            if row != column:
                covariance[row][column] *= 1.0 - shrinkage

    best: tuple[float, dict[str, float]] | None = None
    for mask in range(1, 1 << size):
        active = [index for index in range(size) if mask & (1 << index)]
        sub = [
            [covariance[row][column] for column in active]
            for row in active
        ]
        solved = _solve_symmetric(sub, [1.0] * len(active))
        if solved is None:
            continue
        total = sum(solved)
        if abs(total) < 1e-12:
            continue
        shares = [value / total for value in solved]
        if any(share < -1e-9 for share in shares):
            continue
        objective = 0.0
        for row in range(len(active)):
            for column in range(len(active)):
                objective += shares[row] * sub[row][column] * shares[column]
        if objective <= 0.0:
            continue
        if best is None or objective < best[0] - 1e-15:
            best = (
                objective,
                {
                    systems[active[index]]: max(shares[index], 0.0)
                    for index in range(len(active))
                },
            )
    if best is None:
        return None
    raw = best[1]
    # The served blend must span two independence families.  Where the
    # unconstrained-but-non-negative optimum does not, the honest report is
    # that this scheme wants a blend the serving contract refuses, not a
    # patched-up version of it: forcing a family back in at some floor would
    # be inventing a weight no criterion chose.
    if len({family_by_system[system] for system in raw}) < 2:
        return None
    family_totals: dict[str, float] = defaultdict(float)
    for system, share in raw.items():
        family_totals[family_by_system[system]] += share
    capped = _capped_family_weights(
        dict(family_totals),
        {
            family: float(family_caps.get(family, 0.75))
            for family in family_totals
        },
    )
    weights: dict[str, float] = {}
    for system, share in raw.items():
        family = family_by_system[system]
        if family_totals[family] <= 0.0:
            continue
        weights[system] = capped[family] * share / family_totals[family]
    total = sum(weights.values())
    if total <= 0.0:
        return None
    return {system: value / total for system, value in weights.items()}


def _capped_family_weights(
    family_weights: Mapping[str, float],
    caps: Mapping[str, float],
) -> dict[str, float]:
    """Hold every family under its cap, spilling the excess into the rest.

    Shared by every weighting scheme so a candidate scheme faces exactly the
    concentration limit the served one does: a comparison where one side may
    put 90% on a single family and the other may not is a comparison of
    constraints, not of schemes.
    """

    families = sorted(family_weights)
    weights = dict(family_weights)
    if sum(caps.values()) < 1.0 - EPS:
        raise ContractError("cross_native_ensemble_family_caps_infeasible")
    for _iteration in range(len(families) + 2):
        over = [
            family
            for family in families
            if weights[family] > caps[family] + EPS
        ]
        if not over:
            break
        excess = 0.0
        for family in over:
            excess += weights[family] - caps[family]
            weights[family] = caps[family]
        receivers = [
            family
            for family in families
            if family not in over
            and weights[family] < caps[family] - EPS
        ]
        capacity = sum(
            caps[family] - weights[family] for family in receivers
        )
        if not receivers or capacity + EPS < excess:
            raise ContractError(
                "cross_native_ensemble_family_caps_infeasible"
            )
        for family in receivers:
            weights[family] += excess * (
                (caps[family] - weights[family]) / capacity
            )
    total = sum(weights.values())
    if total <= 0.0:
        raise ContractError("cross_native_ensemble_family_caps_infeasible")
    return {family: value / total for family, value in weights.items()}


SEAT_WEIGHT_RULE_COVARIANCE = "MINIMUM_VARIANCE_SHRUNK_COVARIANCE_V1"
SEAT_WEIGHT_RULE_INVERSE_RMSE = "INVERSE_TRAIN_RMSE_MATURITY_RAMP_V1"


def _seat_weights(
    *,
    residuals_by_system: Mapping[str, Mapping[str, float]],
    train_error_scale_by_system: Mapping[str, float],
    family_by_system: Mapping[str, str],
    family_caps: Mapping[str, float],
    train_n_by_system: Mapping[str, int] | None = None,
) -> tuple[dict[str, float], str]:
    """How a seated cohort divides its weight, and under which rule.

    Inverse RMSE prices each system on how good it is alone, so systems making
    the same mistake are paid twice for one piece of information.  Where the
    train window supports a covariance, the weights come from minimising the
    combined error variance instead; where it does not, the older rule stands
    and says so on the receipt.

    Measured before promotion, as a shadow beside the served blend: minimum
    variance beat inverse RMSE by 0.018 bits over 471 station-days, and the
    advantage grew with the length of the train window -- estimation noise was
    the binding constraint, not absence of structure.  Roughly a fifth of that
    is the covariance proper; the rest is the exponent, inverse RMSE against
    inverse variance.

    Both branches face the same family caps, so this chooses a rule and never
    a concentration limit.
    """

    try:
        covariance_weights = _minimum_variance_weights(
            residuals_by_system=residuals_by_system,
            family_by_system=family_by_system,
            family_caps=family_caps,
        )
    except ContractError:
        covariance_weights = None
    if covariance_weights:
        return covariance_weights, SEAT_WEIGHT_RULE_COVARIANCE
    return (
        _bounded_family_weights(
            train_error_scale_by_system=train_error_scale_by_system,
            family_by_system=family_by_system,
            family_caps=family_caps,
            train_n_by_system=train_n_by_system,
        ),
        SEAT_WEIGHT_RULE_INVERSE_RMSE,
    )


def _bounded_family_weights(
    *,
    train_error_scale_by_system: Mapping[str, float],
    family_by_system: Mapping[str, str],
    family_caps: Mapping[str, float],
    train_n_by_system: Mapping[str, int] | None = None,
) -> dict[str, float]:
    """Weight inversely to each system's error scale, capped by family.

    The scale is the caller's to choose because two callers legitimately want
    different ones: the residual sigma about the corrected centre is what a
    served distribution actually has to cover, while the shadow that watches
    the rule this replaced must keep passing train RMSE or it stops being
    that rule.  Naming the argument for one of them, as ``train_rmse_by_system``
    did, is how a caller ends up passing a sigma to something that documents
    itself as an RMSE.
    """

    family_scores: dict[str, float] = defaultdict(float)
    system_scores: dict[str, float] = {}
    for system_id, rmse in train_error_scale_by_system.items():
        # Inverse RMSE is only as trustworthy as the RMSE behind it.  A system
        # admitted on a handful of days can post a flattering error by luck and
        # would then walk straight into a large share of the blend, which is
        # the failure mode that a high admission bar used to prevent by simply
        # keeping newcomers out entirely.  Shrinking the score toward zero on
        # small samples replaces that wall with a ramp: a system enters at a
        # fraction of the weight its raw error would earn and grows into its
        # full share as its record accumulates forward.
        score = 1.0 / max(float(rmse), 0.1)
        score *= _weight_maturity(
            (train_n_by_system or {}).get(system_id)
        )
        system_scores[system_id] = score
        family_scores[family_by_system[system_id]] += score
    families = sorted(family_scores)
    if len(families) < 2:
        raise ContractError("cross_native_ensemble_requires_two_families")
    raw_total = sum(family_scores.values())
    family_weights = {
        family: family_scores[family] / raw_total
        for family in families
    }
    caps = {
        family: float(family_caps.get(family, 0.75))
        for family in families
    }
    family_weights = _capped_family_weights(family_weights, caps)
    system_weights: dict[str, float] = {}
    for family in families:
        members = [
            system
            for system, candidate_family in family_by_system.items()
            if candidate_family == family
        ]
        family_score = sum(system_scores[system] for system in members)
        for system in members:
            system_weights[system] = (
                family_weights[family]
                * system_scores[system]
                / family_score
            )
    validate_cross_native_ensemble_weights(
        system_weights=system_weights,
        systems_by_id={
            system: {
                "independence_family_id": family_by_system[system],
            }
            for system in system_weights
        },
        family_weight_caps=caps,
    )
    return dict(sorted(system_weights.items()))


def _load_systems(
    conn: sqlite3.Connection,
) -> dict[str, dict[str, Any]]:
    systems = {
        str(row[0]): {
            "ensemble_system_id": str(row[0]),
            "canonical_name": str(row[1]),
            "independence_family_id": str(row[2]),
            "provider_id": str(row[3]),
            "expected_member_count": int(row[4]),
            "content_sha256": str(row[5]),
        }
        for row in conn.execute(
            "SELECT ensemble_system_id,canonical_name,"
            "independence_family_id,provider_id,"
            "expected_member_count,content_sha256 "
            "FROM calibration_native_ensemble_system"
        )
    }
    republisher_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND "
        "name='calibration_republisher_ensemble_product'"
    ).fetchone()
    if republisher_table is not None:
        for row in conn.execute(
            "SELECT ensemble_system_id,canonical_name,"
            "independence_family_id,upstream_provider_id,"
            "expected_member_count,content_sha256 "
            "FROM calibration_republisher_ensemble_product"
        ):
            # Native product registration wins if an official lane has
            # independently registered the same upstream system.  The
            # republisher row remains available to its own candidate queries
            # below, but cannot replace a native registry record.
            systems.setdefault(str(row[0]), {
                "ensemble_system_id": str(row[0]),
                "canonical_name": str(row[1]),
                "independence_family_id": str(row[2]),
                "provider_id": str(row[3]),
                "expected_member_count": int(row[4]),
                "content_sha256": str(row[5]),
            })
    for system_id, registration in POINT_FORECAST_SYSTEMS.items():
        # Code-registered point forecasts; the table's plural-member CHECK
        # stays authoritative for genuine ensembles.
        systems.setdefault(system_id, dict(registration))
    return systems


def _scope_contract_variants(
    official: Mapping[str, Any],
) -> tuple[Mapping[str, Any], ...]:
    """Normalise a scope entry into one contract per lead and cycle.

    An entry is either a single contract (the historical shape, one lead) or a
    ``leads`` mapping from lead bucket to its own cycle and extraction.  Both
    are accepted so registering a second lead for a system never requires
    rewriting the ones already in production.

    A lead's value is one contract or a list of them, because a family may be
    admitted at more than one cycle for the same lead.  Each cycle carries its
    own extraction version, so flattening them here widens what is admitted
    without letting two runs be mistaken for one another downstream.
    """

    leads = official.get("leads")
    if not isinstance(leads, Mapping):
        return (official,)
    variants: list[Mapping[str, Any]] = []
    for lead_bucket, entry in sorted(leads.items()):
        contracts = entry if isinstance(entry, Sequence) and not isinstance(
            entry, (str, bytes, Mapping)
        ) else [entry]
        for contract in contracts:
            if not isinstance(contract, Mapping):
                continue
            variants.append({
                "transport_provider_id": official["transport_provider_id"],
                "lead_bucket": str(lead_bucket),
                **contract,
            })
    return tuple(variants)


# One group yields at most this many cycle assignments.  The bound exists so
# a runaway registry cannot explode the enumeration; it must not bind for the
# live configuration, because the collapse fallback below keeps one
# best-evidenced cycle per system and that is exactly the freeze the cohort
# grouper was built to prevent (a serving key's cycle deduped away, its
# weight row aging while corrections advance).  Four ready native families at
# two admitted cycles each is sixteen; anything past that is a registry bug.
MAX_CYCLE_ASSIGNMENTS_PER_GROUP = 16


def _cycle_variant_groups(
    scope_groups: Mapping[
        tuple[str, str],
        Mapping[str, list[tuple[str, dict[str, Any]]]],
    ],
) -> list[tuple[tuple[str, str], dict[str, list[tuple[str, dict[str, Any]]]]]]:
    """Split each lead group into one entry per realizable cycle assignment.

    A system can reach the same lead-hour group from two cycles: an 18Z run two
    calendar days out and a 00Z run one day out are both 24-48h ahead of some
    station-days.  That is the grouping working as intended -- the hour bucket
    exists because the calendar label was not what predicted skill -- but a
    cross-ensemble artifact names one cycle per system.

    Collapsing to a single "best" variant looks tidy and breaks P0.  The
    activation lookup matches the artifact's ``run_cycle_by_system`` against
    the cycles the forward packet actually carries, so an artifact built at a
    cycle the packet does not use is unreachable.  Which cycle a packet carries
    depends on which capture P0 reads, and both are legitimate, so both need an
    artifact.

    Assignments are emitted in a fixed order -- systems sorted, cycles sorted
    within each system -- so the same evidence always produces the same
    artifacts.
    """

    expanded: list[
        tuple[tuple[str, str], dict[str, list[tuple[str, dict[str, Any]]]]]
    ] = []
    for scope_key, system_rows in sorted(scope_groups.items()):
        ordered_systems = sorted(system_rows)
        cycles_by_system = [
            sorted(system_rows[system_id], key=lambda scope: str(scope[0]))
            for system_id in ordered_systems
        ]
        assignment_count = 1
        for scopes in cycles_by_system:
            assignment_count *= max(1, len(scopes))
        if assignment_count > MAX_CYCLE_ASSIGNMENTS_PER_GROUP:
            # Keep only each system's best-evidenced cycle rather than dropping
            # the group.  Weight of evidence, never score: ranking these on OOS
            # metrics would be selecting on the holdout the artifact is about
            # to be judged against.
            cycles_by_system = [
                [
                    max(
                        scopes,
                        key=lambda scope: (
                            int(scope[1].get("holdout_n") or 0),
                            int(scope[1].get("train_n") or 0),
                        ),
                    )
                ]
                for scopes in cycles_by_system
            ]
        for assignment in product(*cycles_by_system):
            expanded.append((
                scope_key,
                {
                    system_id: [scope]
                    for system_id, scope in zip(ordered_systems, assignment)
                },
            ))
    return expanded


# How far back a scope's last committed decision may sit and still name the
# set this build refits.  A served scope decides many times a day; one that
# has not decided in three days is not being served, and its set is not owed
# a twin.  The bound is what lets the lookup seek the recency index instead of
# walking a station's whole history (0.7 ms against 110 ms, measured
# 2026-09-10 on KAUS).
SERVED_SET_LOOKBACK_DAYS = 3


def _served_cross_identities(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    as_of_utc: str,
) -> dict[tuple[str, str], tuple[tuple[str, ...], dict[str, str]]]:
    """The exact (system set, fitted cycle map) each scope is serving now.

    Why the build refits these beside the contest's own pick.  The serving
    sort prefers, before anything else, an activation whose system set equals
    the packet's and whose fitted cycles need no rider -- and a new fit can
    only satisfy both by being fitted on exactly today's packet.  The contest
    fits what it chooses, which is a pair 92-97% of the time (2026-08-24 to
    09-10, every day), under the cycles it saw.  So whenever an older row
    happens to match today's packet exactly it outranks every newer fit for
    its whole thirty-day window, and anything only newer fits carry -- the
    centre/width pin that reached the fleet on 2026-09-04, the band axis on
    09-07 -- never reaches that scope.  Measured 2026-09-10: 21 of 142 P0
    scopes were serving 08-25..09-03 rows without the width layer while
    tagged successors were minted for them daily; 15 because the contest had
    not chosen that set since, 6 because the tagged fit of that set carried
    riders.  The same mechanism held the band axis at 70% of the fleet.

    Refitting the served identity gives it a tagged twin.  The twin ties the
    old row on set and riders and beats it on freshness, so the pin arrives
    the next day without the sort, the contest or any membership changing.

    Keyed by the scope the compile actually served under -- ``season_bin``
    and ``training_lead_bucket`` from the lineage -- and read from the
    artifact the decision pinned, not the packet: the packet's cycles are
    today's capture, the artifact's are the pairing the calibration was
    fitted under, and it is the latter the sort compares.  Fails soft to
    empty on any store without the tables: a replay or a fixture is not a
    station with a served set.
    """

    try:
        as_of_instant = datetime.fromisoformat(
            as_of_utc.replace("Z", "+00:00")
        )
        since = (
            as_of_instant - timedelta(days=SERVED_SET_LOOKBACK_DAYS)
        ).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        # Bounded above too: a forward build has nothing after now, but a
        # replay at a past moment would otherwise read which set was served
        # after that moment (review 2026-09-13, R4).
        until = (
            as_of_instant + timedelta(milliseconds=1)
        ).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        rows = conn.execute(
            "SELECT lineage_json FROM basket_probability_envelope"
            " WHERE station_id=? AND stage='P0' AND state='COMMITTED'"
            " AND decision_at_utc>=? AND decision_at_utc<?"
            " ORDER BY decision_at_utc DESC",
            (str(station_id).strip().upper(), since, until),
        ).fetchall()
    except sqlite3.Error:
        return {}
    served: dict[tuple[str, str], tuple[tuple[str, ...], dict[str, str]]] = {}
    seen_scopes: set[tuple[str, str]] = set()
    for (lineage_json,) in rows:
        try:
            lineage = json.loads(str(lineage_json or "{}"))
        except ValueError:
            continue
        scope = (
            str(lineage.get("season_bin") or ""),
            str(lineage.get("training_lead_bucket") or ""),
        )
        if not all(scope) or scope in seen_scopes:
            continue
        seen_scopes.add(scope)
        artifact_sha = str(
            lineage.get("native_cross_ensemble_artifact_sha256") or ""
        )
        if not artifact_sha:
            continue
        try:
            artifact = conn.execute(
                "SELECT ensemble_system_ids_json, run_cycle_by_system_json"
                " FROM calibration_cross_native_ensemble_artifact"
                " WHERE content_sha256=?",
                (artifact_sha,),
            ).fetchone()
            if artifact is None:
                continue
            system_ids = tuple(sorted(
                str(value) for value in json.loads(str(artifact[0]))
            ))
            cycles = {
                str(key): str(value)
                for key, value in json.loads(str(artifact[1] or "{}")).items()
            }
        except (sqlite3.Error, ValueError, TypeError):
            continue
        if len(system_ids) >= 2 and set(cycles) == set(system_ids):
            served[scope] = (system_ids, cycles)
    return served


def _historical_transport_scope_allowed(
    *,
    system_id: str,
    transport_provider_id: str,
    lead_bucket: str,
    run_cycle: str,
    extraction_version: str,
) -> bool:
    """Admit only rows produced the way this system is read *today*.

    ``extraction_version`` is part of the scope because a change in how values
    are pulled from a run silently invalidates everything captured before it.
    ICON is the worked example: sampling moved from a 6-hour to a 3-hour grid
    after the 6-hour grid was measured to inject 0.32C of per-station noise that
    no bias correction can remove.  Rows from both grids describe the same model
    and pass every other check, so without this the training set would mix two
    incompatible measurement conventions and calibration would fit the average
    of them.

    Rows are dropped rather than migrated: the archive is append-only by
    trigger, and a superseded extraction cannot be recomputed from what was
    stored.  A convention change therefore costs the accumulated history, which
    is the honest price and the reason to make such changes early.
    """

    official = _admitted_scope_contract(system_id)
    if official is not None:
        # A system may be registered at several leads.  P0 follows the freshest
        # run available, so the same system supplies d2 two days out and d1 or
        # d0 nearer the event, each with its own cycle and extraction.  Matching
        # a single lead admitted only the longest one and discarded every later
        # model update.
        #
        # Admitting more leads does not blur them: each lead keeps a separate
        # contract, and OOS trains per (season_bin, lead_bucket), so weights are
        # never shared across leads.
        #
        # Captures stamp the per-station hour bucket since 2026-08-14, while
        # the contract registry keys its variants by the collector's calendar
        # lead.  An hour label cannot be compared to a calendar key -- it is a
        # property of the station's timezone, not of which run-relative day
        # the collector fetched -- so for hour-era rows the extraction version
        # is the lead statement: every registered variant's extraction names
        # its own (lead, cycle) convention, and matching it pins the contract
        # exactly as the calendar label used to.  Comparing the vocabularies
        # directly is what silently dropped every capture after the cutover
        # and froze the fleet's selection windows at 2026-08-14.
        hour_era = lead_bucket in {
            name for name, _upper in LEAD_HOUR_BUCKETS
        } | {LONGEST_LEAD_HOUR_BUCKET}
        for contract in _scope_contract_variants(official):
            expected_extraction = str(
                contract.get("extraction_version") or ""
            )
            if (
                transport_provider_id
                == contract["transport_provider_id"]
                and (hour_era or lead_bucket == contract["lead_bucket"])
                and run_cycle == contract["run_cycle"]
                and (
                    not expected_extraction
                    or extraction_version == expected_extraction
                )
            ):
                return True
        return False
    return transport_provider_id == "OPEN_METEO"


def _table_columns(
    conn: sqlite3.Connection,
    table_name: str,
) -> set[str]:
    """Return the columns for a known local table.

    Historical republisher contracts are introduced by an append-only SSOT
    migration.  The OOS reader must remain safe while an older database is
    being upgraded: an absent contract column/table means *not eligible*, not
    a permissive fallback.  Table names are internal constants in this
    module, so PRAGMA interpolation is bounded to those names.
    """

    return {
        str(row[1])
        for row in conn.execute(f"PRAGMA table_info({table_name})")
    }


def _parse_utc(value: Any) -> datetime | None:
    if value is None or not str(value).strip():
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _historical_republisher_contract_state(
    conn: sqlite3.Connection,
    *,
    contract_id: str,
    system_id: str,
    station: str,
    day: str,
    lead_bucket: str,
    run_cycle: str,
    day_end: datetime,
    as_of: datetime,
    truth: Mapping[str, Any],
) -> tuple[bool, bool]:
    """Validate a republisher receipt contract without widening authority.

    The first result says that the contract is structurally valid and can be
    identified as a historical receipt.  The second result is the explicit
    ``historical_oos_eligible`` bit.  A valid contract with that bit cleared
    remains training evidence; a missing or malformed contract can never
    become an OOS sample.
    """

    if not contract_id:
        return False, False
    table = "calibration_republisher_historical_oos_contract"
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    if exists is None:
        return False, False
    required = {
        "historical_oos_contract_id",
        "contract_version",
        "ensemble_system_id",
        "station_id",
        "target_local_date",
        "archive_lead_bucket",
        "run_cycle",
        "station_day_closed",
        "station_day_end_utc",
        "station_day_closed_at_utc",
        "awc_truth_id",
        "awc_truth_revision_no",
        "awc_truth_asof_utc",
        "awc_truth_source_lane",
        "receipt_class",
        "temporal_scope",
        "future_event_count",
        "historical_oos_eligible",
        "republisher_receipt_sha256",
        "content_sha256",
        "created_at_utc",
    }
    columns = _table_columns(conn, table)
    if not required.issubset(columns):
        return False, False
    select_columns = sorted(required)
    row = conn.execute(
        "SELECT " + ",".join(select_columns) + " FROM " + table
        + " WHERE historical_oos_contract_id=?",
        (contract_id,),
    ).fetchone()
    if row is None:
        return False, False
    contract = dict(zip(select_columns, row))
    # Contract identity must bind exactly to the member run scope.  This
    # prevents a valid receipt for one station/day being reused elsewhere.
    try:
        station_day_closed = int(contract["station_day_closed"])
        future_event_count = int(contract["future_event_count"])
        contract_revision = int(contract["awc_truth_revision_no"])
    except (TypeError, ValueError):
        return False, False
    if (
        str(contract["historical_oos_contract_id"]) != contract_id
        or str(contract["ensemble_system_id"]) != system_id
        or str(contract["station_id"]).upper() != station
        or str(contract["target_local_date"]) != day
        or str(contract["archive_lead_bucket"]) != lead_bucket
        or str(contract["run_cycle"]) != run_cycle
        or station_day_closed != 1
        or str(contract["awc_truth_source_lane"]) != "AWC"
        or str(contract["receipt_class"]) != HISTORICAL_REPUBLISHER_RECEIPT
        or str(contract["temporal_scope"]) != HISTORICAL_OOS_TEMPORAL_SCOPE
        or future_event_count != 0
    ):
        return False, False
    if (
        len(str(contract["republisher_receipt_sha256"])) != 64
        or len(str(contract["content_sha256"])) != 64
        or not str(contract["contract_version"]).strip()
    ):
        return False, False
    contract_day_end = _parse_utc(contract["station_day_end_utc"])
    closed_at = _parse_utc(contract["station_day_closed_at_utc"])
    truth_asof = _parse_utc(contract["awc_truth_asof_utc"])
    created_at = _parse_utc(contract["created_at_utc"])
    truth_committed = _parse_utc(truth.get("canonical_committed_at_utc"))
    if (
        contract_day_end is None
        or closed_at is None
        or truth_asof is None
        or created_at is None
        or truth_committed is None
        or contract_day_end != day_end
        or closed_at < day_end
        or closed_at > as_of
        or truth_asof < truth_committed
        or truth_asof > as_of
        or created_at > as_of
        or str(contract["awc_truth_id"]) != str(truth.get("truth_id") or "")
        or contract_revision != int(truth.get("revision_no", -1))
    ):
        return False, False
    try:
        eligible = int(contract["historical_oos_eligible"]) == 1
    except (TypeError, ValueError):
        return False, False
    return True, eligible


def _load_cases(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    as_of_utc: str,
) -> dict[tuple[str, str, str, str], list[dict[str, Any]]]:
    station = station_id.strip().upper()
    as_of = datetime.fromisoformat(as_of_utc.replace("Z", "+00:00"))
    if as_of.tzinfo is None:
        raise ContractError("native_ensemble_oos_as_of_timezone_missing")
    as_of = as_of.astimezone(timezone.utc)
    station_row = conn.execute(
        "SELECT settlement_definition "
        "FROM calibration_station_registry WHERE station_id=?",
        (station,),
    ).fetchone()
    if station_row is None:
        raise ContractError("native_ensemble_oos_station_unregistered")
    timezone_rows = conn.execute(
        "SELECT DISTINCT settlement_timezone "
        "FROM basket_target_market_registry "
        "WHERE station_id=? AND settlement_timezone<>''",
        (station,),
    ).fetchall()
    if len(timezone_rows) != 1:
        raise ContractError(
            "native_ensemble_oos_market_timezone_not_unique"
        )
    timezone_name = str(timezone_rows[0][0])
    binding_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND "
        "name='calibration_station_market_timezone_binding'"
    ).fetchone()
    if binding_table is not None:
        binding = conn.execute(
            "SELECT settlement_timezone,market_registry_timezone_count "
            "FROM calibration_station_market_timezone_binding "
            "WHERE station_id=?",
            (station,),
        ).fetchone()
        if (
            binding is not None
            and (
                str(binding[0]) != timezone_name
                or int(binding[1]) != 1
            )
        ):
            raise ContractError(
                "native_ensemble_oos_market_timezone_binding_mismatch"
            )
    settlement_definition = str(station_row[0])
    systems = _load_systems(conn)
    truth = {
        str(row[1]): {
            "truth_id": str(row[0]),
            "actual_tmax_c": float(row[2]),
            "canonical_committed_at_utc": str(row[3]),
            "revision_no": int(row[4]),
            "source_lane": str(row[5]),
        }
        for row in conn.execute(
            "SELECT t.truth_id,t.local_date,t.official_tmax_c,"
            "t.canonical_committed_at_utc,t.revision_no,t.source_lane "
            "FROM calibration_station_daily_high_truth AS t "
            "WHERE t.station_id=? AND t.source_lane='AWC' "
            "AND t.qc_status='ACCEPTED' "
            "AND t.authority_scope IN "
            "('FORMAL_NATIVE_ELIGIBLE','NO_LIVE_REPLAY_CANDIDATE') "
            "AND julianday(t.canonical_committed_at_utc)<=julianday(?) "
            "AND t.revision_no=( "
            "SELECT MAX(t2.revision_no) "
            "FROM calibration_station_daily_high_truth AS t2 "
            "WHERE t2.station_id=t.station_id "
            "AND t2.local_date=t.local_date "
            "AND t2.settlement_definition=t.settlement_definition "
            "AND t2.source_lane='AWC' AND t2.qc_status='ACCEPTED' "
            "AND t2.authority_scope IN "
            "('FORMAL_NATIVE_ELIGIBLE','NO_LIVE_REPLAY_CANDIDATE') "
            "AND julianday(t2.canonical_committed_at_utc)<=julianday(?)"
            ") ORDER BY t.local_date",
            (station, as_of_utc, as_of_utc),
        )
        # Station-days settled by the retired ingest before their own local
        # afternoon.  They cannot be revised -- no canonical fact was ever
        # written for them -- so a model scored against one is scored against
        # a partial morning: KSFO 2026-08-01 recorded 14C from three
        # observations on a day that reached 23C.  Skipping them here is the
        # difference between a gate that measures skill and one that also
        # measures which mornings the old path happened to catch.
        if not is_excluded(station, str(row[1]))
    }
    grouped: dict[
        tuple[str, str, str, str, str],
        list[tuple[str, float, str, bool, str, str, str]],
    ] = defaultdict(list)
    historical_contract_ids_by_group: dict[
        tuple[str, str, str, str, str],
        set[str],
    ] = defaultdict(set)
    tz = ZoneInfo(timezone_name)
    # Charter 62: training does not partition by season.  Every settled day
    # in the rolling window trains together -- recency weighting is the
    # season tracker -- and the whole build carries the single station-local
    # label of the fit moment.  Serving-horizon labels beyond it are stamped
    # at emission (``season_bins_for_window``), never fitted separately.
    fit_season_bin = station_season_bin(
        station, as_of.astimezone(tz).date().isoformat()
    )
    # The live/forward republisher table is intentionally *not* a source for
    # OOS or training.  Its rows remain observational candidates forever; a
    # later station-day close must not retro-promote them.  The legacy native
    # member table with OPEN_METEO transport is likewise prospective evidence
    # and is excluded here.  Only the append-only historical archive below,
    # with an explicit historical OOS contract, can supply republisher
    # holdout rows.
    historical_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND "
        "name='calibration_native_ensemble_historical_member_run'"
    ).fetchone()
    if historical_table is not None:
        for row in conn.execute(
            "SELECT ensemble_system_id,target_local_date,"
            "archive_lead_bucket,run_cycle,archive_query_at_utc,"
            "member_id,raw_tmax_c,content_sha256,"
            "transport_provider_id,native_run_init_utc,"
            "offline_oos_eligible,extraction_version "
            "FROM calibration_native_ensemble_historical_member_run "
            "WHERE station_id=? AND qc_status='ACCEPTED' "
            "AND authority_scope='CALIBRATION_TRAIN_ONLY' "
            "AND holdout_eligible=0 "
            f"AND transport_provider_id IN ({_OFFICIAL_TRANSPORT_SQL_LIST}) "
            # Every lead the archive holds, not d2 alone.  This filter dated
            # from when d2 was the only lead captured; the scope contracts were
            # later widened to d0/d1/d2 so that P0 could follow the freshest
            # run, but this query was not, so the shorter leads were collected,
            # stored, and never read.  No d1 or d0 weights have ever existed as
            # a result, and P0 fails closed at those leads while the rows sit
            # in the table.
            #
            # Widening here does not admit anything unvetted: the scope check
            # below still requires the row to match a registered contract for
            # its own lead, cycle and extraction version, and training groups
            # by lead so weights are never shared across them.
            "ORDER BY target_local_date,archive_query_at_utc,member_id",
            (station,),
        ):
            run_cycle = str(row[3] or "").strip()
            if run_cycle not in VALID_NATIVE_RUN_CYCLES:
                raise ContractError(
                    "native_ensemble_oos_run_cycle_mapping_missing"
                )
            if not _historical_transport_scope_allowed(
                system_id=str(row[0]),
                transport_provider_id=str(row[8]),
                lead_bucket=str(row[2]),
                run_cycle=run_cycle,
                extraction_version=str(row[11] or ""),
            ):
                continue
            offline_oos_eligible = (
                int(row[10]) == 1
                and str(row[8]) in OFFICIAL_ARCHIVE_TRANSPORT_PROVIDER_IDS
                and bool(str(row[9] or "").strip())
            )
            decision_at = (
                str(row[9])
                if offline_oos_eligible
                else str(row[4])
            )
            grouped[
                (
                    str(row[0]),
                    str(row[1]),
                    str(row[2]),
                    run_cycle,
                    decision_at,
                )
            ].append((
                str(row[5]),
                float(row[6]),
                str(row[7]),
                offline_oos_eligible,
                (
                    "OFFLINE_CHRONOLOGICAL_NATIVE_ARCHIVE"
                    if offline_oos_eligible
                    else "RETROSPECTIVE_ARCHIVE_TRAIN_ONLY"
                ),
                str(row[8]),
                str(row[9] or "").strip(),
            ))
    republisher_historical_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND "
        "name='calibration_republisher_ensemble_historical_member_run'"
    ).fetchone()
    if republisher_historical_table is not None:
        historical_table_name = (
            "calibration_republisher_ensemble_historical_member_run"
        )
        historical_columns = _table_columns(
            conn,
            historical_table_name,
        )
        base_historical_columns = {
            "ensemble_system_id",
            "target_local_date",
            "archive_lead_bucket",
            "run_cycle",
            "archive_query_at_utc",
            "member_id",
            "raw_tmax_c",
            "content_sha256",
            "transport_provider_id",
            "authority_scope",
            "holdout_eligible",
        }
        if base_historical_columns.issubset(historical_columns):
            contract_columns = {
                "historical_oos_contract_id",
                "historical_oos_eligible",
            } & historical_columns
            run_init_columns = {"native_run_init_utc"} & historical_columns
            selected_columns = sorted(
                (
                    base_historical_columns
                    | contract_columns
                    | run_init_columns
                    | {"station_id"}
                )
                & historical_columns
            )
            # SELECT * is deliberately avoided: this keeps the loader stable
            # across append-only migrations while preserving strict column
            # presence checks above.
            select_sql = ",".join(selected_columns)
            for raw_row in conn.execute(
                "SELECT " + select_sql + " FROM " + historical_table_name
                + " WHERE station_id=? AND qc_status='ACCEPTED' "
                "AND authority_scope='CALIBRATION_TRAIN_ONLY' "
                "AND holdout_eligible=0 AND transport_provider_id='OPEN_METEO' "
                "ORDER BY target_local_date,archive_query_at_utc,member_id",
                (station,),
            ):
                row = dict(zip(selected_columns, raw_row))
                run_cycle = str(row["run_cycle"] or "").strip()
                if run_cycle not in VALID_NATIVE_RUN_CYCLES:
                    raise ContractError(
                        "native_ensemble_oos_run_cycle_mapping_missing"
                    )
                system_id = str(row["ensemble_system_id"])
                day = str(row["target_local_date"])
                lead_bucket = str(row["archive_lead_bucket"])
                snapshot_at = str(row["archive_query_at_utc"])
                contract_id = str(
                    row.get("historical_oos_contract_id") or ""
                )
                contract_valid = False
                contract_oos_eligible = False
                if system_id in systems and day in truth:
                    day_end = (
                        datetime.fromisoformat(day).replace(tzinfo=tz)
                        + timedelta(days=1)
                    ).astimezone(timezone.utc)
                    contract_valid, contract_oos_eligible = (
                        _historical_republisher_contract_state(
                            conn,
                            contract_id=contract_id,
                            system_id=system_id,
                            station=station,
                            day=day,
                            lead_bucket=lead_bucket,
                            run_cycle=run_cycle,
                            day_end=day_end,
                            as_of=as_of,
                            truth=truth[day],
                        )
                    )
                try:
                    row_oos_eligible = (
                        int(row.get("historical_oos_eligible", 0) or 0)
                        == 1
                    )
                except (TypeError, ValueError):
                    row_oos_eligible = False
                group_key = (
                    system_id,
                    day,
                    lead_bucket,
                    run_cycle,
                    snapshot_at,
                )
                if contract_valid and contract_id:
                    historical_contract_ids_by_group[group_key].add(
                        contract_id
                    )
                candidate_member = (
                    str(row["member_id"]),
                    float(row["raw_tmax_c"]),
                    str(row["content_sha256"]),
                    bool(
                        row_oos_eligible
                        and contract_valid
                        and contract_oos_eligible
                    ),
                    (
                        HISTORICAL_REPUBLISHER_RECEIPT
                        if contract_valid
                        else "RETROSPECTIVE_ARCHIVE_TRAIN_ONLY"
                    ),
                    str(row["transport_provider_id"]),
                    str(row.get("native_run_init_utc") or "").strip(),
                )
                # Contract binding is append-only, so a bound successor row
                # legitimately coexists with its earlier train-only row.
                # Select the bound successor for the same member/group rather
                # than counting both copies and discarding an otherwise
                # complete ensemble.
                existing_members = grouped[group_key]
                existing_index = next(
                    (
                        index
                        for index, existing_member in enumerate(existing_members)
                        if existing_member[0] == candidate_member[0]
                    ),
                    None,
                )
                if existing_index is None:
                    existing_members.append(candidate_member)
                elif (
                    candidate_member[3]
                    and not existing_members[existing_index][3]
                ):
                    existing_members[existing_index] = candidate_member
        # A pre-contract historical archive remains training-only.  It is
        # intentionally not loaded into ``grouped`` because the absence of an
        # explicit eligibility bit must not be inferred as a holdout.
    by_scope_day: dict[
        tuple[str, str, str, str, str],
        dict[str, Any],
    ] = {}
    for (
        system_id,
        day,
        lead_bucket,
        run_cycle,
        snapshot_at,
    ), member_rows in grouped.items():
        if system_id not in systems or day not in truth:
            continue
        expected = systems[system_id]["expected_member_count"]
        member_ids = {
            member
            for member, _value, _sha256, _holdout, _class, _transport, _native_run_init
            in member_rows
        }
        if len(member_rows) != expected or len(member_ids) != expected:
            continue
        snapshot = datetime.fromisoformat(
            snapshot_at.replace("Z", "+00:00")
        )
        if snapshot.tzinfo is None:
            continue
        day_end = (
            datetime.fromisoformat(day)
            .replace(tzinfo=tz)
            + timedelta(days=1)
        ).astimezone(timezone.utc)
        truth_committed = datetime.fromisoformat(
            truth[day]["canonical_committed_at_utc"].replace(
                "Z",
                "+00:00",
            )
        )
        if truth_committed.tzinfo is None:
            continue
        if (
            # A record is allowed into OOS only after its station-local day
            # has actually closed.  A prospective forecast remains an
            # arrival/interface observation until that boundary and closed
            # AWC truth are both historical as-of this evaluation.
            as_of < day_end
            or truth_committed.astimezone(timezone.utc) < day_end
            or snapshot.astimezone(timezone.utc) > as_of
            or truth_committed.astimezone(timezone.utc) > as_of
        ):
            continue
        oos_eligible = all(
            bool(row[3]) for row in member_rows
        )
        evidence_classes = {
            str(row[4]) for row in member_rows
        }
        if len(evidence_classes) != 1:
            continue
        evidence_timing_class = next(iter(evidence_classes))
        transport_ids = {
            str(row[5]) for row in member_rows
        }
        if len(transport_ids) != 1:
            continue
        transport_provider_id = next(iter(transport_ids))
        retrospective_contract_bound = (
            transport_provider_id == "OPEN_METEO"
            and evidence_timing_class == HISTORICAL_REPUBLISHER_RECEIPT
            and bool(historical_contract_ids_by_group.get((
                system_id,
                day,
                lead_bucket,
                run_cycle,
                snapshot_at,
            )))
        )
        if (
            oos_eligible
            and not retrospective_contract_bound
            and (
                snapshot.astimezone(timezone.utc) >= day_end
                or snapshot.astimezone(timezone.utc)
                >= truth_committed.astimezone(timezone.utc)
            )
        ):
            # Native chronological archives still require an arrival before
            # the station-day/truth boundary.  A retained Open-Meteo archive
            # is different: it can only enter OOS after an explicit immutable
            # CLOSED_HISTORICAL_AWC_TRUTH contract binds the retrospective
            # receipt to the exact station/day/revision.  The contract does
            # not create native arrival, P1, or live authority.
            oos_eligible = False
        if transport_provider_id == "OPEN_METEO":
            # A republisher holdout requires the explicit historical contract
            # validated while ingesting the member rows.  Never infer that
            # contract from a closed day or from the transport name alone.
            if evidence_timing_class != HISTORICAL_REPUBLISHER_RECEIPT:
                oos_eligible = False
        # Group by how far ahead the run was issued, not by the calendar-day
        # label the collector wrote.  ``archive_lead_bucket`` stays on the row
        # as the capture fact; it is the wrong key for training, because it is
        # a property of the run's UTC date while skill is a property of the
        # lead to this station's own local day.  Measured over the retained
        # archive the two disagree by enough to matter: d1 and d2 overlap
        # across 18-31h, and d2 alone spans 37 hours.
        run_inits = {
            str(native_run_init or "")
            for _m, _v, _s, _h, _c, _t, native_run_init in member_rows
        }
        training_lead_bucket = lead_bucket
        if len(run_inits) == 1 and next(iter(run_inits)):
            # Members of one capture come from one run, so a single run
            # initialisation is what lets this be bucketed by lead at all.
            # More than one would mean the group is not the unit it claims to
            # be, and averaging across runs is what the hour bucket exists to
            # prevent -- so that case keeps the calendar label rather than
            # inventing an hour for it.
            try:
                run_init = datetime.fromisoformat(
                    next(iter(run_inits)).replace("Z", "+00:00")
                )
            except ValueError:
                run_init = None
            if run_init is not None and run_init.tzinfo is not None:
                day_start = (
                    datetime.fromisoformat(day).replace(tzinfo=tz)
                ).astimezone(timezone.utc)
                training_lead_bucket = lead_hour_bucket_for_run(
                    day_start_utc=day_start, run_init=run_init
                )
        # Aggregator rows keep their calendar label.  A republisher packet has
        # no native run initialisation by construction -- that is what
        # ``AGGREGATOR_UNKNOWN_NATIVE_CYCLE`` records -- so there is no lead to
        # measure, and forcing one would be inventing a number.  Its lane is
        # grouped as it always was.
        scope_day = (
            system_id,
            fit_season_bin,
            training_lead_bucket,
            run_cycle,
            day,
        )
        existing = by_scope_day.get(scope_day)
        if (
            existing is not None
            and (
                bool(existing["holdout_eligible"])
                or not oos_eligible
            )
        ):
            continue
        by_scope_day[scope_day] = {
            "event_date": day,
            # The label the collector wrote, kept alongside the training group
            # above.  Training groups by lead in hours, but the archive tables
            # are keyed by the calendar label, so a consumer that needs to look
            # a case back up in the archive has to ask in the collector's own
            # vocabulary.  Dropping this made the scope key the only lead on
            # the case, and any such lookup silently matched nothing.
            "archive_lead_bucket": lead_bucket,
            "aggregator_snapshot_at_utc": snapshot_at,
            "values": [
                value
                for _member, value, _sha256, _holdout, _class, _transport, _native_run_init
                in sorted(member_rows)
            ],
            "member_run_content_shas": sorted(
                sha256
                for _member, _value, sha256, _holdout, _class, _transport, _native_run_init
                in member_rows
            ),
            "member_set_sha256": content_sha256(sorted(
                sha256
                for _member, _value, sha256, _holdout, _class, _transport, _native_run_init
                in member_rows
            )),
            "member_native_run_init_utcs": [
                native_run_init
                for _member, _value, _sha256, _holdout, _class, _transport, native_run_init
                in sorted(member_rows)
            ],
            "holdout_eligible": oos_eligible,
            "evidence_timing_class": evidence_timing_class,
            "transport_provider_id": transport_provider_id,
            "actual_tmax_c": truth[day]["actual_tmax_c"],
            "historical_closed_day": True,
            "oos_event_temporal_scope": HISTORICAL_OOS_TEMPORAL_SCOPE,
            "future_event_count": 0,
            "historical_oos_contract_ids": sorted(
                historical_contract_ids_by_group.get((
                    system_id,
                    day,
                    lead_bucket,
                    run_cycle,
                    snapshot_at,
                ), set())
            ),
            "settlement_definition": settlement_definition,
            "timezone_name": timezone_name,
        }
    result: dict[
        tuple[str, str, str, str],
        list[dict[str, Any]],
    ] = defaultdict(list)
    for (
        system_id,
        season_bin,
        lead_bucket,
        run_cycle,
        _day,
    ), case in by_scope_day.items():
        result[
            (system_id, season_bin, lead_bucket, run_cycle)
        ].append(case)
    for cases in result.values():
        cases.sort(key=lambda item: item["event_date"])
    return dict(result)


def _evaluate_system(
    *,
    system_id: str,
    cases: Sequence[Mapping[str, Any]],
    min_train_days: int,
    min_holdout_days: int,
    ladder_for: Callable[[str], Mapping[str, Any] | None] | None = None,
) -> dict[str, Any]:
    transports = {
        str(case.get("transport_provider_id") or "")
        for case in cases
    }
    if len(transports) != 1 or "" in transports:
        raise ContractError(
            "native_ensemble_oos_transport_scope_mixed"
        )
    transport_provider_id = next(iter(transports))
    # A point forecast is not an ensemble and is not sized like one.  The 20+8
    # window exists to characterise a fifty-member spread before its centre is
    # trusted; a TAF TX is a single number a forecaster signed, with no spread
    # to fit, so the same history buys far more certainty about it.  Its own
    # profile is allowed here and nowhere else -- an ensemble family that tried
    # to use it still fails closed on the line below.
    is_point_forecast = system_id in POINT_FORECAST_SYSTEMS
    allowed_windows = {
        (DEFAULT_MIN_TRAIN_DAYS, DEFAULT_MIN_HOLDOUT_DAYS),
        # The live forward-accumulation window, valid for every official
        # system: training floors nothing (expanding window), and thin
        # evidence is priced by the weight ramp rather than barred here.
        (
            FORWARD_ACCUMULATION_MIN_TRAIN_DAYS,
            FORWARD_ACCUMULATION_MIN_HOLDOUT_DAYS,
        ),
    }
    if is_point_forecast:
        allowed_windows.add(
            (POINT_FORECAST_MIN_TRAIN_DAYS, POINT_FORECAST_MIN_HOLDOUT_DAYS)
        )
    if (
        transport_provider_id != "OPEN_METEO"
        and (min_train_days, min_holdout_days) not in allowed_windows
    ):
        raise ContractError(
            "native_ensemble_oos_official_window_must_be_20_train_8_holdout"
        )
    holdout_cases = [
        case for case in cases
        if case.get("holdout_eligible") is True
    ]
    # A retrospective Open-Meteo contract makes a closed day eligible for
    # strict historical evaluation, but the append-only contract intentionally
    # does not mutate the retained member row's ``holdout_eligible`` bit.  In
    # a multi-day republisher cohort that means every valid day can appear as
    # a holdout candidate.  Treat the latest chronological ``min_holdout``
    # days as holdout and the earlier valid days as train.  This is the same
    # walk-forward split used by the formal window; it never adds a future
    # event, widens the truth scope, or upgrades republisher authority.
    # Native archives retain their explicit holdout flags and are unchanged.
    if (
        transport_provider_id == "OPEN_METEO"
        and len(holdout_cases) == len(cases)
        and len(cases) > min_holdout_days
    ):
        chronological = sorted(
            holdout_cases,
            key=lambda case: str(case.get("event_date") or ""),
        )
        holdout_cases = chronological[-min_holdout_days:]
    # This evaluator is also callable with externally supplied case maps.
    # Keep the historical boundary here in addition to the SQL loader so a
    # prospective/forward row cannot be reintroduced by a caller or by a
    # later database migration.
    if any(
        case.get("historical_closed_day") is not True
        or case.get("future_event_count", 0) != 0
        or case.get("evidence_timing_class")
        not in {
            "OFFLINE_CHRONOLOGICAL_NATIVE_ARCHIVE",
            HISTORICAL_REPUBLISHER_RECEIPT,
        }
        for case in holdout_cases
    ):
        raise ContractError("native_ensemble_oos_forward_holdout_forbidden")
    if len(holdout_cases) < min_holdout_days:
        return {
            "state": "ACCUMULATING_STRICT_OOS",
            "system_id": system_id,
            "transport_provider_id": transport_provider_id,
            "case_count": len(cases),
            "training_case_count": len(cases) - len(holdout_cases),
            "strict_holdout_case_count": len(holdout_cases),
            "required_train_case_count": min_train_days,
            "required_strict_holdout_case_count": min_holdout_days,
            "missing_train_case_count": max(
                0, min_train_days - (len(cases) - len(holdout_cases))
            ),
            "missing_strict_holdout_case_count": (
                min_holdout_days - len(holdout_cases)
            ),
        }
    holdout = list(holdout_cases[-min_holdout_days:])
    holdout_start = str(holdout[0]["event_date"])
    first_holdout_decision = min(
        str(case["aggregator_snapshot_at_utc"])
        for case in holdout
    )
    # Charter 62: the fit window is a rolling tail window anchored on the
    # holdout, not an expanding one.  Anchoring on data rather than the wall
    # clock keeps a replayed fit byte-identical to the original.
    rolling_window_start = (
        date.fromisoformat(holdout_start[:10])
        - timedelta(days=ROLLING_TRAIN_WINDOW_DAYS)
    ).isoformat()
    train = [
        case
        for case in cases
        if rolling_window_start <= str(case["event_date"]) < holdout_start
        and (
            # Open-Meteo retrospective receipts are queried in one bounded
            # archive batch, so several closed event days legitimately share
            # one receipt timestamp.  The immutable historical OOS contract
            # already supplies the strict truth/as-of boundary; requiring a
            # strictly earlier transport timestamp here would erase every
            # training day from that cohort.  Native chronological archives
            # retain the stricter arrival ordering check.
            transport_provider_id == "OPEN_METEO"
            or str(case["aggregator_snapshot_at_utc"])
            < first_holdout_decision
        )
    ]
    if len(train) < min_train_days:
        return {
            "state": "ACCUMULATING_STRICT_OOS",
            "system_id": system_id,
            "transport_provider_id": transport_provider_id,
            "case_count": len(cases),
            "training_case_count": len(train),
            "strict_holdout_case_count": len(holdout),
            "required_train_case_count": min_train_days,
            "required_strict_holdout_case_count": min_holdout_days,
            "missing_train_case_count": min_train_days - len(train),
            "missing_strict_holdout_case_count": 0,
        }
    parameters = _fit_parameters(train)
    serving_cases = sorted(
        (
            case for case in cases
            if rolling_window_start <= str(case["event_date"])
        ),
        key=lambda case: str(case["event_date"]),
    )
    serving_parameters = _fit_parameters(serving_cases)

    def _prediction_row(
        case: Mapping[str, Any], fitted: Mapping[str, Any]
    ) -> dict[str, Any]:
        ladder = (
            ladder_for(str(case["event_date"])) if ladder_for else None
        )
        probability = _apply_parameters(
            case["values"],
            fitted,
            intervals=ladder["intervals"] if ladder is not None else None,
        )
        day_values = [float(value) for value in case["values"]]
        grid_fields = (
            {"holdout_grid": HOLDOUT_GRID_MARKET_LADDER}
            if ladder is not None else {}
        )
        return {
            **grid_fields,
            "event_date": case["event_date"],
            "decision_at_utc": case["aggregator_snapshot_at_utc"],
            "member_set_sha256": case["member_set_sha256"],
            "member_run_content_shas": case[
                "member_run_content_shas"
            ],
            "historical_oos_contract_ids": list(
                case.get("historical_oos_contract_ids") or []
            ),
            "probability": probability,
            # The day's calibrated center, for the cross consensus bridge:
            # the bridge recenters this system's histogram from here onto the
            # weighted cross center, so it needs the center the histogram was
            # actually built around, not a train-window average.
            "calibrated_center_c": _corrected_center(
                sum(day_values) / len(day_values), fitted
            ),
            "actual_tmax_c": float(case["actual_tmax_c"]),
            "metrics": _metrics(
                probability,
                float(case["actual_tmax_c"]),
                ladder,
            ),
        }

    predictions = [_prediction_row(case, parameters) for case in holdout]
    # A second, longer walk-forward exposure for the cross cohort only.
    # The cross contest can only seat systems on days every one of them
    # holds out, and each system's own holdout is its latest few settled
    # days.  A single missing day in one archive shifts that set by one, so
    # with a three-day window four systems seldom hold out the same three
    # days at once: measured 2026-09-10 across 45 stations, 88 of 118
    # four-system groups could not form a cohort while sharing four or five
    # settled days inside their latest six.  These extra days are predicted
    # from a fit whose training window ends before ALL of them, so a day is
    # out of sample for this system whether it is scored from ``predictions``
    # or from here.  The gate's own fit and window above are untouched.
    cross_predictions: list[dict[str, Any]] | None = None
    cross_parameters: Mapping[str, Any] | None = None
    cross_train_end: str | None = None
    cross_train_n = 0
    cross_exposure = min_holdout_days + CROSS_HOLDOUT_SLACK_DAYS
    chronological_holdout = sorted(
        holdout_cases, key=lambda case: str(case["event_date"])
    )
    if len(chronological_holdout) >= cross_exposure:
        cross_holdout = chronological_holdout[-cross_exposure:]
        cross_holdout_start = str(cross_holdout[0]["event_date"])
        cross_first_decision = min(
            str(case["aggregator_snapshot_at_utc"]) for case in cross_holdout
        )
        cross_window_start = (
            date.fromisoformat(cross_holdout_start[:10])
            - timedelta(days=ROLLING_TRAIN_WINDOW_DAYS)
        ).isoformat()
        cross_train = sorted(
            (
                case for case in cases
                if cross_window_start <= str(case["event_date"])
                < cross_holdout_start
                and (
                    transport_provider_id == "OPEN_METEO"
                    or str(case["aggregator_snapshot_at_utc"])
                    < cross_first_decision
                )
            ),
            key=lambda case: str(case["event_date"]),
        )
        if len(cross_train) >= min_train_days:
            cross_parameters = _fit_parameters(cross_train)
            cross_predictions = [
                _prediction_row(case, cross_parameters)
                for case in cross_holdout
            ]
            cross_train_end = str(cross_train[-1]["event_date"])
            cross_train_n = len(cross_train)
    return {
        "state": "OOS_READY",
        "system_id": system_id,
        "transport_provider_id": transport_provider_id,
        "oos_event_temporal_scope": HISTORICAL_OOS_TEMPORAL_SCOPE,
        "future_event_count": 0,
        "case_count": len(cases),
        "train_start_date": train[0]["event_date"],
        "train_end_date": train[-1]["event_date"],
        "holdout_start_date": holdout[0]["event_date"],
        "holdout_end_date": holdout[-1]["event_date"],
        "train_n": len(train),
        "holdout_n": len(holdout),
        "train_source_counts": {
            "RETROSPECTIVE_ARCHIVE_TRAIN_ONLY": sum(
                case.get("evidence_timing_class")
                == "RETROSPECTIVE_ARCHIVE_TRAIN_ONLY"
                for case in train
            ),
            "FORWARD_LOCAL_RECEIPT": sum(
                case.get("evidence_timing_class")
                == "FORWARD_LOCAL_RECEIPT"
                for case in train
            ),
            HISTORICAL_REPUBLISHER_RECEIPT: sum(
                case.get("evidence_timing_class")
                == HISTORICAL_REPUBLISHER_RECEIPT
                for case in train
            ),
            "OFFLINE_CHRONOLOGICAL_NATIVE_ARCHIVE": sum(
                case.get("evidence_timing_class")
                == "OFFLINE_CHRONOLOGICAL_NATIVE_ARCHIVE"
                for case in train
            ),
        },
        "holdout_source_class": (
            next(iter({
                str(case["evidence_timing_class"])
                for case in holdout
            }))
            if len({
                str(case["evidence_timing_class"])
                for case in holdout
            }) == 1
            else "MIXED_STRICT_CHRONOLOGICAL"
        ),
        "parameters": parameters,
        # What gets served (user ruling 2026-09-02, option 1).  The gate fits
        # on the training window and scores on the holdout; that is what
        # ``parameters``/``oos_metrics`` above are and they do not change,
        # so the cross weights, the residual series and the selection
        # evidence keep their bytes.  But the row that reaches the compiler
        # was that same train-only fit, and the holdout it withheld is the
        # newest evidence there is: measured 2026-09-02 on 361 served
        # artifacts, the event day sat a median six days past
        # ``train_end_date`` (p90 twelve), so under a five-day half-life the
        # served bias lacked 56% of its intended weight.  Refit on the whole
        # rolling window through the newest closed day: -7.7% centre MAE on
        # 13,431 scope-days, 39/45 stations, every family (tmp/bias_gap_arms.py).
        "serving_parameters": serving_parameters,
        "serving_fit_through_date": serving_cases[-1]["event_date"],
        "serving_n": len(serving_cases),
        "predictions": predictions,
        # The cross cohort's longer exposure (see above).  Absent when the
        # archive is too short to train a fit that predates all of it; the
        # cohort then has only ``predictions`` to match on, as before.
        "cross_predictions": cross_predictions,
        "cross_parameters": cross_parameters,
        "cross_train_end_date": cross_train_end,
        "cross_train_n": cross_train_n,
        "oos_metrics": _mean_metrics([
            row["metrics"] for row in predictions
        ]),
    }


# A window cohort pool wider than this is a registry bug, not a fleet: four
# native families at two cycles each is eight rows, and the subset walk is
# 2^n.  Ten keeps it under a thousand and leaves headroom for one more
# family; past that the pass declines rather than stalling the build.
MAX_WINDOW_COHORT_SYSTEMS = 10


def _cohort_view(
    evaluation: Mapping[str, Any],
    common_holdout: Sequence[str],
) -> Mapping[str, Any]:
    """The member as this cohort may score it: a fit that predates its holdout.

    The gate's own ``predictions`` cover the member's latest holdout days
    and were made by a fit trained before them.  A cohort formed on the
    shared window may hold out a day older than that, which the gate's fit
    trained on; scoring it there would be in sample.  Such a member is scored
    from ``cross_predictions`` instead, whose fit predates every exposed day,
    and its train-window numbers -- the error scale the seat weights read,
    the train count the maturity ramp reads, the train end the residual
    series is cut at -- come from that same fit.  A member whose own window
    already covers the holdout is returned as it is, so an exact cohort
    scores exactly as it did.
    """

    own = {
        str(prediction["event_date"])
        for prediction in evaluation["predictions"]
    }
    if all(str(day) in own for day in common_holdout):
        return evaluation
    cross = evaluation.get("cross_predictions") or []
    exposed = {str(prediction["event_date"]) for prediction in cross}
    if not all(str(day) in exposed for day in common_holdout):
        raise ContractError("native_ensemble_oos_cohort_day_not_exposed")
    view = dict(evaluation)
    view["predictions"] = list(cross)
    view["parameters"] = evaluation["cross_parameters"]
    view["train_end_date"] = evaluation["cross_train_end_date"]
    view["train_n"] = int(evaluation.get("cross_train_n") or 0)
    view["cohort_fit_basis"] = "walk_forward_window"
    return view


def _eligible_exact_shared_holdout_cohorts(
    *,
    ready_rows: Sequence[tuple[str, str, Mapping[str, Any]]],
    systems: Mapping[str, Mapping[str, Any]],
    min_holdout_days: int,
    allowed_system_ids: Sequence[str] | None = None,
) -> list[tuple[
    tuple[str, ...],
    list[tuple[str, str, Mapping[str, Any]]],
]]:
    """Every bounded availability-only cross-system OOS cohort, widest first.

    Exact-window cohorts first, then shared-window ones; see
    ``_shared_holdout_cohorts`` for the two passes and why the order matters.
    """

    exact, window = _shared_holdout_cohorts(
        ready_rows=ready_rows,
        systems=systems,
        min_holdout_days=min_holdout_days,
        allowed_system_ids=allowed_system_ids,
    )
    return exact + window


def _shared_holdout_cohorts(
    *,
    ready_rows: Sequence[tuple[str, str, Mapping[str, Any]]],
    systems: Mapping[str, Mapping[str, Any]],
    min_holdout_days: int,
    allowed_system_ids: Sequence[str] | None = None,
) -> tuple[
    list[tuple[tuple[str, ...], list[tuple[str, str, Mapping[str, Any]]]]],
    list[tuple[tuple[str, ...], list[tuple[str, str, Mapping[str, Any]]]]],
]:
    """The exact-window cohorts and the shared-window cohorts, separately.

    Separately because the caller has to emit every exact cohort of every
    cycle variant before any shared-window cohort of any variant.  The
    activation key downstream is (season, lead, run_cycle, system set) and
    does not carry the per-system cycle map, so a shared-window cohort in
    one variant can pre-empt the exact cohort of another variant that reduces
    to the same key -- measured 2026-09-10 on 18 stations, 7 of 271 existing
    candidates changed or vanished that way when the two kinds were merely
    ordered within a variant.  Emitted exact-first globally, the old build's
    candidates come out first and identically, and the window pass only adds.
    """

    # Do not let the forward packet's two-anchor transport requirement leak
    # into calibration selection.  Every ready official system is eligible;
    # exact holdout matching below naturally falls back to the largest valid
    # pair when a third family is unavailable.
    allowed = (
        None
        if allowed_system_ids is None
        else {str(value) for value in allowed_system_ids}
    )
    candidate_rows = [
        row for row in ready_rows
        if allowed is None or str(row[0]) in allowed
    ]
    # Grouped by exact shared holdout *and* cycle combination.  Holdout
    # alone was not enough: the per-system reduction below keeps one row per
    # system, and it only knows how to break a tie between transports, so
    # when a system had two official rows differing only in run cycle the
    # second was dropped on iteration order before any cohort formed.  At
    # ZUUU h12_24 that silently discarded ECMWF@18Z and GEFS@18Z in favour
    # of their 00Z rows, so no 18Z cohort could exist, so no 18Z candidate
    # was ever offered -- and the 18Z weight row the packet actually needed
    # froze at train_end 08-08 while its correction rows advanced to 08-10.
    #
    # Each distinct cycle present becomes its own cohort, plus the mixed
    # variant that the split-packet contract still needs (charter 48c).
    # Bounded: at most one cohort per cycle plus one, and cycles are a
    # closed four-value vocabulary.
    ready_by_holdout: dict[
        tuple[str, ...],
        list[tuple[str, str, Mapping[str, Any]]],
    ] = defaultdict(list)
    for ready_row in candidate_rows:
        holdout_days = tuple(sorted(
            str(prediction["event_date"])
            for prediction in ready_row[2]["predictions"]
        ))
        if len(holdout_days) != min_holdout_days:
            raise ContractError(
                "native_ensemble_oos_ready_holdout_window_invalid"
            )
        ready_by_holdout[holdout_days].append(ready_row)
    ready_by_group: dict[
        tuple[tuple[str, ...], str],
        list[tuple[str, str, Mapping[str, Any]]],
    ] = {}
    for holdout_days, holdout_rows in ready_by_holdout.items():
        for cycle in sorted({str(row[1]) for row in holdout_rows}):
            ready_by_group[(holdout_days, cycle)] = [
                row for row in holdout_rows if str(row[1]) == cycle
            ]
        ready_by_group[(holdout_days, "*")] = list(holdout_rows)
    def _reduce(
        cohort: Sequence[tuple[str, str, Mapping[str, Any]]],
    ) -> list[tuple[str, str, Mapping[str, Any]]] | None:
        # Transport tie-break only.  A system can have both an official
        # native row and an Open-Meteo republisher row inside one group;
        # keep the official one.  Cycles are no longer a tie to break here
        # -- the grouping above already separated them -- so this can no
        # longer discard a cycle by iteration order.
        preferred_by_system: dict[
            str, tuple[str, str, Mapping[str, Any]]
        ] = {}
        for row in cohort:
            system_key = str(row[0])
            current = preferred_by_system.get(system_key)
            if current is None:
                preferred_by_system[system_key] = row
                continue
            current_official = (
                str(current[2].get("transport_provider_id") or "")
                != "OPEN_METEO"
            )
            row_official = (
                str(row[2].get("transport_provider_id") or "")
                != "OPEN_METEO"
            )
            if row_official and not current_official:
                preferred_by_system[system_key] = row
        preferred = list(preferred_by_system.values())
        official = [
            row
            for row in preferred
            if str(row[2].get("transport_provider_id") or "")
            != "OPEN_METEO"
        ]
        official_families = {
            systems[system_id]["independence_family_id"]
            for system_id, _run_cycle, _evaluation in official
        }
        # Native history is the authority when it already supplies two or
        # more independent families.  Only if that official set is below the
        # two-family floor may the explicitly marked Open-Meteo rows fill a
        # missing family.
        candidate = official if len(official_families) >= 2 else preferred
        cohort_families = {
            systems[system_id]["independence_family_id"]
            for system_id, _run_cycle, _evaluation in candidate
        }
        return candidate if len(cohort_families) >= 2 else None

    def _shape(
        candidate: Sequence[tuple[str, str, Mapping[str, Any]]],
    ) -> tuple[tuple[str, str], ...]:
        return tuple(sorted((str(row[0]), str(row[1])) for row in candidate))

    eligible_cohorts = []
    seen_cohort_shapes: set[tuple[tuple[str, str], ...]] = set()
    for (holdout_days, _cycle_key), cohort in ready_by_group.items():
        candidate = _reduce(cohort)
        if candidate is None:
            continue
        shape = _shape(candidate)
        if shape in seen_cohort_shapes:
            continue
        seen_cohort_shapes.add(shape)
        eligible_cohorts.append((holdout_days, candidate))
    # Widest first, then deterministic.  Every cohort is returned rather
    # than only the widest: cohorts differ by cycle pairing now, so a
    # narrower one is a different combination and not a worse version of
    # the wide one.  Downstream already dedupes on (season, lead,
    # run_cycle, system-set) and ranks the survivors, so offering more
    # costs a candidate row, never a wrong seat.
    exact_cohorts = sorted(
        eligible_cohorts,
        key=lambda item: (
            -len(item[1]),
            tuple(sorted(row[0] for row in item[1])),
            item[0],
        ),
    )
    # The second pass: cohorts on a shared window rather than an identical
    # one.  The exact rule above requires every member's latest
    # ``min_holdout_days`` to be the same days, and a single missing capture
    # in one archive shifts that set by one.  Measured 2026-09-10 across 45
    # stations, 88 of 118 four-system groups failed it while sharing four
    # or five settled days inside their latest six -- which is why the
    # contest seats a pair 92-97% of the time and why 41% of the sets in
    # service were rows from the last day their four systems happened to
    # line up.  Here a group forms when the members' exposed days (their
    # holdout plus the longer walk-forward window ``_evaluate_system`` adds)
    # share at least ``min_holdout_days``; its holdout is the newest such
    # days, so the window length the contest scores on is unchanged.
    #
    # Strictly additive.  Every exact cohort is returned first, byte for
    # byte as before, and a window cohort whose shape an exact cohort
    # already has is not offered again; an exact cohort's newest common days
    # ARE its shared holdout, so nothing scored today is scored differently.
    # Only maximal groups are offered: the contest inside a cohort already
    # enumerates its subsets, and a smaller group would be that same
    # enumeration under a different holdout.
    window_cohorts: list[tuple[
        tuple[str, ...], list[tuple[str, str, Mapping[str, Any]]]
    ]] = []
    for cycle_key in sorted({str(row[1]) for row in candidate_rows} | {"*"}):
        pool = _reduce([
            row for row in candidate_rows
            if cycle_key == "*" or str(row[1]) == cycle_key
        ])
        if pool is None or len(pool) < 2:
            continue
        if len(pool) > MAX_WINDOW_COHORT_SYSTEMS:
            continue
        exposed = [
            frozenset(
                str(prediction["event_date"])
                for prediction in (
                    row[2].get("cross_predictions") or row[2]["predictions"]
                )
            )
            for row in pool
        ]
        qualifying: list[tuple[int, frozenset[str]]] = []
        for mask in range(1, 1 << len(pool)):
            members = [index for index in range(len(pool)) if mask & (1 << index)]
            if len(members) < 2:
                continue
            common = frozenset.intersection(*(exposed[i] for i in members))
            if len(common) >= min_holdout_days:
                qualifying.append((mask, common))
        for mask, common in qualifying:
            if any(
                other != mask and other & mask == mask
                for other, _common in qualifying
            ):
                continue
            group = [pool[i] for i in range(len(pool)) if mask & (1 << i)]
            candidate = _reduce(group)
            if candidate is None:
                continue
            shape = _shape(candidate)
            if shape in seen_cohort_shapes:
                continue
            seen_cohort_shapes.add(shape)
            holdout_days = tuple(sorted(common)[-min_holdout_days:])
            window_cohorts.append((holdout_days, candidate))
    window_cohorts.sort(
        key=lambda item: (
            -len(item[1]),
            tuple(sorted(row[0] for row in item[1])),
            item[0],
        ),
    )
    return exact_cohorts, window_cohorts


def p0_gate_tables(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    tables_as_of_utc: str | None = None,
) -> dict[str, Any]:
    """The band and centre/width tables P0's gate scores one station under.

    One resolver for the OOS build and for anything that replays it (charter
    134's nested replay), so a replay scores a blend under exactly the layers
    the build would have: the active tables now, or with ``tables_as_of_utc``
    the ones that were active at that moment.
    """

    # The active DB-resident band table, or the static bootstrap constants
    # when none is activated yet.  The sha rides into every candidate so
    # each artifact pins the exact table its holdout was scored against.
    if tables_as_of_utc is None:
        active_band_table = load_band_table_with_axis(conn, stage="P0")
        active_center_width = load_center_width(conn, stage="P0")
        active_station_width = load_station_width(conn, stage="P0")
        replay_width = None
    else:
        active_band_table = band_table_as_of(
            conn, stage="P0", as_of_utc=tables_as_of_utc
        )
        replay_width = center_width_as_of(
            conn, stage="P0", as_of_utc=tables_as_of_utc
        )
        active_center_width = (
            (
                replay_width["delta_c"],
                replay_width["width_lambda"],
                replay_width["center_width_sha256"],
            )
            if replay_width is not None
            else None
        )
        active_station_width = station_width_as_of(
            conn, stage="P0", as_of_utc=tables_as_of_utc
        )
    band_factors, band_table_sha, band_table_axis_id = (
        active_band_table
        if active_band_table is not None
        else (None, None, BAND_AXIS_CLAIM)
    )
    # Same contract for the stage scalars: whatever is active now is what
    # this build's holdout scores, and its sha rides into every candidate so
    # serving reproduces that exact pair or fails closed.  None until the
    # layer's own forward window admits a fit, which is the identity case
    # and changes nothing.
    center_width = (
        (active_center_width[0], active_center_width[1])
        if active_center_width is not None
        else None
    )
    center_width_sha = (
        active_center_width[2] if active_center_width is not None else None
    )
    # The floor those scalars were fitted under.  It rides on the same row
    # and therefore on the same sha, so the holdout scores the pair under the
    # floor serving will apply and the artifact needs no second pin.
    center_width_floor = (
        (
            load_center_width_floor(conn, stage="P0")
            if replay_width is None
            else replay_width["sigma_floor_rungs"]
        )
        if active_center_width is not None
        else 0.0
    )
    # The width tiers, under the same contract: on the active sha, so the
    # holdout scores what serving applies.
    center_width_tiers = (
        (
            load_center_width_tiers(conn, stage="P0")
            if replay_width is None
            else replay_width["width_tiers"]
        )
        if active_center_width is not None
        else None
    )
    # The per-station width scale, under the same contract as the stage
    # scalars: whatever is active now is what this build's holdout scores,
    # and its sha rides into every candidate.  This build is one station's,
    # so only that station's scale matters here.
    station_width_scale = (
        float(active_station_width[0].get(station_id, 1.0))
        if active_station_width is not None
        else 1.0
    )
    station_width_sha = (
        active_station_width[1]
        if active_station_width is not None and station_width_scale != 1.0
        else None
    )
    # The two fitted tables as the layers ``served_shape`` applies, resolved
    # once so the contest, the gate and the ablation read one object.  The
    # fleet regime shift stays at zero: it has no version a gate could score
    # (see ``served_shape``).
    gate_center_width_layer = (
        CenterWidthLayer(
            delta_c=center_width[0] if center_width else 0.0,
            width_lambda=center_width[1] if center_width else 1.0,
            station_width_scale=station_width_scale,
            sigma_floor_rungs=center_width_floor,
            width_tier_lambdas=(
                (center_width_tiers or {}).get("lambdas") or None
            ),
            width_tier_boundaries=(
                (center_width_tiers or {}).get("boundaries_rungs") or None
            ),
        )
        if (
            center_width is not None
            or station_width_scale != 1.0
            or center_width_floor
            or center_width_tiers
        )
        else None
    )
    # No active table means identity, for the holdout exactly as for serving:
    # ``factors=None`` would reach ``apply_band_reliability``'s pre-store
    # bootstrap constants, which is the shape a veto exists to stop.
    gate_band_layer = (
        BandLayer(factors=band_factors, axis=band_table_axis_id)
        if band_factors is not None else None
    )
    return {
        "band_factors": band_factors,
        "band_table_sha256": band_table_sha,
        "band_table_axis": band_table_axis_id,
        "center_width_sha256": center_width_sha,
        "station_width_sha256": station_width_sha,
        "center_width_layer": gate_center_width_layer,
        "band_layer": gate_band_layer,
    }


def build_native_ensemble_oos(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    as_of_utc: str,
    min_train_days: int = DEFAULT_MIN_TRAIN_DAYS,
    min_holdout_days: int = DEFAULT_MIN_HOLDOUT_DAYS,
    family_weight_caps: Mapping[str, float] | None = None,
    system_mask: Sequence[str] | None = None,
    max_event_date: str | None = None,
    tables_as_of_utc: str | None = None,
    membership_mode: str | None = None,
    holdout_grid: str | None = None,
) -> dict[str, Any]:
    """Build candidate artifacts; perform no writes.

    ``holdout_grid`` (charter 211 (六)) defaults to ``HOLDOUT_GRID``:
    ``market_ladder`` dresses, blends, shapes and scores every holdout day
    on the ladder that station-day was priced against; ``integer_c`` on the
    1C grid it replaced.

    ``membership_mode`` defaults to ``stable_pool.P0_MEMBERSHIP_MODE``.  Under
    charter 134's ``stable_pool`` every cohort mints its whole pool and the
    missing-member views ``pool_subsets`` names, each weighted by the shrunk
    skill rule and certified on the cohort holdout with nothing chosen on it;
    ``subset_contest`` is the rule it replaced.

    ``tables_as_of_utc`` scores the holdout under the band, centre/width and
    station-width tables that were active at that moment instead of now.  A
    forward build leaves it unset: what it mints is served from now on.  A
    replay of a past build sets it to the replayed moment, or the gate scores
    past days under tables fitted on those days (review 2026-09-13, R4).

    Bias and weights are both fitted from this station's own record; weights
    must never borrow other stations' errors (user ruling, 2026-08-14).
    """

    try:
        station_latitude = float(
            station_coordinates(station_id)["latitude"]
        )
    except Exception:  # noqa: BLE001 -- unknown latitude serves neutral
        station_latitude = None
    if native_ensemble_foundation_schema_status(conn)["ok"] is not True:
        raise ContractError("native_ensemble_foundation_schema_required")
    gate_tables = p0_gate_tables(
        conn, station_id=station_id, tables_as_of_utc=tables_as_of_utc
    )
    band_factors = gate_tables["band_factors"]
    band_table_sha = gate_tables["band_table_sha256"]
    band_table_axis_id = gate_tables["band_table_axis"]
    center_width_sha = gate_tables["center_width_sha256"]
    station_width_sha = gate_tables["station_width_sha256"]
    gate_center_width_layer = gate_tables["center_width_layer"]
    gate_band_layer = gate_tables["band_layer"]
    if min_train_days < 2 or min_holdout_days < 1:
        raise ContractError("native_ensemble_oos_window_invalid")
    as_of = datetime.fromisoformat(as_of_utc.replace("Z", "+00:00"))
    if as_of.tzinfo is None:
        raise ContractError("native_ensemble_oos_as_of_timezone_missing")
    if (
        min_train_days == NO_LIVE_BOOTSTRAP_MIN_TRAIN_DAYS
        and min_holdout_days == NO_LIVE_BOOTSTRAP_MIN_HOLDOUT_DAYS
    ):
        threshold_profile = NO_LIVE_BOOTSTRAP_THRESHOLD_PROFILE
    elif (
        min_train_days == DEFAULT_MIN_TRAIN_DAYS
        and min_holdout_days == DEFAULT_MIN_HOLDOUT_DAYS
    ):
        threshold_profile = FORMAL_PROMOTION_THRESHOLD_PROFILE
    elif (
        min_train_days == FORWARD_ACCUMULATION_MIN_TRAIN_DAYS
        and min_holdout_days == FORWARD_ACCUMULATION_MIN_HOLDOUT_DAYS
    ):
        threshold_profile = FORWARD_ACCUMULATION_THRESHOLD_PROFILE
    else:
        threshold_profile = "CUSTOM_STRICT_CHRONOLOGICAL_OOS"
    station = station_id.strip().upper()
    historical_event_date_cutoff = None
    if max_event_date is not None:
        historical_event_date_cutoff = str(max_event_date).strip()
        try:
            cutoff = datetime.strptime(
                historical_event_date_cutoff,
                "%Y-%m-%d",
            ).date()
        except ValueError as exc:
            raise ContractError(
                "native_ensemble_oos_event_date_cutoff_invalid"
            ) from exc
        if cutoff > as_of.astimezone(timezone.utc).date():
            raise ContractError(
                "native_ensemble_oos_event_date_cutoff_future_forbidden"
            )
    systems = _load_systems(conn)
    grid = HOLDOUT_GRID if holdout_grid is None else str(holdout_grid)
    if grid not in HOLDOUT_GRIDS:
        raise ContractError("native_ensemble_oos_holdout_grid_unknown")
    ladder_cache: dict[str, Mapping[str, Any] | None] = {}

    def _ladder_for_day(day: str) -> Mapping[str, Any] | None:
        key = str(day)[:10]
        if key not in ladder_cache:
            ladder_cache[key] = _station_day_ladder(
                conn, station_id=station, event_date=key,
                as_of_utc=as_of_utc,
            )
        return ladder_cache[key]

    ladder_for = (
        _ladder_for_day if grid == HOLDOUT_GRID_MARKET_LADDER else None
    )
    cases_by_scope = _load_cases(
        conn,
        station_id=station,
        as_of_utc=as_of_utc,
    )
    if historical_event_date_cutoff is not None:
        # Keep only scopes that still have at least one historical case after
        # applying the cutoff.  ``_load_cases`` also returns registered
        # scopes whose rows are entirely newer than the requested cutoff;
        # passing those empty lists to ``_evaluate_system`` makes the
        # transport-set guard report a misleading ``transport_scope_mixed``
        # error and prevents a valid earlier strict-OOS replay from running.
        cases_by_scope = {
            scope: filtered
            for scope, cases in cases_by_scope.items()
            if (filtered := [
                case for case in cases
                if str(case["event_date"]) <= historical_event_date_cutoff
            ])
        }
    evaluations: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    readiness = []
    ready_system_evaluations: list[dict[str, Any]] = []
    # A no-live bootstrap report may intentionally scope itself to retained
    # Open-Meteo republisher systems.  Do not evaluate unrelated official
    # scopes with the 2-train/1-holdout bootstrap window: official archives
    # remain on the fixed 20-train/8-holdout contract and must not be
    # accidentally promoted through a republisher fallback report.
    bootstrap_mask_only = (
        threshold_profile == NO_LIVE_BOOTSTRAP_THRESHOLD_PROFILE
        and system_mask is not None
    )
    for scope, cases in sorted(cases_by_scope.items()):
        if bootstrap_mask_only:
            allowed_bootstrap_systems = {
                str(value) for value in system_mask or ()
            }
            if (
                scope[0] not in allowed_bootstrap_systems
                or any(
                    str(case.get("transport_provider_id") or "")
                    != "OPEN_METEO"
                    for case in cases
                )
            ):
                continue
        # Each system is held to the window its own class requires.  Applying
        # the ensemble window to a point forecast is what kept the TAF out of
        # 9 of the 17 stations that issue one, waiting for spread statistics a
        # single signed number does not have.
        scope_min_train, scope_min_holdout = (
            (POINT_FORECAST_MIN_TRAIN_DAYS, POINT_FORECAST_MIN_HOLDOUT_DAYS)
            if (
                scope[0] in POINT_FORECAST_SYSTEMS
                and min_train_days == DEFAULT_MIN_TRAIN_DAYS
                and min_holdout_days == DEFAULT_MIN_HOLDOUT_DAYS
            )
            else (min_train_days, min_holdout_days)
        )
        result = _evaluate_system(
            system_id=scope[0],
            cases=cases,
            min_train_days=scope_min_train,
            min_holdout_days=scope_min_holdout,
            ladder_for=ladder_for,
        )
        # A scope whose archive lane has stopped supplying cases is not a
        # calibration, it is a fossil that keeps re-certifying itself from a
        # frozen case set: ECCC's 00Z h24_48/h48_plus lanes died 2026-08-11
        # and were still being re-activated with fresh 30-day windows -- and
        # served (WMKK, trained_through 08-11) -- two weeks later, because
        # nothing here ever asked when the newest case arrived.  Suppress
        # emission at the source; the rows already written age out on their
        # own validity windows, and the serving sort prefers living rows.
        if result["state"] == "OOS_READY":
            newest_case_day = max(
                str(case["event_date"]) for case in cases
            )
            # A pinned historical replay measures freshness against its own
            # cutoff, not today -- otherwise every replayed scope is "stale"
            # and the replay certifies nothing.
            supply_reference_day = (
                date.fromisoformat(historical_event_date_cutoff)
                if historical_event_date_cutoff is not None
                else as_of.astimezone(timezone.utc).date()
            )
            supply_age_days = (
                supply_reference_day
                - date.fromisoformat(newest_case_day[:10])
            ).days
            if supply_age_days > SCOPE_SUPPLY_MAX_AGE_DAYS:
                result = {
                    **result,
                    "state": "STALE_SUPPLY_SUPPRESSED",
                    "newest_case_event_date": newest_case_day,
                    "supply_age_days": supply_age_days,
                }
        evaluations[scope] = result
        if result["state"] == "OOS_READY":
            # A system's calibration is a property of the system, not of
            # whether it currently holds a seat.  Persisting only the
            # systems inside a winning candidate froze every loser's
            # artifact at the vintage of its last win: ZUUU's GEFS stopped
            # being selected and kept serving a spread factor truncated by
            # the 3.0 bound that aaecfc76 raised to 5.0.  Measured
            # 2026-08-15, 46 live-vocabulary scopes across 21 stations were
            # serving a distribution a median 1.67x too narrow, and no
            # refit could reach them, because refitting only reached
            # winners.  A loser is not out of service: any bound selection
            # row that names it still puts it into P0 -- ZUUU's did, at
            # 24% -- and it rejoins the contest later carrying whatever it
            # last stored.  Its parameters have to stay current either way.
            ready_system_evaluations.append({
                "ensemble_system_id": scope[0],
                "season_bin": scope[1],
                "lead_bucket": scope[2],
                "run_cycle": scope[3],
                "settlement_definition": cases[0]["settlement_definition"],
                "evaluation": result,
            })
        readiness.append({
            "ensemble_system_id": scope[0],
            "season_bin": scope[1],
            "lead_bucket": scope[2],
            "run_cycle": scope[3],
            "transport_provider_id": result[
                "transport_provider_id"
            ],
            **{
                key: value
                for key, value in result.items()
                if key
                in {
                    "state",
                    "case_count",
                    "training_case_count",
                    "strict_holdout_case_count",
                    "required_train_case_count",
                    "required_strict_holdout_case_count",
                    "missing_train_case_count",
                    "missing_strict_holdout_case_count",
                    "train_n",
                    "holdout_n",
                    "oos_metrics",
                }
            },
        })
    for system_id in sorted(systems):
        if not any(scope[0] == system_id for scope in cases_by_scope):
            readiness.append({
                "ensemble_system_id": system_id,
                "season_bin": "*",
                "lead_bucket": "*",
                "run_cycle": "*",
                "state": "ACCUMULATING_STRICT_OOS",
                "case_count": 0,
                "training_case_count": 0,
                "strict_holdout_case_count": 0,
                "required_train_case_count": min_train_days,
                "required_strict_holdout_case_count":
                    min_holdout_days,
                "missing_train_case_count": min_train_days,
                "missing_strict_holdout_case_count":
                    min_holdout_days,
            })
    cross_candidates: list[dict[str, Any]] = []
    emitted_activation_keys: set[tuple[str, str, str, str]] = set()
    scope_groups: dict[
        tuple[str, str],
        dict[str, list[tuple[str, dict[str, Any]]]],
    ] = defaultdict(lambda: defaultdict(list))
    for (
        system_id,
        season_bin,
        lead_bucket,
        run_cycle,
    ), evaluation in evaluations.items():
        # Only rows that can hold a seat belong in the assignment product.
        # An accumulating system's rows cannot join any cohort -- the ready
        # filter below discards them -- but counting them still multiplied
        # the assignment total past the enumeration bound, and the collapse
        # fallback then kept one best-evidenced cycle per system.  That is
        # how KSFO h24_48 lost its 18Z variants: ICON's two accumulating
        # cycles pushed 8 realizable assignments to a nominal 16, and the
        # collapse pinned every system to 00Z while the packet P0 actually
        # serves carries 18Z -- the weight row froze at train_end 07-28
        # while its correction rows advanced to 08-10 (38 scopes measured
        # on 2026-08-15).
        if evaluation["state"] != "OOS_READY":
            continue
        # Charter 95: the regional deterministic models contest seats; the
        # TAF TX and the METAR path keep their evaluation lane only.
        if is_fusion_excluded_point_system(system_id):
            continue
        scope_groups[(season_bin, lead_bucket)][system_id].append((
            run_cycle,
            evaluation,
        ))
    configured_caps = {
        **DEFAULT_FAMILY_WEIGHT_CAPS,
        **dict(family_weight_caps or {}),
    }
    # What each scope is serving right now, so the identity the sort keeps
    # choosing gets refitted beside the contest's pick.  A pinned historical
    # replay has no "now" and asks for none.
    served_identities = (
        _served_cross_identities(
            conn, station_id=station, as_of_utc=as_of_utc
        )
        if historical_event_date_cutoff is None
        else {}
    )
    membership = str(membership_mode or P0_MEMBERSHIP_MODE)
    if membership not in {P0_MEMBERSHIP_MODE_STABLE_POOL, "subset_contest"}:
        raise ContractError("native_ensemble_oos_membership_mode_unknown")
    pool_mode = membership == P0_MEMBERSHIP_MODE_STABLE_POOL
    cohort_tasks: list[tuple[str, str, Any, Any, Any, Any]] = []
    window_tasks: list[tuple[str, str, Any, Any, Any, Any]] = []
    served_set_tasks: list[tuple[str, str, Any, Any, Any, Any]] = []
    for (
        season_bin,
        lead_bucket,
    ), system_rows in _cycle_variant_groups(scope_groups):
        ready_rows = [
            (system_id, run_cycle, evaluation)
            for system_id, scopes in sorted(system_rows.items())
            for run_cycle, evaluation in scopes
            if evaluation["state"] == "OOS_READY"
            # The 2026-08-15 stage ruling: the P0 combination is numerical
            # models only.  Charter 95 (2026-09-06) narrows the exclusion to
            # the METAR path (and the TAF TX, whose lane charter 133 retired):
            # a regional deterministic model is a numerical model and may
            # compete.
            and not is_fusion_excluded_point_system(system_id)
        ]
        if len(ready_rows) < 2:
            continue
        # A ready system whose independently-fixed strict holdout window
        # does not overlap the other ready systems must not poison an
        # otherwise valid cross-native calibration.  Every system evaluation
        # exposes exactly ``min_holdout_days`` latest chronological cases, so
        # grouping by the complete holdout set is a bounded, deterministic
        # way to enumerate every exact common-OOS cohort.  Forming the
        # cohorts uses availability and timing only; the subset contest run
        # inside each cohort does read the holdout (see its docstring).
        exact_cohorts, window_cohorts = _shared_holdout_cohorts(
            ready_rows=ready_rows,
            systems=systems,
            min_holdout_days=min_holdout_days,
            allowed_system_ids=system_mask,
        )
        if pool_mode:
            # Charter 134: no contest.  Each cohort seats its whole pool and
            # the views a late member needs, every one certified on the same
            # holdout with nothing picked on it.
            for tasks, cohorts in (
                (cohort_tasks, exact_cohorts), (window_tasks, window_cohorts)
            ):
                for cohort in cohorts:
                    for subset in pool_subsets(
                        [row[0] for row in cohort[1]],
                        {
                            row[0]: systems[row[0]]["independence_family_id"]
                            for row in cohort[1]
                        },
                    ):
                        tasks.append(
                            (season_bin, lead_bucket, system_rows, ready_rows,
                             cohort, subset)
                        )
            continue
        for cohort in exact_cohorts:
            cohort_tasks.append(
                (season_bin, lead_bucket, system_rows, ready_rows, cohort,
                 None)
            )
        for cohort in window_cohorts:
            window_tasks.append(
                (season_bin, lead_bucket, system_rows, ready_rows, cohort,
                 None)
            )
        for cohort in exact_cohorts + window_cohorts:
            # The served identity rides this cohort only if the cohort can
            # actually produce it: every served system ready here, at the
            # cycle it was fitted under, and inside any mask the caller set.
            served = served_identities.get((season_bin, lead_bucket))
            if served is None:
                continue
            served_ids, served_cycles = served
            cohort_cycles = {
                system_id: run_cycle
                for system_id, run_cycle, _evaluation in cohort[1]
            }
            if (
                set(served_ids).issubset(cohort_cycles)
                and all(
                    cohort_cycles[system_id] == served_cycles[system_id]
                    for system_id in served_ids
                )
                and (
                    system_mask is None
                    or set(served_ids).issubset(
                        {str(value) for value in system_mask}
                    )
                )
            ):
                served_set_tasks.append(
                    (season_bin, lead_bucket, system_rows, ready_rows,
                     cohort, served_ids)
                )
    # Every exact cohort of every variant first, byte for byte the old
    # build; then the shared-window cohorts, which only add; then the
    # served-set refits, so a refit whose set the contest already chose can
    # be skipped before any of the work below.
    cohort_tasks.extend(window_tasks)
    cohort_tasks.extend(served_set_tasks)
    contest_pick_by_cohort: dict[tuple[Any, ...], tuple[str, ...]] = {}
    # One candidate attempt per cohort.  Flattened into a task list rather
    # than nested inside the scope loop so the body below reads the same as
    # it did when a scope produced exactly one cohort.
    for (
        season_bin,
        lead_bucket,
        system_rows,
        ready_rows,
        selected_cohort,
        pinned_subset,
    ) in cohort_tasks:
        common_holdout, selected_ready_rows = selected_cohort
        cohort_key = (
            season_bin,
            lead_bucket,
            tuple(common_holdout),
            tuple(sorted(
                (system_id, run_cycle)
                for system_id, run_cycle, _evaluation in selected_ready_rows
            )),
        )
        if (
            pinned_subset is not None
            and contest_pick_by_cohort.get(cohort_key) == tuple(pinned_subset)
        ):
            # The contest chose the served set itself; the twin already
            # exists one task up and the activation key would collide.
            continue
        cycle_by_system, cycle_mapping_sha, run_cycle, run_cycle_scope = (
            _cycle_mapping_for_rows(selected_ready_rows)
        )
        # Two views of each member.  The contest scores the view whose fit
        # predates every day in this cohort's holdout -- the gate's own fit
        # where the holdout is inside the member's own window, the longer
        # walk-forward fit otherwise -- so a day is never scored by a fit
        # that trained on it, and the weights are derived from the same fit
        # for the same reason.  What persists for a winner is the original
        # evaluation: serving reads its serving refit regardless, and its
        # gate fit stays the row the system artifact was already keyed on.
        original_by_system = {
            system_id: evaluation
            for system_id, _run_cycle, evaluation in selected_ready_rows
        }
        rows = [
            (system_id, _cohort_view(evaluation, common_holdout))
            for system_id, _run_cycle, evaluation in selected_ready_rows
        ]
        cohort_fit_basis_by_system = {
            system_id: str(evaluation.get("cohort_fit_basis") or "gate")
            for system_id, evaluation in rows
        }
        family_by_system = {
            system_id: systems[system_id]["independence_family_id"]
            for system_id, _evaluation in rows
        }
        if len(set(family_by_system.values())) < 2:
            continue
        # ``selected_ready_rows`` is grouped by this exact set above.  Keep
        # the explicit guard beside the cross calculation as a fail-closed
        # invariant if the per-system evaluator ever changes its shape.
        if len(common_holdout) != min_holdout_days:
            raise ContractError(
                "native_ensemble_oos_cross_holdout_window_invalid"
            )
        # Weights are fitted from this station's own train RMSE, never a
        # cross-station pool (user ruling, 2026-08-14).  A pooled lane was
        # built for this lookup and never wired to a caller; it has been
        # removed so the ruling is structural rather than an accident of
        # which arguments the callers pass.  Thin per-station samples are
        # priced by the maturity ramp, not by borrowing other stations'
        # errors.
        train_rmse = {
            system_id: float(evaluation["parameters"]["train_rmse_c"])
            for system_id, evaluation in rows
        }
        # What a served distribution has to cover is the error left after the
        # centre shift, not the error about zero.  Falling back on train RMSE
        # would charge a system for a bias the serving path already removed --
        # sigma/rmse runs as low as 0.68 on the METAR path, so the penalty is
        # far from uniform across systems and would tilt the blend.
        train_error_scale = {
            system_id: float(
                evaluation["parameters"].get("train_residual_sigma_c")
                or evaluation["parameters"]["train_rmse_c"]
            )
            for system_id, evaluation in rows
        }
        # Train-window residuals for every ready system, read before the
        # contest so a covariance-aware weighting can be derived without ever
        # seeing a holdout day.  Each candidate subset is weighted the way it
        # would be served, so the residuals have to exist before the first
        # subset is scored -- and the correlation matrix further down reuses
        # this same series, so the evidence a reader sees and the numbers the
        # weights were built on cannot drift apart.
        certified_train_end = max(
            evaluation["train_end_date"] for _system, evaluation in rows
        )
        train_residuals: dict[str, dict[str, float]] = {
            system_id: {
                case["event_date"]: (
                    float(case["actual_tmax_c"])
                    - sum(float(value) for value in case["values"])
                    / len(case["values"])
                )
                for case in cases_by_scope.get(
                    (
                        system_id,
                        season_bin,
                        lead_bucket,
                        cycle_by_system[system_id],
                    ),
                    (),
                )
                if case["event_date"] <= certified_train_end
            }
            for system_id, _evaluation in rows
        }
        weights, _cohort_weight_rule = _seat_weights(
            residuals_by_system=train_residuals,
            train_error_scale_by_system=train_error_scale,
            family_by_system=family_by_system,
            family_caps=configured_caps,
            train_n_by_system={
                system_id: int(evaluation.get("train_n") or 0)
                for system_id, evaluation in rows
            },
        )
        predictions_by_system = {
            system_id: {
                row["event_date"]: row
                for row in evaluation["predictions"]
            }
            for system_id, evaluation in rows
        }
        # The seat is earned, not granted with admission: every combination
        # of the ready systems is scored on this exact holdout and the best
        # blend takes the weights.  ``weights`` from the full set above stays
        # only as the enumeration's starting vocabulary.
        # The shift signal the serving transform will read, resolved per
        # holdout day from the same sources P0 uses: the day's forecast
        # radiation as known at its cutoff, and the TAF's cooling fields where
        # serving would fall back to them.  Scoring must see exactly what
        # serving will see.
        # Resolved once here so the value the holdout scores, the value the
        # artifact pins and the value serving reads back are one number.
        scope_radiation_sigma = radiation_shift_sigma_for_lead(lead_bucket)
        conditioning_by_day = _holdout_conditioning_signals(
            conn,
            station_id=station_id,
            holdout_days=common_holdout,
            predictions_by_system=predictions_by_system,
            radiation_shift_sigma=scope_radiation_sigma,
        )
        weights, subset_selection = _select_minimum_crps_subset(
            station_latitude=station_latitude,
            ready_system_ids=[system_id for system_id, _e in rows],
            predictions_by_system=predictions_by_system,
            residuals_by_system=train_residuals,
            common_holdout=common_holdout,
            conditioning_by_day=conditioning_by_day,
            family_by_system=family_by_system,
            configured_caps=configured_caps,
            band_factors=band_factors,
            band_axis=band_table_axis_id,
            center_width_layer=gate_center_width_layer,
            train_error_scale=train_error_scale,
            train_n_by_system={
                system_id: int(evaluation.get("train_n") or 0)
                for system_id, evaluation in rows
            },
            pinned_subset=pinned_subset,
            weight_mode=(
                P0_MEMBERSHIP_MODE_STABLE_POOL if pool_mode else "skill"
            ),
            ladder_for=ladder_for,
        )
        if pinned_subset is None:
            contest_pick_by_cohort[cohort_key] = tuple(sorted(weights))
        family_by_system = {
            system_id: family_by_system[system_id]
            for system_id in weights
        }
        # The cycle mapping, its hash and the scope's run_cycle label all
        # describe what the activation actually blends, so they are recomputed
        # for the seated subset -- a full-cohort mapping under a subset
        # selection is exactly the identity mismatch the persist guard
        # rejects.  A subset that is single-cycle also correctly sheds the
        # MIXED label its cohort carried.
        selected_ready_rows = [
            row for row in selected_ready_rows if row[0] in weights
        ]
        rows = [
            (system_id, evaluation)
            for system_id, evaluation in rows
            if system_id in weights
        ]
        cycle_by_system, cycle_mapping_sha, run_cycle, run_cycle_scope = (
            _cycle_mapping_for_rows(selected_ready_rows)
        )
        # Narrowed to the seated cohort now the contest has chosen it.  The
        # series themselves are the ones the weights were derived from above.
        train_residuals = {
            system_id: series
            for system_id, series in train_residuals.items()
            if system_id in weights
        }
        # Shadow schemes, scored beside the served one and serving nothing.
        # The pair inverted when minimum variance was promoted: what is now
        # shadowed is the rule it replaced, so a regression shows up as the
        # old scheme pulling ahead rather than as silence.  ``inverse_variance``
        # stays because the gap between it and the served blend is what the
        # measured correlations are worth on their own, which is the part of
        # the promotion that rested on the thinnest evidence.
        shadow_weight_schemes: dict[str, dict[str, float]] = {}
        try:
            previous_rule_weights = _bounded_family_weights(
                # Train RMSE on purpose: this leg *is* the retired rule, and
                # a shadow that quietly adopts the new scale stops being a
                # comparison against what was replaced.
                train_error_scale_by_system={
                    system_id: train_rmse[system_id] for system_id in weights
                },
                family_by_system=family_by_system,
                family_caps=configured_caps,
                train_n_by_system={
                    system_id: int(evaluation.get("train_n") or 0)
                    for system_id, evaluation in rows
                },
            )
        except ContractError:
            previous_rule_weights = None
        if previous_rule_weights:
            shadow_weight_schemes["inverse_rmse"] = previous_rule_weights
        try:
            diagonal_weights = _minimum_variance_weights(
                residuals_by_system=train_residuals,
                family_by_system=family_by_system,
                family_caps=configured_caps,
                shrinkage=1.0,
            )
        except ContractError:
            diagonal_weights = None
        if diagonal_weights:
            shadow_weight_schemes["inverse_variance"] = diagonal_weights
        cross_predictions = []
        climate_train = next(
            iter(rows)
        )[1]["parameters"]["climate_samples_c"]
        climate_probability = _probabilities([
            float(value) for value in climate_train
        ])
        baseline_metrics = []
        # The third leg of the diagnostic triangle assembled after the loop:
        # climatology and the learned blend were already scored against each
        # other, which cannot separate "the models are worth something" from
        # "our weighting of them is worth something".
        equal_weight_metrics: list[dict[str, float]] = []
        equal_cohort_metrics: list[dict[str, float]] = []
        scheme_daily_scores: list[dict[str, Any]] = []
        # Marginal worth of each shape layer, scored on the same holdout the
        # gate uses.  The gate itself asks only whether the assembled
        # distribution beats climatology, which a harmful layer passes as
        # easily as a helpful one so long as the rest of the stack carries it;
        # no layer in this stack had ever been scored against its own absence.
        # Recorded, not enforced -- a layer's fate belongs to whoever reads
        # enough of these, not to one artifact's holdout.
        ablation_metrics: dict[str, list[dict[str, float]]] = {
            layer: []
            for layer in (
                "synoptic_cooling",
                "neighbour_direction",
                "band_reliability",
            )
        }
        for day in common_holdout:
            day_ladder = _day_ladder(
                predictions_by_system, sorted(predictions_by_system),
                day, ladder_for,
            )
            ordered_keys, key_centers = _day_grid(
                predictions_by_system, sorted(weights), day, day_ladder
            )
            day_climate_probability = (
                _ladder_climate_probability(climate_train, day_ladder)
                if day_ladder is not None else climate_probability
            )
            day_centers = {
                system: float(
                    predictions_by_system[system][day][
                        "calibrated_center_c"
                    ]
                )
                for system in weights
            }
            # The bridged blend is what P0 will serve under this artifact, so
            # the holdout has to score the bridged blend: a gate that measures
            # the plain mixture while the live path serves the bridge would be
            # certifying math nobody runs.
            probability, _bridge_receipt = bridged_cross_probability(
                system_probabilities={
                    system: predictions_by_system[system][day][
                        "probability"
                    ]
                    for system in weights
                },
                system_weights=weights,
                system_centers_c=day_centers,
                ordered_keys=ordered_keys,
                key_centers_c=key_centers,
            )
            # Only the radiation shift reads these now.  The conditional tail
            # skew was withdrawn 2026-08-18: measured against its own absence
            # over 1712 evaluations it cost 3.0% CRPS, won on 17% of scopes,
            # and was net negative at every lead and at 36 of 45 stations --
            # no pocket of the network it was helping.  The mechanism was not
            # the problem; overlapping with the band table was.  It lifted
            # buckets below 5% into the 2-12% band the band table then
            # multiplies by 0.84, so two thirds of every lift was undone, and
            # each layer had been fitted with the other held still as
            # background.  Serving kept applying it to artifacts gated with the
            # tag until the last one's window closed (2026-09-17); the branch
            # was deleted 2026-09-22 (charter 43 amendment).
            cooling_shift = (
                conditioning_by_day.get(str(day)) or {}
            ).get("synoptic_cooling_shift_sigma")
            seated_center_c = served_model_center_c(weights, day_centers)

            def _shaped(
                base: Mapping[str, float],
                *,
                skip: str | None = None,
                grid: Sequence[str] = ordered_keys,
                center_c: float = seated_center_c,
            ) -> dict[str, float]:
                """The shape stack, optionally with one layer left out.

                ``_holdout_shape_stack`` is the arithmetic; this closure only
                binds the day.  The ablation scores that same function minus
                one layer rather than a re-derivation of it, and the grid and
                the lean's centre are parameters because the equal-weight
                diagnostic blends a wider cohort than the seated one, over
                its own bucket union and at its own weights.
                """

                return _holdout_shape_stack(
                    base,
                    grid=grid,
                    cooling_shift_sigma=cooling_shift,
                    station_latitude=station_latitude,
                    neighbour_center_c=center_c,
                    center_width_layer=gate_center_width_layer,
                    band_layer=gate_band_layer,
                    skip=skip,
                    ladder=day_ladder,
                )

            def _weighted_blend(
                system_weights: Mapping[str, float],
            ) -> dict[str, float]:
                """One day, one set of weights, everything else held equal.

                Bridge, shape stack and grid are the served ones, so any
                scheme routed through here differs from the seated blend in
                its weights and in nothing else.
                """

                cohort = sorted(system_weights)
                cohort_centers = {
                    system: float(
                        predictions_by_system[system][day][
                            "calibrated_center_c"
                        ]
                    )
                    for system in cohort
                }
                grid, grid_centers = _day_grid(
                    predictions_by_system, cohort, day, day_ladder
                )
                blended, _receipt = bridged_cross_probability(
                    system_probabilities={
                        system: predictions_by_system[system][day][
                            "probability"
                        ]
                        for system in cohort
                    },
                    system_weights=dict(system_weights),
                    system_centers_c=cohort_centers,
                    ordered_keys=grid,
                    key_centers_c=grid_centers,
                )
                return _shaped(
                    blended,
                    grid=grid,
                    center_c=served_model_center_c(
                        system_weights, cohort_centers
                    ),
                )

            def _equal_blend(cohort: Sequence[str]) -> dict[str, float]:
                """The same systems, weighted alike.

                Holding the candidate set fixed is what makes the contrast
                attributable: the bridge, the shape stack and the scorer are
                the ones above, so between this and the learned blend only the
                weights differ.  Over the seated set that is exactly the
                weighting question; over the full ready cohort it also carries
                whatever the subset contest chose to drop.
                """

                grid, grid_centers = _day_grid(
                    predictions_by_system, cohort, day, day_ladder
                )
                share = 1.0 / float(len(cohort))
                equal_weights = {system: share for system in cohort}
                cohort_centers = {
                    system: float(
                        predictions_by_system[system][day][
                            "calibrated_center_c"
                        ]
                    )
                    for system in cohort
                }
                blended, _receipt = bridged_cross_probability(
                    system_probabilities={
                        system: predictions_by_system[system][day][
                            "probability"
                        ]
                        for system in cohort
                    },
                    system_weights=equal_weights,
                    system_centers_c=cohort_centers,
                    ordered_keys=grid,
                    key_centers_c=grid_centers,
                )
                return _shaped(
                    blended,
                    grid=grid,
                    center_c=served_model_center_c(
                        equal_weights, cohort_centers
                    ),
                )

            available_systems = sorted(predictions_by_system)
            equal_probability = _equal_blend(sorted(weights))
            # Second leg, and only when the contest actually dropped someone:
            # otherwise it is the same arithmetic on the same systems and
            # would read as an independent measurement of nothing.
            equal_cohort_probability = (
                _equal_blend(available_systems)
                if set(available_systems) != set(weights)
                else None
            )

            unshaped = probability
            probability = _shaped(unshaped)
            actual = float(
                predictions_by_system[
                    next(iter(weights))
                ][day]["actual_tmax_c"]
            )
            equal_weight_metrics.append(
                _metrics(equal_probability, actual, day_ladder)
            )
            if equal_cohort_probability is not None:
                equal_cohort_metrics.append(
                    _metrics(equal_cohort_probability, actual, day_ladder)
                )
            # The three schemes scored on one day, kept paired.  A scope's
            # holdout is three or four days, so no interval is estimable
            # inside it; the unit that can carry one is the day, pooled across
            # scopes, and a pooled bootstrap needs the samples rather than the
            # means this row would otherwise reduce them to.
            scheme_daily_scores.append({
                "event_date": str(day),
                **(
                    {"holdout_grid": HOLDOUT_GRID_MARKET_LADDER}
                    if day_ladder is not None else {}
                ),
                "climate": _metrics(
                    day_climate_probability, actual, day_ladder
                ),
                "equal_weight": _metrics(
                    equal_probability, actual, day_ladder
                ),
                "learned": _metrics(probability, actual, day_ladder),
                **{
                    label: _metrics(
                        _weighted_blend(scheme_weights), actual, day_ladder
                    )
                    for label, scheme_weights
                    in shadow_weight_schemes.items()
                },
            })
            for layer, samples in ablation_metrics.items():
                samples.append(_metrics(
                    _shaped(unshaped, skip=layer), actual, day_ladder
                ))
            decision_at_utc = max(
                str(predictions_by_system[system][day][
                    "decision_at_utc"
                ])
                for system in weights
            )
            cross_predictions.append({
                "event_date": day,
                "decision_at_utc": decision_at_utc,
                "trained_through_event_date": max(
                    evaluation["train_end_date"]
                    for _system, evaluation in rows
                ),
                "system_member_set_shas": {
                    system: predictions_by_system[system][day][
                        "member_set_sha256"
                    ]
                    for system in sorted(weights)
                },
                "historical_oos_contract_ids_by_system": {
                    system: list(
                        predictions_by_system[system][day].get(
                            "historical_oos_contract_ids"
                        ) or []
                    )
                    for system in sorted(weights)
                },
                "actual_tmax_c": actual,
                "probability": probability,
                "metrics": _metrics(probability, actual, day_ladder),
                **(
                    {"holdout_grid": HOLDOUT_GRID_MARKET_LADDER,
                     "bucket_centers_c": dict(zip(ordered_keys, key_centers))}
                    if day_ladder is not None else {}
                ),
            })
            baseline_metrics.append(_metrics(
                day_climate_probability,
                actual,
                day_ladder,
            ))
        cross_metrics = _mean_metrics([
            row["metrics"] for row in cross_predictions
        ])
        climate_metrics = _mean_metrics(baseline_metrics)
        equal_weight_diagnostic = _equal_weight_diagnostic(
            climate_metrics=climate_metrics,
            equal_weight_metrics=(
                _mean_metrics(equal_weight_metrics)
                if equal_weight_metrics else None
            ),
            equal_cohort_metrics=(
                _mean_metrics(equal_cohort_metrics)
                if equal_cohort_metrics else None
            ),
            learned_metrics=cross_metrics,
            available_system_ids=sorted(predictions_by_system),
            seated_system_ids=sorted(weights),
            seated_weights=weights,
            daily_scores=scheme_daily_scores,
            shadow_weight_schemes=shadow_weight_schemes,
            shadow_metrics={
                label: _mean_metrics([
                    row[label] for row in scheme_daily_scores
                ])
                for label in shadow_weight_schemes
                if all(label in row for row in scheme_daily_scores)
            } if scheme_daily_scores else {},
        )
        # Positive percentages mean the layer earned its place: the stack
        # without it scores worse.  Sign is what a reader wants first, so it
        # is stated in the units the gate argues in rather than as two sets of
        # raw means to difference by hand.
        shape_layer_ablation = {}
        for layer, samples in ablation_metrics.items():
            if not samples:
                continue
            without = _mean_metrics(samples)
            shape_layer_ablation[layer] = {
                metric: (
                    100.0
                    * (without[metric] - cross_metrics[metric])
                    / cross_metrics[metric]
                    if cross_metrics[metric]
                    else 0.0
                )
                for metric in ("crps", "nll", "brier")
            }
        # No provisional admission: every served weight is a certified
        # weight, awarded by the subset contest on the exact shared holdout
        # (the 2026-08-15 ruling -- eligibility, never a mandate).  The two
        # fields stay persisted so a reader can verify the sets no longer
        # diverge, and so old rows, where they did, remain interpretable.
        certified_weights = dict(weights)
        provisional_system_ids: list[str] = []
        selected_system_ids = sorted(weights)
        family_caps_exact = {
            family: float(configured_caps.get(family, 0.75))
            for family in sorted(set(family_by_system.values()))
        }
        weight_receipt = validate_cross_native_ensemble_weights(
            system_weights=weights,
            systems_by_id=systems,
            family_weight_caps=family_caps_exact,
        )
        # The same series the shadow weighting above was derived from, read
        # once before the holdout and reused here.  Where a system has no
        # cases inside the certified train window the series is empty and
        # ``_correlation`` reports 0.0, exactly as it does for any other pair
        # too short to measure.  This is evidence, not a weight -- nothing
        # here feeds the served blend.
        residuals = train_residuals
        correlations: dict[str, dict[str, float]] = {}
        for left in selected_system_ids:
            correlations[left] = {}
            for right in selected_system_ids:
                days = sorted(
                    set(residuals[left]) & set(residuals[right])
                )
                correlations[left][right] = _correlation(
                    [residuals[left][day] for day in days],
                    [residuals[right][day] for day in days],
                )
        # The gate record must stay out of ``cross_metrics``: those bytes are
        # part of the selection-evidence identity, and the activation pointers
        # the P0 compile validates against were bound under the gate-free
        # bytes.  Injecting it there changed every re-evaluated scope's
        # selection sha, unbound the current pointers, and silenced the whole
        # envelope pipeline within one OOS scan of the next worker restart.
        oos_gate = {
            **_cross_oos_gate(cross_metrics, climate_metrics),
            "membership_mode": membership,
        }
        # Activations are unique on
        # (kind, station, system, season, lead, run_cycle, set_hash,
        # generation) and the per-system cycle mapping is not part of that key.
        # Two assignments can therefore reduce to the same key: with two
        # systems and two cycles each, the two crossed pairs both label as
        # MIXED_NATIVE_CYCLES.  Persisting both aborts the whole station's
        # transaction on the unique constraint, which would turn a widened
        # training set into no training at all.
        #
        # Keep the first in the fixed assignment order.  Only one mapping per
        # label is reachable anyway: a forward packet comes from one collector
        # pass per lead, so its systems carry the cycles their own contracts
        # name, and the alternative crossing never appears in a packet.
        candidate_activation_key = (
            season_bin,
            lead_bucket,
            run_cycle,
            content_sha256(selected_system_ids),
        )
        if candidate_activation_key in emitted_activation_keys:
            continue
        emitted_activation_keys.add(candidate_activation_key)
        settlement_definition = next(
            iter(cases_by_scope.values())
        )[0]["settlement_definition"]
        if pool_mode:
            # Charter 134 (七): certified on the key's rolling record.
            oos_gate = _stable_pool_window_gate(
                own_learned_metrics=cross_metrics,
                own_climate_metrics=climate_metrics,
                own_daily_scores=scheme_daily_scores,
                history=_stable_pool_gate_history(
                    conn,
                    station_id=station,
                    season_bin=season_bin,
                    lead_bucket=lead_bucket,
                    run_cycle=run_cycle,
                    run_cycle_by_system_json=_json(cycle_by_system),
                    run_cycle_by_system_sha256=cycle_mapping_sha,
                    settlement_definition=settlement_definition,
                    system_ids=selected_system_ids,
                    threshold_profile=threshold_profile,
                    as_of_utc=as_of_utc,
                ),
                holdout_end_date=common_holdout[-1],
            )
        oos_pass = bool(oos_gate["pass"])
        cross_candidates.append({
            "state": (
                "PRODUCTION_NO_LIVE_OOS_PASS"
                if oos_pass
                else "OOS_REJECTED"
            ),
            # Which gate path decided the state, for review.  Deliberately a
            # sibling of ``oos_metrics``: the evaluation row embeds it, the
            # selection identity never sees it.
            "oos_gate": oos_gate,
            "oos_event_temporal_scope": HISTORICAL_OOS_TEMPORAL_SCOPE,
            "future_event_count": 0,
            "station_id": station,
            "season_bin": season_bin,
            "lead_bucket": lead_bucket,
            "run_cycle": run_cycle,
            "run_cycle_scope": run_cycle_scope,
            "run_cycle_by_system": {
                system_id: cycle_by_system[system_id]
                for system_id in selected_system_ids
            },
            "run_cycle_by_system_sha256": cycle_mapping_sha,
            "band_table_sha256": band_table_sha,
            "center_width_sha256": center_width_sha,
            "station_width_sha256": station_width_sha,
            "radiation_shift_sigma": scope_radiation_sigma,
            "p0_exact_match_ready": True,
            "p0_exact_match_blocker": None,
            "settlement_definition": settlement_definition,
            "settlement_timezone": cases_by_scope[(
                selected_system_ids[0],
                season_bin,
                lead_bucket,
                cycle_by_system[selected_system_ids[0]],
            )][0]["timezone_name"],
            "selected_system_ids": selected_system_ids,
            "selected_system_count": len(selected_system_ids),
            "ready_system_ids": sorted(
                system_id
                for system_id, _run_cycle, _evaluation in ready_rows
            ),
            "excluded_ready_system_ids": sorted(
                system_id
                for system_id, _run_cycle, _evaluation in ready_rows
                if system_id not in set(selected_system_ids)
            ),
            "cross_selection_rule": (
                "PRE_REGISTERED_SYSTEM_MASK::"
                + ("ALL_READY_OFFICIAL" if system_mask is None else
                   _json(sorted(str(value) for value in system_mask)))
                + "::MAX_EXACT_SHARED_STRICT_HOLDOUT"
                + (
                    "::STABLE_POOL_SEAT" if pool_mode
                    else "::SERVED_SET_REFIT" if pinned_subset is not None
                    else "::MIN_CRPS_SUBSET"
                )
            ),
            "subset_selection": subset_selection,
            "system_transport_provider_ids": {
                system_id: str(
                    evaluation["transport_provider_id"]
                )
                for system_id, evaluation in rows
                if system_id in weights
            },
            # This is an explicit lineage flag, not a probability or an
            # authority upgrade.  It lets the ledger distinguish a governed
            # Open-Meteo no-live fallback from an all-native candidate while
            # keeping the official transport preferred whenever both cover
            # the same exact scope.
            "republisher_fallback_used": any(
                str(evaluation["transport_provider_id"]) == "OPEN_METEO"
                for _system_id, evaluation in rows
                if _system_id in weights
            ),
            "source_role_by_system": {
                system_id: (
                    "aggregator_republisher"
                    if str(evaluation["transport_provider_id"]) == "OPEN_METEO"
                    else "official_native_archive"
                )
                for system_id, evaluation in (
                    # A provisional seat is absent from ``rows`` -- it has no
                    # certified evaluation -- but it is in ``system_weights``,
                    # so a role map built only from ``rows`` would name a
                    # blended system nowhere in it.
                    list(rows)
                    + [
                        (system_id, evaluation)
                        for system_id, _cycle, evaluation in selected_ready_rows
                        if system_id in provisional_system_ids
                    ]
                )
                if system_id in weights
            },
            "independence_family_ids": sorted(
                set(family_by_system.values())
            ),
            "system_family_ids": dict(sorted(
                family_by_system.items()
            )),
            "independent_family_count": len(
                set(family_by_system.values())
            ),
            "available_system_set_hash": content_sha256(
                selected_system_ids
            ),
            "available_family_set_hash":
                native_ensemble_family_set_hash(
                    list(family_by_system.values())
                ),
            # ``system_weights`` is what P0 blends.  When a provisional seat is
            # present these are NOT the weights the metrics above were computed
            # from, and that has to be stated rather than inferred: the gate
            # certified ``certified_system_weights`` on the shared holdout, and
            # the systems named here as provisional were never scored on it.
            # A reader comparing the two sets can see exactly how much of the
            # served blend the gate actually covers.
            "provisional_system_ids": sorted(provisional_system_ids),
            "certified_system_weights": certified_weights,
            "system_weights": weights,
            "family_weights": weight_receipt["family_weights"],
            "family_weight_caps": family_caps_exact,
            "train_start_date": min(
                evaluation["train_start_date"]
                for _system, evaluation in rows
            ),
            "train_end_date": max(
                evaluation["train_end_date"]
                for _system, evaluation in rows
            ),
            "holdout_start_date": common_holdout[0],
            "holdout_end_date": common_holdout[-1],
            "rolling_origin_fold_count": len(common_holdout),
            "effective_sample_n": float(len(common_holdout)),
            "oos_metrics": cross_metrics,
            "shape_layer_ablation": shape_layer_ablation,
            "equal_weight_diagnostic": equal_weight_diagnostic,
            "climatology_baseline_metrics": climate_metrics,
            "cross_system_residual_correlation": correlations,
            "predictions": cross_predictions,
            # Which fit each seated member was scored with in this cohort.
            # Informational: outside every identity, so an artifact whose
            # cohort formed on the shared window is addressed exactly as one
            # that formed on the identical one.
            "cohort_fit_basis_by_system": {
                system_id: cohort_fit_basis_by_system[system_id]
                for system_id in sorted(weights)
            },
            "system_evaluations": {
                system_id: original_by_system[system_id]
                for system_id, _evaluation in rows
                if system_id in weights
            },
            "threshold_profile": threshold_profile,
            "min_train_days": min_train_days,
            "min_holdout_days": min_holdout_days,
            "formal_promotion_min_train_days":
                DEFAULT_MIN_TRAIN_DAYS,
            "formal_promotion_min_holdout_days":
                DEFAULT_MIN_HOLDOUT_DAYS,
            "formal_p0_eligible": False,
            "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
        })
    # Charter 62 boundary continuity: every candidate and ready system is
    # emitted once per station-local season label its validity window will
    # serve, so the first event date past a season boundary finds scopes
    # already certified instead of an empty vocabulary.  This replaces the
    # calendar cliff (all 45 stations losing P0 at once on September 1)
    # with nothing: the fit is label-blind, so the clones are the same
    # arithmetic under every label they serve.  The UTC day is padded one
    # day each way so the station's local calendar can never fall outside
    # the window.
    serve_window_start = (
        as_of.astimezone(timezone.utc).date() - timedelta(days=1)
    ).isoformat()
    serve_window_end = (
        as_of.astimezone(timezone.utc).date()
        + timedelta(days=DEFAULT_VALID_DAYS + 1)
    ).isoformat()
    serve_labels = season_bins_for_window(
        station, serve_window_start, serve_window_end
    )
    primary_labels = {
        str(row["season_bin"]) for row in ready_system_evaluations
    } | {
        str(candidate["season_bin"]) for candidate in cross_candidates
    }
    original_ready = list(ready_system_evaluations)
    original_candidates = list(cross_candidates)
    for extra_label in serve_labels:
        if extra_label in primary_labels:
            continue
        for ready in original_ready:
            clone = copy.deepcopy(ready)
            clone["season_bin"] = extra_label
            ready_system_evaluations.append(clone)
        for candidate in original_candidates:
            clone = copy.deepcopy(candidate)
            clone["season_bin"] = extra_label
            cross_candidates.append(clone)
    identity = {
        "schema": SCHEMA,
        "station_id": station,
        "as_of_utc": as_of_utc,
        "min_train_days": min_train_days,
        "min_holdout_days": min_holdout_days,
        "threshold_profile": threshold_profile,
        "historical_event_date_cutoff": historical_event_date_cutoff,
        "formal_promotion_threshold_profile":
            FORMAL_PROMOTION_THRESHOLD_PROFILE,
        "formal_promotion_min_train_days": DEFAULT_MIN_TRAIN_DAYS,
        "formal_promotion_min_holdout_days":
            DEFAULT_MIN_HOLDOUT_DAYS,
        "registered_system_count": len(systems),
        "readiness": sorted(
            readiness,
            key=lambda row: (
                row["ensemble_system_id"],
                row["season_bin"],
                row["lead_bucket"],
                row["run_cycle"],
            ),
        ),
        "ready_system_evaluations": sorted(
            ready_system_evaluations,
            key=lambda row: (
                row["ensemble_system_id"],
                row["season_bin"],
                row["lead_bucket"],
                row["run_cycle"],
            ),
        ),
        "cross_candidates": cross_candidates,
        "probability_pathway": P0_PROBABILITY_PATHWAY,
        "formal_p0_eligible": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "live_submit_authorized": False,
    }
    return {
        **identity,
        "content_sha256": content_sha256(identity),
    }


def _insert_content_row(
    conn: sqlite3.Connection,
    *,
    table: str,
    primary_key: str,
    row: Mapping[str, Any],
) -> int:
    existing = conn.execute(
        f"SELECT content_sha256 FROM {table} WHERE {primary_key}=?",
        (row[primary_key],),
    ).fetchone()
    if existing is not None:
        if str(existing[0]) != str(row["content_sha256"]):
            raise ContractError(
                f"native_ensemble_oos_{table}_content_conflict"
            )
        return 0
    columns = tuple(row)
    conn.execute(
        f"INSERT INTO {table}({','.join(columns)}) "
        f"VALUES({','.join('?' for _ in columns)})",
        tuple(row[column] for column in columns),
    )
    return 1


def _selection_identity(
    candidate: Mapping[str, Any],
) -> dict[str, Any]:
    _mapping, mapping_sha, run_cycle, run_cycle_scope = (
        _candidate_cycle_mapping(candidate)
    )
    mapping_json = _json(_mapping)
    return {
        "station_id": candidate["station_id"],
        "season_bin": candidate["season_bin"],
        "lead_bucket": candidate["lead_bucket"],
        "run_cycle": run_cycle,
        "run_cycle_by_system_json": mapping_json,
        "run_cycle_by_system_sha256": mapping_sha,
        "settlement_definition": candidate[
            "settlement_definition"
        ],
        "method_version": (
            "strict_forward_member_family_capped_v2_transport_exact::"
            + content_sha256(
                candidate["system_transport_provider_ids"]
            )
            + "::cycle_scope="
            + run_cycle_scope
            + "::run_cycle_by_system_sha256="
            + mapping_sha
            + "::run_cycle_by_system="
            + mapping_json
            + "::"
            + candidate["threshold_profile"]
        ),
        "candidate_system_ids_json": _json(
            candidate["selected_system_ids"]
        ),
        "selected_system_ids_json": _json(
            candidate["selected_system_ids"]
        ),
        "selected_system_count": candidate["selected_system_count"],
        "independent_family_count": candidate[
            "independent_family_count"
        ],
        "selected_weights_json": _json(candidate["system_weights"]),
        "family_weights_json": _json(candidate["family_weights"]),
        "family_weight_caps_json": _json(
            candidate["family_weight_caps"]
        ),
        "supported_exact_masks_json": _json([
            candidate["selected_system_ids"]
        ]),
        "train_start_date": candidate["train_start_date"],
        "train_end_date": candidate["train_end_date"],
        "holdout_start_date": candidate["holdout_start_date"],
        "holdout_end_date": candidate["holdout_end_date"],
        "rolling_origin_fold_count": candidate[
            "rolling_origin_fold_count"
        ],
        "effective_sample_n": candidate["effective_sample_n"],
        "oos_metrics_json": _json(candidate["oos_metrics"]),
        "cross_system_residual_correlation_json": _json(
            candidate["cross_system_residual_correlation"]
        ),
        "deterministic_baseline_metrics_json": _json(
            candidate["climatology_baseline_metrics"]
        ),
    }


def _evaluation_identity(
    candidate: Mapping[str, Any],
    *,
    selection_evidence_sha256: str,
    evaluated_at_utc: str,
) -> dict[str, Any]:
    """Return the immutable OOS result for one exact cross-system mask."""

    (
        cycle_by_system,
        cycle_mapping_sha,
        run_cycle,
        _run_cycle_scope,
    ) = _candidate_cycle_mapping(candidate)
    state = str(candidate.get("state") or "")
    if state not in {
        "PRODUCTION_NO_LIVE_OOS_PASS",
        "OOS_REJECTED",
    }:
        raise ContractError("native_ensemble_oos_evaluation_state_invalid")
    selected_system_ids = sorted(
        str(value) for value in candidate["selected_system_ids"]
    )
    available_system_set_hash = str(
        candidate.get("available_system_set_hash") or ""
    )
    if (
        not selected_system_ids
        or available_system_set_hash != content_sha256(selected_system_ids)
        or str(selection_evidence_sha256 or "")
        != content_sha256(_selection_identity(candidate))
    ):
        raise ContractError("native_ensemble_oos_evaluation_identity_invalid")
    return {
        "station_id": candidate["station_id"],
        "season_bin": candidate["season_bin"],
        "lead_bucket": candidate["lead_bucket"],
        "run_cycle": run_cycle,
        "run_cycle_by_system_json": _json(cycle_by_system),
        "run_cycle_by_system_sha256": cycle_mapping_sha,
        "settlement_definition": candidate["settlement_definition"],
        "ensemble_system_ids_json": _json(selected_system_ids),
        "available_system_set_hash": available_system_set_hash,
        "selection_evidence_sha256": selection_evidence_sha256,
        "oos_state": state,
        "threshold_profile": candidate["threshold_profile"],
        "min_train_days": candidate["min_train_days"],
        "min_holdout_days": candidate["min_holdout_days"],
        "train_end_date": candidate["train_end_date"],
        "holdout_start_date": candidate["holdout_start_date"],
        "holdout_end_date": candidate["holdout_end_date"],
        # The gate record and the per-layer ablation ride only here: the
        # evaluation row is a new append-only fact each scan, so embedding
        # them is free, while the selection identity must keep the gate-free
        # bytes its activation pointers were bound under.  Anything added to
        # this dict must stay out of ``_selection_identity`` for that reason,
        # and out of the table's own columns, which no reader migrates for a
        # diagnostic.
        "oos_metrics_json": _json({
            **candidate["oos_metrics"],
            **(
                {"gate": candidate["oos_gate"]}
                if isinstance(candidate.get("oos_gate"), Mapping)
                else {}
            ),
            **(
                {"shape_layer_ablation": candidate["shape_layer_ablation"]}
                if isinstance(
                    candidate.get("shape_layer_ablation"), Mapping
                )
                else {}
            ),
            **(
                {
                    "equal_weight_diagnostic": candidate[
                        "equal_weight_diagnostic"
                    ]
                }
                if isinstance(
                    candidate.get("equal_weight_diagnostic"), Mapping
                )
                else {}
            ),
        }),
        "climatology_baseline_metrics_json": _json(
            candidate["climatology_baseline_metrics"]
        ),
        "evaluated_at_utc": evaluated_at_utc,
    }


def _validate_native_ensemble_oos_report(
    conn: sqlite3.Connection,
    report: Mapping[str, Any],
) -> None:
    if report.get("schema") != SCHEMA:
        raise ContractError("native_ensemble_oos_report_schema_invalid")
    identity = {
        key: value
        for key, value in report.items()
        if key != "content_sha256"
    }
    if str(report.get("content_sha256") or "") != content_sha256(identity):
        raise ContractError("native_ensemble_oos_report_sha_mismatch")
    if native_ensemble_foundation_schema_status(conn)["ok"] is not True:
        raise ContractError(
            "native_ensemble_oos_foundation_schema_missing"
        )
    # Persistence is a second trust boundary.  A signed report may arrive
    # after the read-only builder, so do not let a caller reintroduce a
    # prospective/forward row by editing the in-memory map.  Legacy native
    # reports can omit the explicit scope because their offline archive class
    # is already historical; republisher reports never receive that
    # relaxation and must carry the historical receipt contract pointer.
    for candidate in report.get("cross_candidates") or []:
        if not isinstance(candidate, Mapping):
            raise ContractError("native_ensemble_oos_candidate_invalid")
        temporal_scope = str(
            candidate.get("oos_event_temporal_scope") or ""
        )
        if temporal_scope and temporal_scope != HISTORICAL_OOS_TEMPORAL_SCOPE:
            raise ContractError("native_ensemble_oos_forward_scope_forbidden")
        try:
            future_event_count = int(
                candidate.get("future_event_count", 0) or 0
            )
        except (TypeError, ValueError) as exc:
            raise ContractError(
                "native_ensemble_oos_future_event_count_invalid"
            ) from exc
        if future_event_count != 0:
            raise ContractError(
                "native_ensemble_oos_future_event_count_nonzero"
            )
        evaluations = candidate.get("system_evaluations")
        transports = candidate.get("system_transport_provider_ids")
        if not isinstance(evaluations, Mapping) or not isinstance(transports, Mapping):
            raise ContractError(
                "native_ensemble_oos_historical_lineage_missing"
            )
        for system_id, raw_evaluation in evaluations.items():
            if not isinstance(raw_evaluation, Mapping):
                raise ContractError(
                    "native_ensemble_oos_system_evaluation_invalid"
                )
            provider = str(
                transports.get(system_id)
                or raw_evaluation.get("transport_provider_id")
                or ""
            )
            holdout_class = str(
                raw_evaluation.get("holdout_source_class") or ""
            )
            if holdout_class == "FORWARD_LOCAL_RECEIPT":
                raise ContractError(
                    "native_ensemble_oos_forward_holdout_forbidden"
                )
            if provider == "OPEN_METEO":
                if (
                    temporal_scope != HISTORICAL_OOS_TEMPORAL_SCOPE
                    or holdout_class != HISTORICAL_REPUBLISHER_RECEIPT
                ):
                    raise ContractError(
                        "native_ensemble_oos_republisher_history_required"
                    )
                for prediction in raw_evaluation.get("predictions") or []:
                    if (
                        not isinstance(prediction, Mapping)
                        or not prediction.get("historical_oos_contract_ids")
                    ):
                        raise ContractError(
                            "native_ensemble_oos_republisher_contract_missing"
                        )
            elif holdout_class not in {
                "",
                "OFFLINE_CHRONOLOGICAL_NATIVE_ARCHIVE",
            }:
                raise ContractError(
                    "native_ensemble_oos_native_holdout_lineage_invalid"
                )


def persist_native_ensemble_oos_evaluations(
    conn: sqlite3.Connection,
    report: Mapping[str, Any],
    *,
    evaluated_at_utc: str,
    _manage_savepoint: bool = True,
) -> dict[str, Any]:
    """Append PASS and REJECTED evidence without creating activations.

    This is deliberately separate from the OOS builder and activation writer:
    a completed signed manifest can be ingested later without recomputing,
    downloading, or widening model authority.
    """

    _validate_native_ensemble_oos_report(conn, report)
    try:
        datetime.fromisoformat(evaluated_at_utc.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(
            "native_ensemble_oos_evaluation_timestamp_invalid"
        ) from exc
    if _manage_savepoint:
        conn.execute("SAVEPOINT native_ensemble_oos_evaluations")
    count = 0
    try:
        for candidate in report.get("cross_candidates") or []:
            selection_sha = content_sha256(_selection_identity(candidate))
            evaluation_identity = _evaluation_identity(
                candidate,
                selection_evidence_sha256=selection_sha,
                evaluated_at_utc=evaluated_at_utc,
            )
            evaluation_sha = content_sha256(evaluation_identity)
            evaluation_row = {
                "evaluation_id": (
                    "native-oos-evaluation-" + evaluation_sha[:24]
                ),
                **evaluation_identity,
                "content_sha256": evaluation_sha,
            }
            count += _insert_content_row(
                conn,
                table="calibration_cross_native_ensemble_oos_evaluation",
                primary_key="evaluation_id",
                row=evaluation_row,
            )
    except Exception:
        if _manage_savepoint:
            conn.execute("ROLLBACK TO native_ensemble_oos_evaluations")
            conn.execute("RELEASE native_ensemble_oos_evaluations")
        raise
    if _manage_savepoint:
        conn.execute("RELEASE native_ensemble_oos_evaluations")
    return {
        "schema": "native_ensemble_oos_evaluation_persist_v1",
        "state": "COMMITTED" if count else "NOOP_IDEMPOTENT",
        "oos_evaluations": count,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "live_submit_authorized": False,
    }


def _system_artifact_identity(
    *,
    system_id: str,
    system_run_cycle: str,
    scope: Mapping[str, Any],
    evaluation: Mapping[str, Any],
    activated_at_utc: str,
    valid_until: str,
) -> dict[str, Any]:
    """The append-only calibration row for one system in one scope.

    Shared by the two callers that write it: the winners inside a cross
    candidate, and every other system whose evaluation reached OOS_READY.
    Both must produce byte-identical rows for the same inputs -- a second
    copy of this construction is how a bound that has already been
    corrected once comes back (see the ICON spread bound, 7ec2e22f).

    ``scope`` supplies station/season/lead/settlement and the threshold
    profile; a cross candidate and the report itself both satisfy it.
    """

    if not system_run_cycle:
        raise ContractError(
            "native_ensemble_oos_run_cycle_mapping_missing"
        )
    # The served row carries the serving refit (whole rolling window through
    # the newest closed day); the gate's train-only fit stays in
    # ``evaluation["parameters"]`` for the weights and the evidence.  An
    # evaluation without a serving fit (older callers, replays) serves what
    # it always did.
    parameters = (
        evaluation.get("serving_parameters") or evaluation["parameters"]
    )
    transport_provider_id = str(
        evaluation["transport_provider_id"]
    )
    official_native_archive = (
        transport_provider_id != "OPEN_METEO"
    )
    train_source_counts = evaluation[
        "train_source_counts"
    ]
    source_authority_scopes = (
        ["CALIBRATION_TRAIN_ONLY"]
        if official_native_archive
        else sorted({
            *(
                ["CALIBRATION_TRAIN_ONLY"]
                if int(train_source_counts[
                    "RETROSPECTIVE_ARCHIVE_TRAIN_ONLY"
                ]) > 0
                else []
            ),
            *(
                ["CALIBRATION_TRAIN_ONLY"]
                if int(train_source_counts.get(
                    HISTORICAL_REPUBLISHER_RECEIPT,
                    0,
                )) > 0
                else []
            ),
            *(
                ["PRODUCTION_NO_LIVE_CANDIDATE"]
                if int(train_source_counts[
                    "FORWARD_LOCAL_RECEIPT"
                ]) > 0
                else []
            ),
        })
    )
    system_identity = {
        "ensemble_system_id": system_id,
        "station_id": scope["station_id"],
        "season_bin": scope["season_bin"],
        "lead_bucket": scope["lead_bucket"],
        "run_cycle": system_run_cycle,
        "settlement_definition": scope["settlement_definition"],
        "calibration_method": (
            "member_bias_spread_reliability_v1"
        ),
        "bias_parameters_json": _json({
            "bias_c": parameters["bias_c"],
            "correction_form": parameters["correction_form"],
            "bias_bin_cuts_c": parameters["bias_bin_cuts_c"],
            "bias_bin_shifts_c": parameters["bias_bin_shifts_c"],
        }),
        "spread_parameters_json": _json({
            "spread_factor": parameters["spread_factor"],
            "train_rmse_c": parameters["train_rmse_c"],
            "train_residual_sigma_c": parameters[
                "train_residual_sigma_c"
            ],
            "train_mean_spread_c": parameters[
                "train_mean_spread_c"
            ],
        }),
        "reliability_parameters_json": _json({
            "reliability_weight": parameters[
                "reliability_weight"
            ],
            "climate_samples_c": parameters[
                "climate_samples_c"
            ],
            "train_n_effective": parameters[
                "train_n_effective"
            ],
            "train_source_counts": evaluation[
                "train_source_counts"
            ],
            "holdout_source_class": evaluation[
                "holdout_source_class"
            ],
            "oos_event_temporal_scope": evaluation.get(
                "oos_event_temporal_scope"
            ),
            "future_event_count": int(
                evaluation.get("future_event_count", 0)
            ),
            "transport_provider_id":
                transport_provider_id,
            "source_authority_scopes":
                source_authority_scopes,
            "official_native_archive":
                official_native_archive,
            "oos_threshold_profile": scope["threshold_profile"],
            "oos_min_train_days": scope["min_train_days"],
            "oos_min_holdout_days": scope["min_holdout_days"],
            "formal_promotion_min_train_days": scope["formal_promotion_min_train_days"],
            "formal_promotion_min_holdout_days": scope["formal_promotion_min_holdout_days"],
            "formal_p0_eligible": False,
            # Subscripted, like every other fitted key in these three
            # columns.  ``.get`` was the odd one out and it cost the pair
            # register its voice: ``test_paired_contracts`` reads this
            # writer's SOURCE for the keys it persists -- deliberately, so
            # the register cannot drift the way the thing it guards did --
            # and its pattern only sees ``parameters["..."]``.  So the one
            # contract written to catch a fitted key nobody persists sat red
            # from 2026-09-02 over two keys that were persisted all along,
            # and a red contract is one nobody reads when it goes red for a
            # real reason.
            #
            # The stronger form is also the correct one here.  These two are
            # produced unconditionally by ``_fit_parameters`` and both
            # ``parameters`` and ``serving_parameters`` carry them (pinned in
            # test_native_ensemble_serving_refit).  Measured on the store:
            # every one of the 21,000+ artifacts written since 2026-09-03
            # carries both, across both writer paths, so a missing key here
            # is not a case that happens -- it is a fit that changed shape
            # without this column being told, and that should raise rather
            # than write a silent null the OOS gate would score.
            "fit_through_date": parameters["fit_through_date"],
            "fit_case_count": parameters["fit_case_count"],
        }),
        "member_qc_contract_sha256": content_sha256({
            "expected_complete_member_set": True,
            "transport_provider_id":
                transport_provider_id,
            "source_authority_scopes":
                source_authority_scopes,
            "aggregator_or_republisher": (
                transport_provider_id == "OPEN_METEO"
            ),
            "official_native_archive":
                official_native_archive,
            "native_run_init_required":
                official_native_archive,
            "retrospective_archive_training_only": (
                not official_native_archive
            ),
            "retrospective_archive_holdout_eligible": (
                evaluation.get("holdout_source_class")
                == HISTORICAL_REPUBLISHER_RECEIPT
            ),
            "offline_chronological_oos_eligible":
                official_native_archive,
            # These two facts are native-archive timing facts;
            # do not assert them for a retrospective Open-Meteo
            # packet.  The republisher path is instead bound by
            # its explicit CLOSED_HISTORICAL_AWC_TRUTH contract.
            "holdout_snapshot_before_day_end": (
                official_native_archive
            ),
            "holdout_snapshot_before_truth_commit": (
                official_native_archive
            ),
            "historical_republisher_contract_bound": (
                evaluation.get("holdout_source_class")
                == HISTORICAL_REPUBLISHER_RECEIPT
            ),
        }),
        "train_start_date": evaluation["train_start_date"],
        "train_end_date": evaluation["train_end_date"],
        "holdout_start_date": evaluation[
            "holdout_start_date"
        ],
        "holdout_end_date": evaluation["holdout_end_date"],
        "rolling_origin_fold_count": int(
            evaluation["holdout_n"]
        ),
        # Kish effective days under the half-life weights, not the raw day
        # count -- the number the reliability blend actually rests on.
        "effective_sample_n": float(
            evaluation["parameters"].get("train_n_effective")
            or evaluation["train_n"]
        ),
        "oos_metrics_json": _json(
            evaluation["oos_metrics"]
        ),
        "valid_from_utc": activated_at_utc,
        "valid_until_utc": valid_until,
    }
    return system_identity


_SYSTEM_ARTIFACT_CLOCK_FIELDS = ("valid_from_utc", "valid_until_utc")


def _reusable_system_artifact_id(
    conn: sqlite3.Connection,
    identity: Mapping[str, Any],
    *,
    activated_at_utc: str,
) -> str | None:
    """The scope's newest system artifact, when it says what ``identity`` says.

    ``_system_artifact_identity`` carries ``valid_from_utc`` and
    ``valid_until_utc`` inside the content it is addressed by, so two persists
    of the same evaluation a minute apart minted two artifacts, and
    ``_activate_system_artifact`` -- which advances the generation only when
    the artifact id changes -- advanced it every time.  Measured on the
    production store 2026-09-06: 2,600-3,900 system artifacts a day, 70-190
    an hour around the clock, 60,132 rows, while the evaluation behind them
    changes once a day when the rolling window moves.  The cross artifact
    already reuses on identical selection evidence; this is the same rule for
    the system layer.

    Reusable when the newest artifact for the same system and scope carries
    the same evidence -- every identity field but the two clock fields -- and
    is still valid at ``activated_at_utc``.  An expired one is not reused, so
    a served artifact is never left to run out under an activation that still
    points at it; the next persist mints afresh, exactly as before.
    """

    evidence_columns = [
        key for key in identity if key not in _SYSTEM_ARTIFACT_CLOCK_FIELDS
    ]
    row = conn.execute(
        "SELECT system_artifact_id,valid_until_utc,"
        + ",".join(evidence_columns)
        + " FROM calibration_native_ensemble_system_artifact"
        " WHERE ensemble_system_id=? AND station_id=? AND season_bin=?"
        " AND lead_bucket=? AND run_cycle=? AND settlement_definition=?"
        " ORDER BY valid_from_utc DESC, system_artifact_id LIMIT 1",
        (
            str(identity["ensemble_system_id"]),
            str(identity["station_id"]),
            str(identity["season_bin"]),
            str(identity["lead_bucket"]),
            str(identity["run_cycle"]),
            str(identity["settlement_definition"]),
        ),
    ).fetchone()
    if row is None:
        return None
    try:
        still_valid = datetime.fromisoformat(
            str(row[1]).replace("Z", "+00:00")
        ) > datetime.fromisoformat(
            str(activated_at_utc).replace("Z", "+00:00")
        )
    except ValueError:
        return None
    if not still_valid:
        return None
    for column, stored in zip(evidence_columns, row[2:]):
        wanted = identity[column]
        if isinstance(wanted, float):
            try:
                if not math.isclose(float(stored), wanted, rel_tol=0.0, abs_tol=1e-9):
                    return None
            except (TypeError, ValueError):
                return None
        elif isinstance(wanted, bool) or isinstance(wanted, int):
            try:
                if int(stored) != int(wanted):
                    return None
            except (TypeError, ValueError):
                return None
        elif str(stored) != str(wanted):
            return None
    return str(row[0])


def _activate_system_artifact(
    conn: sqlite3.Connection,
    *,
    artifact_id: str,
    station_id: str,
    system_id: str,
    season_bin: str,
    lead_bucket: str,
    system_run_cycle: str,
    settlement_definition: str,
    activated_at_utc: str,
    valid_until: str,
) -> tuple[str, int]:
    """Point the scope's activation at ``artifact_id``, or reuse it.

    Returns the activation id and how many rows this wrote, so a caller can
    keep its own counts.  Generation only advances when the artifact
    actually changed, which keeps a re-persist of identical evidence from
    inflating the chain the P0 compile reads newest-first.
    """

    existing = conn.execute(
        "SELECT activation_id,artifact_id "
        "FROM calibration_native_ensemble_activation "
        "WHERE artifact_kind='NATIVE_ENSEMBLE_SYSTEM_CALIBRATION' "
        "AND station_id=? AND ensemble_system_id_or_star=? "
        "AND season_bin=? AND lead_bucket=? AND run_cycle=? "
        "AND available_system_set_hash_or_star='*' "
        "AND run_cycle_by_system_json=? "
        "AND run_cycle_by_system_sha256=? "
        "ORDER BY generation DESC LIMIT 1",
        (
            station_id,
            system_id,
            season_bin,
            lead_bucket,
            system_run_cycle,
            _json({system_id: system_run_cycle}),
            content_sha256({system_id: system_run_cycle}),
        ),
    ).fetchone()
    if existing is not None and str(existing[1]) == artifact_id:
        return str(existing[0]), 0
    generation = 1 + int(conn.execute(
        "SELECT COALESCE(MAX(generation),0) "
        "FROM calibration_native_ensemble_activation "
        "WHERE artifact_kind='NATIVE_ENSEMBLE_SYSTEM_CALIBRATION' "
        "AND station_id=? AND ensemble_system_id_or_star=? "
        "AND season_bin=? AND lead_bucket=? AND run_cycle=? "
        "AND available_system_set_hash_or_star='*'",
        (
            station_id,
            system_id,
            season_bin,
            lead_bucket,
            system_run_cycle,
        ),
    ).fetchone()[0])
    identity = {
        "artifact_kind": "NATIVE_ENSEMBLE_SYSTEM_CALIBRATION",
        "artifact_id": artifact_id,
        "station_id": station_id,
        "ensemble_system_id_or_star": system_id,
        "season_bin": season_bin,
        "lead_bucket": lead_bucket,
        "run_cycle": system_run_cycle,
        "run_cycle_by_system_json": _json({
            system_id: system_run_cycle,
        }),
        "run_cycle_by_system_sha256": content_sha256({
            system_id: system_run_cycle,
        }),
        "settlement_definition": settlement_definition,
        "available_system_set_hash_or_star": "*",
        "generation": generation,
        "valid_from_utc": activated_at_utc,
        "valid_until_utc": valid_until,
        "activated_at_utc": activated_at_utc,
        "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
    }
    sha = content_sha256(identity)
    activation_id = "native-activation-" + sha[:24]
    written = _insert_content_row(
        conn,
        table="calibration_native_ensemble_activation",
        primary_key="activation_id",
        row={
            "activation_id": activation_id,
            **identity,
            "content_sha256": sha,
        },
    )
    return activation_id, written


def persist_native_ensemble_oos(
    conn: sqlite3.Connection,
    report: Mapping[str, Any],
    *,
    activated_at_utc: str,
    valid_days: int = DEFAULT_VALID_DAYS,
) -> dict[str, Any]:
    """Persist passing candidate artifacts and no-live activations."""

    _validate_native_ensemble_oos_report(conn, report)
    if valid_days < 1 or valid_days > 90:
        raise ContractError("native_ensemble_oos_valid_days_invalid")
    activated = datetime.fromisoformat(
        activated_at_utc.replace("Z", "+00:00")
    )
    valid_until = (
        activated + timedelta(days=valid_days)
    ).isoformat().replace("+00:00", "Z")
    counts = {
        "system_artifacts": 0,
        "selection_evidence": 0,
        "cross_artifacts": 0,
        "oos_evaluations": 0,
        "oos_predictions": 0,
        "activations": 0,
    }
    receipts = []
    conn.execute("SAVEPOINT native_ensemble_oos_persist")
    try:
        counts["oos_evaluations"] = int(
            persist_native_ensemble_oos_evaluations(
                conn,
                report,
                evaluated_at_utc=activated_at_utc,
                _manage_savepoint=False,
            )["oos_evaluations"]
        )
        # Every system that reached OOS_READY gets its calibration stored,
        # not only the ones a candidate seated.  Written before the
        # candidates so a system that is both ready and seated ends up with
        # the candidate's row: identical inputs give an identical sha, so
        # the second write is a no-op rather than a duplicate.
        for ready in report.get("ready_system_evaluations") or []:
            ready_identity = _system_artifact_identity(
                system_id=str(ready["ensemble_system_id"]),
                system_run_cycle=str(ready["run_cycle"]),
                scope={
                    "station_id": report["station_id"],
                    "season_bin": ready["season_bin"],
                    "lead_bucket": ready["lead_bucket"],
                    "settlement_definition": ready[
                        "settlement_definition"
                    ],
                    "threshold_profile": report["threshold_profile"],
                    "min_train_days": report["min_train_days"],
                    "min_holdout_days": report["min_holdout_days"],
                    "formal_promotion_min_train_days": report[
                        "formal_promotion_min_train_days"
                    ],
                    "formal_promotion_min_holdout_days": report[
                        "formal_promotion_min_holdout_days"
                    ],
                },
                evaluation=ready["evaluation"],
                activated_at_utc=activated_at_utc,
                valid_until=valid_until,
            )
            ready_sha = content_sha256(ready_identity)
            reused_ready_id = _reusable_system_artifact_id(
                conn, ready_identity, activated_at_utc=activated_at_utc,
            )
            if reused_ready_id is not None:
                ready_artifact_id = reused_ready_id
            else:
                ready_artifact_id = "native-system-artifact-" + ready_sha[:24]
                counts["system_artifacts"] += _insert_content_row(
                    conn,
                    table="calibration_native_ensemble_system_artifact",
                    primary_key="system_artifact_id",
                    row={
                        "system_artifact_id": ready_artifact_id,
                        **ready_identity,
                        "content_sha256": ready_sha,
                    },
                )
            counts["activations"] += _activate_system_artifact(
                conn,
                artifact_id=ready_artifact_id,
                station_id=str(report["station_id"]),
                system_id=str(ready["ensemble_system_id"]),
                season_bin=str(ready["season_bin"]),
                lead_bucket=str(ready["lead_bucket"]),
                system_run_cycle=str(ready["run_cycle"]),
                settlement_definition=str(
                    ready["settlement_definition"]
                ),
                activated_at_utc=activated_at_utc,
                valid_until=valid_until,
            )[1]
        for candidate in report.get("cross_candidates") or []:
            cycle_by_system, cycle_mapping_sha, candidate_run_cycle, (
                _candidate_run_cycle_scope
            ) = _candidate_cycle_mapping(candidate)
            cycle_mapping_json = _json(cycle_by_system)
            candidate_selection_identity = _selection_identity(candidate)
            candidate_selection_sha = content_sha256(
                candidate_selection_identity
            )
            if candidate.get("state") != "PRODUCTION_NO_LIVE_OOS_PASS":
                continue
            reusable = conn.execute(
                "SELECT a.activation_id,a.artifact_id,"
                "c.selection_evidence_sha256,a.valid_until_utc "
                "FROM calibration_native_ensemble_activation a "
                "JOIN calibration_cross_native_ensemble_artifact c "
                "ON c.cross_ensemble_artifact_id=a.artifact_id "
                "WHERE a.artifact_kind='CROSS_NATIVE_ENSEMBLE' "
                "AND a.station_id=? AND a.season_bin=? "
                "AND a.lead_bucket=? AND a.run_cycle=? "
                "AND a.available_system_set_hash_or_star=? "
                "AND a.run_cycle_by_system_json=? "
                "AND a.run_cycle_by_system_sha256=? "
                "AND c.run_cycle_by_system_json=? "
                "AND c.run_cycle_by_system_sha256=? "
                "AND a.authority_scope='PRODUCTION_NO_LIVE_ONLY' "
                "ORDER BY a.generation DESC LIMIT 1",
                (
                    candidate["station_id"],
                    candidate["season_bin"],
                    candidate["lead_bucket"],
                    candidate_run_cycle,
                    candidate["available_system_set_hash"],
                    cycle_mapping_json,
                    cycle_mapping_sha,
                    cycle_mapping_json,
                    cycle_mapping_sha,
                ),
            ).fetchone()
            if (
                reusable is not None
                and str(reusable[2]) == candidate_selection_sha
                and datetime.fromisoformat(
                    str(reusable[3]).replace("Z", "+00:00")
                )
                > activated
            ):
                selection_row = conn.execute(
                    "SELECT selection_evidence_id FROM "
                    "calibration_cross_native_ensemble_selection_evidence "
                    "WHERE content_sha256=?",
                    (candidate_selection_sha,),
                ).fetchone()
                if selection_row is None:
                    raise ContractError(
                        "native_ensemble_oos_reusable_selection_missing"
                    )
                receipts.append({
                    "station_id": candidate["station_id"],
                    "season_bin": candidate["season_bin"],
                    "lead_bucket": candidate["lead_bucket"],
                    "run_cycle": candidate_run_cycle,
                    "run_cycle_by_system": cycle_by_system,
                    "run_cycle_by_system_sha256": cycle_mapping_sha,
                    "system_artifact_ids": {},
                    "system_activation_ids": {},
                    "selection_evidence_id": str(selection_row[0]),
                    "cross_ensemble_artifact_id": str(reusable[1]),
                    "activation_id": str(reusable[0]),
                    "reused_existing": True,
                })
                continue
            system_artifact_ids: dict[str, str] = {}
            system_activation_ids: dict[str, str] = {}
            for system_id, evaluation in sorted(
                candidate["system_evaluations"].items()
            ):
                system_run_cycle = cycle_by_system.get(system_id)
                system_identity = _system_artifact_identity(
                    system_id=system_id,
                    system_run_cycle=system_run_cycle,
                    scope=candidate,
                    evaluation=evaluation,
                    activated_at_utc=activated_at_utc,
                    valid_until=valid_until,
                )
                system_sha = content_sha256(system_identity)
                reused_system_id = _reusable_system_artifact_id(
                    conn, system_identity, activated_at_utc=activated_at_utc,
                )
                system_row = {
                    "system_artifact_id": (
                        reused_system_id
                        if reused_system_id is not None
                        else "native-system-artifact-" + system_sha[:24]
                    ),
                    **system_identity,
                    "content_sha256": system_sha,
                }
                if reused_system_id is None:
                    counts["system_artifacts"] += _insert_content_row(
                        conn,
                        table="calibration_native_ensemble_system_artifact",
                        primary_key="system_artifact_id",
                        row=system_row,
                    )
                system_artifact_ids[system_id] = str(
                    system_row["system_artifact_id"]
                )
                existing_system_activation = conn.execute(
                    "SELECT activation_id,artifact_id "
                    "FROM calibration_native_ensemble_activation "
                    "WHERE artifact_kind="
                    "'NATIVE_ENSEMBLE_SYSTEM_CALIBRATION' "
                    "AND station_id=? AND ensemble_system_id_or_star=? "
                    "AND season_bin=? AND lead_bucket=? AND run_cycle=? "
                    "AND available_system_set_hash_or_star='*' "
                    "AND run_cycle_by_system_json=? "
                    "AND run_cycle_by_system_sha256=? "
                    "ORDER BY generation DESC LIMIT 1",
                    (
                        candidate["station_id"],
                        system_id,
                        candidate["season_bin"],
                        candidate["lead_bucket"],
                        system_run_cycle,
                        _json({system_id: system_run_cycle}),
                        content_sha256({system_id: system_run_cycle}),
                    ),
                ).fetchone()
                if (
                    existing_system_activation is not None
                    and str(existing_system_activation[1])
                    == str(system_row["system_artifact_id"])
                ):
                    system_activation_ids[system_id] = str(
                        existing_system_activation[0]
                    )
                else:
                    system_generation = 1 + int(conn.execute(
                        "SELECT COALESCE(MAX(generation),0) "
                        "FROM calibration_native_ensemble_activation "
                        "WHERE artifact_kind="
                        "'NATIVE_ENSEMBLE_SYSTEM_CALIBRATION' "
                        "AND station_id=? "
                        "AND ensemble_system_id_or_star=? "
                        "AND season_bin=? AND lead_bucket=? "
                        "AND run_cycle=? "
                        "AND available_system_set_hash_or_star='*'",
                        (
                            candidate["station_id"],
                            system_id,
                            candidate["season_bin"],
                            candidate["lead_bucket"],
                            system_run_cycle,
                        ),
                    ).fetchone()[0])
                    system_activation_identity = {
                        "artifact_kind": (
                            "NATIVE_ENSEMBLE_SYSTEM_CALIBRATION"
                        ),
                        "artifact_id": system_row[
                            "system_artifact_id"
                        ],
                        "station_id": candidate["station_id"],
                        "ensemble_system_id_or_star": system_id,
                        "season_bin": candidate["season_bin"],
                        "lead_bucket": candidate["lead_bucket"],
                        "run_cycle": system_run_cycle,
                        "run_cycle_by_system_json": _json({
                            system_id: system_run_cycle,
                        }),
                        "run_cycle_by_system_sha256": content_sha256({
                            system_id: system_run_cycle,
                        }),
                        "settlement_definition": candidate[
                            "settlement_definition"
                        ],
                        "available_system_set_hash_or_star": "*",
                        "generation": system_generation,
                        "valid_from_utc": activated_at_utc,
                        "valid_until_utc": valid_until,
                        "activated_at_utc": activated_at_utc,
                        "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
                    }
                    system_activation_sha = content_sha256(
                        system_activation_identity
                    )
                    system_activation_row = {
                        "activation_id": (
                            "native-activation-"
                            + system_activation_sha[:24]
                        ),
                        **system_activation_identity,
                        "content_sha256": system_activation_sha,
                    }
                    counts["activations"] += _insert_content_row(
                        conn,
                        table=(
                            "calibration_native_ensemble_activation"
                        ),
                        primary_key="activation_id",
                        row=system_activation_row,
                    )
                    system_activation_ids[system_id] = str(
                        system_activation_row["activation_id"]
                    )
            selection_identity = candidate_selection_identity
            selection_sha = content_sha256(selection_identity)
            selection_row = {
                "selection_evidence_id": (
                    "native-selection-" + selection_sha[:24]
                ),
                **selection_identity,
                "content_sha256": selection_sha,
            }
            counts["selection_evidence"] += _insert_content_row(
                conn,
                table=(
                    "calibration_cross_native_ensemble_selection_evidence"
                ),
                primary_key="selection_evidence_id",
                row=selection_row,
            )
            # Which rule divided this winner's weight (minimum variance, or
            # the inverse-RMSE fallback when the shared train window was too
            # short for a covariance).  The selection identity bytes cannot
            # carry this -- touching them would unbind every active pointer
            # -- and the artifact's combination_method keeps its legacy tag
            # for hash compatibility, so until now the rule lived only in an
            # in-memory receipt and no served scope could say which rule
            # weighted it.  Append-only sidecar, keyed by the selection sha.
            subset_receipt = candidate.get("subset_selection") or {}
            seat_weight_rule = str(subset_receipt.get("weight_rule") or "")
            if seat_weight_rule:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS "
                    "calibration_seat_weight_rule_journal ("
                    " selection_content_sha256 TEXT PRIMARY KEY"
                    "  CHECK(length(selection_content_sha256)=64),"
                    " station_id TEXT NOT NULL,"
                    " season_bin TEXT NOT NULL,"
                    " lead_bucket TEXT NOT NULL,"
                    " run_cycle TEXT NOT NULL,"
                    " weight_rule TEXT NOT NULL,"
                    " evaluated_subset_count INTEGER NOT NULL,"
                    " decided_at_utc TEXT NOT NULL"
                    ")"
                )
                conn.execute(
                    "INSERT OR IGNORE INTO "
                    "calibration_seat_weight_rule_journal("
                    "selection_content_sha256,station_id,season_bin,"
                    "lead_bucket,run_cycle,weight_rule,"
                    "evaluated_subset_count,decided_at_utc"
                    ") VALUES(?,?,?,?,?,?,?,?)",
                    (
                        selection_sha,
                        str(candidate["station_id"]),
                        str(candidate["season_bin"]),
                        str(candidate["lead_bucket"]),
                        str(candidate_run_cycle),
                        seat_weight_rule,
                        int(
                            subset_receipt.get("evaluated_subset_count")
                            or 0
                        ),
                        activated_at_utc,
                    ),
                )
            cross_identity = {
                "station_id": candidate["station_id"],
                "season_bin": candidate["season_bin"],
                "lead_bucket": candidate["lead_bucket"],
                "run_cycle": candidate_run_cycle,
                "run_cycle_by_system_json": cycle_mapping_json,
                "run_cycle_by_system_sha256": cycle_mapping_sha,
                "settlement_definition": candidate[
                    "settlement_definition"
                ],
                "ensemble_system_ids_json": _json(
                    candidate["selected_system_ids"]
                ),
                "ensemble_system_count": candidate[
                    "selected_system_count"
                ],
                "independent_family_count": candidate[
                    "independent_family_count"
                ],
                "supported_exact_masks_json": _json([
                    candidate["selected_system_ids"]
                ]),
                "selection_evidence_sha256": selection_sha,
                "combination_method": (
                    # The prefix names the rule that actually divided this
                    # winner's weight.  It carried the legacy literal
                    # "train_rmse_inverse_family_capped_v1" long after
                    # e1c05256 promoted the covariance seat, and since the
                    # tag string is the only human-readable statement of the
                    # serving rule on the artifact itself, the stale name
                    # kept getting read as the live rule (removed by user
                    # order, 2026-08-21).  Old artifacts keep their bytes --
                    # rewriting them would unbind every active pointer --
                    # and serving keys only on contained tags, never on this
                    # prefix, so the truthful name rolls out with normal
                    # artifact regeneration.
                    str(
                        (candidate.get("subset_selection") or {}).get(
                            "weight_rule"
                        )
                        or "seat_weight_rule_unrecorded"
                    ).lower()
                    # Charter 134: a pool seat names its shrinkage, so the
                    # compile can tell a pool artifact from a contest one and
                    # a later kappa never reads as this one.
                    + (
                        "::stable_pool_kappa="
                        + f"{float((candidate.get('subset_selection') or {}).get('stable_pool_kappa')):g}"
                        if (candidate.get("subset_selection") or {}).get(
                            "stable_pool_kappa"
                        ) is not None
                        else ""
                    )
                    + "::"
                    + candidate["threshold_profile"]
                    + "::cycle_scope="
                    + str(candidate["run_cycle_scope"])
                    + "::run_cycle_by_system_sha256="
                    + str(candidate["run_cycle_by_system_sha256"])
                    # The holdout above scored the bridged blend, so the
                    # artifact says so; P0 keys its own bridging on this tag,
                    # which is what keeps the served math identical to the
                    # gated math while pre-bridge artifacts roll off.
                    + "::" + CONSENSUS_BRIDGE_METHOD_TAG
                    # Same admission pattern as the bridge: the holdout
                    # above scored the conditionally skewed blend with this
                    # day's own signals, so the artifact says so and P0 keys
                    # the serving transform on this tag.
                    # Charter 147: empty while the tilt is retired, so the
                    # certificate says what the holdout actually scored.
                    + neighbour_direction_method_tag_suffix()
                    + "::" + SYNOPTIC_COOLING_METHOD_TAG
                    # Pin the coefficient the holdout above was scored under.
                    # Absent on artifacts built before this existed, which are
                    # served the legacy value and so keep their own math.
                    + "::radiation_shift_sigma="
                    + f"{float(candidate['radiation_shift_sigma']):g}"
                    # Charter 160: the holdout scored the clearness form, so
                    # serving may read this artifact's own pin back.
                    + "::" + RADIATION_CLEARNESS_METHOD_TAG
                    # 2026-09-21: the holdout read each day's radiation as
                    # known at its cutoff and scaled it by the served spread.
                    + "::" + RADIATION_ASOF_METHOD_TAG
                    # The tag rides only with a table: it keys the serving
                    # transform, and a bare one used to mean the pre-store
                    # bootstrap constants (2026-09-15: that is how a vetoed
                    # stage kept rescaling, on the wrong axis).  No table
                    # means no band layer, said by leaving the tag off.
                    + (
                        "::" + BAND_RELIABILITY_METHOD_TAG
                        + "::band_table_sha256="
                        + str(candidate["band_table_sha256"])
                        if candidate.get("band_table_sha256")
                        else ""
                    )
                    # Absent while the layer has fitted nothing, so artifacts
                    # built before it stay byte-identical and keep serving
                    # exactly the math they were gated on.
                    + (
                        "::" + CENTER_WIDTH_METHOD_TAG
                        + "::center_width_sha256="
                        + str(candidate["center_width_sha256"])
                        if candidate.get("center_width_sha256")
                        else ""
                    )
                    # Absent while no station scale differs from one, so
                    # artifacts built before the layer fitted anything stay
                    # byte-identical and keep the math they were gated on.
                    + (
                        "::" + STATION_WIDTH_METHOD_TAG
                        + "::station_width_sha256="
                        + str(candidate["station_width_sha256"])
                        if candidate.get("station_width_sha256")
                        else ""
                    )
                ),
                "family_weight_caps_json": _json(
                    candidate["family_weight_caps"]
                ),
                "probability_pathway": P0_PROBABILITY_PATHWAY,
                "valid_from_utc": activated_at_utc,
                "valid_until_utc": valid_until,
            }
            cross_sha = content_sha256(cross_identity)
            cross_row = {
                "cross_ensemble_artifact_id": (
                    "native-cross-artifact-" + cross_sha[:24]
                ),
                **cross_identity,
                "content_sha256": cross_sha,
            }
            counts["cross_artifacts"] += _insert_content_row(
                conn,
                table="calibration_cross_native_ensemble_artifact",
                primary_key="cross_ensemble_artifact_id",
                row=cross_row,
            )
            for prediction in candidate["predictions"]:
                probabilities = {
                    str(key): float(value)
                    for key, value in prediction["probability"].items()
                }
                prediction_identity = {
                    "cross_ensemble_artifact_id": cross_row[
                        "cross_ensemble_artifact_id"
                    ],
                    "selection_evidence_sha256": selection_sha,
                    "station_id": candidate["station_id"],
                    "event_date": prediction["event_date"],
                    "settlement_timezone": candidate[
                        "settlement_timezone"
                    ],
                    "season_bin": candidate["season_bin"],
                    "lead_bucket": candidate["lead_bucket"],
                    "run_cycle": candidate_run_cycle,
                    "run_cycle_by_system_json": cycle_mapping_json,
                    "run_cycle_by_system_sha256": cycle_mapping_sha,
                    "settlement_definition": candidate[
                        "settlement_definition"
                    ],
                    "trained_through_event_date": prediction[
                        "trained_through_event_date"
                    ],
                    "decision_at_utc": prediction["decision_at_utc"],
                    "ensemble_system_ids_json": _json(
                        candidate["selected_system_ids"]
                    ),
                    "independence_family_ids_json": _json(
                        candidate["independence_family_ids"]
                    ),
                    "system_family_ids_json": _json(
                        candidate["system_family_ids"]
                    ),
                    "system_weights_json": _json(
                        candidate["system_weights"]
                    ),
                    "family_weight_caps_json": _json(
                        candidate["family_weight_caps"]
                    ),
                    "system_member_set_shas_json": _json(
                        prediction["system_member_set_shas"]
                    ),
                    "probabilities_json": _json(probabilities),
                    # Charter 213: a ladder day's keys are rung labels, and
                    # its centres are the served axis's, not the label.
                    "bucket_centers_c_json": _json(
                        prediction.get("bucket_centers_c")
                        or {key: float(key) for key in probabilities}
                    ),
                    "prediction_oos": 1,
                    "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
                }
                prediction_sha = content_sha256(prediction_identity)
                prediction_row = {
                    "prediction_id": (
                        "native-oos-prediction-"
                        + prediction_sha[:24]
                    ),
                    **prediction_identity,
                    "content_sha256": prediction_sha,
                }
                counts["oos_predictions"] += _insert_content_row(
                    conn,
                    table=(
                        "calibration_cross_native_ensemble_oos_prediction"
                    ),
                    primary_key="prediction_id",
                    row=prediction_row,
                )
            activation_scope = (
                candidate["station_id"],
                candidate["season_bin"],
                candidate["lead_bucket"],
                candidate_run_cycle,
                candidate["available_system_set_hash"],
            )
            existing_activation = conn.execute(
                "SELECT activation_id,artifact_id,content_sha256 "
                "FROM calibration_native_ensemble_activation "
                "WHERE artifact_kind='CROSS_NATIVE_ENSEMBLE' "
                "AND station_id=? AND season_bin=? AND lead_bucket=? "
                "AND run_cycle=? "
                "AND available_system_set_hash_or_star=? "
                "AND run_cycle_by_system_json=? "
                "AND run_cycle_by_system_sha256=? "
                "ORDER BY generation DESC LIMIT 1",
                (
                    *activation_scope,
                    cycle_mapping_json,
                    cycle_mapping_sha,
                ),
            ).fetchone()
            if (
                existing_activation is not None
                and str(existing_activation[1])
                == str(cross_row["cross_ensemble_artifact_id"])
            ):
                activation_id = str(existing_activation[0])
            else:
                generation = 1 + int(conn.execute(
                    "SELECT COALESCE(MAX(generation),0) "
                    "FROM calibration_native_ensemble_activation "
                    "WHERE artifact_kind='CROSS_NATIVE_ENSEMBLE' "
                    "AND station_id=? AND season_bin=? "
                    "AND lead_bucket=? AND run_cycle=? "
                    "AND available_system_set_hash_or_star=?",
                    activation_scope,
                ).fetchone()[0])
                activation_identity = {
                    "artifact_kind": "CROSS_NATIVE_ENSEMBLE",
                    "artifact_id": cross_row[
                        "cross_ensemble_artifact_id"
                    ],
                    "station_id": candidate["station_id"],
                    "ensemble_system_id_or_star": "*",
                    "season_bin": candidate["season_bin"],
                    "lead_bucket": candidate["lead_bucket"],
                    "run_cycle": candidate_run_cycle,
                    "run_cycle_by_system_json": cycle_mapping_json,
                    "run_cycle_by_system_sha256": cycle_mapping_sha,
                    "settlement_definition": candidate[
                        "settlement_definition"
                    ],
                    "available_system_set_hash_or_star": candidate[
                        "available_system_set_hash"
                    ],
                    "generation": generation,
                    "valid_from_utc": activated_at_utc,
                    "valid_until_utc": valid_until,
                    "activated_at_utc": activated_at_utc,
                    "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
                }
                activation_sha = content_sha256(activation_identity)
                activation_row = {
                    "activation_id": (
                        "native-activation-" + activation_sha[:24]
                    ),
                    **activation_identity,
                    "content_sha256": activation_sha,
                }
                counts["activations"] += _insert_content_row(
                    conn,
                    table="calibration_native_ensemble_activation",
                    primary_key="activation_id",
                    row=activation_row,
                )
                activation_id = str(activation_row["activation_id"])
            receipts.append({
                "station_id": candidate["station_id"],
                "season_bin": candidate["season_bin"],
                "lead_bucket": candidate["lead_bucket"],
                "run_cycle": candidate_run_cycle,
                "run_cycle_by_system": cycle_by_system,
                "run_cycle_by_system_sha256": cycle_mapping_sha,
                "system_artifact_ids": system_artifact_ids,
                "system_activation_ids": system_activation_ids,
                "selection_evidence_id": selection_row[
                    "selection_evidence_id"
                ],
                "cross_ensemble_artifact_id": cross_row[
                    "cross_ensemble_artifact_id"
                ],
                "activation_id": activation_id,
            })
    except Exception:
        conn.execute("ROLLBACK TO native_ensemble_oos_persist")
        conn.execute("RELEASE native_ensemble_oos_persist")
        raise
    conn.execute("RELEASE native_ensemble_oos_persist")
    return {
        "schema": "native_ensemble_oos_persist_v1",
        "state": (
            "COMMITTED" if receipts
            else (
                "COMMITTED_EVALUATIONS_ONLY"
                if counts["oos_evaluations"]
                else "NO_OOS_PASS_NO_WRITE"
            )
        ),
        "counts": counts,
        "receipts": receipts,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "live_submit_authorized": False,
    }


__all__ = [
    "DEFAULT_FAMILY_WEIGHT_CAPS",
    "DEFAULT_MIN_HOLDOUT_DAYS",
    "DEFAULT_MIN_TRAIN_DAYS",
    "FORMAL_PROMOTION_THRESHOLD_PROFILE",
    "NO_LIVE_BOOTSTRAP_MIN_HOLDOUT_DAYS",
    "NO_LIVE_BOOTSTRAP_MIN_TRAIN_DAYS",
    "NO_LIVE_BOOTSTRAP_THRESHOLD_PROFILE",
    "SCHEMA",
    "build_native_ensemble_oos",
    "persist_native_ensemble_oos_evaluations",
    "persist_native_ensemble_oos",
]
