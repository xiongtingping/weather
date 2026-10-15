"""Forward read of the frozen mode-keeping direction arm (FROZEN_ARM.json).

  python3 forward.py fetch --from 2026-10-07 --to 2026-10-28 [--stations ...]
      Open-Meteo previous_day1 (temperature, cloud, shortwave, precipitation)
      and the satellite-relay shortwave for the stations that used a relay in
      training, charged to the production quota ledger (lane "research").
      GOES DSRF for the Americas stations comes from
      ../sat_cloud_obs_20261008/goes_extract.py --products ABI-L2-DSRF (run it
      with .venv-sat for the same dates first).
  python3 forward.py build --from ... --to ...
      Served hourly P1 (newest committed P1 decided in each local hour 07-19),
      METAR state at the decision, truth, market vector -> forward_rows.jsonl.gz.
  python3 forward.py score
      Applies the frozen arm unchanged; refuses before 2026-10-29 (the
      registered reread) unless --allow-early.

Read-only on SSOT/archive; every source keeps the training definition.
"""
from __future__ import annotations

import argparse
import gzip
import json
import math
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
import ssl

import certifi
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "docs/research/benchmark_account_0x8fbd_20261008/scripts"))
import om_research_quota as omq  # noqa: E402
from bayes_basket_target import station_registry  # noqa: E402
from bayes_basket_target.basket_kernel.valuation_v1 import market_reference_v1  # noqa: E402
from bayes_basket_target.metar_path_state import parse_metar_physical_state  # noqa: E402
from bayes_basket_target.proper_bet_lane import market_mode_bucket  # noqa: E402

SSOT = ROOT / "data/bayes_basket_target_ssot.db"
ARCHIVE = ROOT / "data/archive.db"
INPUTS = ROOT / "docs/research/p1_forward_physics_20261015/inputs.db"
GOES_DIR = ROOT / "docs/research/sat_cloud_obs_20261008/goes"
CACHE = HERE / "forward_cache"
REREAD = date(2026, 10, 29)
MAX_AGE = timedelta(minutes=90)
CTX = ssl.create_default_context(cafile=certifi.where())
PREV_VARS = "temperature_2m_previous_day1,cloud_cover_previous_day1,shortwave_radiation_previous_day1,precipitation_previous_day1"


def utc(s):
    return datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone(timezone.utc)


def stamp(t):
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


def training_sources():
    """The radiation source each station used in training (pinned, never switched)."""
    con = sqlite3.connect(INPUTS)
    return dict(con.execute("select station_id, min(source) from rad group by station_id"))


def timezones():
    con = sqlite3.connect(INPUTS)
    return dict(con.execute("select station_id, settlement_timezone from tz"))


def om_get(url, label):
    CACHE.mkdir(exist_ok=True)
    path = CACHE / f"{label}.json.gz"
    if path.exists():
        return json.load(gzip.open(path, "rt"))["payload"]
    omq.charge(1)
    req = urllib.request.Request(url, headers={"User-Agent": "weather-forward-research"})
    with urllib.request.urlopen(req, timeout=30, context=CTX) as r:
        raw = r.read(4 * 1024 * 1024 + 1)
    if len(raw) > 4 * 1024 * 1024:
        raise RuntimeError("response_cap")
    payload = json.loads(raw)
    if omq.on_reply(payload):
        raise RuntimeError(f"open_meteo_refused:{payload.get('reason')}")
    json.dump({"url": url, "received_at": datetime.now(timezone.utc).isoformat(), "source_class": "aggregator_republisher", "payload": payload},
              gzip.open(path, "wt"))
    time.sleep(1.0)
    return payload


