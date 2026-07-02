#!/usr/bin/env python3
"""Fetch a read-only Chengdu climate reference cache for P_climo_regime.

The output is not settlement truth and is never written to archive.db. It is a
multi-year background reference used only by the weak P_climo_regime layer.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "data" / "climo" / "chengdu_zuuu_open_meteo_climo_2016_2025.json"
OPEN_METEO_ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--latitude", type=float, default=30.5785)
    parser.add_argument("--longitude", type=float, default=103.9471)
    parser.add_argument("--start-date", default="2016-01-01")
    parser.add_argument("--end-date", default="2025-12-31")
    parser.add_argument("--timezone", default="Asia/Shanghai")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def build_payload(args: argparse.Namespace) -> dict[str, Any]:
    params = {
        "latitude": args.latitude,
        "longitude": args.longitude,
        "start_date": args.start_date,
        "end_date": args.end_date,
        "daily": "temperature_2m_max,dew_point_2m_mean,wind_speed_10m_mean,precipitation_sum",
        "timezone": args.timezone,
    }
    response = requests.get(OPEN_METEO_ARCHIVE_URL, params=params, timeout=60)
    response.raise_for_status()
    payload = response.json()
    daily = payload.get("daily") if isinstance(payload.get("daily"), dict) else {}
    return {
        "source": "open_meteo_archive_reference_prior_years",
        "provider": "Open-Meteo Archive API",
        "role": "P_climo_regime weak climate/circulation background reference",
        "station_id": "ZUUU",
        "city": "chengdu",
        "is_settlement_truth": False,
        "is_observed_nowcast": False,
        "created_at_guard": "runtime_uses_only_years_before_target_event_year",
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "request_url": response.url,
        "sample_count": len(daily.get("time") or []),
        **payload,
    }


def main() -> int:
    args = parse_args()
    payload = build_payload(args)
    out = Path(args.out)
    if args.dry_run:
        print(json.dumps({k: payload.get(k) for k in ("source", "provider", "sample_count", "request_url")}, indent=2))
        return 0
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"ok": True, "out": str(out), "sample_count": payload.get("sample_count")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
