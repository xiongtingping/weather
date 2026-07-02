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
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.bayesian_readiness_audit import DEFAULT_MARKET_SCOPE, _market_scope_sql


DEFAULT_DB = ROOT / "data" / "archive.db"
DEFAULT_OUT = ROOT / "data" / "reports" / "bayesian_reliability_report_latest.json"
DEFAULT_SVG = ROOT / "data" / "reports" / "bayesian_reliability_report_latest.svg"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _cutoff_iso(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=max(1, int(days)))).isoformat(timespec="seconds")


def _to_float(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except Exception:
        return None
    return out if math.isfinite(out) else None


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


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


def _pct(num: int, den: int) -> Optional[float]:
    if den <= 0:
        return None
    return float(num) / float(den)


def _logloss(p: float, y: int) -> float:
    p2 = max(1e-6, min(1.0 - 1e-6, float(p)))
    return -(float(y) * math.log(p2) + (1.0 - float(y)) * math.log(1.0 - p2))


def _connect_readonly(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise FileNotFoundError(f"db_missing:{db_path}")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table,),
    ).fetchone()
    return bool(row)


def _empty_report(
    db_path: Path,
    *,
    since_days: int,
    city: str,
    market_scope: str,
    blockers: Optional[List[str]] = None,
    warnings: Optional[List[str]] = None,
    bins: int = 10,
    min_samples: int = 30,
) -> Dict[str, Any]:
    metrics = _metrics([], [])
    return {
        "ok": False,
        "read_only": True,
        "generated_at": _utc_now(),
        "db": str(db_path),
        "since_days": max(1, int(since_days)),
        "city_filter": str(city or ""),
        "market_scope": str(market_scope or ""),
        "blockers": blockers or ["no_labeled_bayesian_samples"],
        "warnings": warnings or [],
        "metrics": metrics,
        "bins": _bins_for_samples([], bins),
        "segments": {
            "prior_source": {},
            "side": {},
            "bias_correction_enabled": {},
        },
        "calibration_contract": _calibration_contract([], bins, min_samples=max(1, int(min_samples))),
        "label_counts": {},
        "recommendations": _recommendations(metrics, min_samples=max(1, int(min_samples))),
    }


def _fetch_samples(
    conn: sqlite3.Connection,
    *,
    since_iso: str,
    city: str,
    market_scope: str,
) -> List[Dict[str, Any]]:
    city_like = f"%{city.lower()}%"
    market_sql, market_params = _market_scope_sql(market_scope, alias="d")
    rows = conn.execute(
        f"""
        SELECT
          d.id AS decision_id,
          d.slug,
          d.event_date,
          d.title,
          d.outcome,
          d.side,
          d.details_json,
          d.created_at,
          l.label
        FROM calibration_labels l
        JOIN auto_trade_decisions d ON d.id = l.decision_id
        WHERE d.action = 'entry'
          AND datetime(d.created_at) >= datetime(?)
          AND (? = '' OR lower(d.slug) LIKE ? OR lower(d.title) LIKE ?)
          {market_sql}
        ORDER BY d.id DESC
        """,
        (since_iso, city.lower(), city_like, city_like, *market_params),
    ).fetchall()
    samples: List[Dict[str, Any]] = []
    for row in rows:
        details = _json_obj(row["details_json"])
        if not _truthy(details.get("q_bayes_enabled")):
            continue
        p = _to_float(details.get("q_bayes_posterior"))
        if p is None:
            continue
        label = row["label"]
        try:
            y = int(label)
        except Exception:
            continue
        if y not in {0, 1}:
            continue
        samples.append(
            {
                "decision_id": int(row["decision_id"] or 0),
                "slug": str(row["slug"] or ""),
                "event_date": str(row["event_date"] or ""),
                "outcome": str(row["outcome"] or ""),
                "side": str(row["side"] or "").upper(),
                "p": max(0.0, min(1.0, float(p))),
                "label": y,
                "prior_source": str(details.get("q_bayes_prior_source") or ""),
                "bias_correction_enabled": _truthy(details.get("q_bayes_bias_correction_enabled")),
                "created_at": str(row["created_at"] or ""),
            }
        )
    return samples


def _bins_for_samples(samples: Iterable[Mapping[str, Any]], bins: int) -> List[Dict[str, Any]]:
    counters: Dict[int, Dict[str, float]] = defaultdict(lambda: {"n": 0.0, "wins": 0.0, "p_sum": 0.0, "brier_sum": 0.0})
    bins_count = max(1, int(bins))
    for sample in samples:
        p = _to_float(sample.get("p"))
        if p is None:
            continue
        y = int(sample.get("label") or 0)
        idx = min(bins_count - 1, max(0, int(float(p) * bins_count)))
        bucket = counters[idx]
        bucket["n"] += 1.0
        bucket["wins"] += float(y)
        bucket["p_sum"] += float(p)
        bucket["brier_sum"] += (float(p) - float(y)) ** 2
    out: List[Dict[str, Any]] = []
    for idx in range(bins_count):
        item = counters.get(idx, {"n": 0.0, "wins": 0.0, "p_sum": 0.0, "brier_sum": 0.0})
        n = int(item["n"])
        p_avg = item["p_sum"] / n if n else None
        obs = item["wins"] / n if n else None
        out.append(
            {
                "bin": f"{idx / bins_count:.1f}-{(idx + 1) / bins_count:.1f}",
                "n": n,
                "predicted_rate": p_avg,
                "observed_rate": obs,
                "wins": int(item["wins"]),
                "brier": (item["brier_sum"] / n) if n else None,
                "abs_gap": abs(float(p_avg) - float(obs)) if p_avg is not None and obs is not None else None,
            }
        )
    return out