def cmd_fetch(a):
    tzs = timezones()
    src = training_sources()
    stations = a.stations or sorted(tzs)
    for st in stations:
        c = station_registry.station_coordinates(st)
        q = dict(latitude=c["latitude"], longitude=c["longitude"], timezone=tzs[st], start_date=a.date_from, end_date=a.date_to, hourly=PREV_VARS)
        om_get("https://previous-runs-api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(q), f"{st}_previous_{a.date_from}_{a.date_to}")
        s = src.get(st, "")
        if s.startswith("OPENMETEO_"):
            model = s[len("OPENMETEO_"):]
            q = dict(latitude=c["latitude"], longitude=c["longitude"], timezone=tzs[st], start_date=a.date_from, end_date=a.date_to,
                     hourly="shortwave_radiation", models=model)
            om_get("https://satellite-api.open-meteo.com/v1/archive?" + urllib.parse.urlencode(q), f"{st}_{model}_{a.date_from}_{a.date_to}")
        print(st, "ok", flush=True)


def load_forecast(st, d_from, d_to):
    p = json.load(gzip.open(CACHE / f"{st}_previous_{d_from}_{d_to}.json.gz", "rt"))["payload"]
    h = p["hourly"]
    out = defaultdict(lambda: [None] * 24)
    for i, tm in enumerate(h["time"]):
        day, hh = tm[:10], int(tm[11:13])
        out[day][hh] = {"t": h["temperature_2m_previous_day1"][i], "cloud": h["cloud_cover_previous_day1"][i],
                        "sw": h["shortwave_radiation_previous_day1"][i], "pr": h["precipitation_previous_day1"][i]}
    return out


def load_radiation(st, src, d_from, d_to):
    rad = {}
    if src.startswith("GOES_"):
        bucket = src[len("GOES_"):]
        for f in sorted((GOES_DIR / bucket / "ABI-L2-DSRF").glob("*.jsonl.gz")):
            if not (d_from <= f.name[:10] <= (date.fromisoformat(d_to) + timedelta(days=1)).isoformat()):
                continue
            for line in gzip.open(f, "rt"):
                r = json.loads(line)
                if r["station"] == st and r.get("DSR_box_mean") is not None:
                    rad[utc(r["scan_start"]).strftime("%Y-%m-%dT%H:00:00Z")] = float(r["DSR_box_mean"])
        return rad, timedelta(minutes=20)
    model = src[len("OPENMETEO_"):]
    p = json.load(gzip.open(CACHE / f"{st}_{model}_{d_from}_{d_to}.json.gz", "rt"))["payload"]
    off = timedelta(seconds=int(p["utc_offset_seconds"]))
    for tm, v in zip(p["hourly"]["time"], p["hourly"]["shortwave_radiation"]):
        if v is not None:
            t = datetime.fromisoformat(tm).replace(tzinfo=timezone(off)).astimezone(timezone.utc)
            rad[t.strftime("%Y-%m-%dT%H:00:00Z")] = float(v)
    return rad, timedelta(minutes=75)


def cmd_build(a):
    tzs = timezones()
    src = training_sources()
    ssot = sqlite3.connect(f"file:{SSOT}?mode=ro", uri=True, timeout=5)
    arch = sqlite3.connect(f"file:{ARCHIVE}?mode=ro", uri=True, timeout=5)
    truth = {}
    for st, d, t, rev in ssot.execute(
        "select station_id, local_date, official_tmax_c, revision_no from calibration_station_daily_high_truth"
        " where source_lane in ('AWC','SETTLEMENT_SOURCE') and qc_status='ACCEPTED' and local_date between ? and ? order by revision_no",
        (a.date_from, a.date_to)):
        truth[(st, d)] = float(t)
    out = gzip.open(HERE / "forward_rows.jsonl.gz", "wt")
    stats = defaultdict(int)
    days = [(date.fromisoformat(a.date_from) + timedelta(days=i)).isoformat()
            for i in range((date.fromisoformat(a.date_to) - date.fromisoformat(a.date_from)).days + 1)]
    for st in (a.stations or sorted(tzs)):
        try:
            fcs = load_forecast(st, a.date_from, a.date_to)
            rad, lag = load_radiation(st, src.get(st, ""), a.date_from, a.date_to)
        except FileNotFoundError as exc:
            stats["missing_source:" + st] += 1
            print(st, "missing source", exc)
            continue
        zone = ZoneInfo(tzs[st])
        for d in days:
            if (st, d) not in truth:
                stats["no_truth"] += 1
                continue
            fc = fcs.get(d)
            if not fc or any(x is None for x in fc):
                stats["no_forecast"] += 1
                continue
            for hour in range(7, 20):
                lo = datetime.fromisoformat(d).replace(hour=hour, tzinfo=zone).astimezone(timezone.utc)
                hi = lo + timedelta(hours=1)
                row = ssot.execute(
                    "select decision_at_utc, probabilities_json, lineage_json, posterior_version from basket_probability_envelope"
                    " where station_id=? and event_date=? and state='COMMITTED' and stage='P1' and decision_at_utc>=? and decision_at_utc<?"
                    " order by decision_at_utc desc limit 1", (st, d, stamp(lo), stamp(hi))).fetchone()
                if row is None:
                    stats["no_p1_in_hour"] += 1
                    continue
                dec = utc(row[0])
                lin = json.loads(row[2])
                intervals = json.loads(lin.get("bucket_intervals_c_json") or "null")
                if not intervals:
                    stats["no_intervals"] += 1
                    continue
                lh = (dec.astimezone(zone).hour + dec.astimezone(zone).minute / 60.0)
                ob = arch.execute(
                    "select reported_temperature_c, raw_text from reported_trajectory_observations where station_id=? and source_lane='awc'"
                    " and valid_at<=? and canonical_committed_at<=? order by valid_at desc limit 1", (st, stamp(dec), stamp(dec))).fetchone()
                temp_now = cloud_now = precip_now = None
                if ob is not None:
                    temp_now = float(ob[0])
                    try:
                        ps = parse_metar_physical_state(ob[1], expected_temperature_c=temp_now)
                        cloud_now, precip_now = ps.cloud_cover_fraction, ps.precipitation_present
                    except Exception:
                        stats["metar_parse"] += 1
                i = min(23, int(lh))
                fc_t = fc[i]["t"] + (fc[min(23, i + 1)]["t"] - fc[i]["t"]) * (lh - i)
                dT = temp_now - fc_t if temp_now is not None else None
                dC = cloud_now - fc[i]["cloud"] / 100.0 if (cloud_now is not None and fc[i]["cloud"] is not None) else None
                dR = (float(bool(precip_now)) - float((fc[i]["pr"] or 0.0) > 0.1)) if precip_now is not None else None
                offset_h = round((lh - (dec.hour + dec.minute / 60.0)) % 24)
                avail_end = (dec - lag).replace(minute=0, second=0, microsecond=0)
                pairs = []
                hs = avail_end - timedelta(hours=1)
                for _ in range(6):
                    v = rad.get(hs.strftime("%Y-%m-%dT%H:00:00Z"))
                    fsw = fc[(hs.hour + offset_h) % 24]["sw"]
                    if v is not None and fsw is not None and fsw > 50.0:
                        pairs.append((v, fsw))
                        if len(pairs) == 2:
                            break
                    hs -= timedelta(hours=1)
                dSW = None
                if pairs:
                    m = sum(x for x, _ in pairs) / len(pairs)
                    f = sum(y for _, y in pairs) / len(pairs)
                    dSW = (m - f) / max(f, 200.0)
                snap = ssot.execute(
                    "SELECT buckets_json, observed_at FROM basket_official_ws_snapshot WHERE station_id=? AND event_date=?"
                    " AND state='COMMITTED' AND observed_at<=? AND observed_at>=? ORDER BY observed_at DESC LIMIT 1",
                    (st, d, stamp(dec), stamp(dec - MAX_AGE))).fetchone()
                if snap is None:
                    stats["no_snapshot"] += 1
                    continue
                buckets = json.loads(snap[0] or "[]")
                ref = market_reference_v1(buckets)
                if ref.get("state") != "complete":
                    stats["market_incomplete"] += 1
                    continue
                q = {str(k): float(v) for k, v in ref["vector"].items()}
                ords = {str(b["bucket"]): int(b["ordinal"]) for b in buckets if b.get("ordinal") is not None}
                order = sorted(q, key=lambda b: ords.get(b, 0))
                out.write(json.dumps({
                    "station": st, "day": d, "decided_at": row[0], "local_hour": lh, "p1": json.loads(row[1]),
                    "intervals": intervals, "truth": truth[(st, d)], "posterior_version": row[3],
                    "unit": "F" if "°F" in next(iter(intervals)) else "C", "rad_official": int(lag == timedelta(minutes=20)),
                    "dT": dT, "dSW": dSW, "dCloud": dC, "dRain": dR,
                    "market": {"0": {"q": q, "mode": market_mode_bucket(q, order), "observed_at": snap[1],
                                     "age_min": (dec - utc(snap[1])).total_seconds() / 60.0}},
                }) + "\n")
                stats["rows"] += 1
        print(st, dict(stats), flush=True)
    out.close()
    print("built", dict(stats))


def cmd_score(a):
    if date.today() < REREAD and not a.allow_early:
        raise SystemExit(f"registered reread is on or after {REREAD}; refusing to score earlier")
    import analyze as A
    import mode_keep as M
    frozen = json.load(open(HERE / "FROZEN_ARM.json"))
    A_rows = HERE / "rows.jsonl.gz"
    # analyze.load reads rows.jsonl.gz; point it at the forward rows without touching training data
    import shutil
    tmp = HERE / "_forward_view"
    tmp.mkdir(exist_ok=True)
    shutil.copy(HERE / "forward_rows.jsonl.gz", tmp / "rows.jsonl.gz")
    A.HERE = tmp
    recs = A.load()
    mu = np.array(frozen["standardise_mean"])
    sd = np.array(frozen["standardise_sd"])
    n = len(recs)
    st = np.array([r["station"] for r in recs])
    days = np.array([r["day"] for r in recs])
    base = np.zeros((n, 3))
    arm = np.zeros((n, 3))
    ctrl = np.zeros((n, 3))
    truth_side = np.zeros(n)
    dirs = np.zeros(n)
    for i, r in enumerate(recs):
        mi = r["mk"]["0"]["mode_i"]
        mode_c = r["c"][mi]
        tilt = r["mk"]["0"]["mean"] - mode_c
        x = np.array(r["x"] + [tilt], float)
        z = np.where(np.isnan((x - mu) / sd), 0.0, (x - mu) / sd)
        b = np.array(frozen["station_beta"].get(r["station"], {"beta": frozen["fleet_beta"]})["beta"])
        e = b[0] + z @ b[1:]
        d = 0.0 if abs(e) < 0.25 * r["w"] else float(np.sign(e))
        dc = 0.0 if abs(tilt) < 0.25 * r["w"] else float(np.sign(tilt))
        base[i] = A.score(r["p"], r)
        arm[i] = A.score(M.mode_keep(r["p"], mi, d, frozen["kappa"]), r)
        ctrl[i] = A.score(M.mode_keep(r["p"], mi, dc, frozen["kappa"]), r)
        truth_side[i] = np.sign(r["ti"] - mi)
        dirs[i] = d
    sd_key = np.array([r["station"] + "|" + r["day"] for r in recs])
    sd_ids, sd_idx = np.unique(sd_key, return_inverse=True)
    day_ids, day_idx = np.unique(days, return_inverse=True)
    sd_day = np.zeros(len(sd_ids), int)
    sd_day[sd_idx] = day_idx
    nd = len(day_ids)
    draws = np.random.default_rng(7).multinomial(nd, np.full(nd, 1.0 / nd), size=2000)
    res = {"rows": n, "station_days": len(sd_ids), "event_days": nd, "frozen_sha": frozen["training_rows_sha256"], "arms": {}}
    for name, sc in (("frozen_main", arm), ("market_tilt_control", ctrl)):
        dd = sc - base
        fleet = {m: A.boot_sd_day(dd[:, k], sd_idx, sd_day, nd, draws) for k, m in enumerate(("dlog", "dcrps", "dbrier"))}
        per = {s: A.boot_sd_day(dd[:, 0], sd_idx, sd_day, nd, draws, st == s) for s in sorted(set(st))}
        per = {s: v for s, v in per.items() if v}
        ok = {s: v["p"] for s, v in per.items() if v["station_days"] >= 10}
        passed = A.bh(ok)
        res["arms"][name] = {"fleet": fleet, "stations": {s: {**v, "bh_q10": s in passed} for s, v in per.items()},
                             "better_bh": sorted(s for s in passed if per[s]["point"] < 0), "worse_bh": sorted(s for s in passed if per[s]["point"] > 0)}
        print(name, {m: [round(fleet[m][k], 4) for k in ("point", "lo", "hi")] for m in fleet}, "BH+", res["arms"][name]["better_bh"], "BH-", res["arms"][name]["worse_bh"])
    live = (dirs != 0) & (truth_side != 0)
    res["direction_hit_off_mode"] = float((dirs[live] == truth_side[live]).mean()) if live.any() else None
    json.dump(res, open(HERE / "readings_forward.json", "w"), indent=1, default=float)
    shutil.rmtree(tmp)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("fetch", "build"):
        p = sub.add_parser(name)
        p.add_argument("--from", dest="date_from", required=True)
        p.add_argument("--to", dest="date_to", required=True)
        p.add_argument("--stations", nargs="*")
    s = sub.add_parser("score")
    s.add_argument("--allow-early", action="store_true")
    a = ap.parse_args()
    {"fetch": cmd_fetch, "build": cmd_build, "score": cmd_score}[a.cmd](a)


if __name__ == "__main__":
    main()
