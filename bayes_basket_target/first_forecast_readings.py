"""Each estimate's first forecast of a station-day against the truth, the last 30 days
(user 2026-10-22 「增加首次预报和真值的曲线对比（近30天）」).

The station board's truth hit strip reads every method at the same local hours of the
event day; this reads, for every station-day from 30 days back to two days ahead, the
first number each method gave for that day -- however early it came -- as °C, beside
the settled formal truth (charter 225 lanes):

  P0 / P0-M / P1  -- the stage's first committed ``basket_probability_envelope``, its mean
  market          -- the first committed ``basket_official_ws_snapshot`` whose market_reference_v1
                     ladder is complete (the opening snapshots are often one-sided), its mean
  F               -- charter 267's ledger (F as recorded at listing)
  member Bayes    -- the first row of ``data/board_readings/member_bayes_history`` (post mean)
  D0              -- the first row of ``data/board_readings/d0_history`` (centre)
  WRF             -- ZUUU: the earliest forecast init that covered the day's peak hours
                     (``wrf_fcst_runs_v1.json``), else the day's nowcast record

Means are taken on the station-day's own ladder (rung midpoints; an open end one
interior width out), so C and F stations read in °C alike.  MOS is not here: its
first value needs the day's satellite and Open-Meteo re-read, which the hit strip
already does for the hours it scores.

Writes ``data/board_readings/first_forecast.json``.  A station-day is cached under
``data/runtime/first_forecast`` once every method that can still appear has; open
days are re-read each pass.  Read only; display only; enters no layer.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from . import board_readings
from .canonical_contract import FORMAL_TRUTH_LANES_SQL

ROOT = Path(__file__).resolve().parents[1]
READING_NAME = "first_forecast"
CACHE_PATH = ROOT / "data" / "runtime" / "first_forecast" / "first_v1.json"
READINGS_DIR = board_readings.READINGS_DIR
F_LEDGER = ROOT / "data" / "runtime" / "listing_forecast_f_mos_ledger_v1.jsonl"
WINDOW_DAYS = 30
AHEAD_DAYS = 2
METHODS = ("P0", "P0-M", "P1", "market", "F", "member_bayes", "D0", "WRF")
#: One pass stops reading new station-days after this long; the rest wait for the next.
PASS_BUDGET_SEC = 150.0
#: How far into a station-day's snapshots to look for the first complete market ladder.
MARKET_SCAN_ROWS = 400


def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _zones(conn: sqlite3.Connection) -> dict[str, ZoneInfo]:
    out: dict[str, ZoneInfo] = {}
    for st, tz in conn.execute(
        "SELECT station_id, settlement_timezone FROM calibration_station_market_timezone_binding ORDER BY bound_at_utc"
    ):
        try:
            out[str(st)] = ZoneInfo(str(tz))
        except Exception:  # noqa: BLE001 -- an unknown zone is no station here
            continue
    return out


def _truths(conn: sqlite3.Connection, since: date) -> dict[tuple[str, str], float]:
    out: dict[tuple[str, str], float] = {}
    for st, day, value in conn.execute(
        "SELECT station_id, local_date, official_tmax_c FROM calibration_station_daily_high_truth"
        f" WHERE source_lane IN {FORMAL_TRUTH_LANES_SQL} AND qc_status='ACCEPTED' AND local_date>=?"
        " ORDER BY revision_no", (since.isoformat(),),
    ):
        out[(str(st), str(day))] = float(value)
    return out


def _ladder(conn: sqlite3.Connection, station: str, day: str):
    """(labels, closed edges) of the station-day's ladder in °C, lowest first."""

    from .listing_forecast_f import _rung_edges, station_day_rungs  # noqa: PLC0415

    rungs = station_day_rungs(conn, station_id=station, event_date=date.fromisoformat(day), prefer_registry=False)
    if not rungs:
        return None
    edges = _rung_edges(rungs)
    return ([r[0] for r in rungs], edges[0]) if edges else None


def _mean(ladder, vector: dict[str, float] | None) -> float | None:
    if ladder is None or not vector:
        return None
    labels, edges = ladder
    mid = {lab: (lo + hi) / 2.0 for lab, (lo, hi) in zip(labels, edges)}
    rows = [(mid[k], float(v or 0.0)) for k, v in vector.items() if k in mid]
    mass = sum(p for _c, p in rows)
    return round(sum(c * p for c, p in rows) / mass, 2) if mass > 0 else None


def _bucket(ladder, value: float | None) -> str | None:
    from .listing_forecast_f import _rung_of  # noqa: PLC0415

    if ladder is None or value is None:
        return None
    labels, edges = ladder
    return labels[_rung_of(edges, float(value))]


