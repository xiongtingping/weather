"""Truth hit rate of every estimate the station board shows (user 2026-10-22
「统计以上各个方式的真值命中率，前端显式出来！」).

A hit: the bucket the method points to (its mode, or the bucket holding its point
estimate) is the bucket the settled formal truth (charter 225 lanes) falls in, on
the station-day's own market ladder.  Every method is read as of the same local
snapshot times on the event day (``SNAPSHOT_HOURS``), so the rates are comparable;
each method is also paired with the market on the station-days both have.

Methods and where their history lives (all read only):
  P0 / P1         -- ``basket_probability_envelope`` (newest committed per stage; P0-M retired,
                     charter 283)
  market          -- ``basket_official_ws_snapshot`` within 30 minutes
  deterministic F -- charter 267's ledger (F as recorded at listing)
  member Bayes    -- ``data/board_readings/member_bayes_history``
  D0              -- ``data/board_readings/d0_history``
  MOS             -- the station board's composite series, recomputed per closed
                     day with the current coefficients; only days after their fit
                     window count (out of sample)
  WRF             -- the ZUUU nowcast record (``wrf_readings``)
  TAF TX          -- the official TAFs captured for the station-day (charter 263's forward capture
                     table, the rolling revision inbox after it stopped on 10-27): the first TAF
                     carrying a TX for the day, and the newest one out by local 13:00 when that is a
                     later TAF (user 2026-11-02 「改为taf的tx真值命中率（首次和更新（如有）」); judged
                     once, when the day settles, so the inbox rolling over loses nothing

Writes ``data/board_readings/estimate_hit.json``.  A settled station-day's verdicts
are cached under ``data/runtime/estimate_hit`` and recomputed only when its truth
changes.  Display only; enters no layer.
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
READING_NAME = "estimate_hit"
CACHE_PATH = ROOT / "data" / "runtime" / "estimate_hit" / "verdicts_v1.json"
READINGS_DIR = ROOT / "data" / "board_readings"
F_LEDGER = ROOT / "data" / "runtime" / "listing_forecast_f_mos_ledger_v1.jsonl"
TAF_INBOX = ROOT / "data" / "bayes_basket_target_taf_revision_inbox.json"
COMPOSITE_FIT = ROOT / "data" / "runtime" / "tmax_composite" / "tmax_composite_shift_v1.json"
#: The board's estimates started on different days; the window starts with D0.
SINCE = date(2026, 10, 15)
SNAPSHOT_HOURS = (10, 13, 16)
CURVE_HOURS = tuple(range(24))
METHODS = ("P0", "P1", "market", "F", "member_bayes", "D0", "MOS", "WRF")
#: TAF TX: the update is the newest TAF out by this local hour on the event day.
TAF_UPDATE_HOUR = 13
MARKET_WINDOW_MIN = 30
#: MOS is recomputed (Open-Meteo, satellite) only for this many closed days per pass,
#: and only while the pass is inside its time budget: each costs ~15-20 s of network,
#: and 15 of them overran the sweep's 240 s kill from 2026-10-23 05:23Z on, so no
#: pass wrote anything.
MOS_DAYS_PER_PASS = 15
MOS_BUDGET_SEC = 150.0
#: The Open-Meteo quota lane the MOS recompute is charged to (open_meteo_quota's
#: default 500 a day); the live board keeps its own.
QUOTA_LANE = "estimate_hit_mos"


def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _argmax(vec: dict[str, float] | None) -> str | None:
    if not vec:
        return None
    return max(vec.items(), key=lambda kv: float(kv[1] or 0.0))[0]


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


def _rungs(conn: sqlite3.Connection, station: str, day: str):
    from .listing_forecast_f import _rung_edges, station_day_rungs  # noqa: PLC0415

    rungs = station_day_rungs(conn, station_id=station, event_date=date.fromisoformat(day), prefer_registry=False)
    if not rungs:
        return None
    edges = _rung_edges(rungs)
    return (rungs, edges[0]) if edges else None


def _label_of(ladder, value: float | None) -> str | None:
    from .listing_forecast_f import _rung_of  # noqa: PLC0415

    if ladder is None or value is None:
        return None
    rungs, edges = ladder
    return rungs[_rung_of(edges, float(value))][0]


def _history(name: str, day: str) -> dict[str, list[dict[str, Any]]]:
    """station -> that day's history rows (oldest first) from a board-readings history file."""

    out: dict[str, list[dict[str, Any]]] = {}
    try:
        with (READINGS_DIR / f"{name}_history" / f"{day}.jsonl").open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("event_date", day) == day and r.get("station"):
                    out.setdefault(str(r["station"]), []).append(r)
    except OSError:
        pass
    for rows in out.values():
        rows.sort(key=lambda r: str(r.get("as_of_utc") or ""))
    return out


