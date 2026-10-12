"""Round 45d data: Open-Meteo's DEFAULT forecast (``best_match``) at the
airport station and at the city centre, as a plain bot would read it.

The cluster's money centre sits ~1.1 C (sd) from our F -- about the gap
between two independent D+2 forecasts -- so it may read a source we do not
hold.  Rounds 32-40 used ten named models at the station's coordinates; the
API's default model mix at the city a bot would geocode from the market title
was never tried.  Per station: the city centre = the most populous Open-Meteo
geocoding hit within 150 km of the station; then one previous-runs request per
point (hourly temperature_2m_previous_day1/2/3, best_match, local timezone,
09-22..10-14).  Aggregator data, research only (charter AGENTS: never P0/P1);
every call is charged to the production quota ledger's research lane first.
Writes ``<bench>/best_match_r45d.json``.
"""
import json
import math
import sqlite3
import sys
import subprocess
import time
import urllib.parse
from pathlib import Path

B = Path(sys.argv[1])
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(B))
import om_research_quota as omq  # noqa: E402
from lib import CITY2ST  # noqa: E402

sys.path.insert(0, "/Users/xiong/weather")
from bayes_basket_target.deterministic_forecast_family import global_roster  # noqa: E402

conn = sqlite3.connect("file:/Users/xiong/weather/data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=60)
ROSTER = {st: (lat, lon, zone) for st, lat, lon, zone in global_roster(conn)}
NAMES = {"nyc": "New York"}
OUT = B / "best_match_r45d.json"
have = json.load(open(OUT)) if OUT.exists() else {}


def get(url, weight):
    omq.charge(weight)
    for attempt in range(5):
        r = subprocess.run(["curl", "-s", "-m", "60", url], capture_output=True, text=True)
        try:
            j = json.loads(r.stdout)
        except ValueError:
            time.sleep(2 + 2 * attempt)
            continue
        if isinstance(j, dict) and j.get("error"):
            if omq.on_reply(j) == "day":
                raise RuntimeError("open-meteo day limit")
        return j
    return None


def km(a, b, c, d):
    p = math.pi / 180
    h = math.sin((c - a) * p / 2) ** 2 + math.cos(a * p) * math.cos(c * p) * math.sin((d - b) * p / 2) ** 2
    return 12742 * math.asin(math.sqrt(h))


for slug, st in sorted(CITY2ST.items()):
    if st not in ROSTER or (st in have and "station" in have[st] and "city" in have[st]):
        continue
    lat, lon, zone = ROSTER[st]
    rec = have.setdefault(st, {"slug": slug, "zone": zone, "station_point": [lat, lon]})
    if "city_point" not in rec:
        name = NAMES.get(slug, slug.replace("-", " ").title())
        g = get("https://geocoding-api.open-meteo.com/v1/search?" + urllib.parse.urlencode({"name": name, "count": 20}), 1)
        hits = [h for h in (g or {}).get("results", []) if km(lat, lon, h["latitude"], h["longitude"]) <= 150]
        if not hits:
            rec["city_error"] = "no geocoding hit within 150 km"
        else:
            best = max(hits, key=lambda h: h.get("population") or 0)
            rec["city_point"] = [best["latitude"], best["longitude"]]
            rec["city_name"] = f"{best['name']}, {best.get('country_code')}"
            rec["city_km"] = round(km(lat, lon, best["latitude"], best["longitude"]), 1)
    for point in ("station", "city"):
        if point in rec or f"{point}_point" not in rec:
            continue
        plat, plon = rec[f"{point}_point"]
        j = get("https://previous-runs-api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode({
            "latitude": plat, "longitude": plon, "timezone": zone, "start_date": "2026-09-22", "end_date": "2026-10-14",
            "hourly": "temperature_2m_previous_day1,temperature_2m_previous_day2,temperature_2m_previous_day3"}), 2)
        if not j or "hourly" not in j:
            rec[f"{point}_error"] = str(j)[:200]
            continue
        h = j["hourly"]
        daily = {}
        for k in ("temperature_2m_previous_day1", "temperature_2m_previous_day2", "temperature_2m_previous_day3"):
            by = {}
            for t, v in zip(h["time"], h.get(k) or []):
                if v is not None:
                    by.setdefault(t[:10], []).append(v)
            daily[k[-4:]] = {d: max(v) for d, v in by.items() if len(v) >= 20}
        rec[point] = daily
        rec[f"{point}_elevation"] = j.get("elevation")
    json.dump(have, open(OUT, "w"), indent=1)
    print(st, rec.get("city_name"), rec.get("city_km"), "station" in rec, "city" in rec, file=sys.stderr)
print("stations", len(have), file=sys.stderr)
