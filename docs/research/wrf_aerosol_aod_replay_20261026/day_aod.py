"""Each event day's aerosol for the sat_aod arm, by PROTOCOL.md: JAXA ARP 3.1 scans 00:00..02:00Z,
pixels available + retrieved + AOT confidence very good/good within 1.0 deg of ZUUU (2.0, 3.0 deg
when fewer than 20), the median AOT500 / AE / SSA over all of them, AOD550 = AOT500 (550/500)^-AE;
a day still short takes the median of the earlier days' values.
usage: day_aod.py <dir of per-scan npz subsets> <out json>"""

import glob
import json
import math
import os
import sys

import numpy as np

LAT, LON = 30.578, 103.947
RADII = (1.0, 2.0, 3.0)
MIN_PIXELS = 20


def scale(name, raw):
    a = raw.astype(float)
    if name == "AOT":
        bad = (a < 0) | (a > 25000); a = a * 0.0002
    elif name == "AE":
        bad = (a < 0) | (a > 25000); a = a * 0.0001
    else:  # SSA, int8 with offset
        bad = (raw == -128) | (a < 0) | (a > 100); a = a * 0.003 + 0.7
    a[bad] = np.nan
    return a


def main(src: str, out: str) -> None:
    days: dict[str, list[str]] = {}
    for fn in sorted(glob.glob(os.path.join(src, "*.npz"))):
        tag = os.path.basename(fn)[:-4]
        if tag[:8] <= "20261024" and tag[9:13] <= "0200":
            days.setdefault(tag[:8], []).append(fn)
    result, earlier = {}, []
    for day in sorted(days):
        pix = {r: {"AOT": [], "AE": [], "SSA": []} for r in RADII}
        for fn in days[day]:
            z = np.load(fn)
            lat, lon = np.meshgrid(z["lat"], z["lon"], indexing="ij")
            dist = np.hypot(lat - LAT, (lon - LON) * math.cos(math.radians(LAT)))
            qa = z["QA_flag"].astype(int)
            ok = ((qa & 1) == 0) & (((qa >> 3) & 1) == 0) & (((qa >> 4) & 3) <= 1)
            vals = {k: scale(k, z[k]) for k in ("AOT", "AE", "SSA")}
            for r in RADII:
                m = ok & (dist <= r) & ~np.isnan(vals["AOT"]) & ~np.isnan(vals["AE"])
                for k in vals:
                    pix[r][k].extend(vals[k][m & ~np.isnan(vals[k])].tolist())
        rec = {"scans": len(days[day])}
        for r in RADII:
            if len(pix[r]["AOT"]) >= MIN_PIXELS:
                aot, ae = float(np.median(pix[r]["AOT"])), float(np.median(pix[r]["AE"]))
                ssa = float(np.median(pix[r]["SSA"])) if pix[r]["SSA"] else None
                rec.update(source=f"ARP within {r:g} deg", pixels=len(pix[r]["AOT"]), aot500=round(aot, 3),
                           ae=round(ae, 3), ssa=None if ssa is None else round(ssa, 3))
                break
        if "aot500" not in rec:
            if not earlier:
                rec.update(source="none")
                result[day] = rec
                continue
            rec.update(source="median of earlier days", pixels=0,
                       aot500=round(float(np.median([e["aot500"] for e in earlier])), 3),
                       ae=round(float(np.median([e["ae"] for e in earlier])), 3),
                       ssa=round(float(np.median([e["ssa"] for e in earlier if e["ssa"] is not None])), 3))
        else:
            earlier.append(rec)
        if rec.get("ssa") is None and earlier:
            rec["ssa"] = round(float(np.median([e["ssa"] for e in earlier if e["ssa"] is not None])), 3)
        rec["aod550"] = round(rec["aot500"] * (550 / 500) ** (-rec["ae"]), 3)
        result[day] = rec
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=1, ensure_ascii=False)
    for day, rec in result.items():
        print(day, rec)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