def _latest(rows: list[dict[str, Any]], at: str, key: str) -> Any:
    hit = None
    for r in rows:
        if str(r.get("as_of_utc") or "") <= at:
            hit = r.get(key)
    return hit


def _f_ledger() -> dict[tuple[str, str], float]:
    out: dict[tuple[str, str], float] = {}
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
            out[(str(st), str(r["event_date"]))] = float(r["f_c_at_record"])
    return out


def _wrf_records() -> dict[str, list[dict[str, Any]]]:
    """day -> the frozen issue and its re-runs on newer scans, by ready time."""

    from . import wrf_readings  # noqa: PLC0415

    try:
        return wrf_readings.nowcast_issues()
    except Exception:  # noqa: BLE001 -- the WRF volume is optional
        return {}


def _taf_txs(conn: sqlite3.Connection) -> dict[tuple[str, str], list[tuple[str, str, float]]]:
    """(station, event day) -> its TAFs with a TX, (available, issued, TX °C) by availability."""

    seen: dict[tuple[str, str, str], tuple[str, float]] = {}
    for st, day, issued, avail, tx in conn.execute(
            "SELECT station_id, event_date, issued_at_utc, provider_available_at_utc, tx_c FROM calibration_forward_taf_capture"
            " WHERE event_date>=? AND tx_c IS NOT NULL AND source_class='official_forward_native_receipt'", (SINCE.isoformat(),)):
        k = (st, day, issued)
        if k not in seen or str(avail) < seen[k][0]:
            seen[k] = (str(avail), float(tx))
    try:
        captures = json.loads(TAF_INBOX.read_text(encoding="utf-8")).get("captures") or []
    except (OSError, ValueError, AttributeError):
        captures = []
    for c in captures:
        if not isinstance(c, dict) or c.get("source_class") != "official_forward_native_receipt":
            continue
        rev, tx = c.get("forecast_revision") or {}, (c.get("taf_fields") or {}).get("tx_c")
        st, day, issued, avail = c.get("station_id"), c.get("event_date"), rev.get("issued_at_utc"), c.get("provider_available_at_utc")
        if tx is None or not (st and day and issued and avail) or str(day) < SINCE.isoformat():
            continue
        k = (str(st), str(day), str(issued))
        if k not in seen or str(avail) < seen[k][0]:
            seen[k] = (str(avail), float(tx))
    out: dict[tuple[str, str], list[tuple[str, str, float]]] = {}
    for (st, day, issued), (avail, tx) in seen.items():
        out.setdefault((st, day), []).append((avail, issued, tx))
    for rows in out.values():
        rows.sort()
    return out


def taf_verdict(ladder, truth_label: str | None, txs: list[tuple[str, str, float]], day: str, zone: ZoneInfo) -> dict[str, Any] | None:
    """The day's first TAF TX and, when a later TAF is out by local 13:00, the newest such, each
    against the truth's bucket."""

    if not txs or truth_label is None:
        return None
    cut = _iso(datetime.combine(date.fromisoformat(day), datetime.min.time(), zone) + timedelta(hours=TAF_UPDATE_HOUR))
    first = txs[0]
    out: dict[str, Any] = {"first": {"tx_c": first[2], "issued_utc": first[1], "hit": _label_of(ladder, first[2]) == truth_label}}
    by13 = [r for r in txs if r[0].replace("+00:00", "Z") <= cut]
    if by13 and by13[-1] != first:
        u = by13[-1]
        out["update"] = {"tx_c": u[2], "issued_utc": u[1], "hit": _label_of(ladder, u[2]) == truth_label}
    return out


