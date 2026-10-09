"""Round 19 part 4: what agrees with them where ECMWF momentum is quiet?
Consensus levels (bias-corrected, as-of) relative to the market mean: all
sources, natives only, points only, Open-Meteo only, regional only; ECMWF
family level minus the rest's consensus; and model spread."""
import json, collections, random, statistics, sys
B = sys.argv[1]
random.seed(19119)
R = json.load(open(f'{B}/direction_rows.json'))
Mm = json.load(open(f'{B}/momentum_M.json'))
for r in R:
    r['q'] = {int(k): v for k, v in r['q'].items()}
    r['M'] = Mm.get(f'{r["station"]}|{r["ed"]}|{r["ts"]}')
REG = ('om:icon_d2', 'om:icon_eu', 'om:ukmo_uk_deterministic_2km', 'om:meteofrance_arome_france_hd', 'om:knmi_harmonie_arome_europe', 'om:dmi_harmonie_arome_europe',
       'om:gfs_hrrr', 'om:gem_hrdps_continental', 'om:gem_regional', 'om:jma_msm', 'om:kma_ldps', 'om:italia_meteo_arpae_icon_2i', 'om:metno_nordic', 'om:ncep_nbm_conus', 'om:meteofrance_arpege_europe')
def cons(r, pred):
    v = [x for k, x in r['sig'].items() if pred(k)]
    return statistics.mean(v) if len(v) >= 2 else None
SIG = {
    'consensus all levels': lambda r: cons(r, lambda k: k.startswith(('pt:', 'om:', 'nat:'))),
    'consensus natives': lambda r: cons(r, lambda k: k.startswith('nat:')),
    'consensus points': lambda r: cons(r, lambda k: k.startswith('pt:')),
    'consensus open-meteo global': lambda r: cons(r, lambda k: k.startswith('om:') and k not in REG),
    'consensus regional': lambda r: cons(r, lambda k: k in REG),
    'ECMWF-family level minus rest': lambda r: (lambda e, o: e - o if e is not None and o is not None else None)(
        cons(r, lambda k: k.startswith(('pt:', 'om:', 'nat:')) and ('ECMWF' in k or 'ecmwf' in k)),
        cons(r, lambda k: k.startswith(('pt:', 'om:', 'nat:')) and not ('ECMWF' in k or 'ecmwf' in k))),
    'rest minus ECMWF-family level': lambda r: (lambda e, o: o - e if e is not None and o is not None else None)(
        cons(r, lambda k: k.startswith(('pt:', 'om:', 'nat:')) and ('ECMWF' in k or 'ecmwf' in k)),
        cons(r, lambda k: k.startswith(('pt:', 'om:', 'nat:')) and not ('ECMWF' in k or 'ecmwf' in k))),
}
def dscore(r, side):
    sd = lambda o: (o - r['mode']) * side
    D = 1 if sd(r['truth']) > 0 else -1 if sd(r['truth']) < 0 else 0
    return D - sum(v * (1 if sd(o) > 0 else -1 if sd(o) < 0 else 0) for o, v in r['q'].items())
sgn = lambda x: 1 if x > 0 else -1
for subset, rows in (('quiet (|M|<0.5)', [r for r in R if r['M'] is None or abs(r['M']) < 0.5]), ('all off-mode', R)):
    print(f'=== {subset}: n={len(rows)} ===')
    for name, f in SIG.items():
        sel = [(r, f(r)) for r in rows]
        sel = [(r, x) for r, x in sel if x is not None and abs(x) >= 0.3]
        if len(sel) < 40: continue
        ag = sum(1 for r, x in sel if sgn(x) == r['s']) / len(sel)
        sk = sum(dscore(r, sgn(x)) for r, x in sel) / len(sel)
        A = [r for r, x in sel if sgn(x) == r['s']]; D = [r for r, x in sel if sgn(x) != r['s']]
        print(f'   {name:34s} n={len(sel):4d} agree {ag:.3f} skill {sk:+.3f}  theirs agree/disagree {sum(dscore(r, r["s"]) for r in A)/max(1,len(A)):+.3f}/{sum(dscore(r, r["s"]) for r in D)/max(1,len(D)):+.3f}')
    # spread of levels
    for lo, hi in ((0, 0.8), (0.8, 1.5), (1.5, 99)):
        sub = [r for r in rows if (lambda v: len(v) >= 4 and lo <= statistics.pstdev(v) < hi)([x for k, x in r['sig'].items() if k.startswith(('pt:', 'om:', 'nat:'))])]
        if sub:
            print(f'   model spread {lo}-{hi}C: n={len(sub):4d} their skill {sum(dscore(r, r["s"]) for r in sub)/len(sub):+.3f}')
