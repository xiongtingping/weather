"""At what |M| does charter 266's momentum say more than 「if the day leaves
the market mode, which way」 (user 2026-10-25 「找到方向只说「如果离开众数，往哪
边」的阈值！即动量 M的大小！」).

Same rows as ``analyze.py`` (prices left out).  For each |M| floor k: among
station-day-times with |M| >= k, the shares of truth on M's side, on the mode,
and on the other side; whether the day leaves the mode more often than with M
quiet; and the hit rate when it leaves.  The threshold asked for is the
smallest k at which M's side is reached more often than the mode itself
(P(side) - P(mode) > 0 with its 95% interval clear of zero) -- from there M
says where the day lands, not only which way it would leave.  Also the
same in disjoint |M| bands, and 10-09-on alone (after charter 266's window).

    .venv/bin/python -I docs/research/direction_mos_truth_20261025/m_threshold.py
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import analyze as A  # noqa: E402

FLOORS = (0.0, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 6.0, 8.0)
BANDS = ((0.0, 1.0), (1.0, 2.0), (2.0, 3.0), (3.0, 4.0), (4.0, 6.0), (6.0, 99.0))


def side(r):
    return A.sgn(r["M_raw"])


def share(kind):
    def stat(rows):
        if not rows:
            return None
        if kind == "side":
            return sum(r["y"] == side(r) for r in rows) / len(rows)
        if kind == "mode":
            return sum(r["y"] == 0 for r in rows) / len(rows)
        if kind == "other":
            return sum(r["y"] == -side(r) for r in rows) / len(rows)
        if kind == "side_minus_mode":
            return (sum(r["y"] == side(r) for r in rows) - sum(r["y"] == 0 for r in rows)) / len(rows)
        if kind == "hit_if_left":
            left = [r for r in rows if r["y"]]
            return None if not left else sum(r["y"] == side(r) for r in left) / len(left)
    return stat


def table(rows, quiet_leave, title):
    print(f"\n== {title}（M 静默 |M|<1 时真值离开众数 {quiet_leave:.0%}）")
    print("|M|≥k | n | 落 M 那侧 | 正落众数 | 落反侧 | 那侧−众数 [95%] | 离开时命中 [95%]")
    for k in FLOORS:
        rs = [r for r in rows if r["M_raw"] is not None and abs(r["M_raw"]) >= k and side(r)]
        if len(rs) < 20:
            print(f"{k:>4} | {len(rs)} | —")
            continue
        s, m, o = (share(x)(rs) for x in ("side", "mode", "other"))
        dm = A.boot(rs, share("side_minus_mode"))
        hl = A.boot(rs, share("hit_if_left"), lambda r: r["y"])
        print(f"{k:>4} | {len(rs)} | {s:.0%} | {m:.0%} | {o:.0%} | {A.fmt(dm)} | {A.fmt((hl[0] - 0.5, (hl[1][0] - 0.5, hl[1][1] - 0.5) if hl[1] else None, hl[2], hl[3]))}")
    print("分档（互不重叠）")
    for lo, hi in BANDS:
        rs = [r for r in rows if r["M_raw"] is not None and lo <= abs(r["M_raw"]) < hi and side(r)]
        if len(rs) < 20:
            print(f"  [{lo},{hi}) n={len(rs)} —")
            continue
        s, m, o = (share(x)(rs) for x in ("side", "mode", "other"))
        hl = share("hit_if_left")(rs)
        print(f"  [{lo},{hi}) n={len(rs)} d={len({r['day'] for r in rs})}: 那侧 {s:.0%} 众数 {m:.0%} 反侧 {o:.0%} 离开时命中 {hl:.0%}")


def main():
    conn = sqlite3.connect(f"file:{A.DB}?mode=ro", uri=True, timeout=60)
    conn.execute("PRAGMA query_only=ON")
    rows = A.build_direction(conn)
    for title, rs in (("全部 09-24..", rows), ("前推 ≥10-09", [r for r in rows if r["day"] >= A.FORWARD_FROM]),
                      ("拟合窗内 <10-09", [r for r in rows if r["day"] < A.FORWARD_FROM])):
        quiet = [r for r in rs if r["M_raw"] is not None and abs(r["M_raw"]) < 1.0]
        ql = sum(r["y"] != 0 for r in quiet) / len(quiet) if quiet else float("nan")
        table(rs, ql, title)
    for band, _lo, _hi in A.BANDS:
        rs = [r for r in rows if r["band"] == band]
        quiet = [r for r in rs if r["M_raw"] is not None and abs(r["M_raw"]) < 1.0]
        table(rs, sum(r["y"] != 0 for r in quiet) / max(1, len(quiet)), f"距结算 {band}")


if __name__ == "__main__":
    main()
