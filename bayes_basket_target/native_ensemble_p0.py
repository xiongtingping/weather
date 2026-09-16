"""Compile activated calibrated native-member ensembles into P0 packets."""

from __future__ import annotations

import json
import math
import os
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Any
from zoneinfo import ZoneInfo

from .contracts import (
    MIN_USABLE_FRESHNESS_SECONDS,
    ContractError,
    ProbabilityEnvelope,
    assert_mintable_freshness_window,
    content_sha256,
)

# Weight-zero tolerance for provisional riders; matches the kernel's EPS.
EPS = 1e-9
from .station_registry import station_coordinates
from .station_season import station_season_bin
from .band_reliability_store import (
    BAND_AXIS_CENTRE_DISTANCE,
    BAND_AXIS_CLAIM,
    load_band_table,
    load_band_table_with_axis,
    pinned_band_table_sha,
)
from .center_width_store import (
    CENTER_WIDTH_METHOD_TAG,
    STATION_WIDTH_METHOD_TAG,
    load_center_width,
    load_center_width_floor,
    load_center_width_tiers,
    load_station_width,
    pinned_center_width_sha,
    pinned_station_width_sha,
)
from .fleet_regime_shift import load_regime_shift
from .cross_consensus import (
    BAND_RELIABILITY_METHOD_TAG,
    CONSENSUS_BRIDGE_METHOD_TAG,
    CONDITIONAL_SKEW_METHOD_TAG,
    NEIGHBOUR_DIRECTION_METHOD_TAG,
    RADIATION_SHIFT_METHOD_TAG,
    pinned_radiation_shift_sigma,
    SYNOPTIC_COOLING_METHOD_TAG,
    SYNOPTIC_COOLING_SHIFT_SIGMA,
    apply_conditional_tail_skew,
    apply_neighbour_direction_tilt,
    forecast_radiation_shift_sigma,
    synoptic_cooling_index,
    bridged_cross_probability,
)
from .stable_pool import (
    P0_MEMBERSHIP_MODE,
    P0_MEMBERSHIP_MODE_STABLE_POOL,
    STABLE_POOL_WEIGHT_RULE,
)
from .served_shape import (
    BandLayer,
    CenterWidthLayer,
    apply_served_table_layers,
    model_center_c as served_model_center_c,
)
from .future_temperature_curve import (
    MIN_SAME_DAY_CURVE_POINTS,
    build_native_family_temperature_curve,
    build_root_future_temperature_curve,
)
from .native_ensemble_contract import (
    P0_PROBABILITY_PATHWAY,
    TAF_NATIVE_ENSEMBLE_LIVE_FINGERPRINT_SCHEMA,
    validate_cross_native_ensemble_weights,
    validate_native_ensemble_transport_gate,
)
from .native_archive_lead import lead_hour_bucket_for_run
from .native_ensemble_oos import BIAS_CORRECTION_FORM, _resolved_bias_c
from .native_ensemble_authority import (
    NativeOosSnapshot,
    pointer_activation_mismatch_reasons,
    pointer_for_scope,
    station_has_pointer,
    validate_native_oos_snapshot_manifest,
)
from .open_meteo_ensemble import (
    FORWARD_OOS_TEMPORAL_SCOPE,
    OBSERVATIONAL_EVIDENCE_ROLE,
    SCHEMA as FORWARD_PACKET_SCHEMA,
)
from .official_native_forward_packet import (
    MIXED_NATIVE_CYCLES,
    POINT_FORECAST_SYSTEMS,
    SCHEMA as OFFICIAL_FORWARD_PACKET_SCHEMA,
    is_fusion_excluded_point_system,
)
from .ssot import native_ensemble_foundation_schema_status


SCHEMA = "calibrated_native_cross_ensemble_p0_packet_v1"
# v2: member bias may vary with the member's own forecast level
# (``level_terciles_v1``, user ruling 2026-08-27).  The bump is the point,
# not a formality -- posterior_version now bounds the centre/width evidence
# clock (the AST fingerprint only watches), so a serving change that keeps
# its version steals evidence from a fit describing a shape nobody serves.
# v3: the width the stage serves, which three changes moved between 09-03 and
# 09-07 without anyone bumping this string -- the centre/width layer wired at
# lambda 0.75, charter 86 retiring the climate blend, and the station width
# table at 0.5 on 32 of 45 stations.  Together they took P0's served sigma from
# 1.6 to 0.70 bucket steps.  The bump was owed on 09-06 and was not made
# because it would have anchored the evidence clock at the day it was typed;
# it says 2026-09-06T04:18Z in ``serving_arithmetic.STAGE_VERSION_EFFECTIVE_FROM``
# instead, which is the moment the last of the three actually reached serving.
# v4: the station width table retired (2026-09-10T15:27Z).  On every cell
# served after it went live the served distribution lost to a bounded 1.5x
# widening (+0.19 nat, sd(z) 1.40/1.85 on the two normal days), so the lane's
# new incumbent veto took it out; served sigma goes back from 0.70 to about
# 1.2 bucket steps.  Cells served under the 0.5 scale must not train the next
# width fit, hence the bump, with its start declared at the deactivation.
# v5: charter 134, the stable qualified pool.  Every OOS_READY native system
# with a run sits, weights are the skill rule shrunk toward the family
# baseline (kappa 0.5), and the subset contest no longer seats anything.  The
# served distribution changes on every scope the contest seated a strict
# subset for (88% of replayed updates), so the width, band and lambda lanes
# must not keep training on contest-era cells; the start is declared at the
# deploy in ``serving_arithmetic.STAGE_VERSION_EFFECTIVE_FROM``.
POSTERIOR_VERSION = "calibrated_native_cross_ensemble_p0_v5"
DEFAULT_FRESH_SECONDS = 6 * 60 * 60
# Systems whose members enter cross-ensemble artifacts without being model
# runs.  They carry an issuance instant, not a run initialisation, so they
# never vote on the lead-hour bucket -- the bucket belongs to the NWP runs.
# Cold-start chain for virgin lead buckets (the listing-at-entry ruling,
# 2026-08-14).  A newly reachable lead group -- h24_48 exists only since the
# D-2 capture lane opened -- has no trained weights of its own for its first
# forward-accumulation window.  Waiting starves the listing window the
# opening stage exists to trade, so serving borrows the nearest *shorter*
# trained bucket: shorter leads are strictly better-informed, so the borrow
# overstates nothing directional, only understates spread -- and the listing
# book it prices against is wider still.  The borrow is named in lineage,
# the trainer keeps stamping true buckets on every capture, and the exact
# bucket takes over the moment its own activation exists because it is
# always first in the chain.
HOUR_LEAD_BUCKET_FRESHNESS_ORDER = (
    "h00_12", "h12_24", "h24_48", "h48_plus",
)
# Charter 88 (user ruling, 2026-09-06: "h00_12 允许借 h12_24 同 run 的激活"):
# the shortest bucket has no shorter group to borrow from, and the day's
# last ECMWF/GEFS run (18Z, initialised after an Asian station's local
# midnight) lands in it with no activation fitted at that cycle -- the
# (h00_12, 18Z) facts only started accumulating on 2026-09-03, while the same
# cycle has weeks of fitted h12_24 activations.  So h00_12 may borrow the
# h12_24 activation *of the same run cycle*: the borrow is taken only when
# the exact bucket seats none of the packet's h00_12 systems at their own
# cycle and h12_24 does (``_activated_lead_bucket``), it is named in lineage
# (``same_run_lead_borrow_json``), and the exact bucket takes over the moment
# its own same-cycle activation exists because it is first in the chain.
# Unlike the cold-start borrows below this one reads a *longer* lead, so it
# overstates spread rather than understating it.
HOUR_LEAD_FALLBACK_CHAIN = {
    "h00_12": ("h00_12", "h12_24"),
    "h12_24": ("h12_24",),
    "h24_48": ("h24_48", "h12_24"),
    "h48_plus": ("h48_plus", "h24_48", "h12_24"),
}
NON_RUN_LEAD_BYSTANDER_SYSTEMS = frozenset({
    "OFFICIAL_AIRPORT_TAF_TX",
    "METAR_PHYSICAL_PATH_TMAX",
})
SAME_RUN_LEAD_BORROW_RULE = "h00_12_borrows_h12_24_same_run_v1"


def _same_run_fresh_count(
    selected_ids: Sequence[str],
    fitted_cycle_mapping: Mapping[str, str],
    packet_cycle_mapping: Mapping[str, str],
    freshest_bucket_system_ids: Sequence[str],
) -> int:
    """How many of the packet's freshest-bucket systems this fit seats at their own run.

    Charter 88's tie-break: between activations the packet can be served
    under, the one that seats the systems carrying the day's freshest run --
    at the cycle they were actually fitted under -- outranks one seating only
    the older split riders.  Zero when the fit seats none of them.
    """

    fresh = set(freshest_bucket_system_ids)
    return sum(
        1
        for system_id in selected_ids
        if system_id in fresh
        and str(packet_cycle_mapping.get(system_id) or "")
        and str(fitted_cycle_mapping.get(system_id) or "")
        == str(packet_cycle_mapping.get(system_id) or "")
    )


def _json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _season_bin(day: str) -> str:
    # Legacy calendar vocabulary, kept only for reading rows written before
    # charter 62's station-local labels.  The compile path no longer calls
    # it; do not key anything new with it.
    month = int(day[5:7])
    if month in (12, 1, 2):
        return "DJF"
    if month in (3, 4, 5):
        return "MAM"
    if month in (6, 7, 8):
        return "JJA"
    return "SON"


# Leads P0 may be compiled at, freshest first.  A shorter lead supersedes a
# longer one for the same station-day: the newest run replaces the previous
# P0 rather than sitting alongside it.
# Hour-lead vocabulary (charter 47): the calendar labels are retired and the
# packet builder now stamps the lead the run itself states.  The calendar
# trio stays admissible only while already-captured packets age out of the
# six-hour inbox window; the compiler re-derives its own training bucket from
# run initialisations regardless of this label.
SUPPORTED_P0_LEAD_BUCKETS = (
    "h00_12", "h12_24", "h24_48", "h48_plus", "d0", "d1", "d2",
)


def _lead_bucket(
    *,
    event_date: str,
    timezone_name: str,
    captured_at_utc: str,
) -> str:
    captured = datetime.fromisoformat(
        captured_at_utc.replace("Z", "+00:00")
    ).astimezone(ZoneInfo(timezone_name))
    delta = (
        datetime.fromisoformat(event_date).date()
        - captured.date()
    ).days
    return f"d{delta}" if delta >= 0 else f"d_minus_{abs(delta)}"


def _hour_lead_buckets(
    *,
    event_date: str,
    timezone_name: str,
    run_init_utc_by_system: Mapping[str, Any],
    observed_system_ids: Sequence[str],
) -> tuple[tuple[str, ...], dict[str, str]]:
    """The training groups this packet's runs touch, freshest first.

    OOS trains by how far ahead a run was issued rather than by the calendar
    label the collector wrote, because the two disagree badly: the calendar
    ``d1`` and ``d2`` groups overlap across 18-31h and ``d2`` alone spans 37h.
    P0 has to ask for weights under the same key they were fitted under, and
    that key is derived here from the runs the packet actually carries.

    ``None`` means no single hour bucket describes this packet -- either no run
    is stated at all, or the systems that state one were issued far enough
    apart to fall in different groups.  A cross-ensemble artifact holds systems
    that all share one bucket, so a genuinely split packet cannot match one,
    and saying so is different from guessing which half to believe.

    A system with no run initialisation is skipped rather than treated as a
    split.  It cannot be in a cross-ensemble artifact -- that requires a native
    run -- so it has no bucket to disagree about, and letting it veto the
    others made a bystander decide their key.  ZUUU carried ECCC as observed
    while excluding it for having no exact OOS activation; ECCC states no run,
    and the two systems actually used were both issued 22h ahead, yet the
    packet fell back to the calendar label and asked for weights under a key
    nothing is fitted under any more.
    """

    if not run_init_utc_by_system or not observed_system_ids:
        return (), {}
    try:
        day_start = datetime.fromisoformat(event_date).replace(
            tzinfo=ZoneInfo(timezone_name)
        ).astimezone(timezone.utc)
    except (ValueError, TypeError, KeyError):
        return (), {}
    bucket_by_system: dict[str, str] = {}
    for system_id in observed_system_ids:
        if system_id in NON_RUN_LEAD_BYSTANDER_SYSTEMS:
            # A revision stream states when it was issued, but it is not a
            # model run and holds no seat in the lead vocabulary: a TAF is
            # re-issued every six hours for the same day, so its issuance
            # lead is structurally one bucket fresher than the runs it rides
            # with, and letting it vote split every TAF-carrying packet --
            # the whole forward fleet fell back to the retired calendar
            # label and found nothing.  Same principle as the no-run-stated
            # skip below: a bystander must not decide the key.
            continue
        raw = str(run_init_utc_by_system.get(system_id) or "").strip()
        if not raw:
            continue
        try:
            run_init = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return (), {}
        if run_init.tzinfo is None or run_init.utcoffset() is None:
            return (), {}
        bucket_by_system[system_id] = lead_hour_bucket_for_run(
            day_start_utc=day_start, run_init=run_init
        )
    # A capture routinely mixes cycles across families -- each family is read
    # at its own newest published run, so after 06Z lands for ECMWF and GEFS
    # while ICON and ECCC still serve 00Z, the packet legitimately spans two
    # adjacent hour groups.  Treating that split as unresolvable fail-closed
    # the whole daytime fleet (28 scopes at once on 2026-08-14).  The split is
    # not an ambiguity to guess about: each bucket the packet touches is a
    # real trained grouping, so the caller tries them freshest-first and the
    # one with an admissible activation is the key -- named in lineage along
    # with every system whose own run sits outside it.
    ordered = tuple(
        bucket
        for bucket in HOUR_LEAD_BUCKET_FRESHNESS_ORDER
        if bucket in set(bucket_by_system.values())
    )
    return ordered, bucket_by_system


def _activated_lead_bucket(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    season_bin: str,
    decision_at_utc: str,
    observed_system_ids: Sequence[str],
    candidates: Sequence[str],
    packet_cycle_by_system: Mapping[str, str] | None = None,
    freshest_bucket_system_ids: Sequence[str] = (),
) -> str:
    """The first candidate lead bucket with an admissible cross activation.

    Candidates are ordered freshest-vocabulary first: the hour bucket the
    trainer now writes, then the calendar bucket earlier artifacts were keyed
    under.  Both are real trained groupings of the same rows, so falling back
    is not borrowing another lead's weights -- it is reading a coarser grouping
    that still contains this run.  Preferring the hour bucket matters because
    no new calendar-keyed artifact is written any more; without it P0 would
    coast on artifacts already in their validity window and then fail closed
    when they expire.

    Only admissibility is probed here -- scope window and whether the selected
    systems are among those observed.  The OOS-pass and lineage checks stay
    where they were: this chooses which grouping to ask under, not whether the
    answer is usable.

    Charter 88 (2026-09-06) adds one preference on top of chain order: a
    candidate whose admissible activation seats at least one of the packet's
    freshest-bucket systems *at that system's own run cycle* wins over an
    earlier candidate that seats none of them.  That is what lets an h00_12
    packet be served under h12_24 when only h12_24 was fitted at the day's
    last cycle; with no cycle map handed in the rule reduces to the old one.
    """

    observed = set(observed_system_ids)
    cycles = dict(packet_cycle_by_system or {})
    fresh = tuple(freshest_bucket_system_ids or ())
    first_admissible: str | None = None
    for candidate in candidates:
        rows = conn.execute(
            "SELECT r.ensemble_system_ids_json,a.run_cycle_by_system_json "
            "FROM calibration_native_ensemble_activation a "
            "JOIN calibration_cross_native_ensemble_artifact r "
            "ON r.cross_ensemble_artifact_id=a.artifact_id "
            "WHERE a.artifact_kind='CROSS_NATIVE_ENSEMBLE' "
            "AND a.station_id=? AND a.season_bin=? AND a.lead_bucket=? "
            "AND a.authority_scope='PRODUCTION_NO_LIVE_ONLY' "
            "AND a.valid_from_utc<=? AND a.valid_until_utc>? "
            "ORDER BY a.generation DESC,a.activation_id LIMIT 256",
            (
                station_id,
                season_bin,
                candidate,
                decision_at_utc,
                decision_at_utc,
            ),
        ).fetchall()
        for row in rows:
            try:
                selected = {str(value) for value in json.loads(str(row[0]))}
            except (TypeError, ValueError):
                continue
            if not (selected and selected.issubset(observed)):
                continue
            if first_admissible is None:
                first_admissible = candidate
            if not (fresh and cycles):
                # No cycle map: the pre-ruling rule, first admissible wins.
                return candidate
            try:
                fitted = {
                    str(key): str(value)
                    for key, value in json.loads(str(row[1] or "{}")).items()
                }
            except (TypeError, ValueError, AttributeError):
                fitted = {}
            if _same_run_fresh_count(sorted(selected), fitted, cycles, fresh) > 0:
                return candidate
    if first_admissible is not None:
        return first_admissible
    return candidates[-1]


def _interval_probability(
    values: Sequence[float],
    intervals: Mapping[str, Sequence[float | None]],
    bandwidth_c: float | None = None,
) -> dict[str, float]:
    """Integrate a kernel-dressed sample over the bucket contract.

    A histogram of a few dozen discrete values serves zeros even beside its
    own mean: truth once settled at a calibrated center of 30.0C while the
    30C bucket held no member and was served as impossible.  Dressing each
    value with a normal kernel makes mass decay with distance the way the
    sample's own shape says it should -- a far bucket is small because the
    model puts little there, never zero because the sample is finite.  The
    open edge buckets integrate their whole tails, so the contract still
    receives exactly the full unit of mass.
    """

    if not values:
        raise ContractError("native_ensemble_p0_values_missing")
    current: list[float] = []
    for raw_value in values:
        value = float(raw_value)
        if not math.isfinite(value):
            raise ContractError("native_ensemble_p0_value_invalid")
        current.append(value)
    count = len(current)
    mean = sum(current) / count
    if count > 1:
        std = math.sqrt(
            sum((v - mean) ** 2 for v in current) / count
        )
    else:
        std = 0.0
    # Half a degree: the discreteness scale, not a smoothing choice --
    # Silverman-sized bandwidths were rejected by the holdout for widening
    # what the calibration had already sized.  Deviations shrink so sample
    # variance plus kernel variance reproduces the original: dressing may
    # not change the width the calibration chose, only make it smooth.
    # A caller may widen it explicitly -- a lone point member has no sample
    # variance, so its calibrated width must arrive through the kernel.
    bandwidth = 0.5 if bandwidth_c is None else float(bandwidth_c)
    if std > bandwidth:
        shrink = math.sqrt(1.0 - (bandwidth / std) ** 2)
        current = [mean + shrink * (v - mean) for v in current]
    sqrt2 = math.sqrt(2.0)

    def _cdf(x: float) -> float:
        return sum(
            0.5 * (1.0 + math.erf((x - mu) / (bandwidth * sqrt2)))
            for mu in current
        ) / count

    probability: dict[str, float] = {}
    for bucket, bounds in intervals.items():
        if (
            not isinstance(bounds, Sequence)
            or isinstance(bounds, (str, bytes, bytearray))
            or len(bounds) != 2
        ):
            raise ContractError(
                "native_ensemble_p0_bucket_interval_invalid"
            )
        lower, upper = bounds
        upper_mass = 1.0 if upper is None else _cdf(float(upper))
        lower_mass = 0.0 if lower is None else _cdf(float(lower))
        probability[str(bucket)] = max(0.0, upper_mass - lower_mass)
    total = sum(probability.values())
    if not 0.999 <= total <= 1.001:
        # The intervals no longer partition the line; a histogram would have
        # caught this per value, the integral catches it in aggregate.
        raise ContractError(
            "native_ensemble_p0_bucket_contract_not_exhaustive"
        )
    return {
        bucket: value / total
        for bucket, value in probability.items()
    }


