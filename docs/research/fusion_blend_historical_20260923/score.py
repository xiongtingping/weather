#!/usr/bin/env python3
"""Pass 2: the charter 199 blend against the seat, on pass 1's updates.

Training rows are pass 1's day-ahead roots in the production training
table's own schema (a temporary database), and every update's weights come
from ``fusion_blend.overlay_system_weights(mode=blend)`` itself -- the
serving function, not a copy of it.  Both arms are then blended and scored
by the replay's ``_blend_and_score`` with the tables pickled at the same
update hour.  Writes results.json.  See PROTOCOL.md.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import math
import pickle
import sqlite3
import sys
import tempfile
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
for extra in (ROOT, ROOT / "scripts"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import replay_p0_stable_pool as replay  # noqa: E402
from bayes_basket_target import fusion_blend as FB  # noqa: E402
from bayes_basket_target import native_ensemble_oos as oos  # noqa: E402
from calibration_dashboard import _core_murphy  # noqa: E402

PASS1 = HERE / "pass1"
FAMILY_CAP = 0.75
BOOT = 2000
BLOCK = 3
RESEARCH_SPAN = ("2026-08-14", "2026-09-22")  # the fusion study's own days
STABLE_POOL_FROM = "2026-09-14"


def _stamp(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _intervals(keys):
    ints = sorted(int(k) for k in keys)
    full = [str(k) for k in range(ints[0], ints[-1] + 1)]
    out = {}
    for i, key in enumerate(full):
        lo = None if i == 0 else float(key) - 0.5
        hi = None if i == len(full) - 1 else float(key) + 0.5
        out[key] = [lo, hi]
    return full, out


def load_pass1():
    rows = []
    for path in sorted(PASS1.glob("*.jsonl.gz")):
        with gzip.open(path, "rt") as handle:
            rows.extend(json.loads(line) for line in handle)
    return rows


def training_db(records, path: Path) -> int:
    """Day-ahead roots, in the production training table's schema."""

    by_day = defaultdict(list)
    for r in records:
        by_day[(r["station"], r["event_date"])].append(r)
    conn = sqlite3.connect(path)
    for ddl in FB.TRAINING_DDL:
        conn.execute(ddl)
    written = 0
    for (station, day), recs in by_day.items():
        if station in FB.FUSION_BLEND_UNBOUND_LABEL_STATIONS:
            continue
        zone = ZoneInfo(recs[0]["tz"])
        midnight = datetime.fromisoformat(day).replace(tzinfo=zone)
        roots = [r for r in recs if midnight - timedelta(days=1) <= _stamp(r["at"]) < midnight]
        if not roots:
            continue
        root = max(roots, key=lambda r: r["at"])
        pool = root["pool"]
        keys, intervals = _intervals(set().union(*(root["day_rows"][s]["p"] for s in pool)))
        probs = {s: {k: float(root["day_rows"][s]["p"].get(k, 0.0)) for k in keys} for s in pool}
        stats = FB.row_statistics(
            intervals=intervals, truth_c=float(root["actual"]),
            centers_c={s: root["day_rows"][s]["c"] for s in pool},
            system_probabilities=probs, systems=pool,
        )
        record = {
            "station_id": station, "event_date": day, "truth_revision": 0,
            "settlement_timezone": root["tz"], "root_decision_at_utc": root["at"],
            "root_checkpoint_id": "replay", "root_envelope_sha256": "0" * 64,
            "root_posterior_version": "replay", "truth_c": float(root["actual"]),
            "truth_known_at_utc": root["truth_known_at"], "truth_id": "replay",
            "systems_json": json.dumps(pool),
            "lead_by_system_json": json.dumps({s: root["scopes"][s][0] for s in pool}),
            "center_error_json": json.dumps(stats["center_error"]),
            "crps_systems_json": json.dumps(stats["crps_systems"]),
            "crps_a_json": json.dumps(stats["crps_a"]),
            "crps_b_json": json.dumps(stats["crps_b"]),
            "probability_source": "replay", "seat_weights_json": json.dumps(root["seat"]["w"]),
            "family_by_system_json": json.dumps(root["family"]),
            "family_caps_json": "{}", "bucket_keys_json": json.dumps(keys),
            "truth_bucket_index": 0, "probabilities_json": "{}",
            "fusion_blend_state": "REPLAY", "shadow_weights_json": "{}",
            "schema": FB.FUSION_BLEND_TRAINING_SCHEMA, "created_at_utc": root["at"],
        }
        record["content_sha256"] = hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest()
        cols = list(record)
        conn.execute(
            f"INSERT INTO fusion_blend_training_fact({','.join(cols)}) VALUES({','.join('?' for _ in cols)})",
            [record[c] for c in cols],
        )
        written += 1
    conn.commit()
    conn.close()
    return written


