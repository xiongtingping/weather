"""Station regional features, step 5: one table of the per-station readings (main run and the +6 h knowable sensitivity)."""
import json, os
HERE = os.path.dirname(os.path.abspath(__file__))
R = json.load(open(os.path.join(HERE, "results.json"))); L = json.load(open(os.path.join(HERE, "results_lag6.json")))
r3 = lambda t: None if t is None else [round(x, 3) for x in t[:3]]
rows = []
for st, ms in R["stations"].items():
    for m, res in ms.items():
        b = res.get("evaluable_all")
        if not b:
            continue
        mk = b.get("market", {}); bl = (L["stations"].get(st, {}).get(m) or {}).get("evaluable_all")
        rows.append(dict(station=st, model=m, event_days=b["n_event_days"], cells=b["n_cells"], envelopes=b["n_envelopes"],
                         k_median=round(b["k_median"], 2), corr_err_vs_d=r3(res.get("evaluable_corr_err_vs_d")),
                         mae_base=round(b["mae"]["base"], 2), mae_roll=round(b["mae"]["roll"], 2),
                         market_paired_days=mk.get("n_event_days"), mae_market=mk.get("mae_market"),
                         gap_mae_base=mk.get("gap_mae_base"), gap_mae_roll=mk.get("gap_mae_roll"),
                         rps_base=round(b["rps"]["base"], 3), d_rps_roll=r3(b["d_rps_roll"]), d_rps_fixed=r3(b["d_rps_fixed"]),
                         d_rps_roll_minus_rsign=r3(b["d_rps_roll_minus_rsign"]), d_rps_roll_drop_best2=r3(b["d_rps_roll_drop_best2"]),
                         d_nll_nz_roll=r3(b["d_nll_nz_roll"]), gap_rps_base=mk.get("gap_rps_base"), gap_rps_roll=mk.get("gap_rps_roll"),
                         d_rps_roll_lag_plus6h=r3(bl["d_rps_roll"]) if bl else None))
json.dump(dict(window="P0 envelopes 2026-08-01..09-21, clean (no bias_refused), hours to settlement >= 12; rolling k from >= 10 earlier knowable event days",
               across_stations=R["across_stations"], across_stations_lag_plus6h=L["across_stations"], table=rows),
          open(os.path.join(HERE, "results_summary.json"), "w"), indent=1)
for r in rows:
    print(r["station"], r["model"], r["event_days"], r["d_rps_roll"], r["d_rps_roll_drop_best2"], r["d_rps_roll_lag_plus6h"])
