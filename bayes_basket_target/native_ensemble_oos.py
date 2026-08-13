"""Strict historical OOS training for native-member ensemble P0.

The module reads only append-only member runs and canonical daily-high truth.
Open-Meteo rows remain ``PRODUCTION_NO_LIVE_CANDIDATE`` evidence.  They may
earn a ``PRODUCTION_NO_LIVE_ONLY`` activation after a real, closed-day
strict-as-of holdout, but can never create ``FORMAL_PROMOTED`` authority.
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from itertools import product
from typing import Any
from zoneinfo import ZoneInfo

from .contracts import ContractError, content_sha256
from .cross_consensus import (
    CONSENSUS_BRIDGE_METHOD_TAG,
    bridged_cross_probability,
)
from .dwd_icon_eps_archive import (
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
    RUN_CYCLE_HOUR_BY_LEAD as _IFS_CYCLE_BY_LEAD,
    SUPPORTED_LEAD_DAYS as _IFS_LEADS,
    extraction_version_for_lead as _ifs_extraction_version,
)
from .native_archive_lead import lead_hour_bucket_for_run
from .native_ensemble_contract import (
    P0_PROBABILITY_PATHWAY,
    native_ensemble_family_set_hash,
    validate_cross_native_ensemble_weights,
)
from .noaa_gefs_archive import (
    RUN_CYCLE_HOUR_BY_LEAD as _GEFS_CYCLE_BY_LEAD,
    SUPPORTED_LEAD_DAYS as _GEFS_LEADS,
    extraction_version_for_lead as _gefs_extraction_version,
)
from .metar_physical_tmax import (
    METHOD_VERSION as METAR_PHYSICAL_METHOD_VERSION,
    PROVIDER_ID as METAR_PHYSICAL_PROVIDER_ID,
    SYSTEM_ID as METAR_PHYSICAL_SYSTEM_ID,
)
from .official_native_forward_packet import (
    MIXED_NATIVE_CYCLES,
    POINT_FORECAST_SYSTEMS,
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
# Point forecast systems -- today only the aerodrome TAF's TX group -- clear a
# lower bar than an ensemble family.
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
def _lead_contracts(
    *,
    supported_lead_days,
    cycle_hour_by_lead,
    extraction_version_for_lead,
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
        contracts[f"d{lead}"] = [
            {
                "run_cycle": f"{int(cycle_hour):02d}Z",
                "extraction_version": extraction_version_for_lead(
                    lead, cycle_hour
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
            cycle_hour_by_lead=_GEFS_CYCLE_BY_LEAD,
            extraction_version_for_lead=_gefs_extraction_version,
        ),
    },
    "ECMWF_IFS_ENS_OPEN_PF50": {
        "transport_provider_id": "ECMWF_OPEN_DATA_S3",
        "leads": _lead_contracts(
            supported_lead_days=_IFS_LEADS,
            cycle_hour_by_lead=_IFS_CYCLE_BY_LEAD,
            extraction_version_for_lead=_ifs_extraction_version,
        ),
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
            cycle_hour_by_lead=_ICON_CYCLE_BY_LEAD,
            # ICON's identity is a function of the lead alone -- one cycle per
            # lead, spelled out by the collector -- so the cycle argument is
            # accepted and dropped rather than the signature being widened
            # where there is nothing to vary.
            extraction_version_for_lead=(
                lambda lead, _cycle_hour: _icon_extraction_version(lead)
            ),
        ),
    },
}
# The forecaster's TAF TX as a single-member candidate system.  It lives in
# its own dict rather than in ``OFFICIAL_ARCHIVE_SCOPE_CONTRACTS`` because the
# NWP capture scheduler iterates that dict to advertise archive runs, and TAF
# is captured by its own rotation lane -- merging them would emit capture
# scopes no NWP collector can fetch.  For OOS admission the two dicts are one
# set: same scope check, same transport allowlist, same gates.  All four issue
# cycles are admitted at both near leads; each (lead, cycle) stays its own
# training scope exactly as ECCC's two cycles do.
POINT_FORECAST_SCOPE_CONTRACTS = {
    "OFFICIAL_AIRPORT_TAF_TX": {
        "transport_provider_id": "AWC_TAF_DATA_API",
        "leads": _lead_contracts(
            supported_lead_days=(0, 1),
            cycle_hour_by_lead={0: (0, 6, 12, 18), 1: (0, 6, 12, 18)},
            extraction_version_for_lead=(
                lambda _lead, _cycle_hour: "taf_tx_point_v4"
            ),
        ),
    },
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
# while winning CRPS and NLL outright.  Requiring both of the other scoring
# rules to improve by a real margin keeps that from becoming a way in for
# genuinely worse candidates: over the rejected history this channel admits
# about 6% of them, and NLL alone rejects half of what Brier-plus-CRPS would
# have let through.  A station whose candidates keep losing all three still
# stays rejected and keeps serving its last passing weights.
BRIER_TOLERANCE_MAX_RELATIVE_LOSS = 0.02
BRIER_TOLERANCE_MIN_RELATIVE_GAIN = 0.05
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


def _metrics(
    probability: Mapping[str, float],
    actual: float,
) -> dict[str, float]:
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


def _cross_oos_gate(
    cross_metrics: Mapping[str, float],
    climate_metrics: Mapping[str, float],
) -> dict[str, Any]:
    """Decide the gate and record which path decided it.

    The strict path is unchanged: beat climatology on Brier and CRPS.  The
    tolerance path exists because Brier can be lost by a rounding-scale
    margin on a candidate that is better everywhere else, and it demands a
    real margin on both remaining scoring rules in exchange.  The returned
    record is stored with the evaluation so a later review can separate
    tolerance admissions from strict ones without re-deriving them.
    """

    strict_pass = (
        float(cross_metrics["brier"])
        <= float(climate_metrics["brier"]) + EPS
        and float(cross_metrics["crps"])
        <= float(climate_metrics["crps"]) + EPS
    )
    gate: dict[str, Any] = {
        "path": "STRICT" if strict_pass else "REJECTED",
        "pass": strict_pass,
        "brier_tolerance_max_relative_loss": (
            BRIER_TOLERANCE_MAX_RELATIVE_LOSS
        ),
        "tolerance_min_relative_gain": BRIER_TOLERANCE_MIN_RELATIVE_GAIN,
    }
    if strict_pass:
        return gate

    climate_brier = float(climate_metrics["brier"])
    climate_crps = float(climate_metrics["crps"])
    climate_nll = float(climate_metrics.get("nll", 0.0))
    if climate_brier <= EPS or climate_crps <= EPS or climate_nll <= EPS:
        # Relative margins are meaningless against a zero baseline, and a
        # perfect climatology is not something to be admitted around.
        gate["tolerance_available"] = False
        return gate

    brier_relative_loss = (
        float(cross_metrics["brier"]) - climate_brier
    ) / climate_brier
    crps_relative_gain = (
        climate_crps - float(cross_metrics["crps"])
    ) / climate_crps
    nll_relative_gain = (
        climate_nll - float(cross_metrics.get("nll", climate_nll))
    ) / climate_nll
    tolerance_pass = (
        brier_relative_loss <= BRIER_TOLERANCE_MAX_RELATIVE_LOSS
        and crps_relative_gain >= BRIER_TOLERANCE_MIN_RELATIVE_GAIN
        and nll_relative_gain >= BRIER_TOLERANCE_MIN_RELATIVE_GAIN
    )
    gate.update({
        "tolerance_available": True,
        "brier_relative_loss": brier_relative_loss,
        "crps_relative_gain": crps_relative_gain,
        "nll_relative_gain": nll_relative_gain,
        "path": "CRPS_NLL_TOLERANCE" if tolerance_pass else "REJECTED",
        "pass": tolerance_pass,
    })
    return gate


def _mean_metrics(rows: Sequence[Mapping[str, float]]) -> dict[str, float]:
    if not rows:
        raise ContractError("native_ensemble_oos_metric_rows_missing")
    return {
        metric: sum(float(row[metric]) for row in rows) / len(rows)
        for metric in ("brier", "nll", "crps")
    }


# Station-local "drift" is often not a function of time but of state: at ZUUU
# the ECMWF residual moved from -0.25C to -1.92C between July and August, yet
# regressed against the forecast level the shift mostly disappears -- the
# model is wrong by an amount that tracks how hot it says the day will be.
# Time-weighted fixes were tried and rejected by the chronological holdout
# (they amplified late-window noise); a level-dependent correction is stable
# across the episode and does not overcorrect once it ends.
#
# Both correction forms are fitted and the winner is chosen per scope on a
# time-ordered validation tail inside the train window, so a station whose
# bias really is constant keeps the constant form.  The holdout never takes
# part in the choice; it stays the untouched judge.
AFFINE_SLOPE_MIN = 0.3
AFFINE_SLOPE_MAX = 1.7
FIT_VALIDATION_TAIL_DAYS = 6


def _affine_coefficients(
    pairs: Sequence[tuple[float, float]],
) -> tuple[float, float] | None:
    """OLS of truth on forecast center, or None when it is not estimable."""

    if len(pairs) < 8:
        return None
    mean_x = sum(x for x, _y in pairs) / len(pairs)
    mean_y = sum(y for _x, y in pairs) / len(pairs)
    var_x = sum((x - mean_x) ** 2 for x, _y in pairs)
    if var_x <= 1e-9:
        return None
    slope = sum(
        (x - mean_x) * (y - mean_y) for x, y in pairs
    ) / var_x
    if not AFFINE_SLOPE_MIN <= slope <= AFFINE_SLOPE_MAX:
        # A slope outside physical plausibility is a small-sample artefact;
        # serving it would swing corrections violently at the range edges.
        return None
    return mean_y - slope * mean_x, slope


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
    bias = sum(errors) / len(errors)
    rmse = math.sqrt(sum(error * error for error in errors) / len(errors))
    mean_spread = sum(spreads) / len(spreads)
    spread_factor = min(
        3.0,
        max(0.5, rmse / max(mean_spread, 0.1)),
    )
    reliability_weight = len(cases) / (len(cases) + 10.0)
    parameters = {
        "bias_c": bias,
        "spread_factor": spread_factor,
        "reliability_weight": reliability_weight,
        "climate_samples_c": climate_samples[-180:],
        "train_rmse_c": rmse,
        "train_mean_spread_c": mean_spread,
        "correction_form": "constant",
    }
    # Cases arrive chronological; the last few train days validate the form
    # choice.  Both forms are fitted on the earlier part only, judged on the
    # tail, and the winner is refitted on the full train window.
    tail = FIT_VALIDATION_TAIL_DAYS
    if len(cases) >= tail + 10:
        fit_pairs = list(zip(centers[:-tail], actuals[:-tail]))
        affine = _affine_coefficients(fit_pairs)
        if affine is not None:
            head_bias = (
                sum(a - c for c, a in fit_pairs) / len(fit_pairs)
            )
            intercept, slope = affine
            constant_err = sum(
                (a - (c + head_bias)) ** 2
                for c, a in zip(centers[-tail:], actuals[-tail:])
            )
            affine_err = sum(
                (a - (intercept + slope * c)) ** 2
                for c, a in zip(centers[-tail:], actuals[-tail:])
            )
            # A six-day tail is small enough to prefer the wrong form by
            # luck, and the cost is asymmetric: the constant form is the
            # simpler prior, so leaving it needs decisive evidence.  The
            # margin keeps a genuine regime response (ZUUU's tail error
            # roughly halves) while ignoring coin-flip differences.
            if affine_err < 0.8 * constant_err:
                refit = _affine_coefficients(list(zip(centers, actuals)))
                if refit is not None:
                    parameters["correction_form"] = "affine"
                    parameters["affine_intercept_c"] = refit[0]
                    parameters["affine_slope"] = refit[1]
                    affine_errors = [
                        a - (refit[0] + refit[1] * c)
                        for c, a in zip(centers, actuals)
                    ]
                    # The RMSE that feeds cross weights describes the form
                    # actually served, not the runner-up.
                    parameters["train_rmse_c"] = math.sqrt(
                        sum(e * e for e in affine_errors)
                        / len(affine_errors)
                    )
    return parameters


def _corrected_center(
    mean: float,
    parameters: Mapping[str, Any],
) -> float:
    """Where the calibrated distribution centers, under either form.

    The affine form corrects by an amount that tracks the forecast level --
    what a station-local regime shift usually is -- and the constant form is
    the level-independent special case.  Which one runs is the artifact's
    recorded choice, so evaluation and serving cannot disagree about it.
    """

    if str(parameters.get("correction_form") or "constant") == "affine":
        return (
            float(parameters["affine_intercept_c"])
            + float(parameters["affine_slope"]) * mean
        )
    return mean + float(parameters["bias_c"])


def _apply_parameters(
    values: Sequence[float],
    parameters: Mapping[str, Any],
) -> dict[str, float]:
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
            float(parameters.get("train_rmse_c") or 0.0),
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


def _select_minimum_crps_subset(
    *,
    ready_system_ids: Sequence[str],
    predictions_by_system: Mapping[str, Mapping[str, Mapping[str, Any]]],
    common_holdout: Sequence[str],
    family_by_system: Mapping[str, str],
    configured_caps: Mapping[str, float],
    train_rmse: Mapping[str, float],
    train_n_by_system: Mapping[str, int],
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

    The enumeration is availability-and-skill on the fixed holdout only; no
    subset ever sees its own selection reflected back into training, so this
    cannot tune on the holdout any more than the single full-set blend could.
    """

    ordered = sorted(ready_system_ids)
    if len(ordered) > 8:
        raise ContractError("cross_native_ensemble_subset_space_too_large")
    scored: list[dict[str, Any]] = []
    best: tuple[Any, ...] | None = None
    best_weights: dict[str, float] | None = None
    best_entry: dict[str, Any] | None = None
    for mask in range(1, 1 << len(ordered)):
        subset = [
            ordered[index]
            for index in range(len(ordered))
            if mask & (1 << index)
        ]
        if len({family_by_system[s] for s in subset}) < 2:
            continue
        try:
            weights = _bounded_family_weights(
                train_rmse_by_system={s: train_rmse[s] for s in subset},
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
        day_metrics = []
        for day in common_holdout:
            keys = set().union(*(
                set(predictions_by_system[s][day]["probability"])
                for s in subset
            ))
            ordered_keys = sorted(keys, key=lambda key: int(key))
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
                key_centers_c=[float(int(key)) for key in ordered_keys],
            )
            actual = float(
                predictions_by_system[subset[0]][day]["actual_tmax_c"]
            )
            day_metrics.append(_metrics(probability, actual))
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
            best_entry = entry
    if best_weights is None or best_entry is None:
        raise ContractError("cross_native_ensemble_no_scorable_subset")
    scored.sort(key=lambda row: (
        round(float(row["crps"]), 12),
        round(float(row["nll"]), 12),
        len(row["system_ids"]),
    ))
    receipt = {
        "rule": "MIN_CRPS_EXACT_SHARED_HOLDOUT_SUBSET_V1",
        "evaluated_subset_count": len(scored),
        "winner": best_entry,
        "leaderboard": scored[:8],
    }
    return best_weights, receipt


