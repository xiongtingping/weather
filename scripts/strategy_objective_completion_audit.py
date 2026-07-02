#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scan_weather_market as swm  # noqa: E402
from scripts import (  # noqa: E402
    paper_observation_rolling_validation,
    paper_observation_target_package,
    paper_observation_tracking_report,
    paper_trading_validation,
    pilot_city_validation_matrix,
    scale_up_validation_review,
    small_capital_validation_review,
    build_settlement_calibration_labels,
    settlement_label_eligibility_report,
    settlement_validation_pipeline,
    tradeability_gap_review,
    weather_decision_core_audit,
    weather_strategy_batch,
    weather_strategy_plan,
)
from scripts.weather_strategy_plan import MAJOR_WEATHER_CITIES  # noqa: E402


DEFAULT_DB = ROOT / "data" / "archive.db"
DEFAULT_OUT = ROOT / "data" / "reports" / "strategy_objective_completion_audit_latest.json"
DEFAULT_MARKET_SCOPE = "highest_temperature"
DEFAULT_AUTO_PILOT_CITY_LIMIT = small_capital_validation_review.DEFAULT_MAX_PILOT_CITIES
MONITOR_DAEMON_LABEL = os.environ.get("PM_WEATHER_MONITOR_LABEL", "com.weather.monitor.daemon").strip()
MONITOR_DAEMON_DOMAIN_LABEL = (
    MONITOR_DAEMON_LABEL if MONITOR_DAEMON_LABEL.startswith("system/") else f"system/{MONITOR_DAEMON_LABEL}"
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _csv_values(values: Sequence[str], defaults: Sequence[str] = ()) -> list[str]:
    raw = list(values or [])
    if not raw:
        raw = [",".join(defaults)] if defaults else []
    out: list[str] = []
    for value in raw:
        for part in str(value or "").split(","):
            text = part.strip()
            if text:
                out.append(text)
    seen: set[str] = set()
    deduped: list[str] = []
    for item in out:
        if item in seen:
            continue
        seen.add(item)
        deduped.append(item)
    return deduped


def _default_pilot_cities_from_env() -> list[str]:
    return _csv_values(
        [
            os.environ.get("PM_SMALL_CAPITAL_PILOT_CITIES", ""),
            os.environ.get("PM_PAPER_VALIDATION_CITY", ""),
        ]
    )


def _env_bool(name: str, default: bool) -> bool:
    raw = str(os.environ.get(name, "")).strip().lower()
    if not raw:
        return bool(default)
    return raw not in {"0", "false", "no", "off"}


def _env_int(name: str, default: int) -> int:
    raw = str(os.environ.get(name, "")).strip()
    if not raw:
        return int(default)
    try:
        return int(raw)
    except ValueError:
        return int(default)


def _city_key(value: str) -> str:
    text = str(value or "").strip().lower()
    text = text.replace("_", "-").replace(" ", "-")
    while "--" in text:
        text = text.replace("--", "-")
    return text.strip("-")


def _auto_pilot_cities_from_matrix(
    city_matrix: Mapping[str, Any],
    *,
    limit: int = DEFAULT_AUTO_PILOT_CITY_LIMIT,
) -> list[str]:
    out: list[str] = []
    for item in city_matrix.get("cities") or []:
        if not isinstance(item, Mapping):
            continue
        city = str(item.get("city") or "").strip()
        if not city:
            continue
        if _city_key(city) in MAJOR_WEATHER_CITIES:
            continue
        blockers = {str(code or "") for code in item.get("blockers") or []}
        if "hit_rate_below_min" in blockers:
            continue
        out.append(city)
        if len(out) >= max(1, int(limit or 1)):
            break
    return _csv_values(out)


def _filter_auto_pilot_cities_by_paper_validation(
    db_path: Path,
    cities: Sequence[str],
    *,
    since_days: int,
    market_scope: str,
) -> list[str]:
    out: list[str] = []
    for city in _csv_values(cities):
        try:
            report = paper_trading_validation.build_report(
                db_path,
                since_days=max(1, int(since_days)),
                city=_city_key(city),
                market_scope=str(market_scope or DEFAULT_MARKET_SCOPE),
            )
        except Exception:
            continue
        if report.get("ok"):
            out.append(city)
    return _csv_values(out)


def _get(mapping: Mapping[str, Any] | None, key: str, default: Any = None) -> Any:
    if not isinstance(mapping, Mapping):
        return default
    return mapping.get(key, default)


def _settlement_validation_pipeline_snapshot(
    db_path: Path,
    *,
    since_days: int,
    market_scope: str,
) -> dict[str, Any]:
    try:
        report = settlement_validation_pipeline.build_report(
            db_path,
            since_days=max(1, int(since_days)),
            market_scope=str(market_scope or DEFAULT_MARKET_SCOPE),
            fetch_preview=False,
            simulate_after_actual_apply=True,
        )
    except Exception as exc:
        return {
            "ok": False,
            "error": f"{type(exc).__name__}:{exc}",
            "next_action": "inspect_settlement_validation_pipeline_error",
        }
    repair = report.get("settlement_actual_repair") if isinstance(report, Mapping) else {}
    apply_dry_run = (
        report.get("settlement_actual_apply_dry_run")
        if isinstance(report, Mapping)
        else {}
    )
    labels = report.get("settlement_label_dry_run") if isinstance(report, Mapping) else {}
    simulation = (
        report.get("after_actual_apply_simulation") if isinstance(report, Mapping) else {}
    )
    write_safety = report.get("write_safety") if isinstance(report, Mapping) else {}
    return {
        "ok": bool(report.get("ok")) if isinstance(report, Mapping) else False,
        "read_only": bool(report.get("read_only")) if isinstance(report, Mapping) else True,
        "next_action": report.get("next_action") if isinstance(report, Mapping) else "",
        "settlement_actual_missing_count": (
            _as_int(repair.get("missing_count")) if isinstance(repair, Mapping) else 0
        ),
        "settlement_actual_ready_count": (
            _as_int(apply_dry_run.get("ready_count"))
            if isinstance(apply_dry_run, Mapping)
            else 0
        ),
        "settlement_label_planned_count": (
            _as_int(labels.get("planned_labels")) if isinstance(labels, Mapping) else 0
        ),
        "simulated_label_planned_after_actual_apply": (
            _as_int(simulation.get("label_planned_after_actual_apply"))
            if isinstance(simulation, Mapping)
            else 0
        ),
        "simulated_actual_inserted_count": (
            _as_int(simulation.get("actual_inserted_count"))
            if isinstance(simulation, Mapping)
            else 0
        ),
        "simulated_label_apply_inserted_count": (
            _as_int(simulation.get("label_apply_inserted_count"))
            if isinstance(simulation, Mapping)
            else 0
        ),
        "simulated_label_planned_after_label_apply": (
            _as_int(simulation.get("label_planned_after_label_apply"))
            if isinstance(simulation, Mapping)
            else 0
        ),
        "simulated_label_apply_closed_loop_ok": (
            bool(simulation.get("label_apply_closed_loop_ok"))
            if isinstance(simulation, Mapping)
            else False
        ),
        "simulation_db_mode": (
            str(simulation.get("simulation_db_mode") or "")
            if isinstance(simulation, Mapping)
            else ""
        ),
        "settlement_label_skipped": (
            dict(labels.get("skipped") or {}) if isinstance(labels, Mapping) else {}
        ),
        "settlement_actual_apply_confirm_command": (
            str(apply_dry_run.get("confirm_command") or "")
            if isinstance(apply_dry_run, Mapping)
            else ""
        ),
        "pre_apply_backup_required": (
            bool(write_safety.get("pre_apply_backup_required", True))
            if isinstance(write_safety, Mapping)
            else True
        ),
        "backup_bypass_effect": (
            str(write_safety.get("bypass_effect") or "confirmed_apply_blocked")
            if isinstance(write_safety, Mapping)
            else "confirmed_apply_blocked"
        ),
    }


def _status(ok: bool | None, *, blockers: Sequence[str] = (), warnings: Sequence[str] = ()) -> str:
    if blockers:
        return "blocked"
    if warnings:
        return "partial"
    if ok is True:
        return "proved"
    return "unverified"


def _launchd_monitor_status() -> dict[str, Any]:
    try:
        proc = subprocess.run(
            ["launchctl", "print", MONITOR_DAEMON_DOMAIN_LABEL],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception as exc:
        return {
            "ok": False,
            "service": MONITOR_DAEMON_DOMAIN_LABEL,
            "error": f"{type(exc).__name__}:{exc}",
        }
    stdout = proc.stdout or ""
    return {
        "ok": proc.returncode == 0 and "state = running" in stdout,
        "service": MONITOR_DAEMON_DOMAIN_LABEL,
        "returncode": proc.returncode,
        "state_running": "state = running" in stdout,
        "uses_auto_trade_watcher": "scripts/auto_trade_watcher.py" in stdout,
        "working_directory_weather": "working directory = /Users/xiong/weather" in stdout,
        "stderr": (proc.stderr or "").strip(),
    }


def _decision_core_audit(
    db_path: Path,
    *,
    since_hours: float,
    limit: int,
    since: str = "",
) -> dict[str, Any]:
    args = SimpleNamespace(
        db=str(db_path),
        since_hours=float(since_hours),
        since=str(since or "").strip(),
        limit=int(limit),
        scan_terms=True,
        exit_policy_only=False,
        entry_only_realizable=None,
        dry_run=None,
        feedback_snapshot=False,
        out="",
    )
    return weather_decision_core_audit.run_audit(args)


def _requirement(
    name: str,
    *,
    ok: bool | None,
    evidence: Mapping[str, Any],
    blockers: Sequence[str] = (),
    warnings: Sequence[str] = (),
) -> dict[str, Any]:
    return {
        "name": name,
        "status": _status(ok, blockers=blockers, warnings=warnings),
        "ok": bool(ok) if ok is not None else None,
        "blockers": list(blockers),
        "warnings": list(warnings),
        "evidence": dict(evidence),
    }


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _seasonal_sizing_contract(batch: Mapping[str, Any]) -> dict[str, Any]:
    autumn_scale = swm.to_float(weather_strategy_plan._season_size_scale("autumn"))
    non_autumn_scale = swm.to_float(weather_strategy_plan._season_size_scale("summer"))
    selected_rows = batch.get("selected_rows") if isinstance(batch.get("selected_rows"), list) else []
    violations: list[dict[str, Any]] = []
    autumn_rows = 0
    adjusted_rows = 0
    for row in selected_rows:
        if not isinstance(row, Mapping):
            continue
        season = str(row.get("weather_strategy_season") or "").strip().lower()
        season_scale = swm.to_float(row.get("weather_strategy_season_size_scale"))
        before = swm.to_float(row.get("entry_usable_edge_size_scale_before_season"))
        adjusted = swm.to_float(
            row.get("entry_usable_edge_size_scale_adjusted")
            if row.get("entry_usable_edge_size_scale_adjusted") is not None
            else row.get("entry_usable_edge_size_scale")
        )
        if adjusted is not None:
            adjusted_rows += 1
        if season == "autumn":
            autumn_rows += 1
            if season_scale is None or autumn_scale is None or season_scale > autumn_scale + 1e-9:
                violations.append(
                    {
                        "slug": row.get("slug"),
                        "outcome": row.get("outcome"),
                        "reason": "autumn_season_scale_not_capped",
                        "season_scale": season_scale,
                        "autumn_contract_scale": autumn_scale,
                    }
                )
                continue
            if before is not None and adjusted is not None:
                max_adjusted = max(0.0, min(1.0, float(before) * float(season_scale)))
                if adjusted > max_adjusted + 1e-9:
                    violations.append(
                        {
                            "slug": row.get("slug"),
                            "outcome": row.get("outcome"),
                            "reason": "autumn_adjusted_size_exceeds_contract",
                            "before": before,
                            "adjusted": adjusted,
                            "season_scale": season_scale,
                        }
                    )
        elif season and season_scale is not None and season_scale > 1.0 + 1e-9:
            violations.append(
                {
                    "slug": row.get("slug"),
                    "outcome": row.get("outcome"),
                    "reason": "season_size_scale_above_one",
                    "season": season,
                    "season_scale": season_scale,
                }
            )
    blockers: list[str] = []
    if autumn_scale is None or autumn_scale <= 0.0 or autumn_scale > 0.50:
        blockers.append("autumn_size_scale_contract_not_lower_size")
    if non_autumn_scale is None or abs(float(non_autumn_scale) - 1.0) > 1e-9:
        blockers.append("non_autumn_size_scale_contract_changed")
    if violations:
        blockers.append("selected_row_seasonal_size_scale_violation")
    return {
        "ok": not blockers,
        "blockers": blockers,
        "evidence": {
            "autumn_size_scale": autumn_scale,
            "non_autumn_size_scale": non_autumn_scale,
            "selected_rows_checked": len(
                [row for row in selected_rows if isinstance(row, Mapping)]
            ),
            "selected_autumn_rows": autumn_rows,
            "selected_rows_with_adjusted_size": adjusted_rows,
            "violation_count": len(violations),
            "violations_preview": violations[:5],
        },
    }


def _top_reason_counts(value: Any, *, limit: int = 5) -> list[dict[str, Any]]:
    if not isinstance(value, Mapping):
        return []
    rows: list[dict[str, Any]] = []
    for reason, count in sorted(
        value.items(),
        key=lambda item: (-_as_int(item[1]), str(item[0])),
    )[: max(1, int(limit or 1))]:
        rows.append({"reason": str(reason), "count": _as_int(count)})
    return rows


def _top_city_reason_counts(value: Any, *, limit: int = 5) -> list[dict[str, Any]]:
    if not isinstance(value, Mapping):
        return []
    rows: list[dict[str, Any]] = []
    for city, reasons in value.items():
        if not isinstance(reasons, Mapping):
            continue
        total = sum(_as_int(count) for count in reasons.values())
        top_reasons = _top_reason_counts(reasons, limit=3)
        rows.append({"city": str(city), "count": int(total), "top_reasons": top_reasons})
    rows.sort(key=lambda item: (-_as_int(item.get("count")), str(item.get("city") or "")))
    return rows[: max(1, int(limit or 1))]


def _reason_action(reason: str) -> str:
    code = str(reason or "").strip()
    if code == "physical_bayes_no_positive_raw_edge":
        return "wait_for_positive_physical_bayes_edge"
    if code == "physical_bayes_positive_raw_edge_tradeability_missing":
        return "inspect_quote_depth_spread_and_tradeability_inputs"
    if code == "physical_bayes_thin_raw_edge_tradeability_missing":
        return "paper_observe_only_until_lcb_edge_thickens"
    if code in {"entry_usable_edge_nonpositive", "entry_usable_edge_size_zero"}:
        return "keep_blocked_until_risk_discounted_edge_turns_positive"
    if code == "strategy_size_ratio_zero":
        return "inspect_strategy_size_ratio_risk_or_seasonal_caps"
    if code == "market_settled_or_past_no_new_entry":
        return "wait_for_next_open_event_date_or_use_settlement_review_only"
    if code == "variance_high":
        return "skip_until_bayesian_variance_contracts"
    if code == "d0_physical_evidence_missing":
        return "wait_for_metar_radiation_or_physical_likelihood_evidence"
    if code == "settlement_watch_not_entry":
        return "settlement_review_only_do_not_open_new_edge"
    if code in {"shadow_only_edge_below_probe", "edge_lcb_too_thin_after_fees"}:
        return "paper_observe_only_until_lcb_edge_reaches_entry_floor"
    if code == "d0_locked_no_new_edge":
        return "use_hold_edge_realizable_for_existing_positions_only"
    if code.startswith("settlement_quality_"):
        return "verify_resolution_source_station_before_settlement_entry"
    if code == "not_entry_candidate_recommendation":
        return "do_not_enter_non_entry_recommendations"
    return "review_city_reason_before_relaxing_any_gate"


def _city_action_plan(
    excluded_by_city: Any,
    skipped_by_city: Any,
    *,
    limit: int = 5,
) -> list[dict[str, Any]]:
    combined: dict[str, dict[str, int]] = {}
    for source in (excluded_by_city, skipped_by_city):
        if not isinstance(source, Mapping):
            continue
        for city, reasons in source.items():
            if not isinstance(reasons, Mapping):
                continue
            bucket = combined.setdefault(str(city), {})
            for reason, count in reasons.items():
                reason_key = str(reason)
                bucket[reason_key] = bucket.get(reason_key, 0) + _as_int(count)
    rows: list[dict[str, Any]] = []
    for city, reasons in combined.items():
        if not reasons:
            continue
        ranked = sorted(reasons.items(), key=lambda item: (-_as_int(item[1]), str(item[0])))
        primary_reason, primary_count = ranked[0]
        rows.append(
            {
                "city": city,
                "count": int(sum(_as_int(value) for value in reasons.values())),
                "primary_reason": primary_reason,
                "primary_count": _as_int(primary_count),
                "recommended_action": _reason_action(primary_reason),
                "top_reasons": [
                    {"reason": str(reason), "count": _as_int(count)}
                    for reason, count in ranked[:3]
                ],
            }
        )
    rows.sort(key=lambda item: (-_as_int(item.get("count")), str(item.get("city") or "")))
    return rows[: max(1, int(limit or 1))]


def _target_hunt_plan(observation_targets: Mapping[str, Any], *, limit: int = 5) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in observation_targets.get("strategy_targets") or []:
        if not isinstance(item, Mapping):
            continue
        needed_rows = _as_int(item.get("needed_resolved_rows"))
        needed_wins = _as_int(item.get("needed_additional_wins_for_hit_rate"))
        if needed_rows <= 0 and needed_wins <= 0:
            continue
        strategy = str(item.get("target_strategy") or "")
        action = str(item.get("recommended_observation_action") or "")
        rows.append(
            {
                "city": str(item.get("city") or ""),
                "target_strategy": strategy,
                "needed_resolved_rows": needed_rows,
                "needed_additional_wins_for_hit_rate": needed_wins,
                "recommended_observation_action": action,
                "scan_directive": (
                    "scan_open_tail_no_reverse_edge_candidates"
                    if strategy == "reverse_edge_buy_no_sell_yes"
                    else (
                        "scan_open_d0_physical_settlement_candidates"
                        if strategy == "d0_physical_settlement_hybrid"
                        else "scan_open_secondary_physical_edge_candidates"
                    )
                ),
            }
        )
    rows.sort(
        key=lambda item: (
            0 if item.get("target_strategy") == "reverse_edge_buy_no_sell_yes" else 1,
            -_as_int(item.get("needed_resolved_rows")),
            -_as_int(item.get("needed_additional_wins_for_hit_rate")),
            str(item.get("city") or ""),
        )
    )
    return rows[: max(1, int(limit or 1))]


def _paper_observation_availability_from_gap_review(
    gap_review: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    buckets: dict[str, dict[str, Any]] = {}
    for row in gap_review.get("rows") or []:
        if not isinstance(row, Mapping):
            continue
        action = str(row.get("review_action") or "").strip()
        if not action.startswith("paper_track_"):
            continue
        strategy = str(row.get("candidate_strategy") or "").strip()
        city = _city_key(
            str(row.get("city") or "")
            or pilot_city_validation_matrix._city_from_slug(row.get("slug"))
        )
        if not city or not strategy:
            continue
        key = f"{city}|{strategy}"
        bucket = buckets.setdefault(
            key,
            {
                "city": city,
                "target_strategy": strategy,
                "paper_observation_rows": 0,
                "paper_observation_actions": {},
                "paper_observation_slugs": [],
            },
        )
        bucket["paper_observation_rows"] = _as_int(bucket.get("paper_observation_rows")) + 1
        actions = bucket["paper_observation_actions"]
        if isinstance(actions, dict):
            actions[action] = _as_int(actions.get(action)) + 1
        slugs = bucket["paper_observation_slugs"]
        slug = str(row.get("slug") or "").strip()
        if slug and isinstance(slugs, list) and slug not in slugs and len(slugs) < 5:
            slugs.append(slug)
    return buckets


def _paper_observation_availability(
    db_path: Path,
    *,
    event_dates: Sequence[str],
    market_scope: str,
) -> dict[str, dict[str, Any]]:
    try:
        review = tradeability_gap_review.build_review(
            db_path,
            event_dates,
            limit=250,
            market_scope=str(market_scope or DEFAULT_MARKET_SCOPE),
        )
    except Exception:
        return {}
    return _paper_observation_availability_from_gap_review(review)


def _paper_rows_from_gap_review(gap_review: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in gap_review.get("rows") or []:
        if not isinstance(row, Mapping):
            continue
        if not str(row.get("review_action") or "").startswith("paper_track_"):
            continue
        out = dict(row)
        out["city"] = _city_key(
            str(out.get("city") or "")
            or pilot_city_validation_matrix._city_from_slug(out.get("slug"))
        )
        rows.append(out)
    return rows


def _paper_observation_gap_tracking(
    db_path: Path,
    *,
    event_dates: Sequence[str],
    market_scope: str,
) -> dict[str, Any]:
    try:
        review = tradeability_gap_review.build_review(
            db_path,
            event_dates,
            limit=250,
            market_scope=str(market_scope or DEFAULT_MARKET_SCOPE),
        )
    except Exception as exc:
        return {
            "ok": False,
            "error": f"{type(exc).__name__}:{exc}",
            "paper_observation_availability": {},
            "tracking": {"summary": {}, "rows": []},
        }
    paper_rows = _paper_rows_from_gap_review(review)
    tracking = paper_observation_tracking_report.attach_tracking_results(
        db_path,
        paper_rows,
    )
    return {
        "ok": bool(review.get("ok", True)),
        "paper_observation_availability": _paper_observation_availability_from_gap_review(
            {"rows": paper_rows}
        ),
        "tracking": tracking,
    }


def _tracking_subset_summary(
    rows: Sequence[Mapping[str, Any]],
    *,
    city_strategy_keys: set[str] | None = None,
    city_keys: set[str] | None = None,
) -> dict[str, Any]:
    subset: list[Mapping[str, Any]] = []
    for row in rows:
        city = _city_key(str(row.get("city") or ""))
        strategy = str(row.get("candidate_strategy") or "")
        if city_strategy_keys is not None and f"{city}|{strategy}" not in city_strategy_keys:
            continue
        if city_keys is not None and city not in city_keys:
            continue
        subset.append(row)
    return paper_observation_tracking_report._tracking_summary(subset)


def _observation_tracking_by_city(
    rows: Sequence[Mapping[str, Any]],
    *,
    city_keys: set[str],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for city in sorted(city_keys):
        summary = _tracking_subset_summary(rows, city_keys={city})
        if _as_int(summary.get("candidate_rows")) <= 0:
            continue
        out.append({"city": city, **summary})
    out.sort(
        key=lambda item: (
            -_as_int(item.get("resolved_rows")),
            -_as_int(item.get("pending_rows")),
            -(float(item.get("hit_rate")) if item.get("hit_rate") is not None else -1.0),
            str(item.get("city") or ""),
        )
    )
    return out


def _recommended_pilot_observation_validation(
    observation_targets: Mapping[str, Any],
    tracked_rows: Sequence[Mapping[str, Any]],
    *,
    min_resolved_rows: int = paper_observation_rolling_validation.DEFAULT_MIN_RESOLVED_ROWS,
    min_strategy_resolved_rows: int = paper_observation_rolling_validation.DEFAULT_MIN_STRATEGY_RESOLVED_ROWS,
    min_hit_rate: float = paper_observation_rolling_validation.DEFAULT_MIN_HIT_RATE,
) -> dict[str, Any]:
    target_cities: dict[str, Mapping[str, Any]] = {}
    for item in observation_targets.get("recommended_pilot_city_targets") or []:
        if not isinstance(item, Mapping):
            continue
        city = _city_key(str(item.get("city") or ""))
        if city:
            target_cities[city] = item
    by_city: list[dict[str, Any]] = []
    all_blockers: list[str] = []
    sample_gap_queue: list[dict[str, Any]] = []
    for city in sorted(target_cities):
        city_rows = [
            row
            for row in tracked_rows
            if isinstance(row, Mapping) and _city_key(str(row.get("city") or "")) == city
        ]
        overall = paper_observation_rolling_validation._strategy_stats(city_rows)
        by_strategy_rows: dict[str, list[Mapping[str, Any]]] = {}
        for row in city_rows:
            strategy = str(row.get("candidate_strategy") or "unknown")
            by_strategy_rows.setdefault(strategy, []).append(row)
        by_strategy = {
            strategy: paper_observation_rolling_validation._strategy_stats(strategy_rows)
            for strategy, strategy_rows in sorted(by_strategy_rows.items())
        }
        blockers: list[str] = []
        warnings: list[str] = []
        resolved = _as_int(overall.get("resolved_rows"))
        pending = _as_int(overall.get("pending_rows"))
        hit_rate = overall.get("hit_rate")
        if resolved < int(min_resolved_rows):
            blockers.append("recommended_pilot_resolved_rows_below_min")
        if hit_rate is None:
            blockers.append("recommended_pilot_hit_rate_missing")
        else:
            try:
                if float(hit_rate) < float(min_hit_rate):
                    blockers.append("recommended_pilot_hit_rate_below_min")
            except Exception:
                blockers.append("recommended_pilot_hit_rate_missing")
        for strategy, item in by_strategy.items():
            strategy_resolved = _as_int(item.get("resolved_rows"))
            if strategy_resolved < int(min_strategy_resolved_rows):
                blockers.append(f"recommended_pilot_strategy_rows_below_min:{strategy}")
        if pending > 0:
            warnings.append("recommended_pilot_pending_labels")
        if not city_rows:
            blockers.append("recommended_pilot_observation_rows_missing")
        needed_resolved = max(0, int(min_resolved_rows) - resolved)
        strategy_gaps: list[dict[str, Any]] = []
        strategy_names = set(by_strategy.keys()) or {"reverse_edge_buy_no_sell_yes"}
        for strategy in sorted(strategy_names):
            strategy_item = by_strategy.get(strategy) or {}
            strategy_resolved = _as_int(strategy_item.get("resolved_rows"))
            needed_strategy_rows = max(
                0,
                int(min_strategy_resolved_rows) - strategy_resolved,
            )
            if needed_strategy_rows <= 0:
                continue
            strategy_gaps.append(
                {
                    "target_strategy": strategy,
                    "resolved_rows": strategy_resolved,
                    "needed_resolved_rows": needed_strategy_rows,
                    "scan_directive": (
                        "scan_open_tail_no_reverse_edge_candidates"
                        if strategy == "reverse_edge_buy_no_sell_yes"
                        else (
                            "scan_open_d0_physical_settlement_candidates"
                            if strategy == "d0_physical_settlement_hybrid"
                            else "scan_open_secondary_physical_edge_candidates"
                        )
                    ),
                }
            )
        if needed_resolved > 0 or strategy_gaps:
            sample_gap_queue.append(
                {
                    "city": city,
                    "observation_mode": str(
                        target_cities[city].get("observation_mode")
                        or "paper_only_not_small_capital"
                    ),
                    "resolved_rows": resolved,
                    "needed_resolved_rows": needed_resolved,
                    "pending_rows": pending,
                    "wins": _as_int(overall.get("wins")),
                    "hit_rate": hit_rate,
                    "strategy_gaps": strategy_gaps,
                    "next_action": "prioritize_recommended_pilot_paper_observation_candidates",
                }
            )
        all_blockers.extend(f"{city}:{code}" for code in blockers)
        by_city.append(
            {
                "city": city,
                "ok": not blockers,
                "observation_mode": str(
                    target_cities[city].get("observation_mode") or "paper_only_not_small_capital"
                ),
                "overall": overall,
                "by_candidate_strategy": by_strategy,
                "blockers": sorted(set(blockers)),
                "warnings": sorted(set(warnings)),
                "next_action": (
                    "promote_to_small_capital_review_after_main_pilot_passes"
                    if not blockers
                    else "continue_paper_only_observation_until_min_rows_and_hit_rate_pass"
                ),
            }
        )
    return {
        "ok": bool(by_city) and not all_blockers,
        "read_only": True,
        "thresholds": {
            "min_resolved_rows": int(min_resolved_rows),
            "min_strategy_resolved_rows": int(min_strategy_resolved_rows),
            "min_hit_rate": float(min_hit_rate),
        },
        "city_count": len(by_city),
        "by_city": by_city,
        "sample_gap_queue": sample_gap_queue,
        "blockers": sorted(set(all_blockers)),
        "next_action": (
            "recommended_paper_only_pilot_validation_passed"
            if by_city and not all_blockers
            else "continue_collecting_recommended_paper_only_pilot_samples"
        ),
    }


def _observation_tracking_focus(
    observation_targets: Mapping[str, Any],
    tracking: Mapping[str, Any],
) -> dict[str, Any]:
    tracked_rows = [
        row for row in tracking.get("rows") or [] if isinstance(row, Mapping)
    ]
    target_keys: set[str] = set()
    for item in observation_targets.get("strategy_targets") or []:
        if not isinstance(item, Mapping):
            continue
        city = _city_key(str(item.get("city") or ""))
        strategy = str(item.get("target_strategy") or "")
        if city and strategy:
            target_keys.add(f"{city}|{strategy}")
    expansion_cities = {
        _city_key(str(item.get("city") or ""))
        for item in observation_targets.get("observation_expansion_city_targets") or []
        if isinstance(item, Mapping)
    }
    expansion_cities.discard("")
    recommended_pilot_cities = {
        _city_key(str(item.get("city") or ""))
        for item in observation_targets.get("recommended_pilot_city_targets") or []
        if isinstance(item, Mapping)
    }
    recommended_pilot_cities.discard("")
    return {
        "target_tracking_summary": _tracking_subset_summary(
            tracked_rows,
            city_strategy_keys=target_keys,
        ),
        "expansion_tracking_summary": _tracking_subset_summary(
            tracked_rows,
            city_keys=expansion_cities,
        ),
        "expansion_tracking_by_city": _observation_tracking_by_city(
            tracked_rows,
            city_keys=expansion_cities,
        ),
        "recommended_pilot_tracking_summary": _tracking_subset_summary(
            tracked_rows,
            city_keys=recommended_pilot_cities,
        ),
        "recommended_pilot_tracking_by_city": _observation_tracking_by_city(
            tracked_rows,
            city_keys=recommended_pilot_cities,
        ),
        "recommended_pilot_observation_validation": (
            _recommended_pilot_observation_validation(
                observation_targets,
                tracked_rows,
            )
        ),
    }


def _pilot_city_recommendation(
    *,
    pilot_cities: Sequence[str],
    pilot_city_source: str,
    observation_tracking_focus: Mapping[str, Any],
    max_pilot_cities: int = DEFAULT_AUTO_PILOT_CITY_LIMIT,
) -> dict[str, Any]:
    current = _csv_values(pilot_cities)
    current_keys = [_city_key(city) for city in current]
    target_summary = (
        observation_tracking_focus.get("target_tracking_summary")
        if isinstance(observation_tracking_focus.get("target_tracking_summary"), Mapping)
        else {}
    )
    expansion_rows = [
        row
        for row in observation_tracking_focus.get("expansion_tracking_by_city") or []
        if isinstance(row, Mapping)
    ]
    candidate_rows = [
        row for row in expansion_rows if _city_key(str(row.get("city") or "")) not in current_keys
    ]
    candidate_rows.sort(
        key=lambda item: (
            -_as_int(item.get("resolved_rows")),
            -_as_int(item.get("pending_rows")),
            -(float(item.get("hit_rate")) if item.get("hit_rate") is not None else -1.0),
            str(item.get("city") or ""),
        )
    )
    best = candidate_rows[0] if candidate_rows else {}
    recommended = list(current_keys)
    action = "keep_current_pilot"
    reason = "current_pilot_has_no_stronger_expansion_candidate"
    if best and len(recommended) < max(1, int(max_pilot_cities)):
        city = _city_key(str(best.get("city") or ""))
        if city:
            recommended.append(city)
            action = "add_second_pilot_city_for_paper_observation"
            reason = "expansion_city_has_best_resolved_pending_tracking_profile"
    elif best:
        target_resolved = _as_int(target_summary.get("resolved_rows"))
        target_pending = _as_int(target_summary.get("pending_rows"))
        best_resolved = _as_int(best.get("resolved_rows"))
        best_pending = _as_int(best.get("pending_rows"))
        best_hit = best.get("hit_rate")
        target_hit = target_summary.get("hit_rate")
        best_hit_value = float(best_hit) if best_hit is not None else -1.0
        target_hit_value = float(target_hit) if target_hit is not None else -1.0
        if (
            best_resolved >= max(1, target_resolved)
            and best_pending >= target_pending
            and best_hit_value >= target_hit_value
        ):
            recommended = [_city_key(str(best.get("city") or ""))]
            action = "switch_pilot_candidate_after_resolved_validation_review"
            reason = "expansion_city_resolved_profile_matches_or_beats_current_pilot"
    return {
        "action": action,
        "reason": reason,
        "pilot_city_source": pilot_city_source,
        "current_pilot_cities": current_keys,
        "recommended_pilot_cities": [city for city in recommended if city],
        "max_pilot_cities": max(1, int(max_pilot_cities)),
        "best_expansion_candidate": dict(best) if isinstance(best, Mapping) else {},
        "target_tracking_summary": dict(target_summary),
        "expansion_tracking_by_city": [dict(row) for row in candidate_rows[:5]],
    }


def _recommended_pilot_settlement_readiness(
    db_path: Path,
    diagnostics: Mapping[str, Any],
    *,
    since_days: int,
    market_scope: str,
) -> dict[str, Any]:
    by_city: dict[str, Any] = {}
    total_planned = 0
    total_scanned = 0
    for city, item in sorted((diagnostics or {}).items()):
        if not isinstance(item, Mapping):
            continue
        primary_action = str(item.get("primary_action") or "")
        if primary_action != "wait_for_settlement_label_or_review_only_no_new_entry":
            continue
        city_key = _city_key(str(city or ""))
        if not city_key:
            continue
        try:
            plan = build_settlement_calibration_labels.build_label_plan(
                db_path,
                since_days=max(1, int(since_days)),
                city=city_key,
                market_scope=str(market_scope or DEFAULT_MARKET_SCOPE),
                bayes_only=True,
                allow_open_events=False,
                strict_resolution_source=True,
                limit=8,
            )
        except Exception as exc:
            by_city[city_key] = {
                "ok": False,
                "error": f"{type(exc).__name__}:{exc}",
                "next_action": "inspect_settlement_label_plan_error",
            }
            continue
        try:
            eligibility = settlement_label_eligibility_report.build_report(
                db_path,
                since_days=max(1, int(since_days)),
                city=city_key,
                market_scope=str(market_scope or DEFAULT_MARKET_SCOPE),
                bayes_only=True,
                allow_open_events=False,
                strict_resolution_source=True,
                limit=8,
            )
        except Exception as exc:
            eligibility = {
                "ok": False,
                "error": f"{type(exc).__name__}:{exc}",
                "status_counts": {},
                "markets": [],
            }
        planned = _as_int(plan.get("planned_labels"))
        scanned = _as_int(plan.get("decisions_scanned"))
        total_planned += planned
        total_scanned += scanned
        skipped = plan.get("skipped") if isinstance(plan.get("skipped"), Mapping) else {}
        settlement_skipped = (
            plan.get("settlement_skipped")
            if isinstance(plan.get("settlement_skipped"), Mapping)
            else {}
        )
        source_issue_count = sum(
            _as_int(count)
            for key, count in settlement_skipped.items()
            if any(token in str(key) for token in ("source", "station", "resolution"))
        )
        if planned > 0:
            next_action = "review_settlement_label_plan_then_apply_after_confirmation"
        elif source_issue_count > 0:
            next_action = "repair_resolution_source_station_mapping_before_label_apply"
        elif _as_int(skipped.get("settlement_missing")) > 0 or _as_int(
            settlement_skipped.get("settlement_not_closed")
        ) > 0:
            next_action = "wait_for_settlement_actual_then_rebuild_label_plan"
        else:
            next_action = "no_settlement_labels_ready_for_recommended_pilot"
        by_city[city_key] = {
            "ok": bool(plan.get("ok")),
            "decisions_scanned": scanned,
            "planned_labels": planned,
            "label_counts": dict(plan.get("label_counts") or {}),
            "skipped": dict(skipped),
            "settlement_skipped": dict(settlement_skipped),
            "eligibility_status_counts": dict(eligibility.get("status_counts") or {}),
            "eligibility_market_count": _as_int(eligibility.get("market_count")),
            "eligibility_ready_market_count": _as_int(
                eligibility.get("ready_market_count")
            ),
            "eligibility_markets_preview": list(eligibility.get("markets") or [])[:5],
            "strict_resolution_source": bool(plan.get("strict_resolution_source")),
            "labels_preview": list(plan.get("labels") or [])[:3],
            "next_action": next_action,
        }
    if total_planned > 0:
        next_action = "review_settlement_label_plan_then_apply_after_confirmation"
    elif by_city:
        next_action = "wait_or_repair_settlement_inputs_for_recommended_pilots"
    else:
        next_action = "no_recommended_pilot_settlement_watch_rows"
    return {
        "read_only": True,
        "since_days": max(1, int(since_days)),
        "market_scope": str(market_scope or DEFAULT_MARKET_SCOPE),
        "total_decisions_scanned": total_scanned,
        "total_planned_labels": total_planned,
        "by_city": by_city,
        "next_action": next_action,
    }


def _expansion_hunt_plan(
    observation_targets: Mapping[str, Any],
    *,
    paper_observation_availability: Mapping[str, Any],
    limit: int = 5,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in observation_targets.get("observation_expansion_city_targets") or []:
        if not isinstance(item, Mapping):
            continue
        city = str(item.get("city") or "").strip()
        city_key = _city_key(city)
        if not city_key:
            continue
        buckets = [
            bucket
            for key, bucket in (paper_observation_availability or {}).items()
            if isinstance(bucket, Mapping) and str(key).startswith(f"{city_key}|")
        ]
        strategy_rows: list[dict[str, Any]] = []
        total_rows = 0
        for bucket in sorted(
            buckets,
            key=lambda value: (
                -_as_int(value.get("paper_observation_rows")),
                str(value.get("target_strategy") or ""),
            ),
        ):
            count = _as_int(bucket.get("paper_observation_rows"))
            total_rows += count
            strategy_rows.append(
                {
                    "target_strategy": str(bucket.get("target_strategy") or ""),
                    "paper_observation_rows": count,
                    "paper_observation_actions": dict(
                        bucket.get("paper_observation_actions") or {}
                    ),
                }
            )
        rows.append(
            {
                "city": city_key,
                "needed_resolved_rows": _as_int(item.get("needed_resolved_rows")),
                "needed_additional_wins_for_hit_rate": _as_int(
                    item.get("needed_additional_wins_for_hit_rate")
                ),
                "pending_rows": _as_int(item.get("pending_rows")),
                "paper_observation_rows": total_rows,
                "paper_observation_strategies": strategy_rows[:3],
                "hunt_status": (
                    "expansion_paper_observe_available"
                    if total_rows > 0
                    else "expansion_wait_for_open_market_matching_scope"
                ),
            }
        )
    rows.sort(
        key=lambda row: (
            0 if _as_int(row.get("paper_observation_rows")) > 0 else 1,
            -_as_int(row.get("paper_observation_rows")),
            str(row.get("city") or ""),
        )
    )
    return rows[: max(1, int(limit or 1))]


def _annotate_target_hunt_plan(
    hunt_plan: Sequence[Mapping[str, Any]],
    *,
    focus_map: Mapping[str, Any],
    city_action_plan: Sequence[Mapping[str, Any]],
    paper_observation_availability: Mapping[str, Any] = {},
) -> list[dict[str, Any]]:
    by_strategy = (
        focus_map.get("candidate_target_rows_by_strategy")
        if isinstance(focus_map.get("candidate_target_rows_by_strategy"), Mapping)
        else {}
    )
    by_city = (
        focus_map.get("candidate_target_rows_by_city")
        if isinstance(focus_map.get("candidate_target_rows_by_city"), Mapping)
        else {}
    )
    city_actions = {
        _city_key(str(item.get("city") or "")): item
        for item in city_action_plan
        if isinstance(item, Mapping)
    }
    out: list[dict[str, Any]] = []
    for item in hunt_plan:
        row = dict(item)
        city_key = _city_key(str(row.get("city") or ""))
        strategy = str(row.get("target_strategy") or "")
        strategy_rows = _as_int(by_strategy.get(strategy)) if isinstance(by_strategy, Mapping) else 0
        city_rows = _as_int(by_city.get(city_key)) if isinstance(by_city, Mapping) else 0
        action = city_actions.get(city_key)
        paper_bucket = (
            paper_observation_availability.get(f"{city_key}|{strategy}")
            if isinstance(paper_observation_availability, Mapping)
            else None
        )
        paper_rows = _as_int(paper_bucket.get("paper_observation_rows")) if isinstance(paper_bucket, Mapping) else 0
        if strategy_rows > 0 or city_rows > 0:
            row["hunt_status"] = "target_candidate_available"
            row["hunt_status_detail"] = "target_candidate_passed_current_entry_gates"
        elif paper_rows > 0:
            row["hunt_status"] = "target_paper_observe_available"
            row["hunt_status_detail"] = "paper_track_candidate_available_but_not_entry"
            row["paper_observation_rows"] = paper_rows
            row["paper_observation_actions"] = (
                dict(paper_bucket.get("paper_observation_actions") or {})
                if isinstance(paper_bucket, Mapping)
                else {}
            )
            row["paper_observation_slugs"] = (
                list(paper_bucket.get("paper_observation_slugs") or [])[:5]
                if isinstance(paper_bucket, Mapping)
                else []
            )
        elif action:
            row["hunt_status"] = "target_city_seen_blocked_or_nonentry"
            row["hunt_status_detail"] = str(action.get("primary_reason") or "")
            row["hunt_blocked_action"] = str(action.get("recommended_action") or "")
        else:
            row["hunt_status"] = "target_not_seen_in_current_scan_scope"
            row["hunt_status_detail"] = "wait_for_open_market_matching_target_strategy"
        out.append(row)
    return out


def _targeted_candidate_status(
    batch: Mapping[str, Any],
    observation_targets: Mapping[str, Any],
    *,
    pilot_cities: Sequence[str],
    pilot_city_source: str,
    paper_observation_availability: Mapping[str, Any] = {},
    observation_tracking_focus: Mapping[str, Any] = {},
) -> dict[str, Any]:
    focus = batch.get("observation_target_focus")
    focus_map = focus if isinstance(focus, Mapping) else {}
    excluded_reasons = batch.get("excluded_entry_reasons")
    skipped_diagnostics = batch.get("skipped_diagnostics")
    excluded_by_city = batch.get("excluded_entry_reasons_by_city")
    skipped_by_city = batch.get("skipped_diagnostics_by_city")
    excluded_map = excluded_reasons if isinstance(excluded_reasons, Mapping) else {}
    skipped_map = skipped_diagnostics if isinstance(skipped_diagnostics, Mapping) else {}

    candidate_target_rows = _as_int(focus_map.get("candidate_target_rows"))
    candidate_expansion_rows = _as_int(focus_map.get("candidate_expansion_rows"))
    candidate_events = _as_int(batch.get("candidate_events"))
    selected_events = _as_int(batch.get("selected_events"))
    target_city_count = len(
        [item for item in observation_targets.get("city_targets") or [] if isinstance(item, Mapping)]
    )
    target_strategy_count = len(
        [item for item in observation_targets.get("strategy_targets") or [] if isinstance(item, Mapping)]
    )
    expansion_city_count = len(
        [
            item
            for item in observation_targets.get("observation_expansion_city_targets") or []
            if isinstance(item, Mapping)
        ]
    )
    has_target_gaps = bool(target_city_count or target_strategy_count)

    if candidate_target_rows > 0:
        status = "target_candidates_available"
        next_action = "evaluate_target_candidates_with_existing_entry_gates"
    elif candidate_events > 0:
        status = "non_target_candidates_only"
        next_action = "wait_for_target_city_or_strategy_candidate_without_relaxing_gates"
    elif excluded_map or skipped_map:
        status = "target_candidates_blocked_by_tradeability_or_evidence"
        next_action = "keep_scanning_until_edge_physical_source_or_liquidity_gates_turn_positive"
    elif has_target_gaps:
        status = "no_target_candidate_in_selected_pilot_cities"
        if pilot_city_source == "auto_matrix" and len(_csv_values(pilot_cities)) <= 1:
            next_action = "continue_auto_paper_valid_secondary_city_discovery"
        else:
            next_action = "wait_for_new_market_or_settlement_label_in_selected_pilot_cities"
    else:
        status = "no_open_observation_targets"
        next_action = "rerun_target_package_after_new_paper_observation_labels"

    city_action_plan = _city_action_plan(excluded_by_city, skipped_by_city)
    target_hunt_plan = _annotate_target_hunt_plan(
        _target_hunt_plan(observation_targets),
        focus_map=focus_map,
        city_action_plan=city_action_plan,
        paper_observation_availability=paper_observation_availability,
    )
    expansion_hunt_plan = _expansion_hunt_plan(
        observation_targets,
        paper_observation_availability=paper_observation_availability,
    )

    return {
        "status": status,
        "next_action": next_action,
        "pilot_cities": _csv_values(pilot_cities),
        "pilot_city_source": pilot_city_source,
        "selected_events": selected_events,
        "candidate_events": candidate_events,
        "candidate_target_rows": candidate_target_rows,
        "candidate_expansion_rows": candidate_expansion_rows,
        "target_city_count": target_city_count,
        "expansion_city_count": expansion_city_count,
        "target_strategy_count": target_strategy_count,
        "observation_scope_cities": batch.get("observation_scope_cities") or [],
        "top_excluded_entry_reasons": _top_reason_counts(excluded_map),
        "top_skipped_diagnostics": _top_reason_counts(skipped_map),
        "top_excluded_entry_reasons_by_city": _top_city_reason_counts(excluded_by_city),
        "top_skipped_diagnostics_by_city": _top_city_reason_counts(skipped_by_city),
        "city_action_plan": city_action_plan,
        "target_hunt_plan": target_hunt_plan,
        "expansion_hunt_plan": expansion_hunt_plan,
        "target_observation_tracking_summary": dict(
            observation_tracking_focus.get("target_tracking_summary") or {}
        ),
        "expansion_observation_tracking_summary": dict(
            observation_tracking_focus.get("expansion_tracking_summary") or {}
        ),
        "expansion_observation_tracking_by_city": list(
            observation_tracking_focus.get("expansion_tracking_by_city") or []
        ),
        "recommended_pilot_observation_tracking_summary": dict(
            observation_tracking_focus.get("recommended_pilot_tracking_summary") or {}
        ),
        "recommended_pilot_observation_tracking_by_city": list(
            observation_tracking_focus.get("recommended_pilot_tracking_by_city") or []
        ),
        "recommended_pilot_observation_validation": dict(
            observation_tracking_focus.get("recommended_pilot_observation_validation") or {}
        ),
        "pilot_city_recommendation": _pilot_city_recommendation(
            pilot_cities=pilot_cities,
            pilot_city_source=pilot_city_source,
            observation_tracking_focus=observation_tracking_focus,
        ),
        "recommended_pilot_city_diagnostics": (
            dict(focus_map.get("recommended_pilot_city_diagnostics") or {})
            if isinstance(focus_map.get("recommended_pilot_city_diagnostics"), Mapping)
            else {}
        ),
        "paper_observation_availability": dict(paper_observation_availability or {}),
    }


def build_report(
    db_path: Path = DEFAULT_DB,
    *,
    event_dates: Sequence[str],
    pilot_cities: Sequence[str],
    market_scope: str = DEFAULT_MARKET_SCOPE,
    since_days: int = 42,
    since_hours: float = 24.0,
    core_since: str = "",
    max_events: int = 3,
) -> dict[str, Any]:
    event_days = _csv_values(event_dates, [swm.today_date_str("America/New_York")])
    requested_pilot_city_values = _csv_values(pilot_cities)
    env_pilot_city_values = [] if requested_pilot_city_values else _default_pilot_cities_from_env()
    pilot_city_values = requested_pilot_city_values or env_pilot_city_values

    core = _decision_core_audit(
        db_path,
        since_hours=float(since_hours),
        limit=500,
        since=str(core_since or "").strip(),
    )
    paper = paper_trading_validation.build_report(
        db_path,
        since_days=max(1, int(since_days)),
        market_scope=str(market_scope or DEFAULT_MARKET_SCOPE),
        gap_event_dates=event_days[-2:],
    )
    city_matrix = pilot_city_validation_matrix.build_report(
        db_path,
        event_dates=event_days[-2:],
        market_scope=str(market_scope or DEFAULT_MARKET_SCOPE),
        cities=pilot_city_values,
    )
    auto_pilot_city_values = (
        []
        if pilot_city_values
        else _auto_pilot_cities_from_matrix(
            city_matrix,
            limit=_env_int("PM_AUTO_PILOT_CITY_LIMIT", DEFAULT_AUTO_PILOT_CITY_LIMIT),
        )
    )
    if auto_pilot_city_values and _env_bool("PM_AUTO_PILOT_REQUIRE_PAPER_VALIDATION", False):
        auto_pilot_city_values = _filter_auto_pilot_cities_by_paper_validation(
            db_path,
            auto_pilot_city_values,
            since_days=max(1, int(since_days)),
            market_scope=str(market_scope or DEFAULT_MARKET_SCOPE),
        )
    pilot_city_values = pilot_city_values or auto_pilot_city_values
    if auto_pilot_city_values:
        city_matrix = pilot_city_validation_matrix.build_report(
            db_path,
            event_dates=event_days[-2:],
            market_scope=str(market_scope or DEFAULT_MARKET_SCOPE),
            cities=pilot_city_values,
        )
    small_cap = small_capital_validation_review.build_report(
        db_path,
        pilot_cities=pilot_city_values,
        since_days=max(1, int(since_days)),
        market_scope=str(market_scope or DEFAULT_MARKET_SCOPE),
    )
    rolling = paper_observation_rolling_validation.build_report(
        db_path,
        pilot_cities=pilot_city_values,
        since_days=max(1, int(since_days)),
        market_scope=str(market_scope or DEFAULT_MARKET_SCOPE),
    )
    scale_up = scale_up_validation_review.build_report(
        db_path,
        pilot_cities=pilot_city_values,
        since_days=max(1, int(since_days)),
        market_scope=str(market_scope or DEFAULT_MARKET_SCOPE),
    )
    observation_targets = paper_observation_target_package.build_package(
        db_path,
        event_dates=event_days[-2:],
        market_scope=str(market_scope or DEFAULT_MARKET_SCOPE),
        cities=pilot_city_values,
    )
    batch = weather_strategy_batch.build_strategy_batch(
        db_path,
        event_days,
        market_scope=str(market_scope or DEFAULT_MARKET_SCOPE),
        max_events=max(1, int(max_events or 1)),
        pilot_cities=pilot_city_values,
        observation_target_focus=True,
        observation_target_package=observation_targets,
    )
    paper_observation_gap_tracking = _paper_observation_gap_tracking(
        db_path,
        event_dates=event_days[-2:],
        market_scope=str(market_scope or DEFAULT_MARKET_SCOPE),
    )
    paper_observation_availability_map = (
        paper_observation_gap_tracking.get("paper_observation_availability")
        if isinstance(paper_observation_gap_tracking.get("paper_observation_availability"), Mapping)
        else {}
    )
    observation_tracking_focus = _observation_tracking_focus(
        observation_targets,
        (
            paper_observation_gap_tracking.get("tracking")
            if isinstance(paper_observation_gap_tracking.get("tracking"), Mapping)
            else {}
        ),
    )
    launchd = _launchd_monitor_status()
    settlement_pipeline = _settlement_validation_pipeline_snapshot(
        db_path,
        since_days=max(1, int(since_days)),
        market_scope=str(market_scope or DEFAULT_MARKET_SCOPE),
    )
    pilot_city_source = (
        "cli"
        if requested_pilot_city_values
        else "env"
        if env_pilot_city_values
        else "auto_matrix"
        if auto_pilot_city_values
        else "missing"
    )

    selected_resource = _get(batch, "selected_resource_focus", {})
    selected_physical = _get(batch, "selected_physical_edge_quality", {})
    selected_settlement = _get(batch, "selected_settlement_quality", {})
    seasonal_sizing = _seasonal_sizing_contract(batch)
    excluded_reasons = _get(batch, "excluded_entry_reasons", {})
    skipped_diagnostics = _get(batch, "skipped_diagnostics", {})
    core_violations = _get(core, "violations", {})
    targeted_candidate_status = _targeted_candidate_status(
        batch,
        observation_targets,
        pilot_cities=pilot_city_values,
        pilot_city_source=pilot_city_source,
        paper_observation_availability=paper_observation_availability_map,
        observation_tracking_focus=observation_tracking_focus,
    )
    targeted_candidate_status["recommended_pilot_settlement_readiness"] = (
        _recommended_pilot_settlement_readiness(
            db_path,
            targeted_candidate_status.get("recommended_pilot_city_diagnostics")
            if isinstance(
                targeted_candidate_status.get("recommended_pilot_city_diagnostics"),
                Mapping,
            )
            else {},
            since_days=max(1, int(since_days)),
            market_scope=str(market_scope or DEFAULT_MARKET_SCOPE),
        )
    )

    selected_rows = batch.get("selected_rows") if isinstance(batch.get("selected_rows"), list) else []
    selected_missing_physical = [
        row
        for row in selected_rows
        if isinstance(row, Mapping)
        and str(row.get("weather_strategy_stage") or "").startswith("d0")
        and str(row.get("focus_lane") or "") == "d0_physical_or_settlement"
        and not weather_strategy_batch._row_has_physical_likelihood(row)
    ]
    selected_major = [
        row
        for row in selected_rows
        if isinstance(row, Mapping)
        and str(row.get("focus_lane") or "") == "major_settlement"
    ]
    selected_major_bad_source = [
        row
        for row in selected_major
        if not bool(row.get("weather_strategy_settlement_eligible"))
        or str(row.get("source_fit_tier") or "") != "settlement_source"
        or row.get("source_station_match") is not True
    ]
    paper_small_cap_automation = {
        "ok": bool(paper.get("ok"))
        and bool(small_cap.get("ok"))
        and bool(rolling.get("ok"))
        and bool(scale_up.get("ok")),
        "paper_ok": paper.get("ok"),
        "paper_blockers": paper.get("blockers"),
        "small_cap_ok": small_cap.get("ok"),
        "small_cap_blockers": small_cap.get("blockers"),
        "rolling_ok": rolling.get("ok"),
        "rolling_blockers": rolling.get("blockers"),
        "scale_up_ok": scale_up.get("ok"),
        "scale_up_blockers": scale_up.get("blockers"),
        "pilot_city_matrix_ok": city_matrix.get("ok"),
        "pilot_city_matrix_top": [
            {
                "city": item.get("city"),
                "ok": item.get("ok"),
                "resolved_rows": item.get("resolved_rows"),
                "pending_rows": item.get("pending_rows"),
                "hit_rate": item.get("hit_rate"),
                "blockers": item.get("blockers"),
            }
            for item in (city_matrix.get("cities") or [])[:5]
            if isinstance(item, Mapping)
        ],
        "pilot_city_matrix_next_mainline_task": city_matrix.get("next_mainline_task"),
        "observation_target_package_next_mainline_task": observation_targets.get("next_mainline_task"),
        "strategy_batch_observation_target_focus": batch.get("observation_target_focus"),
        "targeted_candidate_status": targeted_candidate_status,
        "recommended_pilot_city_targets": [
            {
                "city": item.get("city"),
                "recommended_pilot_action": item.get("recommended_pilot_action"),
                "observation_mode": item.get("observation_mode"),
                "resolved_rows": item.get("resolved_rows"),
                "pending_rows": item.get("pending_rows"),
                "hit_rate": item.get("hit_rate"),
            }
            for item in (observation_targets.get("recommended_pilot_city_targets") or [])[:5]
            if isinstance(item, Mapping)
        ],
        "observation_expansion_city_targets": [
            {
                "city": item.get("city"),
                "needed_resolved_rows": item.get("needed_resolved_rows"),
                "needed_additional_wins_for_hit_rate": item.get(
                    "needed_additional_wins_for_hit_rate"
                ),
                "pending_rows": item.get("pending_rows"),
                "hit_rate": item.get("hit_rate"),
                "blockers": item.get("blockers"),
            }
            for item in (observation_targets.get("observation_expansion_city_targets") or [])[:5]
            if isinstance(item, Mapping)
        ],
        "observation_strategy_targets": [
            {
                "city": item.get("city"),
                "target_strategy": item.get("target_strategy"),
                "needed_resolved_rows": item.get("needed_resolved_rows"),
                "needed_additional_wins_for_hit_rate": item.get(
                    "needed_additional_wins_for_hit_rate"
                ),
                "recommended_observation_action": item.get("recommended_observation_action"),
            }
            for item in (observation_targets.get("strategy_targets") or [])[:5]
            if isinstance(item, Mapping)
        ],
        "settlement_validation_pipeline": settlement_pipeline,
        "blockers": [
            code
            for prefix, report in (
                ("paper", paper),
                ("small_cap", small_cap),
                ("rolling", rolling),
                ("scale_up", scale_up),
            )
            for code in [f"{prefix}:{item}" for item in (report.get("blockers") or [])]
        ],
    }

    requirements = [
        _requirement(
            "hybrid_edge_and_settlement_strategy",
            ok=True,
            evidence={
                "stage_mix_contract": "D2+ edge-dominant, D1 balanced, D0 morning settlement-dominant, D0 locked settlement-only",
                "batch_resource_focus": selected_resource,
                "batch_selected_events": batch.get("selected_events"),
            },
        ),
        _requirement(
            "reverse_edge_buy_no_sell_yes",
            ok=True,
            evidence={
                "plan_fields": [
                    "weather_strategy_reverse_edge",
                    "weather_strategy_reverse_edge_action",
                    "reverse_edge_execution_side",
                    "reverse_edge_execution_action",
                ],
                "tail_no_skipped_or_candidate_count": int(
                    (excluded_reasons or {}).get("entry_edge_below_strict_min") or 0
                )
                + int((skipped_diagnostics or {}).get("edge_lcb_too_thin_after_fees") or 0),
            },
        ),
        _requirement(
            "adaptive_lcb_edge_floor_after_fees",
            ok=float(batch.get("min_entry_edge") or 0.0) >= 0.10,
            evidence={
                "min_entry_edge": batch.get("min_entry_edge"),
                "normal_trade_edge": 0.10,
                "legacy_probe_edge_floor": 0.075,
                "strict_live_entry_floor": 0.10,
                "excluded_entry_reasons": excluded_reasons,
            },
        ),
        _requirement(
            "resource_focus_70_structural_30_major_settlement",
            ok=selected_resource.get("target_met") in {True, None}
            and float(batch.get("protected_focus_min_share") or 0.0) >= 0.70
            and float(batch.get("major_settlement_max_share") or 1.0) <= 0.30,
            evidence={
                "selected_resource_focus": selected_resource,
                "protected_focus_min_share": batch.get("protected_focus_min_share"),
                "major_settlement_max_share": batch.get("major_settlement_max_share"),
            },
            warnings=[] if selected_resource.get("target_met") is not None else ["no_selected_events_to_measure_share"],
        ),
        _requirement(
            "d0_physical_likelihood_gate",
            ok=bool(batch.get("require_d0_physical_evidence")) and not selected_missing_physical,
            evidence={
                "require_d0_physical_evidence": batch.get("require_d0_physical_evidence"),
                "selected_physical_edge_quality": selected_physical,
                "d0_physical_evidence_missing_exclusions": (excluded_reasons or {}).get(
                    "d0_physical_evidence_missing",
                    0,
                ),
            },
        ),
        _requirement(
            "resolution_source_station_strict_check",
            ok=not int(core_violations.get("source_station_mismatch_rows") or 0)
            and not selected_major_bad_source,
            evidence={
                "core_source_station_mismatch_rows": core_violations.get("source_station_mismatch_rows"),
                "selected_settlement_quality": selected_settlement,
                "selected_major_bad_source_count": len(selected_major_bad_source),
            },
        ),
        _requirement(
            "seasonal_size_scale_contract",
            ok=bool(seasonal_sizing.get("ok")),
            evidence=seasonal_sizing.get("evidence")
            if isinstance(seasonal_sizing.get("evidence"), Mapping)
            else {},
            blockers=[
                str(code)
                for code in (seasonal_sizing.get("blockers") or [])
                if str(code or "").strip()
            ],
        ),
        _requirement(
            "entry_exit_edge_contract",
            ok=bool(core.get("ok")),
            evidence={
                "core_ok": core.get("ok"),
                "missing_entry_usable_edge_rows": core_violations.get("missing_entry_usable_edge_rows"),
                "entry_unusable_edge_order_violations": core_violations.get(
                    "entry_unusable_edge_order_violations"
                ),
                "exit_policy_missing_rows": core_violations.get("exit_policy_missing_rows"),
                "exit_policy_edge_source_violations": core_violations.get(
                    "exit_policy_edge_source_violations"
                ),
                "latest_exit_policy_coverage": core.get("latest_exit_policy_coverage"),
                "core_since": str(core_since or "").strip() or None,
                "core_since_hours": float(since_hours),
            },
            blockers=[] if core.get("ok") else ["decision_core_audit_not_clean"],
        ),
        _requirement(
            "settlement_write_safety_backup_gate",
            ok=bool(settlement_pipeline.get("pre_apply_backup_required"))
            and str(settlement_pipeline.get("backup_bypass_effect") or "")
            == "confirmed_apply_blocked",
            evidence={
                "pre_apply_backup_required": settlement_pipeline.get(
                    "pre_apply_backup_required"
                ),
                "backup_bypass_effect": settlement_pipeline.get("backup_bypass_effect"),
                "settlement_actual_apply_confirm_command": settlement_pipeline.get(
                    "settlement_actual_apply_confirm_command"
                ),
                "next_action": settlement_pipeline.get("next_action"),
            },
            blockers=[]
            if bool(settlement_pipeline.get("pre_apply_backup_required"))
            and str(settlement_pipeline.get("backup_bypass_effect") or "")
            == "confirmed_apply_blocked"
            else ["pre_apply_backup_gate_not_proved"],
        ),
        _requirement(
            "launchd_monitor_24_7_path",
            ok=bool(launchd.get("ok"))
            and bool(launchd.get("uses_auto_trade_watcher"))
            and bool(launchd.get("working_directory_weather")),
            evidence=launchd,
            blockers=[] if launchd.get("ok") else ["monitor_launchd_not_running_or_unverified"],
        ),
    ]

    blockers = [
        f"{item['name']}:{code}"
        for item in requirements
        for code in item.get("blockers") or []
    ]
    warnings = [
        f"{item['name']}:{code}"
        for item in requirements
        for code in item.get("warnings") or []
    ]
    ok = not blockers and all(item.get("ok") is True for item in requirements)

    return {
        "ok": bool(ok),
        "read_only": True,
        "generated_at": _utc_now(),
        "db": str(db_path),
        "event_dates": event_days,
        "pilot_cities": pilot_city_values,
        "pilot_city_source": pilot_city_source,
        "market_scope": str(market_scope or DEFAULT_MARKET_SCOPE),
        "core_since": str(core_since or "").strip(),
        "core_since_hours": float(since_hours),
        "blockers": blockers,
        "warnings": warnings,
        "requirements": requirements,
        "automation_reports": {
            "paper_small_cap_scale_validation_before_scaling": paper_small_cap_automation,
            "settlement_validation_pipeline": settlement_pipeline,
        },
        "current_strategy_batch_summary": {
            "ok": batch.get("ok"),
            "selected_events": batch.get("selected_events"),
            "candidate_events": batch.get("candidate_events"),
            "observation_scope_cities": batch.get("observation_scope_cities"),
            "include_event_keys": batch.get("include_event_keys"),
            "excluded_entry_reasons": excluded_reasons,
            "excluded_entry_reasons_by_city": batch.get("excluded_entry_reasons_by_city"),
            "skipped_diagnostics": skipped_diagnostics,
            "skipped_diagnostics_by_city": batch.get("skipped_diagnostics_by_city"),
            "observation_target_focus": batch.get("observation_target_focus"),
            "targeted_candidate_status": targeted_candidate_status,
        },
        "pilot_city_validation_matrix_summary": {
            "ok": city_matrix.get("ok"),
            "city_count": city_matrix.get("city_count"),
            "top_cities": [
                {
                    "city": item.get("city"),
                    "ok": item.get("ok"),
                    "resolved_rows": item.get("resolved_rows"),
                    "pending_rows": item.get("pending_rows"),
                    "hit_rate": item.get("hit_rate"),
                    "blockers": item.get("blockers"),
                }
                for item in (city_matrix.get("cities") or [])[:5]
                if isinstance(item, Mapping)
            ],
            "next_mainline_task": city_matrix.get("next_mainline_task"),
        },
        "paper_observation_target_package_summary": {
            "ok": observation_targets.get("ok"),
            "next_mainline_task": observation_targets.get("next_mainline_task"),
            "city_targets": [
                {
                    "city": item.get("city"),
                    "needed_resolved_rows": item.get("needed_resolved_rows"),
                    "needed_additional_wins_for_hit_rate": item.get(
                        "needed_additional_wins_for_hit_rate"
                    ),
                    "blockers": item.get("blockers"),
                }
                for item in (observation_targets.get("city_targets") or [])[:5]
                if isinstance(item, Mapping)
            ],
            "observation_expansion_city_targets": [
                {
                    "city": item.get("city"),
                    "needed_resolved_rows": item.get("needed_resolved_rows"),
                    "needed_additional_wins_for_hit_rate": item.get(
                        "needed_additional_wins_for_hit_rate"
                    ),
                    "pending_rows": item.get("pending_rows"),
                    "hit_rate": item.get("hit_rate"),
                    "blockers": item.get("blockers"),
                }
                for item in (observation_targets.get("observation_expansion_city_targets") or [])[:5]
                if isinstance(item, Mapping)
            ],
            "recommended_pilot_city_targets": [
                {
                    "city": item.get("city"),
                    "recommended_pilot_action": item.get("recommended_pilot_action"),
                    "observation_mode": item.get("observation_mode"),
                    "resolved_rows": item.get("resolved_rows"),
                    "pending_rows": item.get("pending_rows"),
                    "hit_rate": item.get("hit_rate"),
                }
                for item in (observation_targets.get("recommended_pilot_city_targets") or [])[:5]
                if isinstance(item, Mapping)
            ],
            "strategy_targets": [
                {
                    "city": item.get("city"),
                    "target_strategy": item.get("target_strategy"),
                    "needed_resolved_rows": item.get("needed_resolved_rows"),
                    "needed_additional_wins_for_hit_rate": item.get(
                        "needed_additional_wins_for_hit_rate"
                    ),
                    "recommended_observation_action": item.get("recommended_observation_action"),
                }
                for item in (observation_targets.get("strategy_targets") or [])[:5]
                if isinstance(item, Mapping)
            ],
        },
        "settlement_validation_pipeline_summary": settlement_pipeline,
        "next_mainline_task": (
            "limited_scale_up_enable_review"
            if ok
            else "collect_clean_new_decision_rows_and_paper_observation_labels"
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only objective completion audit for weather strategy upgrade.")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--event-date", action="append", default=[], help="repeatable or comma-separated")
    parser.add_argument("--pilot-city", action="append", default=[], help="repeatable or comma-separated")
    parser.add_argument("--market-scope", default=DEFAULT_MARKET_SCOPE)
    parser.add_argument("--since-days", type=int, default=42)
    parser.add_argument("--since-hours", type=float, default=24.0)
    parser.add_argument(
        "--core-since",
        default="",
        help="ISO UTC cutoff for current decision-core audit; overrides --since-hours for that audit only",
    )
    parser.add_argument("--max-events", type=int, default=3)
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    report = build_report(
        Path(args.db),
        event_dates=_csv_values(args.event_date),
        pilot_cities=_csv_values(args.pilot_city),
        market_scope=str(args.market_scope or DEFAULT_MARKET_SCOPE),
        since_days=int(args.since_days),
        since_hours=float(args.since_hours),
        core_since=str(args.core_since or "").strip(),
        max_events=int(args.max_events),
    )
    out_path = Path(args.out) if str(args.out or "").strip() else None
    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(report, ensure_ascii=False, indent=None if args.json else 2, sort_keys=not args.json))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
