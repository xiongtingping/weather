#!/usr/bin/env python3
"""IEM daily highs 2025-12-15..2026-07-31 for the 45 stations, by production's
IEM audit rules (fetch, settlement-reading of temperatures, warm-window
completeness).  Network read only; writes iem_daily_high.json here."""
from __future__ import annotations
import json, sqlite3, sys, time
from datetime import date, timedelta
from pathlib import Path
HERE = Path(__file__).resolve().parent; ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts"))
import httpx
from bayes_basket_target.contracts import ContractError
from bayes_basket_target.iem_truth_audit import USER_AGENT, fetch_iem_reports, iem_day_summary, iem_station_id
from bayes_basket_target.station_registry import _COORDINATES
from official_nwp_https_transport import official_nwp_proxy_url
OUT = HERE / "iem_daily_high.json"
done = json.loads(OUT.read_text()) if OUT.exists() else {}
conn = sqlite3.connect(f"file:{ROOT/'data/bayes_basket_target_ssot.db'}?mode=ro", uri=True, timeout=30)
zones = dict(conn.execute("SELECT station_id, settlement_timezone FROM calibration_station_market_timezone_binding").fetchall()); conn.close()
FIRST, LAST = date(2025, 12, 15), date(2026, 7, 31)
CHUNKS = [(date(2025, 12, 14), date(2026, 1, 31)), (date(2026, 1, 31), date(2026, 3, 31)), (date(2026, 3, 31), date(2026, 5, 31)), (date(2026, 5, 31), date(2026, 8, 1))]
t0 = time.monotonic()
with httpx.Client(proxy=official_nwp_proxy_url() or None, trust_env=False, timeout=httpx.Timeout(120.0, connect=20.0),
                  headers={"User-Agent": USER_AGENT}, follow_redirects=False) as client:
    for station in sorted(_COORDINATES):
        if station in done: continue
        reports = []
        for a, b in CHUNKS:
            for attempt in range(5):
                try:
                    reports += fetch_iem_reports(client, iem_id=iem_station_id(station), first_utc_date=a, last_utc_date=b, max_bytes=8 * 1024 * 1024)
                    break
                except (httpx.HTTPError, OSError, ContractError) as exc:
                    print(f"  {station} {a}..{b} attempt {attempt+1}: {type(exc).__name__}", flush=True); time.sleep(45.0 * (attempt + 1))
            else:
                raise SystemExit(f"{station}: IEM fetch failed 5 times; stop")
            time.sleep(6.0)
        days = {}
        d = FIRST
        while d <= LAST:
            s = iem_day_summary(reports, event_date=d.isoformat(), settlement_timezone=zones[station])
            gap = s.get("warm_window_gap") or {}
            days[d.isoformat()] = {"max_c": s.get("max_c"), "n": s.get("report_count"), "complete": bool(isinstance(gap, dict) and gap.get("complete") is True)}
            d += timedelta(days=1)
        done[station] = days
        OUT.write_text(json.dumps(done))
        ok = sum(1 for v in days.values() if v["complete"] and v["max_c"] is not None)
        print(f"{station}: reports={len(reports)} complete_days={ok}/{len(days)} t={time.monotonic()-t0:.0f}s", flush=True)
        if time.monotonic() - t0 > 540: print("time budget reached; rerun to continue", flush=True); break