def _metrics(samples: List[Mapping[str, Any]], bins: List[Mapping[str, Any]]) -> Dict[str, Any]:
    n = len(samples)
    if n <= 0:
        return {"n": 0, "brier": None, "logloss": None, "ece": None, "mce": None, "avg_predicted": None, "observed_rate": None}
    brier = sum((float(s["p"]) - float(s["label"])) ** 2 for s in samples) / n
    logloss = sum(_logloss(float(s["p"]), int(s["label"])) for s in samples) / n
    avg_pred = sum(float(s["p"]) for s in samples) / n
    obs = sum(int(s["label"]) for s in samples) / n
    ece = 0.0
    mce = 0.0
    for item in bins:
        bin_n = int(item.get("n") or 0)
        gap = _to_float(item.get("abs_gap"))
        if bin_n <= 0 or gap is None:
            continue
        ece += (bin_n / n) * gap
        mce = max(mce, gap)
    return {
        "n": n,
        "brier": brier,
        "logloss": logloss,
        "ece": ece,
        "mce": mce,
        "avg_predicted": avg_pred,
        "observed_rate": obs,
    }


def _segment(samples: List[Dict[str, Any]], key: str, bins_count: int) -> Dict[str, Any]:
    groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for sample in samples:
        raw = sample.get(key)
        label = str(raw if raw not in (None, "") else "na")
        groups[label].append(sample)
    out = {}
    for label, group in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        bins = _bins_for_samples(group, bins_count)
        out[label] = {"metrics": _metrics(group, bins), "bins": bins}
    return out


def _is_calibration_contract_sample(sample: Mapping[str, Any]) -> bool:
    prior_source = str(sample.get("prior_source") or "").strip().lower()
    return "history" in prior_source and bool(sample.get("bias_correction_enabled"))


def _calibration_contract(samples: List[Dict[str, Any]], bins_count: int, *, min_samples: int) -> Dict[str, Any]:
    contract_samples = [sample for sample in samples if _is_calibration_contract_sample(sample)]
    bins = _bins_for_samples(contract_samples, bins_count)
    metrics = _metrics(contract_samples, bins)
    sample_n = int(metrics.get("n") or 0)
    return {
        "description": "Samples whose Bayesian posterior used historical prior and model-bias correction.",
        "requires_historical_prior": True,
        "requires_bias_correction": True,
        "min_samples": max(1, int(min_samples)),
        "sample_count": sample_n,
        "sample_share": _pct(sample_n, len(samples)),
        "ok": sample_n >= max(1, int(min_samples)),
        "metrics": metrics,
        "bins": bins,
    }


def _recommendations(metrics: Mapping[str, Any], *, min_samples: int) -> List[str]:
    n = int(metrics.get("n") or 0)
    ece = _to_float(metrics.get("ece"))
    brier = _to_float(metrics.get("brier"))
    recs: List[str] = []
    if n < max(1, int(min_samples)):
        recs.append("collect_more_settled_bayesian_labels")
    if ece is not None and ece > 0.08:
        recs.append("review_bayesian_prior_and_model_evidence_strength")
    if brier is not None and brier > 0.24:
        recs.append("review_probability_calibration_before_live_cutover")
    if not recs:
        recs.append("continue_paper_trading_and_weekly_recalibration")
    return recs


