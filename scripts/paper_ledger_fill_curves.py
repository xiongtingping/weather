#!/usr/bin/env python3
"""Calibrate the paper ledger's maker venue on production's real orders.

The paper ledger decides through production's own entries (user ruling
2026-09-24: 「账本对齐」); the one thing it has to model is the venue's answer
to a resting maker order.  The recorded books cannot answer it directly: a
resting bid is filled by a seller hitting it, which never shows in a snapshot
as the ask crossing the bid (the match is instant), and the store keeps no
trade tape.  Measured 2026-09-25: the crossing rule predicted 0 of the 52
fills among production's 137 basket maker orders that reached the exchange
from 2026-09-12.

So the venue is calibrated on what the exchange actually did with the orders
production posted.  For every basket maker order (``intent_`` prefix, GTD,
post-only) that reached the exchange and whose station-day has a settlement
label, the time from posting to the first fill is an event and an order that
ended unfilled is censored at its end.  A Kaplan-Meier curve F(t) = P(first
fill by t) is fitted per cell:

* lead band -- hours from posting to the end of the event's local day:
  ``in_day`` (< 24), ``d1`` (24-48), ``d2plus`` (>= 48).  Fill intensity
  falls with lead (77% / 44% / 28% of orders filled);
* whether the order's bucket settled -- adverse selection: an order on a
  losing bucket fills more readily (above the bid, 47% against 32%).  Only
  the venue sees this; nothing the strategy reads carries it.

The artifact records the window it was fitted on and a validation: fitted on
event days from ``--fit-from``, it predicts the fills of the earlier orders
over their real lives.
"""
from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "config" / "paper_ledger" / "maker_fill_curves_v1.json"
SCHEMA = "paper_ledger_maker_fill_curves_v1"
LEAD_BANDS = (("in_day", 24.0), ("d1", 48.0), ("d2plus", float("inf")))