def _first_history(name: str, day: str, key: str) -> dict[str, tuple[str, float]]:
    """station -> (as_of_utc, value) of the first row for ``day`` in a board-readings history."""

    out: dict[str, tuple[str, float]] = {}
    try:
        fh = (READINGS_DIR / f"{name}_history" / f"{day}.jsonl").open(encoding="utf-8")
    except OSError:
        return out
    with fh:
        for line in fh:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            st, at, v = r.get("station"), str(r.get("as_of_utc") or ""), r.get(key)
            if not st or r.get("event_date", day) != day or v is None:
                continue
            if st not in out or at < out[st][0]:
                out[str(st)] = (at, float(v))
    return out


def _f_ledger() -> dict[tuple[str, str], tuple[str, float]]:
    out: dict[tuple[str, str], tuple[str, float]] = {}
    try:
        lines = F_LEDGER.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        st = r.get("station_id") or r.get("station")
        if st and r.get("event_date") and r.get("f_c_at_record") is not None:
            key = (str(st), str(r["event_date"]))
            at = str(r.get("listing_instant_utc") or r.get("f_computed_at_utc") or "")
            if key not in out or at < out[key][0]:
                out[key] = (at, float(r["f_c_at_record"]))
    return out


def _wrf_first() -> dict[str, tuple[str, float]]:
    """day -> (init, °C): the earliest forecast init that covered the day's peak hours,
    else the day's nowcast (satellite arm, with the observed maximum so far)."""

    from . import wrf_readings  # noqa: PLC0415

    out: dict[str, tuple[str, float]] = {}
    for init, per_day in sorted((wrf_readings.load_fcst_runs().get("ZUUU") or {}).items()):
        for day, r in per_day.items():
            if r.get("peak_covered") and r.get("tmax_c") is not None and day not in out:
                out[day] = (init, float(r["tmax_c"]))
    try:
        records = wrf_readings.nowcast_records()
    except Exception:  # noqa: BLE001 -- the WRF volume is optional
        records = {}
    for day, rec in records.items():
        v = rec.get("wrf_sat_tmax_with_obs_c")
        if day not in out and v is not None:
            out[day] = (str(rec.get("ready_utc") or ""), float(v))
    return out


def first_values(conn: sqlite3.Connection, station: str, day: str, *, f_ledger, wrf, mb, d0) -> dict[str, Any]:
    """Each method's first forecast of one station-day: {method: [at_utc, °C]}."""

    ladder = _ladder(conn, station, day)
    out: dict[str, Any] = {}
    for stage in ("P0", "P0-M", "P1"):
        row = conn.execute(
            "SELECT decision_at_utc, probabilities_json FROM basket_probability_envelope"
            " WHERE station_id=? AND event_date=? AND state='COMMITTED' AND +stage=?"
            " ORDER BY decision_at_utc LIMIT 1", (station, day, stage)).fetchone()
        if row and row[1]:
            v = _mean(ladder, json.loads(row[1]))
            if v is not None:
                out[stage] = [str(row[0]), v]
    # The market's first complete ladder: the opening snapshots are often one-sided.
    from .basket_kernel.valuation_v1 import market_reference_v1  # noqa: PLC0415

    for at, buckets in conn.execute(
        "SELECT observed_at, buckets_json FROM basket_official_ws_snapshot WHERE station_id=? AND event_date=?"
        " AND state='COMMITTED' ORDER BY observed_at LIMIT ?", (station, day, MARKET_SCAN_ROWS)):
        ref = market_reference_v1(json.loads(buckets)) if buckets else {}
        if ref.get("state") == "complete":
            v = _mean(ladder, {str(k): float(x) for k, x in ref["vector"].items()})
            if v is not None:
                out["market"] = [str(at), v]
            break
    if (station, day) in f_ledger:
        out["F"] = list(f_ledger[(station, day)])
    if station in mb:
        out["member_bayes"] = list(mb[station])
    if station in d0:
        out["D0"] = list(d0[station])
    if station == "ZUUU" and day in wrf:
        out["WRF"] = list(wrf[day])
    for m, (at, v) in list(out.items()):
        out[m] = [at, v, _bucket(ladder, v)]
    return {"methods": out, "ladder": ladder is not None}


def _stats(rows: list[tuple[float, bool]]) -> dict[str, Any]:
    if not rows:
        return {"n": 0}
    errs = [e for e, _h in rows]
    return {"n": len(rows), "bias_c": round(sum(errs) / len(errs), 2),
            "mae_c": round(sum(abs(e) for e in errs) / len(errs), 2),
            "hit": round(sum(1 for _e, h in rows if h) / len(rows), 3)}


