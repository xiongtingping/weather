#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import (  # noqa: E402
    paper_observation_rolling_validation,
    paper_observation_target_package,
    settlement_label_eligibility_report,
    strategy_objective_completion_audit,
)


DEFAULT_DB = ROOT / "data" / "archive.db"
DEFAULT_AUDIT = ROOT / "data" / "reports" / "strategy_objective_completion_audit_latest.json"
DEFAULT_OUT = ROOT / "data" / "reports" / "recommended_pilot_settlement_wakeup_latest.json"
DEFAULT_MARKET_SCOPE = "highest_temperature"


def _load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


def _city_key(value: Any) -> str:
    text = str(value or "").strip().lower().replace("_", "-").replace(" ", "-")
    while "--" in text:
        text = text.replace("--", "-")
    return text.strip("-")


def _csv_values(values: Sequence[Any]) -> list[str]:
    out: list[str] = []
    for value in values or []:
        for part in str(value or "").split(","):
            text = part.strip()
            if text and text not in out:
                out.append(text)
    return out


def _watched_markets(audit: Mapping[str, Any]) -> list[dict[str, Any]]:
    target = (
        ((audit.get("current_strategy_batch_summary") or {}).get("targeted_candidate_status") or {})
        if isinstance(audit.get("current_strategy_batch_summary"), Mapping)
        else {}
    )
    readiness = (
        target.get("recommended_pilot_settlement_readiness")
        if isinstance(target.get("recommended_pilot_settlement_readiness"), Mapping)
        else {}
    )
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()

    def add_market(city: Any, slug: Any, event_date: Any = "", previous_status: str = "") -> None:
        city_key = _city_key(city)
        slug_text = str(slug or "").strip()
        event_day = str(event_date or "").split("T", 1)[0]
        if not city_key or not slug_text:
            return
        key = (city_key, slug_text, event_day)
        if key in seen:
            return
        seen.add(key)
        out.append(
            {
                "city": city_key,
                "slug": slug_text,
                "event_date": event_day,
                "previous_status": previous_status or "paper_observation_pending_settlement",
            }
        )

    for city, item in (readiness.get("by_city") or {}).items():
        if not isinstance(item, Mapping):
            continue
        for market in item.get("eligibility_markets_preview") or []:
            if not isinstance(market, Mapping):
                continue
            if str(market.get("status") or "") != "settlement_not_closed":
                continue
            add_market(
                city,
                market.get("slug"),
                market.get("event_date"),
                "settlement_not_closed",
            )

    scoped_cities: set[str] = set()
    for row in target.get("target_hunt_plan") or []:
        if not isinstance(row, Mapping):
            continue
        city_key = _city_key(row.get("city"))
        if city_key:
            scoped_cities.add(city_key)
        for slug in row.get("paper_observation_slugs") or []:
            add_market(city_key, slug)
    for row in target.get("expansion_hunt_plan") or []:
        if not isinstance(row, Mapping):
            continue
        city_key = _city_key(row.get("city"))
        if city_key:
            scoped_cities.add(city_key)

    availability = (
        target.get("paper_observation_availability")
        if isinstance(target.get("paper_observation_availability"), Mapping)
        else {}
    )
    for bucket in availability.values():
        if not isinstance(bucket, Mapping):
            continue
        city_key = _city_key(bucket.get("city"))
        if scoped_cities and city_key not in scoped_cities:
            continue
        for slug in bucket.get("paper_observation_slugs") or []:
            add_market(city_key, slug)
    return out


def _eligibility_by_key(
    db_path: Path,
    *,
    city: str,
    since_days: int,
    market_scope: str,
) -> dict[tuple[str, str], dict[str, Any]]:
    report = settlement_label_eligibility_report.build_report(
        db_path,
        since_days=max(1, int(since_days)),
        city=city,
        market_scope=str(market_scope or DEFAULT_MARKET_SCOPE),
        bayes_only=True,
        allow_open_events=False,
        strict_resolution_source=True,
        limit=0,
    )
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for item in report.get("markets") or []:
        if not isinstance(item, Mapping):
            continue
        slug = str(item.get("slug") or "").strip()
        event_date = str(item.get("event_date") or "").split("T", 1)[0]
        if slug and event_date:
            out[(slug, event_date)] = dict(item)
    return out


