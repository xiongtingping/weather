"""Round 29 part 2 (熊先生 10-11 「方向是：模型动量、taf方向、云雨辐射的差异性
（实况和预报的差异）导致的……实况和预测基本一致，那基本就在众数桶，如果实况和
预报有额外的差异，那就有偏置！」).

Every settled station-day 09-12..10-09, at local 10:00, 12:00 and 14:00 on the
event day, from local 06:00 to the instant:
  temp   observed METAR temperature minus the day-before forecast, same hours (C)
  cloud  forecast low cloud minus observed METAR cover (+ = clearer than forecast)
  rain   forecast wet hours minus observed wet hours (+ = drier than forecast)
  rad    satellite shortwave / forecast shortwave - 1 (+ = sunnier; 34 stations)
plus ECMWF momentum M at the instant and the newest TAF's TX against the market
mode's centre (station leave-one-date-out TX bias added).  All signed + = warmer.
Against the market at the instant (newest committed snapshot <= 30 min, the
market vector rule): (1) on truth off the market mode, how often each signal
calls the side, and its walk-forward log-loss increment over the market's own
lean; (2) the user's claim: truth on the market mode vs the market's own
q(mode), by how far observed and forecast drift apart; (3) the cluster's
same-day first takers: does their side follow the signals?
"""
import collections
import gzip
import json
import math
import random
import re
import sqlite3
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
from scipy.optimize import minimize

sys.path.insert(0, "/Users/xiong/weather")
from bayes_basket_target import direction_signal as ds  # noqa: E402
from bayes_basket_target import station_day_board as board  # noqa: E402
from bayes_basket_target.basket_kernel.valuation_v1 import market_reference_v1  # noqa: E402

