#!/usr/bin/env python3
"""Check whether newly listed markets can be admitted as target stations.

Market discovery runs ahead of station onboarding: Polymarket listed Jinan and
Zhengzhou days before either had a coordinate entry or a timezone binding, and
because those rows carried no ``settlement_timezone`` they silently failed the
registry export -- freezing the published catalog for two days behind them.

Admission is not automatic, because a station without observations cannot be
settled.  Jinan is the worked example: it publishes TAF but no METAR at all, so
it can never produce a daily-high truth however complete its market listing
looks.  Each candidate is therefore checked against the four things onboarding
actually needs, and one that fails any of them is recorded with the reason
rather than quietly dropped.

Read-only.  It proposes onboarding; it never edits the coordinate registry.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.station_registry import (  # noqa: E402
    coordinate_registry_manifest,
)

AWC_BASE = "https://aviationweather.gov/api/data"
REQUEST_TIMEOUT_SECONDS = 30.0
# A station needs enough recent METAR to establish a daily high, not merely one
# report that happens to be present at probe time.
MIN_METAR_REPORTS = 3
METAR_LOOKBACK_HOURS = 12


def _proxy() -> str | None:
    import os

    for name in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        value = str(os.environ.get(name) or "").strip()
        if value:
            return value
    return None


def _awc(client: httpx.Client, path: str, **params: object) -> object:
    query = urllib.parse.urlencode({**params, "format": "json"})
    response = client.get(f"{AWC_BASE}/{path}?{query}")
    response.raise_for_status()
    try:
        return response.json()
    except ValueError:
        return []


def _candidates(conn: sqlite3.Connection) -> list[dict[str, str]]:
    """Markets whose station cannot yet be placed on a settlement day."""

    rows = conn.execute(
        "SELECT DISTINCT station_id,city_slug,COUNT(*) AS listings,"
        "       MIN(event_date) AS first_date,MAX(event_date) AS last_date "
        "FROM reported_trajectory_market_registry "
        "WHERE (settlement_timezone IS NULL OR settlement_timezone='') "
        "  AND station_id IS NOT NULL AND station_id<>'' "
        "GROUP BY station_id,city_slug ORDER BY station_id"
    ).fetchall()
    return [
        {
            "station_id": str(row[0]).strip().upper(),
            "city_slug": str(row[1] or ""),
            "listing_count": int(row[2] or 0),
            "first_event_date": str(row[3] or ""),
            "last_event_date": str(row[4] or ""),
        }
        for row in rows
    ]


def _assess(
    client: httpx.Client, candidate: dict[str, str], *, known: set[str]
) -> dict[str, object]:
    station = str(candidate["station_id"])
    checks: dict[str, object] = {}
    blockers: list[str] = []

    checks["coordinate_registered"] = station in known
    if not checks["coordinate_registered"]:
        blockers.append("coordinate_registry_missing")

    try:
        info = _awc(client, "stationinfo", ids=station)
        entry = info[0] if isinstance(info, list) and info else {}
    except Exception as exc:
        entry = {}
        blockers.append(f"stationinfo_unavailable:{type(exc).__name__}")
    checks["awc_stationinfo"] = {
        "latitude": entry.get("lat"),
        "longitude": entry.get("lon"),
        "site": entry.get("site"),
        "country": entry.get("country"),
    }
    if entry.get("lat") is None or entry.get("lon") is None:
        blockers.append("awc_coordinates_missing")

    try:
        metar = _awc(
            client, "metar", ids=station, hours=METAR_LOOKBACK_HOURS
        )
        metar_count = len(metar) if isinstance(metar, list) else 0
    except Exception as exc:
        metar_count = 0
        blockers.append(f"metar_unavailable:{type(exc).__name__}")
    checks["metar_reports_12h"] = metar_count
    if metar_count < MIN_METAR_REPORTS:
        # Without METAR there is no settled daily high, so the station can
        # never be scored or settled no matter how its market looks.
        blockers.append("metar_insufficient")

    try:
        taf = _awc(client, "taf", ids=station)
        taf_count = len(taf) if isinstance(taf, list) else 0
    except Exception as exc:
        taf_count = 0
        blockers.append(f"taf_unavailable:{type(exc).__name__}")
    checks["taf_reports"] = taf_count
    if taf_count < 1:
        blockers.append("taf_missing")

    return {
        **candidate,
        "checks": checks,
        "blockers": blockers,
        "admissible": not blockers,
        "proposed_coordinates": (
            {
                "latitude": entry.get("lat"),
                "longitude": entry.get("lon"),
            }
            if entry.get("lat") is not None and entry.get("lon") is not None
            else None
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--shared-db", type=Path, default=ROOT / "data" / "archive.db"
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    database = args.shared_db.expanduser().resolve(strict=True)
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as conn:
        conn.execute("PRAGMA query_only=ON")
        candidates = _candidates(conn)

    known = set(coordinate_registry_manifest()["station_ids"])
    assessed: list[dict[str, object]] = []
    if candidates:
        with httpx.Client(
            timeout=REQUEST_TIMEOUT_SECONDS,
            follow_redirects=True,
            proxy=_proxy(),
        ) as client:
            for candidate in candidates:
                assessed.append(_assess(client, candidate, known=known))

    admissible = [item for item in assessed if item["admissible"]]
    blocked = [item for item in assessed if not item["admissible"]]
    report = {
        "schema": "new_market_station_admission_audit_v1",
        "audited_at_utc": (
            datetime.now(timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        ),
        "registered_station_count": len(known),
        "candidate_count": len(assessed),
        "admissible_count": len(admissible),
        "blocked_count": len(blocked),
        "admissible": admissible,
        "blocked": blocked,
        "writes_registry": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }
    if args.output is not None:
        output = args.output.expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
    for item in assessed:
        mark = "ADMISSIBLE" if item["admissible"] else "BLOCKED"
        detail = (
            f"metar={item['checks']['metar_reports_12h']} "
            f"taf={item['checks']['taf_reports']}"
        )
        reasons = ",".join(item["blockers"]) or "-"
        print(
            f"{mark:11s} {item['station_id']:6s} {item['city_slug']:14s} "
            f"{detail:20s} {reasons}"
        )
    print(json.dumps({
        "candidates": len(assessed),
        "admissible": len(admissible),
        "blocked": len(blocked),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
