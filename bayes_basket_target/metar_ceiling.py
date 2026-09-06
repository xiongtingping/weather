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
from typing import Any, Mapping
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ARTIFACT = (
    ROOT / "data" / "runtime" / "bayes_basket_target_nwp"
    / "metar_ceiling_artifact.json"
)
SCHEMA = "bayes_basket_target_metar_ceiling_v1"

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
CEILING_MAX_HOURS_LEFT = float(
    os.environ.get("PM_METAR_CEILING_MAX_HOURS_LEFT", "14.0") or 14.0
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
        if payload is not None and payload.get("schema") != SCHEMA:
            payload = None
        _CACHE[key] = payload
    return _CACHE[key]


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


def probability_still_rising(
    artifact: Mapping[str, Any], remaining_hours: float
) -> float | None:
    """P(the day ends above the bucket already holding its high)."""

    bins = artifact.get("bins") or {}
    for entry in bins.values():
        low = float(entry.get("hours_from", 0.0))
        high = float(entry.get("hours_to", 0.0))
        if low <= remaining_hours < high:
            # A bin that fell back to the pooled rate is describing every hour
            # at once, which is the one thing this layer must not do.
            if entry.get("backed_off"):
                return None
            return float(entry["p_still_rose"])
    return None


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
) -> tuple[dict[str, float], dict[str, str]]:
    """Rescale the above/below split to the fitted rate; keep both shapes.

    Returns the posterior unchanged, with a lineage note saying why, whenever
    the layer declines to speak -- no artifact, a backed-off bin, too much of
    the day still ahead, or a split already at 0 or 1 that no rescaling can
    move without inventing support.
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
    target = probability_still_rising(payload, remaining)
    if target is None:
        note["metar_ceiling_skip_reason"] = "BIN_BACKED_OFF"
        return base, note
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
        if payload is not None and payload.get("schema") != SCHEMA + "_replay":
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
