"""Round 30 data: the full bucket ladder (label, YES token) of every settled
station-day 2026-08-15..10-10.  Event days from 09-21 take the SSOT registry
(read-only); earlier ones the public gamma event by slug (the station's city
slug as the registry spells it).  Writes <bench>/ladders.json; resumable.
"""
import json
import re
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

sys.path.insert(0, "/Users/xiong/weather")
from bayes_basket_target.copy_follow_lane import parse_event_slug  # noqa: E402

B = Path(sys.argv[1])
B.mkdir(parents=True, exist_ok=True)
START, END = "2026-08-15", "2026-10-10"
OUT = B / "ladders.json"
conn = sqlite3.connect("file:/Users/xiong/weather/data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=60)
conn.execute("PRAGMA query_only=1")
tz = {s: z for s, z in conn.execute("SELECT station_id, settlement_timezone FROM calibration_station_market_timezone_binding")}
days = sorted({(s, d) for s, d in conn.execute(
    "SELECT station_id, event_date FROM basket_station_day_settlement_label WHERE event_date BETWEEN ? AND ?", (START, END))})
city = {}
for st, slug in conn.execute("SELECT station_id, slug FROM basket_target_market_registry WHERE slug<>'' AND event_date>='2026-09-21'"):
    p = parse_event_slug(slug)
    if p:
        city.setdefault(st, p[0])
out = json.load(open(OUT)) if OUT.exists() else {}
MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"]


def registry(st, ed):
    seen, rows = set(), []
    for outcome, tok in conn.execute(
            "SELECT outcome, yes_token FROM basket_target_market_registry WHERE station_id=? AND event_date=?"
            " AND yes_token IS NOT NULL AND yes_token<>'' ORDER BY rowid DESC", (st, ed)):
        if outcome not in seen:
            seen.add(outcome)
            rows.append({"label": outcome, "yes": tok})
    return rows


def gamma(st, ed):
    y, m, d = (int(x) for x in ed.split("-"))
    slug = f"highest-temperature-in-{city[st]}-on-{MONTHS[m - 1]}-{d}-{y}"
    for attempt in range(4):
        r = subprocess.run(["curl", "-s", "-m", "30", f"https://gamma-api.polymarket.com/events?slug={slug}"],
                           capture_output=True, text=True)
        try:
            ev = json.loads(r.stdout)
            if not ev:
                return {"error": "no_event", "slug": slug}
            rows = []
            for mk in ev[0].get("markets", []):
                outs = json.loads(mk["outcomes"]) if isinstance(mk["outcomes"], str) else mk["outcomes"]
                toks = json.loads(mk["clobTokenIds"]) if isinstance(mk["clobTokenIds"], str) else mk["clobTokenIds"]
                rows.append({"label": mk["groupItemTitle"], "yes": toks[outs.index("Yes")]})
            return {"rows": rows, "slug": slug}
        except Exception:
            time.sleep(2 + 3 * attempt)
    return {"error": "fetch_failed", "slug": slug}


todo = [(st, ed) for st, ed in days if f"{st}|{ed}" not in out or "error" in out[f"{st}|{ed}"]]
print(f"station-days {len(days)}, to build {len(todo)}", file=sys.stderr)
gjobs = []
for st, ed in todo:
    key = f"{st}|{ed}"
    if st not in tz:
        continue
    if ed >= "2026-09-21":
        rows = registry(st, ed)
        if rows:
            out[key] = {"tz": tz[st], "source": "registry", "buckets": rows}
            continue
    if st in city:
        gjobs.append((st, ed))
with ThreadPoolExecutor(max_workers=8) as pool:
    for (st, ed), res in zip(gjobs, pool.map(lambda j: gamma(*j), gjobs)):
        key = f"{st}|{ed}"
        out[key] = ({"tz": tz[st], "source": "gamma", "slug": res["slug"], "buckets": res["rows"]} if "rows" in res
                    else {"tz": tz[st], "error": res["error"], "slug": res["slug"]})
json.dump(out, open(OUT, "w"))
ok = sum(1 for v in out.values() if "error" not in v)
print(f"ladders ok {ok} of {len(out)}; errors {sorted({v['error'] for v in out.values() if 'error' in v})}", file=sys.stderr)