def _instant(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def lead_band(lead_hours: float) -> str:
    for name, upper in LEAD_BANDS:
        if lead_hours < upper:
            return name
    return LEAD_BANDS[-1][0]


def lead_hours(placed_at_utc: str, event_date: str, settlement_timezone: str) -> float:
    try:
        zone = ZoneInfo(str(settlement_timezone))
    except Exception:  # noqa: BLE001 -- an unknown zone reads as UTC
        zone = ZoneInfo("UTC")
    end_local = datetime.fromisoformat(str(event_date) + "T00:00:00").replace(tzinfo=zone) + timedelta(days=1)
    return (end_local - _instant(placed_at_utc)).total_seconds() / 3600.0


def kaplan_meier(observations: Sequence[tuple[float, int]]) -> tuple[list[float], list[float]]:
    """(time, 1 = fill / 0 = censored) -> step points of F(t) = 1 - S(t)."""

    obs = sorted(observations)
    at_risk, survival, times, cdf, i = len(obs), 1.0, [], [], 0
    while i < len(obs):
        t, fills, leaving = obs[i][0], 0, 0
        while i < len(obs) and obs[i][0] == t:
            fills += obs[i][1]
            leaving += 1
            i += 1
        if fills:
            survival *= 1.0 - fills / at_risk
            times.append(float(t))
            cdf.append(1.0 - survival)
        at_risk -= leaving
    return times, cdf


def cdf_at(curve: Mapping[str, Sequence[float]], seconds: float) -> float:
    times, cdf = curve["t_seconds"], curve["F"]
    k = bisect.bisect_right(times, seconds)
    return float(cdf[k - 1]) if k else 0.0


def fill_seconds_for_draw(curve: Mapping[str, Sequence[float]], draw: float) -> float | None:
    """The first time the curve reaches ``draw``; None when it never does."""

    for t, value in zip(curve["t_seconds"], curve["F"]):
        if value >= draw:
            return float(t)
    return None


def order_draw(*keys: Any) -> float:
    """A deterministic uniform draw for one order: a replay is byte-identical."""

    digest = hashlib.sha256("|".join(("maker_fill_v1", *map(str, keys))).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") / float(1 << 64)


def real_basket_maker_orders(ssot: sqlite3.Connection) -> list[dict[str, Any]]:
    labels = {
        (str(s), str(d)): str(b)
        for s, d, b in ssot.execute(
            "SELECT station_id, event_date, settled_bucket FROM basket_station_day_settlement_label l"
            " WHERE truth_revision_no=(SELECT MAX(truth_revision_no) FROM basket_station_day_settlement_label x"
            "  WHERE x.station_id=l.station_id AND x.event_date=l.event_date)"
        )
    }
    out = []
    for intent_id, station, day, tz, bucket, intent_json, created in ssot.execute(
        "SELECT i.order_intent_id, c.station_id, c.event_date, c.settlement_timezone, i.bucket,"
        " i.intent_json, i.created_at FROM basket_target_order_intents i"
        " JOIN basket_target_execution_cycles c ON c.cycle_id=i.cycle_id"
        " WHERE substr(c.plan_id,1,5)='plan_' AND i.order_intent_id LIKE 'intent_%'"
        " AND i.order_action='BUY' ORDER BY i.created_at"
    ).fetchall():
        body = json.loads(intent_json or "{}")
        if body.get("execution_candidate") != "MAKER":
            continue
        settled = labels.get((str(station), str(day)))
        if settled is None:
            continue
        events = ssot.execute(
            "SELECT event_type, observed_at FROM basket_target_order_events"
            " WHERE order_intent_id=? ORDER BY observed_at", (intent_id,),
        ).fetchall()
        kinds = [kind for kind, _at in events]
        if "POST_ACK" not in kinds:
            continue
        placed = _instant(created)
        fills = [_instant(at) for kind, at in events if kind in {"FILL", "PARTIAL_FILL"}]
        ends = [_instant(at) for kind, at in events if kind in {"CANCEL_ACK", "EXPIRED", "FILL"}]
        expiration = body.get("expiration")
        if expiration:
            ends.append(datetime.fromtimestamp(int(expiration), timezone.utc))
        if not fills and not ends:
            continue
        out.append({
            "intent_id": str(intent_id), "station_id": str(station), "event_date": str(day),
            "bucket": str(bucket), "won": str(bucket) == settled,
            "lead_band": lead_band(lead_hours(str(created), str(day), str(tz or "UTC"))),
            "filled": bool(fills),
            "seconds": ((fills[0] if fills else min(ends)) - placed).total_seconds(),
        })
    return out


def cell_key(row: Mapping[str, Any]) -> str:
    return f"{row['lead_band']}|{'won' if row['won'] else 'lost'}"


#: A cell is fitted on at least this many orders: the recent window first,
#: then earlier orders newest-first until it has enough, then its lead band
#: with won and lost pooled, then every order.
MIN_CELL_ORDERS = 30
#: A lead band too rare to reach MIN_CELL_ORDERS in all its history is still
#: fitted on that history when it has this many orders -- thin, and marked so
#: -- rather than borrowing another band's intensity (the basket's same-day
#: maker orders: 11 in all, every one filled, against 28% two days out).
MIN_BAND_ORDERS = 10


def _curve(rows: Sequence[Mapping[str, Any]], rule: str) -> dict[str, Any]:
    observations = [(max(0.0, float(row["seconds"])), int(row["filled"])) for row in rows]
    times, cdf = kaplan_meier(observations)
    return {
        "orders": len(observations), "fills": sum(event for _t, event in observations),
        "rule": rule,
        "earliest_event_date": min((str(row["event_date"]) for row in rows), default=""),
        "t_seconds": [round(t, 3) for t in times], "F": [round(v, 6) for v in cdf],
    }


def _recent_then_back(rows: Sequence[Mapping[str, Any]], fit_from: str) -> list[Mapping[str, Any]]:
    recent = [row for row in rows if str(row["event_date"]) >= fit_from]
    if len(recent) >= MIN_CELL_ORDERS:
        return recent
    older = sorted(
        (row for row in rows if str(row["event_date"]) < fit_from),
        key=lambda row: str(row["event_date"]), reverse=True,
    )
    return recent + older[: MIN_CELL_ORDERS - len(recent)]


def fit(rows: Sequence[Mapping[str, Any]], *, fit_from: str) -> dict[str, dict[str, Any]]:
    out = {}
    for band, _upper in LEAD_BANDS:
        for won in (True, False):
            key = f"{band}|{'won' if won else 'lost'}"
            cell = _recent_then_back(
                [row for row in rows if row["lead_band"] == band and row["won"] is won], fit_from,
            )
            if len(cell) >= MIN_CELL_ORDERS:
                out[key] = _curve(cell, "cell")
                continue
            band_rows = [row for row in rows if row["lead_band"] == band]
            pooled = _recent_then_back(band_rows, fit_from)
            if len(pooled) >= MIN_CELL_ORDERS:
                out[key] = _curve(pooled, "lead_band_pooled")
                continue
            if len(band_rows) >= MIN_BAND_ORDERS:
                out[key] = _curve(band_rows, "lead_band_all_history_thin")
                continue
            out[key] = _curve(_recent_then_back(list(rows), fit_from), "all_pooled")
        # A station-day the venue cannot yet say settled either way (no label
        # yet) is answered by the band with won and lost pooled.
        band_rows = [row for row in rows if row["lead_band"] == band]
        pooled = _recent_then_back(band_rows, fit_from)
        if len(pooled) >= MIN_CELL_ORDERS:
            out[f"{band}|any"] = _curve(pooled, "lead_band_pooled")
        elif len(band_rows) >= MIN_BAND_ORDERS:
            out[f"{band}|any"] = _curve(band_rows, "lead_band_all_history_thin")
        else:
            out[f"{band}|any"] = _curve(_recent_then_back(list(rows), fit_from), "all_pooled")
    return out


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--ssot", default=str(ROOT / "data" / "bayes_basket_target_ssot.db"))
    parser.add_argument("--fit-from", default="2026-09-15", help="first event date the curves are fitted on")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    args = parser.parse_args(argv)
    ssot = sqlite3.connect(f"file:{args.ssot}?mode=ro", uri=True, timeout=60)
    rows = real_basket_maker_orders(ssot)
    fitted = [row for row in rows if row["event_date"] >= args.fit_from]
    earlier = [row for row in rows if row["event_date"] < args.fit_from]
    curves = fit(rows, fit_from=args.fit_from)
    predicted = sum(
        cdf_at(curves[cell_key(row)], float(row["seconds"]))
        for row in earlier if cell_key(row) in curves
    )
    artifact = {
        "schema": SCHEMA,
        "fitted_on_event_dates_from": args.fit_from,
        "fitted_orders": len(fitted),
        "fitted_fills": sum(int(row["filled"]) for row in fitted),
        "lead_bands_hours": {name: upper for name, upper in LEAD_BANDS if upper != float("inf")},
        "min_cell_orders": MIN_CELL_ORDERS,
        "min_band_orders": MIN_BAND_ORDERS,
        "validation": {
            "note": "cells extended back in time for sample size predict some of these orders in-sample",
            "orders_before_fit_window": len(earlier),
            "predicted_fills": round(predicted, 2),
            "actual_fills": sum(int(row["filled"]) for row in earlier),
        },
        "curves": curves,
    }
    artifact["content_sha256"] = hashlib.sha256(
        json.dumps(artifact, sort_keys=True).encode("utf-8")
    ).hexdigest()
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(artifact, indent=1, sort_keys=True) + "\n")
    print(json.dumps({k: artifact[k] for k in ("fitted_orders", "fitted_fills", "validation", "content_sha256")}, indent=1))
    for key, curve in curves.items():
        print(key, "%s from %s orders=%d fills=%d F(30m)=%.2f F(1h)=%.2f F(3h)=%.2f" % (
            curve["rule"], curve["earliest_event_date"], curve["orders"], curve["fills"],
            cdf_at(curve, 1800), cdf_at(curve, 3600), cdf_at(curve, 10800)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
