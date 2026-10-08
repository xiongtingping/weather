"""Coverage and sanity of the satellite sky against the airport's own METAR.

For every station: how many local-daytime hours have a usable scan, how late
the scans became public (S3 LastModified minus scan start), and -- the check
that the satellite and the observer see the same sky -- the satellite's
cloud fraction against the METAR's reported coverage at the nearest report
within 30 minutes.  Read-only; the archive is opened ``mode=ro``.
"""
from __future__ import annotations

import collections
import json
import sqlite3
import statistics
import sys
from datetime import timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))
from sat_features import load, utc  # noqa: E402
from bayes_basket_target.metar_path_state import parse_metar_physical_state  # noqa: E402

COVER = {"CLR": 0.0, "SKC": 0.0, "NSC": 0.0, "NCD": 0.0, "CAVOK": 0.0}


def main() -> None:
    by = load()
    arc = sqlite3.connect(f"file:{ROOT / 'data' / 'archive.db'}?mode=ro", uri=True, timeout=30)
    report = {}
    for st, recs in sorted(by.items()):
        lags = [(utc(r["available_at"]) - utc(r["scan_start"])).total_seconds() / 60 for r in recs]
        lo, hi = recs[0]["scan_start"], recs[-1]["scan_start"]
        mets = []
        for va, raw, temp in arc.execute(
                "SELECT valid_at, raw_text, reported_temperature_c FROM reported_trajectory_observations"
                " WHERE station_id=? AND valid_at>=? AND valid_at<=? AND raw_text<>'' ORDER BY valid_at",
                (st, lo[:19], hi[:19])):
            if temp is None:
                continue
            try:
                ps = parse_metar_physical_state(raw, expected_temperature_c=float(temp))
            except Exception:  # noqa: BLE001
                continue
            if ps.cloud_cover_fraction is not None:
                mets.append((utc(va), float(ps.cloud_cover_fraction)))
        pairs = []
        j = 0
        for r in recs:
            t = utc(r["scan_start"])
            near = [m for m in mets if abs(m[0] - t) <= timedelta(minutes=30)]
            if near:
                m = min(near, key=lambda m: abs(m[0] - t))
                pairs.append((r["cloudy"], m[1]))
        corr = None
        if len(pairs) >= 20:
            xs, ys = zip(*pairs)
            if statistics.pstdev(xs) > 0 and statistics.pstdev(ys) > 0:
                corr = round(statistics.correlation(xs, ys), 3)
        report[st] = dict(source=recs[0]["source"], scans=len(recs), first=lo, last=hi,
                          days=len({r["scan_start"][:10] for r in recs}),
                          public_lag_min_median=round(statistics.median(lags), 1), public_lag_min_p95=round(sorted(lags)[int(0.95 * (len(lags) - 1))], 1),
                          metar_pairs=len(pairs), corr_sat_cloudy_vs_metar_cover=corr,
                          low_5000_known=sum(r.get("low_5000") is not None for r in recs))
    (HERE / "coverage.json").write_text(json.dumps(report, indent=1))
    for st, v in report.items():
        print(st, v["source"], v["days"], "d", v["scans"], "scans  lag", v["public_lag_min_median"], "/", v["public_lag_min_p95"],
              "min  r(METAR)", v["corr_sat_cloudy_vs_metar_cover"], "n", v["metar_pairs"], " low5000", v["low_5000_known"])


if __name__ == "__main__":
    main()
