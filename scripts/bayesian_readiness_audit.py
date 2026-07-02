#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.build_settlement_calibration_labels import build_label_plan

DEFAULT_DB = ROOT / "data" / "archive.db"
DEFAULT_OUT = ROOT / "data" / "reports" / "bayesian_readiness_audit_live.json"
DEFAULT_MARKET_SCOPE = "highest_temperature"


def _to_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        out = float(value)
    except Exception:
        return None
    if not math.isfinite(out):
        return None
    return out


def _pct(num: int, den: int) -> Optional[float]:
    if den <= 0:
        return None
    return float(num) / float(den)


def _q(vals: Iterable[float], q: float) -> Optional[float]:
    xs = sorted(float(v) for v in vals if math.isfinite(float(v)))
    if not xs:
        return None
    idx = int(round((len(xs) - 1) * max(0.0, min(1.0, float(q)))))
    return float(xs[idx])


def _truthy(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes", "on"}


def _json_loads(raw: Any) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        return None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _cutoff_iso(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=max(1, int(days)))).isoformat(
        timespec="seconds"
    )


def _coverage_status(rate: Optional[float], *, warn: float, ok: float) -> str:
    if rate is None:
        return "missing"
    if rate >= ok:
        return "ok"
    if rate >= warn:
        return "warn"
    return "fail"


def _market_scope_sql(scope: str, *, alias: str = "") -> Tuple[str, Tuple[str, ...]]:
    scope_key = str(scope or "").strip().lower()
    prefix = f"{alias}." if alias else ""
    if scope_key in {"", "all", "weather"}:
        return "", ()
    if scope_key in {"highest_temperature", "highest-temp", "highest_temp", "temperature_2m_max"}:
        slug = f"lower({prefix}slug)"
        title = f"lower({prefix}title)"
        return (
            f"""
            AND (
              {slug} LIKE 'highest-temperature-in-%'
              OR {slug} LIKE '%high-temperature%'
              OR {title} LIKE '%highest temperature%'
              OR {title} LIKE '%high temperature%'
            )
            AND {slug} NOT LIKE 'lowest-temperature-in-%'
            AND {title} NOT LIKE '%lowest temperature%'
            """,
            (),
        )
    return "", ()


def _decision_rows(
    conn: sqlite3.Connection,
    since_iso: str,
    city: str,
    *,
    market_scope: str = DEFAULT_MARKET_SCOPE,
) -> List[sqlite3.Row]:
    city_like = f"%{city.lower()}%"
    market_sql, market_params = _market_scope_sql(market_scope, alias="d")
    return conn.execute(
        f"""
        SELECT
          d.id,
          d.run_id,
          d.run_date,
          d.slug,
          d.event_date,
          d.title,
          d.outcome,
          d.side,
          d.will_order,
          d.reason_codes_json,
          d.details_json,
          d.created_at,
          l.label AS calibration_label
        FROM auto_trade_decisions d
        LEFT JOIN calibration_labels l ON l.decision_id = d.id
        WHERE d.action = 'entry'
          AND datetime(d.created_at) >= datetime(?)
          AND (? = '' OR lower(d.slug) LIKE ? OR lower(d.title) LIKE ?)
          {market_sql}
        ORDER BY d.id DESC
        """,
        (since_iso, city.lower(), city_like, city_like, *market_params),
    ).fetchall()


