"""The cooling-side ceiling: what is left of the day above the measured high.

The hard bound cuts only from below.  An observation says the day has reached
X and every bucket under X dies, and the posterior is renormalised -- which
scales the mass above X *up*, because renormalisation does not know which side
it is lifting.  Nothing ever cuts from above, so at local midnight a posterior
can still hold most of its mass on buckets that needed an afternoon that is
already over.  Measured on 25890 beliefs across 45 stations: inside six hours
of local settlement the day's max ended above the bucket already holding it
0.0% of the time, while the posterior claimed 55-60%.

What closes it is not a stronger trajectory likelihood but a different fact --
the clock.  The trajectory artifacts cannot supply it: their folds exist only
where the running high advanced, and the thing to be learned here is that it
usually does not.

This layer corrects exactly one number, the split between mass above the
running high's bucket and mass at or below it, and leaves the shape inside
each side alone.  That split is the measured error and it is zero sum: what is
wrongly left above was taken from the bucket that settles.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ARTIFACT = (
    ROOT / "data" / "runtime" / "bayes_basket_target_nwp"
    / "metar_ceiling_artifact.json"
)
# v2 (operator ruling 2026-09-06): the rate is fitted on two axes -- hours
# left, and how far the current temperature sits below the day's running high
# -- because a day that has fallen two degrees off its high at noon is not the
# day the hour-only rate describes.  A v1 table (hour axis only) still loads
# and serves the hour marginal, so a serving side and a fitted file may move
# on different days without a gap.
SCHEMA = "bayes_basket_target_metar_ceiling_v2"
LEGACY_SCHEMAS = frozenset({"bayes_basket_target_metar_ceiling_v1"})
CEILING_GAP_DIMENSION_VERSION = "hours_left_x_high_minus_current_v1"
# Cell edges on the gap axis, in degrees C below the running high: at the
# high, one degree under, two under, three or more.  METAR temperatures are
# whole degrees, so the half-degree edges split the integers cleanly.
GAP_EDGES_C = (0.5, 1.5, 2.5)

# Off until switched on deliberately: with it off this module cannot alter a
# single posterior, so every content hash downstream is byte-identical.
CEILING_ENABLED = (
    os.environ.get("PM_METAR_CEILING", "0").strip()
    not in {"0", "false", "FALSE", "no", ""}
)
# How early in the day the ceiling may speak, in hours left before local
# midnight.  It was 10 (about 14:00 local): beyond that the day still had an
# afternoon in it, and the 2026-08-23 measurement found the correction there
# cost hit rate while inside it bought 10 points of argmax accuracy.
#
# Operator ruling 2026-09-06: widened to 14, which is 10:00 local.  ZGSZ
# 2026-09-06 -- 27°C at midnight, heavy showers from 05:00, 25°C at noon --
# served 80% on 30-32°C all morning with the receipt reading TOO_EARLY, and
# nothing else in P1 reads the observations before the peak (the soft
# likelihood is suspended by the 2026-09-02 ruling, the hard bound only cuts
# from below).  The fitted bins the window now reaches are populated
# (10-13h: p_still_rose 0.668 on 5,663 samples; 13-16h: 0.859 on 3,402), so
# nothing is extrapolated; what the earlier hours cost or buy on the ledger
# is the measurement this ruling asks for, and PAPER_LEDGER_METAR_CEILING
# reads this same constant so both sides move together.
# Reverted to 10 by measurement (2026-09-07).  On the 4,232 committed P1
# prefixes over 2026-08-24..09-04 that fall in the 10-14h band this
# widening newly admits -- production's own apply_ceiling, its own as-of
# replay curve, only the window constant lifted -- the ceiling costs
# 0.136 nat of log loss, makes 72.3% of prefixes worse against 27.7%
# better, and buys no tail protection: the share of prefixes where truth
# is given under 5% stays at 3.4%, with 36 catastrophic misses rescued
# and 38 created.  The ZGSZ 2026-09-06 case the widening was ruled on is
# real, and this says the fleet pays more for it than it is worth.
CEILING_MAX_HOURS_LEFT = float(
    os.environ.get("PM_METAR_CEILING_MAX_HOURS_LEFT", "10.0") or 10.0
)

DEFAULT_REPLAY_TABLE = (
    ROOT / "data" / "runtime" / "bayes_basket_target_nwp"
    / "metar_ceiling_replay_table.json"
)

_CACHE: dict[str, Any] = {}


def load_artifact(path: Path | None = None) -> dict[str, Any] | None:
    """The fitted curve, or None when there is nothing fitted to read."""

    target = Path(path or os.environ.get("PM_METAR_CEILING_ARTIFACT")
                  or DEFAULT_ARTIFACT)
    key = str(target)
    if key not in _CACHE:
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            payload = None
        if payload is not None and payload.get("schema") not in (
            {SCHEMA} | LEGACY_SCHEMAS
        ):
            payload = None
        _CACHE[key] = payload
    return _CACHE[key]


def gap_bin(gap_c: float) -> int:
    """Index of the gap cell holding ``gap_c`` degrees below the high."""

    for index, edge in enumerate(GAP_EDGES_C):
        if float(gap_c) < float(edge):
            return index
    return len(GAP_EDGES_C)


def gap_cell_bounds(index: int) -> tuple[float, float]:
    edges = (float("-inf"),) + tuple(float(e) for e in GAP_EDGES_C) + (1e9,)
    return edges[index], edges[index + 1]


def latest_temperature_c(
    points: Iterable[tuple[Any, Any]], as_of_utc: str | None = None
) -> float | None:
    """The temperature of the newest observation at or before ``as_of_utc``.

    ``points`` are (observed_at_utc, temperature_c) pairs from the formal
    report series -- the caller decides which lanes count, the same way the
    running high is taken from formal reports only.
    """

    limit = None
    if as_of_utc:
        try:
            limit = datetime.fromisoformat(
                str(as_of_utc).replace("Z", "+00:00")
            )
        except ValueError:
            limit = None
    best: tuple[datetime, float] | None = None
    for stamp, temperature in points:
        try:
            when = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
            value = float(temperature)
        except (TypeError, ValueError):
            continue
        if limit is not None and when > limit:
            continue
        if best is None or when >= best[0]:
            best = (when, value)
    return None if best is None else best[1]


def current_gap_c(
    running_high_c: Any, latest_temperature: Any
) -> float | None:
    """Degrees the current temperature sits below the day's running high."""

    try:
        high = float(running_high_c)
        latest = float(latest_temperature)
    except (TypeError, ValueError):
        return None
    return max(0.0, high - latest)


