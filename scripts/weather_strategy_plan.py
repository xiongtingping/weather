#!/usr/bin/env python3
"""Build per-event-date strategy output from archive SSOT.

This tool is read-only and only queries ``data/archive.db``. It is intended as a
bridge artifact between decision run facts and strategy output review. It does not
write back to trading tables.
"""

from __future__ import annotations

import os

import argparse
import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover - Python without zoneinfo fallback
    ZoneInfo = None

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "archive.db"
MAX_SUMMARY_ROWS = 50
MAJOR_WEATHER_CITIES = {
    "new-york",
    "nyc",
    "london",
    "paris",
    "tokyo",
    "seoul",
    "hong-kong",
    "singapore",
    "los-angeles",
    "chicago",
    "miami",
    "dubai",
}
SOUTHERN_HEMISPHERE_WEATHER_CITIES = {
    "buenos-aires",
    "cape-town",
    "sao-paulo",
    "rio-de-janeiro",
    "sydney",
    "melbourne",
    "brisbane",
    "auckland",
    "johannesburg",
}
STRUCTURAL_EDGE_FOCUS_LANES = {
    "tail_no_reverse_edge",
    "secondary_edge",
    "d0_physical_or_settlement",
}
MAJOR_SETTLEMENT_FOCUS_LANE = "major_settlement"
TARGET_STRUCTURAL_EDGE_MIN_SHARE = 0.70
TARGET_MAJOR_SETTLEMENT_MAX_SHARE = 0.30


def _to_float(value: Any) -> Optional[float]:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(num):
        return None
    return num


def _to_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    text = str(value).strip().lower()
    return text in {"1", "true", "yes", "on"}


def _json_list(raw: Any) -> List[str]:
    if not isinstance(raw, str):
        return []
    try:
        parsed = json.loads(raw)
    except Exception:
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item or "").strip() for item in parsed if str(item or "").strip()]


def _safe_json_text(raw: Any) -> Dict[str, Any]:
    if not isinstance(raw, str):
        return {}
    txt = raw.strip()
    if not txt:
        return {}
    try:
        payload = json.loads(txt)
    except Exception:
        return {}
    if isinstance(payload, dict):
        return payload
    return {}


def _parse_json_field_list(payload: Mapping[str, Any], keys: Iterable[str]) -> List[str]:
    for key in keys:
        value = payload.get(key)
        if not value:
            continue
        if isinstance(value, list):
            return [str(item or "").strip() for item in value if str(item or "").strip()]
        if isinstance(value, str):
            parsed = _json_list(value)
            if parsed:
                return parsed
    return []


def _is_highest_temperature_market(slug: str, title: str) -> bool:
    slug_l = str(slug or "").strip().lower()
    title_l = str(title or "").strip().lower()
    if slug_l.startswith("lowest-temperature-in-") or "lowest temperature" in title_l:
        return False
    return (
        slug_l.startswith("highest-temperature-in-")
        or "high-temperature" in slug_l
        or "highest temperature" in title_l
        or "high temperature" in title_l
    )


def _normalize_event_date(raw: str) -> str:
    if not raw:
        return ""
    return str(raw).strip().split("T", 1)[0]


def _normalize_event_dates(raw: Sequence[str]) -> List[str]:
    seen: Set[str] = set()
    out: List[str] = []
    for token in raw:
        if token is None:
            continue
        for part in str(token).split(","):
            day = _normalize_event_date(part)
            if not day or len(day) < 10:
                continue
            if day not in seen:
                seen.add(day)
                out.append(day)
    return out


def _slug_city(slug: str) -> str:
    text = str(slug or "").strip().lower()
    prefix = "highest-temperature-in-"
    if text.startswith(prefix):
        text = text[len(prefix):]
    marker = "-on-"
    if marker in text:
        text = text.split(marker, 1)[0]
    return text.strip("-")


def _season_for_event(event_date: str, *, city_key: str = "") -> str:
    day = _normalize_event_date(event_date)
    if not day:
        return "unknown"
    try:
        month = int(day.split("-", 2)[1])
    except Exception:
        return "unknown"
    southern = str(city_key or "").strip().lower() in SOUTHERN_HEMISPHERE_WEATHER_CITIES
    if southern:
        if month in {3, 4, 5}:
            return "autumn"
        if month in {6, 7, 8}:
            return "winter"
        if month in {9, 10, 11}:
            return "spring"
        return "summer"
    if month in {9, 10, 11}:
        return "autumn"
    if month in {12, 1, 2}:
        return "winter"
    if month in {3, 4, 5}:
        return "spring"
    return "summer"


def _season_size_scale(season: str) -> float:
    if str(season or "").strip().lower() == "autumn":
        return 0.40
    return 1.0


