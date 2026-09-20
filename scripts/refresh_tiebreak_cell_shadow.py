#!/usr/bin/env python3
"""Replay the tiebreak rule over history and score the registered hypotheses.

Charter 164.  Read-only against the SSOT; everything it writes goes to
``data/tiebreak_cell_shadow.db``.  Without ``--commit`` it prints the
readings and writes nothing.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import sqlite3  # noqa: E402

from bayes_basket_target.tiebreak_cell_shadow import (  # noqa: E402
    DEFAULT_FLOOR_EVENT_DATE,
    PRODUCTION_SSOT_DB,
    hypothesis_readings,
    one_per_station_day,
    open_shadow_connection,
    refresh,
    replay_cell,
)


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:+.1f}%"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--floor", default=DEFAULT_FLOOR_EVENT_DATE)
    parser.add_argument("--commit", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    # A dry run reads the store and nothing else: it neither creates the
    # shadow file nor needs one to exist.
    conn = (
        open_shadow_connection(writable=True) if args.commit
        else sqlite3.connect(f"file:{PRODUCTION_SSOT_DB}?mode=ro", uri=True, timeout=120.0)
    )
    try:
        if args.commit:
            result = refresh(conn, floor_event_date=args.floor)
            print(json.dumps(result, ensure_ascii=False))
            readings = conn.execute(
                "SELECT hypothesis_id,reading_window,n_treatment,n_control,event_days,"
                " contrast,ci_lo,ci_hi,p_at_or_below_zero,p_permutation FROM tiebreak_shadow."
                "tiebreak_cell_hypothesis WHERE as_of_date=?",
                (result["as_of_date"],),
            ).fetchall()
            rows = [
                {"hypothesis_id": r[0], "reading_window": r[1], "n_treatment": r[2],
                 "n_control": r[3], "event_days": r[4], "contrast": r[5],
                 "ci_lo": r[6], "ci_hi": r[7], "p_at_or_below_zero": r[8],
                 "p_permutation": r[9]}
                for r in readings
            ]
        else:
            replay = replay_cell(conn, floor_event_date=args.floor)
            grain = one_per_station_day(replay)
            print(json.dumps({
                "replayed": len(replay), "station_days": len(grain),
                "event_days": len({r["event_date"] for r in replay}),
                "committed": False,
            }, ensure_ascii=False))
            rows = hypothesis_readings(replay)
        if args.json:
            print(json.dumps(rows, ensure_ascii=False))
            return 0
        print(f"{'假设':22}{'窗口':>9}{'处理n':>7}{'对照n':>7}{'事件日':>7}"
              f"{'差/ROI':>9}{'90%区间':>22}{'P(<=0)':>8}{'重排p':>7}")
        for row in rows:
            span = (
                f"[{_pct(row['ci_lo'])}, {_pct(row['ci_hi'])}]"
                if row.get("ci_lo") is not None else "事件日太少"
            )
            p = row.get("p_at_or_below_zero")
            print(f"{row['hypothesis_id']:22}{row['reading_window']:>9}{row['n_treatment']:>7}"
                  f"{row['n_control']:>7}{row['event_days']:>7}{_pct(row.get('contrast')):>9}"
                  f"{span:>22}{('—' if p is None else f'{p:.2f}'):>8}"
                  f"{'—' if row.get('p_permutation') is None else format(row['p_permutation'], '.2f'):>7}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