def _event_run_rows(
    conn: sqlite3.Connection,
    since_iso: str,
    city: str,
    *,
    market_scope: str = DEFAULT_MARKET_SCOPE,
) -> List[sqlite3.Row]:
    city_like = f"%{city.lower()}%"
    market_sql, market_params = _market_scope_sql(market_scope)
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(event_runs)").fetchall()}
    settlement_expr = "settlement_actual" if "settlement_actual" in columns else "NULL AS settlement_actual"
    closed_expr = "market_closed" if "market_closed" in columns else "NULL AS market_closed"
    end_expr = "event_end_utc" if "event_end_utc" in columns else "NULL AS event_end_utc"
    return conn.execute(
        f"""
        SELECT id, run_date, event_date, slug, title, payload_json, created_at,
               {settlement_expr}, {closed_expr}, {end_expr}
        FROM event_runs
        WHERE datetime(created_at) >= datetime(?)
          AND (? = '' OR lower(slug) LIKE ? OR lower(title) LIKE ?)
          {market_sql}
        ORDER BY id DESC
        """,
        (since_iso, city.lower(), city_like, city_like, *market_params),
    ).fetchall()


def _latest_observed_event_keys(rows: List[sqlite3.Row]) -> set[Tuple[str, str]]:
    latest: Dict[Tuple[str, str], sqlite3.Row] = {}
    for row in rows:
        key = (str(row["slug"] or ""), str(row["event_date"] or ""))
        if key not in latest:
            latest[key] = row
    out: set[Tuple[str, str]] = set()
    for key, row in latest.items():
        payload = _json_loads(row["payload_json"])
        if not isinstance(payload, dict):
            continue
        dist = payload.get("rows")
        if not isinstance(dist, list):
            dist = payload.get("distribution")
        if not isinstance(dist, list) or not dist:
            continue
        observed = 0
        parsed = 0
        for item in dist:
            if not isinstance(item, dict):
                continue
            parsed += 1
            source_kind = str(item.get("source_kind") or "").strip()
            trust_tier = str(
                item.get("observed_nowcast_source_trust_tier")
                or item.get("source_trust_tier")
                or ""
            ).strip()
            if source_kind == "wunderground_history" or trust_tier == "settlement_source":
                observed += 1
        if parsed > 0 and observed == parsed:
            out.add(key)
    return out


