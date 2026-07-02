#!/usr/bin/env python3
"""Build calibration labels from settled weather outcomes.

Default mode is read-only. Pass --apply to insert missing calibration_labels rows.
The script labels entry decisions by comparing the decision bucket to the latest
event_runs.settlement_actual for the same slug/date.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
DEFAULT_DB = ROOT / "data" / "archive.db"
DEFAULT_MARKET_SCOPE = "highest_temperature"
DEFAULT_BACKUP_DIR = ROOT / "data" / "backups"
APPLY_CONFIRM_PHRASE = "APPLY_SETTLEMENT_LABELS"
_MONTH_TO_NUM = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sqlite_backup(
    db_path: Path,
    *,
    backup_before_apply: bool,
    backup_dir: Path,
) -> Dict[str, Any]:
    if not backup_before_apply:
        return {
            "requested": True,
            "created": False,
            "path": "",
            "reason": "disabled_by_no_backup_before_apply",
        }
    if not db_path.exists():
        return {
            "requested": True,
            "created": False,
            "path": "",
            "reason": "source_db_missing",
        }
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = backup_dir / f"archive.before-settlement-label-apply.{stamp}.db"
    suffix = 1
    while backup_path.exists():
        backup_path = backup_dir / f"archive.before-settlement-label-apply.{stamp}.{suffix}.db"
        suffix += 1
    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
    try:
        dst = sqlite3.connect(str(backup_path), timeout=30)
        try:
            src.backup(dst)
            dst.commit()
        finally:
            dst.close()
    finally:
        src.close()
    return {
        "requested": True,
        "created": True,
        "path": str(backup_path),
        "reason": "created_before_direct_label_apply",
    }


def _cutoff_iso(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=max(1, int(days)))).isoformat(
        timespec="seconds"
    )


def _to_float(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _json_obj(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _event_date_key(value: Any) -> str:
    return str(value or "").strip().split("T", 1)[0]


def _slug_event_date(slug: Any) -> str:
    text = str(slug or "").strip().lower()
    if not text:
        return ""
    m = re.search(
        r"-on-(january|february|march|april|may|june|july|august|september|october|november|december)-(\d{1,2})-(\d{4})(?:$|[^0-9])",
        text,
    )
    if not m:
        return ""
    month = _MONTH_TO_NUM.get(m.group(1))
    if not month:
        return ""
    try:
        return datetime(int(m.group(3)), int(month), int(m.group(2))).date().isoformat()
    except ValueError:
        return ""


def _event_date_mismatch(slug: Any, event_date: Any) -> bool:
    from_slug = _slug_event_date(slug)
    from_row = _event_date_key(event_date)
    return bool(from_slug and from_row and from_slug != from_row)


def _is_closed_settlement_row(row: sqlite3.Row, *, now_utc: datetime, today_et: str) -> bool:
    market_closed = str(row["market_closed"] if "market_closed" in row.keys() else "").strip().lower()
    if market_closed in {"1", "true", "yes", "on"}:
        return True
    if market_closed in {"0", "false", "no", "off"}:
        return False
    event_end_raw = str(row["event_end_utc"] if "event_end_utc" in row.keys() else "").strip()
    if event_end_raw:
        try:
            event_end = datetime.fromisoformat(event_end_raw.replace("Z", "+00:00"))
            if event_end.tzinfo is None:
                event_end = event_end.replace(tzinfo=timezone.utc)
            if event_end <= now_utc:
                return True
        except ValueError:
            pass
    event_date = _event_date_key(row["event_date"] if "event_date" in row.keys() else "")
    return bool(event_date and event_date < today_et)


def bucket_hit(actual: float, label: str) -> Optional[bool]:
    text = str(label or "").strip()
    if not text:
        return None
    nums = [float(x) for x in re.findall(r"-?\d+(?:\.\d+)?", text)]
    low = text.lower()
    if any(token in low for token in ("or higher", "above", "+")) and nums:
        return actual >= nums[0]
    if any(token in low for token in ("or lower", "or below", "below")) and nums:
        return actual <= nums[0]
    if len(nums) >= 2:
        lo, hi = min(nums[0], nums[1]), max(nums[0], nums[1])
        return lo <= actual <= hi
    if len(nums) == 1:
        return round(actual) == round(nums[0])
    return None


def _decision_rows(
    conn: sqlite3.Connection,
    *,
    since_iso: str,
    city: str,
    bayes_only: bool,
    market_scope: str = DEFAULT_MARKET_SCOPE,
) -> List[sqlite3.Row]:
    city_like = f"%{city.lower()}%"
    scope_key = str(market_scope or "").strip().lower()
    market_sql = ""
    if scope_key in {"highest_temperature", "highest-temp", "highest_temp", "temperature_2m_max"}:
        market_sql = """
          AND (
            lower(d.slug) LIKE 'highest-temperature-in-%'
            OR lower(d.slug) LIKE '%high-temperature%'
            OR lower(d.title) LIKE '%highest temperature%'
            OR lower(d.title) LIKE '%high temperature%'
          )
          AND lower(d.slug) NOT LIKE 'lowest-temperature-in-%'
          AND lower(d.title) NOT LIKE '%lowest temperature%'
        """
    label_cols = {str(row[1]) for row in conn.execute("PRAGMA table_info(calibration_labels)").fetchall()}
    existing_label_expr = "c.id" if "id" in label_cols else "c.decision_id"
    rows = conn.execute(
        f"""
        SELECT
          d.id,
          d.slug,
          d.event_date,
          d.title,
          d.outcome,
          d.side,
          d.details_json,
          d.created_at,
          {existing_label_expr} AS existing_label_id
        FROM auto_trade_decisions d
        LEFT JOIN calibration_labels c ON c.decision_id = d.id
        WHERE d.action = 'entry'
          AND datetime(d.created_at) >= datetime(?)
          AND (? = '' OR lower(d.slug) LIKE ? OR lower(d.title) LIKE ?)
          {market_sql}
        ORDER BY d.id ASC
        """,
        (since_iso, city.lower(), city_like, city_like),
    ).fetchall()
    if not bayes_only:
        return rows
    out: List[sqlite3.Row] = []
    for row in rows:
        if _truthy(_json_obj(row["details_json"]).get("q_bayes_enabled")):
            out.append(row)
    return out


def _load_settlements(
    conn: sqlite3.Connection,
    keys: Iterable[Tuple[str, str]],
    *,
    allow_open_events: bool,
    strict_resolution_source: bool,
) -> Tuple[Dict[Tuple[str, str], Dict[str, Any]], Counter[str]]:
    out: Dict[Tuple[str, str], Dict[str, Any]] = {}
    skipped: Counter[str] = Counter()
    now_utc = datetime.now(timezone.utc)
    today_et = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    cols = {str(row[1]) for row in conn.execute("PRAGMA table_info(event_runs)").fetchall()}
    if "settlement_actual" not in cols:
        skipped["settlement_actual_column_missing"] += 1
        return out, skipped
    market_closed_expr = "market_closed" if "market_closed" in cols else "NULL AS market_closed"
    event_end_expr = "event_end_utc" if "event_end_utc" in cols else "NULL AS event_end_utc"
    resolution_source_expr = (
        "resolution_source" if "resolution_source" in cols else "NULL AS resolution_source"
    )
    station_id_expr = "station_id" if "station_id" in cols else "NULL AS station_id"
    rounding_rule_expr = "rounding_rule" if "rounding_rule" in cols else "NULL AS rounding_rule"
    market_unit_expr = "market_unit" if "market_unit" in cols else "NULL AS market_unit"
    native_value_expr = (
        "settlement_native_value"
        if "settlement_native_value" in cols
        else "NULL AS settlement_native_value"
    )
    native_unit_expr = (
        "settlement_native_unit"
        if "settlement_native_unit" in cols
        else "NULL AS settlement_native_unit"
    )
    native_precision_expr = (
        "settlement_native_precision"
        if "settlement_native_precision" in cols
        else "NULL AS settlement_native_precision"
    )
    for slug, event_date in sorted(set(keys)):
        slug_s = str(slug or "").strip()
        date_s = _event_date_key(event_date)
        if not slug_s or not date_s:
            continue
        latest_fact = _latest_fact_payload(conn, slug_s, date_s)
        rows = conn.execute(
            f"""
            SELECT settlement_actual, created_at, id, event_date, {market_closed_expr}, {event_end_expr},
                   {resolution_source_expr}, {station_id_expr}, {rounding_rule_expr},
                   {market_unit_expr}, {native_value_expr}, {native_unit_expr}, {native_precision_expr}
            FROM event_runs
            WHERE slug = ?
              AND event_date = ?
              AND settlement_actual IS NOT NULL
            ORDER BY created_at DESC, id DESC
            """,
            (slug_s, date_s),
        ).fetchall()
        row = None
        saw_unclosed = False
        saw_source_rejected = False
        source_reject_reason = ""
        for candidate in rows:
            if not (
                allow_open_events
                or _is_closed_settlement_row(
                candidate,
                now_utc=now_utc,
                today_et=today_et,
                )
            ):
                saw_unclosed = True
                continue
            boundary_reject_reason = _settlement_unit_boundary_reject_reason(candidate)
            if boundary_reject_reason:
                saw_source_rejected = True
                source_reject_reason = boundary_reject_reason
                continue
            hard_observation_reject_reason = _settlement_hard_observation_reject_reason(
                candidate,
                latest_fact,
            )
            if hard_observation_reject_reason:
                saw_source_rejected = True
                source_reject_reason = hard_observation_reject_reason
                continue
            if strict_resolution_source:
                reject_reason = _settlement_source_reject_reason(candidate)
                if reject_reason:
                    saw_source_rejected = True
                    source_reject_reason = reject_reason
                    continue
            row = candidate
            break
        if not row:
            if rows and saw_source_rejected:
                skipped[source_reject_reason or "settlement_source_rejected"] += 1
                continue
            if rows and saw_unclosed:
                skipped["settlement_not_closed"] += 1
            continue
        actual = _to_float(row["settlement_actual"])
        if actual is None:
            continue
        out[(slug_s, date_s)] = {
            "settlement_actual": actual,
            "resolved_at": str(row["created_at"] or ""),
            "event_run_id": int(row["id"] or 0),
            "resolution_source": str(row["resolution_source"] or ""),
            "station_id": str(row["station_id"] or ""),
        }
    return out, skipped


def _row_value(row: sqlite3.Row, key: str, default: Any = None) -> Any:
    try:
        if key in row.keys():
            return row[key]
    except Exception:
        return default
    return default


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (name,),
    ).fetchone()
    return row is not None


def _latest_fact_payload(conn: sqlite3.Connection, slug: str, event_date: str) -> Dict[str, Any]:
    if not (_table_exists(conn, "event_fact_latest") and _table_exists(conn, "event_fact_history")):
        return {}
    try:
        row = conn.execute(
            """
            SELECT h.fact_json
            FROM event_fact_latest l
            JOIN event_fact_history h ON h.id = l.fact_id
            WHERE l.slug = ? AND l.event_date = ?
            LIMIT 1
            """,
            (slug, event_date),
        ).fetchone()
    except sqlite3.OperationalError:
        return {}
    if row is None:
        return {}
    return _json_obj(row["fact_json"] if isinstance(row, sqlite3.Row) else row[0])


def _payload_features(payload: Dict[str, Any]) -> Dict[str, Any]:
    features = payload.get("weather_likelihood_features") if isinstance(payload, dict) else {}
    return dict(features) if isinstance(features, dict) else {}


def _settlement_bucket_for_policy(value: Any, rounding_rule: Any) -> Optional[int]:
    raw = _to_float(value)
    if raw is None:
        return None
    policy = str(rounding_rule or "whole_degrees").strip().lower()
    if "round" in policy and "whole" not in policy and "floor" not in policy:
        return int(round(float(raw)))
    return int(math.floor(float(raw)))


def _fact_awc_hard_observation_c(payload: Dict[str, Any]) -> Optional[float]:
    features = _payload_features(payload)
    if _truthy(features.get("zuuu_metar_raw_awc_date_mismatch")):
        return None
    awc_value = _to_float(features.get("zuuu_metar_raw_awc_today_max_c"))
    if awc_value is not None:
        return float(awc_value)
    source = str(features.get("source") or "").strip().lower()
    observed_source = str(features.get("observed_today_max_source") or "").strip().lower()
    if (
        "zuuu_metar_raw_awc" in source
        or "zuuu_metar_raw_awc" in observed_source
    ) and _truthy(features.get("observed_today_max_trusted")):
        observed = _to_float(features.get("observed_today_max_c"))
        if observed is not None:
            return float(observed)
    return None


def _settlement_hard_observation_reject_reason(
    row: sqlite3.Row,
    fact_payload: Dict[str, Any],
) -> str:
    actual = _to_float(_row_value(row, "settlement_actual"))
    awc_value = _fact_awc_hard_observation_c(fact_payload)
    if actual is None or awc_value is None:
        return ""
    rounding_rule = _row_value(row, "rounding_rule", "whole_degrees")
    settlement_bucket = _settlement_bucket_for_policy(actual, rounding_rule)
    awc_bucket = _settlement_bucket_for_policy(awc_value, rounding_rule)
    if settlement_bucket is None or awc_bucket is None:
        return ""
    if int(settlement_bucket) < int(awc_bucket):
        return "settlement_below_awc_hard_observation"
    return ""


def _settlement_unit_boundary_reject_reason(row: sqlite3.Row) -> str:
    actual = _to_float(_row_value(row, "settlement_actual"))
    if actual is None:
        return ""
    settlement_unit = str(_row_value(row, "market_unit", "C") or "C")
    rounding_rule = str(_row_value(row, "rounding_rule", "whole_degrees") or "whole_degrees")
    native_value = _row_value(row, "settlement_native_value")
    native_unit = _row_value(row, "settlement_native_unit")
    native_precision = _row_value(row, "settlement_native_precision")

    profile: Dict[str, Any] = {}
    if native_value is not None and str(native_unit or "").strip():
        try:
            from scan_weather_market import temperature_settlement_precision_profile

            profile = temperature_settlement_precision_profile(
                native_value=native_value,
                native_unit=native_unit,
                settlement_unit=settlement_unit,
                settlement_policy=rounding_rule,
                native_precision=str(native_precision or ""),
                source="event_runs.settlement_actual_native",
                display_value=actual,
                display_unit=settlement_unit,
            )
        except Exception:
            profile = {}
    elif abs(float(actual) - round(float(actual))) > 1e-6:
        f_value = float(actual) * 9.0 / 5.0 + 32.0
        f_integer = int(round(float(f_value)))
        if abs(float(f_value) - float(f_integer)) <= 1e-6:
            try:
                from scan_weather_market import temperature_settlement_precision_profile

                profile = temperature_settlement_precision_profile(
                    native_value=f_integer,
                    native_unit="F",
                    settlement_unit=settlement_unit,
                    settlement_policy=rounding_rule,
                    native_precision="integer_display",
                    source="event_runs.settlement_actual_reverse_f_guard",
                    display_value=actual,
                    display_unit=settlement_unit,
                )
            except Exception:
                profile = {}
    if bool(profile.get("boundary_ambiguous")):
        return "settlement_unit_boundary_ambiguous"
    return ""


def _settlement_source_reject_reason(row: sqlite3.Row) -> str:
    station_id = str((row["station_id"] if "station_id" in row.keys() else "") or "").strip()
    resolution_source = str(
        (row["resolution_source"] if "resolution_source" in row.keys() else "") or ""
    ).strip()
    if not station_id:
        return "settlement_station_missing"
    if not resolution_source:
        return "settlement_source_missing"
    try:
        from scan_weather_market import weather_resolution_source_profile

        profile = weather_resolution_source_profile(
            resolution_source,
            station_id=station_id,
        )
    except Exception:
        return "settlement_source_profile_unavailable"
    fit = str(profile.get("source_fit_tier") or profile.get("source_trust_tier") or "").strip()
    if str(profile.get("source_station_match")).strip().lower() == "false":
        return "resolution_station_mismatch"
    if fit != "settlement_source" or profile.get("source_usable_for_settlement") is False:
        return str(profile.get("source_na_reason") or "settlement_source_not_strict")
    return ""


def build_label_plan(
    db_path: Path,
    *,
    since_days: int,
    city: str = "",
    market_scope: str = DEFAULT_MARKET_SCOPE,
    bayes_only: bool = True,
    allow_open_events: bool = False,
    strict_resolution_source: bool = True,
    limit: int = 0,
) -> Dict[str, Any]:
    since_iso = _cutoff_iso(since_days)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        decisions = _decision_rows(
            conn,
            since_iso=since_iso,
            city=str(city or ""),
            bayes_only=bool(bayes_only),
            market_scope=str(market_scope or ""),
        )
        settlements, settlement_skipped = _load_settlements(
            conn,
            ((str(r["slug"] or ""), _event_date_key(r["event_date"])) for r in decisions),
            allow_open_events=bool(allow_open_events),
            strict_resolution_source=bool(strict_resolution_source),
        )
    finally:
        conn.close()

    planned: List[Dict[str, Any]] = []
    skipped: Counter[str] = Counter()
    labels: Counter[str] = Counter()
    for row in decisions:
        if row["existing_label_id"] is not None:
            skipped["already_labeled"] += 1
            continue
        slug = str(row["slug"] or "").strip()
        event_date = _event_date_key(row["event_date"])
        if _event_date_mismatch(slug, event_date):
            skipped["event_date_mismatch"] += 1
            continue
        settlement = settlements.get((slug, event_date))
        if not settlement:
            skipped["settlement_missing"] += 1
            continue
        hit = bucket_hit(float(settlement["settlement_actual"]), str(row["outcome"] or ""))
        if hit is None:
            skipped["bucket_unparseable"] += 1
            continue
        side = str(row["side"] or "").strip().upper()
        if side not in {"YES", "NO"}:
            skipped["side_invalid"] += 1
            continue
        side_hit = bool(hit) if side == "YES" else not bool(hit)
        label = 1 if side_hit else 0
        labels[str(label)] += 1
        planned.append(
            {
                "decision_id": int(row["id"]),
                "slug": slug,
                "event_date": event_date,
                "title": str(row["title"] or ""),
                "outcome": str(row["outcome"] or ""),
                "side": side,
                "label": label,
                "label_source": "settlement_actual_bucket",
                "settlement_actual": float(settlement["settlement_actual"]),
                "resolved_at": str(settlement.get("resolved_at") or ""),
                "event_run_id": int(settlement.get("event_run_id") or 0),
                "resolution_source": str(settlement.get("resolution_source") or ""),
                "station_id": str(settlement.get("station_id") or ""),
                "decision_created_at": str(row["created_at"] or ""),
            }
        )
        if limit and len(planned) >= max(1, int(limit)):
            break

    return {
        "ok": True,
        "db": str(db_path),
        "generated_at": _utc_now(),
        "since_days": max(1, int(since_days)),
        "city_filter": str(city or ""),
        "market_scope": str(market_scope or ""),
        "bayes_only": bool(bayes_only),
        "allow_open_events": bool(allow_open_events),
        "strict_resolution_source": bool(strict_resolution_source),
        "decisions_scanned": len(decisions),
        "settlement_keys": len(settlements),
        "planned_labels": len(planned),
        "label_counts": dict(labels),
        "settlement_skipped": dict(settlement_skipped),
        "skipped": dict(skipped),
        "labels": planned,
    }


def apply_label_plan(db_path: Path, labels: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    now = _utc_now()
    inserted = 0
    skipped_existing = 0
    conn = sqlite3.connect(str(db_path))
    try:
        for item in labels:
            decision_id = int(item.get("decision_id") or 0)
            if decision_id <= 0:
                continue
            existing = conn.execute(
                "SELECT id FROM calibration_labels WHERE decision_id = ? LIMIT 1",
                (decision_id,),
            ).fetchone()
            if existing:
                skipped_existing += 1
                continue
            conn.execute(
                """
                INSERT INTO calibration_labels(
                  sample_id, decision_id, slug, event_date, outcome, side, label, label_source,
                  realized_pnl, path_hit, path_timeout, path_adverse, state, resolved_at, created_at
                )
                VALUES(
                  (SELECT id FROM calibration_samples WHERE decision_id = ? LIMIT 1),
                  ?, ?, ?, ?, ?, ?, ?,
                  NULL, ?, 0, ?, 'settled', ?, ?
                )
                """,
                (
                    decision_id,
                    decision_id,
                    str(item.get("slug") or ""),
                    str(item.get("event_date") or ""),
                    str(item.get("outcome") or ""),
                    str(item.get("side") or "").upper(),
                    int(item.get("label") or 0),
                    str(item.get("label_source") or "settlement_actual_bucket"),
                    1 if int(item.get("label") or 0) == 1 else 0,
                    1 if int(item.get("label") or 0) == 0 else 0,
                    str(item.get("resolved_at") or ""),
                    now,
                ),
            )
            inserted += 1
        conn.commit()
    finally:
        conn.close()
    return {"inserted": inserted, "skipped_existing": skipped_existing}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build settlement-driven calibration labels for weather decisions."
    )
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--since-days", type=int, default=30)
    parser.add_argument("--city", default="")
    parser.add_argument("--market-scope", default=DEFAULT_MARKET_SCOPE, help="highest_temperature|all")
    parser.add_argument("--all-decisions", action="store_true", help="Include non-Bayesian decisions.")
    parser.add_argument(
        "--allow-open-events",
        action="store_true",
        help="Allow labels from same-day/future events that are not closed. Unsafe for calibration.",
    )
    parser.add_argument(
        "--allow-unverified-resolution-source",
        action="store_true",
        help="Unsafe: allow labels without strict Polymarket resolution-source/station validation.",
    )
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--apply", action="store_true", help="Insert missing labels. Default is dry-run.")
    parser.add_argument(
        "--confirm-apply",
        default="",
        help=f"Required with --apply. Must equal {APPLY_CONFIRM_PHRASE}.",
    )
    parser.add_argument(
        "--no-backup-before-apply",
        action="store_true",
        help="Disable direct-apply backup; confirmed apply will be blocked.",
    )
    parser.add_argument("--backup-dir", default=str(DEFAULT_BACKUP_DIR))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    report = build_label_plan(
        Path(args.db),
        since_days=int(args.since_days),
        city=str(args.city or ""),
        market_scope=str(args.market_scope or DEFAULT_MARKET_SCOPE),
        bayes_only=not bool(args.all_decisions),
        allow_open_events=bool(args.allow_open_events),
        strict_resolution_source=not bool(args.allow_unverified_resolution_source),
        limit=max(0, int(args.limit or 0)),
    )
    report["mode"] = "apply" if args.apply else "dry_run"
    if args.apply:
        if str(args.confirm_apply or "").strip() != APPLY_CONFIRM_PHRASE:
            report["ok"] = False
            report["apply_result"] = {
                "inserted": 0,
                "skipped_existing": 0,
                "blocked": True,
                "block_reason": "missing_apply_confirmation",
                "required_confirm_apply": APPLY_CONFIRM_PHRASE,
            }
            text = json.dumps(
                report,
                ensure_ascii=False,
                indent=None if args.json else 2,
                sort_keys=not args.json,
            )
            print(text)
            return 2
        backup = _sqlite_backup(
            Path(args.db),
            backup_before_apply=not bool(args.no_backup_before_apply),
            backup_dir=Path(args.backup_dir),
        )
        report["pre_apply_backup"] = backup
        if not bool(backup.get("created")):
            report["ok"] = False
            report["apply_result"] = {
                "inserted": 0,
                "skipped_existing": 0,
                "blocked": True,
                "block_reason": "pre_apply_backup_not_created",
                "required_confirm_apply": APPLY_CONFIRM_PHRASE,
            }
            text = json.dumps(
                report,
                ensure_ascii=False,
                indent=None if args.json else 2,
                sort_keys=not args.json,
            )
            print(text)
            return 2
        report["apply_result"] = apply_label_plan(Path(args.db), report.get("labels") or [])

    text = json.dumps(report, ensure_ascii=False, indent=None if args.json else 2, sort_keys=not args.json)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
