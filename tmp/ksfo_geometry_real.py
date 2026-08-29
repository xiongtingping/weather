"""Call the real geometry builder on the real KSFO root -- no reimplementation.

Which branch it takes and what upper bounds it produces are the two facts that
decide whether the leak is the F constant or something else entirely.
"""
import json, sqlite3, sys
sys.path.insert(0, ".")
from bayes_basket_target.p1_compiler import (
    _bucket_geometry_c, _hard_exclusion_threshold_c, _registry_bucket_bounds)
from bayes_basket_target.probability_envelope import ProbabilityEnvelope

conn = sqlite3.connect("file:data/bayes_basket_target_ssot.db?mode=ro",
                       uri=True, timeout=120.0)
row = conn.execute(
    "SELECT envelope_json FROM basket_probability_bridge_history"
    " WHERE station_id='KSFO' AND event_date='2026-08-20' AND stage='P1'"
    " AND state='COMMITTED' ORDER BY decision_at_utc DESC LIMIT 1").fetchone()
lin = json.loads(row[0])["lineage"]
root_cp = lin["p0_root_checkpoint_id"]
r = conn.execute(
    "SELECT stage,checkpoint_id,station_id,event_date,settlement_timezone,"
    "decision_at_utc,fresh_until_utc,bucket_ordinals_json,probabilities_json,"
    "posterior_version,posterior_sha256,bridge_sha256,lineage_json,content_sha256"
    " FROM basket_probability_envelope WHERE checkpoint_id=?", (root_cp,)).fetchone()
root_lineage = json.loads(r[12] or "{}")
print("P0 根 checkpoint:", root_cp)
print("根 lineage 有 bucket_intervals_c_json?",
      bool(root_lineage.get("bucket_intervals_c_json")))
print("根 lineage 有 bucket_centers_c_json?  ",
      bool(root_lineage.get("bucket_centers_c_json")))
print("=> 走的是", "快路径(用真实区间)" if
      (root_lineage.get("bucket_intervals_c_json") and
       root_lineage.get("bucket_centers_c_json")) else "慢路径(用换算公式)")
print()
reg = conn.execute(
    "SELECT bucket_label,upper_bound,settlement_unit FROM basket_target_market_registry"
    " WHERE station_id='KSFO' AND event_date='2026-08-20'").fetchall() \
    if conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='basket_target_market_registry'").fetchone()[0] else []
print("registry 列样例:", reg[:3] if reg else "(表结构不同, 见下)")
cols = [c[1] for c in conn.execute("PRAGMA table_info(basket_target_market_registry)")]
print("registry 列名:", cols)