def blend_arm(records, db: Path):
    tables_by_station: dict[str, dict] = {}
    states: Counter = Counter()
    moved = []
    for i, r in enumerate(records):
        seat = {s: float(v) for s, v in r["seat"]["w"].items()}
        weights, receipt = FB.overlay_system_weights(
            station_id=r["station"], event_date=r["event_date"], decision_at_utc=r["at"],
            timezone_name=r["tz"], system_weights=seat, family_by_system=r["family"],
            family_caps={f: FAMILY_CAP for f in set(r["family"].values())},
            lead_by_system={s: r["scopes"][s][0] for s in r["pool"]},
            training_db=db, mode=FB.FUSION_BLEND_MODE_BLEND,
        )
        states[receipt["state"]] += 1
        r["blend_state"] = receipt["state"]
        if receipt["state"] != "APPLIED":
            r["blend"] = r["seat"]
            continue
        moved.append(receipt.get("moved_half_l1", 0.0))
        station = r["station"]
        if station not in tables_by_station:
            with open(PASS1 / f"{station}.tables.pkl", "rb") as handle:
                tables_by_station = {station: pickle.load(handle)}
        tables = tables_by_station[station][r["hour_key"]]
        day_rows = {s: {"probability": r["day_rows"][s]["p"], "calibrated_center_c": r["day_rows"][s]["c"]} for s in r["pool"]}
        positive = {s: v for s, v in weights.items() if v > 0.0}
        scored = replay._blend_and_score(
            weights=positive, day_rows=day_rows, cooling_shift_sigma=r["cooling"],
            latitude=r["latitude"], tables=tables, actual=r["actual"],
        )
        r["blend"] = {"w": scored["w"], "mean": scored["mean"], "served": scored["served"], "vec": scored["vec"]}
        if i % 2000 == 0:
            print(f"  {i}/{len(records)}", flush=True)
    return states, moved


def arm_scores(records, arm):
    """Station-day-weighted RMSE, CRPS, NLL, Brier, REL, RES for one arm."""

    copies = Counter((r["station"], r["event_date"]) for r in records)
    w = np.array([1.0 / copies[(r["station"], r["event_date"])] for r in records])
    err = np.array([r[arm]["mean"] - r["actual"] for r in records])
    crps = np.array([r[arm]["served"][0] for r in records])
    nll = np.array([r[arm]["served"][1] for r in records])
    brier = np.array([r[arm]["served"][2] for r in records])
    cells = []
    for weight, r in zip(w, records):
        vec = r[arm]["vec"]
        hit_key = oos._temperature_bucket(r["actual"])
        keys = sorted(set(vec) | {hit_key}, key=int)
        cells.append((weight, {"probabilities": [float(vec.get(k, 0.0)) for k in keys], "hit": keys.index(hit_key)}))
    murphy = _core_murphy(cells)
    total = w.sum()
    return {
        "rmse": math.sqrt(float((w * err * err).sum() / total)),
        "crps": float((w * crps).sum() / total), "nll": float((w * nll).sum() / total),
        "brier": float((w * brier).sum() / total),
        "rel": murphy["reliability"], "res": murphy["resolution"],
    }, {"w": w, "sq": err * err, "crps": crps, "nll": nll}


