#!/usr/bin/env python3
"""Cases for the TAF physics retest (PROTOCOL.md), SSOT read-only.

Per settled station-day the first P0-M, its TAF found in ``taf_raw_archive`` by
the receipt's ``forecast_source_object_sha256`` (AWC-decoded rows only), the
features of PROTOCOL.md, the observed bound, the truth, and two roots: the new
(charter 281 rebuild) and the old (the P0 vector actually served).

    python3 docs/research/taf_physics_20261025/collect.py
"""

from __future__ import annotations

import glob
import json
import math
import pickle
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[3]
for extra in (ROOT, ROOT / "scripts"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import fit_taf_likelihood as TAFFIT  # noqa: E402
from bayes_basket_target import p0_ngr as NGR  # noqa: E402

HERE = Path(__file__).resolve().parent
DB = ROOT / "data" / "bayes_basket_target_ssot.db"
REPLAY = ROOT / "data" / "runtime" / "bayes_basket_target_nwp" / "p0_ngr_root_replay.db"
ARCHIVE = ROOT / "data" / "runtime" / "taf_raw_archive"
COVER = {"FEW": 0.19, "SCT": 0.44, "BKN": 0.75, "OVC": 1.0, "VV": 1.0}
PRECIP = ("RA", "SN", "DZ", "SH", "TS", "GR", "PL", "GS", "UP")
P0M_SQL = (
    "SELECT station_id, event_date, decision_at_utc, settlement_timezone, bucket_ordinals_json,"
    " json_extract(lineage_json,'$.bucket_intervals_c_json'),"
    " json_extract(lineage_json,'$.forecast_source_object_sha256'),"
    " json_extract(lineage_json,'$.observed_hard_excluded_buckets'),"
    " json_extract(lineage_json,'$.p0_root_checkpoint_id')"
    " FROM basket_probability_envelope WHERE stage='P0-M' AND state='COMMITTED'"
    " AND event_date>=? AND event_date<=? ORDER BY decision_at_utc"
)
ROOT_SQL = "SELECT probabilities_json, json_extract(lineage_json,'$.deterministic_consensus_json') FROM basket_probability_envelope WHERE checkpoint_id=? LIMIT 1"


def archive_index() -> dict[str, dict]:
    out = {}
    for path in sorted(glob.glob(str(ARCHIVE / "*.jsonl"))):
        for line in open(path, encoding="utf-8"):
            try:
                row = json.loads(line)
            except ValueError:
                continue
            awc = row.get("awc_row") or {}
            if not awc.get("fcsts"):
                continue
            for key in (row.get("raw_taf_sha256"), row.get("response_sha256")):
                if key:
                    out[key] = awc
    return out


def _cover(clouds) -> tuple[float, float, bool]:
    total = low = 0.0
    conv = False
    for layer in clouds or []:
        c = COVER.get(str(layer.get("cover") or "").upper(), 0.0)
        total = max(total, c)
        base = layer.get("base")
        if base is not None and float(base) < 3000:
            low = max(low, c)
        if str(layer.get("type") or "").upper() in ("CB", "TCU"):
            conv = True
    return total, low, conv


def _visib_m(v) -> float | None:
    if v is None:
        return None
    s = str(v).replace("+", "")
    try:
        x = float(s)
    except ValueError:
        return None
    return x * 1609.34 if x <= 15 else x


def features(awc: dict, *, day: str, tz: str, mu: float) -> dict[str, float] | None:
    zone = ZoneInfo(tz)
    fc = awc.get("fcsts") or []
    base = [g for g in fc if (g.get("fcstChange") in (None, "FM", "BECMG")) and g.get("probability") is None]
    temp = [g for g in fc if g.get("fcstChange") in ("TEMPO", "PROB") or g.get("probability") is not None]
    if not base:
        return None
    y, m, d = (int(x) for x in day.split("-"))

    def at(hour: int) -> float:
        return datetime(y, m, d, hour, 30, tzinfo=zone).timestamp()

    def base_at(t: float):
        cur = None
        for g in sorted(base, key=lambda g: g.get("timeFrom") or 0):
            start = g.get("timeBec") if g.get("fcstChange") == "BECMG" and g.get("timeBec") else g.get("timeFrom")
            if start is not None and start <= t:
                cur = g
        return cur if cur is not None and (cur.get("timeTo") or 0) > t else cur

    window = [at(h) for h in range(10, 17)]
    clouds, lows, precips, wspds, gusts, us, vs = [], [], [], [], [], [], []
    conv = ptempo = False
    n_change = 0
    for t in window:
        g = base_at(t)
        if g is None:
            continue
        c, lo, cv = _cover(g.get("clouds"))
        wx = str(g.get("wxString") or "")
        clouds.append(c)
        lows.append(lo)
        precips.append(1.0 if any(p in wx for p in PRECIP) else 0.0)
        conv = conv or cv or "TS" in wx
        spd = float(g.get("wspd") or 0.0)
        wspds.append(spd)
        gusts.append(float(g.get("wgst") or 0.0))
        wdir = g.get("wdir")
        if isinstance(wdir, (int, float)) and spd > 0:
            us.append(spd * math.sin(math.radians(float(wdir))))
            vs.append(spd * math.cos(math.radians(float(wdir))))
    if not clouds:
        return None
    for g in temp:
        if (g.get("timeFrom") or 0) < window[-1] and (g.get("timeTo") or 0) > window[0]:
            n_change += 1
            wx = str(g.get("wxString") or "")
            if any(p in wx for p in PRECIP):
                ptempo = True
            _c, _lo, cv = _cover(g.get("clouds"))
            conv = conv or cv or "TS" in wx
    fog = False
    for h in range(5, 10):
        t = at(h)
        for g in [base_at(t)] + [g for g in temp if (g.get("timeFrom") or 0) <= t < (g.get("timeTo") or 0)]:
            if g is None:
                continue
            vis = _visib_m(g.get("visib"))
            wx = str(g.get("wxString") or "")
            if (vis is not None and vis < 5000) or "FG" in wx or "BR" in wx:
                fog = True
    tx = tn = tx_hour = None
    for g in fc:
        for tt in g.get("temp") or []:
            if tt.get("maxOrMin") == "MAX" and tt.get("surfaceTemp") is not None:
                local = datetime.fromtimestamp(tt["validTime"], tz=timezone.utc).astimezone(zone)
                if local.date().isoformat() == day:
                    tx, tx_hour = float(tt["surfaceTemp"]), local.hour + local.minute / 60.0
            if tt.get("maxOrMin") == "MIN" and tt.get("surfaceTemp") is not None:
                tn = float(tt["surfaceTemp"])
    mean_speed = sum(wspds) / len(wspds)
    u = sum(us) / len(window) if us else 0.0
    v = sum(vs) / len(window) if vs else 0.0
    norm = math.hypot(u, v)
    return {
        "cloud": sum(clouds) / len(clouds), "low_cloud": sum(lows) / len(lows),
        "precip": sum(precips) / len(precips), "precip_tempo": float(ptempo), "convection": float(conv),
        "wspd": mean_speed, "gust": max(gusts), "fog": float(fog),
        "tx_warm": max(tx - mu, 0.0) if tx is not None else 0.0,
        "tx_cold": min(tx - mu, 0.0) if tx is not None else 0.0,
        "tx_abs": abs(tx - mu) if tx is not None else 0.0,
        "tx_hour": (tx_hour - 14.0) if tx_hour is not None else 0.0,
        "dtr_raw": (tx - tn) if (tx is not None and tn is not None) else None,
        "wind_u": u / norm if norm > 0 else 0.0, "wind_v": v / norm if norm > 0 else 0.0,
        "n_change": float(n_change), "has_tx": float(tx is not None),
    }


def _epoch(value) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def covers(awc: dict, *, day: str, tz: str) -> bool:
    """The bulletin is valid over the whole 10-17 local window of ``day``."""

    zone = ZoneInfo(tz)
    y, m, d = (int(x) for x in day.split("-"))
    lo = datetime(y, m, d, 10, 0, tzinfo=zone).timestamp()
    hi = datetime(y, m, d, 17, 0, tzinfo=zone).timestamp()
    start, end = _epoch(awc.get("validTimeFrom")), _epoch(awc.get("validTimeTo"))
    return start is not None and end is not None and start <= lo and end >= hi


def moments(probs: dict, ordered: list, intervals: dict) -> tuple[float, float]:
    geom = NGR.geometry(ordered, intervals)
    return NGR.vector_moments([probs[k] for k in ordered], geom)


def main() -> int:
    index = archive_index()
    print("archived TAFs with groups", len(index), flush=True)
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=120)
    replay = sqlite3.connect(f"file:{REPLAY}?mode=ro", uri=True)
    truth = TAFFIT.load_truth_revisions(conn)
    # PROTOCOL 补记一: the first P0-M whose TAF covers the day's 10-17 local window.
    first: dict[tuple, tuple] = {}
    for row in conn.execute(P0M_SQL, ("2026-08-20", "2026-10-24")):
        key = (row[0], row[1])
        if key not in truth or key in first:
            continue
        awc = index.get(row[6])
        if awc is None or not covers(awc, day=row[1], tz=row[3]):
            continue
        first[key] = row
    cases = []
    counts = {"station_days": len(first), "no_taf": 0, "no_features": 0, "truth_excluded": 0, "no_root": 0}
    for (station, day), (_s, _d, at, tz, oj, ivj, sha, excl, root_id) in sorted(first.items()):
        awc = index.get(sha)
        if awc is None:
            counts["no_taf"] += 1
            continue
        ordinals, intervals = json.loads(oj), json.loads(ivj)
        ordered = sorted(ordinals, key=lambda k: ordinals[k])
        labels = TAFFIT._revision_labels(truth[(station, day)], lineage={"bucket_intervals_c_json": ivj}, ordinals=ordinals)
        if not labels:
            continue
        excluded = [b for b in str(excl or "").split(",") if b and b != "NONE" and b in ordinals]
        if labels[-1][3] in excluded:
            counts["truth_excluded"] += 1
            continue
        roots = {}
        hit = replay.execute("SELECT mu_c, sd_c, probabilities_json FROM root WHERE checkpoint_id=?", (root_id,)).fetchone()
        if hit is not None:
            p = json.loads(hit[2])
            roots["new"] = {"mu": float(hit[0]), "sd": float(hit[1]), "p": [float(p[k]) for k in ordered]}
        old = conn.execute(ROOT_SQL, (root_id,)).fetchone()
        if old is not None:
            p = {k: float(v) for k, v in json.loads(old[0]).items()}
            pre = json.loads(old[1]).get("pre_shift_probabilities") if old[1] and '"SERVED"' in old[1] else None
            if pre:
                p = {k: float(v) for k, v in pre.items()}
            if set(p) == set(ordered):
                mu, sd = moments(p, ordered, intervals)
                roots["old"] = {"mu": mu, "sd": sd, "p": [p[k] for k in ordered]}
        if not roots:
            counts["no_root"] += 1
            continue
        feats = {name: features(awc, day=day, tz=tz, mu=r["mu"]) for name, r in roots.items()}
        if any(f is None for f in feats.values()):
            counts["no_features"] += 1
            continue
        cases.append({
            "station": station, "day": day, "decided_at": at, "ordered": ordered,
            "tlo": [intervals[k][0] if intervals[k][0] is not None else -math.inf for k in ordered],
            "thi": [intervals[k][1] if intervals[k][1] is not None else math.inf for k in ordered],
            "excluded": [ordered.index(b) for b in excluded], "winner": ordered.index(labels[-1][3]),
            "roots": roots, "features": feats,
        })
    conn.close()
    (HERE / "cases.pkl").write_bytes(pickle.dumps(cases))
    new = sum(1 for c in cases if "new" in c["roots"])
    print(json.dumps({**counts, "cases": len(cases), "with_new_root": new,
                      "stations": len({c["station"] for c in cases})}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