def _audit_decisions(
    rows: List[sqlite3.Row],
    *,
    observed_event_keys: Optional[set[Tuple[str, str]]] = None,
) -> Dict[str, Any]:
    raw_will_order_rows = 0
    raw_bayesian_will_order_rows = 0
    for raw_row in rows:
        if int(raw_row["will_order"] or 0) != 1:
            continue
        raw_will_order_rows += 1
        raw_details = _json_loads(raw_row["details_json"])
        if not isinstance(raw_details, dict):
            raw_details = {}
        if _truthy(raw_details.get("q_bayes_enabled")):
            raw_bayesian_will_order_rows += 1

    latest_by_key: Dict[Tuple[str, str, str, str], sqlite3.Row] = {}
    for row in rows:
        key = (
            str(row["slug"] or ""),
            str(row["event_date"] or ""),
            str(row["outcome"] or ""),
            str(row["side"] or ""),
        )
        if key not in latest_by_key:
            latest_by_key[key] = row
    current_rows = list(latest_by_key.values())
    total = len(current_rows)
    bayes_applicable_rows = 0
    bayes_not_applicable_observed_rows = 0
    bayes_rows = 0
    static_rows = 0
    posterior_rows = 0
    lcb_rows = 0
    will_order_rows = 0
    labeled_rows = 0
    shifts: List[float] = []
    lcb_gaps: List[float] = []
    by_slug: Dict[str, Counter] = defaultdict(Counter)
    by_source: Counter = Counter()
    by_prior_source: Counter = Counter()
    bias_correction_rows = 0
    examples: List[Dict[str, Any]] = []
    reliability_bins: Dict[str, Counter] = defaultdict(Counter)

    for row in current_rows:
        details = _json_loads(row["details_json"])
        if not isinstance(details, dict):
            details = {}
        enabled = _truthy(details.get("q_bayes_enabled"))
        source_kind = str(details.get("source_kind") or "").strip()
        trust_tier = str(details.get("observed_nowcast_source_trust_tier") or "").strip()
        observed_source = source_kind == "wunderground_history" or trust_tier == "settlement_source"
        if not enabled and not observed_source:
            event_key = (str(row["slug"] or ""), str(row["event_date"] or ""))
            observed_source = event_key in (observed_event_keys or set())
        bayes_applicable = enabled or not observed_source
        if bayes_applicable:
            bayes_applicable_rows += 1
        else:
            bayes_not_applicable_observed_rows += 1
        static_q = _to_float(details.get("q_raw_static"))
        posterior = _to_float(details.get("q_bayes_posterior"))
        lcb = _to_float(details.get("q_bayes_lcb"))
        source = str(details.get("q_bayes_source") or "").strip()
        prior_source = str(details.get("q_bayes_prior_source") or "").strip()
        if enabled:
            bayes_rows += 1
            if _truthy(details.get("q_bayes_bias_correction_enabled")):
                bias_correction_rows += 1
        if static_q is not None:
            static_rows += 1
        if posterior is not None:
            posterior_rows += 1
        if lcb is not None:
            lcb_rows += 1
        if int(row["will_order"] or 0) == 1:
            will_order_rows += 1
        if row["calibration_label"] is not None:
            labeled_rows += 1
        if source:
            by_source[source] += 1
        if prior_source:
            by_prior_source[prior_source] += 1
        slug = str(row["slug"] or "")
        by_slug[slug]["n"] += 1
        by_slug[slug]["bayes_applicable_n"] += 1 if bayes_applicable else 0
        by_slug[slug]["bayes_not_applicable_observed_n"] += 0 if bayes_applicable else 1
        by_slug[slug]["bayes_n"] += 1 if enabled else 0
        by_slug[slug]["will_order_n"] += 1 if int(row["will_order"] or 0) == 1 else 0
        if static_q is not None and posterior is not None:
            shifts.append(float(posterior) - float(static_q))
        if posterior is not None and lcb is not None:
            lcb_gaps.append(float(posterior) - float(lcb))
        if posterior is not None and row["calibration_label"] in (0, 1):
            labeled = int(row["calibration_label"])
            bin_idx = min(9, max(0, int(float(posterior) * 10.0)))
            key = f"{bin_idx/10:.1f}-{(bin_idx+1)/10:.1f}"
            reliability_bins[key]["n"] += 1
            reliability_bins[key]["wins"] += 1 if labeled == 1 else 0
        if enabled and len(examples) < 8:
            examples.append(
                {
                    "id": int(row["id"] or 0),
                    "slug": slug,
                    "event_date": str(row["event_date"] or ""),
                    "outcome": str(row["outcome"] or ""),
                    "side": str(row["side"] or ""),
                    "q_raw_static": static_q,
                    "q_bayes_posterior": posterior,
                    "q_bayes_lcb": lcb,
                    "q_bayes_source": source,
                    "q_bayes_prior_source": prior_source,
                    "q_bayes_historical_samples": _to_float(details.get("q_bayes_historical_samples")),
                    "q_bayes_bias_correction_enabled": _truthy(details.get("q_bayes_bias_correction_enabled")),
                    "will_order": int(row["will_order"] or 0),
                }
            )

    slug_payload = []
    for slug, ctr in sorted(by_slug.items(), key=lambda kv: (-kv[1]["n"], kv[0]))[:30]:
        n = int(ctr["n"])
        slug_payload.append(
            {
                "slug": slug,
                "n": n,
                "bayes_applicable_n": int(ctr["bayes_applicable_n"]),
                "bayes_not_applicable_observed_n": int(
                    ctr["bayes_not_applicable_observed_n"]
                ),
                "bayes_n": int(ctr["bayes_n"]),
                "bayes_coverage": _pct(
                    int(ctr["bayes_n"]), int(ctr["bayes_applicable_n"])
                ),
                "raw_bayes_coverage": _pct(int(ctr["bayes_n"]), n),
                "will_order_n": int(ctr["will_order_n"]),
            }
        )

    reliability_payload = []
    for key in sorted(reliability_bins.keys()):
        ctr = reliability_bins[key]
        n = int(ctr["n"])
        wins = int(ctr["wins"])
        reliability_payload.append(
            {
                "bin": key,
                "n": n,
                "wins": wins,
                "observed_rate": _pct(wins, n),
            }
        )

    return {
        "total": total,
        "raw_total": len(rows),
        "bayes_applicable_rows": bayes_applicable_rows,
        "bayes_not_applicable_observed_rows": bayes_not_applicable_observed_rows,
        "bayes_rows": bayes_rows,
        "bayes_coverage": _pct(bayes_rows, bayes_applicable_rows),
        "raw_bayes_coverage": _pct(bayes_rows, total),
        "static_rows": static_rows,
        "posterior_rows": posterior_rows,
        "lcb_rows": lcb_rows,
        "will_order_rows": will_order_rows,
        "raw_will_order_rows": raw_will_order_rows,
        "raw_bayesian_will_order_rows": raw_bayesian_will_order_rows,
        "labeled_rows": labeled_rows,
        "calibration_label_coverage": _pct(labeled_rows, total),
        "posterior_shift": {
            "n": len(shifts),
            "avg": (sum(shifts) / len(shifts)) if shifts else None,
            "p10": _q(shifts, 0.10),
            "p50": _q(shifts, 0.50),
            "p90": _q(shifts, 0.90),
            "max_abs": max((abs(v) for v in shifts), default=None),
        },
        "posterior_lcb_gap": {
            "n": len(lcb_gaps),
            "avg": (sum(lcb_gaps) / len(lcb_gaps)) if lcb_gaps else None,
            "p50": _q(lcb_gaps, 0.50),
            "p90": _q(lcb_gaps, 0.90),
        },
        "sources": dict(by_source.most_common()),
        "prior_sources": dict(by_prior_source.most_common()),
        "bias_correction_rows": bias_correction_rows,
        "by_slug": slug_payload,
        "examples": examples,
        "reliability_bins": reliability_payload,
    }