def build_report(
    db_path: Path = DEFAULT_DB,
    *,
    since_days: int = 30,
    city: str = "",
    market_scope: str = DEFAULT_MARKET_SCOPE,
    bins: int = 10,
    min_samples: int = 30,
) -> Dict[str, Any]:
    if not db_path.exists():
        return {"ok": False, "error": f"db_missing:{db_path}", "generated_at": _utc_now()}
    conn = _connect_readonly(db_path)
    try:
        missing_tables = [
            table
            for table in ("calibration_labels", "auto_trade_decisions")
            if not _table_exists(conn, table)
        ]
        if missing_tables:
            return _empty_report(
                db_path,
                since_days=since_days,
                city=city,
                market_scope=market_scope,
                blockers=["no_labeled_bayesian_samples"],
                warnings=[f"reliability_table_missing:{table}" for table in missing_tables],
                bins=bins,
                min_samples=min_samples,
            )
        samples = _fetch_samples(
            conn,
            since_iso=_cutoff_iso(since_days),
            city=str(city or ""),
            market_scope=str(market_scope or DEFAULT_MARKET_SCOPE),
        )
    finally:
        conn.close()
    bins_payload = _bins_for_samples(samples, bins)
    metrics = _metrics(samples, bins_payload)
    calibration_contract = _calibration_contract(samples, bins, min_samples=max(1, int(min_samples)))
    blockers: List[str] = []
    warnings: List[str] = []
    if int(metrics["n"] or 0) <= 0:
        blockers.append("no_labeled_bayesian_samples")
    elif int(metrics["n"] or 0) < max(1, int(min_samples)):
        warnings.append("low_labeled_bayesian_sample_count")
    if int((calibration_contract.get("metrics") or {}).get("n") or 0) < max(1, int(min_samples)):
        warnings.append("low_calibrated_bayesian_sample_count")
    if _to_float(metrics.get("ece")) is not None and float(metrics["ece"]) > 0.08:
        warnings.append("bayesian_reliability_ece_high")
    return {
        "ok": not blockers,
        "read_only": True,
        "generated_at": _utc_now(),
        "db": str(db_path),
        "since_days": max(1, int(since_days)),
        "city_filter": str(city or ""),
        "market_scope": str(market_scope or ""),
        "blockers": blockers,
        "warnings": warnings,
        "metrics": metrics,
        "bins": bins_payload,
        "segments": {
            "prior_source": _segment(samples, "prior_source", bins),
            "side": _segment(samples, "side", bins),
            "bias_correction_enabled": _segment(samples, "bias_correction_enabled", bins),
        },
        "calibration_contract": calibration_contract,
        "label_counts": dict(Counter(int(s["label"]) for s in samples)),
        "recommendations": _recommendations(metrics, min_samples=max(1, int(min_samples))),
    }


def build_svg(report: Mapping[str, Any]) -> str:
    width = 640
    height = 440
    pad = 56
    plot = 320
    bins = report.get("bins") if isinstance(report.get("bins"), list) else []

    def x(v: float) -> float:
        return pad + max(0.0, min(1.0, v)) * plot

    def y(v: float) -> float:
        return pad + (1.0 - max(0.0, min(1.0, v))) * plot

    points = []
    for item in bins:
        if not isinstance(item, Mapping) or int(item.get("n") or 0) <= 0:
            continue
        pred = _to_float(item.get("predicted_rate"))
        obs = _to_float(item.get("observed_rate"))
        if pred is None or obs is None:
            continue
        radius = min(14.0, 4.0 + math.sqrt(int(item.get("n") or 0)))
        points.append(
            f'<circle cx="{x(pred):.1f}" cy="{y(obs):.1f}" r="{radius:.1f}" fill="#2563eb" opacity="0.78">'
            f"<title>{item.get('bin')} n={item.get('n')} pred={pred:.3f} obs={obs:.3f}</title></circle>"
        )
    metrics = report.get("metrics") if isinstance(report.get("metrics"), Mapping) else {}
    title = (
        f"Bayes reliability n={metrics.get('n') or 0} "
        f"Brier={_to_float(metrics.get('brier')) if _to_float(metrics.get('brier')) is not None else 'na'} "
        f"ECE={_to_float(metrics.get('ece')) if _to_float(metrics.get('ece')) is not None else 'na'}"
    )
    return "\n".join(
        [
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
            '<rect width="100%" height="100%" fill="#ffffff"/>',
            f'<text x="{pad}" y="30" font-family="Arial, sans-serif" font-size="16" fill="#111827">{title}</text>',
            f'<line x1="{pad}" y1="{pad + plot}" x2="{pad + plot}" y2="{pad + plot}" stroke="#111827"/>',
            f'<line x1="{pad}" y1="{pad}" x2="{pad}" y2="{pad + plot}" stroke="#111827"/>',
            f'<line x1="{pad}" y1="{pad + plot}" x2="{pad + plot}" y2="{pad}" stroke="#9ca3af" stroke-dasharray="6 5"/>',
            f'<text x="{pad + plot / 2 - 60}" y="{pad + plot + 42}" font-family="Arial, sans-serif" font-size="13" fill="#374151">Predicted probability</text>',
            f'<text x="12" y="{pad + plot / 2}" transform="rotate(-90 12 {pad + plot / 2})" font-family="Arial, sans-serif" font-size="13" fill="#374151">Observed rate</text>',
            *points,
            "</svg>",
        ]
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only Bayesian reliability report.")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--since-days", type=int, default=30)
    parser.add_argument("--city", default="")
    parser.add_argument("--market-scope", default=DEFAULT_MARKET_SCOPE, help="highest_temperature|all")
    parser.add_argument("--bins", type=int, default=10)
    parser.add_argument("--min-samples", type=int, default=30)
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--svg-out", default=str(DEFAULT_SVG))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    report = build_report(
        Path(args.db),
        since_days=int(args.since_days),
        city=str(args.city or ""),
        market_scope=str(args.market_scope or DEFAULT_MARKET_SCOPE),
        bins=max(1, int(args.bins)),
        min_samples=max(1, int(args.min_samples)),
    )
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.svg_out:
        svg_path = Path(args.svg_out)
        svg_path.parent.mkdir(parents=True, exist_ok=True)
        svg_path.write_text(build_svg(report), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, separators=(",", ":") if args.json else None, indent=None if args.json else 2))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
