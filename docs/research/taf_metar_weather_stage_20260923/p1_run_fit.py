"""Scratch only: run the production P1 fitter, optionally with the TAF forecast fields in the direct update's state."""
import importlib.util, sys, json
ROOT = "/Users/xiong/weather"; sys.path.insert(0, ROOT)
mode, samples, out = sys.argv[1], sys.argv[2], sys.argv[3]
spec = importlib.util.spec_from_file_location("_p1fit", f"{ROOT}/scripts/fit_p1_observation_likelihood.py")
fit = importlib.util.module_from_spec(spec); spec.loader.exec_module(fit)
from bayes_basket_target import p1_direct_update as DU
EXTRA = ("taf_cloud_cover_fraction", "taf_precipitation_probability", "taf_wind_speed_mps")
if mode == "surprise":
    EXTRA = ("cloud_surprise", "precip_surprise")
    DU.FEATURES = tuple(DU.FEATURES) + EXTRA
    DU.STATE_COLUMNS = DU.FEATURES + DU.ROOT_FEATURES
    base_features_of = fit.features_of
    def features_ext(sample):
        f = base_features_of(sample)
        tc, tp = sample.get("taf_cloud_cover_fraction"), sample.get("taf_precipitation_probability")
        f["cloud_surprise"] = None if tc is None else float(sample["cloud_cover_fraction"]) - float(tc)
        f["precip_surprise"] = None if tp is None else float(sample["precipitation_present"]) - float(tp)
        return f
    base_dd = fit.direct_design
    def dd(rows, clock):
        fit.features_of = features_ext
        try: return base_dd(rows, clock)
        finally: fit.features_of = base_features_of
    fit.direct_design = dd
if mode == "taf":
    DU.FEATURES = tuple(DU.FEATURES) + EXTRA
    DU.STATE_COLUMNS = DU.FEATURES + DU.ROOT_FEATURES
    base_features_of = fit.features_of
    def features_ext(sample):
        f = base_features_of(sample)
        for k in EXTRA: f[k] = None if sample.get(k) is None else float(sample[k])
        return f
    base_dd = fit.direct_design
    def dd(rows, clock):
        fit.features_of = features_ext
        try: return base_dd(rows, clock)
        finally: fit.features_of = base_features_of
    fit.direct_design = dd
sys.argv = ["fit", "--samples", samples, "--out", out, "--since", "2026-08-10", "--through", "2026-09-23",
            "--fitted-at-utc", "2026-09-23T00:00:00Z", "--force"]
rc = fit.main()
a = json.load(open(out)); d = a["direct_update"]; w = d["walk_forward"]
print(json.dumps({"mode": mode, "rc": rc, "state": d["state"], "veto": d["veto"], "lambda": d["lambda"], "rows": d["rows"],
                  "days": w["days"], "gain_vs_root": w["gain_vs_root_nat"], "gain_vs_alpha": w["gain_vs_alpha_nat"], "ci": w["gain_vs_alpha_ci"],
                  "x_width": len(d["x_layout"])}))
json.dump(w, open(out + ".walk.json", "w"))
