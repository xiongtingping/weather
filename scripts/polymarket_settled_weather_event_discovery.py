#!/usr/bin/env python3
"""Discover resolved Polymarket temperature events without touching local facts.

The default mode is plan-only and performs no network request.  ``--fetch``
allows bounded public Gamma reads.  File output is research-only and requires
both an explicit path and ``--allow-research-write``.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlencode
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from external_forecast_snapshot_runtime import redact_request_url


SCHEMA_VERSION = "polymarket_settled_weather_event_discovery_v1"
GAMMA_SEARCH_URL = "https://gamma-api.polymarket.com/public-search"
DEFAULT_RESEARCH_ROOT = ROOT / "data" / "research" / "forecast_source_skill"
USER_AGENT = "weather-source-skill-research/1.0"


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return list(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return list(parsed) if isinstance(parsed, list) else []
    return []


def _float(value: Any) -> float | None:
    try:
        output = float(value)
    except (TypeError, ValueError):
        return None
    return output


def _temperature_unit(text: str) -> str:
    match = re.search(r"°\s*([CF])\b", str(text or ""), flags=re.I)
    return match.group(1).upper() if match else ""


def bucket_bounds(label: Any) -> dict[str, Any] | None:
    text = str(label or "").strip().replace("–", "-")
    if not text:
        return None
    number = r"(-?\d+(?:\.\d+)?)"
    unit = _temperature_unit(text)
    match = re.match(rf"^{number}\s*(?:°?[CF])?\s+or\s+below$", text, flags=re.I)
    if match:
        value = float(match.group(1))
        return {"lower": None, "upper": value + 0.5, "lower_inclusive": False, "upper_inclusive": False, "unit": unit}
    match = re.match(rf"^{number}\s*(?:°?[CF])?\s+or\s+(?:higher|above|more)$", text, flags=re.I)
    if match:
        value = float(match.group(1))
        return {"lower": value - 0.5, "upper": None, "lower_inclusive": True, "upper_inclusive": False, "unit": unit}
    match = re.match(rf"^{number}\s*-\s*{number}\s*(?:°?[CF])?$", text, flags=re.I)
    if match:
        low, high = sorted((float(match.group(1)), float(match.group(2))))
        return {"lower": low - 0.5, "upper": high + 0.5, "lower_inclusive": True, "upper_inclusive": False, "unit": unit}
    match = re.match(rf"^{number}\s*(?:°?[CF])?$", text, flags=re.I)
    if match:
        value = float(match.group(1))
        return {"lower": value - 0.5, "upper": value + 0.5, "lower_inclusive": True, "upper_inclusive": False, "unit": unit}
    return None


def _winning_yes(market: Mapping[str, Any]) -> bool:
    if str(market.get("umaResolutionStatus") or "").strip().lower() != "resolved":
        return False
    outcomes = [str(value).strip().lower() for value in _json_list(market.get("outcomes"))]
    prices = [_float(value) for value in _json_list(market.get("outcomePrices"))]
    if len(outcomes) != len(prices) or "yes" not in outcomes:
        return False
    yes_price = prices[outcomes.index("yes")]
    return yes_price is not None and yes_price >= 0.999


def _event_date(event: Mapping[str, Any]) -> str:
    slug = str(event.get("slug") or "")
    match = re.search(r"-(\d{4})$", slug)
    title = str(event.get("title") or "")
    if match:
        year = int(match.group(1))
        month_day = re.search(
            r"\bon\s+(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2})",
            title,
            flags=re.I,
        )
        if month_day:
            try:
                parsed = datetime.strptime(
                    f"{year} {month_day.group(1)} {month_day.group(2)}", "%Y %B %d"
                )
                return parsed.date().isoformat()
            except ValueError:
                pass
    return str(event.get("eventDate") or event.get("endDate") or "")[:10]


def _station_id(resolution_source: str) -> str:
    path = str(resolution_source or "").rstrip("/").split("/")
    return str(path[-1] if path else "").strip().upper()


def normalize_resolved_weather_event(
    event: Mapping[str, Any],
    *,
    city: str = "Chengdu",
    station_id: str = "ZUUU",
) -> tuple[dict[str, Any] | None, list[str]]:
    issues: list[str] = []
    title = str(event.get("title") or "")
    if city and city.lower() not in title.lower():
        return None, ["event_city_mismatch"]
    if event.get("closed") is not True:
        return None, ["event_not_closed"]
    markets = [market for market in (event.get("markets") or []) if isinstance(market, Mapping)]
    resolved = [market for market in markets if str(market.get("umaResolutionStatus") or "").lower() == "resolved"]
    if not resolved:
        return None, ["resolved_markets_missing"]
    winners = [market for market in resolved if _winning_yes(market)]
    if len(winners) != 1:
        return None, ["winning_yes_market_not_unique"]
    sources = {
        str(market.get("resolutionSource") or event.get("resolutionSource") or "").strip()
        for market in resolved
    }
    sources.discard("")
    if len(sources) != 1:
        issues.append("resolution_source_not_unique")
    resolution_source = next(iter(sources), str(event.get("resolutionSource") or "").strip())
    resolved_station = _station_id(resolution_source)
    if station_id and resolved_station and resolved_station != station_id.upper():
        issues.append("resolution_station_mismatch")

    bucket_schema: list[dict[str, Any]] = []
    units: set[str] = set()
    for market in resolved:
        label = str(market.get("groupItemTitle") or market.get("question") or "").strip()
        bounds = bucket_bounds(label)
        if bounds is None:
            issues.append("bucket_label_unparseable")
            continue
        if bounds.get("unit"):
            units.add(str(bounds["unit"]))
        bucket_schema.append(
            {
                "market_id": str(market.get("id") or ""),
                "label": label,
                **bounds,
            }
        )
    if len(bucket_schema) != len(resolved):
        issues.append("bucket_schema_incomplete")
    if len(units) > 1:
        issues.append("temperature_unit_not_unique")
    winner = winners[0]
    winner_label = str(winner.get("groupItemTitle") or winner.get("question") or "").strip()
    winner_bounds = bucket_bounds(winner_label)
    if winner_bounds is None:
        issues.append("winning_bucket_unparseable")
    if issues:
        return None, sorted(set(issues))
    event_date = _event_date(event)
    if not event_date:
        return None, ["event_date_missing"]
    output = {
        "schema": "external_settled_weather_market_event_v1",
        "event_id": str(event.get("id") or ""),
        "event_slug": str(event.get("slug") or ""),
        "event_title": title,
        "station_id": resolved_station or station_id.upper(),
        "event_date_local": event_date,
        "timezone": "Asia/Shanghai" if (resolved_station or station_id.upper()) == "ZUUU" else "",
        "temperature_unit": next(iter(units), _temperature_unit(title)),
        "rounding_rule": "whole_degrees",
        "resolution_source": resolution_source,
        "closed_at": str(event.get("closedTime") or ""),
        "resolved_at": max((str(market.get("closedTime") or "") for market in resolved), default=""),
        "uma_resolution_status": "resolved",
        "bucket_schema": bucket_schema,
        "winning_bucket_label": winner_label,
        "winning_market_id": str(winner.get("id") or ""),
        "settlement_actual": {
            "kind": "resolved_market_bucket",
            "bucket_label": winner_label,
            "lower": winner_bounds.get("lower") if winner_bounds else None,
            "upper": winner_bounds.get("upper") if winner_bounds else None,
            "unit": winner_bounds.get("unit") if winner_bounds else next(iter(units), ""),
        },
        "market_count": len(resolved),
        "source_event_id": f"polymarket_gamma_event_{event.get('id')}",
    }
    return output, []


def gamma_search_url(*, query: str, page: int, limit_per_type: int) -> str:
    parameters = {
        "q": query,
        "events_status": "closed",
        "keep_closed_markets": 1,
        "limit_per_type": max(1, min(50, int(limit_per_type))),
        "page": max(1, int(page)),
        "search_profiles": "false",
    }
    return f"{GAMMA_SEARCH_URL}?{urlencode(parameters)}"


def fetch_gamma_search_page(*, query: str, page: int, limit_per_type: int, timeout_sec: float) -> dict[str, Any]:
    url = gamma_search_url(query=query, page=page, limit_per_type=limit_per_type)
    request = Request(url, headers={"Accept": "application/json", "User-Agent": USER_AGENT})
    with urlopen(request, timeout=max(1.0, float(timeout_sec))) as response:  # noqa: S310 - fixed HTTPS host
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("gamma_search_response_not_object")
    return dict(payload)


def build_discovery_report(
    pages: Sequence[Mapping[str, Any]],
    *,
    query: str,
    city: str,
    station_id: str,
    planned_page_count: int,
    fetched: bool,
) -> dict[str, Any]:
    events_by_id: dict[str, dict[str, Any]] = {}
    exclusions: dict[str, int] = {}
    raw_event_count = 0
    for page in pages:
        for event in page.get("events") or []:
            if not isinstance(event, Mapping):
                continue
            raw_event_count += 1
            normalized, issues = normalize_resolved_weather_event(event, city=city, station_id=station_id)
            if normalized is None:
                for issue in issues:
                    exclusions[issue] = exclusions.get(issue, 0) + 1
                continue
            events_by_id[str(normalized["event_id"])] = normalized
    events_by_day: dict[tuple[str, str, str], dict[str, Any]] = {}
    duplicate_event_day_trace: list[dict[str, Any]] = []
    for event in events_by_id.values():
        key = (
            str(event.get("station_id") or ""),
            str(event.get("event_date_local") or ""),
            str(event.get("temperature_unit") or ""),
        )
        previous = events_by_day.get(key)
        if previous is None:
            events_by_day[key] = event
            continue
        event_key = (str(event.get("resolved_at") or ""), int(event.get("market_count") or 0), str(event.get("event_id") or ""))
        previous_key = (str(previous.get("resolved_at") or ""), int(previous.get("market_count") or 0), str(previous.get("event_id") or ""))
        kept, superseded = (event, previous) if event_key > previous_key else (previous, event)
        events_by_day[key] = kept
        duplicate_event_day_trace.append(
            {
                "station_id": key[0],
                "event_date_local": key[1],
                "temperature_unit": key[2],
                "kept_event_id": str(kept.get("event_id") or ""),
                "kept_resolved_at": str(kept.get("resolved_at") or ""),
                "superseded_event_id": str(superseded.get("event_id") or ""),
                "superseded_resolved_at": str(superseded.get("resolved_at") or ""),
                "selection_rule": "latest_resolved_at_then_market_count_then_event_id",
            }
        )
        exclusions["duplicate_event_day_superseded"] = exclusions.get("duplicate_event_day_superseded", 0) + 1
    events = sorted(events_by_day.values(), key=lambda row: (str(row["event_date_local"]), str(row["event_id"])))
    request_urls = [
        redact_request_url(gamma_search_url(query=query, page=page, limit_per_type=50))
        for page in range(1, max(1, int(planned_page_count)) + 1)
    ]
    return {
        "schema": SCHEMA_VERSION,
        "generated_at": _now_iso(),
        "mode": "bounded_fetch_read_only" if fetched else "plan_only_no_network",
        "source_data_policy": "external_only",
        "query": query,
        "city": city,
        "station_id": station_id,
        "planned_request_count": max(1, int(planned_page_count)),
        "request_urls_redacted": request_urls,
        "fetched_page_count": len(pages),
        "raw_event_count": raw_event_count,
        "resolved_event_day_count": len(events),
        "event_date_min": min((row["event_date_local"] for row in events), default=None),
        "event_date_max": max((row["event_date_local"] for row in events), default=None),
        "resolution_sources": sorted({str(row["resolution_source"]) for row in events}),
        "excluded_counts_by_reason": dict(sorted(exclusions.items())),
        "duplicate_event_day_trace": duplicate_event_day_trace,
        "events": events,
        "impact": {
            "downloads_historical_forecasts": False,
            "writes_research_files": False,
            "writes_archive_db": False,
            "writes_facts_sqlite": False,
            "changes_parameters": False,
            "places_orders": False,
            "restarts_launchd": False,
        },
        "guards": {
            "reads_existing_local_history": False,
            "writes_research_files": False,
            "writes_archive_db": False,
            "writes_facts_sqlite": False,
            "changes_parameters": False,
            "places_orders": False,
            "restarts_launchd": False,
        },
        "next_boundary": "review_discovery_and_build_external_coverage_manifest_before_historical_download",
    }


def _safe_research_output(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    root = DEFAULT_RESEARCH_ROOT.resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError("research_output_outside_forecast_source_skill_root")
    return resolved


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--query", default="highest temperature Chengdu")
    parser.add_argument("--city", default="Chengdu")
    parser.add_argument("--station-id", default="ZUUU")
    parser.add_argument("--max-pages", type=int, default=1)
    parser.add_argument("--limit-per-type", type=int, default=50)
    parser.add_argument("--timeout-sec", type=float, default=15.0)
    parser.add_argument("--fetch", action="store_true", help="Perform bounded public Gamma reads.")
    parser.add_argument("--input-json", type=Path, help="Offline Gamma response object or list of response objects.")
    parser.add_argument("--output", type=Path, help="Optional research-only JSON output path.")
    parser.add_argument("--allow-research-write", action="store_true")
    args = parser.parse_args()

    max_pages = max(1, min(200, int(args.max_pages)))
    pages: list[dict[str, Any]] = []
    if args.input_json:
        raw = json.loads(args.input_json.read_text(encoding="utf-8"))
        pages = [dict(item) for item in raw] if isinstance(raw, list) else [dict(raw)]
    elif args.fetch:
        for page_number in range(1, max_pages + 1):
            payload = fetch_gamma_search_page(
                query=args.query,
                page=page_number,
                limit_per_type=args.limit_per_type,
                timeout_sec=args.timeout_sec,
            )
            pages.append(payload)
            pagination = payload.get("pagination") if isinstance(payload.get("pagination"), Mapping) else {}
            if pagination.get("hasMore") is False:
                break

    report = build_discovery_report(
        pages,
        query=args.query,
        city=args.city,
        station_id=args.station_id,
        planned_page_count=max_pages,
        fetched=bool(args.fetch or args.input_json),
    )
    if args.output:
        if not args.allow_research_write:
            raise SystemExit("--output requires --allow-research-write")
        output = _safe_research_output(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        report["impact"]["writes_research_files"] = True
        report["guards"]["writes_research_files"] = True
        report["output_path"] = str(output)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
