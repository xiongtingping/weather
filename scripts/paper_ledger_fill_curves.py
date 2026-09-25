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

Weekly re-pin (user ruling 2026-09-25: 「每周自动（样本外核对过线才换）」):
``--weekly-repin`` fits a candidate on every labelled order before the last
``HOLDOUT_DAYS`` event days, scores it and the curves in service on the
orders of those days -- which neither saw -- and installs the candidate only
when it is no worse on both proper scores.  Fills are censored by cancels
and expiries, so both scores are inverse-probability-of-censoring weighted
(the censored-data Brier of Graf et al., and its log-loss twin) at the
horizons ``SCORE_HORIZONS_SECONDS``.  One decided attempt a week; a holdout
too thin to decide is retried the next day.  Every attempt appends a receipt
to ``data/paper_ledger/maker_fill_curve_repin.jsonl``.
"""
from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import math
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "config" / "paper_ledger" / "maker_fill_curves_v1.json"
REPIN_RECEIPTS = ROOT / "data" / "paper_ledger" / "maker_fill_curve_repin.jsonl"
SCHEMA = "paper_ledger_maker_fill_curves_v1"
LEAD_BANDS = (("in_day", 24.0), ("d1", 48.0), ("d2plus", float("inf")))
#: The weekly re-pin: the last HOLDOUT_DAYS labelled event days are the
#: out-of-sample check; the candidate's recent window is the FIT_WINDOW_DAYS
#: before them (extended back per cell as ``fit`` does).  A decision needs at
#: least MIN_HOLDOUT_ORDERS orders on those days, and one decision stands for
#: REPIN_INTERVAL_DAYS.
HOLDOUT_DAYS = 7
FIT_WINDOW_DAYS = 10
MIN_HOLDOUT_ORDERS = 20
REPIN_INTERVAL_DAYS = 7
#: Where the scores are read: a resting rung's life is tens of minutes to a
#: few hours (``F(30m)``/``F(1h)``/``F(3h)`` are what the fit prints).
SCORE_HORIZONS_SECONDS = (1800.0, 3600.0, 10800.0)


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
            "placed_at_utc": str(created),
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


def _sealed(artifact: Mapping[str, Any]) -> dict[str, Any]:
    body = {k: v for k, v in artifact.items() if k != "content_sha256"}
    body["content_sha256"] = hashlib.sha256(
        json.dumps(body, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return body


def _censoring_survival(rows: Sequence[Mapping[str, Any]]) -> tuple[list[float], list[float]]:
    """Reverse Kaplan-Meier: G(t) = P(an order is still uncensored at t)."""

    times, cdf = kaplan_meier([(max(0.0, float(row["seconds"])), 1 - int(row["filled"])) for row in rows])
    return times, [1.0 - value for value in cdf]


def _survival_before(curve: tuple[list[float], list[float]], seconds: float) -> float:
    """G(t-): the censoring survival just before ``seconds``."""

    times, values = curve
    k = bisect.bisect_left(times, seconds)
    return float(values[k - 1]) if k else 1.0


def censored_scores(
    curves: Mapping[str, Mapping[str, Sequence[float]]],
    rows: Sequence[Mapping[str, Any]],
    horizons: Sequence[float] = SCORE_HORIZONS_SECONDS,
) -> dict[str, float]:
    """IPCW Brier and log loss of ``curves`` on ``rows``, averaged over horizons.

    At horizon h an order that filled by h counts 1 and one still resting
    after h counts 0, each weighted by one over the chance of being observed
    that long; an order cancelled or expired before h says nothing about h
    and weighs zero.  Both are proper under independent censoring.
    """

    g = _censoring_survival(rows)
    n = len(rows)
    brier = log_loss = 0.0
    for horizon in horizons:
        for row in rows:
            seconds = max(0.0, float(row["seconds"]))
            if row["filled"] and seconds <= horizon:
                outcome, weight = 1.0, 1.0 / max(_survival_before(g, seconds), 1e-9)
            elif seconds > horizon:
                outcome, weight = 0.0, 1.0 / max(_survival_before(g, horizon + 1e-9), 1e-9)
            else:
                continue
            p = min(max(cdf_at(curves[cell_key(row)], horizon), 1e-6), 1.0 - 1e-6)
            brier += weight * (outcome - p) ** 2
            log_loss -= weight * (outcome * math.log(p) + (1.0 - outcome) * math.log(1.0 - p))
    scale = float(n * len(horizons)) or 1.0
    return {"brier": round(brier / scale, 6), "log_loss": round(log_loss / scale, 6)}


def build_artifact(
    rows: Sequence[Mapping[str, Any]], *, fit_from: str, trained_through: str | None = None,
    fitted_at_utc: str | None = None, gate: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The curves fitted on ``rows`` (those after ``trained_through`` left out)."""

    rows = [row for row in rows if trained_through is None or str(row["event_date"]) <= trained_through]
    fitted = [row for row in rows if row["event_date"] >= fit_from]
    earlier = [row for row in rows if row["event_date"] < fit_from]
    curves = fit(rows, fit_from=fit_from)
    predicted = sum(
        cdf_at(curves[cell_key(row)], float(row["seconds"]))
        for row in earlier if cell_key(row) in curves
    )
    artifact: dict[str, Any] = {
        "schema": SCHEMA,
        "fitted_on_event_dates_from": fit_from,
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
    if trained_through is not None:
        artifact["trained_through_event_date"] = trained_through
    if fitted_at_utc is not None:
        artifact["fitted_at_utc"] = fitted_at_utc
    if gate is not None:
        artifact["out_of_sample_gate"] = dict(gate)
    return _sealed(artifact)


def _receipts(path: Path) -> list[dict[str, Any]]:
    try:
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except FileNotFoundError:
        return []


def repin_due(receipts: Sequence[Mapping[str, Any]], *, as_of_utc: str) -> bool:
    """Due unless a decided attempt (installed or kept) is under a week old."""

    decided = [
        _instant(r["attempted_at_utc"]) for r in receipts
        if r.get("verdict") in {"installed", "kept", "seeded"}
    ]
    return not decided or _instant(as_of_utc) - max(decided) >= timedelta(days=REPIN_INTERVAL_DAYS)


def decide_repin(
    rows: Sequence[Mapping[str, Any]], incumbent: Mapping[str, Any], *, as_of_utc: str,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """(receipt, the artifact to install or None) for one re-pin attempt."""

    as_of_date = _instant(as_of_utc).date()
    holdout_from = (as_of_date - timedelta(days=HOLDOUT_DAYS)).isoformat()
    holdout_to = (as_of_date - timedelta(days=1)).isoformat()
    trained_through = (as_of_date - timedelta(days=HOLDOUT_DAYS + 1)).isoformat()
    fit_from = (as_of_date - timedelta(days=HOLDOUT_DAYS + FIT_WINDOW_DAYS)).isoformat()
    holdout = [row for row in rows if holdout_from <= str(row["event_date"]) <= holdout_to]
    receipt: dict[str, Any] = {
        "schema": "paper_ledger_maker_fill_curve_repin_v1",
        "attempted_at_utc": str(as_of_utc),
        "holdout_event_dates": [holdout_from, holdout_to],
        "holdout_orders": len(holdout),
        "holdout_fills": sum(int(row["filled"]) for row in holdout),
        "incumbent_sha256": incumbent.get("content_sha256"),
        "horizons_seconds": list(SCORE_HORIZONS_SECONDS),
    }
    if len(holdout) < MIN_HOLDOUT_ORDERS:
        return {**receipt, "verdict": "accumulating", "min_holdout_orders": MIN_HOLDOUT_ORDERS}, None
    candidate = build_artifact(rows, fit_from=fit_from, trained_through=trained_through, fitted_at_utc=str(as_of_utc))
    challenger = censored_scores(candidate["curves"], holdout)
    standing = censored_scores(incumbent["curves"], holdout)
    passed = (
        challenger["brier"] <= standing["brier"] + 1e-12
        and challenger["log_loss"] <= standing["log_loss"] + 1e-12
    )
    gate = {
        "holdout_event_dates": [holdout_from, holdout_to],
        "holdout_orders": len(holdout),
        "candidate": challenger, "incumbent": standing,
        "incumbent_sha256": incumbent.get("content_sha256"),
        "passed": passed,
    }
    if not passed:
        return {**receipt, "verdict": "kept", "candidate": challenger, "incumbent": standing,
                "candidate_sha256": candidate["content_sha256"]}, None
    installed = build_artifact(
        rows, fit_from=fit_from, trained_through=trained_through,
        fitted_at_utc=str(as_of_utc), gate=gate,
    )
    return {**receipt, "verdict": "installed", "candidate": challenger, "incumbent": standing,
            "candidate_sha256": installed["content_sha256"]}, installed


def weekly_repin(
    ssot: sqlite3.Connection, *, as_of_utc: str, curves_path: Path = DEFAULT_OUTPUT,
    receipts_path: Path = REPIN_RECEIPTS,
) -> dict[str, Any]:
    receipts = _receipts(receipts_path)
    if not repin_due(receipts, as_of_utc=as_of_utc):
        return {"verdict": "not_due", "last_decided": max(
            r["attempted_at_utc"] for r in receipts if r.get("verdict") in {"installed", "kept", "seeded"}
        )}
    incumbent = json.loads(Path(curves_path).read_text(encoding="utf-8"))
    receipt, installed = decide_repin(real_basket_maker_orders(ssot), incumbent, as_of_utc=as_of_utc)
    if installed is not None:
        Path(curves_path).write_text(json.dumps(installed, indent=1, sort_keys=True) + "\n")
    receipts_path.parent.mkdir(parents=True, exist_ok=True)
    with receipts_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(receipt, sort_keys=True, ensure_ascii=False) + "\n")
    return receipt


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--ssot", default=str(ROOT / "data" / "bayes_basket_target_ssot.db"))
    parser.add_argument("--fit-from", default="2026-09-15", help="first event date the curves are fitted on")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--weekly-repin", action="store_true",
                        help="the scheduled re-pin: candidate against the curves in service, out of sample")
    parser.add_argument("--receipts", default=str(REPIN_RECEIPTS))
    parser.add_argument("--as-of", default=None, help="the re-pin's clock (default now)")
    args = parser.parse_args(argv)
    ssot = sqlite3.connect(f"file:{args.ssot}?mode=ro", uri=True, timeout=60)
    if args.weekly_repin:
        as_of = args.as_of or datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        print(json.dumps(weekly_repin(
            ssot, as_of_utc=as_of, curves_path=Path(args.output), receipts_path=Path(args.receipts),
        ), sort_keys=True, ensure_ascii=False))
        return 0
    rows = real_basket_maker_orders(ssot)
    artifact = build_artifact(rows, fit_from=args.fit_from)
    curves = artifact["curves"]
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
