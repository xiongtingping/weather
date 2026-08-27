"""Pull one field out of a 78 MB single-message MSM file by byte range.

JMA writes the whole file as one GRIB2 message with sections 4-5-6-7 repeated
once per field, which at first looks like it forecloses ranged access: the
message header states the length of the entire object, so a message-level
walk finds exactly one message and stops.  The fields are still individually
addressable, one section-group at a time, and a group here is about 356 KB
against the file's 78 MB.

So the strategy is: read the header and the shared sections 1 and 3, walk the
section chain reading only each section-4 product definition, stop at the one
naming 2-metre temperature at the wanted forecast hour, fetch that group, and
assemble a single-field message eccodes can open.  Nothing else is
downloaded.
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

URL = (
    "http://database.rish.kyoto-u.ac.jp/arch/jmadata/data/gpv/original"
    "/2026/08/26/Z__C_RJTD_20260826000000_MSM_GPV_Rjp_Lsurf"
    "_FH16-33_grib2.bin"
)
RCSS_LAT, RCSS_LON = 25.0697, 121.5525
WANT_FH = 24


def rng(start: int, length: int) -> bytes:
    result = subprocess.run(
        ["curl", "-s", "--max-time", "60",
         "-r", f"{start}-{start + length - 1}", URL],
        capture_output=True,
    )
    return result.stdout if result.returncode == 0 else b""


def main() -> int:
    started = time.time()
    downloaded = 0

    head = rng(0, 16)
    downloaded += len(head)
    total = struct.unpack(">Q", head[8:16])[0]
    print(f"消息总长 {total/2**20:.1f} MB")

    # Sections 1 and 3 are shared by every field and sit at the front.
    prefix = rng(16, 4096)
    downloaded += len(prefix)
    position = 0
    shared: dict[int, bytes] = {}
    order: list[int] = []
    while position + 5 <= len(prefix):
        length = struct.unpack(">I", prefix[position:position + 4])[0]
        section = prefix[position + 4]
        if length <= 0 or position + length > len(prefix):
            break
        if section in (1, 2, 3):
            shared[section] = prefix[position:position + length]
            order.append(section)
            position += length
            continue
        break
    first_group = 16 + position
    print(f"共享段 {order}，首个场组起于 {first_group}")

    # Walk the section-4 chain.  Only the product definitions are read.
    absolute = first_group
    target = None
    groups = 0
    while absolute < total - 4:
        header = rng(absolute, 5)
        downloaded += len(header)
        if len(header) < 5:
            break
        length = struct.unpack(">I", header[0:4])[0]
        section = header[4]
        if section == 4:
            body = rng(absolute, length)
            downloaded += len(body)
            # Template 4.0: parameter category at 9, number at 10, forecast
            # time at 18-21, first fixed surface type at 22.
            category = body[9]
            number = body[10]
            step = struct.unpack(">I", body[18:22])[0]
            surface = body[22]
            groups += 1
            if (category, number, surface) == (0, 0, 103) and step == WANT_FH:
                target = absolute
                print(f"  命中: 偏移 {absolute} FH{step} "
                      f"(走过 {groups} 个场)")
                break
            # Skip this field's 5, 6 and 7 in one go.
            cursor = absolute + length
            for _ in range(3):
                nxt = rng(cursor, 5)
                downloaded += len(nxt)
                if len(nxt) < 5:
                    break
                cursor += struct.unpack(">I", nxt[0:4])[0]
            absolute = cursor
            continue
        absolute += length
    if target is None:
        print("未找到目标场")
        return 1

    # Fetch the whole 4-5-6-7 group for the field that matched.
    cursor = target
    group = b""
    for _ in range(4):
        header = rng(cursor, 5)
        downloaded += len(header)
        length = struct.unpack(">I", header[0:4])[0]
        group += rng(cursor, length)
        downloaded += length
        cursor += length
    print(f"场组 {len(group)/1024:.0f} KB")

    message = (
        head[:8]
        + struct.pack(">Q", 16 + sum(len(shared[s]) for s in order)
                      + len(group) + 4)
        + b"".join(shared[s] for s in order)
        + group
        + b"7777"
    )
    library = _eccodes()
    buffer = ctypes.create_string_buffer(message, len(message))
    handle = library.codes_handle_new_from_message(
        None, buffer, ctypes.c_size_t(len(message))
    )
    if not handle:
        print("eccodes 打不开重组的消息")
        return 1
    try:
        def gl(key):
            out = ctypes.c_long()
            rc = library.codes_get_long(handle, key.encode(),
                                        ctypes.byref(out))
            return out.value if rc == 0 else None

        size = ctypes.c_size_t()
        library.codes_get_size(handle, b"values", ctypes.byref(size))
        values = (ctypes.c_double * size.value)()
        count = ctypes.c_size_t(size.value)
        library.codes_get_double_array(
            handle, b"values", values, ctypes.byref(count)
        )
        ni, nj = gl("Ni"), gl("Nj")
        first_lat = gl("latitudeOfFirstGridPoint") / 1e6
        first_lon = gl("longitudeOfFirstGridPoint") / 1e6
        di = gl("iDirectionIncrement") / 1e6
        dj = gl("jDirectionIncrement") / 1e6
        print(f"网格 {ni}x{nj}={size.value}  首点 {first_lat:.3f}N"
              f" {first_lon:.3f}E  步长 {di:.4f}x{dj:.4f}")
        row = round(abs(RCSS_LAT - first_lat) / dj)
        col = round(abs(RCSS_LON - first_lon) / di)
        cell = row * ni + col
        kelvin = values[cell]
        print(f"\n✅ RCSS [{row},{col}] FH{WANT_FH}: "
              f"{kelvin:.2f}K = {kelvin - 273.15:.2f}°C")
    finally:
        library.codes_handle_delete(handle)

    print(f"\n下载 {downloaded/1024:.0f} KB（整文件 {total/2**20:.0f} MB，"
          f"省 {total/max(downloaded,1):.0f}×），耗时 "
          f"{time.time()-started:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