def _mos_series(conn: sqlite3.Connection, station: str, day: str, zone: ZoneInfo) -> list[tuple[int, float]] | None:
    """(local hour, estimated maximum °C) as the board's MOS card had it each hour."""

    from . import station_day_board as B  # noqa: PLC0415

    metar = B._metar(station, day, zone)
    # Charged to this reading's own lane, never the live board's.
    token = B.quota_lane.set(QUOTA_LANE)
    try:
        sky = B._hourly_vs_forecast(conn, station, day, zone, metar, extras=False)
    finally:
        B.quota_lane.reset(token)
    cx = (sky or {}).get("composite_tmax_shift") or {}
    if cx.get("state") != "ok" or cx.get("forecast_tmax_d1_c") is None:
        return None
    base = float(cx["forecast_tmax_d1_c"])
    return [(int(p["hour"]), base + float(p["total_c"])) for p in cx.get("series") or [] if p.get("total_c") is not None]


def _mos_first_day() -> date | None:
    """The first day after the MOS coefficients' fit window (out of sample)."""

    try:
        fit = json.loads(COMPOSITE_FIT.read_text(encoding="utf-8"))
        return date.fromisoformat(str(fit["window"][1])) + timedelta(days=1)
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        return None


def _best_match_history() -> dict[tuple[str, str], list[tuple[str, float]]]:
    """(station, day) -> [(fetched_at_utc, latest run's day maximum)], oldest first, from the
    hourly pulls (best_match_hourly)."""

    from .best_match_hourly import HISTORY_PATH  # noqa: PLC0415

    out: dict[tuple[str, str], list[tuple[str, float]]] = {}
    try:
        lines = HISTORY_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("tmax_latest_c") is not None and r.get("fetched_at_utc"):
            out.setdefault((str(r["station_id"]), str(r["event_date"])), []).append(
                (str(r["fetched_at_utc"]), float(r["tmax_latest_c"])))
    for v in out.values():
        v.sort()
    return out


def picks_at(conn: sqlite3.Connection, station: str, day: str, when: datetime, ladder, *,
             f: float | None, wrf, d0, mb, mos: list[tuple[int, float]] | None, bm=None) -> dict[str, str | None]:
    """Each method's bucket as of ``when`` (a local time on the event day)."""

    at = _iso(when)
    h = when.hour
    picks: dict[str, str | None] = {}
    for stage in ("P0", "P1"):
        row = conn.execute(
            "SELECT probabilities_json FROM basket_probability_envelope WHERE station_id=? AND event_date=?"
            " AND state='COMMITTED' AND +stage=? AND decision_at_utc<=? ORDER BY decision_at_utc DESC LIMIT 1",
            (station, day, stage, at)).fetchone()
        picks[stage] = _argmax(json.loads(row[0])) if row and row[0] else None
    lo = _iso(when - timedelta(minutes=MARKET_WINDOW_MIN))
    row = conn.execute(
        "SELECT buckets_json FROM basket_official_ws_snapshot WHERE station_id=? AND event_date=? AND state='COMMITTED'"
        " AND observed_at<=? AND observed_at>=? ORDER BY observed_at DESC LIMIT 1", (station, day, at, lo)).fetchone()
    picks["market"] = None
    if row and row[0]:
        from .basket_kernel.valuation_v1 import market_reference_v1  # noqa: PLC0415

        ref = market_reference_v1(json.loads(row[0]))
        if ref.get("state") == "complete":
            picks["market"] = _argmax({str(k): float(v) for k, v in ref["vector"].items()})
    picks["F"] = _label_of(ladder, f)
    picks["member_bayes"] = _latest(mb.get(station, []), at, "post_mode")
    picks["D0"] = _latest(d0.get(station, []), at, "mode")
    picks["MOS"] = None
    if mos:
        past = [v for hour, v in mos if hour <= h]
        picks["MOS"] = _label_of(ladder, past[-1]) if past else None
    picks["WRF"] = None
    # The newest issue out by then (the frozen one, or a re-run on a newer scan).
    out = [r for r in (wrf.get(day) or [] if station == "ZUUU" else [])
           if r.get("ready_utc") and str(r["ready_utc"]) <= at and r.get("wrf_sat_tmax_with_obs_c") is not None]
    if out:
        picks["WRF"] = _label_of(ladder, out[-1]["wrf_sat_tmax_with_obs_c"])
    # best_match as of the hour: the newest hourly pull fetched by then (plotted, not a voter).
    got = [v for t, v in (bm or {}).get((station, day), []) if t.replace("+00:00", "Z") <= at]
    picks["best_match"] = _label_of(ladder, got[-1]) if got else None
    return picks


