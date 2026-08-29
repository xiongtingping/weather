"""Thirty evening artifacts pass their gate; four fifths of evening rows get none.

Whatever sits between "the artifact passed" and "the compiler held it" is the
real promotion blocker, and it is not the gate.
"""
import glob, json, sqlite3, sys
from collections import defaultdict
sys.path.insert(0, "tmp")
from stage_pick import DB, local_close, parse

passing = {}
for f in glob.glob("data/runtime/bayes_basket_target_nwp/"
                  "metar_trajectory_training/*_evening_artifact.json"):
    station = f.split("/")[-1].split("_")[0]
    d = json.load(open(f))
    passing[station] = {
        "gate": bool(d.get("oos_gate_passed")),
        "state": str(d.get("state")),
        "activations": len(d.get("activations") or []),
    }

conn = sqlite3.connect(DB, uri=True, timeout=120.0)
tz = {(str(r[0]), str(r[1])): str(r[2]) for r in conn.execute(
    "SELECT DISTINCT station_id,event_date,settlement_timezone"
    " FROM basket_official_ws_snapshot")}
seen = defaultdict(lambda: {"have": 0, "none": 0})
for r in conn.execute(
    "SELECT station_id,event_date,decision_at_utc,envelope_json"
    " FROM basket_probability_bridge_history WHERE stage='P1' AND state='COMMITTED'"
):
    k = (str(r[0]), str(r[1]))
    if k not in tz:
        continue
    try:
        close = local_close(k[1], tz[k])
    except Exception:
        continue
    if parse(str(r[2])).astimezone(close.tzinfo).hour < 18:
        continue
    lin = json.loads(r[3]).get("lineage") or {}
    act = str(lin.get("metar_trajectory_activation_id") or "NONE")
    seen[k[0]]["none" if act == "NONE" else "have"] += 1

print("EVENING：产物闸门结果 vs 实际服务到的行")
print("  站     闸门  产物state          activations  有产物行  无产物行")
gate_pass_but_starved = 0
for st in sorted(seen):
    p = passing.get(st, {})
    d = seen[st]
    flag = ""
    if p.get("gate") and d["none"] > d["have"]:
        flag = "  ←← 闸门过了却服务不到"
        gate_pass_but_starved += 1
    print("  %-6s %-5s %-18s %-11s %7d %9d%s" % (
        st, p.get("gate"), p.get("state"), p.get("activations"),
        d["have"], d["none"], flag))
print("\n闸门通过但多数行仍拿不到产物的站: %d" % gate_pass_but_starved)
