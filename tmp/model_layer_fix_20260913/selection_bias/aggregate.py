import json, random, statistics
from collections import Counter, defaultdict
from pathlib import Path

BASE = Path(__file__).resolve().parent
DIRS = {
    "2026-09-03": "stations_20260903T15", "2026-09-05": "stations_20260905T15",
    "2026-09-07": "stations_20260907T15", "2026-09-09": "stations_20260909T15",
    "2026-09-11": "stations_20260911T15", "2026-09-13": "stations",
}


def _documents(origin):
    directory = BASE / DIRS[origin]
    if directory.exists():
        for p in sorted(directory.glob("*.json")):
            yield json.loads(p.read_text())
        return
    import gzip
    with gzip.open(BASE / "stations.jsonl.gz", "rt") as handle:
        for line in handle:
            doc = json.loads(line)
            if doc["origin"] == origin:
                yield doc


def load(origin):
    recs = []
    for d in _documents(origin):
        for r in d["records"]:
            r["station"] = d["station_id"]
            r["origin"] = origin
            recs.append(r)
    return recs


def unique_scored(recs):
    seen, out = set(), []
    for r in recs:
        if r["evaluated_subset_count"] <= 1 or r.get("state") != "SCORED":
            continue
        k = (r["origin"], r["station"], tuple(r["holdout_c"]), tuple(r["ready"]),
             tuple(r["holdout_s"]), round(r["a_on_c"]["crps"], 12))
        if k not in seen:
            seen.add(k)
            out.append(r)
    return out


def rel(r, x, y, m="crps"):
    return (r[x][m] - r[y][m]) / r[y][m]


def cluster_boot(rows, fn, key="station", n=2000, seed=7):
    groups = defaultdict(list)
    for r in rows:
        groups[(r["origin"], r[key])].append(r)
    keys = sorted(groups)
    rng = random.Random(seed)
    vals = []
    for _ in range(n):
        sample = [r for k in (rng.choice(keys) for _ in keys) for r in groups[k]]
        vals.append(fn(sample))
    vals.sort()
    return vals[int(0.05 * n)], vals[int(0.95 * n)]


def mean_rel(x, y, m="crps"):
    return lambda rows: statistics.mean(rel(r, x, y, m) for r in rows)


summary = {}
all_rows = []
for origin in DIRS:
    recs = load(origin)
    multi = [r for r in recs if r["evaluated_subset_count"] > 1]
    rows = unique_scored(recs)
    all_rows += rows
    g = [r for r in rows if "gate_a" in r]
    full = [r for r in rows if "full_on_c" in r]
    summary[origin] = {
        "contests": len(recs),
        "enumerating": len(multi),
        "enumerating_with_selection_window": sum(r.get("state") == "SCORED" for r in multi),
        "scored_unique": len(rows),
        "winner_changes": round(sum(r["a_ids"] != r["b_ids"] for r in rows) / max(1, len(rows)), 3),
        "crps_B_vs_A_mean_rel": round(statistics.mean(rel(r, "b_on_c", "a_on_c") for r in rows), 4) if rows else None,
        "crps_B_vs_A_median_rel": round(statistics.median(rel(r, "b_on_c", "a_on_c") for r in rows), 4) if rows else None,
        "nll_B_vs_A_mean_rel": round(statistics.mean(rel(r, "b_on_c", "a_on_c", "nll") for r in rows), 4) if rows else None,
        "gate_pass_A": sum(r["gate_a"] for r in g),
        "gate_pass_B": sum(r.get("gate_b", False) for r in g),
        "passes_only_by_selection": sum(r["gate_a"] and not r.get("gate_b") for r in g),
        "crps_full_vs_B_mean_rel": round(statistics.mean(rel(r, "full_on_c", "b_on_c") for r in full), 4) if full else None,
        "crps_full_vs_A_mean_rel": round(statistics.mean(rel(r, "full_on_c", "a_on_c") for r in full), 4) if full else None,
    }
for origin, s in summary.items():
    print(origin, json.dumps(s))

primary = [r for r in all_rows if r["origin"] in ("2026-09-03", "2026-09-07", "2026-09-11")]
full = [r for r in primary if "full_on_c" in r]
g = [r for r in primary if "gate_a" in r]
pooled = {
    "origins": ["2026-09-03", "2026-09-07", "2026-09-11"],
    "scored_unique": len(primary),
    "stations": len({r["station"] for r in primary}),
    "winner_changes": round(sum(r["a_ids"] != r["b_ids"] for r in primary) / len(primary), 3),
    "crps_B_vs_A_mean_rel": round(mean_rel("b_on_c", "a_on_c")(primary), 4),
    "crps_B_vs_A_90ci_station_cluster": [round(v, 4) for v in cluster_boot(primary, mean_rel("b_on_c", "a_on_c"))],
    "nll_B_vs_A_mean_rel": round(mean_rel("b_on_c", "a_on_c", "nll")(primary), 4),
    "nll_B_vs_A_90ci": [round(v, 4) for v in cluster_boot(primary, mean_rel("b_on_c", "a_on_c", "nll"))],
    "crps_full_vs_B_mean_rel": round(mean_rel("full_on_c", "b_on_c")(full), 4),
    "crps_full_vs_B_90ci": [round(v, 4) for v in cluster_boot(full, mean_rel("full_on_c", "b_on_c"))],
    "nll_full_vs_B_mean_rel": round(mean_rel("full_on_c", "b_on_c", "nll")(full), 4),
    "nll_full_vs_B_90ci": [round(v, 4) for v in cluster_boot(full, mean_rel("full_on_c", "b_on_c", "nll"))],
    "gate_pass_A": sum(r["gate_a"] for r in g),
    "gate_pass_B": sum(r.get("gate_b", False) for r in g),
    "gate_records": len(g),
    "passes_only_by_selection": sum(r["gate_a"] and not r.get("gate_b") for r in g),
}
print("POOLED", json.dumps(pooled, indent=1))
(BASE / "summary.json").write_text(json.dumps({"by_origin": summary, "pooled_non_overlapping": pooled}, indent=1, sort_keys=True))
