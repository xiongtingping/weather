"""Did the served envelopes actually carry the fleet regime shift?

Reads the payload the way production reads it -- through
``history_envelope_payload`` -- because the stored ``envelope_json`` column
can be a stub whose lineage resolves elsewhere.
"""

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bayes_basket_target.probability_bridge import history_envelope_payload

CUT = sys.argv[1] if len(sys.argv) > 1 else "2026-08-27T04:32:00Z"

conn = sqlite3.connect(
    f"file:{ROOT / 'data' / 'bayes_basket_target_ssot.db'}?mode=ro",
    uri=True, timeout=30.0,
)
rows = conn.execute(
    "SELECT stage, station_id, event_date, decision_at_utc, envelope_json,"
    " checkpoint_id, envelope_content_sha256"
    " FROM basket_probability_bridge_history"
    " WHERE decision_at_utc >= ?"
    " ORDER BY decision_at_utc DESC LIMIT 30",
    (CUT,),
).fetchall()

print(f"envelopes since {CUT}: {len(rows)}")
with_receipt = 0
for stage, sid, day, dat, ej, cp, sha in rows:
    try:
        env = history_envelope_payload(
            conn, checkpoint_id=str(cp), envelope_content_sha256=str(sha),
            stored=str(ej) if ej else None,
        )
    except Exception as exc:
        print(f"  {stage} {sid} {day} payload unreadable: {exc}")
        continue
    lineage = env.get("lineage") or {}
    raw = lineage.get("center_width_json")
    if not raw:
        print(f"  {stage} {sid} {day} @{dat[11:19]}  no center_width_json")
        continue
    receipt = json.loads(raw)
    regime = receipt.get("fleet_regime") or {}
    with_receipt += 1
    print(
        f"  {stage} {sid} {day} @{dat[11:19]}"
        f"  regime={receipt.get('regime_delta_c')}"
        f"  fitted={receipt.get('fitted_delta_c')}"
        f"  tier={regime.get('tier')}"
        f"  state={regime.get('state')}"
        f"  signal_day={regime.get('signal_event_date')}"
    )
print(f"carrying a fleet_regime receipt: {with_receipt}/{len(rows)}")
