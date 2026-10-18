"""Charter 267 补记九: the deterministic centre's final chain, written as a shadow beside F.

User 2026-10-18 「预登记前向检验：最终链 L1→L2→L4 并行写进 F 文件作影子，不接车道，
满一定天数后与现行 F 对比再裁」.  Research round 75 walked four MOS layers strictly
forward and kept three: station values (L1), each system's station bias shrunk
to the system's fleet bias (L2), and shrunk inverse-MAE weights (L4); the chain
beat production F by 0.038 C [-0.057, -0.021] in both halves.  Round 77 checks
it forward: this module records, once per station-day and as of that day's
listing instant T (D-2 05:00Z), the chain's centre in an append-only ledger
beside the F file -- never in it, while ``listing_forecast_f.py`` carries
another session's uncommitted change.

The algorithm and every constant are round 75's (``docs/research/
deterministic_centre_mos_r75_20261018/run.py``), unchanged:

* members: each F system's newest run knowable at T that covers the
  afternoon (``listing_forecast_f.system_maxima``), plus its lapse-rate
  offset (``listing_forecast_f_terrain.offset_c``); a member without an
  offset is dropped; fewer than three members, no record.
* L2: per system, b = b_fleet + k (b_station - b_fleet); both are
  ``HALF_LIFE``-day decay-weighted medians of truth - x' over station-days
  settled by T; k = n_eff / (n_eff + sigma2 / tau2) by moments, 0 under
  ``MIN_NEFF``.
* L4: the station's own settled days, each at its own listing instant's L2,
  give each system a decay-weighted MAE; weights 1/MAE shrunk to equal by
  lambda = n_eff / (n_eff + ``N0``).

History is the station-days settled by T within ``HISTORY_DAYS`` before it
(round 75 read from 07-25, under 90 days; the decay makes older days
negligible).  Execution-layer only: no lane, no P0/P0-M/P1 reads it.
"""

from __future__ import annotations

import json
import os
import sqlite3
import statistics
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from . import listing_forecast_f as L
from . import listing_forecast_f_terrain as terrain
from .deterministic_forecast_family import F_SYSTEM_IDS, global_roster

ROOT = Path(__file__).resolve().parents[1]
LEDGER_PATH = Path(
    os.environ.get("PM_LISTING_FORECAST_F_MOS_LEDGER") or ROOT / "data" / "runtime" / "listing_forecast_f_mos_ledger_v1.jsonl"
)
SCHEMA = "listing_forecast_f_mos_v1"
RULE = "station_value_shrunk_bias_shrunk_inverse_mae_r75"
SYSTEMS = sorted(F_SYSTEM_IDS)
HALF_LIFE = 25.0
MIN_NEFF = 3.0
MIN_MEMBERS = 3
N0 = 20.0
HISTORY_DAYS = 120
# Only event days whose listing instant passed within this window are recorded:
# a forward record, never a late fill.
RECORD_WINDOW = timedelta(hours=24)


def _wmedian(values: Sequence[float], weights: Sequence[float]) -> float:
    pairs = sorted(zip(values, weights))
    total, run = sum(weights), 0.0
    for value, weight in pairs:
        run += weight
        if run >= total / 2.0:
            return value
    return pairs[-1][0]


def _decay(day: date, ref: date) -> float:
    return 0.5 ** ((ref - day).days / HALF_LIFE)


def station_values(maxima: Mapping[str, Mapping[str, Any]], *, station: str, table: Mapping[str, Any]) -> dict[str, float]:
    """L1: each member's grid maximum plus its lapse-rate offset; no offset, no member."""

    out = {}
    for system, entry in maxima.items():
        offset = terrain.offset_c(table, system_id=system, station_id=station)
        if offset is not None:
            out[system] = float(entry["tmax_c"]) + float(offset)
    return out


