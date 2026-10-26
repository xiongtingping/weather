"""Charter 260: which stage is the belief at an instant.

Charter 283 (user ruling 2026-10-26 「按清单退役 P0-M」): P0-M is retired; the
belief is P1 once one stands and P0 before it.  The P0-M rows already
committed stay in the record and are never the belief again; the two
supersession helpers below remain for fits whose windows still hold them.

User ruling 2026-10-09 (「P0-M 在 P1 存在后的当日决策是否不再作为独立信念服务」—
「裁决」): once a P1 decision stands for a station-day, that day's P0-M is no
longer an independent belief.  P0-M keeps being compiled -- it is P1's root --
but a reader of "our probability now" takes the most informed stage standing
at the instant, P1 before P0-M before P0, never the newest row of any stage,
and a fitter does not count a P0-M decision taken after the day's first P1 as
evidence about a distribution anyone serves.

Measured on the served record 2026-09-05..10-08 (``docs/research/
calibration_battery_20261009``): P0-M decisions within 12 hours of settlement
ran 0.57C warm with 26% of their mass above the mode where the truth landed
1.4% of the time, LogScore 0.85 against the same hours' P1 at 0.18 -- the
observed floor (charter 241) cuts the cold side, nothing in P0-M knows the
peak has passed.  P1 does.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Iterable, Mapping, Sequence

#: Most informed first: observations (P1) over NWP alone (P0).  P0-M was
#: retired by charter 283; its committed rows are not read as the belief.
BELIEF_STAGE_PRECEDENCE: tuple[str, ...] = ("P1", "P0")


def served_belief_row(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    event_date: str,
    at_utc: str,
) -> dict[str, Any] | None:
    """The committed bridge decision that is the belief at ``at_utc``: the
    newest decision of the most informed stage that has one by then.  None
    when no stage has decided yet."""

    for stage in BELIEF_STAGE_PRECEDENCE:
        row = conn.execute(
            "SELECT history_id, stage, decision_at_utc FROM basket_probability_bridge_history"
            " WHERE station_id=? AND event_date=? AND state='COMMITTED' AND stage=? AND decision_at_utc<=?"
            " ORDER BY decision_at_utc DESC LIMIT 1",
            (str(station_id), str(event_date), stage, str(at_utc)),
        ).fetchone()
        if row is not None:
            return {"history_id": str(row[0]), "stage": str(row[1]), "decision_at_utc": str(row[2])}
    return None


def first_p1_decisions(
    conn: sqlite3.Connection, *, min_decision_at_utc: str | None = None,
) -> dict[tuple[str, str], str]:
    """``(station_id, event_date) -> decision_at_utc`` of each station-day's
    first committed P1 decision: from that instant the day's P0-M is
    superseded.  Station-days without a P1 are absent."""

    query = (
        "SELECT station_id, event_date, MIN(decision_at_utc) FROM basket_probability_bridge_history"
        " WHERE stage='P1' AND state='COMMITTED'"
    )
    params: list[Any] = []
    if min_decision_at_utc:
        # A P1 before the bound still supersedes every later P0-M, so the bound
        # is not applied to the P1 itself; it only keeps the scan short when
        # the caller's own rows start there.
        query += " AND event_date >= substr(?, 1, 10)"
        params.append(str(min_decision_at_utc))
    query += " GROUP BY station_id, event_date"
    return {(str(s), str(d)): str(at) for s, d, at in conn.execute(query, params)}


def p0m_superseded(stage: str, station_id: str, event_date: str, decision_at_utc: str,
                   first_p1: Mapping[tuple[str, str], str]) -> bool:
    """Whether this P0-M decision was taken at or after the day's first P1."""

    if str(stage) != "P0-M":
        return False
    cutoff = first_p1.get((str(station_id), str(event_date)))
    return cutoff is not None and str(decision_at_utc) >= cutoff


def drop_superseded_p0m(
    rows: Iterable[Sequence[Any]], *, stage: str, first_p1: Mapping[tuple[str, str], str],
) -> list[Sequence[Any]]:
    """Rows of ``(station_id, event_date, decision_at_utc, ...)`` for ``stage``
    with the P0-M decisions a P1 had already superseded removed."""

    if str(stage) != "P0-M":
        return list(rows)
    return [r for r in rows if not p0m_superseded("P0-M", r[0], r[1], r[2], first_p1)]


__all__ = [
    "BELIEF_STAGE_PRECEDENCE", "served_belief_row", "first_p1_decisions", "p0m_superseded", "drop_superseded_p0m",
]