def _calibrated_system_probability(
    *,
    values: Sequence[float],
    intervals: Mapping[str, Sequence[float | None]],
    bias_parameters: Mapping[str, Any],
    spread_parameters: Mapping[str, Any],
    reliability_parameters: Mapping[str, Any],
) -> dict[str, float]:
    current = [float(value) for value in values]
    mean = sum(current) / len(current)
    # The forms this build can reproduce, refused otherwise -- never served
    # as a silent constant in their place.  That silent fallback is the bug
    # this guard replaces: a level-dependent form was once fitted and gated
    # but never persisted, so every affected scope served a centre the OOS
    # gate never scored.  An absent field is the constant form (every
    # artifact before 2026-08-27 omits it).  The resolver is the trainer's
    # own -- ``native_ensemble_oos._resolved_bias_c`` -- so the centre served
    # here is the centre the certificate was earned on, by construction.
    correction_form = str(
        bias_parameters.get("correction_form") or "constant"
    )
    if correction_form not in ("constant", BIAS_CORRECTION_FORM):
        raise ContractError(
            "native_ensemble_p0_calibration_correction_form_unsupported"
        )
    bias = _resolved_bias_c(mean, bias_parameters)
    # Every shift the artifact could ever apply is range-checked, not just
    # the one today's level selects: a bin holding an insane number is a
    # malformed artifact even on the days it is not chosen.
    all_biases = (
        [bias]
        if correction_form == "constant"
        else [float(value) for value in bias_parameters["bias_bin_shifts_c"]]
    )
    spread = float(spread_parameters["spread_factor"])
    reliability = float(
        reliability_parameters["reliability_weight"]
    )
    if (
        any(not -20.0 <= value <= 20.0 for value in all_biases)
        # The upper bound is the trainer's spread-factor cap in
        # native_ensemble_oos.py, not an independent judgement; the two must
        # move together or every artifact fitted at the cap is rejected here.
        or not 0.25 <= spread <= 5.0
        or not 0.0 < reliability <= 1.0
    ):
        raise ContractError(
            "native_ensemble_p0_calibration_parameters_invalid"
        )
    center = mean + bias
    corrected = [
        center + spread * (value - mean)
        for value in current
    ]
    # A single-member system (a point forecast) has no member deviations for
    # the spread factor to scale, so the plain 0.5C kernel would serve the
    # point as near-certainty regardless of how wrong it tends to be.  Its
    # honest width is the artifact's training residual -- the same number the
    # OOS evaluator used when it admitted the system -- so serving stays
    # consistent with what was gated.  Plural ensembles are untouched.
    single_member_bandwidth = None
    if len(current) == 1:
        single_member_bandwidth = max(
            0.5,
            float(
                spread_parameters.get("train_residual_sigma_c")
                or spread_parameters.get("train_rmse_c")
                or 0.0
            ),
        )
    member_probability = _interval_probability(
        corrected,
        intervals,
        bandwidth_c=single_member_bandwidth,
    )
    climate_samples = reliability_parameters.get("climate_samples_c")
    if not isinstance(climate_samples, list) or not climate_samples:
        raise ContractError(
            "native_ensemble_p0_reliability_samples_missing"
        )
    climate_probability = _interval_probability(
        [float(value) for value in climate_samples],
        intervals,
    )
    probability = {
        bucket: (
            reliability * member_probability[bucket]
            + (1.0 - reliability) * climate_probability[bucket]
        )
        for bucket in intervals
    }
    total = sum(probability.values())
    return {
        bucket: value / total
        for bucket, value in probability.items()
    }


def _ordered_buckets(
    intervals: Mapping[str, Sequence[float | None]],
) -> list[str]:
    def key(item: tuple[str, Sequence[float | None]]) -> tuple[float, float]:
        lower, upper = item[1]
        return (
            float("-inf") if lower is None else float(lower),
            float("inf") if upper is None else float(upper),
        )

    ordered = [str(bucket) for bucket, _bounds in sorted(
        intervals.items(),
        key=key,
    )]
    if len(ordered) != len(set(ordered)):
        raise ContractError("native_ensemble_p0_bucket_duplicate")
    return ordered


FORECAST_PHYSICS_HISTORY_DAYS = 180


def _forecast_radiation_sum(
    conn: sqlite3.Connection, *, station_id: str, event_date: str
) -> float | None:
    """The models' forecast daily shortwave radiation for this station-day."""

    try:
        row = conn.execute(
            "SELECT shortwave_radiation_sum "
            "FROM calibration_station_forecast_physics "
            "WHERE station_id=? AND local_date=? AND reference_only=1",
            (station_id, event_date),
        ).fetchone()
    except sqlite3.Error:
        return None
    return float(row[0]) if row and row[0] is not None else None


def _forecast_radiation_history(
    conn: sqlite3.Connection, *, station_id: str, event_date: str
) -> list[float]:
    """This station's own recent radiation record, excluding the event day.

    Radiation is a site quantity -- latitude, season and climate set its
    scale -- so it is standardised against the station's own history rather
    than a fleet constant, and the day being scored is excluded so it cannot
    standardise itself.
    """

    try:
        rows = conn.execute(
            "SELECT shortwave_radiation_sum "
            "FROM calibration_station_forecast_physics "
            "WHERE station_id=? AND local_date<>? AND reference_only=1 "
            "AND shortwave_radiation_sum IS NOT NULL "
            "ORDER BY local_date DESC LIMIT ?",
            (station_id, event_date, FORECAST_PHYSICS_HISTORY_DAYS),
        ).fetchall()
    except sqlite3.Error:
        return []
    return [float(row[0]) for row in rows]


def _shift_probability_c(
    probability: Mapping[str, float],
    *,
    ordered: Sequence[str],
    bucket_centers_c: Sequence[float],
    intervals: Mapping[str, Sequence[float | None]],
) -> dict[str, float]:
    """Move a bucketed distribution along the temperature axis.

    Each bucket's mass is carried to where its centre lands after the shift
    and split linearly between the two buckets it falls between, so a shift
    smaller than a bucket width still moves mass rather than rounding to
    nothing.  Mass leaving the edge stays in the edge bucket: the open-ended
    buckets are unbounded, so nothing falls off the end.
    """

    original_centers = _bucket_centers(intervals, ordered)
    shifted = {str(bucket): 0.0 for bucket in ordered}
    for index, bucket in enumerate(ordered):
        mass = float(probability[str(bucket)])
        if mass <= 0.0:
            continue
        target = float(bucket_centers_c[index])
        position = 0
        while (
            position + 1 < len(original_centers)
            and original_centers[position + 1] <= target
        ):
            position += 1
        if target <= original_centers[0]:
            shifted[str(ordered[0])] += mass
            continue
        if target >= original_centers[-1]:
            shifted[str(ordered[-1])] += mass
            continue
        lower_c = original_centers[position]
        upper_c = original_centers[position + 1]
        span = upper_c - lower_c
        weight_upper = (target - lower_c) / span if span > EPS else 0.0
        shifted[str(ordered[position])] += mass * (1.0 - weight_upper)
        shifted[str(ordered[position + 1])] += mass * weight_upper
    total = sum(shifted.values())
    if total <= 0.0:
        raise ContractError("native_ensemble_p0_shift_probability_empty")
    return {key: value / total for key, value in shifted.items()}


def _distribution_sigma_c(
    probability: Mapping[str, float],
    bucket_centers_c: Sequence[float],
) -> float:
    """The served distribution's own spread in degrees.

    Used only to express the conditioning signals in a scale-free way (a
    2C gap means something different on a 1C-spread day than on a 4C one).
    """

    ordered_values = list(probability.values())
    if len(ordered_values) != len(bucket_centers_c):
        raise ContractError("native_ensemble_p0_bucket_center_count_invalid")
    mean = sum(
        float(weight) * float(center)
        for weight, center in zip(ordered_values, bucket_centers_c)
    )
    variance = sum(
        float(weight) * (float(center) - mean) ** 2
        for weight, center in zip(ordered_values, bucket_centers_c)
    )
    return math.sqrt(variance) if variance > 0.0 else 0.0


# A median needs three points to be a median; over two peers it is their
# midpoint and one outlier carries it.  The constant is the FLOOR of the
# borrow cap, not the cap itself (charter 62): the working cap is the
# largest offset this station's own certified peers have measured, never
# below this fleet-wide floor.  It was the whole cap when +1.94C
# (NOAA_AIGEFS_025) was the largest offset anywhere; the certified fits
# have since measured +5.84C at RCSS, and clamping a tropical borrow to a
# temperate constant was itself a 1.5-3.8C cold error.
PROVISIONAL_BIAS_MIN_PEERS = 3
PROVISIONAL_BIAS_ABS_CAP_C = 2.0
# Charter 101: before borrowing a peer's parameters, ask the system for its
# own, fitted at the same station, season and lead bucket under a different
# run cycle.
#
# The provisional path was written for a system too young to have a fit.
# Measured 2026-09-06, that is not who walks it: the four incumbent global
# ensembles do, on 06Z and 12Z packets, because the training lane has fitted
# only 00Z and 18Z -- ECMWF and GEFS hold 14,178 and 14,373 artifacts each
# across all 45 stations and NOT ONE for 06Z or 12Z, while the serving lane
# began serving those cycles in early September (provisional envelopes rose
# 3% -> 10% between 09-02 and 09-04).  Every 06Z member went provisional,
# 111 of 111.  The scope is young; the model is not.
#
# And the peer borrow refuses far more often than it lends: 90 of ECMWF's 95
# provisional envelopes and 128 of GEFS's 134 fell to bias 0.0 for want of
# three certified peers, which serves the raw centre of a family measured to
# run 0.8C cold.  Widening cannot cover a centre offset; spread says how
# unsure a member is, bias says where it aims.
#
# The borrow is safe because it was measured, not assumed.  Across the 114
# (system, station, season, lead) combinations holding both a 00Z and an 18Z
# fit, the two run cycles' bias differs by a median of 0.198C (p90 0.552,
# max 1.255, signed mean +0.013 -- no systematic offset).  That is four to
# six times smaller than the 0.7-1.2C error the zero it replaces carries.
#
# No cap on this borrow, deliberately: the number is the system's own
# measurement, and the 2026-08-25 note above records what capping a borrow
# to a fleet constant cost when the station genuinely needed more.
CROSS_CYCLE_PARAMETER_BORROW = (
    os.environ.get("PM_CROSS_CYCLE_PARAMETER_BORROW", "").strip().lower()
    or "on"
)
if CROSS_CYCLE_PARAMETER_BORROW not in {"on", "off"}:
    raise RuntimeError(
        f"unknown cross cycle parameter borrow mode: "
        f"{CROSS_CYCLE_PARAMETER_BORROW}"
    )
SAME_DAY_CLIMATOLOGY_WINDOW_DAYS = 7
SAME_DAY_CLIMATOLOGY_MIN_SAMPLES = 8


def _same_day_climatology_headroom(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    event_date: str,
    model_center_c: float,
    sigma_c: float,
) -> float | None:
    """Headroom to this station's own same-calendar-day record, in sigmas.

    The window is the calendar day plus or minus a week, at this station
    only, across every year the truth archive holds, and the event date
    itself is excluded -- a day cannot be its own climatology.  Only the
    latest revision of each station-day counts (the truth table is
    append-only with revisions, and an unfiltered read returns superseded
    values).  Returns ``None`` when the window is too thin to speak.
    """

    if sigma_c <= EPS:
        return None
    try:
        event_day = date.fromisoformat(str(event_date))
    except ValueError:
        return None
    window = {
        (
            event_day + timedelta(days=offset)
        ).isoformat()[5:]
        for offset in range(
            -SAME_DAY_CLIMATOLOGY_WINDOW_DAYS,
            SAME_DAY_CLIMATOLOGY_WINDOW_DAYS + 1,
        )
    }
    latest: dict[str, tuple[int, float]] = {}
    # The multi-year reference record first: ten years of daily maxima at this
    # station's coordinates make "same calendar day" a real climatology rather
    # than this season's seven-day window.  These rows are reference
    # covariates only -- never truth, never a probability authority -- and the
    # settled truth below still contributes its own days, so a station whose
    # reference capture is missing degrades to the in-season window instead of
    # losing the signal.
    try:
        for local_date, high_c in conn.execute(
            "SELECT local_date,daily_high_c "
            "FROM calibration_station_same_day_climatology "
            "WHERE station_id=? AND reference_only=1",
            (station_id,),
        ):
            key = str(local_date)
            if key == str(event_date) or key[5:] not in window:
                continue
            latest[key] = (-1, float(high_c))
    except sqlite3.Error:
        latest = {}
    try:
        rows = conn.execute(
            "SELECT local_date,official_tmax_c,revision_no "
            "FROM calibration_station_daily_high_truth "
            "WHERE station_id=? AND official_tmax_c IS NOT NULL",
            (station_id,),
        ).fetchall()
    except sqlite3.Error:
        rows = []
    for local_date, tmax_c, revision_no in rows:
        key = str(local_date)
        if key == str(event_date) or key[5:] not in window:
            continue
        revision = int(revision_no or 0)
        # Settled truth outranks the reanalysis for a day both cover: the
        # station's own observation is the better record of that day.
        if key not in latest or revision > latest[key][0]:
            latest[key] = (revision, float(tmax_c))
    if len(latest) < SAME_DAY_CLIMATOLOGY_MIN_SAMPLES:
        return None
    record_high = max(value for _revision, value in latest.values())
    return (record_high - float(model_center_c)) / sigma_c


def _bucket_centers(
    intervals: Mapping[str, Sequence[float | None]],
    ordered: Sequence[str],
) -> list[float]:
    """A representative temperature per bucket, for histogram shifting.

    Interior buckets take their midpoint.  An open edge bucket has no
    midpoint, so it takes the point half a degree beyond its finite bound --
    the same 1C pitch as its neighbours -- which only matters as the anchor
    the consensus shift measures distances against.
    """

    centers: list[float] = []
    for bucket in ordered:
        lower, upper = intervals[str(bucket)]
        if lower is None and upper is None:
            raise ContractError("native_ensemble_p0_bucket_interval_invalid")
        if lower is None:
            centers.append(float(upper) - 0.5)
        elif upper is None:
            centers.append(float(lower) + 0.5)
        else:
            centers.append((float(lower) + float(upper)) / 2.0)
    return centers


def _bucket_for_value(
    value: float,
    intervals: Mapping[str, Sequence[float | None]],
) -> str:
    matches = [
        str(bucket)
        for bucket, bounds in intervals.items()
        if (
            len(bounds) == 2
            and (bounds[0] is None or value >= float(bounds[0]))
            and (bounds[1] is None or value < float(bounds[1]))
        )
    ]
    if len(matches) != 1:
        raise ContractError(
            "native_ensemble_p0_bucket_contract_not_exhaustive"
        )
    return matches[0]


_VALID_NATIVE_RUN_CYCLES = frozenset({"00Z", "06Z", "12Z", "18Z"})


def _taf_envelope_witness(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    event_date: str,
    decision_at_utc: str,
) -> dict[str, str] | None:
    """Latest pre-decision TAF TX for this station-day, or None.

    Measured on 24 celsius station-days, the forecaster's TX fell outside the
    model-center envelope 10 times and the settled bucket followed the TAF in
    7 of them.  An envelope that excludes the human forecast therefore forbids
    the winning bucket about a third of the time; the TX is admitted as one
    more center in the min/max below, widening but never narrowing.
    """

    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='calibration_forward_taf_capture'"
    ).fetchone() is None:
        # A deployment without the TAF lane has no TAF evidence at all --
        # that is absence, not failure, mirroring the truth-exclusions rule
        # that a missing file means an empty set.
        return None
    try:
        row = conn.execute(
            "SELECT tx_c, content_sha256, issued_at_utc, revision_sequence, "
            "revision_id, "
            "cloud_cover_fraction, precipitation_probability, wind_speed_mps "
            "FROM calibration_forward_taf_capture "
            "WHERE station_id=? AND event_date=? AND tx_c IS NOT NULL "
            # Knowable, not merely issued: a bulletin issued before the
            # decision but received after it did not exist for this compile.
            # Live, every row in the table was received before any compile
            # can read it, so this changes nothing there; a strict as-of
            # replay is where issue-time alone let a compile read a TAF that
            # had not arrived (review of 2026-09-13: issued 06:00, received
            # 10:00, selected at 08:00).
            "AND issued_at_utc<=? AND received_at_utc<=? "
            "ORDER BY issued_at_utc DESC, revision_sequence DESC LIMIT 1",
            (station_id, event_date, decision_at_utc, decision_at_utc),
        ).fetchone()
    except sqlite3.Error as exc:
        # The TAF is mandatory envelope evidence, not an optional garnish: a
        # swallowed query error here compiles a narrower envelope that then
        # forbids the forecaster's bucket for the rest of the root's life.
        # "No TX issued" is a legitimate None; "could not ask" is not.
        raise ContractError(
            "FAIL_CLOSED_NO_P0:taf_envelope_witness_query_failed"
        ) from exc
    if row is None or row[0] is None:
        return None
    return {
        "tx_c": f"{float(row[0]):.12g}",
        "cloud_cover_fraction": row[5],
        "precipitation_probability": row[6],
        "wind_speed_mps": row[7],
        "capture_content_sha256": str(row[1] or ""),
        "issued_at_utc": str(row[2] or ""),
        "revision_sequence": str(row[3] if row[3] is not None else ""),
        "forecast_revision_id": str(row[4] or ""),
    }


def _station_issues_taf_tx(
    conn: sqlite3.Connection,
    *,
    station_id: str,
) -> bool:
    """Whether this airport has ever published a TAF TX group.

    A property of the aerodrome's own reporting practice, not of today's
    capture: 28 of the 45 stations never carry TX, so waiting for one is
    waiting forever.  Answered from the whole capture history rather than the
    current day so a quiet day at a TX-issuing station is not mistaken for a
    station that does not issue at all.
    """

    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='calibration_forward_taf_capture'"
    ).fetchone() is None:
        return False
    try:
        row = conn.execute(
            "SELECT 1 FROM calibration_forward_taf_capture "
            "WHERE station_id=? AND tx_c IS NOT NULL LIMIT 1",
            (station_id,),
        ).fetchone()
    except sqlite3.Error as exc:
        # False here silently reclassifies a TX-issuing aerodrome as one
        # that never issues, and every downstream TAF obligation evaporates
        # with it.  An unanswerable question fails closed.
        raise ContractError(
            "FAIL_CLOSED_NO_P0:taf_station_capability_query_failed"
        ) from exc
    return row is not None


def _require_aggregator_fallback_authority(
    forward_packet: Mapping[str, Any],
) -> None:
    """Reject a republisher packet unless it carries its non-native limits.

    This is deliberately checked again at P0 compilation rather than relying
    on the candidate-store ingress: a forward inbox is a handoff boundary and
    must not be able to turn an Open-Meteo payload into native arrival proof.
    """

    authority = forward_packet.get("source_authority")
    if not isinstance(authority, Mapping):
        raise ContractError(
            "FAIL_CLOSED_NO_P0:aggregator_fallback_authority_missing"
        )
    if (
        authority.get("source_role") != "aggregator_republisher"
        or authority.get("native_run_init_known") is not False
        or authority.get("native_publication_at_known") is not False
        or authority.get("native_arrival_index_eligible") is not False
        or authority.get("formal_p0_eligible") is not False
        or authority.get("production_no_live_calibration_candidate")
        is not True
        or forward_packet.get("evidence_role")
        != OBSERVATIONAL_EVIDENCE_ROLE
        or forward_packet.get("oos_event_temporal_scope")
        != FORWARD_OOS_TEMPORAL_SCOPE
        or forward_packet.get("strict_oos_sample_eligible") is not False
        or forward_packet.get("can_substitute_for_formal_promotion")
        is not False
        or authority.get("evidence_role")
        != OBSERVATIONAL_EVIDENCE_ROLE
        or authority.get("oos_event_temporal_scope")
        != FORWARD_OOS_TEMPORAL_SCOPE
        or authority.get("counts_as_strict_oos") is not False
        or authority.get("historical_oos_contract_bound") is not False
        or authority.get("can_substitute_for_formal_promotion")
        is not False
    ):
        raise ContractError(
            "FAIL_CLOSED_NO_P0:aggregator_fallback_authority_invalid"
        )


