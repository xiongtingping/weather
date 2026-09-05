"""The belief and the book a station-day held H hours before a decision.

One loader, read by production's two compile entries, the paper ledger's
production judge and the effective-edge store's shadow lane, so every
reader pairs the same earlier checkpoint with the same earlier book: the
latest committed checkpoint at least ``horizon_hours`` before ``as_of_utc``
(any stage -- it is the belief the system held then), priced on the first
committed book at or after it, which is the book production compiled that
checkpoint on (the ledger's ``_books_for_checkpoint`` pairing).  Nothing
at or after ``as_of_utc`` is read.  None when no such checkpoint or book
exists -- every opening seat under charter 48.

The compile chain holds no database handle by design, so this returns a
plain mapping the caller hands in as an artifact.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from .basket_kernel.delta_v2 import CONVERGENCE_CLAIM_HORIZON_HOURS

SCHEMA = "convergence_evidence_v1"


def _clock(ts: str, *, hours_before: float) -> str:
    moment = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    earlier = (moment - timedelta(hours=float(hours_before))).astimezone(UTC)
    return earlier.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def best_asks(buckets_json: str) -> dict[str, float]:
    """Best ask per bucket off one committed book; unquoted buckets absent."""

    try:
        entries = json.loads(str(buckets_json))
    except (ValueError, TypeError):
        return {}
    out: dict[str, float] = {}
    for entry in entries or []:
        levels = entry.get("ask_levels") or []
        if levels:
            out[str(entry.get("bucket"))] = float(levels[0]["price"])
    return out


def load_convergence_evidence(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    event_date: str,
    as_of_utc: str,
    horizon_hours: float | None = None,
) -> dict[str, Any] | None:
    horizon = float(
        CONVERGENCE_CLAIM_HORIZON_HOURS if horizon_hours is None else horizon_hours
    )
    try:
        earlier = _clock(as_of_utc, hours_before=horizon)
    except (ValueError, TypeError):
        return None
    try:
        prior = conn.execute(
            "SELECT history_id, stage, decision_at_utc"
            " FROM basket_probability_bridge_history"
            " WHERE station_id=? AND event_date=? AND state='COMMITTED'"
            "   AND decision_at_utc<=?"
            " ORDER BY decision_at_utc DESC LIMIT 1",
            (str(station_id), str(event_date), earlier),
        ).fetchone()
        if prior is None:
            return None
        probabilities = {
            str(bucket): float(probability)
            for bucket, probability in conn.execute(
                "SELECT bucket, probability"
                " FROM basket_probability_bridge_bucket_probability"
                " WHERE history_id=?",
                (str(prior[0]),),
            )
        }
        book = conn.execute(
            "SELECT observed_at, buckets_json FROM basket_official_ws_snapshot"
            " WHERE station_id=? AND event_date=? AND state='COMMITTED'"
            "   AND observed_at>=? AND observed_at<?"
            " ORDER BY observed_at ASC LIMIT 1",
            (str(station_id), str(event_date), str(prior[2]), str(as_of_utc)),
        ).fetchone()
    except sqlite3.Error:
        return None
    if not probabilities or book is None:
        return None
    asks = best_asks(book[1])
    if not asks:
        return None
    return {
        "schema": SCHEMA,
        "horizon_hours": horizon,
        "as_of_utc": str(as_of_utc),
        "prior_decision_at_utc": str(prior[2]),
        "prior_stage": str(prior[1]),
        "prior_probabilities": probabilities,
        "prior_book_observed_at": str(book[0]),
        "prior_asks": asks,
    }


def window_sum(table: Any, window: Any) -> float | None:
    """Sum a per-bucket table over a window; None when any bucket is absent."""

    if not table:
        return None
    total = 0.0
    for bucket in window:
        value = table.get(str(bucket)) if hasattr(table, "get") else None
        if value is None:
            return None
        total += float(value)
    return total


__all__ = ["SCHEMA", "best_asks", "load_convergence_evidence", "window_sum"]