B = Path(sys.argv[1])
random.seed(2929)
OF = json.load(open(B / "obsfc.json"))
conn = sqlite3.connect("file:/Users/xiong/weather/data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=60)
conn.execute("PRAGMA query_only=1")
labels = {(st, ed): b for st, ed, b in conn.execute(
    "SELECT station_id, event_date, settled_bucket FROM basket_station_day_settlement_label l WHERE event_date BETWEEN '2026-09-12' AND '2026-10-09'"
    " AND truth_revision_no=(SELECT max(truth_revision_no) FROM basket_station_day_settlement_label x"
    " WHERE x.station_id=l.station_id AND x.event_date=l.event_date)")}
truth_c = {(st, ed): float(t) for st, ed, t in conn.execute(
    "SELECT station_id, event_date, settled_tmax_c FROM basket_station_day_settlement_label l WHERE event_date BETWEEN '2026-09-01' AND '2026-10-09'"
    " AND truth_revision_no=(SELECT max(truth_revision_no) FROM basket_station_day_settlement_label x"
    " WHERE x.station_id=l.station_id AND x.event_date=l.event_date)")}
taf = collections.defaultdict(list)
for st, ed, rec, tx, txa, cc, pp in conn.execute(
        "SELECT station_id, event_date, received_at_utc, tx_c, tx_available, cloud_cover_fraction, precipitation_probability"
        " FROM calibration_forward_taf_capture WHERE event_date BETWEEN '2026-09-01' AND '2026-10-09'"):
    taf[(st, ed)].append((str(rec), tx if txa else None))
for v in taf.values():
    v.sort()
taf_bias = collections.defaultdict(dict)
for (st, ed), v in taf.items():
    txs = [tx for _r, tx in v if tx is not None]
    if txs and (st, ed) in truth_c:
        taf_bias[st][ed] = truth_c[(st, ed)] - txs[-1]


def centre_c(label):
    m = re.match(r"^(-?\d+)°([CF]) or (below|higher)$", label)
    if m:
        v = float(m.group(1)) + (-0.5 if m.group(3) == "below" else 0.5)
        return v if m.group(2) == "C" else (v - 32) * 5 / 9
    m = re.match(r"^(-?\d+)-(-?\d+)°F$", label)
    if m:
        return ((float(m.group(1)) + float(m.group(2))) / 2 - 32) * 5 / 9
    m = re.match(r"^(-?\d+)°([CF])$", label)
    if m:
        v = float(m.group(1))
        return v if m.group(2) == "C" else (v - 32) * 5 / 9
    return None


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def market_at(st, ed, at_iso):
    row = conn.execute(
        "SELECT buckets_json, observed_at FROM basket_official_ws_snapshot WHERE station_id=? AND event_date=? AND state='COMMITTED'"
        " AND observed_at<=? AND observed_at>=? ORDER BY observed_at DESC LIMIT 1",
        (st, ed, at_iso, iso(datetime.fromisoformat(at_iso.replace("Z", "+00:00")) - timedelta(minutes=30)))).fetchone()
    if not row:
        return None
    buckets = json.loads(row[0])
    ref = market_reference_v1(buckets)
    if ref.get("state") != "complete":
        return None
    q = {str(k): float(v) for k, v in ref["vector"].items()}
    ords = {str(b["bucket"]): int(b["ordinal"]) for b in buckets}
    return q, ords


def signals(st, ed, at_local, metar):
    """The observed-minus-forecast gaps from local 06:00 to ``at_local``."""
    f = OF.get(st)
    if not f or not f["fc"].get("time"):
        return {}
    idx = {t: i for i, t in enumerate(f["fc"]["time"])}
    H = at_local.hour
    temp_d, cloud_d, wet_o, wet_f = [], [], 0, 0
    for m in metar:
        hh = int(m["local"][:2])
        if not 6 <= hh < H:
            continue
        i = idx.get(f"{ed}T{hh:02d}:00")
        if i is None:
            continue
        fc_t = f["fc"]["temperature_2m"][i]
        # previous_day1 carries no low-cloud field; the total cover stands in.
        fc_low = (f["fc"].get("cloud_cover_low") or [None] * len(f["fc"]["time"]))[i]
        if fc_low is None:
            fc_low = f["fc"]["cloud_cover"][i]
        if m.get("temp_c") is not None and fc_t is not None:
            temp_d.append(m["temp_c"] - fc_t)
        if m.get("cloud_cover") is not None and fc_low is not None:
            cloud_d.append(fc_low / 100.0 - m["cloud_cover"])
    obs_wet_hours = {int(m["local"][:2]) for m in metar if m.get("precip") and 6 <= int(m["local"][:2]) < H}
    for hh in range(6, H):
        i = idx.get(f"{ed}T{hh:02d}:00")
        if i is not None and (f["fc"]["precipitation"][i] or 0.0) >= 0.1:
            wet_f += 1
    out = {}
    if temp_d:
        out["temp"] = statistics.mean(temp_d)
        out["temp_last"] = temp_d[-1]
    if cloud_d:
        out["cloud"] = statistics.mean(cloud_d)
    out["rain"] = float(wet_f - len(obs_wet_hours))
    sat = f.get("sat") or {}
    if sat.get("time"):
        sidx = {t: i for i, t in enumerate(sat["time"])}
        so = sf = 0.0
        for hh in range(7, H + 1):
            key = f"{ed}T{hh:02d}:00"
            i, j = sidx.get(key), idx.get(key)
            if i is None or j is None or sat["sw"][i] is None or f["fc"]["shortwave_radiation"][j] is None:
                continue
            so += sat["sw"][i]
            sf += f["fc"]["shortwave_radiation"][j]
        if sf > 100:
            out["rad"] = so / sf - 1.0
    return out


rows = []
for (st, ed), lab in sorted(labels.items()):
    if st not in OF:
        continue
    zone = ZoneInfo(OF[st]["tz"])
    try:
        metar = board._metar(st, ed, zone)
    except Exception:
        continue
    for H in (10, 12, 14):
        at_local = datetime.fromisoformat(ed).replace(hour=H, tzinfo=zone)
        at_iso = iso(at_local)
        mk = market_at(st, ed, at_iso)
        if not mk:
            continue
        q, ords = mk
        if lab not in ords or not all(b in ords for b in q):
            continue
        mode = max(q, key=lambda b: (q[b], -ords[b]))
        mo = ords[mode]
        lean = sum(v for b, v in q.items() if ords[b] > mo) - sum(v for b, v in q.items() if ords[b] < mo)
        up = sum(v for b, v in q.items() if ords[b] > mo)
        dn = sum(v for b, v in q.items() if ords[b] < mo)
        sig = signals(st, ed, at_local, metar)
        mom = ds.momentum(conn, station_id=st, event_date=ed, at_utc=at_iso)
        sig["M"] = mom["M"]
        tafs = [tx for rec, tx in taf.get((st, ed), []) if rec <= at_iso and tx is not None]
        mc = centre_c(mode)
        if tafs and mc is not None:
            b_other = [v for d, v in taf_bias[st].items() if d != ed]
            bias = statistics.mean(b_other) if len(b_other) >= 3 else 0.0
            sig["taf"] = tafs[-1] + bias - mc
        rows.append(dict(st=st, ed=ed, H=H, mode_ord=mo, truth_ord=ords[lab], q_mode=q[mode], up=up, dn=dn, lean=lean,
                         unit="F" if "°F" in mode else "C", sig=sig))
print(f"instances {len(rows)} ({len({(r['st'], r['ed']) for r in rows})} station-days)")
NAMES = ["temp", "temp_last", "cloud", "rain", "rad", "M", "taf"]
THRESH = {"temp": 0.5, "temp_last": 0.5, "cloud": 0.15, "rain": 0.5, "rad": 0.1, "M": 1.0, "taf": 0.5}

print("\n== (1a) truth off the market mode: how often each signal calls the side (|signal| >= threshold)")
for H in (10, 12, 14):
    off = [r for r in rows if r["H"] == H and r["truth_ord"] != r["mode_ord"]]
    base = sum(1 for r in off if (1 if r["lean"] > 0 else -1) == (1 if r["truth_ord"] > r["mode_ord"] else -1)) / max(1, len(off))
    cells = [f"market lean {base:.3f} (n={len(off)})"]
    for name in NAMES:
        sel = [r for r in off if r["sig"].get(name) is not None and abs(r["sig"][name]) >= THRESH[name]]
        if len(sel) < 30:
            cells.append(f"{name} n={len(sel)}")
            continue
        right = sum(1 for r in sel if (1 if r["sig"][name] > 0 else -1) == (1 if r["truth_ord"] > r["mode_ord"] else -1)) / len(sel)
        cells.append(f"{name} {right:.3f} (n={len(sel)})")
    print(f"   local {H}:00  " + " | ".join(cells))


def wf_increment(name_list, H=None):
    """Walk-forward by event day: logistic P(up | off mode) on logit(market up/(up+dn)) plus the signals;
    increment = log loss with the signals minus without, per instance, day-clustered interval."""
    off = [r for r in rows if r["truth_ord"] != r["mode_ord"] and (H is None or r["H"] == H)]
    off = [r for r in off if r["up"] + r["dn"] > 0]
    days = sorted({r["ed"] for r in off})

    def feats(r, with_sig):
        p = min(max(r["up"] / (r["up"] + r["dn"]), 1e-3), 1 - 1e-3)
        x = [1.0, math.log(p / (1 - p))]
        if with_sig:
            for n in name_list:
                v = r["sig"].get(n)
                x.append(0.0 if v is None else max(-3.0, min(3.0, v / THRESH[n])))
        return x

    def fit(train, with_sig):
        X = np.array([feats(r, with_sig) for r in train])
        y = np.array([1.0 if r["truth_ord"] > r["mode_ord"] else 0.0 for r in train])

        def nll(w):
            z = X @ w
            return float(np.sum(np.logaddexp(0, z) - y * z)) + 0.5 * float(np.sum(w[2:] ** 2))
        return minimize(nll, np.zeros(X.shape[1]), method="L-BFGS-B").x

    inc = collections.defaultdict(list)
    for k, day in enumerate(days):
        cut = (datetime.fromisoformat(day) - timedelta(days=2)).date().isoformat()
        train = [r for r in off if r["ed"] <= cut]
        if len({r["ed"] for r in train}) < 8:
            continue
        test = [r for r in off if r["ed"] == day]
        w0, w1 = fit(train, False), fit(train, True)
        for r in test:
            y = 1.0 if r["truth_ord"] > r["mode_ord"] else 0.0
            z0 = float(np.dot(w0, feats(r, False)))
            z1 = float(np.dot(w1, feats(r, True)))
            l0 = math.log1p(math.exp(z0)) - y * z0 if z0 < 30 else z0 - y * z0
            l1 = math.log1p(math.exp(z1)) - y * z1 if z1 < 30 else z1 - y * z1
            inc[day].append(l1 - l0)
    keys = list(inc)
    if not keys:
        return None
    allv = [v for k in keys for v in inc[k]]
    boot = []
    for _ in range(1000):
        s = [v for k in (random.choice(keys) for _ in keys) for v in inc[k]]
        boot.append(sum(s) / len(s))
    boot.sort()
    return statistics.mean(allv), boot[25], boot[974], len(allv), len(keys)


print("\n== (1b) walk-forward binary log loss increment over the market's own lean (truth off the mode; negative = better)")
for names in (["temp"], ["cloud"], ["rain"], ["rad"], ["M"], ["taf"], ["temp", "cloud", "rain", "rad"],
              ["temp", "cloud", "rain", "rad", "M", "taf"]):
    res = wf_increment(names)
    if res:
        m, lo, hi, n, nd = res
        print(f"   {'+'.join(names):34s} {m:+.4f} [{lo:+.4f}, {hi:+.4f}]  (n={n}, {nd} test days)")

print("\n== (2) the claim: when observed matches forecast, does the truth sit on the market mode more than the market says?")
for H in (10, 12, 14):
    sub = [r for r in rows if r["H"] == H and r["sig"].get("temp") is not None]
    for r in sub:
        parts = [abs(r["sig"]["temp"]) / 1.0]
        if r["sig"].get("cloud") is not None:
            parts.append(abs(r["sig"]["cloud"]) / 0.375)
        if r["sig"].get("rad") is not None:
            parts.append(abs(r["sig"]["rad"]) / 0.25)
        r["gap"] = statistics.mean(parts)
    sub.sort(key=lambda r: r["gap"])
    k = len(sub) // 3
    cells = []
    for name, part in (("observed ~ forecast", sub[:k]), ("middle", sub[k:2 * k]), ("observed far from forecast", sub[2 * k:])):
        on = sum(1 for r in part if r["truth_ord"] == r["mode_ord"]) / len(part)
        said = statistics.mean(r["q_mode"] for r in part)
        cells.append(f"{name}: truth on mode {on:.3f} vs market {said:.3f} (n={len(part)})")
    print(f"   local {H}:00  " + " | ".join(cells))

# ---------------------------------------------------------------- (3) the cluster's same-day first takers
E = json.load(gzip.open(B / "replicate_rows.json.gz", "rt"))
print("\n== (3) the cluster's same-day first takers off the mode (local hour >= 9): does their side follow the signals?")
agree = collections.defaultdict(list)
for e in E:
    if not e.get("is_d0") or e["chosen"]["ord"] == e["mode_ord"] or e["st"] not in OF:
        continue
    zone = ZoneInfo(OF[e["st"]]["tz"])
    at_local = datetime.fromtimestamp(int(e["ts"]), zone)
    if at_local.hour < 9:
        continue
    try:
        metar = board._metar(e["st"], e["ed"], zone)
    except Exception:
        continue
    sig = signals(e["st"], e["ed"], at_local, metar)
    sig["M"] = e.get("M")
    d = e["chosen"]["ord"] - e["mode_ord"]
    their = (1 if d > 0 else -1) * (1 if e["chosen"]["side"] == "Yes" else -1)
    for name in ("temp", "cloud", "rain", "rad", "M"):
        v = sig.get(name)
        if v is not None and abs(v) >= THRESH[name]:
            agree[name].append(1 if (1 if v > 0 else -1) == their else 0)
print("   " + " | ".join(f"{n}: agrees {sum(v)/len(v):.3f} (n={len(v)})" for n, v in agree.items() if v))
