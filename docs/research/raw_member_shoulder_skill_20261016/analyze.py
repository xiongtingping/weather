"""Which-shoulder skill of each system's raw members, by station and lead; and whether the member daily-max extraction loses it.

Per row (system, station, day, lead bucket, run): c = member mean + walk-forward bias (the mean of truth - member mean over
earlier days for the same system/station/lead/cycle, >= 5 days, else the system/lead fleet value).  m = the bucket holding c on the
settlement ladder (C: 1 C buckets on integers; F: 2 F buckets, even lower label).  Rows with the truth in m-1 or m+1:
score = (c - centre of m) / bucket width (where in its bucket the members put the day), y = truth in m+1.  AUC: 0.5 coin, 1 perfect.
Sampling: IFS uses mx2t3 (3-hour period maxima, no miss); GEM, GEFS and ICON 00/12Z use 3-hourly instants; ICON 06/18Z, AIFS
and AIGEFS 6-hourly instants.  Peak gap = hours between the station's typical local time of the daily max and the nearest instant.
"""
import gzip, json, math, sqlite3, sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
rng = np.random.default_rng(7)


def auc(s, y):
    s = np.asarray(s, float); y = np.asarray(y, float)
    if len(s) == 0 or y.sum() in (0, len(y)): return float('nan')
    o = np.argsort(s, kind='mergesort'); r = np.empty(len(s)); r[o] = np.arange(1, len(s) + 1)
    _, inv, cnt = np.unique(s, return_inverse=True, return_counts=True); r = (np.bincount(inv, weights=r) / cnt)[inv]
    n1 = y.sum(); n0 = len(y) - n1
    return float((r[y == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * n0))


def auc_ci(s, y, days, reps=300):
    s = np.asarray(s); y = np.asarray(y); days = np.asarray(days); ud = np.unique(days)
    idx_by = {d: np.where(days == d)[0] for d in ud}
    bs = []
    for _ in range(reps):
        pick = rng.choice(ud, len(ud)); idx = np.concatenate([idx_by[d] for d in pick])
        bs.append(auc(s[idx], y[idx]))
    bs = np.array([b for b in bs if not math.isnan(b)])
    return auc(s, y), float(np.quantile(bs, 0.025)), float(np.quantile(bs, 0.975))


def to_unit(t, unit): return t if unit == 'C' else t * 9 / 5 + 32


def bucket(x, unit):
    if unit == 'C': k = math.floor(x + 0.5); return k, float(k), 1.0
    k = math.floor((x + 0.5) / 2); return k, 2 * k + 0.5, 2.0


def sampling(ext, sysid):
    if 'mx2t3' in ext: return 'period_max_3h'
    if sysid in ('ECMWF_AIFS_ENS_GLOBAL', 'NOAA_AIGEFS_025') or '_6h_' in ext: return 'instant_6h'
    return 'instant_3h'


def main():
    rows = [json.loads(l) for l in gzip.open(HERE / 'members.jsonl.gz', 'rt')]
    for r in rows:
        v = np.array(r['vals']); r['mean'] = float(v.mean()); r['n'] = len(v); r['samp'] = sampling(r['ext'], r['sys'])
    # walk-forward bias
    hist = defaultdict(list); fleet = defaultdict(list)
    for r in rows:
        hist[(r['sys'], r['st'], r['lead'], r['cycle'])].append((r['day'], r['truth'] - r['mean']))
        fleet[(r['sys'], r['lead'])].append((r['day'], r['truth'] - r['mean']))
    for v in list(hist.values()) + list(fleet.values()): v.sort()

    def prior_mean(lst, day, min_days):
        vals = [e for d, e in lst if d < day]
        return float(np.mean(vals)) if len({d for d, _ in lst if d < day}) >= min_days and vals else None
    cache = {}
    recs = []
    for r in rows:
        k = (r['sys'], r['st'], r['lead'], r['cycle'], r['day'])
        if k not in cache:
            b = prior_mean(hist[k[:4]], r['day'], 5)
            if b is None: b = prior_mean(fleet[(r['sys'], r['lead'])], r['day'], 5)
            cache[k] = b
        b = cache[k]
        if b is None: continue
        cu = to_unit(r['mean'] + b, r['unit']); tu = to_unit(r['truth'], r['unit'])
        km, cen, w = bucket(cu, r['unit']); kt, _, _ = bucket(tu, r['unit'])
        if abs(kt - km) != 1: continue
        recs.append({'sys': r['sys'], 'st': r['st'], 'day': r['day'], 'lead': r['lead'], 'samp': r['samp'], 'cycle': r['cycle'],
                     'score': (cu - cen) / w, 'y': float(kt == km + 1), 'unit': r['unit'], 'sd': float(np.std(r['vals']))})
    print('shoulder rows', len(recs))
    out = {}
    systems = sorted({x['sys'] for x in recs}); leads = ['h00_12', 'h12_24', 'h24_48', 'h48_plus']
    # 1. system x lead
    print('\n1. system x lead: which-shoulder AUC [95%] (rows)')
    t1 = {}
    for s in systems:
        line = []
        for ld in leads:
            sel = [x for x in recs if x['sys'] == s and x['lead'] == ld]
            if len(sel) < 100: line.append(f'{ld}: -'); continue
            a, lo, hi = auc_ci([x['score'] for x in sel], [x['y'] for x in sel], [x['day'] for x in sel])
            t1[f'{s}|{ld}'] = {'auc': a, 'lo': lo, 'hi': hi, 'n': len(sel)}
            line.append(f'{ld}: {a:.3f} [{lo:.3f},{hi:.3f}] ({len(sel)})')
        print(f'  {s:26s} ' + '  '.join(line))
    out['system_lead'] = t1
    # 2. station x system (all leads) + best system per station
    print('\n2. station x system (all leads), AUC; * = lower 95% bound > 0.5')
    t2 = {}
    stations = sorted({x['st'] for x in recs})
    hdr = '  station ' + ' '.join(f'{s.split("_")[1][:6]:>13s}' for s in systems) + '   best'
    print(hdr)
    for st in stations:
        cells = []; best = (None, -1)
        for s in systems:
            sel = [x for x in recs if x['sys'] == s and x['st'] == st]
            if len(sel) < 40: cells.append(f'{"-":>13s}'); continue
            a, lo, hi = auc_ci([x['score'] for x in sel], [x['y'] for x in sel], [x['day'] for x in sel], reps=200)
            t2[f'{st}|{s}'] = {'auc': a, 'lo': lo, 'hi': hi, 'n': len(sel)}
            cells.append(f'{a:6.3f}{"*" if lo > 0.5 else " "}({len(sel):4d})')
            if a > best[1]: best = (s, a)
        print(f'  {st:7s} ' + ' '.join(cells) + f'   {best[0].split("_")[1] if best[0] else "-"}')
    out['station_system'] = t2
    # 3. sampling: ICON 3h vs 6h on the same station-day-lead
    print('\n3. ICON same station-day-lead: 3-hourly runs vs 6-hourly runs')
    icon = defaultdict(dict)
    for x in recs:
        if x['sys'] == 'DWD_ICON_GLOBAL_EPS':
            icon[(x['st'], x['day'], x['lead'])].setdefault(x['samp'], []).append(x)
    pairs = [(v['instant_3h'], v['instant_6h']) for v in icon.values() if 'instant_3h' in v and 'instant_6h' in v]
    a3 = [x for p, _ in pairs for x in p]; a6 = [x for _, q in pairs for x in q]
    for lab, sel in (('3-hourly (00/12Z)', a3), ('6-hourly (06/18Z)', a6)):
        a, lo, hi = auc_ci([x['score'] for x in sel], [x['y'] for x in sel], [x['day'] for x in sel])
        print(f'  {lab:20s} AUC {a:.3f} [{lo:.3f},{hi:.3f}] rows {len(sel)}  (paired station-day-leads {len(pairs)})')
        out[f'icon_{lab[:2]}'] = {'auc': a, 'lo': lo, 'hi': hi, 'n': len(sel)}
    # 4. peak gap for instant-sampled systems
    eta = json.load(open(ROOT / 'docs/research/p1_forward_physics_20261015/eta_table.json'))['rows']
    peak = defaultdict(list)
    for e in eta: peak[e['station_id']].append(e['tmax_local_minutes'] / 60.0)
    peak = {s: float(np.median(v)) for s, v in peak.items()}
    tzs = dict(sqlite3.connect(ROOT / 'docs/research/p1_forward_physics_20261015/inputs.db').execute('select station_id, settlement_timezone from tz'))

    def gap(st, day, step):
        off = datetime.fromisoformat(day + 'T12:00:00').replace(tzinfo=ZoneInfo(tzs[st])).utcoffset().total_seconds() / 3600
        pu = (peak[st] - off) % 24
        return min(min(abs(pu - h), 24 - abs(pu - h)) for h in range(0, 24, step))
    print('\n4. peak gap (hours from typical local peak to nearest sampled instant), by sampling, all leads')
    t4 = {}
    for samp, step in (('instant_6h', 6), ('instant_3h', 3), ('period_max_3h', 3)):
        sel = [x for x in recs if x['samp'] == samp and x['st'] in peak and x['st'] in tzs]
        g = np.array([gap(x['st'], x['day'], step) for x in sel])
        bins = [(0, 1), (1, 2), (2, 3.01)] if step == 6 else [(0, 0.75), (0.75, 1.51)]
        line = []
        for lo_, hi_ in bins:
            idx = [i for i in range(len(sel)) if lo_ <= g[i] < hi_]
            if len(idx) < 100: continue
            ss = [sel[i] for i in idx]
            a, lo, hi = auc_ci([x['score'] for x in ss], [x['y'] for x in ss], [x['day'] for x in ss], reps=200)
            t4[f'{samp}|{lo_}-{hi_}'] = {'auc': a, 'lo': lo, 'hi': hi, 'n': len(ss)}
            line.append(f'gap {lo_}-{hi_}h: {a:.3f} [{lo:.3f},{hi:.3f}] ({len(ss)})')
        print(f'  {samp:14s} ' + '  '.join(line))
        # per system within this sampling
        for s in sorted({x['sys'] for x in sel}):
            line = []
            for lo_, hi_ in bins:
                idx = [i for i in range(len(sel)) if lo_ <= g[i] < hi_ and sel[i]['sys'] == s]
                if len(idx) < 100: continue
                ss = [sel[i] for i in idx]
                line.append(f'gap {lo_}-{hi_}h: {auc([x["score"] for x in ss], [x["y"] for x in ss]):.3f} ({len(ss)})')
            print(f'      {s:26s} ' + '  '.join(line))
    out['peak_gap'] = t4
    json.dump(out, open(HERE / 'readings.json', 'w'), indent=1, default=float)


if __name__ == '__main__':
    main()