def _eligibility_for_watched_market(
    by_key: Mapping[tuple[str, str], Mapping[str, Any]],
    *,
    slug: str,
    event_date: str,
) -> Mapping[str, Any]:
    if event_date:
        return by_key.get((slug, event_date), {})
    for (candidate_slug, _candidate_date), item in by_key.items():
        if candidate_slug == slug:
            return item
    return {}


def _wakeup_reason_for_status(status: str) -> str:
    if status == "label_ready":
        return "paper_observation_label_ready"
    if status == "closed_without_settlement_actual":
        return "settlement_actual_missing_after_close"
    if status == "no_unlabeled_parseable_decisions":
        return "paper_observation_already_labeled_or_not_parseable"
    if status == "settlement_missing":
        return "settlement_inputs_missing"
    return "paper_observation_settlement_status_changed"


def _next_action_for_triggered(triggered: Sequence[Mapping[str, Any]]) -> str:
    statuses = {str(item.get("current_status") or "") for item in triggered}
    if "label_ready" in statuses:
        return "review_and_apply_settlement_calibration_labels_then_refresh_validation"
    if "closed_without_settlement_actual" in statuses:
        return "repair_missing_settlement_actual_for_closed_markets"
    if triggered:
        return "run_with_refresh_to_update_validation_reports"
    return "wait_for_paper_observation_settlement_close"


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _targeted_candidate_status(audit: Mapping[str, Any]) -> Mapping[str, Any]:
    summary = (
        audit.get("current_strategy_batch_summary")
        if isinstance(audit.get("current_strategy_batch_summary"), Mapping)
        else {}
    )
    status = (
        summary.get("targeted_candidate_status")
        if isinstance(summary.get("targeted_candidate_status"), Mapping)
        else {}
    )
    return status if isinstance(status, Mapping) else {}


def _recommended_pilot_validation_snapshot(audit: Mapping[str, Any]) -> dict[str, Any]:
    status = _targeted_candidate_status(audit)
    validation = (
        status.get("recommended_pilot_observation_validation")
        if isinstance(status.get("recommended_pilot_observation_validation"), Mapping)
        else {}
    )
    by_city: list[dict[str, Any]] = []
    for item in validation.get("by_city") or []:
        if not isinstance(item, Mapping):
            continue
        overall = item.get("overall") if isinstance(item.get("overall"), Mapping) else {}
        by_city.append(
            {
                "city": str(item.get("city") or ""),
                "ok": bool(item.get("ok")),
                "observation_mode": str(item.get("observation_mode") or ""),
                "resolved_rows": _as_int(overall.get("resolved_rows")),
                "pending_rows": _as_int(overall.get("pending_rows")),
                "wins": _as_int(overall.get("wins")),
                "hit_rate": overall.get("hit_rate"),
                "blockers": list(item.get("blockers") or [])[:5],
                "next_action": str(item.get("next_action") or ""),
            }
        )
    thresholds = (
        validation.get("thresholds")
        if isinstance(validation.get("thresholds"), Mapping)
        else {}
    )
    return {
        "ok": bool(validation.get("ok")) if validation else False,
        "city_count": _as_int(validation.get("city_count")),
        "thresholds": dict(thresholds),
        "by_city": by_city,
        "sample_gap_queue": list(validation.get("sample_gap_queue") or [])[:5],
        "blockers": list(validation.get("blockers") or [])[:8],
        "next_action": str(validation.get("next_action") or ""),
    }


def _refresh_reports(
    db_path: Path,
    audit: Mapping[str, Any],
    *,
    since_days: int,
    market_scope: str,
) -> dict[str, Any]:
    event_dates = _csv_values(audit.get("event_dates") or [])
    pilot_cities = _csv_values(audit.get("pilot_cities") or [])
    target = paper_observation_target_package.build_package(
        db_path,
        event_dates=event_dates[-2:],
        market_scope=str(market_scope or DEFAULT_MARKET_SCOPE),
        cities=pilot_cities,
    )
    _write_json(paper_observation_target_package.DEFAULT_OUT_JSON, target)
    paper_observation_target_package.DEFAULT_OUT_MD.write_text(
        paper_observation_target_package.format_markdown(target) + "\n",
        encoding="utf-8",
    )
    rolling = paper_observation_rolling_validation.build_report(
        db_path,
        pilot_cities=pilot_cities,
        since_days=max(1, int(since_days)),
        market_scope=str(market_scope or DEFAULT_MARKET_SCOPE),
    )
    _write_json(paper_observation_rolling_validation.DEFAULT_OUT, rolling)
    objective = strategy_objective_completion_audit.build_report(
        db_path,
        event_dates=event_dates,
        pilot_cities=pilot_cities,
        market_scope=str(market_scope or DEFAULT_MARKET_SCOPE),
        since_days=max(1, int(since_days)),
    )
    _write_json(strategy_objective_completion_audit.DEFAULT_OUT, objective)
    validation = _recommended_pilot_validation_snapshot(objective)
    return {
        "target_package": str(paper_observation_target_package.DEFAULT_OUT_JSON),
        "rolling_validation": str(paper_observation_rolling_validation.DEFAULT_OUT),
        "objective_audit": str(strategy_objective_completion_audit.DEFAULT_OUT),
        "objective_ok": bool(objective.get("ok")),
        "recommended_pilot_observation_validation": validation,
    }