#: The day "today" means when a row carries no stage of its own.
#:
#: Read through a function rather than inline so it can be pinned.  Every
#: fixture in this lane's suite dates its rows to a fixed day, and the
#: stage falls out of comparing that day to the wall clock -- so on
#: 2026-09-07 twenty-two of its tests were red for no reason except that
#: the calendar had walked past 2026-05-31 and every fixture row now read
#: "settled_or_past".  The override exists for that: production reads the
#: clock, the suite reads the day its fixtures were written for.
REFERENCE_TODAY_ENV = "WEATHER_STRATEGY_REFERENCE_TODAY"


def reference_today() -> str:
    """Today in the market's own timezone, or the pinned override."""

    pinned = str(os.environ.get(REFERENCE_TODAY_ENV, "")).strip()
    if pinned:
        return pinned
    zone = ZoneInfo("America/New_York") if ZoneInfo is not None else timezone.utc
    return datetime.now(zone).date().isoformat()


def _stage_from_details(details: Mapping[str, Any], event_date: str) -> str:
    stage = str(
        details.get("weather_strategy_stage")
        or details.get("q_bayes_z_stage")
        or details.get("q_calibration_z_stage")
        or details.get("q_quote_calibration_z_stage")
        or ""
    ).strip().lower()
    if stage in {"d0_afternoon", "d0_locked"}:
        return "d0_afternoon_or_locked"
    if stage in {"d2_plus", "d1", "d0_morning", "d0_afternoon_or_locked", "settled_or_past"}:
        return stage
    event_day = _normalize_event_date(event_date)
    today = reference_today()
    if event_day and event_day < today:
        return "settled_or_past"
    if event_day == today:
        return "d0_morning"
    return "unknown"


def _mode_from_stage(stage: str) -> Tuple[str, float, float]:
    if stage == "d2_plus":
        return "edge_dominant", 0.75, 0.25
    if stage == "d1":
        return "balanced", 0.50, 0.50
    if stage == "d0_morning":
        return "settlement_dominant", 0.30, 0.70
    if stage == "d0_afternoon_or_locked":
        return "settlement_only", 0.0, 1.0
    return "review", 0.0, 0.0


def _edge_tier_from_profit(value: Optional[float]) -> Tuple[str, str]:
    if value is None:
        return "blocked", "edge_lcb_missing"
    if value >= 0.10:
        return "normal_trade", "edge_lcb_meets_strict_min"
    if value >= 0.075:
        return "probe", "edge_lcb_in_probe_band"
    return "shadow_only", "edge_lcb_too_thin_after_fees"


def _is_tail_no(outcome: str, side: str, q_entry: Optional[float]) -> bool:
    if str(side or "").strip().upper() != "NO":
        return False
    text = str(outcome or "").strip().lower()
    if any(token in text for token in ("or higher", "or above", "above", "or below", "or lower", "below")):
        return True
    return bool(q_entry is not None and q_entry >= 0.75)