def verdicts(conn: sqlite3.Connection, station: str, day: str, zone: ZoneInfo, truth_c: float,
             *, f_ledger, wrf, d0, mb, mos_ok: bool, bm=None, tafs=None) -> dict[str, Any] | None:
    """One settled station-day: the truth's bucket and each method's bucket per snapshot."""

    ladder = _rungs(conn, station, day)
    if ladder is None:
        return None
    truth_label = _label_of(ladder, truth_c)
    local0 = datetime.combine(date.fromisoformat(day), datetime.min.time(), zone)
    mos = None
    if mos_ok:
        try:
            mos = _mos_series(conn, station, day, zone)
        except Exception:  # noqa: BLE001 -- MOS needs network data; a miss is no MOS that day
            mos = None
    # Every local hour of the event day for the convergence timeline (user 2026-10-23
    # 「大概什么时候温度开始靠近并收敛，趋于真值，给我一个信心指标」); the hit cards'
    # snapshots are three of them.
    curve = {str(h): picks_at(conn, station, day, local0 + timedelta(hours=h), ladder,
                              f=f_ledger.get((station, day)), wrf=wrf, d0=d0, mb=mb, mos=mos, bm=bm) for h in CURVE_HOURS}
    return {"truth_c": truth_c, "truth": truth_label, "snaps": {str(h): curve[str(h)] for h in SNAPSHOT_HOURS},
            "curve": curve, "rank": {r[0]: i for i, r in enumerate(ladder[0])},
            "taf": taf_verdict(ladder, truth_label, (tafs or {}).get((station, day)) or [], day, zone)}


def _summarize(cache: dict[str, Any]) -> dict[str, Any]:
    def blank():
        return {m: {"n": 0, "hits": 0, "pair_n": 0, "pair_hits": 0, "pair_market_hits": 0} for m in METHODS}

    fleet = {str(h): blank() for h in SNAPSHOT_HOURS}
    stations: dict[str, Any] = {}
    taf = {"fleet": {"first": {"n": 0, "hits": 0}, "update": {"n": 0, "hits": 0}}, "stations": {}}
    days: set[str] = set()
    for key, v in cache.items():
        if not v or not v.get("truth"):
            continue
        st, day = key.split("|")
        days.add(day)
        per = stations.setdefault(st, {str(h): blank() for h in SNAPSHOT_HOURS})
        for k, x in (v.get("taf") or {}).items():
            own = taf["stations"].setdefault(st, {"first": {"n": 0, "hits": 0}, "update": {"n": 0, "hits": 0}})
            for agg in (taf["fleet"][k], own[k]):
                agg["n"] += 1
                agg["hits"] += int(bool(x.get("hit")))
        for h, picks in (v.get("snaps") or {}).items():
            mk = picks.get("market")
            for m in METHODS:
                p = picks.get(m)
                if p is None:
                    continue
                hit = p == v["truth"]
                for agg in (fleet[h][m], per[h][m]):
                    agg["n"] += 1
                    agg["hits"] += int(hit)
                    if m != "market" and mk is not None:
                        agg["pair_n"] += 1
                        agg["pair_hits"] += int(hit)
                        agg["pair_market_hits"] += int(mk == v["truth"])
    return {"fleet": fleet, "stations": stations, "days": sorted(days), "taf": taf}