def history(conn: sqlite3.Connection, roster, *, since: date, until: date, table) -> list[dict[str, Any]]:
    """Settled station-days in [since, until]: truth, when it settled, members at their own listing."""

    truths = L.formal_truths(conn, since=since, until=until)
    rows = []
    for station, _lat, _lon, zone in roster:
        day = since
        while day <= until:
            truth = truths.get((station, day.isoformat()))
            if truth is not None:
                maxima = L.system_maxima(
                    conn, station_id=station, event_date=day, as_of_utc=L.listing_instant(day),
                    settlement_timezone=zone, systems=SYSTEMS,
                )
                xst = station_values(maxima, station=station, table=table)
                if xst:
                    rows.append({"station": station, "day": day, "truth": float(truth),
                                 "settled": L._settled_at(day, zone), "listing": L._listing_dt(day), "xst": xst})
            day += timedelta(days=1)
    return rows


def bias_table(train: Sequence[Mapping[str, Any]], ref: date) -> dict[str, tuple[float, dict[str, tuple[float, float]]]]:
    """Per system: (fleet bias, {station: (station bias, k)})."""

    out = {}
    for system in SYSTEMS:
        errs: dict[str, list[tuple[float, float]]] = defaultdict(list)
        for r in train:
            if system in r["xst"]:
                errs[r["station"]].append((r["truth"] - r["xst"][system], _decay(r["day"], ref)))
        if not errs:
            continue
        b_fleet = _wmedian([e for v in errs.values() for e, _w in v], [w for v in errs.values() for _e, w in v])
        stats, within = {}, []
        for station, v in errs.items():
            e = np.array([x for x, _ in v])
            w = np.array([y for _, y in v])
            neff = float(w.sum() ** 2 / (w ** 2).sum())
            if len(v) > 1:
                mean = (w * e).sum() / w.sum()
                within.append((float((w * (e - mean) ** 2).sum() / w.sum()), float(w.sum())))
            stats[station] = (_wmedian(list(e), list(w)), neff)
        sigma2 = (sum(a * b for a, b in within) / sum(b for _a, b in within)) if within else 0.0
        eligible = [(b, n) for b, n in stats.values() if n >= MIN_NEFF]
        tau2 = 0.0
        if len(eligible) >= 2:
            bs = np.array([b for b, _ in eligible])
            tau2 = max(float(bs.var(ddof=1)) - statistics.mean(sigma2 / n for _b, n in eligible), 0.0)
        ks = {}
        for station, (b_s, neff) in stats.items():
            k = 0.0 if neff < MIN_NEFF or tau2 <= 0 or sigma2 <= 0 else neff / (neff + sigma2 / tau2)
            ks[station] = (b_s, k)
        out[system] = (b_fleet, ks)
    return out


def l2_members(xst: Mapping[str, float], station: str, table: Mapping[str, Any]) -> dict[str, float]:
    out = {}
    for system, value in xst.items():
        if system not in table:
            out[system] = value
            continue
        b_fleet, ks = table[system]
        b_s, k = ks.get(station, (b_fleet, 0.0))
        out[system] = value + b_fleet + k * (b_s - b_fleet)
    return out


def l4(members: Mapping[str, float], own: Sequence[tuple[date, float, Mapping[str, float]]], ref: date) -> float:
    """Shrunk inverse-MAE weights from the station's own settled days (each at its own L2)."""

    maes, neffs = {}, {}
    for system in members:
        errs = [(abs(truth - values[system]), _decay(day, ref)) for day, truth, values in own if system in values]
        if errs:
            w = np.array([b for _a, b in errs])
            maes[system] = float(sum(a * b for a, b in errs) / w.sum())
            neffs[system] = float(w.sum() ** 2 / (w ** 2).sum())
    equal = 1.0 / len(members)
    inv = {s: 1.0 / max(maes[s], 0.1) for s in members if s in maes and neffs[s] >= MIN_NEFF}
    total = sum(inv.values())
    weights = {}
    for s in members:
        if s in inv:
            lam = neffs[s] / (neffs[s] + N0)
            weights[s] = lam * inv[s] / total * (len(inv) / len(members)) + (1 - lam) * equal
        else:
            weights[s] = equal
    norm = sum(weights.values())
    return sum(members[s] * weights[s] / norm for s in members)


