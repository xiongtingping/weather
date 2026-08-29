"""Task A: re-band the CURRENT paper ledger's bought legs using the
production _is_shoulder / posted window / ladder_center_bucket."""
import sys, json, sqlite3, statistics
sys.path.insert(0, 'scripts')
import paper_trading_ledger as L

DB = 'file:data/bayes_basket_target_ssot.db?mode=ro'
c = sqlite3.connect(DB, uri=True, timeout=60)
c.row_factory = sqlite3.Row

pos = c.execute(
    "SELECT position_id, station_id, event_date, stage, decision_at_utc,"
    " ladder_center_bucket, window_buckets_json, posted_buckets_json,"
    " leg_shares_json, chain_shares_json, resting_fill_json,"
    " probabilities_json, stake_usdc, chain_stake_usdc"
    " FROM paper_trade_position ORDER BY station_id, event_date, decision_at_utc"
).fetchall()

settle = {}
for r in c.execute(
    "SELECT station_id, event_date, stage, settled_bucket, settled_tmax_c, hit,"
    " stake_usdc, payout_usdc, pnl_usdc FROM paper_trade_settlement"):
    settle[(r['station_id'], r['event_date'])] = r

def ladder(st, ed, stage, dec):
    return c.execute(
        "SELECT b.bucket, b.ordinal FROM basket_probability_bridge_history h"
        " JOIN basket_probability_bridge_bucket_probability b"
        "   ON b.history_id = h.history_id"
        " WHERE h.station_id=? AND h.event_date=? AND h.stage=? AND h.decision_at_utc=?"
        " ORDER BY b.ordinal", (st, ed, stage, dec)).fetchall()

legs = []
diag = {'no_settlement':0, 'centre_off_posted':0, 'leg_off_posted':0,
        'no_fill_price':0, 'rows':0, 'noncontig_posted':0}
stake_recon_err = []
for p in pos:
    key = (p['station_id'], p['event_date'])
    s = settle.get(key)
    diag['rows'] += 1
    posted = json.loads(p['posted_buckets_json'] or '[]') or json.loads(p['window_buckets_json'])
    centre = p['ladder_center_bucket']
    per_leg = json.loads(p['leg_shares_json'] or '{}')
    probs = json.loads(p['probabilities_json'] or '{}')
    fills = json.loads(p['resting_fill_json'] or '{}')
    ords = {r['bucket']: r['ordinal'] for r in ladder(
        p['station_id'], p['event_date'], p['stage'], p['decision_at_utc'])}
    if posted and ords:
        o = [ords.get(b) for b in posted]
        if None not in o and (max(o)-min(o) != len(o)-1):
            diag['noncontig_posted'] += 1
    if centre not in posted:
        diag['centre_off_posted'] += 1
    recon = 0.0
    for b, q in per_leg.items():
        if float(q) <= 0:
            continue
        if b not in posted:
            diag['leg_off_posted'] += 1
            d = None
        else:
            d = abs(posted.index(b) - posted.index(centre)) if centre in posted else None
        # cross-check against production predicate
        sh = L._is_shoulder(b, centre, posted)
        assert (d == 1) == sh, (b, centre, posted, d, sh)
        f = fills.get(b)
        price = None
        if f and '@' in f:
            price = float(f.split('@')[1])
        else:
            diag['no_fill_price'] += 1
        stake = (float(q) * price * (1.0 + L.FEE_RATE)) if price is not None else None
        if stake: recon += stake
        legs.append(dict(
            station=p['station_id'], date=p['event_date'], stage=p['stage'],
            bucket=b, dist=d, shares=float(q), price=price, stake=stake,
            prob=float(probs.get(b, 0.0)),
            settled=(s['settled_bucket'] if s else None),
            hit=(1 if s and b == s['settled_bucket'] else 0),
            has_settle=(s is not None),
        ))
    if recon and p['stake_usdc']:
        stake_recon_err.append(recon - float(p['stake_usdc']))

print('rows=%d legs=%d diag=%s' % (diag['rows'], len(legs), diag))
if stake_recon_err:
    print('stake reconstruction err: max abs %.6f  mean %.6f' % (
        max(abs(e) for e in stake_recon_err), statistics.fmean(stake_recon_err)))

def band(d):
    if d is None: return 'off-window'
    if d == 0: return 'centre (d=0)'
    if d == 1: return 'shoulder (d=1)'
    return 'rest (d>=2)'

settled_legs = [x for x in legs if x['has_settle']]
print('\nlegs total=%d  on settled station-days=%d' % (len(legs), len(settled_legs)))

hdr = ('%-16s %5s %6s %10s %10s %10s %10s %7s %7s %8s' % (
    'band','legs','st-day','shares','stake','payout','net','hit%','meanP','ROI%'))
print('\n=== PER-LEG, settled station-days only ===')
print(hdr)
tot = {}
for name in ['centre (d=0)','shoulder (d=1)','rest (d>=2)','off-window']:
    g = [x for x in settled_legs if band(x['dist']) == name]
    if not g: continue
    stake = sum(x['stake'] or 0 for x in g)
    payout = sum(x['shares'] if x['hit'] else 0 for x in g)
    hits = sum(x['hit'] for x in g)
    px = sorted(x['price'] for x in g if x['price'] is not None)
    tot[name] = dict(n=len(g), sd=len({(x['station'],x['date']) for x in g}),
        shares=sum(x['shares'] for x in g), stake=stake, payout=payout,
        net=payout-stake, hits=hits, meanP=statistics.fmean(x['prob'] for x in g),
        pmin=px[0], pmed=statistics.median(px), pmax=px[-1])
    t = tot[name]
    print('%-16s %5d %6d %10.1f %10.1f %10.1f %+10.1f %6.1f%% %7.3f %+7.1f%%' % (
        name, t['n'], t['sd'], t['shares'], t['stake'], t['payout'], t['net'],
        100*t['hits']/t['n'], t['meanP'], 100*t['net']/t['stake'] if t['stake'] else 0))
    print('%-16s   fill price  min %.4f  median %.4f  max %.4f' % ('', t['pmin'], t['pmed'], t['pmax']))
print('%-16s %5d %6d %10.1f %10.1f %10.1f %+10.1f' % ('TOTAL',
    sum(t['n'] for t in tot.values()), len({(x['station'],x['date']) for x in settled_legs}),
    sum(t['shares'] for t in tot.values()), sum(t['stake'] for t in tot.values()),
    sum(t['payout'] for t in tot.values()),
    sum(t['net'] for t in tot.values())))

json.dump(legs, open('tmp/legband_recount_A.json','w'))
