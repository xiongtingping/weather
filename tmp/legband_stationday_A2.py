"""Task A part 2: per-station-day P(settled bucket at distance d from the
ladder centre) -- the quantity comparable to measurement (1)."""
import sys, json, sqlite3, collections
sys.path.insert(0,'scripts')
c = sqlite3.connect('file:data/bayes_basket_target_ssot.db?mode=ro', uri=True, timeout=60)
c.row_factory = sqlite3.Row

settle = {(r['station_id'], r['event_date']): r for r in c.execute(
    "SELECT station_id,event_date,stage,settled_bucket,hit,stake_usdc,payout_usdc,pnl_usdc"
    " FROM paper_trade_settlement")}

rows = c.execute(
    "SELECT position_id,station_id,event_date,stage,decision_at_utc,"
    " ladder_center_bucket,posted_buckets_json,window_buckets_json,leg_shares_json,chain_stake_usdc"
    " FROM paper_trade_position ORDER BY station_id,event_date,decision_at_utc").fetchall()

by_sd = collections.defaultdict(list)
for r in rows: by_sd[(r['station_id'], r['event_date'])].append(r)

def ords(r):
    return {x['bucket']: x['ordinal'] for x in c.execute(
        "SELECT b.bucket,b.ordinal FROM basket_probability_bridge_history h"
        " JOIN basket_probability_bridge_bucket_probability b ON b.history_id=h.history_id"
        " WHERE h.station_id=? AND h.event_date=? AND h.stage=? AND h.decision_at_utc=?",
        (r['station_id'],r['event_date'],r['stage'],r['decision_at_utc']))}

for which, pick in (('entry row (first stage)', lambda v: v[0]),
                    ('last stage row', lambda v: v[-1])):
    dist = collections.Counter(); absd = collections.Counter(); n = 0; off = 0
    bought = collections.Counter()
    for k, v in sorted(by_sd.items()):
        s = settle.get(k)
        if s is None: continue
        r = pick(v)
        o = ords(r)
        if s['settled_bucket'] not in o or r['ladder_center_bucket'] not in o:
            off += 1; continue
        d = o[s['settled_bucket']] - o[r['ladder_center_bucket']]
        n += 1; dist[d] += 1; absd[abs(d)] += 1
        # was that bucket actually bought anywhere in the chain?
        held = set()
        for rr in v: held |= {b for b,q in json.loads(rr['leg_shares_json'] or '{}').items() if float(q)>0}
        if s['settled_bucket'] in held: bought[abs(d)] += 1
    print('\n=== %s : P(settled bucket at distance d from ladder_center_bucket), n=%d station-days (off-ladder %d) ===' % (which, n, off))
    print('signed:', ' '.join('%+d:%d(%.1f%%)' % (d,c_,100*c_/n) for d,c_ in sorted(dist.items())))
    for d in sorted(absd):
        print('  |d|=%d : %3d  %5.1f%%   of which bought: %d (%.0f%%)' % (
            d, absd[d], 100*absd[d]/n, bought[d], 100*bought[d]/absd[d]))
    print('  |d|<=1 : %5.1f%%' % (100*(absd[0]+absd[1])/n))