def _official_forward_lineage(
    forward_packet: Mapping[str, Any],
    *,
    observed_system_ids: Sequence[str],
    captured: datetime,
) -> dict[str, Any]:
    """Resolve official packet timing without inventing a common cycle."""

    packet_cycle = str(forward_packet.get("run_cycle") or "").strip()
    packet_scope = str(
        forward_packet.get("run_cycle_scope") or ""
    ).strip()
    cycle_by_system_raw = forward_packet.get("run_cycle_by_system")
    init_by_system_raw = forward_packet.get("run_init_utc_by_system")
    query_by_system_raw = forward_packet.get(
        "archive_query_at_utc_by_system"
    )
    mixed = (
        packet_cycle == MIXED_NATIVE_CYCLES
        or packet_scope == "PER_SYSTEM_NATIVE_CYCLE"
    )
    observed = {str(system_id) for system_id in observed_system_ids}

    def _mapping_or_missing(
        value: Any,
        error: str,
    ) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise ContractError(error)
        resolved = {str(key): item for key, item in value.items()}
        if set(resolved) != observed:
            raise ContractError(error)
        return resolved

    def _timestamp(value: Any, error: str) -> datetime:
        try:
            parsed = datetime.fromisoformat(
                str(value or "").replace("Z", "+00:00")
            )
        except (TypeError, ValueError) as exc:
            raise ContractError(error) from exc
        if parsed.tzinfo is None:
            raise ContractError(error)
        return parsed.astimezone(captured.tzinfo)

    if mixed:
        cycle_by_system = _mapping_or_missing(
            cycle_by_system_raw,
            "native_ensemble_p0_mixed_cycle_lineage_missing",
        )
        init_by_system = _mapping_or_missing(
            init_by_system_raw,
            "native_ensemble_p0_mixed_init_lineage_missing",
        )
        query_by_system = _mapping_or_missing(
            query_by_system_raw,
            "native_ensemble_p0_mixed_arrival_lineage_missing",
        )
        for system_id in sorted(observed):
            cycle = str(cycle_by_system[system_id] or "").strip()
            if cycle not in _VALID_NATIVE_RUN_CYCLES:
                raise ContractError(
                    "native_ensemble_p0_mixed_cycle_lineage_invalid"
                )
            run_init = _timestamp(
                init_by_system[system_id],
                "native_ensemble_p0_mixed_init_lineage_invalid",
            )
            archive_query = _timestamp(
                query_by_system[system_id],
                "native_ensemble_p0_mixed_arrival_lineage_invalid",
            )
            # A point forecast's run_init is its issuance instant -- 11:05,
            # 17:08 -- and its cycle is the label that pairs it with model
            # cohorts, so the label-hour equality holds only for models.  The
            # packet builder already waives it for point systems; this
            # revalidation must agree with the builder or every MIXED packet
            # observing a TAF dies here after being built successfully.  The
            # ordering conditions stay for everyone: an issuance from the
            # future is wrong no matter what class of system claims it.
            if (
                (
                    system_id not in POINT_FORECAST_SYSTEMS
                    and run_init.hour != int(cycle[:2])
                )
                or archive_query < run_init
                or archive_query > captured
                or run_init > captured
            ):
                raise ContractError(
                    "native_ensemble_p0_mixed_lineage_timestamp_mismatch"
                )
        availability = forward_packet.get("availability_evidence")
        if not isinstance(availability, Mapping):
            raise ContractError(
                "native_ensemble_p0_mixed_availability_evidence_missing"
            )
        availability_by_system = availability.get("by_system")
        if not isinstance(availability_by_system, Mapping):
            raise ContractError(
                "native_ensemble_p0_mixed_availability_evidence_missing"
            )
        if {
            str(key) for key in availability_by_system
        } != observed:
            raise ContractError(
                "native_ensemble_p0_mixed_availability_evidence_incomplete"
            )
        availability_identity = {
            key: value
            for key, value in availability.items()
            if key != "content_sha256"
        }
        if (
            availability.get("content_sha256")
            != content_sha256(availability_identity)
        ):
            raise ContractError(
                "native_ensemble_p0_mixed_availability_evidence_invalid"
            )
        for system_id in sorted(observed):
            receipt = availability_by_system[system_id]
            if not isinstance(receipt, Mapping):
                raise ContractError(
                    "native_ensemble_p0_mixed_availability_evidence_invalid"
                )
            if (
                str(receipt.get("run_cycle") or "")
                != str(cycle_by_system[system_id])
                or str(receipt.get("run_init_utc") or "")
                != str(init_by_system[system_id])
                or str(receipt.get("archive_query_at_utc") or "")
                != str(query_by_system[system_id])
            ):
                raise ContractError(
                    "native_ensemble_p0_mixed_availability_evidence_mismatch"
                )
        return {
            "mixed": True,
            "run_cycle": MIXED_NATIVE_CYCLES,
            "run_cycle_scope": "PER_SYSTEM_NATIVE_CYCLE",
            "run_cycle_by_system": {
                system_id: str(cycle_by_system[system_id])
                for system_id in sorted(observed)
            },
            "run_init_utc_by_system": {
                system_id: str(init_by_system[system_id])
                for system_id in sorted(observed)
            },
            "archive_query_at_utc_by_system": {
                system_id: str(query_by_system[system_id])
                for system_id in sorted(observed)
            },
            "availability_evidence": dict(availability),
        }

    if packet_cycle not in _VALID_NATIVE_RUN_CYCLES:
        raise ContractError("native_ensemble_p0_official_scope_invalid")
    if cycle_by_system_raw is None:
        cycle_by_system = {
            system_id: packet_cycle
            for system_id in sorted(observed)
        }
    else:
        cycle_by_system = _mapping_or_missing(
            cycle_by_system_raw,
            "native_ensemble_p0_same_cycle_lineage_invalid",
        )
        if any(
            str(value or "").strip() != packet_cycle
            for value in cycle_by_system.values()
        ):
            raise ContractError(
                "native_ensemble_p0_same_cycle_lineage_mismatch"
            )
    init_by_system = (
        None
        if init_by_system_raw is None
        else _mapping_or_missing(
            init_by_system_raw,
            "native_ensemble_p0_same_cycle_init_lineage_invalid",
        )
    )
    query_by_system = (
        None
        if query_by_system_raw is None
        else _mapping_or_missing(
            query_by_system_raw,
            "native_ensemble_p0_same_cycle_arrival_lineage_invalid",
        )
    )
    if init_by_system is not None:
        for system_id in sorted(observed):
            run_init = _timestamp(
                init_by_system[system_id],
                "native_ensemble_p0_same_cycle_init_lineage_invalid",
            )
            # Same waiver as the mixed path: a point forecast's label pairs
            # it with the cohort, its issuance hour jitters.
            if (
                system_id not in POINT_FORECAST_SYSTEMS
                and run_init.hour != int(packet_cycle[:2])
            ) or run_init > captured:
                raise ContractError(
                    "native_ensemble_p0_same_cycle_init_lineage_mismatch"
                )
    if query_by_system is not None:
        for system_id in sorted(observed):
            archive_query = _timestamp(
                query_by_system[system_id],
                "native_ensemble_p0_same_cycle_arrival_lineage_invalid",
            )
            if archive_query > captured:
                raise ContractError(
                    "native_ensemble_p0_same_cycle_arrival_lineage_mismatch"
                )
    return {
        "mixed": False,
        "run_cycle": packet_cycle,
        "run_cycle_scope": "SAME_NATIVE_CYCLE",
        "run_cycle_by_system": cycle_by_system,
        "run_init_utc_by_system": init_by_system or {},
        "archive_query_at_utc_by_system": query_by_system or {},
        "availability_evidence": forward_packet.get(
            "availability_evidence"
        ),
    }


def seated_evidence_received_at(
    archive_query_at_utc_by_system: Mapping[str, Any] | None,
    *,
    seated_system_ids: Sequence[str],
) -> datetime | None:
    """The newest archive receipt among the systems a P0 actually seats.

    A packet's ``received_at_utc`` is the newest receipt of *any* archive in
    it, and the TTL used to run from there -- so an archive that entered the
    snapshot and was then left out of the blend renewed the probability of
    the systems that were in it.  Measured 2026-09-11..12: every one of the
    349 same-run P0 re-mints that extended freshness did so on an unseated
    archive, and KSFO 2026-09-11 at 01:39Z served a P0 built only on ICON and
    GEM from 00Z (received 07:53Z) as fresh until 07:31Z because ECMWF and
    GEFS 18Z -- both excluded from the blend -- had just been captured.

    The newest of the seated receipts rather than the oldest: it is the same
    reading ``available_at`` has always had ("the snapshot knows what its
    newest member knows"), restricted to the members that are in it.  None
    when a seated system has no readable receipt; callers fall back to the
    packet's own clock rather than invent one.

    One function for the compiler that mints the window and the capture probe
    that decides whether to re-read, so the two cannot come to disagree on
    what is usable.  When they did (the probe's own note in the worker), a
    packet the compiler refused kept satisfying the probe, no fresh capture
    was ever asked for, and the inbox aged out with nothing to replace it.
    """

    if not isinstance(archive_query_at_utc_by_system, Mapping):
        return None
    seated = [str(system_id) for system_id in seated_system_ids]
    if not seated:
        return None
    receipts: list[datetime] = []
    for system_id in seated:
        raw = str(archive_query_at_utc_by_system.get(system_id) or "").strip()
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return None
        receipts.append(parsed.astimezone(timezone.utc))
    return max(receipts)


def _activation_rank(
    item: tuple[Any, Sequence[str], Sequence[str], int],
    *,
    observed_set: set[str],
    pool_first: bool,
    seats_usable: Callable[[Sequence[str]], bool] | None = None,
) -> tuple[Any, ...]:
    """Sort key over compatible activations, highest first.

    ``item`` is (activation row, seated system ids, cycle riders, same-run
    fresh count).  Under charter 134: a seat whose evidence keeps the P0
    usable first, then a pool seat, then exact packet match, fewest riders,
    freshest runs, latest activation, breadth, generation, id.  See the
    comments at the call site for why each key sits where it does.

    ``seats_usable`` answers the freshness question the compile asks after
    choosing, for each candidate before it: the pool key alone put a view
    without the packet's freshest system ahead of a contest set that had it,
    and that P0 then failed ``seated_evidence_stale`` (ZGSZ and KHOU in the
    2026-09-14 05:22Z inbox, both served by the contest rule).  Read only
    with ``pool_first``, so the contest rule's order is unchanged.
    """

    row, member_ids, riders, fresh_count = item
    usable = (
        True
        if not pool_first or seats_usable is None
        else bool(seats_usable(member_ids))
    )
    return (
        usable,
        bool(pool_first) and STABLE_POOL_WEIGHT_RULE in str(row[15] or ""),
        set(member_ids) == observed_set,
        -len(riders),
        int(fresh_count),
        str(row[4] or ""),
        len(member_ids),
        int(row[3]),
        str(row[0]),
    )


def _activations_ambiguous(
    activations: Sequence[tuple[Any, Sequence[str], Sequence[str], int]],
    *,
    pool_first: bool,
) -> bool:
    """Whether the two best compatible activations are indistinguishable.

    Equal on membership, riders, same-run fresh count, activation instant
    and generation means the rank could only separate them by id, and under
    the contest rule that is refused: two contest picks of one set are two
    different claims about the same holdout.  A pool seat on top is served
    instead (charter 134): both are certified seats of the same systems
    under one rule, and the content-addressed id picks the same one every
    cycle.  Measured on the deploy's first rotated station, WMKK
    2026-09-14T04:01Z, whose exact and shared-window cohorts minted the
    whole four-system pool at h00_12 under the labels 00Z and
    MIXED_NATIVE_CYCLES in one generation.
    """

    if len(activations) < 2:
        return False
    first, second = activations[0], activations[1]
    first_pool = STABLE_POOL_WEIGHT_RULE in str(first[0][15] or "")
    second_pool = STABLE_POOL_WEIGHT_RULE in str(second[0][15] or "")
    if pool_first and first_pool:
        return False
    return (
        first_pool == second_pool
        and len(first[1]) == len(second[1])
        and set(first[1]) == set(second[1])
        and len(first[2]) == len(second[2])
        and int(first[3]) == int(second[3])
        and str(first[0][4] or "") == str(second[0][4] or "")
        and int(first[0][3]) == int(second[0][3])
    )



def resolve_pinned_band_layer(
    conn: "sqlite3.Connection",
    artifact_combination_method: str,
    band_table_override: "tuple | None" = None,
) -> "tuple[BandLayer | None, str | None, str]":
    """Which band table this artifact is served through, its sha and its axis.

    The layer is None when nothing is to be applied -- identity.

    The artifact pins the table version its holdout was scored against
    (``band_table_sha256=`` in the combination method).  A pinned-but-missing
    table fails closed inside the loader: serving an approximation of gated
    math is exactly what the pin exists to forbid.  A pinned table the lane
    has VETOED loads as an empty table -- identity, with its sha kept so the
    receipt can say which version was retired out from under it.

    A BARE tag is identity too, not the pre-store bootstrap constants.  It
    meant them while artifacts older than the store were still alive (the
    store took over 2026-08-15); today its one producer is a build made when
    no table is activated, which is what a veto leaves behind.  Measured
    2026-09-15: the veto retired P0's table at 18:26Z, the next builds minted
    bare tags, and 284 P0 decisions went on being rescaled by the
    hand-written constants -- on the CLAIM axis, while the lane has been on
    centre_distance since 09-07: the layer the veto had just called harmful,
    in a shape nothing had gated.  "No active table" means identity.

    Charter 105: the lookup follows the TABLE's axis, never the caller's.
    """

    if BAND_RELIABILITY_METHOD_TAG not in artifact_combination_method:
        return None, None, BAND_AXIS_CLAIM
    pinned_sha = pinned_band_table_sha(artifact_combination_method)
    pinned_factors = None
    pinned_axis = BAND_AXIS_CLAIM
    if pinned_sha is not None:
        pinned_factors, _sha, pinned_axis = load_band_table_with_axis(
            conn, table_sha256=pinned_sha
        )
    if band_table_override is not None:
        # The counterfactual arm.  Replaces the pinned lookup outright, axis
        # included -- a table handed in without its axis would be applied
        # through whichever one the pin happened to name, which is the
        # failure this triple exists to make impossible.  The receipt records
        # the substitution so a replayed envelope can never be mistaken for a
        # served one.
        pinned_factors = tuple(
            ((float(lo), float(hi)), float(factor))
            for (lo, hi), factor in band_table_override[0]
        )
        pinned_sha = str(band_table_override[1])
        pinned_axis = str(band_table_override[2])
        if pinned_axis not in {BAND_AXIS_CLAIM, BAND_AXIS_CENTRE_DISTANCE}:
            raise ContractError(
                "FAIL_CLOSED_NO_P0:band_table_override_axis_unknown"
            )
    if pinned_factors is None:
        return None, pinned_sha, pinned_axis
    return BandLayer(factors=pinned_factors, axis=pinned_axis), pinned_sha, pinned_axis