#: The methods the convergence timeline follows and that vote in the confidence score.
CONVERGE_METHODS = ("F", "member_bayes", "D0", "MOS", "P1", "market")
#: Drawn on the timeline beside the voters, voting nowhere: best_match as of each hour, from its
#: hourly pulls (user 2026-10-27 「收敛时间线」 also gets best_match; pulls from 2026-10-27 only).
LINE_METHODS = CONVERGE_METHODS + ("best_match",)
BEST_MATCH_FROM = "2026-10-27"
#: Agreement among the voters present: all on one bucket, two thirds, half, less.
AGREE_BINS = (("all", 1.0), ("two_thirds", 2 / 3), ("half", 0.5), ("split", 0.0))
#: Pseudo-days pulling a thin (hour, agreement) cell toward the hour's own rate.
CONF_PRIOR_DAYS = 5.0
MIN_VOTERS = 3


def agreement(picks: dict[str, str | None]) -> dict[str, Any] | None:
    """The bucket most voters point to, how many agree, and the agreement bin."""

    ps = [picks.get(m) for m in CONVERGE_METHODS if picks.get(m) is not None]
    if len(ps) < MIN_VOTERS:
        return None
    # Ties go to the market's bucket when it is one of them, else the first voter's.
    best = max(ps.count(p) for p in ps)
    tied = [p for p in dict.fromkeys(ps) if ps.count(p) == best]
    top = picks.get("market") if picks.get("market") in tied else tied[0]
    share = best / len(ps)
    return {"bucket": top, "agree": best, "voters": len(ps),
            "bin": next(name for name, floor in AGREE_BINS if share >= floor - 1e-9)}


