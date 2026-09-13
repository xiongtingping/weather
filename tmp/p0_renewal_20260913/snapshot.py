"""Read-only: P0 envelopes 2026-09-06..09-13 with the TTL-relevant lineage only."""
import json, sqlite3, time, datetime, resource, shutil, pathlib
OUT = pathlib.Path(__file__).resolve().parent
resource.setrlimit(resource.RLIMIT_FSIZE, (64 * 1024**2, 64 * 1024**2))
assert shutil.disk_usage(OUT).free > 5 * 1024**3
c = sqlite3.connect("file:/Users/xiong/weather/data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=5)
c.execute("pragma query_only=on"); c.execute("pragma cache_size=-16384")
deadline = time.monotonic() + 540
c.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
dates = [(datetime.date(2026, 9, 6) + datetime.timedelta(days=i)).isoformat() for i in range(8)]
stations = [r[0] for r in c.execute("select distinct station_id from calibration_station_market_timezone_binding")]
rows = []
sql = ("select station_id,event_date,settlement_timezone,checkpoint_id,decision_at_utc,fresh_until_utc,"
       "json_extract(lineage_json,'$.archive_query_at_utc_by_system_json'),"
       "json_extract(lineage_json,'$.run_init_utc_by_system_json'),"
       "json_extract(lineage_json,'$.system_weights_json'),"
       "json_extract(lineage_json,'$.native_forward_availability_evidence_json'),"
       "json_extract(lineage_json,'$.native_transport_provider') "
       "from basket_probability_envelope where station_id=? and event_date=? and state='COMMITTED' and stage='P0' "
       "order by decision_at_utc")
docs = {}
for s in stations:
    for d in dates:
        for r in c.execute(sql, (s, d)):
            ref = r[9] or ""
            if ref.startswith("@ref/") and ref not in docs:
                row = c.execute("select document from basket_probability_lineage_document where document_sha256=?", (ref[5:],)).fetchone()
                doc = json.loads(row[0]) if row else None
                docs[ref] = None if doc is None else {k: [v.get("run_init_utc"), v.get("archive_query_at_utc")] for k, v in (doc.get("by_system") or {}).items()}
            rows.append(dict(station_id=r[0], event_date=r[1], tz=r[2], checkpoint_id=r[3], decision=r[4], fresh_until=r[5],
                             seated_query=json.loads(r[6] or "{}"), seated_run=json.loads(r[7] or "{}"),
                             weights=json.loads(r[8] or "{}"), availability=ref, provider=r[10]))
(OUT / "p0_rows.json").write_text(json.dumps(rows))
(OUT / "availability_docs.json").write_text(json.dumps(docs))
print("rows", len(rows), "docs", len(docs), "stations", len(stations))
