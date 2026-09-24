"""The bucket ladder a station-day was priced against.

A station-day carries one ladder: the rungs of its newest committed
decision, of any stage, read off the bridge history that recorded them with
their edges.  The settlement labels place a settled value on it
(``candidate_window_evidence.standing_labels``), the OOS gate scores its
holdout on it (charter 211 (六) / charter 213), and the band lane reads the
gate's archived predictions against it -- one rule, one query.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from typing import Any

LadderRow = tuple[str, int, float | None, float | None]


def station_day_ladder(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    event_date: str,
    as_of_utc: str | None = None,
) -> tuple[str, str, list[LadderRow]] | None:
    """``(history_id, decision_at_utc, rows)`` or None when nothing committed.

    ``rows`` are ``(bucket, ordinal, lower_c, upper_c)`` in ordinal order.
    ``as_of_utc`` holds the read to the decisions committed by then, which
    is what a build replayed at a past moment could have known.
    """

    if as_of_utc is None:
        head = conn.execute(
            "SELECT history_id, decision_at_utc"
            " FROM basket_probability_bridge_history"
            " WHERE station_id=? AND event_date=? AND state='COMMITTED'"
            " ORDER BY decision_at_utc DESC LIMIT 1",
            (station_id, str(event_date)[:10]),
        ).fetchone()
    else:
        head = conn.execute(
            "SELECT history_id, decision_at_utc"
            " FROM basket_probability_bridge_history"
            " WHERE station_id=? AND event_date=? AND state='COMMITTED'"
            " AND decision_at_utc<=?"
            " ORDER BY decision_at_utc DESC LIMIT 1",
            (station_id, str(event_date)[:10], str(as_of_utc)),
        ).fetchone()
    if head is None:
        return None
    rows = conn.execute(
        "SELECT bucket, ordinal, lower_c, upper_c"
        " FROM basket_probability_bridge_bucket_probability"
        " WHERE history_id=? ORDER BY ordinal",
        (head[0],),
    ).fetchall()
    return (
        str(head[0]),
        str(head[1]),
        [
            (
                str(bucket),
                int(ordinal),
                None if lower is None else float(lower),
                None if upper is None else float(upper),
            )
            for bucket, ordinal, lower, upper in rows
        ],
    )


def settled_rung(rows: Sequence[Sequence[Any]], value_c: float) -> str | None:
    """The rung a settled value lands in, lower edge inclusive.

    The settler's rule (``paper_trading_ledger._settled_bucket``); None when
    the value is outside every rung.
    """

    for bucket, _ordinal, lower, upper, *_rest in rows:
        low = -1e9 if lower is None else float(lower)
        high = 1e9 if upper is None else float(upper)
        if low <= float(value_c) < high:
            return str(bucket)
    return None


__all__ = ["LadderRow", "settled_rung", "station_day_ladder"]