def compute(conn: sqlite3.Connection, *, now: datetime | None = None,
            cache: dict[str, Any] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    now = now or datetime.now(timezone.utc)
    cache = dict(cache or {})
    zones = _zones(conn)
    start = time.monotonic()
    today_utc = now.date()
    first = today_utc - timedelta(days=WINDOW_DAYS)
    truths = _truths(conn, first)
    f_ledger, wrf = _f_ledger(), _wrf_first()
    stations = sorted(zones)
    days = [(first + timedelta(days=i)).isoformat() for i in range(WINDOW_DAYS + AHEAD_DAYS + 2)]
    out: dict[str, dict[str, Any]] = {}
    skipped = 0
    for day in days:
        mb = _first_history("member_bayes", day, "post_mean_c")
        d0 = _first_history("d0", day, "centre_c")
        for st in stations:
            zone = zones[st]
            local_today = now.astimezone(zone).date()
            dd = date.fromisoformat(day)
            if not (local_today - timedelta(days=WINDOW_DAYS - 1) <= dd <= local_today + timedelta(days=AHEAD_DAYS)):
                continue
            key, truth = f"{st}|{day}", truths.get((st, day))
            old = cache.get(key)
            # A settled day's firsts never change once read after it settled.
            if old and old.get("settled") and old.get("truth_c") == truth:
                rec = old
            elif time.monotonic() - start > PASS_BUDGET_SEC:
                skipped += 1
                rec = old
            else:
                rec = first_values(conn, st, day, f_ledger=f_ledger, wrf=wrf, mb=mb, d0=d0)
                rec["truth_c"] = truth
                rec["settled"] = truth is not None and dd < local_today
                cache[key] = rec
            if rec:
                out.setdefault(st, {})[day] = rec
    # Errors against the truth, per station and for the fleet, on settled days.
    fleet: dict[str, list[tuple[float, bool]]] = {}
    summary: dict[str, Any] = {}
    for st, per_day in out.items():
        own: dict[str, list[tuple[float, bool]]] = {}
        for day, rec in per_day.items():
            t = rec.get("truth_c")
            if t is None or not rec.get("settled"):
                continue
            ladder_truth = None
            for m, (_at, v, b) in (rec.get("methods") or {}).items():
                if ladder_truth is None:
                    ladder_truth = _truth_bucket(conn, st, day, t, cache)
                row = (v - t, b is not None and b == ladder_truth)
                own.setdefault(m, []).append(row)
                fleet.setdefault(m, []).append(row)
        summary[st] = {m: _stats(r) for m, r in own.items()}
    cutoff = [k for k in cache if k.split("|")[1] < first.isoformat()]
    for k in cutoff:
        cache.pop(k, None)
    value = {"state": "ok", "as_of_utc": _iso(now), "window_days": WINDOW_DAYS, "ahead_days": AHEAD_DAYS,
             "methods": list(METHODS), "pending_station_days": skipped,
             "fleet": {m: _stats(r) for m, r in fleet.items()},
             "stations": {st: {"days": {d: {"truth_c": r.get("truth_c"), "methods": r.get("methods") or {}}
                                         for d, r in sorted(per_day.items())},
                               "scores": summary.get(st) or {}}
                          for st, per_day in out.items()}}
    return value, cache


def _truth_bucket(conn: sqlite3.Connection, station: str, day: str, truth: float, cache: dict[str, Any]) -> str | None:
    rec = cache.get(f"{station}|{day}") or {}
    if rec.get("truth_bucket_for") == truth:
        return rec.get("truth_bucket")
    b = _bucket(_ladder(conn, station, day), truth)
    rec["truth_bucket_for"], rec["truth_bucket"] = truth, b
    return b


def view(station: str) -> dict[str, Any] | None:
    """The station board's chart: this station's days and scores, the fleet's scores --
    the stored reading, read, never computed here."""

    doc = board_readings.load_reading(READING_NAME)
    v = (doc or {}).get("value")
    if not isinstance(v, dict) or v.get("state") != "ok":
        return None
    S = (v.get("stations") or {}).get(station) or {}
    return {"as_of_utc": v.get("as_of_utc"), "window_days": v.get("window_days"), "methods": v.get("methods"),
            "fleet": v.get("fleet"), "scores": S.get("scores") or {}, "days": S.get("days") or {}}


def run(*, db: Path = board_readings.DEFAULT_DB) -> dict[str, Any]:
    t0 = time.monotonic()
    try:
        cache = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cache = {}
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30)
    try:
        value, cache = compute(conn, cache=cache)
    finally:
        conn.close()
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE_PATH.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(cache), encoding="utf-8")
    tmp.replace(CACHE_PATH)
    code = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    board_readings.write_reading(READING_NAME, value, code=code, build_sec=time.monotonic() - t0)
    return value


def main() -> None:
    value = run()
    print(json.dumps({"state": value.get("state"), "pending": value.get("pending_station_days"),
                      "fleet": {m: f"{x.get('mae_c')}°/{x.get('n')}" for m, x in (value.get("fleet") or {}).items()}},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