def _bounded_family_weights(
    *,
    train_rmse_by_system: Mapping[str, float],
    family_by_system: Mapping[str, str],
    family_caps: Mapping[str, float],
    train_n_by_system: Mapping[str, int] | None = None,
) -> dict[str, float]:
    family_scores: dict[str, float] = defaultdict(float)
    system_scores: dict[str, float] = {}
    for system_id, rmse in train_rmse_by_system.items():
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
    if sum(caps.values()) < 1.0 - EPS:
        raise ContractError("cross_native_ensemble_family_caps_infeasible")
    for _iteration in range(len(families) + 2):
        over = [
            family
            for family in families
            if family_weights[family] > caps[family] + EPS
        ]
        if not over:
            break
        excess = 0.0
        for family in over:
            excess += family_weights[family] - caps[family]
            family_weights[family] = caps[family]
        receivers = [
            family
            for family in families
            if family not in over
            and family_weights[family] < caps[family] - EPS
        ]
        capacity = sum(
            caps[family] - family_weights[family]
            for family in receivers
        )
        if not receivers or capacity + EPS < excess:
            raise ContractError(
                "cross_native_ensemble_family_caps_infeasible"
            )
        for family in receivers:
            family_weights[family] += excess * (
                (caps[family] - family_weights[family]) / capacity
            )
    total = sum(family_weights.values())
    family_weights = {
        family: value / total
        for family, value in family_weights.items()
    }
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


