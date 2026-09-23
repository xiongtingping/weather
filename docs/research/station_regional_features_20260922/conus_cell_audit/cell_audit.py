"""CONUS cell audit (read-only): which HRRR / NBM grid cell the production P1
lane reads for each station, whether that cell is water in the model's own
land mask, and what its neighbours look like.

For each product, one run: the 2 m temperature message at a local-afternoon
hour (where the daily maximum lives) plus, for HRRR, the LAND:surface
message of the same file (the model's land-use mask, 1 land / 0 water).
The production cell is the one ``noaa_conus_archive.nearest_point_kelvin``
returns (eccodes nearest); its geographic index is re-derived the way
``GridPointReader`` does and checked equal.  Output: cell_audit.json.
"""
import ctypes, json, math, os, sys
from datetime import datetime, timezone
import numpy as np
sys.path.insert(0, "/Users/xiong/weather")
from bayes_basket_target import noaa_conus_archive as nca
from bayes_basket_target.dwd_icon_eps_archive import _eccodes

HERE = os.path.dirname(os.path.abspath(__file__))
RUN = datetime(2026, 9, 20, 0, tzinfo=timezone.utc)
HOUR = int(sys.argv[1]) if len(sys.argv) > 1 else 22   # 22Z = 15 PDT / 18 EDT
STATIONS = {s[0]: s for s in nca._US_STATIONS + nca._IN_DOMAIN_NON_US_STATIONS}


def message(url, mark):
    raw = nca._get(url + ".idx").decode()
    lines = raw.splitlines(); offs = [int(l.split(":")[1]) for l in lines]
    i = next(k for k, l in enumerate(lines) if mark in l and not l.rstrip(":").endswith("ens std dev"))
    return nca._range_get(url, offs[i], offs[i + 1] if i + 1 < len(offs) else None)


def arrays(msg):
    lib = _eccodes()
    buf = ctypes.create_string_buffer(msg, len(msg))
    h = lib.codes_handle_new_from_message(None, buf, ctypes.c_size_t(len(msg)))
    try:
        L = lambda k: nca.GridPointReader._long(lib, h, k)
        A = lambda k: nca.GridPointReader._array(lib, h, k)
        nx, ny, alt = L(b"Nx"), L(b"Ny"), L(b"alternativeRowScanning")
        return dict(nx=nx, ny=ny, alt=alt == 1, lat=A(b"latitudes"), lon=A(b"longitudes") % 360.0, values=A(b"values"))
    finally:
        lib.codes_handle_delete(h)


def geo_value(g, gi):
    """value at geographic (iterator-order) index gi"""
    return float(g["values"][nca._values_index(gi, g["nx"], g["alt"])])


def haversine(la1, lo1, la2, lo2):
    p1, p2 = math.radians(la1), math.radians(la2)
    d = math.radians(lo2 - lo1)
    c = math.sin(p1) * math.sin(p2) + math.cos(p1) * math.cos(p2) * math.cos(d)
    return 6371.0 * math.acos(max(-1.0, min(1.0, c)))


out = {"run": RUN.isoformat(), "hour": HOUR, "products": {}}
for product in ("HRRR", "NBM"):
    url = nca.file_url(product, RUN, HOUR)
    tmsg = message(url, nca._FIELD_MARK)
    T = arrays(tmsg)
    land = arrays(message(url, ":LAND:surface:")) if product == "HRRR" else None
    lat_r, lon_r = np.radians(T["lat"]), np.radians(T["lon"])
    res = {}
    for st, (sid, la, lo, tz) in STATIONS.items():
        cos_c = math.sin(math.radians(la)) * np.sin(lat_r) + math.cos(math.radians(la)) * np.cos(lat_r) * np.cos(lon_r - math.radians(lo % 360))
        gi = int(np.argmax(cos_c))
        k_prod, dist_prod = nca.nearest_point_kelvin(tmsg, latitude=la, longitude=lo)
        row, col = divmod(gi, T["nx"])
        cell = dict(lat=round(float(T["lat"][gi]), 5), lon=round(float(T["lon"][gi]) - 360, 5),
                    dist_km=round(haversine(la, lo, T["lat"][gi], T["lon"][gi] - 360), 2),
                    t2m_c=round(geo_value(T, gi) - 273.15, 2), production_t2m_c=round(k_prod - 273.15, 2),
                    production_matches_index=abs(geo_value(T, gi) - k_prod) < 1e-9)
        if land is not None:
            cell["land"] = geo_value(land, gi)
        nb = []
        for dr in (-2, -1, 0, 1, 2):
            for dc in (-2, -1, 0, 1, 2):
                r2, c2 = row + dr, col + dc
                if not (0 <= r2 < T["ny"] and 0 <= c2 < T["nx"]):
                    continue
                g2 = r2 * T["nx"] + c2
                d = dict(dr=dr, dc=dc, lat=round(float(T["lat"][g2]), 4), lon=round(float(T["lon"][g2]) - 360, 4),
                         dist_km=round(haversine(la, lo, T["lat"][g2], T["lon"][g2] - 360), 2), t2m_c=round(geo_value(T, g2) - 273.15, 2))
                if land is not None:
                    d["land"] = geo_value(land, g2)
                nb.append(d)
        cell["neighbours_5x5"] = nb
        if land is not None:
            lands = [d for d in nb if d["land"] >= 0.5]
            if lands:
                best = min(lands, key=lambda d: d["dist_km"])
                cell["nearest_land"] = {k: best[k] for k in ("dr", "dc", "lat", "lon", "dist_km", "t2m_c")}
        res[st] = cell
        print(product, st, {k: v for k, v in cell.items() if k != "neighbours_5x5"}, flush=True)
    out["products"][product] = res
    if product == "HRRR":
        HR = dict(T=T, land=land)
    else:
        # NBM core publishes no land mask: read HRRR's LAND at the HRRR cell nearest each NBM cell's centre
        hl = np.radians(HR["T"]["lat"]); hn = np.radians(HR["T"]["lon"])
        for st, cell in res.items():
            la, lo = math.radians(cell["lat"]), math.radians(cell["lon"] % 360)
            gi = int(np.argmax(math.sin(la) * np.sin(hl) + math.cos(la) * np.cos(hl) * np.cos(hn - lo)))
            cell["hrrr_land_at_cell"] = geo_value(HR["land"], gi)
            print("NBM", st, "HRRR land at NBM cell", cell["hrrr_land_at_cell"], flush=True)
json.dump(out, open(os.path.join(HERE, "cell_audit.json"), "w"), indent=1)
