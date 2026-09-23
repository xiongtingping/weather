"""Charter 201 step 2: the P0 shadow leaves the served vector byte-identical, and what it costs.

Read-only replay.  For recent COMMITTED P0 envelopes, load the retained packet
the envelope was compiled from and recompile it at its own decision instant
with production's compile_native_ensemble_p0 twice:

  shadow  the working tree as it is (the deterministic-feature receipt on)
  off     the same code with the receipt replaced by a no-op

and compare both served vectors with the served envelope.  Wall time and
tracemalloc peak are taken per compile for each arm (interleaved, so drift
in machine load hits both).
"""
import gzip, json, os, sqlite3, statistics, sys, time, tracemalloc
sys.path.insert(0, "/Users/xiong/weather")
import bayes_basket_target.native_ensemble_p0 as p0

ARCH = "/Users/xiong/weather/data/runtime/bayes_basket_target_nwp/forward_packet_archive"
SINCE = sys.argv[1]
LIMIT = int(sys.argv[2]) if len(sys.argv) > 2 else 60
OUT = sys.argv[3] if len(sys.argv) > 3 else None
conn = sqlite3.connect("file:/Users/xiong/weather/data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=60)
conn.execute("PRAGMA query_only=ON")
stations = [r[0] for r in conn.execute("select station_id from calibration_station_registry order by 1")]
picked = []
for st in stations:
    for eid, day, dec, pj, snap in conn.execute(
            "select envelope_id, event_date, decision_at_utc, probabilities_json, json_extract(lineage_json,'$.native_forward_snapshot_sha256') "
            "from basket_probability_envelope indexed by basket_probability_envelope_station_stage_recency_idx "
            "where station_id=? and stage='P0' and state='COMMITTED' and decision_at_utc>=? order by decision_at_utc desc limit 2", (st, SINCE)):
        path = os.path.join(ARCH, day, f"{st}-{(snap or '')[:16]}.json.gz")
        if snap and os.path.exists(path):
            picked.append((eid, st, day, dec, json.loads(pj), path))
picked = picked[:LIMIT]
real = p0.deterministic_feature_shadow_receipt
rows = []
for eid, st, day, dec, served, path in picked:
    packet = json.loads(gzip.open(path).read())
    out = {}
    for arm in ("shadow", "off", "shadow2", "off2"):
        p0.deterministic_feature_shadow_receipt = real if arm.startswith("shadow") else (lambda conn, **kw: {"state": "OFF"})
        tracemalloc.start()
        t0 = time.perf_counter()
        try:
            compiled = p0.compile_native_ensemble_p0(conn, forward_packet=packet, decision_at_utc=dec)
            env = compiled["packet"]["native_probability_envelope"]
            probs = {k: float(v) for k, v in env["probabilities"].items()}
            lineage = env["lineage"]
            err = None
        except Exception as exc:  # noqa: BLE001
            probs, lineage, err = None, {}, repr(exc)[:200]
        elapsed = time.perf_counter() - t0
        peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
        out[arm] = dict(probs=probs, ms=elapsed * 1000, peak_kb=peak / 1024, err=err,
                        receipt=lineage.get("deterministic_feature_json"))
    p0.deterministic_feature_shadow_receipt = real
    s, o = out["shadow"], out["off"]
    same = s["probs"] is not None and s["probs"] == o["probs"]
    vs_served = None if s["probs"] is None else max(abs(s["probs"][k] - served[k]) for k in served)
    rec = json.loads(s["receipt"]) if s["receipt"] else {}
    rows.append(dict(eid=eid, st=st, day=day, dec=dec, shadow_equals_off=same, max_abs_vs_served=vs_served,
                     err=s["err"] or o["err"], ms_shadow=[s["ms"], out["shadow2"]["ms"]], ms_off=[o["ms"], out["off2"]["ms"]],
                     peak_kb_shadow=s["peak_kb"], peak_kb_off=o["peak_kb"], receipt_state=rec.get("state"),
                     sources=sorted(rec.get("sources") or {}), candidate=rec.get("candidate")))
ok = [r for r in rows if r["err"] is None]
summary = dict(
    since=SINCE, compiled=len(rows), errors=len(rows) - len(ok),
    shadow_equals_off=sum(r["shadow_equals_off"] for r in ok),
    exact_vs_served=sum(1 for r in ok if r["max_abs_vs_served"] == 0.0),
    max_abs_vs_served=max((r["max_abs_vs_served"] for r in ok), default=None),
    median_ms_shadow=statistics.median(min(r["ms_shadow"]) for r in ok) if ok else None,
    median_ms_off=statistics.median(min(r["ms_off"]) for r in ok) if ok else None,
    median_extra_ms=statistics.median(min(r["ms_shadow"]) - min(r["ms_off"]) for r in ok) if ok else None,
    median_peak_kb_shadow=statistics.median(r["peak_kb_shadow"] for r in ok) if ok else None,
    median_peak_kb_off=statistics.median(r["peak_kb_off"] for r in ok) if ok else None,
    receipt_states={s: sum(1 for r in ok if r["receipt_state"] == s) for s in {r["receipt_state"] for r in ok}},
)
print(json.dumps(summary, indent=1))
if OUT:
    json.dump(dict(summary=summary, rows=rows), open(OUT, "w"), indent=1, default=str)