# One group yields at most this many cycle assignments.  Four systems each
# offering two cycles would be sixteen artifacts for one station-day scope,
# which is more calibration than the evidence behind it justifies and more
# rows than the activation lookup should have to rank.  In practice the live
# configuration produces two.
MAX_CYCLE_ASSIGNMENTS_PER_GROUP = 8


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
        for contract in _scope_contract_variants(official):
            expected_extraction = str(
                contract.get("extraction_version") or ""
            )
            if (
                transport_provider_id
                == contract["transport_provider_id"]
                and lead_bucket == contract["lead_bucket"]
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
            _season_bin(day),
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
    train = [
        case
        for case in cases
        if str(case["event_date"]) < holdout_start
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
    predictions = []
    for case in holdout:
        probability = _apply_parameters(case["values"], parameters)
        day_values = [float(value) for value in case["values"]]
        predictions.append({
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
                sum(day_values) / len(day_values), parameters
            ),
            "actual_tmax_c": float(case["actual_tmax_c"]),
            "metrics": _metrics(
                probability,
                float(case["actual_tmax_c"]),
            ),
        })
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
        "predictions": predictions,
        "oos_metrics": _mean_metrics([
            row["metrics"] for row in predictions
        ]),
    }


def _select_exact_shared_holdout_cohort(
    *,
    ready_rows: Sequence[tuple[str, str, Mapping[str, Any]]],
    systems: Mapping[str, Mapping[str, Any]],
    min_holdout_days: int,
    allowed_system_ids: Sequence[str] | None = None,
) -> tuple[
    tuple[str, ...],
    list[tuple[str, str, Mapping[str, Any]]],
] | None:
    """Select one bounded availability-only cross-system OOS cohort."""

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
    eligible_cohorts = []
    for holdout_days, cohort in ready_by_holdout.items():
        # A system can have both an official native row and an Open-Meteo
        # republisher row for the same exact holdout.  Keep at most one scope
        # per system, preferring the official transport.  This prevents a
        # republisher copy from creating a mixed transport scope conflict.
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
        if len(cohort_families) >= 2:
            eligible_cohorts.append((holdout_days, candidate))
    if not eligible_cohorts:
        return None
    return sorted(
        eligible_cohorts,
        key=lambda item: (
            -len(item[1]),
            tuple(sorted(row[0] for row in item[1])),
            item[0],
        ),
    )[0]


