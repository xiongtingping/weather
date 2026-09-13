"""P0 freshness coverage under three TTL bases, from committed history (read-only files)."""
import json, collections, datetime, pathlib
from zoneinfo import ZoneInfo
OUT = pathlib.Path(__file__).resolve().parent
rows = json.loads((OUT / "p0_rows.json").read_text())
docs = json.loads((OUT / "availability_docs.json").read_text())
TTL = datetime.timedelta(hours=6); FLOOR = datetime.timedelta(seconds=60)
CUTOFF = datetime.datetime(2026, 9, 13, 5, 0, tzinfo=datetime.timezone.utc)
def t(v): return datetime.datetime.fromisoformat(v.replace("Z", "+00:00"))
for r in rows:
    r["d"] = t(r["decision"]); r["fu"] = t(r["fresh_until"])
    q = [t(v) for v in r["seated_query"].values()]
    r["seat_max"] = max(q) + TTL if q else r["fu"]
    r["seat_min"] = min(q) + TTL if q else r["fu"]
scopes = collections.defaultdict(list)
for r in rows: scopes[(r["station_id"], r["event_date"])].append(r)
cov = collections.defaultdict(lambda: collections.Counter())
gap_kinds = collections.Counter(); scope_hit = collections.defaultdict(set)
seat_sets_stale = collections.Counter()
STEP = datetime.timedelta(minutes=5)
for (s, day), items in scopes.items():
    items.sort(key=lambda r: r["d"])
    tz = ZoneInfo(items[0]["tz"]); start = datetime.datetime.fromisoformat(day).replace(tzinfo=tz)
    end = min(start + datetime.timedelta(days=1), CUTOFF)
    at = items[0]["d"]
    while at < end:
        phase = "pre" if at < start else "day"
        vis = [r for r in items if r["d"] <= at]
        cov[phase]["n"] += 1
        head = vis[-1]
        served = at < head["fu"]
        cov[phase]["served"] += served
        for basis in ("seat_max", "seat_min"):
            adm = [r for r in vis if r["d"] < r[basis] - FLOOR]
            ok = bool(adm) and at < adm[-1][basis]
            cov[phase][basis] += ok
            if basis == "seat_max" and served and not ok:
                scope_hit[phase].add((s, day))
                # Could the seated systems have been refreshed? Look for a later packet (any P0 up to the
                # served expiry) whose availability carries a newer archive of every seated system.
                seats = set(head["seated_query"])
                seat_sets_stale[tuple(sorted(seats))] += 1
                newer = False
                for r in items:
                    if r["d"] <= at: continue
                    doc = docs.get(r["availability"]) or {}
                    if seats and all(k in doc and t(doc[k][1]) + TTL > at for k in seats):
                        newer = r["d"] - at; break
                gap_kinds[(phase, "refreshed_later" if newer else "never_refreshed_that_day")] += 1
        at += STEP
print("5-minute samples; share of time with a servable P0 head")
for ph in ("pre", "day"):
    c = cov[ph]; n = c["n"]
    print(f"  {ph}: n={n} served={c['served']/n:.3f} seated_max={c['seat_max']/n:.3f} seated_min={c['seat_min']/n:.3f}")
print("scopes losing time (seated_max):", {k: len(v) for k, v in scope_hit.items()}, "of", len(scopes))
print("gap samples by whether seated systems were refreshed later:", dict(gap_kinds))
print("seat sets during gaps:", seat_sets_stale.most_common(8))
json.dump({"coverage": {k: dict(v) for k, v in cov.items()}, "gap_kinds": {"|".join(k): v for k, v in gap_kinds.items()},
           "seat_sets": {"+".join(k): v for k, v in seat_sets_stale.items()}}, open(OUT / "coverage.json", "w"), indent=2)
