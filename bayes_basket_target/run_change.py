"""The latest-run change of the three systems whose updates carry resolution
the market lacks (charter 265).

User rulings 2026-10-10 「加，并把这个研究结果写进宪章」「作为执行层的选桶、作为模型
的分辨率和条件性」「并不一定是ECMWF家族，去研究一下其他家族」「改」.  Measured
(docs/research/run_change_families_20261010; replication of round 18 in
docs/research/direction_signal_20261010 addendum 2): an hour after a new run is
in hand, "this run minus the previous one" calls the side of the market mode
the truth lands on beyond the market's own lean -- walk-forward binary log loss
AIFS ENS -0.0104 [-0.0127, -0.0084], AIFS single -0.0096 [-0.0152, -0.0025],
GEFS -0.0025 [-0.0038, -0.0011], each past a Bonferroni bar over twenty
systems; AIGEFS, ICON, UKMO, CMA, HRRR and NBM carry none.  The market drifts
toward every model's update but absorbs AIFS's only about half within twelve
hours.  The level of a run against the market carries nothing; the change
does, until the market has taken it in.

A run counts from the instant THIS system had it -- the native ensemble's
earliest ``archive_query_at_utc``, the point forecast's ``knowable_at_utc`` --
never a publication schedule, so no reader of this module can see a run before
production could have.  Pure reads, no writes.
"""
from __future__ import annotations

import math
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

RULE = "latest_run_change_aifs_aifs_single_gefs_v1"
#: The three systems, in the order of the realisation layer's change columns.
SYSTEMS = ("ECMWF_AIFS_ENS_GLOBAL", "ECMWF_AIFS_SINGLE_POINT", "NOAA_GEFS_05")
#: How each system's runs are read.  Wider than ``SYSTEMS``: the direction
#: signal (charter 266) reads the IFS ensemble and the HRES point runs through
#: the same reader, on the same "from the instant we had it" clock.
NATIVE_SYSTEMS = frozenset({"ECMWF_AIFS_ENS_GLOBAL", "NOAA_GEFS_05", "ECMWF_IFS_ENS_OPEN_PF50"})
POINT_SYSTEMS = frozenset({"ECMWF_AIFS_SINGLE_POINT", "ECMWF_IFS_9KM_RELAY_POINT"})
#: A native run is read only once this many members are in hand.
MIN_NATIVE_MEMBERS = 10
#: The conditionality: a change weighs exp(-hours / TAU) since we had the newer
#: run.  The market had moved half its twelve-hour drift by about six hours
#: (AIFS ENS +0.045 of +0.090 C at 6 h), so after a day a change weighs ~2%.
FRESHNESS_TAU_HOURS = 6.0
#: A Fahrenheit ladder's rung in C (2 F); a Celsius ladder's is 1 C.
F_RUNG_C = 10.0 / 9.0


def _instant(value: Any) -> datetime:
    text = str(value).replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def family_runs(
    conn: sqlite3.Connection, *, station_id: str, event_date: str, systems: Sequence[str] = SYSTEMS,
) -> dict[str, list[tuple]]:
    """Every run of ``systems`` (the three by default) for one station-day:
    ``system -> [(had_at, run_init, value_c)]`` sorted by run init, one entry
    per run (a point run re-captured keeps its first capture)."""

    wanted = tuple(str(system) for system in systems)
    unknown = [system for system in wanted if system not in NATIVE_SYSTEMS | POINT_SYSTEMS]
    if unknown:
        raise ValueError(f"run_change_system_unknown:{','.join(unknown)}")
    out: dict[str, list[tuple]] = {}
    try:
        _read_runs(conn, station_id, event_date, out, wanted)
    except sqlite3.OperationalError as exc:
        # A store without the run tables (a fixture, a research copy) has no
        # change to read: every system is simply absent.
        if "no such table" not in str(exc):
            raise
        return {system: [] for system in wanted}
    return out


def _read_runs(
    conn: sqlite3.Connection, station_id: str, event_date: str, out: dict[str, list[tuple]], systems: Sequence[str],
) -> None:
    for system in systems:
        if system in NATIVE_SYSTEMS:
            rows = conn.execute(
                "SELECT native_run_init_utc, MIN(archive_query_at_utc), AVG(raw_tmax_c), COUNT(*)"
                " FROM calibration_native_ensemble_historical_member_run"
                " WHERE station_id=? AND target_local_date=? AND ensemble_system_id=? AND qc_status='ACCEPTED'"
                " AND native_run_init_utc IS NOT NULL AND raw_tmax_c IS NOT NULL"
                " GROUP BY native_run_init_utc",
                (station_id, event_date, system),
            ).fetchall()
            out[system] = sorted(
                ((_instant(had), str(init), float(value))
                 for init, had, value, count in rows
                 if had and int(count) >= MIN_NATIVE_MEMBERS),
                key=lambda row: row[1],
            )
            continue
        first: dict[str, tuple] = {}
        for init, knowable, value in conn.execute(
            "SELECT run_init_utc, knowable_at_utc, forecast_tmax_c FROM calibration_regional_point_run"
            " WHERE station_id=? AND target_local_date=? AND ensemble_system_id=?"
            " AND knowable_at_utc IS NOT NULL AND forecast_tmax_c IS NOT NULL",
            (station_id, event_date, system),
        ):
            had = _instant(knowable)
            if str(init) not in first or had < first[str(init)][0]:
                first[str(init)] = (had, str(init), float(value))
        out[system] = sorted(first.values(), key=lambda row: row[1])


def latest_change(runs: Mapping[str, Sequence[tuple]], *, as_of: Any) -> dict[str, dict[str, float]]:
    """Per system, its newest run in hand at ``as_of`` minus the run before it
    (both in hand by then), and the hours since the newer one arrived.  A
    system without two runs in hand is absent."""

    moment = _instant(as_of)
    out: dict[str, dict[str, float]] = {}
    for system in SYSTEMS:
        held = [row for row in runs.get(system) or () if row[0] <= moment]
        if len(held) < 2:
            continue
        newest = held[-1]
        before = [row for row in held[:-1] if row[1] < newest[1]]
        if not before:
            continue
        out[system] = {
            "delta_c": newest[2] - before[-1][2],
            "hours_since": max(0.0, (moment - newest[0]).total_seconds() / 3600.0),
        }
    return out


def change_columns(change: Mapping[str, Mapping[str, float]], *, rung_c: float) -> list[float]:
    """The realisation layer's three inputs, in ``SYSTEMS`` order: each
    system's change in rungs of the ladder, weighed by its freshness; zero
    for a system with no change in hand."""

    out = []
    for system in SYSTEMS:
        part = change.get(system)
        if not part:
            out.append(0.0)
            continue
        fresh = math.exp(-float(part["hours_since"]) / FRESHNESS_TAU_HOURS)
        out.append(float(part["delta_c"]) * fresh / float(rung_c))
    return out


def ladder_rung_c(bucket_labels: Sequence[str]) -> float:
    """A ladder's rung in C, read off its own labels (a market is either F or C)."""

    return F_RUNG_C if any("°F" in str(label) for label in bucket_labels) else 1.0


def change_at(conn: sqlite3.Connection, *, station_id: str, event_date: str, as_of: Any) -> dict[str, dict[str, float]]:
    """``latest_change`` read straight from the store."""

    return latest_change(family_runs(conn, station_id=station_id, event_date=event_date), as_of=as_of)


__all__ = [
    "FRESHNESS_TAU_HOURS", "RULE", "SYSTEMS", "change_at", "change_columns", "family_runs",
    "ladder_rung_c", "latest_change",
]