def _audit_event_runs(rows: List[sqlite3.Row]) -> Dict[str, Any]:
    total_runs = len(rows)
    total_buckets = 0
    bayes_buckets = 0
    static_buckets = 0
    posterior_buckets = 0
    latest_seen: set[Tuple[str, str]] = set()
    latest_total_buckets = 0
    latest_applicable_buckets = 0
    latest_not_applicable_observed_buckets = 0
    latest_bayes_buckets = 0
    latest_bias_correction_buckets = 0
    latest_historical_prior_buckets = 0
    prior_sources: Counter = Counter()
    latest_examples: List[Dict[str, Any]] = []

    for row in rows:
        payload = _json_loads(row["payload_json"])
        if not isinstance(payload, dict):
            continue
        dist = payload.get("rows")
        if not isinstance(dist, list):
            dist = payload.get("distribution")
        if not isinstance(dist, list):
            continue
        is_settled = row["settlement_actual"] is not None or int(row["market_closed"] or 0) == 1
        latest_key = (str(row["slug"] or ""), str(row["event_date"] or ""))
        is_latest_for_market = (not is_settled) and latest_key not in latest_seen
        if not is_settled:
            latest_seen.add(latest_key)
        run_bucket_n = 0
        run_bayes_n = 0
        run_applicable_n = 0
        run_observed_not_applicable_n = 0
        for item in dist:
            if not isinstance(item, dict):
                continue
            run_bucket_n += 1
            total_buckets += 1
            enabled = _truthy(item.get("q_bayes_enabled"))
            source_kind = str(item.get("source_kind") or "").strip()
            trust_tier = str(
                item.get("observed_nowcast_source_trust_tier")
                or item.get("source_trust_tier")
                or ""
            ).strip()
            observed_source = source_kind == "wunderground_history" or trust_tier == "settlement_source"
            bayes_applicable = enabled or not observed_source
            if bayes_applicable:
                run_applicable_n += 1
            else:
                run_observed_not_applicable_n += 1
            if enabled:
                bayes_buckets += 1
                run_bayes_n += 1
                prior_source = str(item.get("q_bayes_prior_source") or "").strip()
                if prior_source:
                    prior_sources[prior_source] += 1
                    if "history" in prior_source:
                        latest_historical_prior_buckets += 1 if is_latest_for_market else 0
                if _truthy(item.get("q_bayes_bias_correction_enabled")) and is_latest_for_market:
                    latest_bias_correction_buckets += 1
            if _to_float(item.get("q_raw_static")) is not None:
                static_buckets += 1
            if _to_float(item.get("q_bayes_posterior")) is not None:
                posterior_buckets += 1
        if is_latest_for_market:
            latest_total_buckets += run_bucket_n
            latest_applicable_buckets += run_applicable_n
            latest_not_applicable_observed_buckets += run_observed_not_applicable_n
            latest_bayes_buckets += run_bayes_n
        if len(latest_examples) < 10:
            latest_examples.append(
                {
                    "id": int(row["id"] or 0),
                    "slug": str(row["slug"] or ""),
                    "event_date": str(row["event_date"] or ""),
                    "bucket_n": run_bucket_n,
                    "bayes_applicable_bucket_n": run_applicable_n,
                    "bayes_not_applicable_observed_bucket_n": run_observed_not_applicable_n,
                    "bayes_bucket_n": run_bayes_n,
                    "bayes_coverage": _pct(run_bayes_n, run_applicable_n),
                    "raw_bayes_coverage": _pct(run_bayes_n, run_bucket_n),
                    "created_at": str(row["created_at"] or ""),
                }
            )

    return {
        "total_runs": total_runs,
        "total_buckets": total_buckets,
        "bayes_buckets": bayes_buckets,
        "bayes_bucket_coverage": _pct(latest_bayes_buckets, latest_applicable_buckets),
        "raw_bayes_bucket_coverage": _pct(bayes_buckets, total_buckets),
        "latest_total_buckets": latest_total_buckets,
        "latest_applicable_buckets": latest_applicable_buckets,
        "latest_not_applicable_observed_buckets": latest_not_applicable_observed_buckets,
        "latest_bayes_buckets": latest_bayes_buckets,
        "latest_historical_prior_buckets": latest_historical_prior_buckets,
        "latest_bias_correction_buckets": latest_bias_correction_buckets,
        "prior_sources": dict(prior_sources.most_common()),
        "static_buckets": static_buckets,
        "posterior_buckets": posterior_buckets,
        "latest_examples": latest_examples,
    }