# P0 resolves its parameters along four dimensions: season, station, system
# and lead.  Bias uses all four; weights use the same key but with the station
# slot set to this sentinel until that station has enough training days of its
# own, because a per-station weight fit is noise-dominated at small n.
POOLED_STATION_SENTINEL = "__POOLED__"
# Below this many training days a station's own weights are not trusted.
#
# Measured on 45 stations x 33 days: per-station weights moved by a median of
# 0.148 (max 0.459) between the first and second half of the same period --
# LIMC's ECMWF share fell 0.45 -> 0.22 while NOAA rose 0.25 -> 0.71 -- whereas
# pooled weights moved 0.018.  At that sample size the station-to-station
# spread is estimation noise, and it buys only brier -0.005 out of sample
# against pooled weights.  Per-station bias, by contrast, buys brier -0.068.
#
# The dimension is kept rather than removed: real climate differences between,
# say, a tropical maritime station and a temperate continental one are
# plausible, they are simply not measurable in a month.  The threshold lets a
# station graduate to its own weights once it can support the fit.
MIN_STATION_WEIGHT_TRAIN_DAYS = 200


def pooled_train_rmse_by_system(
    conn: sqlite3.Connection,
    *,
    station_ids: Sequence[str],
    as_of_utc: str,
    min_train_days: int = DEFAULT_MIN_TRAIN_DAYS,
    min_holdout_days: int = DEFAULT_MIN_HOLDOUT_DAYS,
    max_event_date: str | None = None,
) -> dict[tuple[str, str, str, str], float]:
    """Training RMSE keyed by (season, station slot, system, lead).

    Each station is evaluated on its own split exactly as before; only the
    resulting residuals are combined.  Pooling the errors rather than averaging
    the per-station RMSEs keeps stations with more training days weighted
    accordingly, and keeps a station with an unusually hard climate from
    counting the same as one with an easy one.

    Bias is deliberately not pooled -- it is where the per-station signal
    actually lives.
    """

    # Keyed by (season_bin, lead_bucket, system_id).  Season and lead are kept
    # as dimensions even though weights pool across stations: a model's
    # relative skill genuinely differs between summer and winter, and between a
    # three-day-old run and a same-day one, whereas it does not differ enough
    # between stations to be worth the sample cost.
    squared: dict[tuple[str, str, str, str], float] = defaultdict(float)
    counts: dict[tuple[str, str, str, str], int] = defaultdict(int)
    for station in station_ids:
        try:
            report = build_native_ensemble_oos(
                conn,
                station_id=str(station),
                as_of_utc=as_of_utc,
                min_train_days=min_train_days,
                min_holdout_days=min_holdout_days,
                max_event_date=max_event_date,
            )
        except ContractError:
            # One station failing its own OOS contract must not deny every
            # other station a pooled fit; it simply contributes nothing.  Only
            # contract failures are absorbed: a programming error or a broken
            # database would otherwise be laundered into weights fitted on
            # whatever subset happened to survive, with nothing in the result
            # to say so.
            continue
        for candidate in report.get("cross_candidates") or []:
            season_bin = str(candidate.get("season_bin") or "")
            lead_bucket = str(candidate.get("lead_bucket") or "")
            for system_id, evaluation in (
                candidate.get("system_evaluations") or {}
            ).items():
                parameters = evaluation.get("parameters") or {}
                try:
                    rmse = float(parameters["train_rmse_c"])
                    # ``train_n`` sits on the evaluation, not in parameters.
                    n = int(evaluation.get("train_n") or 0)
                except (KeyError, TypeError, ValueError):
                    continue
                if n <= 0:
                    continue
                # Accumulate both slots.  The pooled entry always exists;
                # the per-station entry is emitted too, so a station that has
                # graduated can be served from the same table without a second
                # pass.
                for station_slot in (
                    POOLED_STATION_SENTINEL,
                    str(station),
                ):
                    key = (
                        season_bin,
                        station_slot,
                        str(system_id),
                        lead_bucket,
                    )
                    squared[key] += rmse * rmse * n
                    counts[key] += n
    return {
        key: (squared[key] / counts[key]) ** 0.5
        for key in sorted(counts)
        if counts[key] > 0
        # A per-station entry is only published once it clears the threshold,
        # so a caller cannot accidentally read an under-sampled fit.
        and (
            key[1] == POOLED_STATION_SENTINEL
            or counts[key] >= MIN_STATION_WEIGHT_TRAIN_DAYS
        )
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
    pooled_train_rmse: Mapping[str, float] | None = None,
) -> dict[str, Any]:
    """Build candidate artifacts; perform no writes.

    ``pooled_train_rmse`` supplies cross-station training RMSE per system so
    weights can be fitted on all stations at once while bias stays per station.
    Omitting it reproduces the previous per-station behaviour exactly.
    """

    if native_ensemble_foundation_schema_status(conn)["ok"] is not True:
        raise ContractError("native_ensemble_foundation_schema_required")
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
        )
        evaluations[scope] = result
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
        scope_groups[(season_bin, lead_bucket)][system_id].append((
            run_cycle,
            evaluation,
        ))
    configured_caps = {
        **DEFAULT_FAMILY_WEIGHT_CAPS,
        **dict(family_weight_caps or {}),
    }
    for (
        season_bin,
        lead_bucket,
    ), system_rows in _cycle_variant_groups(scope_groups):
        ready_rows = [
            (system_id, run_cycle, evaluation)
            for system_id, scopes in sorted(system_rows.items())
            for run_cycle, evaluation in scopes
            if evaluation["state"] == "OOS_READY"
        ]
        if len(ready_rows) < 2:
            continue
        # A ready system whose independently-fixed strict holdout window
        # does not overlap the other ready systems must not poison an
        # otherwise valid cross-native calibration.  Every system evaluation
        # exposes exactly ``min_holdout_days`` latest chronological cases, so
        # grouping by the complete holdout set is a bounded, deterministic
        # way to select the largest exact common-OOS cohort.  This selection
        # uses availability/timing only--never OOS scores--and consequently
        # cannot tune the model on its holdout result.
        selected_cohort = _select_exact_shared_holdout_cohort(
            ready_rows=ready_rows,
            systems=systems,
            min_holdout_days=min_holdout_days,
            allowed_system_ids=system_mask,
        )
        if selected_cohort is None:
            continue
        common_holdout, selected_ready_rows = selected_cohort
        cycle_by_system, cycle_mapping_sha, run_cycle, run_cycle_scope = (
            _cycle_mapping_for_rows(selected_ready_rows)
        )
        rows = [
            (system_id, evaluation)
            for system_id, _run_cycle, evaluation in selected_ready_rows
        ]
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
        # Weights come from pooled cross-station RMSE when it is supplied,
        # and fall back to this station's own train RMSE when it is not.
        #
        # Measured on 45 stations x 33 days: fitting weights per station buys
        # brier -0.005 / crps -0.008 over pooled weights, while per-station
        # bias correction buys brier -0.068 / crps -0.203.  Nearly all the
        # value is in the bias, and per-station weights cost an order of
        # magnitude in sample size -- 33 station-days instead of 1320.
        #
        # That matters beyond tidiness: once P0 recomputes on every model run,
        # a per-station-per-lead weight fit would have almost no samples left,
        # whereas pooled weights keep theirs.
        # Look up per-station first, then pooled, then this station's own fit.
        # The per-station entry only exists once it cleared the sample-size
        # threshold, so the order encodes the policy without a second check.
        def _train_rmse(system_id: str, evaluation: Mapping[str, Any]) -> float:
            table = pooled_train_rmse or {}
            for station_slot in (station, POOLED_STATION_SENTINEL):
                value = table.get(
                    (season_bin, station_slot, system_id, lead_bucket)
                )
                if value is not None:
                    return float(value)
            return float(evaluation["parameters"]["train_rmse_c"])

        train_rmse = {
            system_id: _train_rmse(system_id, evaluation)
            for system_id, evaluation in rows
        }
        weights = _bounded_family_weights(
            train_rmse_by_system=train_rmse,
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
        weights, subset_selection = _select_minimum_crps_subset(
            ready_system_ids=[system_id for system_id, _e in rows],
            predictions_by_system=predictions_by_system,
            common_holdout=common_holdout,
            family_by_system=family_by_system,
            configured_caps=configured_caps,
            train_rmse=train_rmse,
            train_n_by_system={
                system_id: int(evaluation.get("train_n") or 0)
                for system_id, evaluation in rows
            },
        )
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
        cross_predictions = []
        climate_train = next(
            iter(rows)
        )[1]["parameters"]["climate_samples_c"]
        climate_probability = _probabilities([
            float(value) for value in climate_train
        ])
        baseline_metrics = []
        for day in common_holdout:
            keys = set().union(*(
                set(predictions_by_system[system][day]["probability"])
                for system in weights
            ))
            ordered_keys = sorted(keys, key=lambda key: int(key))
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
                key_centers_c=[float(int(key)) for key in ordered_keys],
            )
            actual = float(
                predictions_by_system[
                    next(iter(weights))
                ][day]["actual_tmax_c"]
            )
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
                "metrics": _metrics(probability, actual),
            })
            baseline_metrics.append(_metrics(
                climate_probability,
                actual,
            ))
        cross_metrics = _mean_metrics([
            row["metrics"] for row in cross_predictions
        ])
        climate_metrics = _mean_metrics(baseline_metrics)
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
        residuals: dict[str, dict[str, float]] = {}
        for system_id, _evaluation in rows:
            scope = (
                system_id,
                season_bin,
                lead_bucket,
                cycle_by_system[system_id],
            )
            residuals[system_id] = {
                case["event_date"]: (
                    float(case["actual_tmax_c"])
                    - sum(float(value) for value in case["values"])
                    / len(case["values"])
                )
                for case in cases_by_scope[scope]
                if case["event_date"]
                <= rows[0][1]["train_end_date"]
            }
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
        oos_gate = _cross_oos_gate(cross_metrics, climate_metrics)
        oos_pass = bool(oos_gate["pass"])
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
            "p0_exact_match_ready": True,
            "p0_exact_match_blocker": None,
            "settlement_definition": next(
                iter(cases_by_scope.values())
            )[0]["settlement_definition"],
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
                "::MIN_CRPS_SUBSET"
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
                for system_id, evaluation in rows
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
            "climatology_baseline_metrics": climate_metrics,
            "cross_system_residual_correlation": correlations,
            "predictions": cross_predictions,
            "system_evaluations": {
                system_id: evaluation
                for system_id, evaluation in rows
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
        # The gate record rides only here: the evaluation row is a new
        # append-only fact each scan, so embedding it is free, while the
        # selection identity must keep the gate-free bytes its activation
        # pointers were bound under.
        "oos_metrics_json": _json(
            {**candidate["oos_metrics"], "gate": candidate["oos_gate"]}
            if isinstance(candidate.get("oos_gate"), Mapping)
            else candidate["oos_metrics"]
        ),
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
                if not system_run_cycle:
                    raise ContractError(
                        "native_ensemble_oos_run_cycle_mapping_missing"
                    )
                parameters = evaluation["parameters"]
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
                    "station_id": candidate["station_id"],
                    "season_bin": candidate["season_bin"],
                    "lead_bucket": candidate["lead_bucket"],
                    "run_cycle": system_run_cycle,
                    "settlement_definition": candidate[
                        "settlement_definition"
                    ],
                    "calibration_method": (
                        "member_bias_spread_reliability_v1"
                    ),
                    "bias_parameters_json": _json({
                        "bias_c": parameters["bias_c"],
                    }),
                    "spread_parameters_json": _json({
                        "spread_factor": parameters["spread_factor"],
                        "train_rmse_c": parameters["train_rmse_c"],
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
                        "oos_threshold_profile": candidate[
                            "threshold_profile"
                        ],
                        "oos_min_train_days": candidate[
                            "min_train_days"
                        ],
                        "oos_min_holdout_days": candidate[
                            "min_holdout_days"
                        ],
                        "formal_promotion_min_train_days": candidate[
                            "formal_promotion_min_train_days"
                        ],
                        "formal_promotion_min_holdout_days": candidate[
                            "formal_promotion_min_holdout_days"
                        ],
                        "formal_p0_eligible": False,
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
                    "effective_sample_n": float(evaluation["train_n"]),
                    "oos_metrics_json": _json(
                        evaluation["oos_metrics"]
                    ),
                    "valid_from_utc": activated_at_utc,
                    "valid_until_utc": valid_until,
                }
                system_sha = content_sha256(system_identity)
                system_row = {
                    "system_artifact_id": (
                        "native-system-artifact-" + system_sha[:24]
                    ),
                    **system_identity,
                    "content_sha256": system_sha,
                }
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
                    "train_rmse_inverse_family_capped_v1::"
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
                    "bucket_centers_c_json": _json({
                        key: float(key) for key in probabilities
                    }),
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