def compare(records, label):
    if not records:
        return None
    seat, s_arr = arm_scores(records, "seat")
    blend, b_arr = arm_scores(records, "blend")
    days = sorted({r["event_date"] for r in records})
    day_idx = defaultdict(list)
    for i, r in enumerate(records):
        day_idx[r["event_date"]].append(i)
    rng = np.random.default_rng(20260923)
    boots = {"rmse": [], "crps": [], "nll": []}
    n_blocks = math.ceil(len(days) / BLOCK)
    for _ in range(BOOT):
        idx = np.concatenate([
            day_idx[days[(s + j) % len(days)]]
            for s in rng.integers(0, len(days), n_blocks) for j in range(BLOCK)
        ])
        w = s_arr["w"][idx]
        tot = w.sum()
        boots["rmse"].append(math.sqrt((w * b_arr["sq"][idx]).sum() / tot) - math.sqrt((w * s_arr["sq"][idx]).sum() / tot))
        boots["crps"].append(float((w * (b_arr["crps"][idx] - s_arr["crps"][idx])).sum() / tot))
        boots["nll"].append(float((w * (b_arr["nll"][idx] - s_arr["nll"][idx])).sum() / tot))
    delta = {k: blend[k] - seat[k] for k in seat}
    rel_change = {k: (blend[k] - seat[k]) / seat[k] if seat[k] else None for k in seat}
    passes = (delta["rmse"] < 0 and delta["crps"] < 0 and delta["nll"] < 0
              and delta["rel"] < 0 and delta["res"] > 0)
    return {
        "label": label, "updates": len(records),
        "station_days": len({(r["station"], r["event_date"]) for r in records}),
        "event_days": len(days), "first_day": days[0], "last_day": days[-1],
        "seat": seat, "blend": blend, "delta": delta, "relative_change": rel_change,
        "ci95": {k: [float(np.percentile(v, 2.5)), float(np.percentile(v, 97.5))] for k, v in boots.items()},
        "passes_switch_rule": bool(passes),
    }


def main() -> int:
    started = time.time()
    records = load_pass1()
    records.sort(key=lambda r: (r["station"], r["at"]))
    print("updates", len(records), "stations", len({r["station"] for r in records}))
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "fusion_blend_training_replay.db"
        written = training_db(records, db)
        print("training rows", written)
        states, moved = blend_arm(records, db)
    print("states", dict(states), f"{time.time() - started:.0f}s")
    roots = []
    by_day = defaultdict(list)
    for r in records:
        by_day[(r["station"], r["event_date"])].append(r)
    for (station, day), recs in by_day.items():
        midnight = datetime.fromisoformat(day).replace(tzinfo=ZoneInfo(recs[0]["tz"]))
        before = [r for r in recs if midnight - timedelta(days=1) <= _stamp(r["at"]) < midnight]
        if before:
            roots.append(max(before, key=lambda r: r["at"]))
    unseen = [r for r in records if not (RESEARCH_SPAN[0] <= r["event_date"] <= RESEARCH_SPAN[1])]
    result = {
        "protocol": "PROTOCOL.md",
        "training_rows": written,
        "blend_states": dict(states),
        "moved_half_l1_median": float(np.median(moved)) if moved else None,
        "moved_half_l1_p90": float(np.percentile(moved, 90)) if moved else None,
        "all_updates": compare(records, "全部更新（主读数）"),
        "day_ahead_roots": compare(roots, "日前根"),
        "applied_only": compare([r for r in records if r["blend_state"] == "APPLIED"], "只算融合真的改了权重的更新"),
        "unseen_by_study": compare(unseen, "研究没用过的站日"),
        "stable_pool_era": compare([r for r in records if r["event_date"] >= STABLE_POOL_FROM], "宪章 134 之后"),
        "asos_excluded": compare([r for r in records if r["station"] not in FB.FUSION_BLEND_UNBOUND_LABEL_STATIONS], "去掉 ASOS 11 站"),
        "seconds": round(time.time() - started),
    }
    (HERE / "results.json").write_text(json.dumps(result, ensure_ascii=False, indent=1))
    for key in ("all_updates", "day_ahead_roots", "applied_only", "unseen_by_study", "stable_pool_era", "asos_excluded"):
        c = result[key]
        if not c:
            continue
        rc = c["relative_change"]
        print(f"{c['label']}: {c['station_days']} sd / {c['event_days']} d | RMSE {rc['rmse']:+.2%} CRPS {rc['crps']:+.2%} "
              f"NLL {rc['nll']:+.2%} REL {rc['rel']:+.1%} RES {rc['res']:+.1%} | pass={c['passes_switch_rule']} ci={c['ci95']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