class Chain:
    """Round 75's chain over one history, evaluated as of any listing instant."""

    def __init__(self, rows: Sequence[Mapping[str, Any]]):
        self.rows = list(rows)
        self.by_station: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for r in self.rows:
            self.by_station[r["station"]].append(r)
        self._tables: dict[date, Any] = {}
        self._own_l2: dict[tuple[str, date], dict[str, float]] = {}

    def table(self, day: date):
        """The bias table as of ``day``'s listing instant, cached."""

        if day not in self._tables:
            listing = L._listing_dt(day)
            self._tables[day] = bias_table([r for r in self.rows if r["settled"] <= listing], day)
        return self._tables[day]

    def own_l2(self, r: Mapping[str, Any]) -> dict[str, float]:
        key = (r["station"], r["day"])
        if key not in self._own_l2:
            self._own_l2[key] = l2_members(r["xst"], r["station"], self.table(r["day"]))
        return self._own_l2[key]

    def centre(self, *, station: str, day: date, xst: Mapping[str, float]) -> dict[str, Any] | None:
        if len(xst) < MIN_MEMBERS:
            return None
        listing = L._listing_dt(day)
        members = l2_members(xst, station, self.table(day))
        own = [(q["day"], q["truth"], self.own_l2(q)) for q in self.by_station.get(station, []) if q["settled"] <= listing]
        return {
            "mos_c": round(l4(members, own, day), 3),
            "l1_c": round(statistics.median(xst.values()), 3),
            "l2_c": round(statistics.median(members.values()), 3),
            "members_l1": {s: round(v, 3) for s, v in xst.items()},
            "members_l2": {s: round(v, 3) for s, v in members.items()},
        }


def recorded(path: Path = LEDGER_PATH) -> set[tuple[str, str]]:
    keys = set()
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                keys.add((row.get("station_id"), row.get("event_date")))
    except OSError:
        pass
    return keys


def due_event_days(now: datetime) -> list[date]:
    """Event days whose listing instant passed within ``RECORD_WINDOW``."""

    out = []
    for offset in range(0, 4):
        day = now.date() + timedelta(days=offset)
        listing = L._listing_dt(day)
        if listing <= now < listing + RECORD_WINDOW:
            out.append(day)
    return out


def record(conn: sqlite3.Connection, *, now: datetime | None = None, path: Path = LEDGER_PATH,
           f_payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Append this pass's due station-days to the ledger; one line each, never rewritten."""

    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    days = due_event_days(now)
    done = recorded(path)
    roster = global_roster(conn)
    todo = [(station, zone, day) for day in days for station, _a, _b, zone in roster
            if (station, day.isoformat()) not in done]
    if not todo:
        return {"due_days": [d.isoformat() for d in days], "written": 0, "skipped": {}}
    table = terrain.read()
    if table is None:
        return {"due_days": [d.isoformat() for d in days], "written": 0, "skipped": {"terrain_table_missing": len(todo)}}
    first = min(day for _s, _z, day in todo)
    chain = Chain(history(conn, roster, since=first - timedelta(days=HISTORY_DAYS), until=max(d for *_r, d in todo), table=table))
    f_payload = f_payload or L.read() or {}
    f_days = f_payload.get("station_days") or {}
    written, skipped = 0, defaultdict(int)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        for station, zone, day in todo:
            listing = L.listing_instant(day)
            maxima = L.system_maxima(conn, station_id=station, event_date=day, as_of_utc=listing,
                                     settlement_timezone=zone, systems=SYSTEMS)
            xst = station_values(maxima, station=station, table=table)
            centre = chain.centre(station=station, day=day, xst=xst)
            if centre is None:
                skipped["too_few_members"] += 1
                continue
            f_entry = f_days.get(f"{station}|{day.isoformat()}") or {}
            fh.write(json.dumps({
                "schema": SCHEMA, "rule": RULE, "station_id": station, "event_date": day.isoformat(),
                "listing_instant_utc": listing, "recorded_at_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                **centre,
                "runs": {s: maxima[s]["run_init_utc"] for s in xst},
                "f_c_at_record": f_entry.get("f_c"), "f_computed_at_utc": f_payload.get("computed_at_utc"),
                "authority": "EXECUTION_LAYER_SHADOW",
            }, sort_keys=True) + "\n")
            written += 1
    return {"due_days": [d.isoformat() for d in days], "written": written, "skipped": dict(skipped)}


__all__ = ["Chain", "LEDGER_PATH", "bias_table", "due_event_days", "history", "l2_members", "l4", "record", "recorded",
           "station_values"]