def hours_left(event_date: str, settlement_timezone: str, as_of_utc: str) -> float | None:
    """Hours from this instant to the local midnight the day settles at."""

    try:
        zone = ZoneInfo(str(settlement_timezone))
        close = datetime.fromisoformat(str(event_date)).replace(
            tzinfo=zone) + timedelta(days=1)
        now = datetime.fromisoformat(str(as_of_utc).replace("Z", "+00:00"))
    except (ValueError, KeyError, TypeError):
        return None
    return (close - now).total_seconds() / 3600.0


def resolve_rate(
    artifact: Mapping[str, Any],
    remaining_hours: float,
    gap_c: float | None = None,
) -> dict[str, Any] | None:
    """The fitted rate for this hour and gap, and where it came from.

    The hour bin is found first; a bin that fell back to the pooled rate is
    describing every hour at once, which is the one thing this layer must not
    do, so it yields None.  Inside the bin the gap cell serves when the fit
    holds one for this gap; a thin cell, a table without cells (v1) or a
    caller without a gap all fall back to the bin's own hour marginal --
    never to the pooled rate.
    """

    bins = artifact.get("bins") or {}
    for entry in bins.values():
        low = float(entry.get("hours_from", 0.0))
        high = float(entry.get("hours_to", 0.0))
        if not (low <= remaining_hours < high):
            continue
        if entry.get("backed_off"):
            return None
        marginal = float(entry["p_still_rose"])
        cells = entry.get("by_gap") or {}
        if gap_c is None:
            return {"rate": marginal, "source": "hours_marginal"}
        if not cells:
            return {"rate": marginal, "source": "hours_marginal_no_gap_cells"}
        gap = float(gap_c)
        for cell in cells.values():
            cell_low = float(cell.get("gap_from", float("-inf")))
            cell_high = float(cell.get("gap_to", 1e9))
            if cell_low <= gap < cell_high:
                if cell.get("backed_off"):
                    return {
                        "rate": marginal,
                        "source": "hours_marginal_gap_cell_backed_off",
                        "gap_from": cell_low, "gap_to": cell_high,
                    }
                return {
                    "rate": float(cell["p_still_rose"]),
                    "source": "hours_x_gap",
                    "gap_from": cell_low, "gap_to": cell_high,
                }
        return {"rate": marginal, "source": "hours_marginal_gap_cell_missing"}
    return None


def probability_still_rising(
    artifact: Mapping[str, Any],
    remaining_hours: float,
    gap_c: float | None = None,
) -> float | None:
    """P(the day ends above the bucket already holding its high)."""

    resolved = resolve_rate(artifact, remaining_hours, gap_c)
    return None if resolved is None else float(resolved["rate"])


