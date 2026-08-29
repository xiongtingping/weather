"""Task A part 4: where the shoulder's 27.7% becomes 4.0% -- side, then fill."""
import json, sqlite3, collections
c = sqlite3.connect('file:data/bayes_basket_target_ssot.db?mode=ro', uri=True, timeout=60)
c.row_factory = sqlite3.Row
settle = {(r['station_id'],r['event_date']):r for r in c.execute(
  "SELECT station_id,event_date,settled_bucket FROM paper_trade_settlement")}
rows = c.execute(
  "SELECT station_id,event_date,stage,decision_at_utc,ladder_center_bucket,"
  " posted_buckets_json,window_buckets_json,leg_shares_json,resting_fill_json"
  " FROM paper_trade_position ORDER BY station_id,event_date,decision_at_utc").fetchall()
by_sd = collections.defaultdict(list)
for r in rows: by_sd[(r['station_id'],r['event_date'])].append(r)
def ords(r):
    return {x['bucket']:x['ordinal'] for x in c.execute(
      "SELECT b.bucket,b.ordinal FROM basket_probability_bridge_history h"
      " JOIN basket_probability_bridge_bucket_probability b ON b.history_id=h.history_id"
      " WHERE h.station_id=? AND h.event_date=? AND h.stage=? AND h.decision_at_utc=?",
      (r['station_id'],r['event_date'],r['stage'],r['decision_at_utc']))}
st = collections.Counter()
detail=[]
for k,v in sorted(by_sd.items()):
    s = settle.get(k)
    if s is None: continue
    r = v[0]; o = ords(r)
    ctr = r['ladder_center_bucket']
    if s['settled_bucket'] not in o or ctr not in o: continue
    if abs(o[s['settled_bucket']] - o[ctr]) != 1: continue
    st['truth_at_|d|=1'] += 1
    posted = set(); filled = set()
    for rr in v:
        posted |= set(json.loads(rr['posted_buckets_json'] or '[]'))
        filled |= {b for b,q in json.loads(rr['leg_shares_json'] or '{}').items() if float(q)>0}
    tgt = s['settled_bucket']
    if tgt not in posted:
        st['a) never posted (wrong side of window)'] += 1; why='never posted'
    elif tgt not in filled:
        st['b) posted but never filled'] += 1; why='posted, unfilled'
    else:
        st['c) posted and filled'] += 1; why='filled'
    detail.append((k[0],k[1],ctr,tgt,why))
for kk,n in sorted(st.items()):
    print('%-42s %3d  (%.0f%% of the |d|=1 days)' % (kk,n,100*n/st['truth_at_|d|=1']))
print()
for d in detail: print('   %-6s %-11s centre=%-14s truth=%-14s %s' % d)
