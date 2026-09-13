"""Read-only: what the seated probe says about the live official inbox right now."""
import json, sqlite3, collections, sys
from datetime import datetime, timezone
sys.path.insert(0, "/Users/xiong/weather")
from bayes_basket_target.production_worker import (
    TargetProductionWorker, _native_forward_packet_is_stale, _official_inbox_packets_all_stale, load_bounded_json,
)
from pathlib import Path
now = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
conn = sqlite3.connect("file:/Users/xiong/weather/data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=5)
conn.execute("pragma query_only=on")
inbox = Path("/Users/xiong/weather/data/runtime/bayes_basket_target_nwp/official_native_ensemble_forward_inbox.json")
root = load_bounded_json(inbox)
packets = root.get("packets") if isinstance(root, dict) else root
w = object.__new__(TargetProductionWorker)
out = collections.Counter(); days = collections.defaultdict(lambda: collections.Counter())
for p in packets:
    day = p.get("event_date"); st = str(p.get("station_id")).upper()
    seats = w._latest_p0_weighted_systems(conn, event_date=day, as_of_utc=now).get(st)
    packet_stale = _native_forward_packet_is_stale(p, as_of_utc=now)
    seated_stale = _native_forward_packet_is_stale(p, as_of_utc=now, seated_system_ids=seats)
    key = ("packet_stale" if packet_stale else "packet_fresh", "seated_stale" if seated_stale else "seated_fresh", "seats_known" if seats else "no_seats")
    out[key] += 1; days[day][key] += 1
print(now, len(packets))
for k, v in sorted(out.items()): print(k, v)
for d in sorted(days): print(d, dict(days[d]))
for d in sorted(days):
    stations = sorted({str(p.get("station_id")).upper() for p in packets if p.get("event_date") == d})
    trig = [s for s in stations if _official_inbox_packets_all_stale(inbox, event_date=d, as_of_utc=now, station_id=s,
            seated_system_ids=w._latest_p0_weighted_systems(conn, event_date=d, as_of_utc=now).get(s))]
    print(d, "all-stale(old)", _official_inbox_packets_all_stale(inbox, event_date=d, as_of_utc=now), "stations that would ask for a re-read", len(trig))
