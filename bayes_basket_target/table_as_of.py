"""Which fitted table a stage was serving at a past moment.

``build_native_ensemble_oos`` loads the centre/width scalars and the
per-station width scales that are active NOW.  That is right for a
forward build -- the artifact it mints is served from now on -- and wrong for
a replay of a past build: a table fitted on the days being replayed, or later,
scores those days in sample.  Model-layer review 2026-09-13, R4: strict
as-of on the cases (which ``_load_cases`` has) is not strict as-of on the
parameters the gate scores them under.

The stores keep one row per table version with the time it was LAST
activated, and every fit writes a journal row saying whether it activated
anything.  An activation event is either record; the table active at ``T`` is
the latest event at or before ``T``, unless that version was retired at or
before ``T``.  A moment before any event has no table, which is what the
stage served then (identity for the widths).  The band table left this
module with its layer (charter 232, 2026-09-26): a replay scores the
arithmetic that exists, and no band layer does.

Limits, stated rather than hidden.  The station-width rows keep the
time a version was LAST activated, so an earlier activation of a re-activated
version that never reached the journal is invisible.  The centre/width row of
a superseded version has its activation time cleared outright, so for that
table the journal is the whole history (on 2026-09-13 it holds the one P0
activation, 2026-09-03T21:19Z, and both P0-M ones).
``activation_events`` returns what was read so a replay can record it.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from .band_reliability_store import utc_instant
from .center_width_store import (
    load_center_width,
    load_center_width_floor,
    load_center_width_slices,
    load_center_width_tiers,
    load_station_width,
)


def _events(
    conn: sqlite3.Connection,
    *,
    table: str,
    journal: str,
    sha_column: str,
    stage: str,
) -> list[tuple[str, str]]:
    events: set[tuple[str, str]] = set()
    try:
        for at, sha in conn.execute(
            f"SELECT activated_at_utc, {sha_column} FROM {table}"
            " WHERE stage=? AND activated_at_utc IS NOT NULL",
            (stage,),
        ):
            events.add((str(at), str(sha)))
    except sqlite3.OperationalError:
        pass
    try:
        for at, sha in conn.execute(
            f"SELECT fitted_at_utc, {sha_column} FROM {journal}"
            " WHERE stage=? AND outcome='ACTIVATED'"
            f" AND {sha_column} IS NOT NULL",
            (stage,),
        ):
            events.add((str(at), str(sha)))
    except sqlite3.OperationalError:
        pass
    return sorted(events, key=lambda event: (utc_instant(event[0]), event[1]))


def _retired_at(
    conn: sqlite3.Connection, *, table: str, sha_column: str, sha: str
) -> str | None:
    try:
        row = conn.execute(
            f"SELECT retired_at_utc FROM {table}"
            f" WHERE {sha_column}=? AND retired_at_utc IS NOT NULL",
            (sha,),
        ).fetchall()
    except sqlite3.OperationalError:
        return None
    stamps = [str(stamp) for (stamp,) in row]
    return min(stamps, key=utc_instant) if stamps else None


def _sha_as_of(
    conn: sqlite3.Connection,
    *,
    table: str,
    journal: str,
    sha_column: str,
    stage: str,
    as_of_utc: str,
    retirable: bool,
) -> str | None:
    at_or_before = [
        (at, sha)
        for at, sha in _events(
            conn, table=table, journal=journal,
            sha_column=sha_column, stage=stage,
        )
        if utc_instant(at) <= utc_instant(as_of_utc)
    ]
    if not at_or_before:
        return None
    _at, sha = at_or_before[-1]
    if retirable:
        retired = _retired_at(
            conn, table=table, sha_column=sha_column, sha=sha
        )
        if retired is not None and (
            utc_instant(retired) <= utc_instant(as_of_utc)
        ):
            return None
    return sha


def activation_events(
    conn: sqlite3.Connection, *, stage: str
) -> dict[str, list[tuple[str, str]]]:
    """Every activation event each resolver reads, for a replay's receipt."""

    return {
        "center_width": _events(
            conn,
            table="calibration_center_width_table",
            journal="calibration_center_width_fit_journal",
            sha_column="center_width_sha256",
            stage=stage,
        ),
        "station_width": _events(
            conn,
            table="calibration_center_width_station",
            journal="calibration_center_width_station_journal",
            sha_column="station_width_sha256",
            stage=stage,
        ),
    }


def center_width_as_of(
    conn: sqlite3.Connection, *, stage: str, as_of_utc: str
) -> dict[str, Any] | None:
    """The scalars, floor and tiers of the version active at ``as_of_utc``."""

    sha = _sha_as_of(
        conn,
        table="calibration_center_width_table",
        journal="calibration_center_width_fit_journal",
        sha_column="center_width_sha256",
        stage=stage,
        # Retirable since charter 138: the pooled lane can now veto its own
        # incumbent, so a replay after the retirement must score identity the
        # same way the station lane's does -- reading the pair back would
        # replay a width the veto already ruled wrong.
        as_of_utc=as_of_utc,
        retirable=True,
    )
    if sha is None:
        return None
    delta_c, width_lambda, resolved = load_center_width(
        conn, stage=stage, table_sha256=sha, as_of_utc=as_of_utc
    )
    return {
        "delta_c": delta_c,
        "width_lambda": width_lambda,
        "center_width_sha256": resolved,
        "sigma_floor_rungs": load_center_width_floor(
            conn, stage=stage, table_sha256=sha, as_of_utc=as_of_utc
        ),
        "width_tiers": load_center_width_tiers(
            conn, stage=stage, table_sha256=sha, as_of_utc=as_of_utc
        ),
        # Charter 239: the slices ride on the same row and the same sha.
        "width_slices": load_center_width_slices(
            conn, stage=stage, table_sha256=sha, as_of_utc=as_of_utc
        ),
    }


def station_width_as_of(
    conn: sqlite3.Connection, *, stage: str, as_of_utc: str
) -> tuple[dict[str, float], str] | None:
    """Per-station scales of the version active at ``as_of_utc``."""

    sha = _sha_as_of(
        conn,
        table="calibration_center_width_station",
        journal="calibration_center_width_station_journal",
        sha_column="station_width_sha256",
        stage=stage,
        as_of_utc=as_of_utc,
        retirable=True,
    )
    if sha is None:
        return None
    loaded = load_station_width(
        conn, stage=stage, table_sha256=sha, as_of_utc=as_of_utc
    )
    if loaded is None or not loaded[0]:
        return None
    return loaded


def describe_as_of(
    conn: sqlite3.Connection, *, stage: str, as_of_utc: str
) -> dict[str, Any]:
    """What a replay at ``as_of_utc`` scores under, as plain JSON."""

    width = center_width_as_of(conn, stage=stage, as_of_utc=as_of_utc)
    station = station_width_as_of(conn, stage=stage, as_of_utc=as_of_utc)
    return json.loads(json.dumps({
        "stage": stage,
        "as_of_utc": as_of_utc,
        "center_width": width,
        "station_width_sha256": None if station is None else station[1],
    }))
