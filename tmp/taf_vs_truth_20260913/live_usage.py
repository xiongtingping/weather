"""Read-only: which TAF touch points actually fire on recent P0 / P0-M rows of the TX stations."""
import json, sqlite3, time, collections
TX = "FACT KBKF LEMD MMMX RKPK RKSI RPLL SAEZ SBGR ZBAA ZGGG ZGSZ ZHHH ZSPD ZSQD ZUCK ZUUU".split()
DAYS = ["2026-09-10", "2026-09-11", "2026-09-12", "2026-09-13", "2026-09-14"]
c = sqlite3.connect("file:/Users/xiong/weather/data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=5)
c.execute("pragma query_only=on")
deadline = time.monotonic() + 540
c.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
cnt = collections.Counter(); methods = collections.Counter()
for stn in TX:
    for day in DAYS:
        for stage in ("P0", "P0-M"):
            for (lin,) in c.execute("select lineage_json from basket_probability_envelope where station_id=? and event_date=? and stage=? and state='COMMITTED'", (stn, day, stage)):
                L = json.loads(lin); cnt[(stage, "rows")] += 1
                if stage == "P0":
                    w = json.loads(L.get("system_weights_json") or "{}")
                    cnt[(stage, "taf_member_in_weights")] += "OFFICIAL_AIRPORT_TAF_TX" in w
                    cnt[(stage, "taf_member_weight>0")] += float(w.get("OFFICIAL_AIRPORT_TAF_TX", 0) or 0) > 0
                    cnt[(stage, "envelope_extended=" + str(L.get("taf_envelope_extended")))] += 1
                    sc = json.loads(L.get("synoptic_cooling_json") or "null")
                    cnt[(stage, "synoptic=" + ("none" if sc is None else f"{sc.get('method_tag')}:{sc.get('state')}"))] += 1
                    tr = L.get("tail_reliability_json")
                    sig = json.loads(tr).get("signals") if tr else None
                    cnt[(stage, "tail_skew=" + ("absent" if sig is None else ",".join(s for s in sig if s.startswith("taf")) or "no_taf_signal"))] += 1
                    m = str(L.get("combination_method") or L.get("artifact_combination_method") or "")
                    methods[m[:160]] += 1
                else:
                    cnt[(stage, "mode=" + str(L.get("revision_mode")))] += 1
                    cnt[(stage, "tx=" + ("none" if L.get("taf_observed_tx_c") in (None, "NONE") else "yes"))] += 1
                    cnt[(stage, "width_cell=" + str(L.get("center_width_taf_cell")))] += 1
for k in sorted(cnt): print(k, cnt[k])
print("method tags seen:", methods.most_common(5))
print("center width P0-M taf cells:", c.execute("select stage, count(*), sum(taf_cells_json is not null and taf_cells_json not in ('', '{}', 'null')) from basket_center_width_table group by stage").fetchall() if c.execute("select 1 from sqlite_master where name='basket_center_width_table'").fetchone() else [r[0] for r in c.execute("select name from sqlite_master where sql like '%taf_cells_json%'")])
