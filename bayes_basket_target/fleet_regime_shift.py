"""Next-day fleet regime shift: react to yesterday's settled residual.

The layer this sits beside -- the per-stage centre/width scalars -- fits two
free parameters and therefore has to earn its way past an out-of-sample gate
on a held-out forward window.  That gate costs days, and it is the right
price for a fitted correction.  It is the wrong price for the question this
layer answers, which is not "what correction does the history support" but
"the fleet settled half a degree above what we served yesterday, and the day
before, and the day before that -- are we still serving the same number".

So there is nothing fitted here.  ``LOW_LAMBDA`` and the caps are constants
chosen conservatively and written down; the signal is a direct measurement
of the residual the fleet actually realised on the most recent settled day.
With no free parameter there is nothing to overfit, which is exactly why
this one is allowed to move on one day of evidence when the fitted layer
beside it is not.

Measured 2026-08-27 on the 12 served days the paper ledger covers
(2026-08-15..26), fleet daily mean gap of settled truth against the served
P0 posterior mean:

  * the standing cold bias is +0.45 buckets, but it explains only 33% of the
    squared gap -- the other 67% moves, and moves predictably: lag-1
    autocorrelation is +0.50 *after* the mean is removed, so this is a
    regime signal and not a constant wearing a regime's clothes.
  * sign persists 10 of 11 day-pairs.
  * closed-loop over those days, mean |gap| falls 0.70 -> 0.54 buckets.  One
    day is made worse -- 2026-08-17, where the regime flipped overnight from
    cold to hot and yesterday's signal pointed the wrong way -- and the cap
    is what bounds that day's cost to 0.45 buckets.  A layer that reacts to
    yesterday will always pay on the day yesterday was wrong; the cap is the
    premium, and it is set so the premium is never larger than the loss it
    is insuring against.

Why the escalation ladder (user ruling, 2026-08-27).  A single day of
evidence buys the low tier.  Three consecutive same-sign days past the arm
threshold buy the high tier.  On the measured window that rule declined to
escalate before 2026-08-17 -- there were only two cold days behind it -- so
the flip was met at low strength, and escalated on 2026-08-22 only after a
genuine run had established itself.  The ladder is not a tuning knob; it is
the difference between reacting to a day and reacting to a regime.

Self-limiting by construction.  The residual is measured on the *served*
distribution, which already carries whatever this layer shifted it by.  When
the slow fitted layers finally absorb a regime, the residual they leave
behind goes to zero, and so does this offset -- with no coordination between
the layers and no way for the same degree to be counted twice.  The measured
window shows it happening: 2026-08-25 opens with the previous day corrected
to +0.05, and the offset it produces is +0.02.
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
from typing import Any

from .center_width_calibration import MAX_ABS_REGIME_SHIFT_C
from .contracts import ContractError, content_sha256

FLEET_REGIME_STAGES = ("P0", "P0-M", "P1")
# Which stages actually serve the shift.  Every stage is measured and
# journalled -- the series is worth having for all three, and the alert has to
# cover a stage whether or not it is corrected -- but a stage only serves the
# correction once its own residual is shown to persist.
#
# Measured 2026-08-27, closed-loop over 2026-08-17..26 on the real series:
#
#     P0    mean |residual| 0.312C -> 0.236C   (-24%)
#     P0-M                  0.418C -> 0.318C   (-24%)
#     P1                    0.142C -> 0.152C   (+7%, worse on 7 of 9 days)
#
# P1 is excluded on that measurement, and the reason is structural rather
# than a thin window.  By the time P1 decides, the observation hard bound and
# the day's own reported high have already removed most of what a forecast
# got wrong: its residual is a third of P0's to begin with, and what is left
# does not persist -- it alternates sign almost every day (-0.43, -0.06,
# +0.19, +0.01, +0.10, -0.14, +0.05, -0.26, -0.03).  Reacting to yesterday on
# a series that alternates is not a slow correction, it is an anticorrelated
# one.  Nothing here needs changing for P1 to join: the day its measured
# series persists, this tuple is the only edit.
FLEET_REGIME_SERVED_STAGES = ("P0", "P0-M")
FLEET_REGIME_METHOD_TAG = "fleet_regime_shift_v1"

# One day of evidence.  Deliberately below half: yesterday is real evidence
# about today, but it is one day, and a correction that believed it fully
# would hand the whole book to a single day's weather.
LOW_LAMBDA = 0.4
LOW_CAP_C = 0.5
# A run.  Three same-sign days past the arm threshold is a regime, not a day,
# and is met with the strength a regime deserves.
HIGH_LAMBDA = 0.8
HIGH_CAP_C = 1.0
ESCALATION_RUN_DAYS = 3
# The caps are what actually bounds the served shift; the entitlement bound
# the composing layer checks is the backstop behind them.  Raising a cap past
# the entitlement would turn a stood-down day into a raised ValueError deep in
# the serving path, so the two are tied together here instead.
if HIGH_CAP_C > MAX_ABS_REGIME_SHIFT_C or LOW_CAP_C > MAX_ABS_REGIME_SHIFT_C:
    raise ImportError("fleet_regime_cap_exceeds_entitlement")
# Below this a day is noise and does not count toward a run.  It still feeds
# the offset -- a small residual produces a small shift -- it just cannot
# help promote the tier.
ARM_THRESHOLD_C = 0.30

# A day's fleet mean needs this many stations behind it to be read as a fleet
# fact rather than a handful of stations having weather.  Measured over
# 2026-07-10..08-26 the fleet settles 31-45 stations a day and this floor
# never once bound; it is a guard against a collapsed truth lane, not a
# waiting period.
MIN_STATION_COUNT = 8
# How stale the newest visible settled day may be before the layer stands
# down.  Past this the truth lane has stopped and yesterday's number is no
# longer a statement about now.
MAX_SIGNAL_AGE_DAYS = 3

# Alert thresholds (user ruling, 2026-08-27).  These drive the dashboard
# light and the status field; they do not gate the correction.
ALERT_YELLOW_C = 0.30
ALERT_RED_C = 0.50

_DAILY_DDL = (
    "CREATE TABLE IF NOT EXISTS calibration_fleet_regime_daily ("
    " stage TEXT NOT NULL,"
    " event_date TEXT NOT NULL,"
    " residual_c_mean REAL NOT NULL,"
    " station_count INTEGER NOT NULL,"
    " cell_count INTEGER NOT NULL,"
    " truth_visible_at_utc TEXT NOT NULL,"
    " computed_at_utc TEXT NOT NULL,"
    " content_sha256 TEXT NOT NULL,"
    " PRIMARY KEY(stage, event_date))"
)
_DAILY_INDEX = (
    "CREATE INDEX IF NOT EXISTS calibration_fleet_regime_daily_asof_idx"
    " ON calibration_fleet_regime_daily(stage, truth_visible_at_utc)"
)


def ensure_fleet_regime_schema(conn: sqlite3.Connection) -> None:
    conn.execute(_DAILY_DDL)
    conn.execute(_DAILY_INDEX)


def _check_stage(stage: str) -> str:
    value = str(stage)
    if value not in FLEET_REGIME_STAGES:
        raise ContractError("fleet_regime_stage_invalid")
    return value


def _utc(value: str) -> datetime:
    return datetime.fromisoformat(
        str(value).replace("Z", "+00:00")
    ).astimezone(timezone.utc)


def cell_residual_c(
    cell: Mapping[str, Any], actual_tmax_c: float
) -> float | None:
    """Settled truth minus the served distribution's own mean, in Celsius.

    Celsius, not buckets, and on purpose: the shift this feeds is applied in
    Celsius against bucket centres that are already Celsius, so measuring in
    the same unit removes the per-station bucket-step conversion entirely --
    the step that has already produced one unit-mixup defect on the Fahrenheit
    stations.  A degree measured here is the same degree that gets shifted.
    """

    probabilities = cell.get("probabilities") or {}
    keys = list(cell.get("ordered_keys") or ())
    centers = list(cell.get("key_centers_c") or ())
    if not probabilities or len(keys) != len(centers) or len(keys) < 2:
        return None
    index = {str(key): position for position, key in enumerate(keys)}
    total = 0.0
    weighted = 0.0
    for key, value in probabilities.items():
        position = index.get(str(key))
        if position is None:
            return None
        weight = float(value)
        if weight < 0.0 or not math.isfinite(weight):
            return None
        total += weight
        weighted += float(centers[position]) * weight
    if total <= 0.0 or not math.isfinite(total):
        return None
    mean_c = weighted / total
    residual = float(actual_tmax_c) - mean_c
    if not math.isfinite(residual):
        return None
    return residual


def summarise_day(
    cells: Sequence[Mapping[str, Any]],
    truth: Mapping[tuple[str, str], float],
) -> dict[str, Any] | None:
    """One day's fleet residual: mean over stations, station first.

    A station is averaged over its own cells before the fleet is averaged
    over stations, so a station that re-decided all day does not get to
    outvote one that decided once.
    """

    by_station: dict[str, list[float]] = {}
    for cell in cells:
        station = str(cell.get("station_id") or "")
        day = str(cell.get("event_date") or "")
        actual = truth.get((station, day))
        if actual is None:
            continue
        residual = cell_residual_c(cell, actual)
        if residual is None:
            continue
        by_station.setdefault(station, []).append(residual)
    if not by_station:
        return None
    per_station = [
        sum(values) / len(values) for values in by_station.values()
    ]
    return {
        "residual_c_mean": sum(per_station) / len(per_station),
        "station_count": len(per_station),
        "cell_count": sum(len(values) for values in by_station.values()),
    }


def upsert_daily_residual(
    conn: sqlite3.Connection,
    *,
    stage: str,
    event_date: str,
    residual_c_mean: float,
    station_count: int,
    cell_count: int,
    truth_visible_at_utc: str,
    now_utc: str,
) -> None:
    """Record one day's fleet residual.

    Overwrites its own day: truth is revised, and a revised day must restate
    its residual rather than keep the one the superseded reading produced.
    """

    stage_id = _check_stage(stage)
    payload = {
        "schema": "fleet_regime_daily_v1",
        "stage": stage_id,
        "event_date": str(event_date),
        "residual_c_mean": round(float(residual_c_mean), 6),
        "station_count": int(station_count),
    }
    conn.execute(
        "INSERT INTO calibration_fleet_regime_daily("
        " stage, event_date, residual_c_mean, station_count, cell_count,"
        " truth_visible_at_utc, computed_at_utc, content_sha256)"
        " VALUES(?,?,?,?,?,?,?,?)"
        " ON CONFLICT(stage, event_date) DO UPDATE SET"
        "  residual_c_mean=excluded.residual_c_mean,"
        "  station_count=excluded.station_count,"
        "  cell_count=excluded.cell_count,"
        "  truth_visible_at_utc=excluded.truth_visible_at_utc,"
        "  computed_at_utc=excluded.computed_at_utc,"
        "  content_sha256=excluded.content_sha256",
        (
            stage_id, str(event_date), float(residual_c_mean),
            int(station_count), int(cell_count),
            str(truth_visible_at_utc), str(now_utc),
            content_sha256(payload),
        ),
    )


def visible_daily_series(
    conn: sqlite3.Connection,
    *,
    stage: str,
    as_of_utc: str,
    limit_days: int = 10,
) -> list[dict[str, Any]]:
    """The days whose truth was already visible at ``as_of_utc``, newest last.

    The as-of cut is on when the truth became visible, never on the event
    date.  A day that settled yesterday but whose truth only materialised an
    hour ago was not available to a decision taken this morning, and letting
    it vote would make every replay of that decision disagree with what
    production actually served.
    """

    stage_id = _check_stage(stage)
    try:
        rows = conn.execute(
            "SELECT event_date, residual_c_mean, station_count, cell_count,"
            " truth_visible_at_utc"
            " FROM calibration_fleet_regime_daily"
            " WHERE stage=? AND truth_visible_at_utc<=?"
            " ORDER BY event_date DESC LIMIT ?",
            (stage_id, str(as_of_utc), int(limit_days)),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [
        {
            "event_date": str(row[0]),
            "residual_c_mean": float(row[1]),
            "station_count": int(row[2]),
            "cell_count": int(row[3]),
            "truth_visible_at_utc": str(row[4]),
        }
        for row in reversed(rows)
    ]


def derive_offset(
    series: Sequence[Mapping[str, Any]],
    *,
    event_date: str,
) -> dict[str, Any]:
    """Turn a visible daily series into the shift to serve, plus its receipt.

    Policy is derived on read rather than stored, so changing a constant here
    re-reads history instead of rewriting it, and no activation pointer has to
    be rebound to make a new constant take effect.
    """

    receipt: dict[str, Any] = {
        "schema": "fleet_regime_receipt_v1",
        "method_tag": FLEET_REGIME_METHOD_TAG,
        "state": "IDENTITY",
        "regime_delta_c": 0.0,
        "tier": "NONE",
        "signal_c": None,
        "signal_event_date": None,
        "run_length": 0,
        "station_count": 0,
        "alert": "GREEN",
    }
    if not series:
        receipt["reason"] = "no_visible_settled_day"
        return receipt
    newest = series[-1]
    signal = float(newest["residual_c_mean"])
    receipt["signal_c"] = signal
    receipt["signal_event_date"] = str(newest["event_date"])
    receipt["station_count"] = int(newest["station_count"])

    # A run is counted from the newest day backwards, over days that agree in
    # sign and clear the arm threshold, and it must be unbroken in calendar
    # terms: a gap in settled days is a gap in evidence, not a continuation.
    run_length = 0
    expected: date | None = None
    for row in reversed(series):
        try:
            day = date.fromisoformat(str(row["event_date"])[:10])
        except ValueError:
            break
        if expected is not None and day != expected:
            break
        value = float(row["residual_c_mean"])
        if value * signal <= 0.0 or abs(value) < ARM_THRESHOLD_C:
            break
        if int(row["station_count"]) < MIN_STATION_COUNT:
            break
        run_length += 1
        expected = day - timedelta(days=1)
    receipt["run_length"] = run_length

    # Alerting is independent of whether the correction fires: a fleet that
    # is drifting is worth saying out loud even on a day the guards stand the
    # correction down, because that is exactly the day somebody should look.
    if abs(signal) >= ALERT_RED_C or run_length >= ESCALATION_RUN_DAYS:
        receipt["alert"] = "RED"
    elif abs(signal) >= ALERT_YELLOW_C:
        receipt["alert"] = "YELLOW"

    if int(newest["station_count"]) < MIN_STATION_COUNT:
        receipt["state"] = "STOOD_DOWN"
        receipt["reason"] = "station_count_below_floor"
        return receipt
    try:
        target = date.fromisoformat(str(event_date)[:10])
        signal_day = date.fromisoformat(str(newest["event_date"])[:10])
    except ValueError:
        receipt["state"] = "STOOD_DOWN"
        receipt["reason"] = "event_date_unparsable"
        return receipt
    age = (target - signal_day).days
    receipt["signal_age_days"] = age
    if age > MAX_SIGNAL_AGE_DAYS:
        receipt["state"] = "STOOD_DOWN"
        receipt["reason"] = "signal_stale"
        return receipt
    if age < 0:
        # The signal day is in the future relative to the day being served.
        # That is a lookahead, and it is refused rather than clamped.
        receipt["state"] = "STOOD_DOWN"
        receipt["reason"] = "signal_after_event_date"
        return receipt

    if run_length >= ESCALATION_RUN_DAYS:
        lam, cap, tier = HIGH_LAMBDA, HIGH_CAP_C, "HIGH"
    else:
        lam, cap, tier = LOW_LAMBDA, LOW_CAP_C, "LOW"
    shift = max(-cap, min(cap, lam * signal))
    if not math.isfinite(shift):
        receipt["state"] = "STOOD_DOWN"
        receipt["reason"] = "shift_not_finite"
        return receipt
    receipt["tier"] = tier
    receipt["lambda"] = lam
    receipt["cap_c"] = cap
    receipt["regime_delta_c"] = shift
    receipt["state"] = "APPLIED" if shift != 0.0 else "IDENTITY"
    return receipt


def _truth_with_visibility(
    conn: sqlite3.Connection, *, min_local_date: str
) -> dict[tuple[str, str], tuple[float, str]]:
    """Newest accepted truth per station-day, with when it became visible.

    The visibility stamp is what the as-of cut reads.  ``received_at_utc`` is
    when this process learned the value; a row without one cannot be placed on
    the timeline and is left out rather than assumed instant, because assuming
    instant is how a replay quietly gains information production never had.
    """

    out: dict[tuple[str, str], tuple[float, str]] = {}
    for station, day, value, received in conn.execute(
        "SELECT station_id, local_date, official_tmax_c, received_at_utc"
        " FROM calibration_station_daily_high_truth t"
        " WHERE local_date>=? AND revision_no = ("
        "   SELECT MAX(revision_no)"
        "   FROM calibration_station_daily_high_truth x"
        "   WHERE x.station_id=t.station_id AND x.local_date=t.local_date)",
        (str(min_local_date),),
    ):
        if not received:
            continue
        out[(str(station), str(day))] = (float(value), str(received))
    return out


def refresh_fleet_regime_daily(
    conn: sqlite3.Connection,
    *,
    stage: str,
    now_utc: str,
    lookback_days: int = 10,
) -> dict[str, Any]:
    """Recompute the recent daily fleet residuals for one stage.

    Recent days only.  An old day's residual cannot change unless its truth is
    revised, and a revision that lands inside the window restates the day on
    the next pass; one that lands outside it is older than any signal this
    layer would read anyway.
    """

    from .band_reliability_store import _begin_immediate  # noqa: PLC0415
    from .center_width_store import serving_cells_by_day  # noqa: PLC0415

    stage_id = _check_stage(stage)
    ensure_fleet_regime_schema(conn)
    now = _utc(now_utc)
    floor_day = (now - timedelta(days=lookback_days)).date().isoformat()
    # Decisions for an event day run ahead of it, so the decision bound has to
    # reach back further than the event bound or the earliest days in the
    # window arrive with only their late decisions represented.
    decision_floor = (
        now - timedelta(days=lookback_days + 3)
    ).isoformat().replace("+00:00", "Z")

    truth_rows = _truth_with_visibility(conn, min_local_date=floor_day)
    truth = {key: value for key, (value, _seen) in truth_rows.items()}

    # Which days could possibly have moved.  Assembling the serving cells is
    # the expensive half -- 192s for P1 against 3s for P0-M, because P1
    # re-decides all day -- and a settled day whose truth has not been revised
    # since it was recorded cannot produce a different number.  Deciding that
    # from the truth table alone costs a fraction of a second, so the steady
    # state, where nothing has changed, never pays for the scan at all.
    latest_truth: dict[str, str] = {}
    for (_station, day), (_value, seen) in truth_rows.items():
        current = latest_truth.get(str(day))
        if current is None or _utc(seen) > _utc(current):
            latest_truth[str(day)] = str(seen)
    recorded: dict[str, str] = {}
    for day, seen in conn.execute(
        "SELECT event_date, truth_visible_at_utc"
        " FROM calibration_fleet_regime_daily WHERE stage=?",
        (stage_id,),
    ):
        recorded[str(day)] = str(seen)
    stale_days = sorted(
        day for day, seen in latest_truth.items()
        if day >= floor_day
        and (day not in recorded or _utc(recorded[day]) < _utc(seen))
    )
    if not stale_days:
        return {
            "stage": stage_id,
            "days_written": 0,
            "event_dates": [],
            "window_floor_date": floor_day,
            "state": "UP_TO_DATE",
        }
    # Only reach back as far as the oldest day that actually needs work.
    decision_floor = max(
        decision_floor,
        (
            datetime.fromisoformat(f"{stale_days[0]}T00:00:00")
            .replace(tzinfo=timezone.utc) - timedelta(days=3)
        ).isoformat().replace("+00:00", "Z"),
    )
    wanted = set(stale_days)

    by_day = serving_cells_by_day(
        conn,
        truth,
        stage=stage_id,
        min_decision_at_utc=decision_floor,
        pre_band=False,
    )
    written = 0
    days: list[str] = []
    for day, cells in sorted(by_day.items()):
        if str(day) not in wanted:
            continue
        summary = summarise_day(cells, truth)
        if summary is None:
            continue
        seen = [
            truth_rows[(str(cell["station_id"]), str(cell["event_date"]))][1]
            for cell in cells
            if (str(cell["station_id"]), str(cell["event_date"])) in truth_rows
        ]
        if not seen:
            continue
        # One short transaction per day, taken and released here rather than
        # held across the scan above.  A maintenance writer that holds the
        # write lock across its own read does not slow the resident down, it
        # fail-closes P0 and P1 for that whole cycle.
        _begin_immediate(conn)
        try:
            upsert_daily_residual(
                conn,
                stage=stage_id,
                event_date=str(day),
                residual_c_mean=summary["residual_c_mean"],
                station_count=summary["station_count"],
                cell_count=summary["cell_count"],
                truth_visible_at_utc=max(seen, key=_utc),
                now_utc=str(now_utc),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        written += 1
        days.append(str(day))
    return {
        "stage": stage_id,
        "days_written": written,
        "event_dates": days,
        "window_floor_date": floor_day,
        "state": "REFRESHED",
        "stale_days_seen": len(stale_days),
    }


def fleet_regime_status(
    conn: sqlite3.Connection, *, now_utc: str
) -> dict[str, Any]:
    """The alert surface: what the fleet has been doing, per stage.

    Reported whether or not the correction fires.  A day the guards stand the
    correction down -- a collapsed truth lane, a signal gone stale -- is
    precisely the day somebody should be told the fleet is drifting, so the
    light is derived from the evidence and not from whether the layer acted
    on it.
    """

    out: dict[str, Any] = {
        "schema": "fleet_regime_status_v1",
        "method_tag": FLEET_REGIME_METHOD_TAG,
        "generated_at_utc": str(now_utc),
        "stages": {},
        "alert": "GREEN",
    }
    worst = 0
    rank = {"GREEN": 0, "YELLOW": 1, "RED": 2}
    for stage in FLEET_REGIME_STAGES:
        # Dated to now so a stale series reports as stale rather than being
        # scored against a day it can no longer speak for.  Read through the
        # loader so a dark stage reports exactly what serving would do with
        # it -- nothing -- while keeping the number it withheld.
        receipt = load_regime_shift(
            conn, stage=stage, event_date=str(now_utc)[:10],
            as_of_utc=now_utc,
        )
        series = visible_daily_series(conn, stage=stage, as_of_utc=now_utc)
        out["stages"][stage] = {
            "alert": receipt["alert"],
            "served": stage in FLEET_REGIME_SERVED_STAGES,
            "state": receipt["state"],
            "reason": receipt.get("reason"),
            "tier": receipt["tier"],
            "run_length": receipt["run_length"],
            "signal_c": receipt["signal_c"],
            "signal_event_date": receipt["signal_event_date"],
            "signal_age_days": receipt.get("signal_age_days"),
            "station_count": receipt["station_count"],
            "regime_delta_c": receipt["regime_delta_c"],
            "withheld_delta_c": receipt.get("withheld_delta_c"),
            "recent": [
                {
                    "event_date": row["event_date"],
                    "residual_c_mean": round(row["residual_c_mean"], 4),
                    "station_count": row["station_count"],
                }
                for row in series[-7:]
            ],
        }
        worst = max(worst, rank.get(receipt["alert"], 0))
    out["alert"] = {0: "GREEN", 1: "YELLOW", 2: "RED"}[worst]
    return out


def load_regime_shift(
    conn: sqlite3.Connection,
    *,
    stage: str,
    event_date: str,
    as_of_utc: str,
) -> dict[str, Any]:
    """The shift to serve for one stage-day, with the receipt that explains it.

    This is a read.  It creates nothing: the serving path calls it from inside
    the resident's write transaction, and a lazy CREATE and its commit would
    end that transaction early -- the same rule the centre/width loader keeps.
    """

    stage_id = _check_stage(stage)
    series = visible_daily_series(
        conn, stage=stage_id, as_of_utc=as_of_utc
    )
    receipt = derive_offset(series, event_date=str(event_date))
    if stage_id not in FLEET_REGIME_SERVED_STAGES:
        # Measured, journalled, alerted on -- and not served.  The receipt
        # keeps what the shift would have been so the dark stage stays
        # auditable against the day it might earn its way in.
        receipt["withheld_delta_c"] = receipt["regime_delta_c"]
        receipt["regime_delta_c"] = 0.0
        receipt["state"] = "NOT_SERVED_FOR_STAGE"
    return receipt
