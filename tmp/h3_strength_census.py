"""How usual is applied_strength=0 across every METAR trajectory proof?"""
import re, sqlite3, sys, statistics
sys.path.insert(0, "tmp")
from stage_pick import DB
conn = sqlite3.connect(DB, uri=True, timeout=120.0)
pat = re.compile(r'"applied_strength":([-0-9.eE]+)')
n = z = 0
vals = []
for (blob,) in conn.execute("SELECT proof_json FROM"
                            " basket_probability_update_proof"
                            " WHERE proof_kind='METAR_TRAJECTORY_UPDATE'"):
    m = pat.search(blob)
    if not m:
        continue
    v = float(m.group(1)); n += 1
    if v == 0.0:
        z += 1
    else:
        vals.append(v)
print("全部 METAR 轨迹更新证明 n=%d；applied_strength=0 的 %d (%.1f%%)" % (
    n, z, 100*z/n))
if vals:
    print("非零那部分：中位 %.3f  90分位 %.3f  最大 %.3f" % (
        statistics.median(vals), sorted(vals)[9*len(vals)//10], max(vals)))
    print("非零时最强可能的尾部压制 0.25^s：中位 ×%.3f" % (
        0.25 ** statistics.median(vals)))
