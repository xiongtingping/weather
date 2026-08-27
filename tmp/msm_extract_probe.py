"""Can a single grid cell be pulled out of a real MSM surface file?

The archive publishes no index, so the message a 2-metre temperature lives in
has to be found by walking GRIB2 headers.  Each header states its own
message length, so the walk is a chain of sixteen-byte range reads -- cheap
individually, and the question this answers is whether the chain is short
enough to be worth it against simply taking the whole 69 MB object.

Decoding reuses the repo's own ctypes binding to eccodes rather than a
second one: the ICON archive already loads that library and already splits
messages, and a second decoder would be a second thing to keep true.
"""

from __future__ import annotations

import ctypes
import struct
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bayes_basket_target.dwd_icon_eps_archive import _eccodes

BASE = (
    "http://database.rish.kyoto-u.ac.jp/arch/jmadata/data/gpv/original"
    "/2026/08/26/Z__C_RJTD_20260826000000_MSM_GPV_Rjp_Lsurf"
    "_FH16-33_grib2.bin"
)
RCSS_LAT, RCSS_LON = 25.0697, 121.5525


def rng(start: int, length: int) -> bytes:
    result = subprocess.run(
        ["curl", "-s", "--max-time", "60",
         "-r", f"{start}-{start + length - 1}", BASE],
        capture_output=True,
    )
    return result.stdout if result.returncode == 0 else b""


def main() -> int:
    started = time.time()
    offsets = []
    position = 0
    # Walk the message chain.  Section 0 is sixteen bytes: "GRIB", two
    # reserved, discipline, edition, then the eight-byte total length.
    while len(offsets) < 400:
        head = rng(position, 16)
        if len(head) < 16 or head[:4] != b"GRIB":
            break
        total = struct.unpack(">Q", head[8:16])[0]
        if total <= 0 or total > 50_000_000:
            break
        offsets.append((position, total))
        position += total
    walk = time.time() - started
    print(f"消息数 {len(offsets)}，走链耗时 {walk:.1f}s")
    if not offsets:
        print("走链失败")
        return 1
    sizes = [length for _o, length in offsets]
    print(f"单消息 {min(sizes)}~{max(sizes)} 字节，"
          f"合计 {sum(sizes)/2**20:.1f} MB")

    library = _eccodes()
    print("\n逐条解码找 2m 温度:")
    found = []
    for index, (offset, length) in enumerate(offsets[:40]):
        body = rng(offset, length)
        if len(body) != length:
            continue
        buffer = ctypes.create_string_buffer(body, len(body))
        handle = library.codes_handle_new_from_message(
            None, buffer, ctypes.c_size_t(len(body))
        )
        if not handle:
            continue
        try:
            def get_long(key: str):
                out = ctypes.c_long()
                rc = library.codes_get_long(
                    handle, key.encode(), ctypes.byref(out)
                )
                return out.value if rc == 0 else None

            param = get_long("parameterNumber")
            category = get_long("parameterCategory")
            level = get_long("level")
            step = get_long("forecastTime")
            surface = get_long("typeOfFirstFixedSurface")
            if category == 0 and param == 0 and surface == 103:
                found.append((index, offset, length, step, level))
                if len(found) <= 3:
                    print(f"  #{index} FH{step} level={level} "
                          f"offset={offset} len={length}")
        finally:
            library.codes_handle_delete(handle)
    print(f"\n找到 2m 温度消息 {len(found)} 条（前 40 条里）")

    if found:
        index, offset, length, step, _level = found[0]
        body = rng(offset, length)
        buffer = ctypes.create_string_buffer(body, len(body))
        handle = library.codes_handle_new_from_message(
            None, buffer, ctypes.c_size_t(len(body))
        )
        try:
            size = ctypes.c_size_t()
            library.codes_get_size(handle, b"values", ctypes.byref(size))
            values = (ctypes.c_double * size.value)()
            count = ctypes.c_size_t(size.value)
            library.codes_get_double_array(
                handle, b"values", values, ctypes.byref(count)
            )

            def gl(key):
                out = ctypes.c_long()
                library.codes_get_long(
                    handle, key.encode(), ctypes.byref(out)
                )
                return out.value

            ni, nj = gl("Ni"), gl("Nj")
            lat1 = gl("latitudeOfFirstGridPointInDegrees")
            print(f"\n网格 {ni}x{nj} = {size.value} 点 (lat1 raw {lat1})")
            # Grid geometry from the message itself, in micro-degrees.
            first_lat = gl("latitudeOfFirstGridPoint") / 1e6
            first_lon = gl("longitudeOfFirstGridPoint") / 1e6
            di = gl("iDirectionIncrement") / 1e6
            dj = gl("jDirectionIncrement") / 1e6
            scan = gl("scanningMode")
            print(f"首点 {first_lat:.4f}N {first_lon:.4f}E  "
                  f"步长 {di:.4f}x{dj:.4f}  扫描模式 {scan}")
            row = round(abs(RCSS_LAT - first_lat) / dj)
            col = round(abs(RCSS_LON - first_lon) / di)
            cell = row * ni + col
            if 0 <= cell < size.value:
                kelvin = values[cell]
                print(f"\n✅ RCSS 格点 [{row},{col}] FH{step}: "
                      f"{kelvin:.2f}K = {kelvin - 273.15:.2f}°C")
            else:
                print(f"格点越界 row={row} col={col}")
        finally:
            library.codes_handle_delete(handle)
    print(f"\n总耗时 {time.time() - started:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
