"""Round 18 part 4: does their agreement rise with the size of the update,
what agrees with them when the ECMWF-family update is silent, and does a
consensus of every system's newest update cover more of their calls?"""
import json, collections, random, sys
B = sys.argv[1]
R = json.load(open(f'{B}/direction_rows.json'))
random.seed(18118)
for r in R:
    r['q'] = {int(k): v for k, v in r['q'].items()}


def dscore(r, side):
    sd = lambda o: (o - r['mode']) * side
    D = 1 if sd(r['truth']) > 0 else -1 if sd(r['truth']) < 0 else 0
    return D - sum(v * (1 if sd(o) > 0 else -1 if sd(o) < 0 else 0) for o, v in r['q'].items())


sgn = lambda x: 1 if x > 0 else -1
print('=== 1. agreement by update size ===')
for key in ('natd:ECMWF_AIFS_ENS_GLOBAL', 'ptd:ECMWF_IFS_9KM_RELAY_POINT', 'natd:ECMWF_IFS_ENS_OPEN_PF50'):
    for lo, hi in ((0.0, 0.3), (0.3, 0.6), (0.6, 1.0), (1.0, 9)):
        rs = [r for r in R if key in r['sig'] and lo <= abs(r['sig'][key]) < hi]
        if len(rs) < 30: continue
        ag = sum(1 for r in rs if sgn(r['sig'][key]) == r['s']) / len(rs)
        sk = sum(dscore(r, sgn(r['sig'][key])) for r in rs) / len(rs)
        print(f'   {key:34s} |d| {lo:.1f}-{hi:.1f}  n={len(rs):4d}  agree {ag:.3f}  update skill {sk:+.3f}  theirs {sum(dscore(r, r["s"]) for r in rs)/len(rs):+.3f}')

FAM = ['natd:ECMWF_AIFS_ENS_GLOBAL', 'natd:ECMWF_IFS_ENS_OPEN_PF50', 'ptd:ECMWF_IFS_9KM_RELAY_POINT', 'ptd:ECMWF_AIFS_SINGLE_POINT']
def fam(r):
    v = [r['sig'][k] for k in FAM if k in r['sig']]
    return sum(v) if v else None
silent = [r for r in R if fam(r) is None or abs(fam(r)) < 0.3]
print(f'\n=== 2. events where the ECMWF-family update is silent: n={len(silent)}, their skill {sum(dscore(r, r["s"]) for r in silent)/len(silent):+.3f} ===')
keys = collections.Counter(k for r in silent for k in r['sig'])
res = []
for k in keys:
    rs = [r for r in silent if k in r['sig'] and abs(r['sig'][k]) >= 0.3]
    if len(rs) < 60: continue
    ag = sum(1 for r in rs if sgn(r['sig'][k]) == r['s']) / len(rs)
    sk = sum(dscore(r, sgn(r['sig'][k])) for r in rs) / len(rs)
    res.append((ag, k, len(rs), sk))
for ag, k, n, sk in sorted(res, reverse=True)[:12]:
    print(f'   {k:40s} n={n:4d} agree {ag:.3f}  own skill {sk:+.3f}')

print('\n=== 3. consensus of every system newest update (natd:* and ptd:*) ===')
UP = [k for k in keys if k.startswith(('natd:', 'ptd:'))]
def cons(r, tau=0.2):
    v = [sgn(r['sig'][k]) for k in UP if k in r['sig'] and abs(r['sig'][k]) >= tau]
    return (sum(v), len(v)) if v else (0, 0)
for need in (1, 2, 3):
    rs = [r for r in R if abs(cons(r)[0]) >= need]
    ag = sum(1 for r in rs if sgn(cons(r)[0]) == r['s']) / len(rs)
    sk = sum(dscore(r, sgn(cons(r)[0])) for r in rs) / len(rs)
    print(f'   net votes >= {need}: n={len(rs):4d} agree {ag:.3f}  consensus skill {sk:+.3f}  theirs {sum(dscore(r, r["s"]) for r in rs)/len(rs):+.3f}')
# non-ECMWF systems only
NON = [k for k in UP if 'ECMWF' not in k]
def cons2(r):
    v = [sgn(r['sig'][k]) for k in NON if k in r['sig'] and abs(r['sig'][k]) >= 0.2]
    return sum(v)
for need in (1, 2):
    rs = [r for r in R if abs(cons2(r)) >= need]
    ag = sum(1 for r in rs if sgn(cons2(r)) == r['s']) / len(rs)
    sk = sum(dscore(r, sgn(cons2(r))) for r in rs) / len(rs)
    print(f'   non-ECMWF systems, net votes >= {need}: n={len(rs):4d} agree {ag:.3f}  skill {sk:+.3f}')
