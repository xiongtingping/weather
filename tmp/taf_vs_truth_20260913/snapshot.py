"""Read-only: TAF TX, settled truth, and the P0 model centre standing when each TAF arrived."""
import json, sqlite3, time, pathlib, resource, shutil, collections
OUT = pathlib.Path(__file__).resolve().parent
resource.setrlimit(resource.RLIMIT_FSIZE, (64 * 1024**2, 64 * 1024**2))
assert shutil.disk_usage(OUT).free > 5 * 1024**3
c = sqlite3.connect("file:/Users/xiong/weather/data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=5)
c.execute("pragma query_only=on"); c.execute("pragma cache_size=-16384")
deadline = time.monotonic() + 540
c.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
tafs = [dict(zip(("source","station","day","tz","issued","received","tx","valid_from","valid_until","kind","seq"), r)) for r in c.execute(
    "select 'forward',station_id,event_date,settlement_timezone,issued_at_utc,received_at_utc,tx_c,valid_from_utc,valid_until_utc,revision_kind,revision_sequence"
    " from calibration_forward_taf_capture where tx_c is not null and event_date<='2026-09-12'")]
tafs += [dict(zip(("source","station","day","tz","issued","received","tx","tx_at","anchor"), r)) for r in c.execute(
    "select 'historical',station_id,event_date,settlement_timezone,issued_at_utc,provider_available_at_utc,tx_c,tx_valid_at_utc,decision_anchor_utc"
    " from calibration_historical_taf_capture where tx_c is not null")]
truth = {}
for station, day, tmax, rev, qc, lane, sdef in c.execute(
    "select station_id,local_date,official_tmax_c,revision_no,qc_status,source_lane,settlement_definition from calibration_station_daily_high_truth"):
    if str(qc).upper() != "ACCEPTED" or tmax is None:
        continue
    key = (station, day)
    if key not in truth or rev > truth[key][1]:
        truth[key] = (float(tmax), rev, lane, sdef)
# P0 centre standing when each forward TAF was received (latest P0 decided at or before receipt).
centre = {}
scopes = sorted({(t["station"], t["day"]) for t in tafs if (t["station"], t["day"]) in truth})
for station, day in scopes:
    rows = c.execute(
        "select decision_at_utc, json_extract(lineage_json,'$.model_center_c'), fresh_until_utc from basket_probability_envelope"
        " where station_id=? and event_date=? and state='COMMITTED' and stage='P0' order by decision_at_utc", (station, day)).fetchall()
    centre[f"{station}|{day}"] = [(r[0], r[1], r[2]) for r in rows if r[1] not in (None, "", "NONE")]
(OUT / "tafs.json").write_text(json.dumps(tafs))
(OUT / "truth.json").write_text(json.dumps({f"{k[0]}|{k[1]}": v for k, v in truth.items()}))
(OUT / "centres.json").write_text(json.dumps(centre))
print("tafs", collections.Counter(t["source"] for t in tafs), "truth station-days", len(truth), "scopes with truth", len(scopes),
      "scopes with P0 centre", sum(1 for v in centre.values() if v))
