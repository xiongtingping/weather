"""Read-only: P0 centres for forward-TAF scopes, with the bias-refusal flag."""
import json, sqlite3, time, pathlib
OUT = pathlib.Path(__file__).resolve().parent
tafs = json.load(open(OUT / "tafs.json")); truth = json.load(open(OUT / "truth.json"))
scopes = sorted({(t["station"], t["day"]) for t in tafs if t["source"] == "forward" and f"{t['station']}|{t['day']}" in truth})
c = sqlite3.connect("file:/Users/xiong/weather/data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=5)
c.execute("pragma query_only=on")
deadline = time.monotonic() + 540
c.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
print(c.execute("explain query plan select 1 from basket_probability_envelope where station_id=? and event_date=? and stage='P0'", ("x", "y")).fetchall())
out = {}
for station, day in scopes:
    out[f"{station}|{day}"] = [
        (dec, cen, fu, "bias_refused" in (prov or ""))
        for dec, cen, fu, prov in c.execute(
            "select decision_at_utc, json_extract(lineage_json,'$.model_center_c'), fresh_until_utc,"
            " json_extract(lineage_json,'$.provisional_parameter_sources_json') from basket_probability_envelope"
            " where station_id=? and event_date=? and state='COMMITTED' and stage='P0' order by decision_at_utc", (station, day))
        if cen not in (None, "", "NONE")]
(OUT / "centres_flag.json").write_text(json.dumps(out))
rows = [r for v in out.values() for r in v]
print("scopes", len(scopes), "with centres", sum(1 for v in out.values() if v), "rows", len(rows), "refused", sum(r[3] for r in rows))