def apply_ceiling(
    posterior: Mapping[str, float],
    *,
    ordinals: Mapping[str, int],
    running_high_bucket: str,
    event_date: str,
    settlement_timezone: str,
    as_of_utc: str,
    artifact: Mapping[str, Any] | None = None,
    enabled: bool | None = None,
    current_gap_c: float | None = None,
) -> tuple[dict[str, float], dict[str, str]]:
    """Rescale the above/below split to the fitted rate; keep both shapes.

    Returns the posterior unchanged, with a lineage note saying why, whenever
    the layer declines to speak -- no artifact, a backed-off bin, too much of
    the day still ahead, or a split already at 0 or 1 that no rescaling can
    move without inventing support.

    ``current_gap_c`` is how far the newest formal report sits below the
    running high (v2, 2026-09-06).  With it the fitted cell for this hour and
    gap serves; without it, or on a v1 table, the hour marginal does.
    """

    base = {str(k): float(v) for k, v in posterior.items()}
    note = {"metar_ceiling_applied": "false"}
    # A replay owns its own switch: it is measuring what production runs, and
    # production's env var says whether the live engine has it on today, which
    # is a different question from whether this replay should model it.
    if not (CEILING_ENABLED if enabled is None else enabled):
        note["metar_ceiling_skip_reason"] = "DISABLED"
        return base, note
    payload = artifact if artifact is not None else load_artifact()
    if not payload:
        note["metar_ceiling_skip_reason"] = "NO_ARTIFACT"
        return base, note
    if str(event_date) <= str(payload.get("trained_through") or ""):
        # Its own day is inside the fit; using it here would be reading the
        # answer off the training set.
        note["metar_ceiling_skip_reason"] = "EVENT_DATE_INSIDE_TRAINING"
        return base, note
    remaining = hours_left(event_date, settlement_timezone, as_of_utc)
    if remaining is None or remaining < 0:
        note["metar_ceiling_skip_reason"] = "CLOCK_UNAVAILABLE"
        return base, note
    if remaining >= CEILING_MAX_HOURS_LEFT:
        note["metar_ceiling_skip_reason"] = "TOO_EARLY"
        return base, note
    if running_high_bucket not in ordinals:
        note["metar_ceiling_skip_reason"] = "RUNNING_HIGH_BUCKET_UNKNOWN"
        return base, note
    resolved = resolve_rate(payload, remaining, current_gap_c)
    if resolved is None:
        note["metar_ceiling_skip_reason"] = "BIN_BACKED_OFF"
        return base, note
    target = float(resolved["rate"])
    floor_ordinal = int(ordinals[running_high_bucket])
    above = sum(v for k, v in base.items()
                if int(ordinals.get(k, floor_ordinal)) > floor_ordinal)
    if above <= 0.0 or above >= 1.0:
        note["metar_ceiling_skip_reason"] = "SPLIT_DEGENERATE"
        return base, note
    # Never drive a side to exactly zero: the ceiling is a rate, not a proof
    # about this station-day, and a zeroed bucket cannot be revived by a later
    # observation that does warm.
    target = min(max(target, 1e-6), 1.0 - 1e-6)
    up = target / above
    down = (1.0 - target) / (1.0 - above)
    scaled = {
        k: v * (up if int(ordinals.get(k, floor_ordinal)) > floor_ordinal else down)
        for k, v in base.items()
    }
    total = sum(scaled.values())
    if total <= 0.0:
        note["metar_ceiling_skip_reason"] = "ZERO_MASS"
        return base, note
    note = {
        "metar_ceiling_applied": "true",
        "metar_ceiling_hours_left": f"{remaining:.4f}",
        "metar_ceiling_mass_above_before": f"{above:.6f}",
        "metar_ceiling_mass_above_after": f"{target:.6f}",
        "metar_ceiling_artifact_trained_through": str(
            payload.get("trained_through") or ""),
        "metar_ceiling_rate_source": str(resolved["source"]),
        "metar_ceiling_gap_c": (
            "NONE" if current_gap_c is None else f"{float(current_gap_c):.4f}"
        ),
        "metar_ceiling_gap_dimension": CEILING_GAP_DIMENSION_VERSION,
    }
    return {k: v / total for k, v in scaled.items()}, note


def load_replay_table(path: Path | None = None) -> dict[str, Any] | None:
    """Curves keyed by event date, each fitted only on strictly earlier days.

    The single-cutoff artifact can only serve dates after its cutoff, which is
    right for production and useless for a replay: every historical day sits
    inside it.  This table gives each day the curve it could have had on the
    morning of that day and nothing later, so a replay measures a rule that
    could actually have been run.
    """

    target = Path(path or os.environ.get("PM_METAR_CEILING_REPLAY_TABLE")
                  or DEFAULT_REPLAY_TABLE)
    key = "replay::" + str(target)
    if key not in _CACHE:
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            payload = None
        if payload is not None and payload.get("schema") not in (
            {SCHEMA + "_replay"} | {legacy + "_replay" for legacy in LEGACY_SCHEMAS}
        ):
            payload = None
        _CACHE[key] = payload
    return _CACHE[key]


def replay_curve(event_date: str, table: Mapping[str, Any] | None = None):
    """The curve this event date is allowed to use, or None if too early."""

    payload = table if table is not None else load_replay_table()
    if not payload:
        return None
    entry = (payload.get("dates") or {}).get(str(event_date))
    if not entry:
        return None
    # Reuse the serving shape so both paths run the same arithmetic; the date
    # guard is already satisfied by construction here, so it is set aside.
    return {"schema": SCHEMA, "bins": entry.get("bins") or {},
            "trained_through": ""}