def build_report(
    db_path: Path = DEFAULT_DB,
    *,
    audit_path: Path = DEFAULT_AUDIT,
    since_days: int = 42,
    market_scope: str = DEFAULT_MARKET_SCOPE,
    refresh: bool = False,
) -> dict[str, Any]:
    audit = _load_json(audit_path)
    current_validation = _recommended_pilot_validation_snapshot(audit)
    watched = _watched_markets(audit)
    triggered: list[dict[str, Any]] = []
    by_city: dict[str, dict[tuple[str, str], dict[str, Any]]] = {}
    for item in watched:
        city = str(item.get("city") or "")
        if city not in by_city:
            by_city[city] = _eligibility_by_key(
                db_path,
                city=city,
                since_days=max(1, int(since_days)),
                market_scope=str(market_scope or DEFAULT_MARKET_SCOPE),
            )
        current = _eligibility_for_watched_market(
            by_city[city],
            slug=str(item.get("slug") or ""),
            event_date=str(item.get("event_date") or ""),
        )
        status = str(current.get("status") or "event_run_missing")
        current_item = {
            **item,
            "event_date": str(item.get("event_date") or current.get("event_date") or "").split("T", 1)[0],
            "current_status": status,
            "planned_labels": _as_int(current.get("planned_labels")),
            "settlement_actual": current.get("settlement_actual"),
            "resolution_source": current.get("resolution_source"),
            "station_id": current.get("station_id"),
        }
        if status != "settlement_not_closed":
            current_item["wakeup_reason"] = _wakeup_reason_for_status(status)
            triggered.append(current_item)
    refresh_result: dict[str, Any] = {}
    if refresh and triggered:
        refresh_result = _refresh_reports(
            db_path,
            audit,
            since_days=max(1, int(since_days)),
            market_scope=str(market_scope or DEFAULT_MARKET_SCOPE),
        )
    return {
        "ok": True,
        "read_only": not bool(refresh and triggered),
        "audit_path": str(audit_path),
        "db": str(db_path),
        "since_days": max(1, int(since_days)),
        "market_scope": str(market_scope or DEFAULT_MARKET_SCOPE),
        "watched_market_count": len(watched),
        "triggered_market_count": len(triggered),
        "triggered_status_counts": {
            status: sum(1 for item in triggered if str(item.get("current_status") or "") == status)
            for status in sorted({str(item.get("current_status") or "") for item in triggered})
        },
        "watched_markets": watched,
        "triggered_markets": triggered,
        "recommended_pilot_observation_validation": (
            refresh_result.get("recommended_pilot_observation_validation")
            if isinstance(refresh_result.get("recommended_pilot_observation_validation"), Mapping)
            else current_validation
        ),
        "refresh_requested": bool(refresh),
        "refresh_ran": bool(refresh_result),
        "refresh_result": refresh_result,
        "next_action": (
            "refreshed_target_package_audit_and_rolling_validation"
            if refresh_result
            else _next_action_for_triggered(triggered)
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Recommended pilot settlement wakeup check.")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--audit", default=str(DEFAULT_AUDIT))
    parser.add_argument("--since-days", type=int, default=42)
    parser.add_argument("--market-scope", default=DEFAULT_MARKET_SCOPE)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    report = build_report(
        Path(args.db),
        audit_path=Path(args.audit),
        since_days=int(args.since_days),
        market_scope=str(args.market_scope or DEFAULT_MARKET_SCOPE),
        refresh=bool(args.refresh),
    )
    if args.out:
        _write_json(Path(args.out), report)
    print(json.dumps(report, ensure_ascii=False, indent=None if args.json else 2, sort_keys=not args.json))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