def _resource_focus_summary(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    actionable = [
        row
        for row in rows
        if str(row.get("weather_strategy_edge_tier") or "") in {"normal_trade", "probe"}
        or str(row.get("strategy_recommendation") or "") in {
            "priority_tail_no_reverse_edge",
            "paper_trade_candidate",
            "strict_edge_candidate_review",
            "probe_candidate",
            "settlement_hold_review",
            "settlement_dominant_watch",
        }
    ]
    total = len(actionable)
    structural = [
        row
        for row in actionable
        if str(row.get("focus_lane") or "") in STRUCTURAL_EDGE_FOCUS_LANES
    ]
    major = [
        row
        for row in actionable
        if str(row.get("focus_lane") or "") == MAJOR_SETTLEMENT_FOCUS_LANE
    ]
    by_lane: Dict[str, int] = {}
    for row in actionable:
        lane = str(row.get("focus_lane") or "unknown")
        by_lane[lane] = by_lane.get(lane, 0) + 1

    def share(n: int) -> Optional[float]:
        if total <= 0:
            return None
        return float(n) / float(total)

    structural_share = share(len(structural))
    major_share = share(len(major))
    return {
        "target_structural_edge_min_share": TARGET_STRUCTURAL_EDGE_MIN_SHARE,
        "target_major_settlement_max_share": TARGET_MAJOR_SETTLEMENT_MAX_SHARE,
        "actionable_count": total,
        "structural_edge_count": len(structural),
        "major_settlement_count": len(major),
        "structural_edge_share": structural_share,
        "major_settlement_share": major_share,
        "by_focus_lane": dict(sorted(by_lane.items())),
        "target_met": (
            None
            if total <= 0
            else bool(
                (structural_share or 0.0) >= TARGET_STRUCTURAL_EDGE_MIN_SHARE
                and (major_share or 0.0) <= TARGET_MAJOR_SETTLEMENT_MAX_SHARE
            )
        ),
    }


def _coalesce_bool(*values: Any) -> Optional[bool]:
    for value in values:
        if value is None:
            continue
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off"}:
            return False
    return None


def _recommendation(
    *,
    trade_status: str,
    stage: str,
    mode: str,
    edge_tier: str,
    reverse_edge: bool,
    tail_no: bool,
    settlement_eligible: bool,
    entry_allowed: Optional[bool],
    entry_usable_edge: Optional[float],
) -> str:
    if trade_status == "order":
        return "dry_run_order_intent"
    if entry_allowed is False:
        return "blocked_by_weather_strategy"
    if mode == "settlement_only":
        return "settlement_hold_review" if settlement_eligible else "no_new_edge_d0_locked"
    if reverse_edge and tail_no and edge_tier == "normal_trade":
        return "priority_tail_no_reverse_edge"
    if edge_tier == "normal_trade":
        return "paper_trade_candidate" if (entry_usable_edge or 0.0) > 0 else "strict_edge_candidate_review"
    if edge_tier == "probe":
        return "probe_candidate"
    if edge_tier == "shadow_only":
        return "shadow_only"
    if stage == "d0_morning" and settlement_eligible:
        return "settlement_dominant_watch"
    return "blocked_or_missing_edge"


def _connect_readonly(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise FileNotFoundError(f"DB missing: {db_path}")
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=20000")
    conn.execute("PRAGMA query_only=ON")
    return conn


def _pick_latest_run_id(conn: sqlite3.Connection, event_date: str) -> Optional[int]:
    row = conn.execute(
        """
        SELECT MAX(run_id)
        FROM auto_trade_decisions
        WHERE action = 'entry'
          AND run_id IS NOT NULL
          AND run_id > 0
          AND date(event_date) = ?
        """,
        (event_date,),
    ).fetchone()
    if not row:
        return None
    return _to_int(row[0])


def _pick_run_date_by_id(conn: sqlite3.Connection, run_id: int) -> str:
    row = conn.execute(
        """
        SELECT run_date
        FROM auto_trade_decisions
        WHERE run_id = ?
        ORDER BY datetime(created_at) DESC, id DESC
        LIMIT 1
        """,
        (run_id,),
    ).fetchone()
    if not row:
        return ""
    return str(row[0] or "").strip()


def _fetch_event_rows(
    conn: sqlite3.Connection,
    event_date: str,
    *,
    run_id: Optional[int] = None,
) -> List[sqlite3.Row]:
    if run_id is not None and run_id > 0:
        sql = (
            "SELECT id, run_id, run_date, slug, event_date, title, outcome, side, will_order, "
            "reason_codes_json, reasons_json, details_json, created_at "
            "FROM auto_trade_decisions "
            "WHERE action = 'entry' AND run_id = ? AND date(event_date) = ? "
            "ORDER BY datetime(created_at) DESC, id DESC"
        )
        return conn.execute(sql, (int(run_id), event_date)).fetchall()

    sql = (
        "SELECT id, run_id, run_date, slug, event_date, title, outcome, side, will_order, "
        "reason_codes_json, reasons_json, details_json, created_at "
        "FROM auto_trade_decisions "
        "WHERE action = 'entry' AND date(event_date) = ? "
        "ORDER BY datetime(created_at) DESC, id DESC"
    )
    return conn.execute(sql, (event_date,)).fetchall()


def _load_fact_profiles(conn: sqlite3.Connection, event_date: str) -> Dict[str, Dict[str, Any]]:
    try:
        rows = conn.execute(
            """
            SELECT latest.slug, hist.fact_json
            FROM event_fact_latest latest
            JOIN event_fact_history hist ON hist.id = latest.fact_id
            WHERE latest.event_date = ?
            """,
            (event_date,),
        ).fetchall()
    except sqlite3.Error:
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        try:
            fact = json.loads(str(row["fact_json"] or "{}"))
        except Exception:
            fact = {}
        if not isinstance(fact, dict):
            continue
        station = str(fact.get("station_id") or "").strip().upper()
        source = str(fact.get("resolution_source") or "").strip()
        if station or source:
            out[str(row["slug"] or "")] = {
                "station_id": station,
                "resolution_source": source,
                "market_var": str(fact.get("market_var") or "").strip(),
                "market_unit": str(fact.get("market_unit") or "").strip(),
                "location_basis": str(fact.get("location_basis") or "").strip(),
            }
    return out


def _source_contains_station(resolution_source: str, station_id: str) -> bool:
    source = str(resolution_source or "").strip().upper()
    station = str(station_id or "").strip().upper()
    return bool(source and station and f"/{station}" in source)


def _to_iso_utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _build_candidate_row(
    row: sqlite3.Row,
    *,
    fact_profile: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    details = _safe_json_text(row["details_json"])
    fact_profile = fact_profile if isinstance(fact_profile, Mapping) else {}
    reason_codes = _json_list(row["reason_codes_json"])
    reasons = _json_list(row["reasons_json"])

    strategy = str(
        details.get("strategy")
        or details.get("entry_strategy_family")
        or "main"
    ).strip() or "main"
    strategy_family = str(details.get("entry_strategy_family") or strategy or "main").strip() or strategy
    q_entry = _to_float(details.get("q_entry"))
    q_quote = _to_float(details.get("q_quote"))
    q_calibrated = _to_float(details.get("q_calibrated"))
    q_raw_static = _to_float(details.get("q_raw_static"))
    q_bayes_posterior = _to_float(details.get("q_bayes_posterior"))
    q_bayes_lcb = _to_float(details.get("q_bayes_lcb"))
    q_bayes_ucb = _to_float(details.get("q_bayes_ucb"))
    q_bayes_sigma = _to_float(details.get("q_bayes_sigma"))
    q_bayes_physical_likelihood_evidence = _to_float(
        details.get("q_bayes_physical_likelihood_evidence")
    )
    q_bayes_physical_likelihood_prob = _to_float(
        details.get("q_bayes_physical_likelihood_prob")
    )
    weather_likelihood_feature_count = _to_float(details.get("weather_likelihood_feature_count"))
    q_bayes_bimodal_risk = _coerce_bool(details.get("q_bayes_bimodal_risk"))
    q_bayes_bimodal_size_scale = _to_float(details.get("q_bayes_bimodal_size_scale"))

    ask = _to_float(details.get("ask"))
    bid = _to_float(details.get("bid"))
    spread = _to_float(details.get("spread"))

    source_fit_tier = str(
        details.get("source_fit_tier")
        or details.get("weather_strategy_source_fit_tier")
        or details.get("observed_nowcast_source_fit_tier")
        or details.get("source_trust_tier")
        or details.get("weather_strategy_source_trust_tier")
        or details.get("observed_nowcast_source_trust_tier")
        or ""
    ).strip()
    source_fit_na_reason = str(
        details.get("source_fit_na_reason")
        or details.get("source_na_reason")
        or details.get("weather_strategy_source_na_reason")
        or details.get("observed_nowcast_source_na_reason")
        or ""
    ).strip()
    resolution_source = str(details.get("resolution_source") or "").strip()
    station_id = str(details.get("station_id") or "").strip()
    source_station_id = str(details.get("source_station_id") or "").strip()
    source_station_match = _coalesce_bool(
        details.get("source_station_match"),
        details.get("weather_strategy_source_station_match"),
    )
    fact_source_used = False
    if not resolution_source and str(fact_profile.get("resolution_source") or "").strip():
        resolution_source = str(fact_profile.get("resolution_source") or "").strip()
        fact_source_used = True
    if not station_id and str(fact_profile.get("station_id") or "").strip():
        station_id = str(fact_profile.get("station_id") or "").strip().upper()
        fact_source_used = True
    if not source_station_id and station_id and _source_contains_station(resolution_source, station_id):
        source_station_id = station_id
    if source_station_match is None and station_id and source_station_id:
        source_station_match = bool(str(station_id).upper() == str(source_station_id).upper())
    if not source_fit_tier and resolution_source and station_id and source_station_match is not False:
        if "/history/daily/" in resolution_source.lower():
            source_fit_tier = "settlement_source"
            source_fit_na_reason = ""

    entry_usable_edge = _to_float(details.get("entry_usable_edge"))
    entry_usable_edge_size_scale = _to_float(details.get("entry_usable_edge_size_scale"))
    entry_usable_edge_sort_score = _to_float(details.get("entry_usable_edge_sort_score"))
    entry_usable_edge_sort_source = str(details.get("entry_usable_edge_sort_source") or "")

    if entry_usable_edge is None and entry_usable_edge_sort_score is not None:
        entry_usable_edge = entry_usable_edge_sort_score / 100.0

    edge_tradeability_pass = _coerce_bool(details.get("edge_tradeability_pass"))
    edge_tradeability_blocked = _coerce_bool(details.get("edge_tradeability_blocked"))
    edge_tradeability_profit_value = _to_float(details.get("edge_tradeability_profit_value"))
    edge_tradeability_reasons = _parse_json_field_list(details, ("edge_tradeability_reasons", "entry_hard_reason_codes"))

    na_reasons = _parse_json_field_list(
        details,
        ("entry_usable_edge_na_reason", "entry_usable_edge_na_reasons", "entry_soft_reason_codes"),
    )

    if _coerce_bool(details.get("entry_soft_no_order")):
        trade_status = "soft_block"
    elif int(row["will_order"] or 0) == 1:
        trade_status = "order"
    elif reason_codes:
        trade_status = "blocked"
    elif _coerce_bool(details.get("entry_usable_edge_sort_score")):
        trade_status = "review"
    else:
        trade_status = "blocked"

    if trade_status == "order":
        priority = 100_000.0
    elif entry_usable_edge is not None:
        priority = float(entry_usable_edge)
    elif edge_tradeability_profit_value is not None:
        priority = float(edge_tradeability_profit_value)
    else:
        priority = 0.0

    stage = _stage_from_details(details, str(row["event_date"] or ""))
    mode_default, edge_weight_default, settlement_weight_default = _mode_from_stage(stage)
    weather_mode = str(details.get("weather_strategy_mode") or mode_default).strip() or mode_default
    edge_weight = _to_float(details.get("weather_strategy_edge_weight"))
    if edge_weight is None:
        edge_weight = edge_weight_default
    settlement_weight = _to_float(details.get("weather_strategy_settlement_weight"))
    if settlement_weight is None:
        settlement_weight = settlement_weight_default
    edge_tier = str(details.get("weather_strategy_edge_tier") or "").strip()
    edge_tier_reason = str(details.get("weather_strategy_edge_tier_reason") or "").strip()
    derived_edge_tier, derived_edge_tier_reason = _edge_tier_from_profit(edge_tradeability_profit_value)
    if not edge_tier or (
        edge_tradeability_profit_value is not None
        and edge_tier != derived_edge_tier
    ):
        edge_tier, edge_tier_reason = derived_edge_tier, derived_edge_tier_reason
    entry_allowed = _coalesce_bool(details.get("weather_strategy_entry_allowed"))
    settlement_eligible = _coalesce_bool(details.get("weather_strategy_settlement_eligible"))
    if settlement_eligible is None:
        settlement_eligible = bool(source_fit_tier == "settlement_source" and not source_fit_na_reason.lower() in {"none", ""})
        if source_fit_tier == "settlement_source":
            settlement_eligible = True
    tail_no = _coalesce_bool(details.get("weather_strategy_tail_no"))
    if tail_no is None:
        tail_no = _is_tail_no(str(row["outcome"] or ""), str(row["side"] or ""), q_entry)
    reverse_edge = _coalesce_bool(details.get("weather_strategy_reverse_edge"))
    if reverse_edge is None:
        reverse_edge = bool(str(row["side"] or "").strip().upper() == "NO" and (edge_tradeability_profit_value or 0.0) > 0)
    reverse_edge_action = str(details.get("weather_strategy_reverse_edge_action") or "").strip()
    if bool(reverse_edge) and not reverse_edge_action:
        reverse_edge_action = "buy_no_equivalent_to_sell_yes"
    reverse_edge_execution_side = "NO" if bool(reverse_edge) else ""
    reverse_edge_execution_action = "BUY_NO" if bool(reverse_edge) else ""
    city_key = _slug_city(str(row["slug"] or ""))
    season = str(details.get("weather_strategy_season") or "").strip().lower()
    if not season:
        season = _season_for_event(str(row["event_date"] or ""), city_key=city_key)
    season_size_scale = _to_float(details.get("weather_strategy_season_size_scale"))
    if season_size_scale is None:
        season_size_scale = _season_size_scale(season)
    season_size_scale = max(0.0, min(1.0, float(season_size_scale)))
    entry_usable_edge_size_scale_before_season = _to_float(
        details.get("entry_usable_edge_size_scale_before_season")
    )
    if entry_usable_edge_size_scale_before_season is None:
        entry_usable_edge_size_scale_before_season = entry_usable_edge_size_scale
    entry_usable_edge_size_scale_adjusted = entry_usable_edge_size_scale
    if (
        entry_usable_edge_size_scale_adjusted is not None
        and entry_usable_edge_size_scale_before_season == entry_usable_edge_size_scale
        and season_size_scale < 1.0
    ):
        entry_usable_edge_size_scale_adjusted = max(
            0.0,
            min(1.0, float(entry_usable_edge_size_scale_adjusted) * float(season_size_scale)),
        )
    market_tier = "major" if city_key in MAJOR_WEATHER_CITIES else "secondary"
    focus_lane = "major_settlement" if market_tier == "major" else "secondary_edge"
    if reverse_edge and tail_no:
        focus_lane = "tail_no_reverse_edge"
    elif stage in {"d0_morning", "d0_afternoon_or_locked"} and settlement_eligible:
        focus_lane = "d0_physical_or_settlement"
    recommendation = _recommendation(
        trade_status=trade_status,
        stage=stage,
        mode=weather_mode,
        edge_tier=edge_tier,
        reverse_edge=bool(reverse_edge),
        tail_no=bool(tail_no),
        settlement_eligible=bool(settlement_eligible),
        entry_allowed=entry_allowed,
        entry_usable_edge=entry_usable_edge,
    )
    if (
        recommendation == "dry_run_order_intent"
        and strategy_family == "posterior_settlement"
        and entry_usable_edge is not None
        and entry_usable_edge > 0
        and entry_allowed is not False
    ):
        recommendation = "target_realization_order_intent"

    return {
        "run_id": _to_int(row["run_id"]),
        "run_date": str(row["run_date"] or ""),
        "slug": str(row["slug"] or ""),
        "event_date": _normalize_event_date(str(row["event_date"] or "")),
        "title": str(row["title"] or ""),
        "outcome": str(row["outcome"] or ""),
        "side": str(row["side"] or "").strip().upper(),
        "strategy": strategy,
        "strategy_family": strategy_family,
        "created_at": str(row["created_at"] or ""),
        "will_order": int(row["will_order"] or 0),
        "trade_status": trade_status,
        "reason_codes": reason_codes,
        "reasons": reasons,
        "entry_usable_edge": entry_usable_edge,
        "entry_usable_edge_size_scale": entry_usable_edge_size_scale,
        "entry_usable_edge_size_scale_before_season": entry_usable_edge_size_scale_before_season,
        "entry_usable_edge_size_scale_adjusted": entry_usable_edge_size_scale_adjusted,
        "entry_usable_edge_na_reason": na_reasons[0] if na_reasons else "",
        "entry_usable_edge_na_reasons": na_reasons,
        "edge_tradeability_pass": edge_tradeability_pass,
        "edge_tradeability_blocked": edge_tradeability_blocked,
        "edge_tradeability_profit_value": edge_tradeability_profit_value,
        "edge_tradeability_reasons": edge_tradeability_reasons,
        "entry_usable_edge_sort_score": entry_usable_edge_sort_score,
        "entry_usable_edge_sort_source": entry_usable_edge_sort_source,
        "q_entry": q_entry,
        "q_quote": q_quote,
        "q_calibrated": q_calibrated,
        "q_raw_static": q_raw_static,
        "q_bayes_enabled": _coerce_bool(details.get("q_bayes_enabled")),
        "q_bayes_source": str(details.get("q_bayes_source") or ""),
        "q_bayes_prior_source": str(details.get("q_bayes_prior_source") or ""),
        "q_bayes_historical_samples": _to_float(details.get("q_bayes_historical_samples")),
        "q_bayes_historical_prior_sample_scale": _to_float(
            details.get("q_bayes_historical_prior_sample_scale")
        ),
        "q_bayes_historical_bucket_count": _to_float(details.get("q_bayes_historical_bucket_count")),
        "q_bayes_bias_correction_enabled": _coerce_bool(details.get("q_bayes_bias_correction_enabled")),
        "q_bayes_posterior": q_bayes_posterior,
        "q_bayes_lcb": q_bayes_lcb,
        "q_bayes_ucb": q_bayes_ucb,
        "q_bayes_sigma": q_bayes_sigma,
        "q_bayes_alpha": _to_float(details.get("q_bayes_alpha")),
        "q_bayes_alpha_total": _to_float(details.get("q_bayes_alpha_total")),
        "q_bayes_prior_strength": _to_float(details.get("q_bayes_prior_strength")),
        "q_bayes_model_evidence": _to_float(details.get("q_bayes_model_evidence")),
        "q_bayes_nowcast_evidence": _to_float(details.get("q_bayes_nowcast_evidence")),
        "q_bayes_physical_likelihood_evidence": q_bayes_physical_likelihood_evidence,
        "q_bayes_physical_likelihood_enabled": _coerce_bool(
            details.get("q_bayes_physical_likelihood_enabled")
        ),
        "q_bayes_physical_likelihood_source": str(
            details.get("q_bayes_physical_likelihood_source") or ""
        ),
        "q_bayes_physical_likelihood_prob": q_bayes_physical_likelihood_prob,
        "q_bayes_bimodal_risk": bool(q_bayes_bimodal_risk),
        "q_bayes_bimodal_size_scale": q_bayes_bimodal_size_scale,
        "q_bayes_bimodal_size_scale_applied": _coerce_bool(
            details.get("q_bayes_bimodal_size_scale_applied")
        ),
        "strategy_size_ratio_before_bayes_bimodal": _to_float(
            details.get("strategy_size_ratio_before_bayes_bimodal")
        ),
        "weather_likelihood_features_source": str(
            details.get("weather_likelihood_features_source") or ""
        ),
        "weather_likelihood_aux_source": str(
            details.get("weather_likelihood_aux_source") or ""
        ),
        "weather_likelihood_feature_count": weather_likelihood_feature_count,
        "peak_estimator_source": str(details.get("peak_estimator_source") or ""),
        "peak_estimator_mu_c": _to_float(details.get("peak_estimator_mu_c")),
        "peak_estimator_sigma_c": _to_float(details.get("peak_estimator_sigma_c")),
        "peak_estimator_kalman_gain": _to_float(details.get("peak_estimator_kalman_gain")),
        "peak_estimator_combined_discount": _to_float(
            details.get("peak_estimator_combined_discount")
        ),
        "ask": ask,
        "bid": bid,
        "spread": spread,
        "source_fit_tier": source_fit_tier,
        "source_fit_na_reason": source_fit_na_reason,
        "resolution_source": resolution_source,
        "station_id": station_id,
        "resolution_source_profile_source": "event_fact_latest" if fact_source_used else "decision_details",
        "source_station_id": source_station_id,
        "source_station_match": source_station_match,
        "city_key": city_key,
        "market_tier": market_tier,
        "focus_lane": focus_lane,
        "strategy_recommendation": recommendation,
        "weather_strategy_stage": stage,
        "weather_strategy_mode": weather_mode,
        "weather_strategy_edge_weight": edge_weight,
        "weather_strategy_settlement_weight": settlement_weight,
        "weather_strategy_edge_tier": edge_tier,
        "weather_strategy_edge_tier_reason": edge_tier_reason,
        "weather_strategy_entry_allowed": entry_allowed,
        "weather_strategy_entry_block_reason": str(details.get("weather_strategy_entry_block_reason") or ""),
        "weather_strategy_reverse_edge": bool(reverse_edge),
        "weather_strategy_reverse_edge_action": reverse_edge_action,
        "reverse_edge_execution_side": reverse_edge_execution_side,
        "reverse_edge_execution_action": reverse_edge_execution_action,
        "weather_strategy_tail_no": bool(tail_no),
        "weather_strategy_settlement_eligible": bool(settlement_eligible),
        "weather_strategy_settlement_eligible_reason": str(
            details.get("weather_strategy_settlement_eligible_reason") or ""
        ),
        "weather_strategy_season": str(season or "unknown"),
        "weather_strategy_season_size_scale": season_size_scale,
        "priority": priority,
        "decision_id": int(row["id"] or 0),
    }


def _build_event_plan(
    conn: sqlite3.Connection,
    event_date: str,
    run_id: Optional[int] = None,
    top: int = MAX_SUMMARY_ROWS,
    rows_limit: Optional[int] = None,
    market_scope: str = "highest_temperature",
) -> Optional[Dict[str, Any]]:
    event_date = _normalize_event_date(event_date)
    if not event_date:
        return None
    chosen_run_id = run_id if run_id is not None and int(run_id) > 0 else None
    rows = _fetch_event_rows(conn, event_date, run_id=chosen_run_id)
    if not rows:
        return {
            "event_date": event_date,
            "run_id": int(chosen_run_id) if chosen_run_id is not None else None,
            "run_date": _pick_run_date_by_id(conn, int(chosen_run_id)) if chosen_run_id is not None else "",
            "rows": [],
            "summary": {
                "total": 0,
                "order_intent": 0,
                "soft_block": 0,
                "hard_block": 0,
                "strategy_count": 0,
            },
            "note": "run_id_has_no_entry_rows_for_event_date" if chosen_run_id is not None else "no_entry_rows_for_event_date",
        }

    by_key: Dict[Tuple[str, str, str, str, str], Dict[str, Any]] = {}
    fact_profiles = _load_fact_profiles(conn, event_date)
    for row in rows:
        candidate = _build_candidate_row(
            row,
            fact_profile=fact_profiles.get(str(row["slug"] or ""), {}),
        )
        if str(market_scope or "").strip().lower() in {
            "highest_temperature",
            "highest-temp",
            "highest_temp",
            "temperature_2m_max",
        } and not _is_highest_temperature_market(candidate.get("slug") or "", candidate.get("title") or ""):
            continue
        key = (
            candidate["slug"],
            candidate["outcome"],
            candidate["side"],
            candidate["strategy"],
            candidate["strategy_family"],
        )
        created_at = candidate["created_at"]
        prev = by_key.get(key)
        if prev is not None and created_at:
            if str(created_at) <= str(prev.get("created_at") or ""):
                continue
        by_key[key] = candidate

    plan_rows = list(by_key.values())
    plan_rows.sort(
        key=lambda item: (
            1 if str(item.get("trade_status") or "") == "order" else 0,
            item.get("priority") if item.get("priority") is not None else -1e18,
            item.get("entry_usable_edge") if item.get("entry_usable_edge") is not None else -1e18,
        ),
        reverse=True,
    )

    order_intent = sum(1 for item in plan_rows if int(item.get("will_order") or 0) == 1)
    soft_block = sum(1 for item in plan_rows if str(item.get("trade_status") or "") == "soft_block")
    hard_block = sum(1 for item in plan_rows if str(item.get("trade_status") or "") == "blocked")
    strategy_count = len({str(item.get("strategy") or "") for item in plan_rows})
    by_recommendation: Dict[str, int] = {}
    by_edge_tier: Dict[str, int] = {}
    by_focus_lane: Dict[str, int] = {}
    settlement_eligible_count = 0
    reverse_edge_count = 0
    tail_no_count = 0
    for item in plan_rows:
        rec = str(item.get("strategy_recommendation") or "unknown")
        by_recommendation[rec] = by_recommendation.get(rec, 0) + 1
        tier = str(item.get("weather_strategy_edge_tier") or "unknown")
        by_edge_tier[tier] = by_edge_tier.get(tier, 0) + 1
        lane = str(item.get("focus_lane") or "unknown")
        by_focus_lane[lane] = by_focus_lane.get(lane, 0) + 1
        if item.get("weather_strategy_settlement_eligible"):
            settlement_eligible_count += 1
        if item.get("weather_strategy_reverse_edge"):
            reverse_edge_count += 1
        if item.get("weather_strategy_tail_no"):
            tail_no_count += 1
    top_rows = plan_rows[: max(1, int(top) if int(top) > 0 else MAX_SUMMARY_ROWS)]

    # Keep default behavior backward-compatible: without explicit limit, emit full rows.
    rows_out = plan_rows
    if isinstance(rows_limit, int) and rows_limit > 0:
        rows_out = plan_rows[:rows_limit]

    return {
        "event_date": event_date,
        "run_id": int(chosen_run_id) if chosen_run_id is not None else None,
        "run_date": (
            _pick_run_date_by_id(conn, int(chosen_run_id))
            if chosen_run_id is not None
            else str(plan_rows[0].get("run_date") or "")
        ),
        "selection": "forced_run_id" if chosen_run_id is not None else "latest_per_market_outcome_side_strategy",
        "rows": rows_out,
        "summary": {
            "total": len(plan_rows),
            "rows": len(rows_out),
            "order_intent": order_intent,
            "soft_block": soft_block,
            "hard_block": hard_block,
            "strategy_count": strategy_count,
            "settlement_eligible": settlement_eligible_count,
            "reverse_edge": reverse_edge_count,
            "tail_no": tail_no_count,
            "by_recommendation": by_recommendation,
            "by_edge_tier": by_edge_tier,
            "by_focus_lane": by_focus_lane,
            "resource_focus": _resource_focus_summary(plan_rows),
            "top": len(top_rows),
            "top_rows": top_rows,
        },
    }


def build_strategy_plan(
    db_path: Path,
    event_dates: Sequence[str],
    run_id: Optional[int] = None,
    top: int = MAX_SUMMARY_ROWS,
    rows_limit: Optional[int] = None,
    market_scope: str = "highest_temperature",
) -> Dict[str, Any]:
    conn = _connect_readonly(db_path)
    try:
        plans = []
        for event_date in _normalize_event_dates(event_dates):
            plan = _build_event_plan(
                conn,
                event_date=event_date,
                run_id=run_id,
                top=top,
                rows_limit=rows_limit,
                market_scope=market_scope,
            )
            if plan is not None:
                plans.append(plan)

        return {
            "ok": True,
            "generated_at": _to_iso_utc_now(),
            "db_path": str(db_path),
            "requested_run_id": int(run_id) if run_id and int(run_id) > 0 else None,
            "market_scope": str(market_scope or ""),
            "plans": plans,
        }
    finally:
        conn.close()


def _print_human_report(report: Mapping[str, Any]) -> None:
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build per-event-date strategy plan from archive auto_trade_decisions.",
    )
    parser.add_argument(
        "--db",
        default=str(DB_PATH),
        help="path to archive.db (default: data/archive.db)",
    )
    parser.add_argument(
        "--event-date",
        action="append",
        default=[],
        help="target event_date(s), repeatable or comma-separated",
    )
    parser.add_argument(
        "--run-id",
        type=int,
        default=0,
        help="if set, force a specific entry run_id for all dates",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=MAX_SUMMARY_ROWS,
        help="limit top rows included in each plan's top_rows",
    )
    parser.add_argument(
        "--rows",
        type=int,
        default=0,
        help="limit rows in each plan output; 0 means keep all rows",
    )
    parser.add_argument("--market-scope", default="highest_temperature", help="highest_temperature|all")
    parser.add_argument("--json", action="store_true", help="print compact JSON only")
    parser.add_argument("--out", default="", help="write report JSON to this file")
    args = parser.parse_args()

    db_path = Path(args.db)
    selected_run_id = _to_int(args.run_id)
    if selected_run_id is not None and selected_run_id <= 0:
        selected_run_id = None
    top_rows = _to_int(args.top)
    if top_rows is None or top_rows <= 0:
        top_rows = MAX_SUMMARY_ROWS

    row_limit = _to_int(args.rows)
    if row_limit is None or row_limit < 0:
        row_limit = None

    event_dates = args.event_date or ["2026-05-31", "2026-06-01"]

    report = build_strategy_plan(
        db_path,
        event_dates,
        run_id=selected_run_id,
        top=top_rows,
        rows_limit=row_limit,
        market_scope=str(args.market_scope or "highest_temperature"),
    )

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if args.json:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    else:
        _print_human_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
