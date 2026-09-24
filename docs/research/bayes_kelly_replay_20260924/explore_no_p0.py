import sys
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
import replay_model_belief as R
by_sd, truth, _ = R.load()
# Exploratory: drop every P0 event, so the basket opens on the first P0-M or P1 belief.
no_p0 = {k: [e for e in v if e[1] != "P0"] for k, v in by_sd.items()}
orig_run = R.run
def run_open_any(sd, truth, **kw):
    # relabel the first remaining event as the opener by presenting it as a P0 event
    patched = {}
    for k, evs in sd.items():
        evs = sorted(evs, key=lambda e: e[0])
        if not evs:
            continue
        first = evs[0]
        patched[k] = [(first[0], "P0", *first[2:])] + evs[1:]
    return orig_run(patched, truth, **kw)
for name, kw in (("F_no_p0_buy_sell_g4", dict(gamma=4.0)), ("F_no_p0_buy_sell_g8", dict(gamma=8.0))):
    r = run_open_any(no_p0, truth, **kw)
    print(name, {k: (round(v, 4) if isinstance(v, float) else ({kk: round(vv, 1) for kk, vv in v.items()} if isinstance(v, dict) else v)) for k, v in r.items()}, flush=True)
