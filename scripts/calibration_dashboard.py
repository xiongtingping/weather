commit be41e44cda22dedf11bfa199dfa941a932ffb060
Author: xiongtingping <xiongtingping@gmail.com>
Date:   Thu Oct 15 09:55:45 2026 +0900

    Station board: east of 90E the satellite sunshine column reads JAXA's own Himawari SWR from P-Tree, not Open-Meteo's copy (user 「接入：http://hokusai.eorc.jaxa.jp/」)
    
    hokusai.eorc.jaxa.jp and ftp.ptree.jaxa.jp are names on one JAXA server's
    certificate.  jaxa_ptree_radiation reads the Level 3 hourly Short Wave
    Radiation (v2.1, 0.05 deg, 60N..60S 70E..150W; file HH00 = mean of
    HH:00-HH:59 UTC, public ~15 min after the hour) over implicit FTPS with
    the account in ~/.netrc (never in this tree) and the pinned SECOM TLS RSA
    Root CA 2024 (sha256 14:35:F2:25...2F:93, matches crt.sh; neither macOS
    nor certifi carries it yet).  One background thread downloads an hour
    once (size cap 80 MB, 2 GiB free-disk floor), reads the cell of every
    covered fleet station, deletes the file, and keeps the readings in
    data/runtime/jaxa_ptree_swr_v1.json (96 h) so a restart fetches nothing
    again; the board never waits on it.  Same number as the column showed:
    ZUUU 2026-10-14 05-06Z 412.7 W/m2 here, 411.8 through Open-Meteo.  The
    other products (IODC, DWD SIS, MSG, GOES) are unchanged, so the
    sunshine-to-Tmax fit keeps its basis; the payload names each source's
    path and lag (satellite.via).  Board only.
    
    Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>

diff --git a/bayes_basket_target/jaxa_ptree_radiation.py b/bayes_basket_target/jaxa_ptree_radiation.py
new file mode 100644
index 00000000..1e5a2b70
--- /dev/null
+++ b/bayes_basket_target/jaxa_ptree_radiation.py
@@ -0,0 +1,260 @@
+"""Measured surface shortwave from JAXA's own Himawari product, through the
+P-Tree FTP service (user 2026-10-15 「接入：http://hokusai.eorc.jaxa.jp/」 --
+hokusai.eorc.jaxa.jp and ftp.ptree.jaxa.jp are names on one JAXA server's
+certificate).
+
+The board's hourly sunshine column read this product republished by
+Open-Meteo (``jma_jaxa_himawari``); the same hour reads the same number at
+the source (ZUUU 2026-10-14 05-06Z: 412.7 W/m2 here, 411.8 there), so this is
+the official copy of what the column already showed, not a new quantity.
+
+Product: JAXA Himawari Short Wave Radiation, Level 3 hourly, version 2.1,
+0.05-degree equal latitude-longitude grid (60N..60S, 70E..150W, 2401 x
+2801), ``SWR`` int16 W/m2 with its own scale factor and missing value; a
+file named ``..._YYYYMMDD_HH00_1H_RFL...`` is the mean of HH:00-HH:59 UTC
+and lands about 15 minutes after that hour ends.  Every file is the whole
+disk, so an hour is downloaded once and read for every fleet station in it,
+the file deleted at once (bounded: size cap, a free-disk floor, one reader
+thread); the readings are kept in a small runtime file so a restart does not
+fetch again.  Access is the account in ``~/.netrc`` (never in this tree)
+over implicit FTPS against the pinned SECOM TLS RSA Root CA 2024
+(``config/certs``; macOS and certifi do not carry it yet).
+
+Board only -- nothing here feeds a probability or an order.
+"""
+
+from __future__ import annotations
+
+import json
+import os
+import queue
+import re
+import shutil
+import subprocess
+import tempfile
+import threading
+import time
+from datetime import datetime, timedelta, timezone
+from pathlib import Path
+from typing import Any
+
+ROOT = Path(__file__).resolve().parents[1]
+HOST = "ftp.ptree.jaxa.jp"
+PRODUCT_DIR = "/pub/himawari/L3/PAR"
+CA_FILE = ROOT / "config" / "certs" / "secom_tls_rsa_root_ca_2024.pem"
+LABEL = "JAXA Himawari（P-Tree 官方）"
+#: Grid of the 5 km full-disk file.
+UPPER_LAT, LEFT_LON, STEP = 60.0, 70.0, 0.05
+ROWS, COLS = 2401, 2801
+#: An hour's file is public this long after the hour began (measured
+#: 2026-10-14: the 22:00 file at 23:15).
+PUBLISH_AFTER = timedelta(minutes=75)
+MAX_FILE_BYTES = 80 * 1024 * 1024
+MIN_FREE_BYTES = 2 * 1024 ** 3
+CURL_TIMEOUT_SECONDS = 300
+LISTING_TTL_SECONDS = 300.0
+MISS_RETRY_SECONDS = 600.0
+KEEP_HOURS = 96
+CACHE_PATH = ROOT / "data" / "runtime" / "jaxa_ptree_swr_v1.json"
+CACHE_SCHEMA = "jaxa_ptree_swr_v1"
+_FILE_RE = re.compile(r"^H\d\d_(\d{8})_(\d{2})00_1H_RFL\d{3}_FLDK\.02801_02401\.nc$")
+
+_LOCK = threading.Lock()
+_QUEUE: "queue.Queue[datetime]" = queue.Queue()
+_PENDING: set[str] = set()
+_MISSES: dict[str, float] = {}
+_LISTINGS: dict[str, tuple[float, list[str]]] = {}
+_CACHE: dict[str, dict[str, float | None]] | None = None   # hour ISO -> station -> W/m2
+_WORKER: threading.Thread | None = None
+_STATUS: dict[str, Any] = {}
+
+
+def covers(lat: float, lon: float) -> bool:
+    east = lon if lon >= 0.0 else lon + 360.0
+    return -60.0 <= lat <= 60.0 and LEFT_LON <= east <= LEFT_LON + STEP * (COLS - 1)
+
+
+def pixel(lat: float, lon: float) -> tuple[int, int] | None:
+    """The grid cell nearest the point, or None outside the disk."""
+
+    if not covers(lat, lon):
+        return None
+    east = lon if lon >= 0.0 else lon + 360.0
+    return round((UPPER_LAT - lat) / STEP), round((east - LEFT_LON) / STEP)
+
+
+def hour_start(instant: datetime) -> datetime:
+    return instant.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
+
+
+def _key(hour: datetime) -> str:
+    return hour_start(hour).strftime("%Y-%m-%dT%H:00Z")
+
+
+def _curl(*args: str, timeout: int = 60) -> subprocess.CompletedProcess:
+    return subprocess.run(
+        ["curl", "--netrc", "--cacert", str(CA_FILE), "-sS", "-m", str(timeout), *args],
+        capture_output=True, timeout=timeout + 15, check=False,
+    )
+
+
+def _listing(directory: str) -> list[str]:
+    hit = _LISTINGS.get(directory)
+    if hit and time.monotonic() - hit[0] < LISTING_TTL_SECONDS:
+        return hit[1]
+    out = _curl("--list-only", f"ftps://{HOST}{directory}/")
+    if out.returncode != 0:
+        raise RuntimeError(f"listing {directory}: curl {out.returncode} {out.stderr.decode(errors='replace')[:120]}")
+    names = [line.strip() for line in out.stdout.decode(errors="replace").splitlines() if line.strip()]
+    _LISTINGS[directory] = (time.monotonic(), names)
+    return names
+
+
+def _version() -> str:
+    versions = sorted(name for name in _listing(PRODUCT_DIR) if name.isdigit())
+    if not versions:
+        raise RuntimeError("no product version directory")
+    return versions[-1]
+
+
+def _remote_file(hour: datetime) -> str | None:
+    hour = hour_start(hour)
+    directory = f"{PRODUCT_DIR}/{_version()}/{hour:%Y%m}/{hour:%d}"
+    stamp = (hour.strftime("%Y%m%d"), hour.strftime("%H"))
+    for name in _listing(directory):
+        m = _FILE_RE.match(name)
+        if m and (m.group(1), m.group(2)) == stamp:
+            return f"{directory}/{name}"
+    return None
+
+
+def _fleet() -> dict[str, tuple[float, float]]:
+    from .station_registry import _COORDINATES  # noqa: PLC0415
+
+    return {str(s).upper(): (float(lat), float(lon)) for s, (lat, lon) in _COORDINATES.items() if covers(lat, lon)}
+
+
+def read_hour(hour: datetime, stations: dict[str, tuple[float, float]]) -> dict[str, float | None] | None:
+    """One hour's SWR at every station's grid cell; None when the hour's file
+    is not on the server (yet).  Raises on a transport or format failure."""
+
+    import h5py  # noqa: PLC0415
+    import numpy as np  # noqa: PLC0415
+
+    remote = _remote_file(hour)
+    if remote is None:
+        return None
+    workdir = CACHE_PATH.parent / "jaxa_ptree_tmp"
+    workdir.mkdir(parents=True, exist_ok=True)
+    if shutil.disk_usage(workdir).free < MIN_FREE_BYTES:
+        raise RuntimeError("free disk below the floor")
+    handle, path = tempfile.mkstemp(suffix=".nc", dir=workdir)
+    os.close(handle)
+    try:
+        got = _curl("--max-filesize", str(MAX_FILE_BYTES), "-o", path, f"ftps://{HOST}{remote}",
+                    timeout=CURL_TIMEOUT_SECONDS)
+        if got.returncode != 0:
+            raise RuntimeError(f"download: curl {got.returncode} {got.stderr.decode(errors='replace')[:120]}")
+        with h5py.File(path, "r") as f:
+            swr = f["SWR"]
+            if swr.shape != (ROWS, COLS):
+                raise RuntimeError(f"grid {swr.shape}")
+            scale = float(np.ravel(swr.attrs.get("scale_factor", [1.0]))[0])
+            missing = int(np.ravel(swr.attrs.get("missing_value", [-32768]))[0])
+            lat_axis, lon_axis = f["latitude"], f["longitude"]
+            out: dict[str, float | None] = {}
+            for station, (lat, lon) in stations.items():
+                cell = pixel(lat, lon)
+                if cell is None:
+                    continue
+                r, c = cell
+                if abs(float(lat_axis[r]) - lat) > STEP or abs(((float(lon_axis[c]) - lon) + 180.0) % 360.0 - 180.0) > STEP:
+                    raise RuntimeError(f"grid axis disagrees at {station}")
+                raw = int(swr[r, c])
+                out[station] = None if raw == missing else round(raw * scale, 1)
+        return out
+    finally:
+        try:
+            os.unlink(path)
+        except OSError:
+            pass
+
+
+def _load() -> dict[str, dict[str, float | None]]:
+    global _CACHE
+    if _CACHE is None:
+        try:
+            doc = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
+            _CACHE = dict(doc.get("hours") or {}) if doc.get("schema") == CACHE_SCHEMA else {}
+        except (OSError, ValueError):
+            _CACHE = {}
+    return _CACHE
+
+
+def _persist(cache: dict[str, dict[str, float | None]]) -> None:
+    keep = sorted(cache)[-KEEP_HOURS:]
+    for stale in set(cache) - set(keep):
+        cache.pop(stale, None)
+    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
+    temporary = CACHE_PATH.with_suffix(".tmp")
+    temporary.write_text(json.dumps({"schema": CACHE_SCHEMA, "written_at_utc": datetime.now(timezone.utc)
+                                     .strftime("%Y-%m-%dT%H:%M:%SZ"), "hours": cache}, sort_keys=True),
+                         encoding="utf-8")
+    os.replace(temporary, CACHE_PATH)
+
+
+def _work() -> None:
+    while True:
+        hour = _QUEUE.get()
+        key = _key(hour)
+        try:
+            values = read_hour(hour, _fleet())
+            error = None
+        except Exception as exc:  # noqa: BLE001 -- asked again after MISS_RETRY_SECONDS
+            values, error = None, f"{type(exc).__name__}: {str(exc)[:120]}"
+        with _LOCK:
+            _PENDING.discard(key)
+            if values is None:
+                _MISSES[key] = time.monotonic()
+                if error:
+                    _STATUS.update(last_error=error, last_error_hour=key)
+                continue
+            cache = _load()
+            cache[key] = values
+            _persist(cache)
+            _STATUS.update(last_read_hour=key, last_read_at_utc=datetime.now(timezone.utc)
+                           .strftime("%Y-%m-%dT%H:%M:%SZ"))
+
+
+def measured(station: str, hours: list[datetime]) -> dict[str, float | None]:
+    """hour start (``YYYY-MM-DDTHH:00Z``) -> W/m2 for the hours already read
+    (None where the file has no value at the station); the others are queued
+    for the background reader, newest first.  Never waits on the network."""
+
+    global _WORKER
+    out: dict[str, float | None] = {}
+    now = time.monotonic()
+    with _LOCK:
+        if _WORKER is None or not _WORKER.is_alive():
+            _WORKER = threading.Thread(target=_work, name="jaxa-ptree-swr", daemon=True)
+            _WORKER.start()
+        cache = _load()
+        for hour in sorted(hours, reverse=True):
+            key = _key(hour)
+            if key in cache:
+                if station.upper() in cache[key]:
+                    out[key] = cache[key][station.upper()]
+                continue
+            if key in _PENDING or now - _MISSES.get(key, -1e9) < MISS_RETRY_SECONDS:
+                continue
+            _PENDING.add(key)
+            _QUEUE.put(hour_start(hour))
+    return out
+
+
+def status() -> dict[str, Any]:
+    with _LOCK:
+        return dict(_STATUS, pending=len(_PENDING))
+
+
+__all__ = ["LABEL", "PUBLISH_AFTER", "covers", "hour_start", "measured", "pixel", "read_hour", "status"]
diff --git a/bayes_basket_target/station_day_board.py b/bayes_basket_target/station_day_board.py
index 88d3f32b..9b8ebecb 100644
--- a/bayes_basket_target/station_day_board.py
+++ b/bayes_basket_target/station_day_board.py
@@ -768,7 +768,11 @@ OPEN_METEO_SATELLITE = "https://satellite-api.open-meteo.com/v1/archive"
 SATELLITE_CACHE_SECONDS = 900.0
 _SATELLITE_CACHE: dict[tuple[str, str, str], tuple[float, dict[str, Any]]] = {}
 SATELLITE_PRODUCTS = {
-    "jma_jaxa_himawari": "JAXA Himawari",
+    # East of 90E the column reads JAXA's own copy of the product it used to
+    # read here republished (user 2026-10-15 「接入：http://hokusai.eorc.jaxa.jp/」,
+    # jaxa_ptree_radiation): same quantity, official source, about 45 minutes
+    # sooner.
+    "jaxa_ptree_himawari": "JAXA Himawari（P-Tree 官方）",
     "eumetsat_lsa_saf_iodc": "EUMETSAT 印度洋（IODC）",
     "dwd_sis_europe_africa_v4": "DWD SIS 欧洲/非洲",
     "eumetsat_lsa_saf_msg": "EUMETSAT MSG",
@@ -776,9 +780,17 @@ SATELLITE_PRODUCTS = {
 }
 
 
+#: How each source reaches the board and how late an hour lands.
+SATELLITE_VIA = {
+    "jaxa_ptree_himawari": "JAXA P-Tree 官方，整点后约 15 分钟",
+    "noaa_goes_dsr": "NOAA 官方，扫描后约 25 分钟",
+}
+SATELLITE_VIA_DEFAULT = "经 Open-Meteo 转发，约晚 1 小时"
+
+
 def _satellite_product(lon: float) -> str | None:
     if lon >= 90.0 or lon <= -150.0:
-        return "jma_jaxa_himawari"
+        return "jaxa_ptree_himawari"
     if lon >= 45.0:
         return "eumetsat_lsa_saf_iodc"
     if lon >= -20.0:
@@ -967,6 +979,39 @@ def _satellite_ahead_of_utc(lat: float, lon: float, day: str, zone: ZoneInfo, mo
     return out
 
 
+def _jaxa_hours(station: str, lat: float, lon: float, day: str, zone: ZoneInfo) -> dict[str, Any]:
+    """The local day's hourly JAXA Himawari SWR, as far as the background
+    reader has it: hour h (the mean of the hour ending at h:00, the forecast
+    column's stamp) reads the UTC hour file whose hour holds h-0:30 -- the
+    same hour in a whole-hour zone; night hours are zero; a published hour not
+    read yet is absent and arrives on a later refresh."""
+
+    from . import jaxa_ptree_radiation as jaxa  # noqa: PLC0415
+
+    d = datetime.fromisoformat(day).date()
+    now_utc = datetime.now(timezone.utc)
+    values: list[float | None] = [None] * 24
+    wanted: dict[int, datetime] = {}
+    for h in range(24):
+        middle = (datetime.combine(d, dtime(h), zone) - timedelta(minutes=30)).astimezone(timezone.utc)
+        start = jaxa.hour_start(middle)
+        if start + jaxa.PUBLISH_AFTER > now_utc:
+            continue  # the hour's file is not public yet
+        if _clear_sky_ghi(lat, lon, middle) <= 0.0:
+            values[h] = 0.0
+            continue
+        wanted[h] = start
+    try:
+        got = jaxa.measured(station, list(wanted.values()))
+    except Exception as exc:  # noqa: BLE001 -- the column says why
+        return {"available": False, "values": values, "reason": f"{type(exc).__name__}: {str(exc)[:120]}"}
+    for h, start in wanted.items():
+        values[h] = got.get(start.strftime("%Y-%m-%dT%H:00Z"))
+    reader = jaxa.status()
+    return {"available": any(v for v in values), "values": values, "fetched_at_utc": reader.get("last_read_at_utc"),
+            "pending_hours": sum(1 for h in wanted if values[h] is None), "reason": reader.get("last_error")}
+
+
 def _goes_hours(station: str, lat: float, lon: float, day: str, zone: ZoneInfo) -> dict[str, Any]:
     """The local day's hourly GOES DSR, as far as the background reader has
     it: hour h is the scan nearest h-1:30 (the middle of the hour the forecast
@@ -1025,7 +1070,9 @@ def _hourly_vs_forecast(conn, station: str, day: str, zone: ZoneInfo,
     product = _satellite_product(lon)
     started = datetime.fromisoformat(day).date() <= datetime.now(timezone.utc).astimezone(zone).date()
     # A day not begun has nothing measured yet (the archive answers 400).
-    if product and started and day > datetime.now(timezone.utc).date().isoformat():
+    if product == "jaxa_ptree_himawari" and started:
+        sat = _jaxa_hours(station, lat, lon, day, zone)
+    elif product and started and day > datetime.now(timezone.utc).date().isoformat():
         sat = _satellite_ahead_of_utc(lat, lon, day, zone, product)
     elif product and started:
         sat = _open_meteo_satellite(lat, lon, str(zone.key), day, product)
@@ -1116,6 +1163,7 @@ def _hourly_vs_forecast(conn, station: str, day: str, zone: ZoneInfo,
         "fetched_at_utc": fc.get("fetched_at_utc"), "rows": rows,
         "forecast_sw_mj": sums["sw"], "forecast_sw_d1_mj": sums["sw_d1"],
         "satellite": {"product": product, "label": SATELLITE_PRODUCTS.get(product or "", ""),
+                      "via": SATELLITE_VIA.get(product or "", SATELLITE_VIA_DEFAULT),
                       "available": bool(sat.get("available")), "reason": sat.get("reason"),
                       "fetched_at_utc": sat.get("fetched_at_utc"),
                       "last_hour": max((r["hour"] for r in measured), default=None)},
diff --git a/config/certs/secom_tls_rsa_root_ca_2024.pem b/config/certs/secom_tls_rsa_root_ca_2024.pem
new file mode 100644
index 00000000..5430c4a4
--- /dev/null
+++ b/config/certs/secom_tls_rsa_root_ca_2024.pem
@@ -0,0 +1,32 @@
+-----BEGIN CERTIFICATE-----
+MIIFmjCCA4KgAwIBAgIJAO6JNNDLgOCyMA0GCSqGSIb3DQEBDAUAMFoxCzAJBgNV
+BAYTAkpQMSYwJAYDVQQKEx1TRUNPTSBUcnVzdCBTeXN0ZW1zIENvLiwgTHRkLjEj
+MCEGA1UEAxMaU0VDT00gVExTIFJTQSBSb290IENBIDIwMjQwHhcNMjQwMTMxMDUx
+MTU1WhcNNDkwMTE0MDUxMTU1WjBaMQswCQYDVQQGEwJKUDEmMCQGA1UEChMdU0VD
+T00gVHJ1c3QgU3lzdGVtcyBDby4sIEx0ZC4xIzAhBgNVBAMTGlNFQ09NIFRMUyBS
+U0EgUm9vdCBDQSAyMDI0MIICIjANBgkqhkiG9w0BAQEFAAOCAg8AMIICCgKCAgEA
+4TjizUwzxbInq8Tx11gaFYNk5fO+34y7TyM4neh0UgL5JIZbJNLTz2x//L/B71+5
+m6X6nGIr7d4lFJBGtjO677hXOz93zkcWaUTm3VbOAjBlt4YWxlcccBHXuZ7o3Q+4
+R+ormrBdHeJ1CTUEG8ttQbKIl3G7OZYbnH8/pP8cjPub/0kDVNuMzp7xsVRROOis
+Qt53fMoJLlYgoebbuMphOqMCtjkJ7R6efEMfLp8UAVi9ZaLRn76ET/CJkk925ndu
+uufC4BatS4mnXFmxN0vUXb0ij9B8O/D8gixQEsVSD4GK8FWRPh3bVd/6bzdkHGJj
+y21XI0yejVomZUbRrOfNuz0boPGV1pt18fFC39IHQEth3OFqb5NDO3L+A9bNqTgA
+yUgRmIn4ucgDc/Ri/Km3V51ueZjy1/yk0qwJVadAVVrCt56iNeXOyEvzJADGgDQ8
+E1Pdaqct8Cynz/47ReQM62vFYO08wcQkrjmX/tesiko1V1yyaf6EfPzUFzmaGy9x
+vkCwdbm15EdTolOjE0H2Vb5/APDOyCFEokiYGmXTLdAUl0wKZ4IyjkHGzy0jhpaX
+EXE/GJcEvI6VzEchjaBL03EJ0h9pG4OqeIOycKvAo3A+TbetyfsrgYyHzU0a7/qU
+jGat1AAq1nVljMpKqpinPTsf/d9H39FTUeJL7TpzzjUCAwEAAaNjMGEwHQYDVR0O
+BBYEFCzrchKOWHdkNRVWNQFXB6l9DTbmMB8GA1UdIwQYMBaAFCzrchKOWHdkNRVW
+NQFXB6l9DTbmMA4GA1UdDwEB/wQEAwIBBjAPBgNVHRMBAf8EBTADAQH/MA0GCSqG
+SIb3DQEBDAUAA4ICAQAVwsvluSafaez5tFPR/hRTBzRxEyMMQF3XJXCVi3yegZyK
+oec7hmE6jx2ZM8KgM1kn2yJRwFXHX8zUW9nBLEDWc4wuE8LrlZqhGZM9pJQXGmGz
+ResDJV6JgRBna+j4sA1M7yIdlvL0sAfFXFCTRaWTD4E1V99RLrFzWfTcC+e180hD
+uNMpqOEo46+lMeW/Wvh7ifOQs+kiK0O2gHxQDNxslSavnCs4V7l8HRDJ2La10o70
+Bo7VLzf1W8MBvv0VTnxB+NjT5qTAbhGFh9Gvp4BaJpmdUf0C5CEP6dbQlfgxWfzY
+r69yVT6dPQB+GFEaY03IMY+AcBCs+om1fNxrQXt9zoofMBNFbLhvpNH/JsXWdGUz
+fNbO12uswTa5wah8LB18FTQN2/zPHYmvBEoLuyUgZ09VNLJo5YA0kXItVYkLjMe2
+SixzK4scUHv81IK99I91DWx7FwMVKw2xgFp+ZLYB2dnpQQrqwlW64glHUcK2N9BD
+snjLSxeZ+UPECh9RxH4WAcKiZW+cqaKMmhP2WBfR4IcR7NOL32ml11ds87hhV1CZ
+WWFCJAcCidYZz6CZa8exzHojP9SB5RH0/v1KdHAisqhSjtJl/UIAHIQ48elOn8wr
+TdFap4Yb5aHglmMeNx+fAIhDluWVfxTO7H4dTPU+SFVRMLAh+wwKZfqb94nMeQ==
+-----END CERTIFICATE-----
diff --git a/tests/test_jaxa_ptree_radiation.py b/tests/test_jaxa_ptree_radiation.py
new file mode 100644
index 00000000..d83366ac
--- /dev/null
+++ b/tests/test_jaxa_ptree_radiation.py
@@ -0,0 +1,100 @@
+"""JAXA's own Himawari shortwave on the station board (user 2026-10-15
+「接入：http://hokusai.eorc.jaxa.jp/」): grid cells, the hour a local stamp
+reads, the background reader's cache, and one synthetic file read end to end."""
+
+from __future__ import annotations
+
+import json
+import time
+from datetime import datetime, timezone
+from zoneinfo import ZoneInfo
+
+import numpy as np
+import pytest
+
+from bayes_basket_target import jaxa_ptree_radiation as jaxa
+from bayes_basket_target import station_day_board as board
+
+
+def test_grid_cells_and_coverage() -> None:
+    assert jaxa.pixel(30.576, 103.95) == (588, 679)         # ZUUU: the 30.60N cell
+    assert jaxa.covers(-41.33, 174.8) and jaxa.covers(21.0, -158.0)   # Wellington, past 180
+    assert not jaxa.covers(51.5, 0.05) and not jaxa.covers(33.9, -118.4)   # London, Los Angeles
+    assert jaxa.pixel(61.0, 120.0) is None
+
+
+def _h5(path, rows=jaxa.ROWS, cols=jaxa.COLS, values=None) -> None:
+    import h5py
+
+    with h5py.File(path, "w") as f:
+        swr = np.full((rows, cols), -32768, dtype=np.int16)
+        for (r, c), v in (values or {}).items():
+            swr[r, c] = v
+        d = f.create_dataset("SWR", data=swr)
+        d.attrs["scale_factor"] = np.array([0.05], dtype=np.float32)
+        d.attrs["missing_value"] = np.array([-32768], dtype=np.int16)
+        f.create_dataset("latitude", data=(60.0 - 0.05 * np.arange(rows)).astype(np.float32))
+        f.create_dataset("longitude", data=(70.0 + 0.05 * np.arange(cols)).astype(np.float32))
+
+
+def test_one_hour_is_read_for_every_station_and_the_file_is_deleted(tmp_path, monkeypatch) -> None:
+    monkeypatch.setattr(jaxa, "CACHE_PATH", tmp_path / "cache.json")
+    monkeypatch.setattr(jaxa, "_remote_file", lambda hour: "/pub/himawari/L3/PAR/021/202610/14/x.nc")
+    written: list[str] = []
+
+    class Done:
+        returncode, stderr = 0, b""
+
+    def fake_curl(*args, timeout=60):
+        path = args[args.index("-o") + 1]
+        _h5(path, values={jaxa.pixel(30.576, 103.95): 8254})   # ZUUU 412.7 W/m2
+        written.append(path)
+        return Done()
+
+    monkeypatch.setattr(jaxa, "_curl", fake_curl)
+    got = jaxa.read_hour(datetime(2026, 10, 14, 5, tzinfo=timezone.utc),
+                         {"ZUUU": (30.576, 103.95), "RJTT": (35.553, 139.781), "EGLC": (51.5, 0.05)})
+    assert got == {"ZUUU": 412.7, "RJTT": None}          # missing value is None; outside the disk is left out
+    assert written and not any((tmp_path / "jaxa_ptree_tmp").iterdir())
+
+
+def test_the_reader_serves_from_its_cache_and_queues_the_rest(tmp_path, monkeypatch) -> None:
+    path = tmp_path / "cache.json"
+    path.write_text(json.dumps({"schema": jaxa.CACHE_SCHEMA, "hours": {"2026-10-14T05:00Z": {"ZUUU": 412.7}}}))
+    monkeypatch.setattr(jaxa, "CACHE_PATH", path)
+    monkeypatch.setattr(jaxa, "_CACHE", None)
+    monkeypatch.setattr(jaxa, "_PENDING", set())
+    monkeypatch.setattr(jaxa, "_MISSES", {})
+    monkeypatch.setattr(jaxa, "_fleet", lambda: {"ZUUU": (30.576, 103.95)})
+    monkeypatch.setattr(jaxa, "read_hour", lambda hour, stations: {"ZUUU": 500.0})
+    hours = [datetime(2026, 10, 14, 5, tzinfo=timezone.utc), datetime(2026, 10, 14, 6, tzinfo=timezone.utc)]
+    assert jaxa.measured("ZUUU", hours) == {"2026-10-14T05:00Z": 412.7}
+    for _ in range(50):
+        if "2026-10-14T06:00Z" in json.loads(path.read_text())["hours"]:
+            break
+        time.sleep(0.05)
+    assert jaxa.measured("ZUUU", hours) == {"2026-10-14T05:00Z": 412.7, "2026-10-14T06:00Z": 500.0}
+
+
+def test_a_local_hour_reads_the_utc_hour_that_holds_its_middle(monkeypatch) -> None:
+    asked: list[list[datetime]] = []
+
+    def fake_measured(station, hours):
+        asked.append(hours)
+        return {h.strftime("%Y-%m-%dT%H:00Z"): 100.0 + h.hour for h in hours}
+
+    monkeypatch.setattr(jaxa, "measured", fake_measured)
+    monkeypatch.setattr(jaxa, "status", lambda: {"last_read_at_utc": "2026-10-14T20:00:00Z"})
+    out = board._jaxa_hours("ZUUU", 30.576, 103.95, "2026-10-14", ZoneInfo("Asia/Shanghai"))
+    # Local 14:00 (the hour 13:00-14:00) is 05:00-06:00 UTC.
+    assert out["values"][14] == 105.0
+    assert out["values"][2] == 0.0                     # night
+    assert out["pending_hours"] == 0 and out["fetched_at_utc"] == "2026-10-14T20:00:00Z"
+    assert board._satellite_product(103.95) == "jaxa_ptree_himawari"
+    assert board.SATELLITE_VIA["jaxa_ptree_himawari"].startswith("JAXA P-Tree 官方")
+
+
+def test_the_account_stays_out_of_the_tree() -> None:
+    source = (jaxa.ROOT / "bayes_basket_target" / "jaxa_ptree_radiation.py").read_text(encoding="utf-8")
+    assert '"--netrc"' in source and "password" not in source.lower().replace("~/.netrc", "")
+    assert jaxa.CA_FILE.exists()