def build_report(
    db_path: Path,
    *,
    since_days: int,
    city: str,
    market_scope: str = DEFAULT_MARKET_SCOPE,
) -> Dict[str, Any]:
    if not db_path.exists():
        return {
            "ok": False,
            "error": f"db_missing:{db_path}",
            "generated_at": _utc_now(),
        }
    since_iso = _cutoff_iso(since_days)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        decision_rows = _decision_rows(conn, since_iso, city, market_scope=market_scope)
        event_rows = _event_run_rows(conn, since_iso, city, market_scope=market_scope)
    finally:
        conn.close()

    observed_event_keys = _latest_observed_event_keys(event_rows)
    decisions = _audit_decisions(decision_rows, observed_event_keys=observed_event_keys)
    event_runs = _audit_event_runs(event_rows)
    label_plan = build_label_plan(
        db_path,
        since_days=max(1, int(since_days)),
        city=str(city or ""),
        market_scope=market_scope,
        bayes_only=True,
        allow_open_events=False,
        limit=0,
    )
    pending_labels = int(label_plan.get("planned_labels") or 0)
    label_plan_summary = {
        "mode": "dry_run",
        "bayes_only": True,
        "allow_open_events": False,
        "strict_resolution_source": bool(label_plan.get("strict_resolution_source", True)),
        "decisions_scanned": int(label_plan.get("decisions_scanned") or 0),
        "settlement_keys": int(label_plan.get("settlement_keys") or 0),
        "planned_labels": pending_labels,
        "label_counts": label_plan.get("label_counts") or {},
        "settlement_skipped": label_plan.get("settlement_skipped") or {},
        "skipped": label_plan.get("skipped") or {},
        "examples": (label_plan.get("labels") or [])[:8],
    }
    blockers: List[str] = []
    warnings: List[str] = []

    decision_cov = decisions.get("bayes_coverage")
    event_cov = event_runs.get("bayes_bucket_coverage")
    label_cov = decisions.get("calibration_label_coverage")
    if _coverage_status(event_cov, warn=0.50, ok=0.90) == "fail":
        blockers.append("event_run_bayes_coverage_low")
    elif _coverage_status(event_cov, warn=0.50, ok=0.90) == "warn":
        warnings.append("event_run_bayes_coverage_partial")
    if _coverage_status(decision_cov, warn=0.50, ok=0.90) == "fail":
        warnings.append("decision_bayes_coverage_low")
    elif _coverage_status(decision_cov, warn=0.50, ok=0.90) == "warn":
        warnings.append("decision_bayes_coverage_partial")
    if not label_cov or label_cov <= 0:
        warnings.append("calibration_labels_missing")
    if pending_labels > 0:
        warnings.append("calibration_labels_pending_apply")

    readiness = {
        "event_run_bayes_status": _coverage_status(event_cov, warn=0.50, ok=0.90),
        "decision_bayes_status": _coverage_status(decision_cov, warn=0.50, ok=0.90),
        "calibration_status": "ok" if label_cov and label_cov > 0 else "needs_labels",
        "pending_label_status": "pending_apply" if pending_labels > 0 else "clear",
        "paper_trading_status": (
            "has_order_intents"
            if int(
                decisions.get("raw_bayesian_will_order_rows")
                or decisions.get("raw_will_order_rows")
                or decisions.get("will_order_rows")
                or 0
            )
            > 0
            else "needs_signal"
        ),
    }

    return {
        "ok": not blockers,
        "generated_at": _utc_now(),
        "db": str(db_path),
        "since_days": max(1, int(since_days)),
        "city_filter": str(city or ""),
        "market_scope": str(market_scope or ""),
        "readiness": readiness,
        "blockers": blockers,
        "warnings": warnings,
        "event_runs": event_runs,
        "decisions": decisions,
        "settlement_label_plan": label_plan_summary,
        "next_actions": [
            "继续收集带 q_bayes_* 的 dry-run / paper-trading 决策样本",
            "若 settlement_label_plan.planned_labels > 0，先审阅样本，再显式运行 build_settlement_calibration_labels.py --apply --confirm-apply APPLY_SETTLEMENT_LABELS",
            "结算后生成 calibration_labels，再启用 reliability bin 胜率校验",
            "若 settlement_label_plan.settlement_skipped 出现 source/station 类原因，先修复 resolution source / station 映射",
            "用 posterior_shift 与 posterior_lcb_gap 调整 Bayes prior/model evidence 参数",
        ],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Read-only Bayesian readiness and calibration audit.")
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--since-days", type=int, default=14)
    ap.add_argument("--city", default="", help="Optional slug/title city filter, e.g. chengdu")
    ap.add_argument("--market-scope", default=DEFAULT_MARKET_SCOPE, help="highest_temperature|all")
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--json", action="store_true", help="Print compact JSON only")
    args = ap.parse_args()

    report = build_report(
        Path(args.db),
        since_days=int(args.since_days),
        city=str(args.city or ""),
        market_scope=str(args.market_scope or DEFAULT_MARKET_SCOPE),
    )
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.json:
        print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