def _convergence(cache: dict[str, Any]) -> dict[str, Any]:
    """Per method x local hour: truth hit rate and bucket error; per method: the lock
    hour (from which it stays on the truth's bucket); per hour x agreement bin: how
    often the voters' bucket was the truth's -- the confidence table."""

    per: dict[str, dict[int, list[float]]] = {m: {} for m in LINE_METHODS}
    locks: dict[str, list[int]] = {m: [] for m in LINE_METHODS}
    conf: dict[int, dict[str, list[int]]] = {}
    for v in cache.values():
        if not v or not v.get("truth") or not v.get("curve"):
            continue
        truth, rank = v["truth"], v.get("rank") or {}
        for m in LINE_METHODS:
            seq = [(h, v["curve"][str(h)].get(m)) for h in CURVE_HOURS if v["curve"][str(h)].get(m) is not None]
            for h, p in seq:
                c = per[m].setdefault(h, [0, 0, 0.0])
                c[0] += 1
                c[1] += int(p == truth)
                c[2] += abs(rank[p] - rank[truth]) if p in rank and truth in rank else 0.0
            if seq:
                lock = 24
                for h, p in reversed(seq):
                    if p != truth:
                        break
                    lock = h
                locks[m].append(lock)
        for h in CURVE_HOURS:
            a = agreement(v["curve"][str(h)])
            if a:
                c = conf.setdefault(h, {}).setdefault(a["bin"], [0, 0])
                c[0] += 1
                c[1] += int(a["bucket"] == truth)
    table = {}
    for h, bins in sorted(conf.items()):
        n_h = sum(c[0] for c in bins.values())
        p_h = sum(c[1] for c in bins.values()) / n_h
        table[str(h)] = {"n": n_h, "hit": round(p_h, 3), "bins": {
            b: {"n": c[0], "hits": c[1], "score": round(100 * (c[1] + CONF_PRIOR_DAYS * p_h) / (c[0] + CONF_PRIOR_DAYS))}
            for b, c in bins.items()}}
    return {
        "methods": list(LINE_METHODS), "voters": list(CONVERGE_METHODS),
        "by_hour": {m: {str(h): {"n": c[0], "hit": round(c[1] / c[0], 3), "bucket_err": round(c[2] / c[0], 2)}
                        for h, c in sorted(per[m].items())} for m in LINE_METHODS},
        "lock": {m: {"n": len(x), "median_hour": sorted(x)[len(x) // 2] if x else None,
                     "never": sum(1 for y in x if y == 24)} for m, x in locks.items()},
        "confidence": table,
    }


def confidence_score(table: dict[str, Any] | None, hour: int, picks: dict[str, str | None]) -> dict[str, Any] | None:
    """The 0..100 score for one station-day's picks at a local hour: how often, on the
    settled days, the voters' bucket at this hour and this agreement was the truth's."""

    a = agreement(picks)
    cell = ((table or {}).get(str(hour)) or {})
    if a is None or not cell:
        return None
    b = (cell.get("bins") or {}).get(a["bin"])
    score = b["score"] if b else round(100 * cell["hit"])
    return {**a, "hour": hour, "score": score, "n": (b or {}).get("n", 0), "hour_hit": cell["hit"]}


def compute(conn: sqlite3.Connection, *, now: datetime | None = None, cache: dict[str, Any] | None = None) -> dict[str, Any]:
    started = time.monotonic()
    now = now or datetime.now(timezone.utc)
    cache = dict(cache or {})
    zones = _zones(conn)
    truths = _truths(conn, SINCE)
    f_ledger, wrf, bm, tafs = _f_ledger(), _wrf_records(), _best_match_history(), _taf_txs(conn)
    mos_from = _mos_first_day()
    mos_budget = MOS_DAYS_PER_PASS
    by_day: dict[str, list[str]] = {}
    for (st, day) in truths:
        if st in zones:
            by_day.setdefault(day, []).append(st)
    for day in sorted(by_day):
        d0, mb = _history("d0", day), _history("member_bayes", day)
        for st in sorted(by_day[day]):
            zone = zones[st]
            if datetime.combine(date.fromisoformat(day) + timedelta(days=1), datetime.min.time(), zone) > now:
                continue  # the local day is not over yet
            key, t = f"{st}|{day}", truths[(st, day)]
            old = cache.get(key)
            if (old and old.get("truth_c") == t and "curve" in old and (old.get("mos_done") or mos_from is None or date.fromisoformat(day) < mos_from)
                    and (old.get("bm_done") or day < BEST_MATCH_FROM)):
                if "taf" not in old:   # cached before the TAF card: judged on its own
                    ladder = _rungs(conn, st, day)
                    old["taf"] = taf_verdict(ladder, old.get("truth"), tafs.get((st, day)) or [], day, zone) if ladder else None
                continue
            mos_ok = (mos_from is not None and date.fromisoformat(day) >= mos_from and mos_budget > 0
                      and time.monotonic() - started < MOS_BUDGET_SEC)
            if mos_ok:
                mos_budget -= 1
            v = verdicts(conn, st, day, zone, t, f_ledger=f_ledger, wrf=wrf, d0=d0, mb=mb, mos_ok=mos_ok, bm=bm, tafs=tafs)
            if v is not None:
                v["mos_done"] = mos_ok
                v["bm_done"] = (st, day) in bm
            cache[key] = v
    out = _summarize(cache)
    out["convergence"] = _convergence(cache)
    return {"state": "ok", "as_of_utc": _iso(now), "since": SINCE.isoformat(), "snapshot_hours_local": list(SNAPSHOT_HOURS),
            "methods": list(METHODS), "mos_from": mos_from.isoformat() if mos_from else None, **out}, cache


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
    f = value.get("fleet", {}).get("13", {})
    print(json.dumps({"state": value.get("state"), "days": len(value.get("days") or []),
                      "at13": {m: f"{x['hits']}/{x['n']}" for m, x in f.items()}}, ensure_ascii=False))


if __name__ == "__main__":
    main()
