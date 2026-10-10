"""Round 27 part 2: what the minute path looks like on the leg the cluster took
against the legs it passed over in the same event (same side, within two
rungs of the mode), its own side's price, strictly before the fill's minute."""
import collections
import gzip
import json
import statistics
import sys
from pathlib import Path

sys.argv = sys.argv[:2]
B = Path(sys.argv[1])
import importlib.util  # noqa: E402
spec = importlib.util.spec_from_file_location("rf", Path(__file__).with_name("replicate_fit.py"))
src = Path(__file__).with_name("replicate_fit.py").read_text()
head = src.split("# ---------------------------------------------------------------- A. single centres")[0]
ns = {"__name__": "rf", "__file__": str(Path(__file__).with_name("replicate_fit.py"))}
exec(compile(head, "replicate_fit_head", "exec"), ns)
ALL, minute_feats = ns["ALL"], ns["minute_feats"]
rows = collections.defaultdict(lambda: collections.defaultdict(list))
for e in ALL:
    ch = e["chosen"]
    y = 1 if ch["side"] == "Yes" else -1
    for o, label in e["labels"].items():
        if abs(o - e["mode_ord"]) > 2:
            continue
        mf = minute_feats(e, label)
        tag = "taken" if o == ch["ord"] else "passed (same side)"
        kind = ch["side"] + (" on mode" if ch["ord"] == e["mode_ord"] else " off mode")
        for k in ("d1", "d5", "d15", "d30", "d60"):
            if mf[k] == mf[k]:
                rows[(kind, tag)][k].append(mf[k] * y * 100)
        if mf["pos60"] == mf["pos60"]:
            rows[(kind, tag)]["pos60"].append(mf["pos60"] if y == 1 else 1 - mf["pos60"])
        if mf["since"] == mf["since"]:
            rows[(kind, tag)]["since"].append(mf["since"])
print("leg (their side)          taken/passed          n     d1    d5   d15   d30   d60 (cents, own side)  pos-in-60m-range  min since last change (median)")
for kind in ("Yes off mode", "Yes on mode", "No off mode", "No on mode"):
    for tag in ("taken", "passed (same side)"):
        r = rows[(kind, tag)]
        if not r["d60"]:
            continue
        m = lambda k: statistics.mean(r[k]) if r[k] else float("nan")
        print(f"{kind:24s} {tag:20s} {len(r['d60']):5d} {m('d1'):+5.2f} {m('d5'):+5.2f} {m('d15'):+5.2f} {m('d30'):+5.2f} {m('d60'):+5.2f}"
              f"   {m('pos60'):.2f}   {statistics.median(r['since']):.1f}")
