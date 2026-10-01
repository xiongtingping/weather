"""The settled decision population the calibration pages and the L4 lane share.

``population`` reads every decision of a stage scope a fit at a cutoff could
use: the committed envelope's vector, the market reference built from the WS
snapshot the decision priced against, and the bucket the truth landed in.
The charter EV page, the centre-shape page and the L4 posterior lane read it.

It was written for charter 132's pooled valuation -- ``(1 - beta) * m + beta
* p`` fitted per stage on the truth bucket's score -- and the fit, its tables,
its journal and its walk-forward verdicts retired with charter 233
(2026-09-27): a shadow no rule read.  The tables stay in the store as history.
"""
from __future__ import annotations

import json
import math
import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping

from .basket_kernel.valuation_v1 import market_reference_v1

STAGE_SCOPES: dict[str, tuple[str, ...]] = {
    "P0": ("P0",), "P0-M": ("P0-M",), "P1": ("P1",),
}
WINDOW_DAYS = 28
BOOTSTRAP_DRAWS = 200
BOOTSTRAP_SEED = 20260913


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    try:
        return conn.execute(
            "SELECT 1 FROM pragma_table_list WHERE type='table' AND name=?", (name,)
        ).fetchone() is not None
    except sqlite3.Error:
        return False


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _instant(value: str) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _anchor(conn: sqlite3.Connection, stage: str) -> tuple[str | None, str]:
    """The stage's serving-arithmetic anchor and how it was read.

    The same three states the beta lane reads (charter 132, audit item 48):
    ``absent`` when the store that keeps the anchor is not in this database
    (an extract, a fixture) and there is no bound; ``ok``, possibly None; and
    ``error``, a present store that failed, which refuses the fit rather than
    widening it onto a distribution nobody serves.
    """

    if not (
        _has_table(conn, "basket_probability_bridge_history")
        and _has_table(conn, "basket_probability_envelope")
    ):
        return None, "absent"
    try:
        from .center_width_store import evidence_anchor  # noqa: PLC0415

        return evidence_anchor(conn, stage=stage), "ok"
    except Exception:  # noqa: BLE001 -- a present store that fails is a refusal
        return None, "error"