def compile_native_ensemble_p0(
    conn: sqlite3.Connection,
    *,
    forward_packet: Mapping[str, Any],
    decision_at_utc: str,
    fresh_seconds: int = DEFAULT_FRESH_SECONDS,
    native_oos_snapshot: NativeOosSnapshot | Mapping[str, Any] | None = None,
    artifact_parameter_override: (
        Mapping[str, Mapping[str, Any]] | None
    ) = None,
    band_table_override: (
        tuple[Sequence[tuple[tuple[float, float], float]], str, str] | None
    ) = None,
) -> dict[str, Any]:
    """Compile one station-day, or fail closed with ``NO_P0``.

    ``artifact_parameter_override`` exists for one purpose: scoring an
    upstream constant on the book.  Everything the trainer decides -- the
    bias, the spread factor, the climatology blend -- is baked into a stored
    artifact by the time this runs, so a replay that varies such a constant
    cannot reach it by setting the constant; it has to hand in the numbers
    the constant would have produced.

    Deliberately narrow.  It is keyed by ``ensemble_system_id`` and replaces
    only the three fitted parameter maps, *after* the activation has been
    resolved and its run cycle checked.  Every gate the compile normally
    passes still runs and still fails closed; what this can change is the
    numbers, never whether a system was entitled to be seated.  An override
    naming a system this station is not serving is ignored rather than
    seated, for the same reason.

    ``band_table_override`` is the same idea for charter 105's axis.  The
    band table is not a constant either: it is loaded from the database by
    the sha the artifact's combination method pins, so a replay that varies
    the table cannot reach it by setting a constant and cannot reach it by
    writing a different table either -- the pin would still name the old
    one.  It is handed in as ``(factors, sha, axis)``, the same triple
    ``load_band_table_with_axis`` returns, and it replaces the pinned lookup
    entirely: the axis rides with it, so a distance-fitted counterfactual is
    applied through the distance lookup and not through the claim one.

    Both are deliberately narrow, and both are applied AFTER the activation
    has been resolved and its run cycle checked.  Every gate the compile
    normally passes still runs and still fails closed; what these can change
    is the numbers, never whether a system was entitled to be seated.  An
    override naming a system this station is not serving is ignored rather
    than seated, for the same reason.

    Production never passes either.  ``None`` leaves this function
    byte-identical.
    """

    if native_ensemble_foundation_schema_status(conn)["ok"] is not True:
        raise ContractError(
            "FAIL_CLOSED_NO_P0:native_ensemble_foundation_schema_missing"
        )
    packet_schema = str(forward_packet.get("schema") or "")
    if packet_schema not in {
        FORWARD_PACKET_SCHEMA,
        OFFICIAL_FORWARD_PACKET_SCHEMA,
    }:
        raise ContractError("native_ensemble_p0_forward_schema_invalid")
    if fresh_seconds < 60 or fresh_seconds > 24 * 60 * 60:
        raise ContractError("native_ensemble_p0_freshness_invalid")
    try:
        decision = datetime.fromisoformat(
            decision_at_utc.replace("Z", "+00:00")
        )
    except (TypeError, ValueError) as exc:
        raise ContractError(
            "native_ensemble_p0_decision_timestamp_invalid"
        ) from exc
    if decision.tzinfo is None:
        raise ContractError("native_ensemble_p0_decision_timestamp_invalid")
    decision = decision.astimezone(timezone.utc)
    official_snapshot = packet_schema == OFFICIAL_FORWARD_PACKET_SCHEMA
    native_pointer_snapshot: NativeOosSnapshot | None = None
    station_attested = False
    if official_snapshot:
        official_identity = {
            key: value
            for key, value in forward_packet.items()
            if key != "snapshot_content_sha256"
        }
        if (
            forward_packet.get("snapshot_content_sha256")
            != content_sha256(official_identity)
        ):
            raise ContractError(
                "native_ensemble_p0_official_snapshot_invalid"
            )
    else:
        _require_aggregator_fallback_authority(forward_packet)
    captured_at = str(
        forward_packet.get(
            (
                "received_at_utc"
                if official_snapshot
                else "aggregator_snapshot_at_utc"
            )
        )
        or ""
    )
    try:
        captured = datetime.fromisoformat(
            captured_at.replace("Z", "+00:00")
        )
    except (TypeError, ValueError) as exc:
        raise ContractError(
            "native_ensemble_p0_capture_timestamp_invalid"
        ) from exc
    if captured.tzinfo is None:
        raise ContractError("native_ensemble_p0_capture_timestamp_invalid")
    captured = captured.astimezone(timezone.utc)
    if captured > decision:
        raise ContractError("native_ensemble_p0_future_capture_forbidden")
    # A compile-time refresh must not revive an old native packet.  Besides
    # preserving the P0 TTL, this is the decisive ``native late`` boundary:
    # the production worker can then try an independently governed
    # Open-Meteo republisher packet for the same station/day.  The fallback
    # still has to pass its own historical-OOS activation checks below; this
    # branch only prevents a stale native receipt from suppressing it.
    # ``ProbabilityEnvelope`` requires a strictly positive freshness window,
    # so an age exactly equal to the TTL is already stale as well.
    #
    # Strictly positive is not the same as usable, and on 2026-08-27 the
    # difference cost a lane.  A compile landed 44 milliseconds inside a
    # six-hour TTL: the age test passed, and forty stations were handed a P0
    # that expired 44ms after it was born.  Sixteen P0-M checkpoints then
    # inherited that window through ``_shift_window`` and the regime lane
    # fail-closed on every cycle afterwards.  What this boundary is for --
    # see the note above -- is deciding when the native receipt should stop
    # suppressing the independently governed fallback, and a receipt with
    # milliseconds left suppresses it just as completely as a fresh one
    # while producing nothing anybody can act on.  So the test is remaining
    # life, not age.
    remaining_seconds = fresh_seconds - (decision - captured).total_seconds()
    if remaining_seconds < MIN_USABLE_FRESHNESS_SECONDS:
        raise ContractError(
            "FAIL_CLOSED_NO_P0:native_ensemble_snapshot_stale"
        )
    station_id = str(
        forward_packet.get("station_id") or ""
    ).strip().upper()
    event_date = str(forward_packet.get("event_date") or "")
    timezone_name = str(
        forward_packet.get("settlement_timezone") or ""
    )
    try:
        settlement_end = (
            datetime.fromisoformat(event_date)
            .replace(tzinfo=ZoneInfo(timezone_name))
            + timedelta(days=1)
        )
    except (ValueError, TypeError) as exc:
        raise ContractError(
            "native_ensemble_p0_settlement_window_invalid"
        ) from exc
    if decision >= settlement_end:
        raise ContractError(
            "FAIL_CLOSED_NO_P0:settlement_day_already_closed"
        )
    station_row = conn.execute(
        "SELECT settlement_definition "
        "FROM calibration_station_registry WHERE station_id=?",
        (station_id,),
    ).fetchone()
    timezone_rows = conn.execute(
        "SELECT DISTINCT settlement_timezone "
        "FROM basket_target_market_registry "
        "WHERE station_id=? AND event_date=? "
        "AND settlement_timezone<>''",
        (station_id, event_date),
    ).fetchall()
    if station_row is None or len(timezone_rows) != 1:
        raise ContractError(
            "native_ensemble_p0_station_scope_invalid"
        )
    if str(timezone_rows[0][0]) != timezone_name:
        raise ContractError(
            "native_ensemble_p0_market_timezone_mismatch"
        )
    binding_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND "
        "name='calibration_station_market_timezone_binding'"
    ).fetchone()
    if binding_table is not None:
        binding = conn.execute(
            "SELECT settlement_timezone,market_registry_timezone_count "
            "FROM calibration_station_market_timezone_binding "
            "WHERE station_id=?",
            (station_id,),
        ).fetchone()
        if (
            binding is not None
            and (
                str(binding[0]) != timezone_name
                or int(binding[1]) != 1
            )
        ):
            raise ContractError(
                "native_ensemble_p0_market_timezone_binding_mismatch"
            )
    settlement_definition = str(station_row[0])
    # Charter 62: the station-local vocabulary, shared byte-for-byte with
    # the trainer's emission labels.  The trainer pre-warms every label its
    # validity window serves, so a season boundary date finds its scopes
    # already certified -- the calendar vocabulary this replaces would have
    # fail-closed all 45 stations at once on September 1.
    season_bin = station_season_bin(station_id, event_date)
    if official_snapshot:
        lead_bucket = str(
            forward_packet.get("lead_bucket") or ""
        )
        # P0 follows the freshest run available, so its lead shortens as the
        # event day approaches: d2 two days out, then d1, then d0.  Pinning the
        # official path to d2 meant the event day itself was still priced off a
        # forecast issued two days earlier, with every later model update
        # discarded -- P0-M and P1 correct from observations and cannot recover
        # what the model itself has since learned.
        #
        # Admitting the lead here is not the same as trusting it.  Skill at d0
        # is far higher than at d2, so the weights are not interchangeable, and
        # the OOS snapshot is keyed by lead further down: a lead with no trained
        # weights fails closed there rather than silently borrowing d2's.
        if lead_bucket not in SUPPORTED_P0_LEAD_BUCKETS:
            raise ContractError(
                "native_ensemble_p0_official_scope_invalid"
            )
    else:
        lead_bucket = _lead_bucket(
            event_date=event_date,
            timezone_name=timezone_name,
            captured_at_utc=captured_at,
        )
        run_cycle = "AGGREGATOR_UNKNOWN_NATIVE_CYCLE"
    if official_snapshot and native_oos_snapshot is not None:
        try:
            snapshot = (
                native_oos_snapshot
                if isinstance(native_oos_snapshot, NativeOosSnapshot)
                else validate_native_oos_snapshot_manifest(native_oos_snapshot)
            )
            # The scope is not known yet -- it depends on the run this packet
            # carries -- so only establish here that the station is attested at
            # all.  The binding for the scope in hand is resolved per candidate
            # activation below, once the lead is settled.
            native_pointer_snapshot = snapshot
            station_attested = station_has_pointer(snapshot, station_id)
        except ContractError as exc:
            raise ContractError(
                "FAIL_CLOSED_NO_P0:native_oos_snapshot_manifest_invalid"
            ) from exc
        if not station_attested:
            raise ContractError(
                "FAIL_CLOSED_NO_P0:native_current_pointer_binding_missing"
            )
    systems_raw = forward_packet.get("systems")
    intervals_raw = forward_packet.get("bucket_intervals")
    if (
        not isinstance(systems_raw, list)
        or len(systems_raw) < 2
        or not isinstance(intervals_raw, Mapping)
    ):
        raise ContractError(
            "native_ensemble_p0_forward_members_missing"
        )
    observed_systems = {
        str(system["ensemble_system_id"]): system
        for system in systems_raw
        if isinstance(system, Mapping)
    }
    if len(observed_systems) != len(systems_raw):
        raise ContractError("native_ensemble_p0_system_identity_invalid")
    observed_system_ids = sorted(observed_systems)
    official_lineage = (
        _official_forward_lineage(
            forward_packet,
            observed_system_ids=observed_system_ids,
            captured=captured,
        )
        if official_snapshot
        else {
            "mixed": False,
            "run_cycle": "AGGREGATOR_UNKNOWN_NATIVE_CYCLE",
            "run_cycle_scope": "AGGREGATOR_NATIVE_CYCLE_UNKNOWN",
            "run_cycle_by_system": {
                system_id: "AGGREGATOR_UNKNOWN_NATIVE_CYCLE"
                for system_id in observed_system_ids
            },
            "run_init_utc_by_system": {},
            "archive_query_at_utc_by_system": {},
            "availability_evidence": None,
        }
    )
    run_cycle = str(official_lineage["run_cycle"])
    # Two different questions share the name "lead", and conflating them is
    # what stranded the retrained weights.
    #
    # ``lead_bucket`` is the calendar label: how far from the event day this
    # decision is being made.  Station policy, the TAF fingerprint identity and
    # the operator-facing scope are all keyed by it, and it stays exactly as the
    # capture stated it.
    #
    # ``training_lead_bucket`` is the group the weights were fitted in.  OOS
    # switched that to lead measured in hours to the station's own local day,
    # because the calendar groups overlap: d1 and d2 both contain runs issued
    # 18-31h ahead, and d2 alone spans 37h.  Asking for weights under the
    # calendar label after that switch finds nothing -- no calendar-keyed
    # artifact is written any more -- so P0 has to ask under the key the fit
    # used.
    hour_bucket_candidates, hour_bucket_by_system = _hour_lead_buckets(
        event_date=event_date,
        timezone_name=timezone_name,
        run_init_utc_by_system=official_lineage["run_init_utc_by_system"],
        observed_system_ids=observed_system_ids,
    )
    hour_lead_bucket = (
        hour_bucket_candidates[0] if hour_bucket_candidates else None
    )
    # The calendar labels d0/d1/d2 are retired vocabulary for the official
    # native lane: no native artifact is written under them any more and the
    # historical ones are being backfilled to hour buckets, so falling back
    # is asking for weights under a key that by decree holds nothing.  The
    # republisher fallback is the one legitimate exception -- an aggregator
    # packet states no native run initialisation at all, its cycle is
    # AGGREGATOR_UNKNOWN_NATIVE_CYCLE, and its separately governed artifacts
    # are calendar-keyed by construction.
    if hour_lead_bucket is None and official_snapshot:
        raise ContractError(
            "FAIL_CLOSED_NO_P0:native_lead_hour_bucket_unresolved"
        )
    if hour_bucket_candidates:
        # Every bucket the packet touches, freshest first, each followed by
        # its cold-start chain.  A split packet's second bucket is a real
        # trained grouping, not a guess; a virgin bucket's chain borrows the
        # nearest shorter trained one.  First admissible activation wins.
        seen: list[str] = []
        for bucket in hour_bucket_candidates:
            for candidate in HOUR_LEAD_FALLBACK_CHAIN.get(
                bucket, (bucket,)
            ):
                if candidate not in seen:
                    seen.append(candidate)
        lead_candidates: tuple[str, ...] = tuple(seen)
    else:
        lead_candidates = (lead_bucket,)
    # The systems carrying the packet's freshest run, by hour bucket; charter
    # 88's borrow and tie-break are about seating these at their own cycle.
    freshest_bucket_system_ids = sorted(
        system_id
        for system_id, bucket in hour_bucket_by_system.items()
        if hour_lead_bucket is not None and bucket == hour_lead_bucket
    )
    training_lead_bucket = _activated_lead_bucket(
        conn,
        station_id=station_id,
        season_bin=season_bin,
        decision_at_utc=decision_at_utc,
        observed_system_ids=observed_system_ids,
        candidates=lead_candidates,
        packet_cycle_by_system={
            str(k): str(v or "")
            for k, v in official_lineage["run_cycle_by_system"].items()
        },
        freshest_bucket_system_ids=freshest_bucket_system_ids,
    )
    provisional_lead_fallback = bool(
        hour_bucket_candidates
        and training_lead_bucket not in hour_bucket_candidates
    )
    hour_bucket_split_riders = sorted(
        system_id
        for system_id, bucket in hour_bucket_by_system.items()
        if bucket != training_lead_bucket
    ) if hour_bucket_candidates else []
    activation_sql = (
        "SELECT a.activation_id,a.artifact_id,a.content_sha256,"
        "a.generation,a.valid_from_utc,"
        "a.available_system_set_hash_or_star,"
        "r.ensemble_system_ids_json,r.ensemble_system_count,"
        "r.run_cycle,a.run_cycle_by_system_json,"
        "a.run_cycle_by_system_sha256,"
        "r.run_cycle_by_system_json,"
        "r.run_cycle_by_system_sha256,"
        "r.selection_evidence_sha256,"
        "r.content_sha256,"
        "r.combination_method "
        "FROM calibration_native_ensemble_activation a "
        "JOIN calibration_cross_native_ensemble_artifact r "
        "ON r.cross_ensemble_artifact_id=a.artifact_id "
        "WHERE a.artifact_kind='CROSS_NATIVE_ENSEMBLE' "
        "AND a.station_id=? AND a.season_bin=? AND a.lead_bucket=? "
        "AND a.authority_scope='PRODUCTION_NO_LIVE_ONLY' "
        "AND a.valid_from_utc<=? AND a.valid_until_utc>? "
        "ORDER BY a.generation DESC,a.activation_id LIMIT 256"
    )
    activation_parameters: tuple[Any, ...] = (
        station_id,
        season_bin,
        training_lead_bucket,
    )
    activation_parameters += (decision_at_utc, decision_at_utc)
    activation_rows = conn.execute(
        activation_sql,
        activation_parameters,
    ).fetchall()
    observed_set = set(observed_system_ids)
    compatible_activations: list[
        tuple[Any, list[str]]
    ] = []
    rejected_or_unproven_activation_seen = False
    for row in activation_rows:
        if station_attested:
            # Resolve the attestation for this activation's own scope.  The
            # pointer names one activation by id and content hash, so it can
            # only speak for the scope that activation was fitted in; a
            # station attested at another lead has nothing to say about this
            # one, and treating its pointer as authoritative here rejected
            # every correctly calibrated candidate.
            native_pointer_binding = pointer_for_scope(
                native_pointer_snapshot,
                station_id,
                season_bin=season_bin,
                lead_bucket=training_lead_bucket,
                run_cycle=str(row[8] or ""),
            )
            if native_pointer_binding is None:
                continue
            pointer_mismatches = pointer_activation_mismatch_reasons(
                native_pointer_binding,
                activation_id=str(row[0]),
                artifact_id=str(row[1]),
                activation_sha256=str(row[2]),
                artifact_sha256=str(row[14] or ""),
                selection_evidence_sha256=str(row[13]),
                generation=int(row[3]),
                season_bin=season_bin,
                lead_bucket=training_lead_bucket,
                run_cycle=str(row[8] or ""),
                run_cycle_by_system_json=str(row[9] or ""),
                authority_scope="PRODUCTION_NO_LIVE_ONLY",
            )
            if pointer_mismatches:
                continue
        selected_ids = sorted(
            str(value) for value in json.loads(str(row[6]))
        )
        packet_cycle_mapping = {
            system_id: str(
                official_lineage["run_cycle_by_system"].get(system_id)
                or ""
            )
            for system_id in selected_ids
        }
        # The activation's identity is the pairing it was fitted and
        # evaluated under -- its own recorded cycle map -- not whatever runs
        # today's capture happens to carry.  Requiring the packet's cycles to
        # equal the fitted map re-imposed same-cycle capture on a lane that
        # reads each family at its own newest published run, so every daytime
        # packet (ECMWF/GEFS on 06Z beside ICON/ECCC on 00Z) failed closed.
        # The fitted identity keeps guarding the certificate below; a system
        # whose packet run differs from the fitted pairing serves as a named
        # cycle rider, and activations whose fitted map matches the packet
        # outrank those that need riders.
        try:
            fitted_cycle_mapping = {
                str(key): str(value)
                for key, value in json.loads(str(row[9] or "{}")).items()
            }
        except ValueError:
            continue
        fitted_cycle_mapping_json = _json(fitted_cycle_mapping)
        fitted_cycle_mapping_sha = content_sha256(fitted_cycle_mapping)
        fitted_run_cycles = set(fitted_cycle_mapping.values())
        fitted_run_cycle = (
            next(iter(fitted_run_cycles))
            if len(fitted_run_cycles) == 1
            else MIXED_NATIVE_CYCLES
        )
        if (
            len(selected_ids) != int(row[7])
            or len(set(selected_ids)) != len(selected_ids)
            or str(row[5]) != content_sha256(selected_ids)
            or not set(selected_ids).issubset(observed_set)
            or not all(packet_cycle_mapping.values())
            or sorted(fitted_cycle_mapping) != selected_ids
            or not all(fitted_cycle_mapping.values())
            or str(row[8] or "") != fitted_run_cycle
            or str(row[9] or "") != fitted_cycle_mapping_json
            or str(row[10] or "") != fitted_cycle_mapping_sha
            or str(row[11] or "") != fitted_cycle_mapping_json
            or str(row[12] or "") != fitted_cycle_mapping_sha
        ):
            continue
        # A selected system may legitimately be absent from the packet's
        # cycle mapping, and indexing it raised a bare KeyError instead of
        # deciding anything (measured 2026-08-15: ZUUU died on
        # KeyError:'OFFICIAL_AIRPORT_TAF_TX' every drain).  Two ways it
        # happens, and neither is an error:
        #
        # * a forecast bystander -- TAF and the METAR physical path carry an
        #   issue time rather than a run init, so clause 47 keeps them out of
        #   cycle voting by construction; a legacy selection row that still
        #   names one is demoted under clause 42, not matched against a cycle
        #   it structurally cannot have;
        # * a system the packet does not carry this round, which the rider
        #   mechanism exists to name.
        #
        # Comparing only what both sides actually hold keeps the rider list
        # meaning "fitted and packet disagree", which is what downstream
        # activation reads it as.
        activation_cycle_riders = sorted(
            system_id
            for system_id in selected_ids
            if system_id not in NON_RUN_LEAD_BYSTANDER_SYSTEMS
            and system_id in packet_cycle_mapping
            and system_id in fitted_cycle_mapping
            and packet_cycle_mapping[system_id]
            != fitted_cycle_mapping[system_id]
        )
        evaluation_rows = conn.execute(
            "SELECT oos_state,selection_evidence_sha256,evaluated_at_utc "
            "FROM calibration_cross_native_ensemble_oos_evaluation "
            "WHERE station_id=? AND season_bin=? AND lead_bucket=? "
            "AND run_cycle=? AND run_cycle_by_system_json=? "
            "AND run_cycle_by_system_sha256=? "
            "AND settlement_definition=? "
            "AND ensemble_system_ids_json=? "
            "AND available_system_set_hash=? AND evaluated_at_utc<=? "
            "ORDER BY evaluated_at_utc DESC,evaluation_id DESC LIMIT 2",
            (
                station_id,
                season_bin,
                training_lead_bucket,
                fitted_run_cycle,
                fitted_cycle_mapping_json,
                fitted_cycle_mapping_sha,
                settlement_definition,
                _json(selected_ids),
                str(row[5]),
                decision_at_utc,
            ),
        ).fetchall()
        if (
            not evaluation_rows
            or (
                len(evaluation_rows) > 1
                and str(evaluation_rows[0][2])
                == str(evaluation_rows[1][2])
            )
            or str(evaluation_rows[0][0])
            != "PRODUCTION_NO_LIVE_OOS_PASS"
            or str(evaluation_rows[0][1])
            != str(row[13])
        ):
            rejected_or_unproven_activation_seen = True
            continue
        compatible_activations.append(
            (
                row,
                selected_ids,
                activation_cycle_riders,
                _same_run_fresh_count(
                    selected_ids,
                    fitted_cycle_mapping,
                    packet_cycle_mapping,
                    freshest_bucket_system_ids,
                ),
            )
        )
    # Exact packet match, then least tolerated mismatch, then freshest fit,
    # and only then breadth.
    #
    # Breadth used to rank second, and that inverted what a cycle rider
    # means.  A rider is a mismatch the activation tolerates -- the packet's
    # run differs from the pairing the calibration was fitted under -- so
    # ranking a blend above another *because* it seats more systems, when
    # those extra seats are exactly the ones needing riders, prefers the
    # mismatch.  ZUUU 2026-08-16 is the specimen: a train_end 08-09 row
    # seating ICON, ECMWF and GEFS on two riders outranked the train_end
    # 08-10 row seating ECCC and ECMWF on one, permanently.  The stale row
    # was fitted before the 2026-08-15 membership ruling, so it still
    # carried the retired equal-family graft, and ICON was serving 33.3% of
    # the blend on a quota that no longer exists and a seat it cannot win
    # today (it has no exact shared holdout at this station, hence no
    # calibration of its own and no candidacy).  Breadth can never age out
    # on its own: a row that seated a system before that system was
    # eliminated will always seat more of them.
    # Charter 88 (2026-09-06): after the rider count and before the fit's
    # freshness, prefer the fit that seats the packet's freshest-run systems
    # at their own cycle -- the day's last ECMWF/GEFS run over yesterday's
    # split riders when both are zero-rider fits.
    # Charter 134: under the stable pool, a pool seat outranks every contest
    # artifact.  Contest rows stay valid for up to thirty days after the
    # switch, and without this key an older pair that happens to match the
    # packet would keep winning the scopes the OOS rotation has not reached
    # yet -- or has reached, and minted the pool for.  Where no pool seat is
    # compatible yet, the contest rows still serve rather than failing closed.
    pool_first = P0_MEMBERSHIP_MODE == P0_MEMBERSHIP_MODE_STABLE_POOL
    def _seats_usable(member_ids: Sequence[str]) -> bool:
        # The same reading the freshness check below makes of the seat the
        # compile finally serves, asked of every candidate before choosing.
        seated_at = seated_evidence_received_at(
            official_lineage["archive_query_at_utc_by_system"],
            seated_system_ids=list(member_ids),
        ) if official_snapshot else None
        received = min(seated_at, captured) if seated_at is not None else captured
        return (
            fresh_seconds - (decision - received).total_seconds()
            >= MIN_USABLE_FRESHNESS_SECONDS
        )

    compatible_activations.sort(
        key=lambda item: _activation_rank(
            item,
            observed_set=observed_set,
            pool_first=pool_first,
            seats_usable=_seats_usable,
        ),
        reverse=True,
    )
    activations = compatible_activations[:2]
    if not activations:
        if station_attested:
            raise ContractError(
                "FAIL_CLOSED_NO_P0:native_current_pointer_not_bound"
            )
        if rejected_or_unproven_activation_seen:
            raise ContractError(
                "FAIL_CLOSED_NO_P0:"
                "native_cross_oos_evaluation_not_passing"
            )
        raise ContractError(
            "FAIL_CLOSED_NO_P0:"
            "native_cross_ensemble_activation_not_exact"
        )
    # Ambiguous means the sort above could not separate them -- not merely
    # that they seat the same systems at the same generation.  The guard
    # exists so a tie never falls through to ``activation_id``, which is
    # arbitrary; testing fewer keys than the sort uses turns a decided
    # ranking back into a refusal.
    #
    # MPMG 2026-08-17 is the specimen: two generation-3 activations, both
    # seating ECMWF and GEFS, one fitted at 00Z and one at mixed cycles.
    # The packet is 00Z, so the sort ranked the exactly-fitted row first on
    # zero riders and the mixed one second on one -- exactly the preference
    # the rider ordering was added for -- and then this guard declared them
    # indistinguishable on membership and generation alone.  The station had
    # no forward P0 at all for as long as both rows stayed in window, which
    # is permanent: activations are append-only and the pair was minted in
    # the same generation.
    # Charter 134 (2026-09-14, the first station rotated after the deploy):
    # one build mints the same pool set in its exact and its shared-window
    # cohort, with the same activation instant and generation, so for a
    # packet both fitted maps serve with the same riders the two tie on every
    # key and this guard failed WMKK closed.  Both are certified seats of the
    # same systems under the pool rule, so the rank's last key (the content
    # addressed activation id) picks one deterministically -- the same one
    # every cycle -- instead of refusing P0.  The guard also reads the
    # same-run fresh count now: charter 88 added that key to the rank and not
    # here, so a pair the rank had separated on it could still be refused.
    if _activations_ambiguous(activations, pool_first=pool_first):
        raise ContractError(
            "FAIL_CLOSED_NO_P0:"
            "native_cross_ensemble_activation_not_exact"
        )
    (
        selected_activation,
        activation_member_ids,
        activation_cycle_riders,
        _selected_same_run_fresh_count,
    ) = activations[0]
    # Charter 88: name the borrow.  Set only when the packet is served under
    # a bucket other than its freshest one *and* the fit seats freshest-run
    # systems at their own cycle -- a split packet served under its older
    # bucket for its older systems is the pre-ruling grouping, not a borrow.
    try:
        _selected_fitted_cycles = {
            str(key): str(value)
            for key, value in json.loads(
                str(selected_activation[9] or "{}")
            ).items()
        }
    except (TypeError, ValueError, AttributeError):
        _selected_fitted_cycles = {}
    _borrowed_system_ids = sorted(
        system_id
        for system_id in activation_member_ids
        if system_id in set(freshest_bucket_system_ids)
        and str(_selected_fitted_cycles.get(system_id) or "")
        and str(_selected_fitted_cycles.get(system_id) or "")
        == str(official_lineage["run_cycle_by_system"].get(system_id) or "")
    )
    same_run_lead_borrow = (
        {
            "packet_freshest_bucket": hour_lead_bucket,
            "served_bucket": training_lead_bucket,
            "borrowed_system_ids": _borrowed_system_ids,
            "rule": SAME_RUN_LEAD_BORROW_RULE,
        }
        if hour_lead_bucket is not None
        and training_lead_bucket != hour_lead_bucket
        and _borrowed_system_ids
        else None
    )
    # Clause 42 (2026-08-15): point forecasts are never P0 members.  Legacy
    # activations fitted before the ruling still name them in the membership
    # set, and a packet that does not carry the point-forecast lane (a stale
    # TAF is ordinary) died on a raw KeyError right below, before the weight
    # demotion further down could speak.  Two vocabularies from here on:
    # ``activation_member_ids`` is the membership the artifact, evaluation
    # and selection evidence were fitted under and is what every identity
    # check must keep holding them to; ``system_ids`` is the numerical-model
    # set that actually serves, and is what every packet index, calibration
    # loop and served lineage uses.  The demoted weight is still named by
    # the demotion block below.
    system_ids = [
        system_id
        for system_id in activation_member_ids
        # Charter 95: only the TAF and the METAR path are stripped; a
        # regional deterministic model the activation seats is served.
        if not is_fusion_excluded_point_system(system_id)
    ]
    if len(system_ids) < 2:
        raise ContractError(
            "FAIL_CLOSED_NO_P0:"
            "p0_membership_below_two_after_point_forecast_demotion"
        )
    systems = {
        system_id: observed_systems[system_id]
        for system_id in system_ids
    }
    if official_snapshot:
        source_archives = forward_packet.get("source_archives")
        if not isinstance(source_archives, list):
            raise ContractError(
                "FAIL_CLOSED_NO_P0:native_official_transport_lineage_missing"
            )
        transport_provider_by_system: dict[str, str] = {}
        for source_archive in source_archives:
            if not isinstance(source_archive, Mapping):
                raise ContractError(
                    "FAIL_CLOSED_NO_P0:native_official_transport_lineage_missing"
                )
            source_system_id = str(
                source_archive.get("ensemble_system_id") or ""
            ).strip()
            if source_system_id not in systems:
                continue
            transport_provider_id = str(
                source_archive.get("transport_provider_id") or ""
            ).strip()
            if (
                source_system_id in transport_provider_by_system
                and transport_provider_by_system[source_system_id]
                != transport_provider_id
            ):
                raise ContractError(
                    "FAIL_CLOSED_NO_P0:native_official_transport_lineage_ambiguous"
                )
            transport_provider_by_system[source_system_id] = (
                transport_provider_id
            )
        systems_by_id: dict[str, dict[str, Any]] = {}
        for system_id in system_ids:
            system_registry = conn.execute(
                "SELECT independence_family_id,provider_id,"
                "expected_member_count FROM calibration_native_ensemble_system "
                "WHERE ensemble_system_id=?",
                (system_id,),
            ).fetchone()
            if system_registry is None:
                raise ContractError(
                    "native_ensemble_p0_system_unregistered"
                )
            systems_by_id[system_id] = {
                "ensemble_system_id": system_id,
                "independence_family_id": str(system_registry[0]),
                "provider_id": str(system_registry[1]),
                "expected_member_count": int(system_registry[2]),
                "source_class": "NATIVE_ENSEMBLE_PROVIDER",
                "aggregator_or_republisher": 0,
            }
        try:
            validate_native_ensemble_transport_gate(
                selected_system_ids=system_ids,
                systems_by_id=systems_by_id,
                transport_provider_by_system=transport_provider_by_system,
            )
        except ContractError as exc:
            raise ContractError(
                "FAIL_CLOSED_NO_P0:" + str(exc)
            ) from exc
    excluded_observed_system_ids = sorted(
        observed_set - set(system_ids)
    )
    available_set_hash = content_sha256(activation_member_ids)
    activation_id = str(selected_activation[0])
    artifact_id = str(selected_activation[1])
    activation_sha = str(selected_activation[2])
    cross_artifact_run_cycle = str(selected_activation[8] or "")
    if not cross_artifact_run_cycle:
        raise ContractError(
            "FAIL_CLOSED_NO_P0:native_cross_ensemble_cycle_missing"
        )
    selected_run_cycle = cross_artifact_run_cycle
    run_cycle = selected_run_cycle
    artifact = conn.execute(
        "SELECT ensemble_system_ids_json,ensemble_system_count,"
        "independent_family_count,selection_evidence_sha256,"
        "family_weight_caps_json,content_sha256,"
        "run_cycle_by_system_json,run_cycle_by_system_sha256,"
        "combination_method "
        "FROM calibration_cross_native_ensemble_artifact "
        "WHERE cross_ensemble_artifact_id=? "
        "AND probability_pathway=? AND valid_from_utc<=? "
        "AND valid_until_utc>?",
        (
            artifact_id,
            P0_PROBABILITY_PATHWAY,
            decision_at_utc,
            decision_at_utc,
        ),
    ).fetchone()
    if artifact is None:
        raise ContractError(
            "FAIL_CLOSED_NO_P0:native_cross_ensemble_artifact_missing"
        )
    selected_system_ids = json.loads(str(artifact[0]))
    # The identity to hold the artifact, evaluation and selection evidence to
    # is the pairing the activation was fitted under -- its own recorded
    # cycle map -- not the runs today's capture carries.  Rebuilding the
    # expectation from the packet re-imposed same-cycle capture here after
    # the activation match had already tolerated named cycle riders, so the
    # same daytime split that passed one gate died at the next.
    expected_cycle_mapping_json = str(selected_activation[11] or "")
    expected_cycle_mapping_sha = str(selected_activation[12] or "")
    if (
        selected_system_ids != activation_member_ids
        or int(artifact[1]) != len(activation_member_ids)
        or int(artifact[2]) < 2
        or str(artifact[6] or "") != expected_cycle_mapping_json
        or str(artifact[7] or "") != expected_cycle_mapping_sha
    ):
        raise ContractError(
            "FAIL_CLOSED_NO_P0:native_cross_ensemble_mask_mismatch"
        )
    selection_sha = str(artifact[3])
    cross_artifact_sha = str(artifact[5])
    latest_oos_evaluations = conn.execute(
        "SELECT oos_state,selection_evidence_sha256,evaluated_at_utc "
        "FROM calibration_cross_native_ensemble_oos_evaluation "
        "WHERE station_id=? AND season_bin=? AND lead_bucket=? "
        "AND run_cycle=? AND run_cycle_by_system_json=? "
        "AND run_cycle_by_system_sha256=? "
        "AND settlement_definition=? AND ensemble_system_ids_json=? "
        "AND available_system_set_hash=? AND evaluated_at_utc<=? "
        "ORDER BY evaluated_at_utc DESC,evaluation_id DESC LIMIT 2",
        (
            station_id,
            season_bin,
            training_lead_bucket,
            selected_run_cycle,
            expected_cycle_mapping_json,
            expected_cycle_mapping_sha,
            settlement_definition,
            _json(activation_member_ids),
            available_set_hash,
            decision_at_utc,
        ),
    ).fetchall()
    if (
        not latest_oos_evaluations
        or (
            len(latest_oos_evaluations) > 1
            and str(latest_oos_evaluations[0][2])
            == str(latest_oos_evaluations[1][2])
        )
    ):
        raise ContractError(
            "FAIL_CLOSED_NO_P0:native_cross_oos_evaluation_not_exact"
        )
    latest_oos_evaluation = latest_oos_evaluations[0]
    if (
        str(latest_oos_evaluation[0])
        != "PRODUCTION_NO_LIVE_OOS_PASS"
        or str(latest_oos_evaluation[1]) != selection_sha
    ):
        raise ContractError(
            "FAIL_CLOSED_NO_P0:native_cross_oos_evaluation_not_passing"
        )
    selection = conn.execute(
        "SELECT selected_weights_json,family_weights_json,"
        "family_weight_caps_json,"
        "cross_system_residual_correlation_json,content_sha256,"
        "train_end_date,run_cycle_by_system_json,"
        "run_cycle_by_system_sha256 "
        "FROM calibration_cross_native_ensemble_selection_evidence "
        "WHERE content_sha256=?",
        (selection_sha,),
    ).fetchone()
    if selection is None:
        raise ContractError(
            "FAIL_CLOSED_NO_P0:native_selection_evidence_missing"
        )
    if (
        str(selection[6] or "") != expected_cycle_mapping_json
        or str(selection[7] or "") != expected_cycle_mapping_sha
    ):
        raise ContractError(
            "FAIL_CLOSED_NO_P0:native_selection_cycle_mapping_mismatch"
        )
    system_weights = {
        str(key): float(value)
        for key, value in json.loads(str(selection[0])).items()
    }
    # Stage ruling, 2026-08-15: the P0 combination is numerical models only.
    # The forecaster's TX acts at P0-M and as envelope evidence (the witness
    # union below is untouched), never as a P0 member.  Selection rows fitted
    # before the ruling can still carry a point-forecast weight -- ZUUU's
    # bound row served TAF at 0.216 -- so the weight is demoted to a rider
    # here and the remainder renormalised, with the demotion named in the
    # lineage rather than inferred.  New fits exclude point forecasts from
    # candidacy, so this path decays to a no-op as rows regenerate.
    p0_membership_demotions: dict[str, float] = {}
    for demoted_id in sorted(
        system_id for system_id in POINT_FORECAST_SYSTEMS
        if is_fusion_excluded_point_system(system_id)
    ):
        if demoted_id not in system_weights:
            continue
        # Popped, not zeroed: membership was already stripped from
        # ``system_ids`` at activation unpack, so a lingering key here would
        # fail the selection-mask subset check below for exactly the rows
        # this demotion exists to serve.
        weight = float(system_weights.pop(demoted_id) or 0.0)
        if weight > EPS:
            p0_membership_demotions[demoted_id] = weight
    if p0_membership_demotions:
        remaining = sum(system_weights.values())
        if remaining <= EPS:
            raise ContractError(
                "FAIL_CLOSED_NO_P0:p0_membership_all_demoted"
            )
        system_weights = {
            system_id: (
                value / remaining if value > EPS else 0.0
            )
            for system_id, value in system_weights.items()
        }
    family_caps = {
        str(key): float(value)
        for key, value in json.loads(str(selection[2])).items()
    }
    # A provisional rider is selected into the combination to accumulate a
    # forward record but certifies at zero weight until its own OOS window
    # fills (the ruling: enter P0 now, samples keep accruing, OOS follows).
    # It therefore contributes nothing to the served distribution and is
    # exempt from the per-system calibration demanded of weighted members.
    # Every *weighted* member must still be among the packet's systems.
    provisional_rider_ids = {
        system_id
        for system_id in systems
        if float(system_weights.get(system_id) or 0.0) <= EPS
    }
    weighted_ids = set(systems) - provisional_rider_ids
    if not set(system_weights).issubset(systems.keys()):
        raise ContractError(
            "FAIL_CLOSED_NO_P0:native_selection_mask_mismatch"
        )
    if len(weighted_ids) < 2:
        raise ContractError(
            "FAIL_CLOSED_NO_P0:native_selection_mask_mismatch"
        )
    system_probabilities: dict[str, dict[str, float]] = {}
    system_artifact_shas: dict[str, str] = {}
    system_activation_shas: dict[str, str] = {}
    aggregator_strict_oos_proof_by_system: dict[str, dict[str, Any]] = {}
    calibrated_system_centers_c: dict[str, float] = {}
    system_bias_c: dict[str, float] = {}
    family_by_system: dict[str, str] = {}
    provisional_uncalibrated_system_ids: list[str] = []
    provisional_parameter_sources: dict[str, dict[str, Any]] = {}
    system_spread_factor: dict[str, float] = {}
    deferred_provisional: list[tuple[str, Any, str]] = []
    for system_id in system_ids:
        if system_id in provisional_rider_ids:
            # Zero certified weight: no calibration demanded, no probability
            # contributed; the member rides for the forward record only.
            continue
        system_config = conn.execute(
            (
                "SELECT independence_family_id,expected_member_count "
                "FROM calibration_native_ensemble_system "
                "WHERE ensemble_system_id=?"
                if official_snapshot
                else
                "SELECT independence_family_id,expected_member_count "
                "FROM calibration_republisher_ensemble_product "
                "WHERE ensemble_system_id=? "
                "AND transport_provider_id='OPEN_METEO' "
                "AND source_role='aggregator_republisher' "
                "AND native_arrival_index_eligible=0 "
                "AND formal_p0_eligible=0"
            ),
            (system_id,),
        ).fetchone()
        if system_config is None and official_snapshot:
            # Point forecasts are registered in code, next to their packet
            # schema; the table's plural-member CHECK stays for ensembles.
            point = POINT_FORECAST_SYSTEMS.get(system_id)
            if point is not None:
                system_config = (
                    str(point["independence_family_id"]),
                    int(point["expected_member_count"]),
                )
        if system_config is None:
            raise ContractError(
                (
                    "native_ensemble_p0_system_unregistered"
                    if official_snapshot
                    else "republisher_p0_product_unregistered"
                )
            )
        family_by_system[system_id] = str(system_config[0])
        members = systems[system_id].get("members")
        if (
            not isinstance(members, list)
            or len(members) != int(system_config[1])
        ):
            raise ContractError(
                "native_ensemble_p0_member_set_incomplete"
            )
        system_run_cycle = str(
            official_lineage["run_cycle_by_system"].get(system_id)
            or ""
        )
        if not system_run_cycle:
            raise ContractError(
                "FAIL_CLOSED_NO_P0:native_system_cycle_lineage_missing"
            )
        system_activation = conn.execute(
            "SELECT activation_id,artifact_id,content_sha256,generation,"
            "run_cycle_by_system_json,run_cycle_by_system_sha256 "
            "FROM calibration_native_ensemble_activation "
            "WHERE artifact_kind="
            "'NATIVE_ENSEMBLE_SYSTEM_CALIBRATION' "
            "AND station_id=? AND ensemble_system_id_or_star=? "
            "AND season_bin=? AND lead_bucket=? AND run_cycle=? "
            "AND available_system_set_hash_or_star='*' "
            "AND run_cycle_by_system_json=? "
            "AND run_cycle_by_system_sha256=? "
            "AND authority_scope='PRODUCTION_NO_LIVE_ONLY' "
            "AND valid_from_utc<=? AND valid_until_utc>? "
            "ORDER BY generation DESC LIMIT 2",
            (
                station_id,
                system_id,
                season_bin,
                training_lead_bucket,
                system_run_cycle,
                _json({system_id: system_run_cycle}),
                content_sha256({system_id: system_run_cycle}),
                decision_at_utc,
                decision_at_utc,
            ),
        ).fetchall()
        if (
            not system_activation
            or (
                len(system_activation) > 1
                and int(system_activation[0][3])
                == int(system_activation[1][3])
            )
        ):
            if official_snapshot:
                # Provisional member: certified into the combination before
                # its own per-system calibration exists (the ruling: enter
                # P0 now at the certified weight, keep accumulating samples,
                # calibration follows when the OOS window fills).  Serving is
                # deferred to a second pass below because the parameters it
                # borrows are the station's certified peers', and those are
                # only complete once every certified system has loaded its
                # own.  Republisher packets keep the hard failure: their
                # members only enter with a strict proof.
                # Charter 101: ask this system for its own fit at another
                # run cycle before asking its peers for theirs.  Same
                # station, same season, same lead bucket -- only the cycle
                # differs, and that difference was measured at 0.198C median
                # against the 0.7-1.2C the peer path's zero costs.  Highest
                # generation, valid at this decision, and it borrows
                # parameters only: the member stays uncertified, keeps its
                # provisional weight, and its own cycle's fit replaces this
                # the moment the training lane produces one.
                borrowed_own: dict[str, Any] | None = None
                if CROSS_CYCLE_PARAMETER_BORROW == "on":
                    own_row = conn.execute(
                        "SELECT a.run_cycle, f.bias_parameters_json,"
                        " f.spread_parameters_json, f.content_sha256"
                        " FROM calibration_native_ensemble_activation a"
                        " JOIN calibration_native_ensemble_system_artifact f"
                        "   ON f.system_artifact_id = a.artifact_id"
                        "  AND f.ensemble_system_id = a.ensemble_system_id_or_star"
                        " WHERE a.artifact_kind="
                        "'NATIVE_ENSEMBLE_SYSTEM_CALIBRATION'"
                        "   AND a.station_id=? AND a.ensemble_system_id_or_star=?"
                        "   AND a.season_bin=? AND a.lead_bucket=?"
                        "   AND a.run_cycle<>?"
                        "   AND a.available_system_set_hash_or_star='*'"
                        "   AND a.authority_scope='PRODUCTION_NO_LIVE_ONLY'"
                        "   AND a.valid_from_utc<=? AND a.valid_until_utc>?"
                        "   AND f.valid_from_utc<=? AND f.valid_until_utc>?"
                        " ORDER BY a.generation DESC, a.valid_from_utc DESC"
                        " LIMIT 1",
                        (
                            station_id,
                            system_id,
                            season_bin,
                            training_lead_bucket,
                            system_run_cycle,
                            decision_at_utc,
                            decision_at_utc,
                            decision_at_utc,
                            decision_at_utc,
                        ),
                    ).fetchone()
                    if own_row is not None:
                        try:
                            borrowed_own = {
                                "from_run_cycle": str(own_row[0]),
                                "bias_parameters": json.loads(str(own_row[1])),
                                "spread_parameters": json.loads(
                                    str(own_row[2])
                                ),
                                "artifact_sha256": str(own_row[3]),
                            }
                        except (ValueError, TypeError):
                            borrowed_own = None
                deferred_provisional.append(
                    (system_id, members, system_run_cycle, borrowed_own)
                )
                continue
            raise ContractError(
                "FAIL_CLOSED_NO_P0:"
                "native_system_activation_not_exact"
            )
        system_artifact = conn.execute(
            "SELECT bias_parameters_json,spread_parameters_json,"
            "reliability_parameters_json,content_sha256,run_cycle "
            "FROM calibration_native_ensemble_system_artifact "
            "WHERE system_artifact_id=? AND ensemble_system_id=? "
            "AND valid_from_utc<=? AND valid_until_utc>?",
            (
                str(system_activation[0][1]),
                system_id,
                decision_at_utc,
                decision_at_utc,
            ),
        ).fetchone()
        if (
            system_artifact is None
            or str(system_artifact[4]) != system_run_cycle
        ):
            raise ContractError(
                "FAIL_CLOSED_NO_P0:native_system_artifact_cycle_mismatch"
            )
        # The replay override, applied after the activation and its cycle
        # check above have both passed -- it substitutes fitted numbers, it
        # does not seat anything.
        override = (artifact_parameter_override or {}).get(system_id)
        if override is not None:
            system_artifact = (
                json.dumps(
                    override.get("bias_parameters")
                    or json.loads(str(system_artifact[0])),
                    sort_keys=True,
                ),
                json.dumps(
                    override.get("spread_parameters")
                    or json.loads(str(system_artifact[1])),
                    sort_keys=True,
                ),
                json.dumps(
                    override.get("reliability_parameters")
                    or json.loads(str(system_artifact[2])),
                    sort_keys=True,
                ),
                system_artifact[3],
                system_artifact[4],
            )
        reliability_parameters = json.loads(str(system_artifact[2]))
        if not official_snapshot:
            source_scopes = reliability_parameters.get(
                "source_authority_scopes"
            )
            if not isinstance(source_scopes, list):
                raise ContractError(
                    "FAIL_CLOSED_NO_P0:"
                    "aggregator_fallback_strict_oos_proof_missing"
                )
            aggregator_proof = {
                "transport_provider_id": reliability_parameters.get(
                    "transport_provider_id"
                ),
                "holdout_source_class": reliability_parameters.get(
                    "holdout_source_class"
                ),
                "oos_event_temporal_scope": reliability_parameters.get(
                    "oos_event_temporal_scope"
                ),
                "future_event_count": reliability_parameters.get(
                    "future_event_count", 0
                ),
                "oos_min_train_days": reliability_parameters.get(
                    "oos_min_train_days"
                ),
                "oos_min_holdout_days": reliability_parameters.get(
                    "oos_min_holdout_days"
                ),
                "source_authority_scopes": sorted(
                    str(value) for value in source_scopes
                ),
            }
            if (
                aggregator_proof["transport_provider_id"] != "OPEN_METEO"
                or aggregator_proof["holdout_source_class"]
                != "HISTORICAL_ASOF_REPUBLISHER_RECEIPT"
                or aggregator_proof["oos_event_temporal_scope"]
                != "CLOSED_HISTORICAL_AWC_TRUTH"
                or aggregator_proof["future_event_count"] != 0
                or not isinstance(
                    aggregator_proof["oos_min_train_days"], int
                )
                or not isinstance(
                    aggregator_proof["oos_min_holdout_days"], int
                )
                or aggregator_proof["oos_min_train_days"] < 1
                or aggregator_proof["oos_min_holdout_days"] < 1
            ):
                raise ContractError(
                    "FAIL_CLOSED_NO_P0:"
                    "aggregator_fallback_strict_oos_proof_missing"
                )
            aggregator_strict_oos_proof_by_system[system_id] = (
                aggregator_proof
            )
        bias_parameters = json.loads(str(system_artifact[0]))
        system_probabilities[system_id] = (
            _calibrated_system_probability(
                values=[
                    float(member["raw_tmax_c"])
                    for member in members
                ],
                intervals=intervals_raw,
                bias_parameters=bias_parameters,
                spread_parameters=json.loads(str(system_artifact[1])),
                reliability_parameters=reliability_parameters,
            )
        )
        raw_center = (
            sum(float(member["raw_tmax_c"]) for member in members)
            / len(members)
        )
        # Same resolver as the histogram above, which has already refused
        # any artifact naming a form it cannot run.  Under the binned form
        # the bias a peer can see -- and borrow -- is the shift this run's
        # own level selects: the number actually moving today's centre, not
        # a bin nobody is standing in.
        resolved_bias_c = _resolved_bias_c(raw_center, bias_parameters)
        calibrated_system_centers_c[system_id] = (
            raw_center + resolved_bias_c
        )
        system_bias_c[system_id] = resolved_bias_c
        system_spread_factor[system_id] = float(
            json.loads(str(system_artifact[1]))["spread_factor"]
        )
        system_artifact_shas[system_id] = str(system_artifact[3])
        system_activation_shas[system_id] = str(
            system_activation[0][2]
        )
    # Peers are the CERTIFIED systems only, frozen before any borrowing
    # happens.  Reading the live maps inside the loop let the second
    # provisional member count the first member's borrowed values as peers
    # -- inflating the peer count past the three-peer gate and feeding a
    # borrowed number back into the median it was borrowed from.
    certified_peer_biases = sorted(system_bias_c.values())
    certified_peer_spreads = sorted(system_spread_factor.values())
    # Charter 62 (user ruling, 2026-08-25): the borrow cap is station-local
    # evidence, not a fleet constant.  The 2.0C constant was sized from the
    # largest fleet-wide offset then measured (+1.94C); the certified fits
    # have since measured +3.56C (WMKK NOAA) and +5.84C (RCSS DWD), so at
    # exactly the stations that need correction most, a capped borrow
    # dragged every provisional member 1.5-3.8C cold.  The station's own
    # certified peers bound what a borrow may claim; the fleet constant
    # stays as the floor so a station with small peers keeps the old bound.
    provisional_bias_cap_c = max(
        PROVISIONAL_BIAS_ABS_CAP_C,
        max(
            (abs(bias) for bias in certified_peer_biases),
            default=0.0,
        ),
    )
    for (
        system_id, members, system_run_cycle, borrowed_own,
    ) in deferred_provisional:
        # An uncalibrated member served with identity parameters is not
        # neutral, it is confidently wrong.  Measured 2026-08-14: ICON served
        # that way ran z-sd 2.88 with its 95% interval covering 61% of
        # outcomes and a +0.85 sigma cold bias while carrying a third of
        # ZUUU's weight.  Every system in this fleet needs 2.5x-4.7x spread
        # inflation and runs cold by 0.3-1.9C; identity parameters assert the
        # opposite of both.  The station's own certified peers in this same
        # combination have already measured both quantities here, so the
        # provisional member borrows their median -- station-local (the
        # weights ruling), bounded, and never narrower than the raw member
        # cloud.  Borrowed parameters are not a certificate: the member stays
        # named as provisional and its own fit replaces them the moment its
        # OOS window fills.
        raw_values = [float(member["raw_tmax_c"]) for member in members]
        peer_biases = certified_peer_biases
        peer_spreads = certified_peer_spreads
        # The two borrows are not symmetric and must not be governed alike.
        #
        # Borrowing spread is conservative: it widens a distribution that is
        # measured too narrow, and a wider uncertified member simply claims
        # less.  Borrow it whenever any peer has one.
        #
        # Borrowing bias moves the centre, which moves seats directly, and a
        # median over two peers is just their midpoint -- no breakdown point
        # at all.  ZUUU 2026-08-14 is the specimen: two peers whose own
        # fitted offsets are large handed ICON +3.09C when ICON's own
        # measured offset at that station is +1.29C, and the served
        # distribution's 39C+ bucket jumped from 14.7% to 30.8% on borrowed
        # arithmetic rather than evidence.  So bias needs three peers before
        # a median means anything, and is capped at the largest offset any
        # system in this fleet has actually been measured to need (+1.94C,
        # rounded to 2.0).  Under-correcting an uncertified member is the
        # safe error: it stays near its raw centre while its borrowed spread
        # covers the difference.  Over-correcting is not.
        borrow_notes: list[str] = []
        # Charter 101: the system's own fit at another cycle outranks the
        # peer median, and outranks it for the reason the ZUUU 2026-08-14
        # specimen above gives -- two peers handed ICON +3.09C where ICON's
        # own measurement was +1.29C.  A borrow from oneself cannot make
        # that error.  Uncapped: it is this system's own number.
        if borrowed_own is not None:
            provisional_bias_c = float(
                borrowed_own["bias_parameters"].get("bias_c") or 0.0
            )
            provisional_spread_factor = max(
                1.0,
                float(
                    borrowed_own["spread_parameters"].get("spread_factor")
                    or 1.0
                ),
            )
            borrow_notes.append(
                f"own_fit_from_run_{borrowed_own['from_run_cycle']}"
            )
        elif len(peer_biases) >= PROVISIONAL_BIAS_MIN_PEERS:
            median_bias = peer_biases[len(peer_biases) // 2]
            provisional_bias_c = max(
                -provisional_bias_cap_c,
                min(provisional_bias_cap_c, median_bias),
            )
            if provisional_bias_c != median_bias:
                borrow_notes.append(
                    f"bias_capped_from_{median_bias:.2f}C"
                )
        else:
            provisional_bias_c = 0.0
            borrow_notes.append(
                f"bias_refused_peer_count_{len(peer_biases)}"
            )
        if borrowed_own is None:
            provisional_spread_factor = max(
                1.0,
                peer_spreads[len(peer_spreads) // 2] if peer_spreads else 1.0,
            )
        # reliability 1.0 zeroes the climate blend term, but the kernel still
        # demands sample shape; the members themselves satisfy it without
        # contributing anything.
        system_probabilities[system_id] = _calibrated_system_probability(
            values=raw_values,
            intervals=intervals_raw,
            bias_parameters={"bias_c": provisional_bias_c},
            spread_parameters={
                "spread_factor": provisional_spread_factor,
            },
            reliability_parameters={
                "reliability_weight": 1.0,
                "climate_samples_c": raw_values,
            },
        )
        calibrated_system_centers_c[system_id] = (
            sum(raw_values) / len(raw_values) + provisional_bias_c
        )
        system_bias_c[system_id] = provisional_bias_c
        system_spread_factor[system_id] = provisional_spread_factor
        provisional_marker = content_sha256({
            "provisional_uncalibrated_system": system_id,
            "station_id": station_id,
            "lead_bucket": training_lead_bucket,
            "run_cycle": system_run_cycle,
            "borrowed_bias_c": provisional_bias_c,
            "borrowed_spread_factor": provisional_spread_factor,
        })
        system_artifact_shas[system_id] = provisional_marker
        system_activation_shas[system_id] = provisional_marker
        provisional_uncalibrated_system_ids.append(system_id)
        provisional_parameter_sources[system_id] = {
            "source": (
                "OWN_FIT_OTHER_RUN_CYCLE" if borrowed_own is not None
                else "PEER_MEDIAN_STATION_LOCAL"
            ),
            "borrowed_from_run_cycle": (
                borrowed_own["from_run_cycle"] if borrowed_own else None
            ),
            "borrowed_artifact_sha256": (
                borrowed_own["artifact_sha256"] if borrowed_own else None
            ),
            "bias_c": provisional_bias_c,
            "spread_factor": provisional_spread_factor,
            "peer_count": len(peer_spreads),
            "bias_peer_count": len(peer_biases),
            "notes": borrow_notes,
        }
    weight_receipt = validate_cross_native_ensemble_weights(
        system_weights=system_weights,
        systems_by_id={
            system: {
                "independence_family_id": family_by_system[system],
            }
            for system in system_ids
        },
        family_weight_caps=family_caps,
    )
    ordered = _ordered_buckets(intervals_raw)
    # The bridge is applied exactly when this artifact's holdout was scored
    # with it (the combination_method carries the tag).  Serving bridged math
    # under a pre-bridge artifact would put live probabilities outside what
    # the OOS gate measured; the plain mixture remains byte-identical for
    # those artifacts until the resident OOS rotation regenerates them.
    artifact_combination_method = str(artifact[8] or "")
    if CONSENSUS_BRIDGE_METHOD_TAG in artifact_combination_method:
        probability, consensus_bridge_receipt = bridged_cross_probability(
            system_probabilities=system_probabilities,
            system_weights=system_weights,
            system_centers_c=calibrated_system_centers_c,
            ordered_keys=ordered,
            key_centers_c=_bucket_centers(intervals_raw, ordered),
        )
    else:
        consensus_bridge_receipt = None
        probability = {
            str(bucket): sum(
                system_weights[system]
                * system_probabilities[system][str(bucket)]
                for system in system_ids
            )
            for bucket in intervals_raw
        }
        total = sum(probability.values())
        probability = {
            bucket: value / total
            for bucket, value in probability.items()
        }
    # The basket anchor is where the models actually point, not whichever
    # bucket holds the most mass.  An open-ended edge bucket ("35°C or higher")
    # covers an unbounded interval, so it accumulates the entire upper tail of
    # every system: with ZUUU's 3.5°C system disagreement (ECMWF 30.87 vs GEFS
    # 34.39) that bucket carried 0.3344 and won an argmax over 1°C buckets --
    # outside the model envelope of 31°C..34°C, which the two centers define.
    # Weighting the calibrated centers by the same system weights used for the
    # probability mixture gives one temperature, and the bucket containing it
    # is the anchor.  An edge bucket is still eligible; it simply has to be
    # where the models point (centers of 36/37 land there legitimately).
    model_center_c = served_model_center_c(
        system_weights, calibrated_system_centers_c, system_ids
    )
    model_center_bucket = _bucket_for_value(model_center_c, intervals_raw)
    lower_center_c = min(calibrated_system_centers_c.values())
    upper_center_c = max(calibrated_system_centers_c.values())
    # The envelope is the range the evidence points at.  The TAF TX is a
    # forecaster's revision of these same models and joins the min/max as one
    # more center: it can only widen the admitted range, never narrow it, and
    # when no TAF carries a TX the envelope is exactly the model pair.
    taf_witness = _taf_envelope_witness(
        conn,
        station_id=station_id,
        event_date=event_date,
        decision_at_utc=decision_at_utc,
    )
    # Always P0.  P0-M is the resident confirmation lane's stage: the one
    # that roots on the newest P0 checkpoint, binds the revision and proof,
    # and is what P1 and the decision loop resolve against (charter 52f's
    # "the forecaster-driven update carries the P0-M label" is that lane's
    # job since 2026-08-31).  This compiler used to label a recompile P0-M
    # when the seated run set was unchanged and a TAF issuance had advanced
    # past the predecessor's witness -- a second P0-M semantics with no root
    # binding, which the P0 ingress then skipped as a non-P0 packet.  Because
    # the resident P0-M rows carry no ``taf_envelope_witness_json``, every
    # TAF-bearing station-day whose newest SSOT row was a confirmation read
    # as "TAF advanced" on every same-run recompile: 24 of 90 compiled
    # packets in the live P0 inbox on 2026-09-13 09:17Z were dropped that
    # way, including the re-reads that renew a P0 on its seated clock.  The
    # label was a trigger, not a stage; the trigger is not needed downstream.
    compiled_stage = "P0"
    # Once the forecaster has spoken, every later compile hears it -- the P0-M
    # that the revision drove and every P0 the next model run drives after it
    # (ruling clarified 2026-08-16).  What P0-M owns is the *stage label* on a
    # forecaster-driven update, not exclusive use of the evidence: a witness
    # that entered the envelope on one compile and left it on the next would
    # make the envelope breathe, and clause 42's union only ever widens.  The
    # opening compile has no witness to read yet, which is why the first
    # basket is the models' alone without a rule saying so.
    taf_stage_witness = taf_witness
    if taf_stage_witness is not None:
        taf_tx_c = float(taf_stage_witness["tx_c"])
        envelope_lower_c = min(lower_center_c, taf_tx_c)
        envelope_upper_c = max(upper_center_c, taf_tx_c)
    else:
        envelope_lower_c = lower_center_c
        envelope_upper_c = upper_center_c
    # The conditional tail skew is applied here, after the day's two
    # measured direction signals exist: the same-day climatological headroom
    # and the forecaster's TX against the model centre.  Keyed on the
    # artifact's combination-method tag exactly like the bridge, so the live
    # math never diverges from the holdout math that admitted the artifact.
    # Cloud, rain and mixing move the day in a direction the models miss, and
    # the forecaster publishes all three beside the TX.  This is a centre
    # shift, not a tail transform: the whole distribution is colder on a
    # cloudy windy day, so it rides on the calibrated bias rather than on the
    # tails.  Applied here, after the witness resolves, and keyed on its own
    # method tag so the holdout that admitted the artifact scored it too.
    synoptic_cooling_receipt = None
    synoptic_cooling_shift_c = 0.0
    if SYNOPTIC_COOLING_METHOD_TAG in artifact_combination_method:
        # Radiation leads: it is the same physics read from the models
        # themselves, twice as strong as the forecaster's fields, available
        # for every station-day rather than the seventeen that publish a TX,
        # and adding the TAF index on top of it gains nothing.  The
        # forecaster's fields are the fallback for a day whose radiation
        # capture is missing or whose station history is still too thin.
        shift_sigma, synoptic_cooling_receipt = (
            forecast_radiation_shift_sigma(
                shift_sigma_per_unit=pinned_radiation_shift_sigma(
                    artifact_combination_method
                ),
                radiation_sum=_forecast_radiation_sum(
                    conn, station_id=station_id, event_date=event_date
                ),
                station_history=_forecast_radiation_history(
                    conn, station_id=station_id, event_date=event_date
                ),
            )
        )
        if shift_sigma is None and taf_witness is not None:
            cooling_index, synoptic_cooling_receipt = synoptic_cooling_index(
                cloud_cover_fraction=taf_witness.get("cloud_cover_fraction"),
                precipitation_probability=taf_witness.get(
                    "precipitation_probability"
                ),
                wind_speed_mps=taf_witness.get("wind_speed_mps"),
            )
            if cooling_index is not None:
                shift_sigma = (
                    -SYNOPTIC_COOLING_SHIFT_SIGMA * float(cooling_index)
                )
        if shift_sigma is not None:
            member_spread_c = _distribution_sigma_c(
                probability, _bucket_centers(intervals_raw, ordered)
            )
            synoptic_cooling_shift_c = float(shift_sigma) * member_spread_c
            if abs(synoptic_cooling_shift_c) > EPS:
                shifted_centers = [
                    value + synoptic_cooling_shift_c
                    for value in _bucket_centers(intervals_raw, ordered)
                ]
                probability = _shift_probability_c(
                    probability,
                    ordered=ordered,
                    bucket_centers_c=shifted_centers,
                    intervals=intervals_raw,
                )
            synoptic_cooling_receipt["shift_c"] = synoptic_cooling_shift_c
    # Shape after position: the radiation shift above moves the centre, this
    # leans the buckets around wherever it landed, and the tail layer below
    # conditions what is further out.  Each touches a region the others do
    # not -- offsets one and two here, beyond that there -- so the order is
    # fixed only so the three never read each other's residue.
    neighbour_direction_receipt = None
    if NEIGHBOUR_DIRECTION_METHOD_TAG in artifact_combination_method:
        # The tilt is a property of where the station is, so it is served by
        # latitude band.  An unresolvable registry scope serves the neutral
        # band rather than guessing.
        try:
            station_latitude = float(
                station_coordinates(station_id)["latitude"]
            )
        except Exception:  # noqa: BLE001 -- unknown latitude is neutral
            station_latitude = None
        probability, neighbour_direction_receipt = (
            apply_neighbour_direction_tilt(
                probability,
                ordinals={
                    str(bucket): index
                    for index, bucket in enumerate(ordered)
                },
                center_bucket=model_center_bucket,
                latitude=station_latitude,
            )
        )
    tail_reliability_receipt = None
    if CONDITIONAL_SKEW_METHOD_TAG in artifact_combination_method:
        bucket_centers_c = _bucket_centers(intervals_raw, ordered)
        distribution_sigma = _distribution_sigma_c(
            probability, bucket_centers_c
        )
        probability, tail_reliability_receipt = apply_conditional_tail_skew(
            probability,
            ordinals={
                str(bucket): index
                for index, bucket in enumerate(ordered)
            },
            climatology_headroom_sigma=_same_day_climatology_headroom(
                conn,
                station_id=station_id,
                event_date=event_date,
                model_center_c=model_center_c,
                sigma_c=distribution_sigma,
            ),
            taf_gap_sigma=(
                (float(taf_stage_witness["tx_c"]) - model_center_c)
                / distribution_sigma
                if taf_stage_witness is not None and distribution_sigma > EPS
                else None
            ),
        )
    # Before the band layer: this moves the distribution along the temperature
    # axis, and the band factors are measured on the claim that distribution
    # produces, so the shape has to be final before the claim is corrected.
    # Only ever applied against the version the artifact names: an artifact
    # built before this existed carries no tag and is served the math it was
    # gated on, and a pinned-but-missing version fails closed inside the
    # loader rather than serving an approximation of it.
    scalars = None
    sigma_floor_rungs = 0.0
    width_tiers = None
    pinned_center_width = None
    if CENTER_WIDTH_METHOD_TAG in artifact_combination_method:
        pinned_center_width = pinned_center_width_sha(
            artifact_combination_method
        )
        if pinned_center_width is not None:
            scalars = load_center_width(
                conn, stage="P0", table_sha256=pinned_center_width
            )
            # The floor the pinned scalars were fitted under, read from the
            # same row.  A pair served without the floor it was chosen under
            # is a different arithmetic wearing the same two numbers.
            sigma_floor_rungs = load_center_width_floor(
                conn, stage="P0", table_sha256=pinned_center_width
            )
            width_tiers = load_center_width_tiers(
                conn, stage="P0", table_sha256=pinned_center_width
            )
    # The per-station width scale, exactly the version the artifact's
    # holdout was scored with.  Same fail-closed contract: the loader raises
    # on a pinned-but-missing table rather than serving an approximation.
    station_scale = 1.0
    pinned_station_width = None
    pinned_station_width_retired = False
    if STATION_WIDTH_METHOD_TAG in artifact_combination_method:
        pinned_station_width = pinned_station_width_sha(
            artifact_combination_method
        )
        if pinned_station_width is not None:
            station_table = load_station_width(
                conn, stage="P0", table_sha256=pinned_station_width
            )
            if station_table is not None:
                station_scale = float(
                    station_table[0].get(station_id, 1.0)
                )
                # An empty table under a pinned sha is the loader saying the
                # version was retired by the lane's veto; the receipt says so.
                pinned_station_width_retired = not station_table[0]
    # The next-day fleet regime shift, which is deliberately *not* pinned to
    # the artifact the way the scalars above are.  The pin exists because a
    # fitted scalar must be served at the version its holdout was scored
    # against; this layer fits nothing, and what it reads -- how far the fleet
    # settled from what was served yesterday -- cannot exist when the artifact
    # is built.  There is no version of it a gate could have scored, so the
    # risk the pin manages is not the risk this layer carries.  What bounds it
    # instead is its own cap and the fact that it measures the served
    # distribution, so it retires itself the moment the fitted layers catch up.
    regime = load_regime_shift(
        conn,
        stage="P0",
        event_date=event_date,
        as_of_utc=decision_at_utc,
    )
    regime_delta = float(regime.get("regime_delta_c") or 0.0)
    center_width_layer = (
        CenterWidthLayer(
            delta_c=scalars[0] if scalars else 0.0,
            width_lambda=scalars[1] if scalars else 1.0,
            station_width_scale=station_scale,
            regime_delta_c=regime_delta,
            sigma_floor_rungs=sigma_floor_rungs,
            width_tier_lambdas=(width_tiers or {}).get("lambdas") or None,
            width_tier_boundaries=(
                (width_tiers or {}).get("boundaries_rungs") or None
            ),
        )
        if (
            scalars is not None
            or station_scale != 1.0
            or regime_delta != 0.0
            or sigma_floor_rungs
            or width_tiers
        )
        else None
    )
    # Outermost, after every shape layer, which as of 2026-08-23 finally
    # includes the centre/width scalars above.  This layer rescales a claim,
    # and a claim rescaled here stops being the claim the moment anything
    # moves the distribution afterwards -- which is exactly what the shape
    # layer had been doing to it.  P1 has always composed them this way and
    # says why in ``materialization._posterior``: geometry before claim.
    # The order itself lives in ``served_shape``; this block only resolves
    # which table version the artifact pinned.
    band_layer, pinned_sha, pinned_axis = resolve_pinned_band_layer(
        conn, artifact_combination_method, band_table_override
    )
    probability, center_width_receipt, band_reliability_receipt = (
        apply_served_table_layers(
            probability,
            ordered_keys=ordered,
            key_centers_c=_bucket_centers(intervals_raw, ordered),
            center_width=center_width_layer,
            band=band_layer,
        )
    )
    if center_width_receipt is not None:
        center_width_receipt["fleet_regime"] = regime
        if pinned_center_width is not None:
            center_width_receipt["center_width_sha256"] = (
                pinned_center_width
            )
        if pinned_station_width is not None:
            center_width_receipt["station_width_sha256"] = (
                pinned_station_width
            )
            if pinned_station_width_retired:
                center_width_receipt["station_width_state"] = (
                    "PINNED_VERSION_RETIRED"
                )
    if band_reliability_receipt is not None:
        if pinned_sha is not None:
            band_reliability_receipt["band_table_sha256"] = pinned_sha
        if band_table_override is not None:
            band_reliability_receipt["band_table_counterfactual"] = True
            band_reliability_receipt["band_table_axis"] = pinned_axis
    model_envelope_lower_bucket = _bucket_for_value(
        envelope_lower_c,
        intervals_raw,
    )
    model_envelope_upper_bucket = _bucket_for_value(
        envelope_upper_c,
        intervals_raw,
    )
    # ``run_cycle`` above is the *calibration* scope -- the cycle the chosen
    # activation was fitted under (line ``run_cycle = selected_run_cycle``).
    # The native-forward block describes something else entirely: the runs
    # today's capture actually carries.  Once the activation match started
    # tolerating named cycle riders, those two stopped being the same value,
    # and stamping the fitted cycle here published a packet that swore every
    # system shared one cycle while its own per-system map said otherwise.
    # Materialization checks exactly that pair, so P1 fail-closed on every
    # such root: 30 of 43 scopes on 2026-08-16, and 86% of that day's P0
    # envelopes carried the contradiction.  Describe the packet from the
    # packet.
    # Per-bucket model reliability: how much of the ensemble's weight points
    # at each bucket, by the calibrated centre of every system that took part
    # (the same definition ``calibration.materialize_p0`` publishes for its
    # own lane).  The support kernel's peripheral gate reads this key and
    # admits a bucket only above ``peripheral_model_reliability_min``; until
    # 2026-09-02 no native envelope published it, so every peripheral
    # candidate read 0.0 and the disagreement-expansion layer never once ran
    # on production.  Keys are the full probability support, zeros where no
    # system centres, so a later stage's support check sees the same set.
    peripheral_model_reliability_by_bucket: dict[str, float] = {
        str(bucket): 0.0 for bucket in ordered
    }
    for system_id in system_ids:
        reliability_bucket = _bucket_for_value(
            calibrated_system_centers_c[system_id], intervals_raw
        )
        peripheral_model_reliability_by_bucket[str(reliability_bucket)] = min(
            1.0,
            peripheral_model_reliability_by_bucket.get(
                str(reliability_bucket), 0.0
            )
            + float(system_weights[system_id]),
        )
    native_forward_cycle_by_system = {
        system_id: official_lineage["run_cycle_by_system"][system_id]
        for system_id in system_ids
    }
    packet_cycles = {
        str(cycle or "").strip()
        for cycle in native_forward_cycle_by_system.values()
    }
    native_forward_same_cycle = official_snapshot and len(packet_cycles) == 1
    active_run_cycle_scope = (
        (
            "SAME_NATIVE_CYCLE"
            if native_forward_same_cycle
            else "PER_SYSTEM_NATIVE_CYCLE"
        )
        if official_snapshot
        else official_lineage["run_cycle_scope"]
    )
    native_forward_run_cycle = (
        (
            next(iter(packet_cycles))
            if native_forward_same_cycle
            else MIXED_NATIVE_CYCLES
        )
        if official_snapshot
        else run_cycle
    )
    native_forward_lineage = {
        "run_cycle_scope": active_run_cycle_scope,
        "run_cycle": native_forward_run_cycle,
        "run_cycle_by_system": native_forward_cycle_by_system,
        "run_init_utc_by_system": {
            system_id: official_lineage["run_init_utc_by_system"][system_id]
            for system_id in system_ids
            if system_id in official_lineage["run_init_utc_by_system"]
        },
        "archive_query_at_utc_by_system": {
            system_id: official_lineage[
                "archive_query_at_utc_by_system"
            ][system_id]
            for system_id in system_ids
            if system_id in official_lineage[
                "archive_query_at_utc_by_system"
            ]
        },
        "availability_evidence": official_lineage[
            "availability_evidence"
        ],
        "source_class": forward_packet.get("source_class"),
        "source_archive_set_sha256": forward_packet.get(
            "source_archive_set_sha256"
        ),
        "source_archives": forward_packet.get("source_archives", []),
        "transport_lineage_class": (
            "official_native"
            if official_snapshot
            else "aggregator_republisher"
        ),
        "native_arrival_index_eligible": official_snapshot,
    }
    # Open-Meteo is never allowed to claim native publication/arrival.  When
    # its packet is backed by an activated strict-OOS cross-ensemble artifact
    # with at least two independent families, however, it may serve as the
    # explicit no-live fallback if no official packet is available.  Keep this
    # decision separate from the native transport label so downstream inbox
    # selection can prefer official evidence without silently discarding a
    # governed fallback candidate.
    aggregator_fallback_p0_eligible = bool(
        not official_snapshot
        and len(system_ids) >= 2
        and len(set(family_by_system.values())) >= 2
        and int(artifact[2]) >= 2
        and bool(selection_sha)
        and bool(activation_sha)
    )
    native_forward_lineage[
        "aggregator_fallback_p0_eligible"
    ] = aggregator_fallback_p0_eligible
    native_forward_lineage[
        "republisher_fallback_p0_eligible"
    ] = aggregator_fallback_p0_eligible
    if not official_snapshot:
        native_forward_lineage[
            "aggregator_strict_oos_proof_by_system"
        ] = aggregator_strict_oos_proof_by_system
    native_forward_lineage_sha = content_sha256(
        native_forward_lineage
    )
    evidence_identity = {
        "forward_snapshot_content_sha256": forward_packet[
            "snapshot_content_sha256"
        ],
        "native_forward_lineage": native_forward_lineage,
        "native_forward_lineage_sha256": native_forward_lineage_sha,
        "cross_calibration_scope_run_cycle": cross_artifact_run_cycle,
        "cross_artifact_sha256": cross_artifact_sha,
        "selection_evidence_sha256": selection_sha,
        "cross_activation_sha256": activation_sha,
        "system_artifact_shas": system_artifact_shas,
        "system_activation_shas": system_activation_shas,
        "system_probabilities": system_probabilities,
        "calibrated_system_centers_c": calibrated_system_centers_c,
        "system_weights": system_weights,
        "provisional_uncalibrated_system_ids": sorted(
            provisional_uncalibrated_system_ids
        ),
        "family_weights": weight_receipt["family_weights"],
        "observed_ensemble_system_ids": observed_system_ids,
        "excluded_observed_system_ids": excluded_observed_system_ids,
        "excluded_observed_system_reason": (
            "NO_EXACT_OOS_ACTIVATION"
        ),
        "probabilities": probability,
    }
    evidence_sha = content_sha256(evidence_identity)
    bridge_sha = content_sha256({
        "bucket_intervals": dict(intervals_raw),
        "settlement_definition": settlement_definition,
        "settlement_timezone": timezone_name,
    })
    taf_compatibility_identity = {
        "schema": TAF_NATIVE_ENSEMBLE_LIVE_FINGERPRINT_SCHEMA,
        "probability_pathway": P0_PROBABILITY_PATHWAY,
        "station_id": station_id,
        "season_bin": season_bin,
        "lead_bucket": lead_bucket,
        # Both leads, for the same reason the lineage carries both: the
        # calendar label identifies the decision to an operator, while a TAF
        # artifact is scoped by the grouping its evidence was fitted in.  A
        # fingerprint naming only the calendar label could never bind an
        # artifact trained under the hour buckets.
        "training_lead_bucket": training_lead_bucket,
        "run_cycle": run_cycle,
        "run_cycle_scope": active_run_cycle_scope,
        "run_cycle_by_system": native_forward_lineage[
            "run_cycle_by_system"
        ],
        "run_init_utc_by_system": native_forward_lineage[
            "run_init_utc_by_system"
        ],
        "archive_query_at_utc_by_system": native_forward_lineage[
            "archive_query_at_utc_by_system"
        ],
        "availability_evidence": official_lineage[
            "availability_evidence"
        ],
        "source_archive_set_sha256": forward_packet.get(
            "source_archive_set_sha256"
        ),
        "settlement_definition": settlement_definition,
        "available_ensemble_system_set_hash": available_set_hash,
        "ensemble_system_ids": system_ids,
        "independence_family_ids": sorted(
            set(family_by_system.values())
        ),
        "system_family_ids": family_by_system,
        "resolved_system_weights": system_weights,
        "resolved_family_weights": weight_receipt["family_weights"],
        "family_weight_caps": family_caps,
        "cross_ensemble_artifact_sha256": cross_artifact_sha,
        "cross_calibration_scope_run_cycle": cross_artifact_run_cycle,
        "cross_selection_evidence_sha256": selection_sha,
        "cross_activation_sha256": activation_sha,
        "system_calibration_artifacts": {
            system_id: {
                "artifact_content_sha256": system_artifact_shas[
                    system_id
                ],
                "activation_content_sha256": system_activation_shas[
                    system_id
                ],
                "run_cycle": native_forward_lineage[
                    "run_cycle_by_system"
                ][system_id],
                "run_init_utc": native_forward_lineage[
                    "run_init_utc_by_system"
                ].get(system_id),
            }
            for system_id in system_ids
        },
        "native_forward_lineage": native_forward_lineage,
        "native_forward_lineage_sha256": native_forward_lineage_sha,
    }
    taf_compatibility = {
        **taf_compatibility_identity,
        "fingerprint_sha256": content_sha256(
            taf_compatibility_identity
        ),
    }
    posterior_sha = content_sha256({
        "station_id": station_id,
        "event_date": event_date,
        "decision_at_utc": decision_at_utc,
        "probabilities": probability,
        "native_probability_evidence_sha256": evidence_sha,
    })
    checkpoint_identity = {
        "station_id": station_id,
        "event_date": event_date,
        "decision_at_utc": decision_at_utc,
        "posterior_sha256": posterior_sha,
    }
    checkpoint_id = "p0-native-" + content_sha256(checkpoint_identity)[:32]
    root_future_curve = None
    if official_snapshot and all(
        isinstance(systems[system_id].get("family_temperature_curve"), Mapping)
        for system_id in system_ids
    ):
        family_curves: dict[str, dict[str, Any]] = {}
        for system_id in system_ids:
            raw_curve = dict(systems[system_id]["family_temperature_curve"])
            # Re-sealing accepts the same-day floor: only the collector knew
            # the lead it captured at, and a same-day run at an easternmost
            # station legitimately seals three 3-hourly points (RKPK, UTC+9,
            # 06Z rung).  Holding the reseal to the pre-day floor of four
            # rejected at compile what capture was allowed to keep.
            sealed_curve = build_native_family_temperature_curve(
                raw_curve, min_points=MIN_SAME_DAY_CURVE_POINTS
            )
            if (
                raw_curve.get("content_sha256")
                != sealed_curve["content_sha256"]
                or sealed_curve["ensemble_system_id"] != system_id
                or sealed_curve["independence_family_id"]
                != family_by_system[system_id]
                or sealed_curve["station_id"] != station_id
                or sealed_curve["event_date"] != event_date
                or sealed_curve["settlement_timezone"] != timezone_name
                or datetime.fromisoformat(
                    sealed_curve["available_at_utc"].replace("Z", "+00:00")
                ) > decision
            ):
                raise ContractError(
                    "native_ensemble_p0_family_temperature_curve_invalid"
                )
            family_curves[system_id] = sealed_curve
        common_times = set.intersection(*(
            {point["valid_time_utc"] for point in curve["points"]}
            for curve in family_curves.values()
        ))
        if len(common_times) >= 4:
            values_by_system = {
                system_id: {
                    point["valid_time_utc"]: float(point["temperature_c"])
                    for point in curve["points"]
                }
                for system_id, curve in family_curves.items()
            }
            curve_source_identity = {
                "family_curve_sha256_by_system": {
                    system_id: family_curves[system_id]["content_sha256"]
                    for system_id in system_ids
                },
                "system_weights": system_weights,
                "system_bias_c": system_bias_c,
            }
            root_future_curve = build_root_future_temperature_curve({
                "station_id": station_id,
                "event_date": event_date,
                "settlement_timezone": timezone_name,
                "root_checkpoint_id": checkpoint_id,
                "root_probability_sha256": posterior_sha,
                "available_at_utc": max(
                    curve["available_at_utc"]
                    for curve in family_curves.values()
                ),
                "source_snapshot_sha256": content_sha256(
                    curve_source_identity
                ),
                "independence_family_ids": sorted(set(
                    family_by_system.values()
                )),
                "points": [
                    {
                        "valid_time_utc": valid_time,
                        "temperature_c": sum(
                            system_weights[system_id]
                            * (
                                values_by_system[system_id][valid_time]
                                + system_bias_c[system_id]
                            )
                            for system_id in system_ids
                        ),
                    }
                    for valid_time in sorted(common_times)
                ],
            })
    lineage = {
        "probability_pathway": P0_PROBABILITY_PATHWAY,
        "p0_probability_pathway": P0_PROBABILITY_PATHWAY,
        "native_authority_scope": "PRODUCTION_NO_LIVE_ONLY",
        "settlement_timezone": timezone_name,
        "settlement_definition": settlement_definition,
        "season_bin": season_bin,
        "lead_bucket": lead_bucket,
        # The grouping the weights came from, recorded separately because it no
        # longer equals the calendar label above.  Without it a plan cannot be
        # traced back to the fit that priced it.
        "training_lead_bucket": training_lead_bucket,
        # Calibration scope, not capture.  The resident TAF lane matches an
        # artifact on this exact value (``root.lineage["run_cycle"]`` vs
        # ``artifact.run_cycle``), so it has to keep naming the cycle the fit
        # was scoped to -- no TAF artifact is ever fitted at MIXED.
        "run_cycle": run_cycle,
        # ...and the capture, recorded beside it, because they diverge the
        # moment a cycle rider is tolerated.  Materialization reads this pair
        # to decide whether the packet may claim one shared cycle.
        "native_forward_run_cycle": native_forward_run_cycle,
        "run_cycle_scope": active_run_cycle_scope,
        "run_cycle_by_system_json": _json(
            native_forward_lineage["run_cycle_by_system"]
        ),
        "run_cycle_by_system_sha256": content_sha256(
            native_forward_lineage["run_cycle_by_system"]
        ),
        "run_init_utc_by_system_json": _json(
            native_forward_lineage["run_init_utc_by_system"]
        ),
        "archive_query_at_utc_by_system_json": _json(
            native_forward_lineage["archive_query_at_utc_by_system"]
        ),
        "native_forward_availability_evidence_json": _json(
            official_lineage["availability_evidence"]
        ),
        "native_forward_source_archive_set_sha256": str(
            forward_packet.get("source_archive_set_sha256")
            or "NOT_PROVIDED_BY_FORWARD_TRANSPORT"
        ),
        "native_forward_lineage_sha256": native_forward_lineage_sha,
        "cross_calibration_scope_run_cycle": cross_artifact_run_cycle,
        "bucket_intervals_c_json": _json(dict(intervals_raw)),
        "ensemble_system_ids_json": _json(system_ids),
        "observed_ensemble_system_ids_json": _json(
            observed_system_ids
        ),
        "excluded_observed_system_ids_json": _json(
            excluded_observed_system_ids
        ),
        "excluded_observed_system_reason": (
            "NO_EXACT_OOS_ACTIVATION"
        ),
        "independence_family_ids_json": _json(sorted(
            set(family_by_system.values())
        )),
        "system_family_ids_json": _json(family_by_system),
        "system_weights_json": _json(system_weights),
        "provisional_uncalibrated_system_ids_json": _json(sorted(provisional_uncalibrated_system_ids)),
        "provisional_parameter_sources_json": _json(provisional_parameter_sources),
        "p0_membership_demotions_json": _json(p0_membership_demotions),
        "training_lead_bucket": training_lead_bucket,
        "provisional_lead_fallback_json": _json(
            {"packet_hour_bucket": hour_lead_bucket, "served_bucket": training_lead_bucket}
            if provisional_lead_fallback else None
        ),
        "same_run_lead_borrow_json": _json(same_run_lead_borrow),
        "synoptic_cooling_json": _json(synoptic_cooling_receipt),
        "neighbour_direction_json": _json(neighbour_direction_receipt),
        "hour_lead_bucket_by_system_json": _json(hour_bucket_by_system),
        "hour_bucket_split_riders_json": _json(hour_bucket_split_riders),
        "activation_cycle_riders_json": _json(activation_cycle_riders),
        "family_weight_caps_json": _json(family_caps),
        "family_weights_json": _json(
            weight_receipt["family_weights"]
        ),
        "system_artifact_shas_json": _json(system_artifact_shas),
        "system_activation_shas_json": _json(
            system_activation_shas
        ),
        "native_cross_ensemble_artifact_sha256":
            cross_artifact_sha,
        "native_selection_evidence_sha256": selection_sha,
        "native_cross_activation_sha256": activation_sha,
        "native_probability_evidence_sha256": evidence_sha,
        "native_forward_snapshot_sha256": str(
            forward_packet["snapshot_content_sha256"]
        ),
        "native_transport_provider": (
            (
                "OFFICIAL_NATIVE_MIXED_CYCLE_SNAPSHOT"
                if official_lineage["mixed"]
                else "OFFICIAL_NATIVE_SAME_CYCLE_SNAPSHOT"
            )
            if official_snapshot
            else "OPEN_METEO"
        ),
        "aggregator_fallback_p0_eligible": (
            "true" if aggregator_fallback_p0_eligible else "false"
        ),
        "republisher_fallback_p0_eligible": (
            "true" if aggregator_fallback_p0_eligible else "false"
        ),
        "transport_lineage_class": (
            "official_native"
            if official_snapshot
            else "aggregator_republisher"
        ),
        "native_arrival_index_eligible": (
            "true" if official_snapshot else "false"
        ),
        "native_formal_p0_eligible": "false",
        "model_envelope_lower_bucket": model_envelope_lower_bucket,
        "model_envelope_upper_bucket": model_envelope_upper_bucket,
        # The basket keys the envelope boundary on ordinals and treats a
        # missing pair as "this stage publishes no envelope" rather than as an
        # error, so emitting only the bucket names silently collapses the
        # candidate boundary onto the probability core and the dispersion
        # widening never runs.  Both keys are required together.
        "model_envelope_lower_ordinal": str(
            ordered.index(model_envelope_lower_bucket)
        ),
        "model_envelope_upper_ordinal": str(
            ordered.index(model_envelope_upper_bucket)
        ),
        "model_envelope_span_c": (
            f"{envelope_upper_c - envelope_lower_c:.12g}"
        ),
        "model_center_bucket": model_center_bucket,
        "model_center_c": f"{model_center_c:.12g}",
        # The TAF witness that took part in the envelope min/max above, or an
        # explicit statement that none was available.  Recording the capture's
        # content hash keeps the widened envelope reproducible from evidence.
        **(
            {
                "taf_envelope_witness_json": _json(taf_witness),
                "taf_envelope_extended": (
                    "lower"
                    if float(taf_witness["tx_c"]) < lower_center_c
                    else "upper"
                    if float(taf_witness["tx_c"]) > upper_center_c
                    else "none"
                ),
            }
            if taf_witness is not None
            else {
                # "Unavailable" conflated two unlike things.  Only 17 of the 45
                # stations issue a TAF carrying a TX temperature group at all;
                # for the other 28 there is no forecaster revision to wait for,
                # so P0-M is not a layer that is missing, it is a layer that
                # does not apply.  Reporting both as one state made a permanent
                # property of the airport look like a recurring capture fault.
                "taf_envelope_extended": (
                    "station_has_no_taf_tx"
                    if not _station_issues_taf_tx(
                        conn, station_id=station_id
                    )
                    else "unavailable"
                )
            }
        ),
        # What the consensus bridge did to this packet, or absent when the
        # serving artifact predates the bridge and the plain mixture ran.
        **({"consensus_bridge_json": json.dumps(
            consensus_bridge_receipt, sort_keys=True
        )} if consensus_bridge_receipt is not None else {}),
        # What the tail-reliability lift did, or absent when the serving
        # artifact predates it and its holdout scored the unlifted blend.
        **({"tail_reliability_json": json.dumps(
            tail_reliability_receipt, sort_keys=True
        )} if tail_reliability_receipt is not None else {}),
        **({"band_reliability_json": json.dumps(
            band_reliability_receipt, sort_keys=True
        )} if band_reliability_receipt is not None else {}),
        # What moved the distribution along the temperature axis, which until
        # now P0 computed and then dropped: the scalars' receipt reached no
        # lineage key here, so a P0 envelope could not be asked whether the
        # correction had run.  P1 has always recorded it.  Absent when nothing
        # corrected the distribution, so an uncorrected envelope keeps its
        # historical content hash.
        **({"center_width_json": json.dumps(
            center_width_receipt, sort_keys=True
        )} if center_width_receipt is not None else {}),
        "native_calibrated_system_centers_c_json": _json(
            calibrated_system_centers_c
        ),
        "peripheral_model_reliability_by_bucket_json": _json(
            peripheral_model_reliability_by_bucket
        ),
        "taf_nwp_live_compatibility_fingerprint_sha256": (
            taf_compatibility["fingerprint_sha256"]
        ),
        "taf_nwp_live_compatibility_json": _json(
            taf_compatibility
        ),
    }
    # TTL belongs to the evidence receipt, not to the moment a caller happens
    # to compile it.  Otherwise a nearly expired packet could be recompiled
    # repeatedly and made arbitrarily fresh without a new model arrival.
    #
    # And to the evidence the distribution is built on, not to every archive
    # the snapshot carried: a system excluded from the blend, or seated as a
    # zero-weight provisional rider, contributes no probability and so cannot
    # renew any.  The packet-level guard near the top still runs first -- the
    # packet clock is never earlier than this one -- and this is the one that
    # decides.
    seated_received = seated_evidence_received_at(
        official_lineage["archive_query_at_utc_by_system"],
        seated_system_ids=[
            system_id
            for system_id in system_ids
            if float(system_weights.get(system_id) or 0.0) > EPS
        ],
    ) if official_snapshot else None
    evidence_received = (
        min(seated_received, captured)
        if seated_received is not None
        else captured
    )
    if (
        fresh_seconds - (decision - evidence_received).total_seconds()
        < MIN_USABLE_FRESHNESS_SECONDS
    ):
        raise ContractError(
            "FAIL_CLOSED_NO_P0:native_ensemble_seated_evidence_stale"
        )
    fresh_until = (
        evidence_received + timedelta(seconds=fresh_seconds)
    ).isoformat().replace("+00:00", "Z")
    # The remaining-life guard above already makes this unreachable; it is
    # here because the two stamps are produced by different code with
    # different fractional precision, and the SSOT compares them as bytes
    # while everything else compares them as instants.  Whatever changes
    # upstream, the pair that leaves this function has to satisfy both.
    assert_mintable_freshness_window(
        decision_at_utc, fresh_until, name="native_ensemble_p0"
    )
    if root_future_curve is not None:
        lineage.update({
            "conditional_future_temperature_curve_json": _json(
                root_future_curve.to_dict()
            ),
            "conditional_future_temperature_curve_sha256": (
                root_future_curve.content_sha256
            ),
            "conditional_future_temperature_curve_root_stage": compiled_stage,
            "conditional_future_temperature_curve_root_checkpoint_id": (
                checkpoint_id
            ),
            "conditional_future_temperature_curve_root_probability_sha256": (
                posterior_sha
            ),
        })
    envelope = ProbabilityEnvelope.from_mapping({
        "stage": compiled_stage,
        "checkpoint_id": checkpoint_id,
        "station_id": station_id,
        "event_date": event_date,
        "settlement_timezone": timezone_name,
        "decision_at_utc": decision_at_utc,
        "fresh_until_utc": fresh_until,
        "bucket_ordinals": {
            bucket: index for index, bucket in enumerate(ordered)
        },
        "probabilities": probability,
        "posterior_version": POSTERIOR_VERSION,
        "posterior_sha256": posterior_sha,
        "bridge_sha256": bridge_sha,
        "lineage": lineage,
    })
    packet = {
        "stage": compiled_stage,
        "station_id": station_id,
        "event_date": event_date,
        "settlement_timezone": timezone_name,
        "decision_at_utc": decision_at_utc,
        "fresh_until_utc": fresh_until,
        "probability_pathway": P0_PROBABILITY_PATHWAY,
        "native_probability_envelope": envelope.to_dict(),
    }
    bucket_centers_c = {}
    for bucket, bounds in intervals_raw.items():
        lower, upper = bounds
        if lower is None:
            bucket_centers_c[str(bucket)] = float(upper) - 0.5
        elif upper is None:
            bucket_centers_c[str(bucket)] = float(lower) + 0.5
        else:
            bucket_centers_c[str(bucket)] = (
                float(lower) + float(upper)
            ) / 2.0
    trained_through_event_date = str(selection[5])
    if trained_through_event_date >= event_date:
        raise ContractError(
            "native_ensemble_forward_prediction_training_leakage"
        )
    forward_prediction_identity = {
        "cross_ensemble_artifact_id": artifact_id,
        "selection_evidence_sha256": selection_sha,
        "station_id": station_id,
        "event_date": event_date,
        "settlement_timezone": timezone_name,
        "season_bin": season_bin,
        "lead_bucket": lead_bucket,
        "run_cycle": run_cycle,
        "run_cycle_scope": active_run_cycle_scope,
        "run_cycle_by_system_json": _json(
            native_forward_lineage["run_cycle_by_system"]
        ),
        "run_cycle_by_system_sha256": content_sha256(
            native_forward_lineage["run_cycle_by_system"]
        ),
        "run_init_utc_by_system_json": _json(
            native_forward_lineage["run_init_utc_by_system"]
        ),
        "archive_query_at_utc_by_system_json": _json(
            native_forward_lineage["archive_query_at_utc_by_system"]
        ),
        "native_forward_lineage_sha256": native_forward_lineage_sha,
        "settlement_definition": settlement_definition,
        "trained_through_event_date": trained_through_event_date,
        "decision_at_utc": decision_at_utc,
        "ensemble_system_ids_json": _json(system_ids),
        "independence_family_ids_json": _json(sorted(
            set(family_by_system.values())
        )),
        "system_family_ids_json": _json(family_by_system),
        "system_weights_json": _json(system_weights),
        "provisional_uncalibrated_system_ids_json": _json(sorted(provisional_uncalibrated_system_ids)),
        "provisional_parameter_sources_json": _json(provisional_parameter_sources),
        "p0_membership_demotions_json": _json(p0_membership_demotions),
        "training_lead_bucket": training_lead_bucket,
        "provisional_lead_fallback_json": _json(
            {"packet_hour_bucket": hour_lead_bucket, "served_bucket": training_lead_bucket}
            if provisional_lead_fallback else None
        ),
        "same_run_lead_borrow_json": _json(same_run_lead_borrow),
        "synoptic_cooling_json": _json(synoptic_cooling_receipt),
        "neighbour_direction_json": _json(neighbour_direction_receipt),
        "hour_lead_bucket_by_system_json": _json(hour_bucket_by_system),
        "hour_bucket_split_riders_json": _json(hour_bucket_split_riders),
        "activation_cycle_riders_json": _json(activation_cycle_riders),
        "family_weight_caps_json": _json(family_caps),
        "system_member_set_shas_json": _json({
            system_id: str(
                systems[system_id].get("member_set_sha256")
                or content_sha256([
                    {
                        "member_id": str(member["member_id"]),
                        "member_role": str(member["member_role"]),
                        "raw_tmax_c": float(member["raw_tmax_c"]),
                    }
                    for member in systems[system_id]["members"]
                ])
            )
            for system_id in system_ids
        }),
        "probabilities_json": _json(probability),
        "bucket_centers_c_json": _json(bucket_centers_c),
        "prediction_oos": 1,
        "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
    }
    forward_prediction_sha = content_sha256(
        forward_prediction_identity
    )
    forward_prediction = {
        "prediction_id": (
            "native-forward-prediction-"
            + forward_prediction_sha[:24]
        ),
        **forward_prediction_identity,
        "content_sha256": forward_prediction_sha,
    }
    return {
        "schema": SCHEMA,
        "packet": packet,
        "packet_content_sha256": content_sha256(packet),
        "source_class": "calibrated_native_cross_ensemble",
        "activation_id": activation_id,
        "cross_ensemble_artifact_id": artifact_id,
        "selection_evidence_sha256": selection_sha,
        "forward_prediction": forward_prediction,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "live_submit_authorized": False,
    }


def persist_native_ensemble_forward_prediction(
    conn: sqlite3.Connection,
    compiled: Mapping[str, Any],
) -> dict[str, Any]:
    """Append the first strict-forward P0 fact for one artifact/day."""

    if native_ensemble_foundation_schema_status(conn)["ok"] is not True:
        raise ContractError(
            "native_ensemble_forward_prediction_schema_missing"
        )
    raw = compiled.get("forward_prediction")
    if not isinstance(raw, Mapping):
        raise ContractError(
            "native_ensemble_forward_prediction_missing"
        )
    row = dict(raw)
    identity = {
        key: value
        for key, value in row.items()
        if key not in {"prediction_id", "content_sha256"}
    }
    expected_sha = content_sha256(identity)
    if (
        row.get("content_sha256") != expected_sha
        or row.get("prediction_id")
        != "native-forward-prediction-" + expected_sha[:24]
        or row.get("prediction_oos") != 1
        or row.get("authority_scope") != "PRODUCTION_NO_LIVE_ONLY"
    ):
        raise ContractError(
            "native_ensemble_forward_prediction_invalid"
        )
    existing = conn.execute(
        "SELECT prediction_id,content_sha256,decision_at_utc "
        "FROM calibration_cross_native_ensemble_oos_prediction "
        "WHERE cross_ensemble_artifact_id=? AND station_id=? "
        "AND event_date=? "
        "ORDER BY decision_at_utc,prediction_id LIMIT 1",
        (
            row["cross_ensemble_artifact_id"],
            row["station_id"],
            row["event_date"],
        ),
    ).fetchone()
    if existing is not None:
        return {
            "state": "EXISTING_FIRST_FORWARD_PREDICTION",
            "prediction_id": str(existing[0]),
            "content_sha256": str(existing[1]),
            "decision_at_utc": str(existing[2]),
            "wrote_count": 0,
        }
    table_columns = {
        str(info[1])
        for info in conn.execute(
            "PRAGMA table_info(calibration_cross_native_ensemble_oos_prediction)"
        ).fetchall()
    }
    if not table_columns:
        raise ContractError(
            "native_ensemble_forward_prediction_table_missing"
        )
    columns = tuple(
        column for column in row if column in table_columns
    )
    conn.execute(
        "INSERT INTO calibration_cross_native_ensemble_oos_prediction("
        + ",".join(columns)
        + ") VALUES("
        + ",".join("?" for _ in columns)
        + ")",
        tuple(row[column] for column in columns),
    )
    return {
        "state": "COMMITTED",
        "prediction_id": str(row["prediction_id"]),
        "content_sha256": expected_sha,
        "decision_at_utc": str(row["decision_at_utc"]),
        "wrote_count": 1,
    }


__all__ = [
    "DEFAULT_FRESH_SECONDS",
    "POSTERIOR_VERSION",
    "SCHEMA",
    "compile_native_ensemble_p0",
    "persist_native_ensemble_forward_prediction",
]