def population(
    conn: sqlite3.Connection,
    *,
    stage_scope: str,
    as_of: str | None = None,
    window_days: int = WINDOW_DAYS,
    cutoff_utc: str | None = None,
    anchored: bool = True,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Every decision of the scope's stages a fit at ``cutoff_utc`` could use.

    One row per decision: the committed envelope's vector, the market
    reference built from the WS snapshot the decision priced against, and the
    bucket the truth landed in -- read off the truth lane's label, with the
    moment it became knowable.  The same strict-as-of contract the beta and
    lambda lanes carry (charter 132): the label must have been knowable at the
    cutoff, and a decision taken after the cutoff is not evidence either.

    And the floor they carry (2026-09-14): a decision taken before the stage's
    serving-arithmetic anchor priced a weather vector nobody serves any more,
    so it is not evidence about how much weight today's vector earns beside
    the market.  This lane shipped without it and pooled 28 days across P0's
    v4 (2026-09-10) and charter 134's stable pool (v5, P1 v10) -- the one
    change this lane exists to judge, since only a better probability layer
    can move its beta off zero.  ``anchored=False`` is a research reading and
    is never persisted.  An anchor set after the cutoff did not exist then
    and is dropped, as on the beta and lambda lanes.
    """

    if stage_scope not in STAGE_SCOPES:
        raise ValueError(f"valuation_scope_invalid:{stage_scope}")
    from .candidate_window_evidence import LABEL_TABLE  # noqa: PLC0415

    # Charter 140: the labels come from the truth lane's own table.
    needed = (
        "basket_policy_training_fact", "basket_probability_envelope",
        "basket_official_ws_snapshot", LABEL_TABLE,
    )
    for table in needed:
        if not _has_table(conn, table):
            return [], {"error": f"missing_table:{table}"}
    cutoff = str(cutoff_utc or _utc_now_iso())
    from .label_knowability import knowable_labels  # noqa: PLC0415

    labels: dict[tuple[str, str], tuple[str, str]] = {
        key: (label["bucket"], label["known_at"])
        for key, label in knowable_labels(conn).items()
        if str(label["known_at"]) <= cutoff
    }
    if not labels:
        return [], {"error": "no_knowable_labels", "cutoff_utc": cutoff}
    latest = str(as_of or max(day for _s, day in labels))
    floor_day = (
        date.fromisoformat(latest) - timedelta(days=int(window_days))
    ).isoformat()
    stages = STAGE_SCOPES[stage_scope]
    anchor_states: dict[str, str] = {}
    anchors: list[str] = []
    anchors_after_cutoff = 0
    if anchored:
        for stage in stages:
            value, state = _anchor(conn, stage)
            anchor_states[stage] = state
            if state == "error":
                return [], {
                    "error": "anchor_unreadable", "stage": stage,
                    "cutoff_utc": cutoff,
                }
            if value:
                if _instant(value) > _instant(cutoff):
                    anchors_after_cutoff += 1
                else:
                    anchors.append(str(value))
    anchor_utc = max(anchors, key=_instant) if anchors else None
    placeholders = ",".join("?" for _ in stages)
    rows: list[dict[str, Any]] = []
    counts = {
        "decisions_read": 0, "no_label": 0, "decided_after_cutoff": 0,
        "excluded_before_anchor": 0,
        "market_incomplete": 0, "label_outside_contract": 0,
        "weather_vector_invalid": 0,
    }
    reasons: dict[str, int] = {}
    if anchor_utc is not None:
        # Counted, not hidden: the seek below never returns these rows, so
        # the count is taken here, over the same stages and event window.
        counts["excluded_before_anchor"] = int(conn.execute(
            "SELECT COUNT(*) FROM basket_policy_training_fact f"
            f" WHERE f.stage IN ({placeholders})"
            "   AND f.event_date > ? AND f.event_date <= ?"
            "   AND f.decision_at_utc < ?",
            (*stages, floor_day, latest, str(anchor_utc)[:19]),
        ).fetchone()[0] or 0)
    # The envelope's settlement clock rides along for the posterior lane's
    # lead bands; a fixture without the column reads None and has no band.
    try:
        has_tz = any(
            str(col[1]) == "settlement_timezone"
            for col in conn.execute("PRAGMA table_info(basket_probability_envelope)")
        )
    except sqlite3.Error:
        has_tz = False
    tz_col = "e.settlement_timezone" if has_tz else "NULL"
    cursor = conn.execute(
        "SELECT f.station_id, f.event_date, f.stage, f.decision_at_utc,"
        f"       e.probabilities_json, s.buckets_json, {tz_col}"
        " FROM basket_policy_training_fact f"
        " JOIN basket_probability_envelope e"
        "   ON e.envelope_id = f.probability_envelope_id"
        " JOIN basket_official_ws_snapshot s ON s.snapshot_id = f.ws_snapshot_id"
        f" WHERE f.stage IN ({placeholders})"
        "   AND f.event_date > ? AND f.event_date <= ?"
        # A seekable superset of the anchor (whole seconds, no zone), so the
        # exact instant test below never parses rows it would drop.
        + ("" if anchor_utc is None else "   AND f.decision_at_utc >= ?"),
        (*stages, floor_day, latest,
         *(() if anchor_utc is None else (str(anchor_utc)[:19],))),
    )
    for station, day, stage, decided_at, probs_json, book_json, tz in cursor:
        counts["decisions_read"] += 1
        labelled = labels.get((str(station), str(day)))
        if labelled is None:
            counts["no_label"] += 1
            continue
        label, label_known_at = labelled
        if str(decided_at) > cutoff:
            counts["decided_after_cutoff"] += 1
            continue
        if anchor_utc is not None and _instant(decided_at) < _instant(anchor_utc):
            counts["excluded_before_anchor"] += 1
            continue
        try:
            p_weather = {
                str(k): float(v) for k, v in json.loads(probs_json or "{}").items()
            }
            book = json.loads(book_json or "[]")
        except (TypeError, ValueError):
            counts["weather_vector_invalid"] += 1
            continue
        total_p = sum(p_weather.values())
        if not (total_p > 0.0 and math.isfinite(total_p)):
            counts["weather_vector_invalid"] += 1
            continue
        p_weather = {k: v / total_p for k, v in p_weather.items()}
        market = market_reference_v1(book if isinstance(book, list) else [])
        if market["state"] != "complete":
            counts["market_incomplete"] += 1
            key = str(market.get("reason") or "").split(":")[0]
            reasons[key] = reasons.get(key, 0) + 1
            continue
        m = market["vector"]
        if label not in m:
            counts["label_outside_contract"] += 1
            continue
        rows.append({
            "station": str(station), "date": str(day), "stage": str(stage),
            "at": str(decided_at), "label": label,
            # When the label became knowable, so a walk-forward caller that
            # cut the population once can cut each fold again in memory.
            "label_known_at": label_known_at,
            "m_label": float(m[label]),
            "p_label": float(p_weather.get(label, 0.0)),
            "brier_m": _brier(m, label),
            "brier_p": _brier_on(p_weather, m, label),
            "m": m, "p": p_weather,
            # The station's settlement clock, so the posterior lane can put
            # the decision in its lead band without a second scan.
            "settlement_timezone": None if tz is None else str(tz),
            # Per bucket, on the vector's scale: what crossing the book costs
            # from the mid.  The charter EV page prices the disagreement
            # strategy's spread cost from it; the fit does not read it.
            "half_spread": dict(market.get("half_spread") or {}),
        })
    # Each station-day counts once, however many decisions it carried.
    per_station_day: dict[tuple[str, str], int] = {}
    for row in rows:
        key = (row["station"], row["date"])
        per_station_day[key] = per_station_day.get(key, 0) + 1
    for row in rows:
        row["weight"] = 1.0 / per_station_day[(row["station"], row["date"])]
    return rows, {
        "as_of_event_date": latest, "floor_day": floor_day,
        "cutoff_utc": cutoff, "market_incomplete_reasons": reasons,
        "anchored": bool(anchored), "anchor_utc": anchor_utc,
        "anchor_states": anchor_states,
        "anchors_after_cutoff": anchors_after_cutoff, **counts,
    }


def _brier(vector: Mapping[str, float], label: str) -> float:
    return sum(
        (float(v) - (1.0 if str(b) == label else 0.0)) ** 2
        for b, v in vector.items()
    )


def _brier_on(
    vector: Mapping[str, float], contract: Mapping[str, float], label: str,
) -> float:
    return sum(
        (float(vector.get(b, 0.0) or 0.0) - (1.0 if str(b) == label else 0.0)) ** 2
        for b in contract
    )


__all__ = [
    "BOOTSTRAP_DRAWS",
    "BOOTSTRAP_SEED",
    "STAGE_SCOPES",
    "WINDOW_DAYS",
    "population",
]
