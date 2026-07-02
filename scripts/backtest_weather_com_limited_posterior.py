#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "archive.db"


@dataclass
class SnapshotSample:
    event_date: str
    snapshot_id: int
    slug: str
    created_at: str
    event_end_utc: str
    event_peak_time_utc: str
    tz: str
    rounding_rule: str
    actual_c: float
    actual_label: str
    actual_bucket_kind: str
    labels: List[str]
    qm_probs: Dict[str, float]
    qm_expected_c: float
    weather_com_peak_c: Optional[float]
    dte_bucket: str


def _to_float(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _parse_dt(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        out = datetime.fromisoformat(text)
    except ValueError:
        return None
    if out.tzinfo is None:
        out = out.replace(tzinfo=timezone.utc)
    return out.astimezone(timezone.utc)


def _zone(name: str) -> timezone:
    if ZoneInfo is not None:
        try:
            return ZoneInfo(str(name or "UTC"))  # type: ignore[return-value]
        except Exception:
            pass
    return timezone.utc


def _date_range(start: str, end: str) -> Iterable[str]:
    current = date.fromisoformat(start)
    stop = date.fromisoformat(end)
    while current <= stop:
        yield current.isoformat()
        current += timedelta(days=1)


def _lookback_start(end_date: str, days: int, extra_history_days: int) -> str:
    end_dt = date.fromisoformat(end_date)
    return (end_dt - timedelta(days=max(0, int(days) - 1 + int(extra_history_days)))).isoformat()


def _label_parts(label: str) -> Tuple[str, float]:
    raw = str(label or "").strip().replace("–", "-")
    num = r"(-?\d+(?:\.\d+)?)"
    match = re.match(rf"^{num}\s*°?C\s+or\s+below$", raw, flags=re.I)
    if match:
        return "le", float(match.group(1))
    match = re.match(rf"^{num}\s*°?C\s+or\s+(higher|above|more)$", raw, flags=re.I)
    if match:
        return "ge", float(match.group(1))
    match = re.match(rf"^{num}\s*°?C$", raw, flags=re.I)
    if match:
        return "eq", float(match.group(1))
    return "unknown", float("nan")


def _bucket_rep(label: str) -> float:
    _kind, value = _label_parts(label)
    return float(value) if math.isfinite(value) else 0.0


def _actual_label(actual_c: float, labels: Sequence[str]) -> str:
    rounded = round(float(actual_c))
    for label in labels:
        kind, value = _label_parts(label)
        if kind == "le" and rounded <= value:
            return str(label)
        if kind == "ge" and rounded >= value:
            return str(label)
        if kind == "eq" and abs(rounded - value) <= 1e-9:
            return str(label)
    return min(labels, key=lambda label: abs(_bucket_rep(str(label)) - rounded)) if labels else ""


def _normalize_probs(probs: Mapping[str, Any], labels: Sequence[str]) -> Dict[str, float]:
    out: Dict[str, float] = {}
    total = 0.0
    for label in labels:
        val = _to_float(probs.get(label))
        safe = max(0.0, float(val or 0.0))
        out[str(label)] = safe
        total += safe
    if total <= 0:
        denom = float(max(1, len(labels)))
        return {str(label): 1.0 / denom for label in labels}
    return {label: val / total for label, val in out.items()}


def _expected_c(probs: Mapping[str, float], labels: Sequence[str]) -> float:
    return sum(float(probs.get(label, 0.0) or 0.0) * _bucket_rep(str(label)) for label in labels)


def _top_label(probs: Mapping[str, float], labels: Sequence[str]) -> str:
    if not labels:
        return ""
    return max(labels, key=lambda label: float(probs.get(label, 0.0) or 0.0))


def _score(probs: Mapping[str, float], actual_label: str, labels: Sequence[str]) -> Dict[str, float]:
    if not labels or not actual_label:
        return {"hit": float("nan"), "brier": float("nan"), "rps": float("nan"), "actual_prob": float("nan")}
    brier = 0.0
    for label in labels:
        target = 1.0 if label == actual_label else 0.0
        brier += (float(probs.get(label, 0.0) or 0.0) - target) ** 2
    brier /= float(len(labels))
    try:
        actual_idx = list(labels).index(actual_label)
    except ValueError:
        actual_idx = 0
    cumulative = 0.0
    rps = 0.0
    for idx, label in enumerate(labels[:-1]):
        cumulative += float(probs.get(label, 0.0) or 0.0)
        target = 1.0 if actual_idx <= idx else 0.0
        rps += (cumulative - target) ** 2
    rps /= float(max(1, len(labels) - 1))
    return {
        "hit": 1.0 if _top_label(probs, labels) == actual_label else 0.0,
        "brier": brier,
        "rps": rps,
        "actual_prob": float(probs.get(actual_label, 0.0) or 0.0),
    }


def _shift_probs(probs: Mapping[str, float], labels: Sequence[str], delta_c: float) -> Dict[str, float]:
    reps = [_bucket_rep(str(label)) for label in labels]
    out = {str(label): 0.0 for label in labels}
    if not labels:
        return out
    for label in labels:
        mass = float(probs.get(label, 0.0) or 0.0)
        target = _bucket_rep(str(label)) + float(delta_c)
        if target <= reps[0]:
            out[str(labels[0])] += mass
            continue
        if target >= reps[-1]:
            out[str(labels[-1])] += mass
            continue
        idx = 0
        while idx < len(reps) - 1 and reps[idx + 1] < target:
            idx += 1
        low = reps[idx]
        high = reps[idx + 1]
        weight = (target - low) / (high - low) if high > low else 0.0
        out[str(labels[idx])] += mass * (1.0 - weight)
        out[str(labels[idx + 1])] += mass * weight
    return _normalize_probs(out, labels)


def _normal_cdf(value: float, mu: float, sigma: float) -> float:
    if math.isinf(value):
        return 0.0 if value < 0 else 1.0
    return 0.5 * (1.0 + math.erf((float(value) - float(mu)) / (max(1e-9, float(sigma)) * math.sqrt(2.0))))


def _bucket_bounds(label: str) -> Optional[Tuple[float, float]]:
    kind, value = _label_parts(label)
    if kind == "le":
        return float("-inf"), value + 0.5
    if kind == "ge":
        return value - 0.5, float("inf")
    if kind == "eq":
        return value - 0.5, value + 0.5
    return None


def _peak_probs(peak_c: float, labels: Sequence[str], sigma_c: float) -> Dict[str, float]:
    raw: Dict[str, float] = {}
    for label in labels:
        bounds = _bucket_bounds(str(label))
        if bounds is None:
            raw[str(label)] = 0.0
            continue
        low, high = bounds
        raw[str(label)] = max(0.0, _normal_cdf(high, peak_c, sigma_c) - _normal_cdf(low, peak_c, sigma_c))
    return _normalize_probs(raw, labels)


def _limited_blend(
    base_probs: Mapping[str, float],
    aux_probs: Mapping[str, float],
    labels: Sequence[str],
    *,
    base_evidence: float,
    aux_evidence: float,
    pp_cap: float,
) -> Dict[str, float]:
    raw = _normalize_probs(
        {
            label: float(base_evidence) * float(base_probs.get(label, 0.0) or 0.0)
            + float(aux_evidence) * float(aux_probs.get(label, 0.0) or 0.0)
            for label in labels
        },
        labels,
    )
    capped = {
        label: min(
            max(float(raw.get(label, 0.0) or 0.0), float(base_probs.get(label, 0.0) or 0.0) - pp_cap),
            float(base_probs.get(label, 0.0) or 0.0) + pp_cap,
        )
        for label in labels
    }
    return _normalize_probs(capped, labels)


def _dte_bucket(created_at: str, event_date: str, tz_name: str) -> str:
    created = _parse_dt(created_at)
    if created is None:
        return "unknown"
    try:
        event_day = date.fromisoformat(str(event_date)[:10])
    except ValueError:
        return "unknown"
    local_day = created.astimezone(_zone(tz_name or "UTC")).date()
    delta = (event_day - local_day).days
    if delta <= 0:
        return "D0"
    if delta == 1:
        return "D1"
    return "D2+"


def _latest_actuals(conn: sqlite3.Connection, start: str, end: str, args: argparse.Namespace) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for event_date in _date_range(start, end):
        rows = conn.execute(
            """
            SELECT settlement_actual, created_at, id
            FROM event_runs
            WHERE event_date = ?
              AND market_var = ?
              AND market_unit = ?
              AND station_id = ?
              AND settlement_actual IS NOT NULL
            ORDER BY id DESC
            LIMIT 80
            """,
            (event_date, args.market_var, args.market_unit, args.station_id),
        ).fetchall()
        best: Optional[sqlite3.Row] = None
        for row in rows:
            if best is None:
                best = row
                continue
            best_created = _parse_dt(best["created_at"])
            row_created = _parse_dt(row["created_at"])
            if row_created is not None and (best_created is None or row_created > best_created):
                best = row
        if best is None:
            continue
        actual = _to_float(best["settlement_actual"])
        if actual is not None:
            out[event_date] = float(actual)
    return out


def _select_snapshot_ids(
    conn: sqlite3.Connection,
    start: str,
    end: str,
    args: argparse.Namespace,
) -> Tuple[Dict[str, sqlite3.Row], Dict[str, int]]:
    selected: Dict[str, sqlite3.Row] = {}
    skipped = {"post_event_end": 0, "missing_event_end": 0}
    for event_date in _date_range(start, end):
        rows = conn.execute(
            """
            SELECT id, event_date, slug, title, city, station_id, created_at,
                   event_end_utc, event_peak_time_utc, tz, rounding_rule,
                   market_closed, settlement_actual
            FROM event_runs
            WHERE event_date = ?
              AND market_var = ?
              AND market_unit = ?
              AND created_at IS NOT NULL
            ORDER BY id DESC
            LIMIT 3000
            """,
            (event_date, args.market_var, args.market_unit),
        ).fetchall()
        best: Optional[sqlite3.Row] = None
        for row in rows:
            station = str(row["station_id"] or "")
            city = str(row["city"] or "").strip().lower()
            if station != str(args.station_id) and city != str(args.city or "").strip().lower():
                continue
            created = _parse_dt(row["created_at"])
            event_end = _parse_dt(row["event_end_utc"])
            if event_end is None:
                skipped["missing_event_end"] += 1
                continue
            if created is None or created >= event_end:
                skipped["post_event_end"] += 1
                continue
            if best is None:
                best = row
                continue
            best_created = _parse_dt(best["created_at"])
            if best_created is None or created > best_created:
                best = row
        if best is not None:
            selected[event_date] = best
    return selected, skipped


def _load_payloads(conn: sqlite3.Connection, ids: Sequence[int]) -> Dict[int, Mapping[str, Any]]:
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT id, payload_json FROM event_runs WHERE id IN ({placeholders})",
        [int(value) for value in ids],
    ).fetchall()
    out: Dict[int, Mapping[str, Any]] = {}
    for row in rows:
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except json.JSONDecodeError:
            payload = {}
        out[int(row["id"])] = payload if isinstance(payload, Mapping) else {}
    return out


def _extract_weather_com_peak(payload: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]) -> Optional[float]:
    candidates: List[Any] = [payload.get("weather_com_forecast_peak_c")]
    peak = payload.get("weather_com_forecast_peak")
    if isinstance(peak, Mapping):
        candidates.extend([peak.get("value_c"), peak.get("peak_c"), peak.get("value")])
    features = payload.get("weather_likelihood_features")
    if isinstance(features, Mapping):
        candidates.append(features.get("weather_com_forecast_peak_c"))
    for row in rows:
        candidates.append(row.get("weather_com_forecast_peak_c"))
        candidates.append(row.get("twc_forecast_peak_c"))
    for value in candidates:
        parsed = _to_float(value)
        if parsed is not None:
            return float(parsed)
    return None


def _sample_from_row(row: sqlite3.Row, payload: Mapping[str, Any], actual_c: float) -> Optional[SnapshotSample]:
    dist_raw = payload.get("rows") or payload.get("distribution") or []
    rows = [item for item in dist_raw if isinstance(item, Mapping)]
    labels = [str(item.get("outcome") or "") for item in rows if item.get("outcome")]
    if not labels:
        return None
    probs: Dict[str, float] = {}
    for item in rows:
        label = str(item.get("outcome") or "")
        if not label:
            continue
        value = _to_float(item.get("q_ensemble_qm_bucket"))
        if value is None:
            return None
        probs[label] = float(value)
    probs = _normalize_probs(probs, labels)
    actual_label = _actual_label(actual_c, labels)
    actual_kind, _value = _label_parts(actual_label)
    event_date = str(row["event_date"] or "")[:10]
    tz_name = str(row["tz"] or payload.get("event_tz") or payload.get("tz") or "UTC")
    return SnapshotSample(
        event_date=event_date,
        snapshot_id=int(row["id"]),
        slug=str(row["slug"] or payload.get("slug") or ""),
        created_at=str(row["created_at"] or ""),
        event_end_utc=str(row["event_end_utc"] or ""),
        event_peak_time_utc=str(row["event_peak_time_utc"] or payload.get("event_peak_time_utc") or ""),
        tz=tz_name,
        rounding_rule=str(row["rounding_rule"] or payload.get("rounding_rule") or ""),
        actual_c=float(actual_c),
        actual_label=actual_label,
        actual_bucket_kind=actual_kind,
        labels=labels,
        qm_probs=probs,
        qm_expected_c=_expected_c(probs, labels),
        weather_com_peak_c=_extract_weather_com_peak(payload, rows),
        dte_bucket=_dte_bucket(str(row["created_at"] or ""), event_date, tz_name),
    )


def load_samples(args: argparse.Namespace) -> Tuple[List[SnapshotSample], Dict[str, Any]]:
    target_start = (date.fromisoformat(args.end_date) - timedelta(days=max(0, int(args.days) - 1))).isoformat()
    query_start = _lookback_start(args.end_date, int(args.days), int(args.residual_history_days) + 7)
    with sqlite3.connect(f"file:{args.db}?mode=ro", uri=True, timeout=30.0) as conn:
        conn.row_factory = sqlite3.Row
        actuals = _latest_actuals(conn, query_start, args.end_date, args)
        selected_rows, skipped = _select_snapshot_ids(conn, query_start, args.end_date, args)
        payloads = _load_payloads(conn, [int(row["id"]) for row in selected_rows.values()])
    samples: List[SnapshotSample] = []
    missing = {"missing_actual": 0, "missing_payload": 0, "missing_qm": 0}
    for event_date in sorted(selected_rows):
        actual = actuals.get(event_date)
        if actual is None:
            missing["missing_actual"] += 1
            continue
        row = selected_rows[event_date]
        payload = payloads.get(int(row["id"]))
        if not payload:
            missing["missing_payload"] += 1
            continue
        sample = _sample_from_row(row, payload, actual)
        if sample is None:
            missing["missing_qm"] += 1
            continue
        samples.append(sample)
    return samples, {
        "target_start": target_start,
        "query_start": query_start,
        "end_date": args.end_date,
        "selected_snapshot_days": len(selected_rows),
        "actual_days": len(actuals),
        "skipped": {**skipped, **missing},
        "db": str(args.db),
        "read_only": True,
        "selection": "latest snapshot with created_at < event_end_utc; payload loaded by selected id only",
    }


def _residual_prior(samples: Sequence[SnapshotSample], sample: SnapshotSample, args: argparse.Namespace) -> Tuple[float, int, float]:
    prior = [item for item in samples if item.event_date < sample.event_date]
    if args.residual_prior == "non_boundary":
        prior = [item for item in prior if item.actual_bucket_kind == "eq"]
    prior = prior[-int(args.residual_history_days) :]
    if not prior:
        return 0.0, 0, 0.0
    raw = sum(item.actual_c - item.qm_expected_c for item in prior) / float(len(prior))
    clipped = max(-float(args.residual_clamp_c), min(float(args.residual_clamp_c), raw))
    return clipped, len(prior), raw


def score_samples(samples: Sequence[SnapshotSample], args: argparse.Namespace) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    target_start = (date.fromisoformat(args.end_date) - timedelta(days=max(0, int(args.days) - 1))).isoformat()
    for sample in samples:
        if not (target_start <= sample.event_date <= args.end_date):
            continue
        residual, prior_n, residual_raw = _residual_prior(samples, sample, args)
        residual_probs = _shift_probs(sample.qm_probs, sample.labels, residual)
        if sample.weather_com_peak_c is not None:
            wc_probs = _peak_probs(sample.weather_com_peak_c, sample.labels, float(args.weather_sigma_c))
            weather_probs = _limited_blend(
                sample.qm_probs,
                wc_probs,
                sample.labels,
                base_evidence=float(args.base_evidence),
                aux_evidence=float(args.weather_com_evidence),
                pp_cap=float(args.weather_com_pp_cap),
            )
        else:
            weather_probs = dict(sample.qm_probs)
        row: Dict[str, Any] = {
            "event_date": sample.event_date,
            "snapshot_id": sample.snapshot_id,
            "created_at": sample.created_at,
            "dte_bucket": sample.dte_bucket,
            "actual_c": sample.actual_c,
            "actual_label": sample.actual_label,
            "actual_bucket_segment": "non_boundary" if sample.actual_bucket_kind == "eq" else "boundary",
            "qm_expected_c": sample.qm_expected_c,
            "residual_c": residual,
            "residual_raw_c": residual_raw,
            "residual_prior_n": prior_n,
            "weather_com_peak_c": sample.weather_com_peak_c,
        }
        for name, probs in (
            ("qm", sample.qm_probs),
            ("residual", residual_probs),
            ("weather_com_limited", weather_probs),
        ):
            scores = _score(probs, sample.actual_label, sample.labels)
            row[f"{name}_top_label"] = _top_label(probs, sample.labels)
            for key, value in scores.items():
                row[f"{name}_{key}"] = value
        out.append(row)
    return out


def _mean(values: Sequence[float]) -> Optional[float]:
    nums = [float(value) for value in values if math.isfinite(float(value))]
    return sum(nums) / len(nums) if nums else None


def _segment_summary(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    if not rows:
        return {"n": 0}
    out: Dict[str, Any] = {"n": len(rows)}
    for method in ("qm", "residual", "weather_com_limited"):
        out[method] = {
            "hit": _mean([float(row[f"{method}_hit"]) for row in rows]),
            "brier": _mean([float(row[f"{method}_brier"]) for row in rows]),
            "rps": _mean([float(row[f"{method}_rps"]) for row in rows]),
            "actual_prob": _mean([float(row[f"{method}_actual_prob"]) for row in rows]),
        }
    for method in ("residual", "weather_com_limited"):
        out[f"{method}_vs_qm"] = {
            "hit_delta": _mean([float(row[f"{method}_hit"]) - float(row["qm_hit"]) for row in rows]),
            "brier_delta": _mean([float(row[f"{method}_brier"]) - float(row["qm_brier"]) for row in rows]),
            "rps_delta": _mean([float(row[f"{method}_rps"]) - float(row["qm_rps"]) for row in rows]),
        }
    out["weather_com_visible_n"] = sum(1 for row in rows if row.get("weather_com_peak_c") is not None)
    return out


def build_report(rows: Sequence[Mapping[str, Any]], load_meta: Mapping[str, Any], args: argparse.Namespace) -> Dict[str, Any]:
    segments: Dict[str, List[Mapping[str, Any]]] = {"overall": list(rows)}
    for dte in ("D0", "D1", "D2+"):
        segments[f"dte:{dte}"] = [row for row in rows if str(row.get("dte_bucket")) == dte]
    for bucket_segment in ("boundary", "non_boundary"):
        segments[f"bucket:{bucket_segment}"] = [
            row for row in rows if str(row.get("actual_bucket_segment")) == bucket_segment
        ]
    summaries = {name: _segment_summary(items) for name, items in segments.items()}
    min_n = int(args.gate_min_segment_n)
    checked = {
        name: summary
        for name, summary in summaries.items()
        if int(summary.get("n") or 0) >= min_n and name != "dte:D2+"
    }
    failures = []
    for name, summary in checked.items():
        delta = summary.get("weather_com_limited_vs_qm") if isinstance(summary, Mapping) else {}
        brier_delta = _to_float(delta.get("brier_delta") if isinstance(delta, Mapping) else None)
        rps_delta = _to_float(delta.get("rps_delta") if isinstance(delta, Mapping) else None)
        if brier_delta is None or rps_delta is None or brier_delta > float(args.gate_max_score_degradation) or rps_delta > float(args.gate_max_score_degradation):
            failures.append({"segment": name, "brier_delta": brier_delta, "rps_delta": rps_delta})
    shadow_ready = bool(checked) and not failures
    return {
        "ok": True,
        "method": {
            "current": "q_ensemble_qm_bucket",
            "residual": {
                "mode": "walk_forward_expected_temperature_shift",
                "prior": args.residual_prior,
                "history_days": int(args.residual_history_days),
                "clamp_c": float(args.residual_clamp_c),
            },
            "weather_com_limited": {
                "source": "weather_com_forecast_peak_c visible in selected snapshot payload",
                "sigma_c": float(args.weather_sigma_c),
                "base_evidence": float(args.base_evidence),
                "weather_com_evidence": float(args.weather_com_evidence),
                "pp_cap": float(args.weather_com_pp_cap),
            },
            "scoring": "single settlement outcome buckets; Brier is multiclass mean squared error; RPS is ordinal cumulative score",
        },
        "load": dict(load_meta),
        "summary": summaries,
        "daily": list(rows),
        "gate": {
            "dry_run_shadow_ready": shadow_ready,
            "checked_segments": sorted(checked.keys()),
            "min_segment_n": min_n,
            "max_score_degradation": float(args.gate_max_score_degradation),
            "failures": failures,
            "next_action": (
                "enter_dry_run_shadow_parameter_review"
                if shadow_ready
                else "keep_read_only_collect_more_samples_or_tune_shadow_grid"
            ),
        },
    }


def _fmt_pct(value: Any) -> str:
    parsed = _to_float(value)
    return "—" if parsed is None else f"{parsed * 100.0:.1f}%"


def _fmt_score(value: Any) -> str:
    parsed = _to_float(value)
    return "—" if parsed is None else f"{parsed:.5f}"


def print_text_report(report: Mapping[str, Any]) -> None:
    load = report.get("load") if isinstance(report.get("load"), Mapping) else {}
    print("READ_ONLY weather_com limited posterior backtest")
    print(
        f"range={load.get('target_start')}..{load.get('end_date')} "
        f"selected_days={load.get('selected_snapshot_days')} read_only={load.get('read_only')}"
    )
    summary = report.get("summary") if isinstance(report.get("summary"), Mapping) else {}
    for name in ("overall", "dte:D0", "dte:D1", "bucket:boundary", "bucket:non_boundary"):
        item = summary.get(name) if isinstance(summary.get(name), Mapping) else {}
        if not item or int(item.get("n") or 0) <= 0:
            continue
        print(f"\n[{name}] n={item.get('n')} weather_com_visible={item.get('weather_com_visible_n')}")
        for method in ("qm", "residual", "weather_com_limited"):
            vals = item.get(method) if isinstance(item.get(method), Mapping) else {}
            print(
                f"{method:22s} hit={_fmt_pct(vals.get('hit'))} "
                f"brier={_fmt_score(vals.get('brier'))} rps={_fmt_score(vals.get('rps'))} "
                f"actual_prob={_fmt_pct(vals.get('actual_prob'))}"
            )
        for method in ("residual", "weather_com_limited"):
            delta = item.get(f"{method}_vs_qm") if isinstance(item.get(f"{method}_vs_qm"), Mapping) else {}
            print(
                f"delta {method:16s} hit={_fmt_pct(delta.get('hit_delta'))} "
                f"brier={_fmt_score(delta.get('brier_delta'))} rps={_fmt_score(delta.get('rps_delta'))}"
            )
    gate = report.get("gate") if isinstance(report.get("gate"), Mapping) else {}
    print("\nGATE")
    print(json.dumps(gate, ensure_ascii=False, sort_keys=True))
    print("\nDAILY")
    for row in report.get("daily") or []:
        if not isinstance(row, Mapping):
            continue
        print(
            f"{row.get('event_date')} {row.get('dte_bucket')} {row.get('actual_bucket_segment')} "
            f"actual={row.get('actual_c'):.2f}->{row.get('actual_label')} "
            f"qm={row.get('qm_top_label')} B={row.get('qm_brier'):.4f} R={row.get('qm_rps'):.4f} "
            f"res={row.get('residual_top_label')} shift={row.get('residual_c'):+.2f} "
            f"B={row.get('residual_brier'):.4f} R={row.get('residual_rps'):.4f} "
            f"wc={row.get('weather_com_peak_c')} top={row.get('weather_com_limited_top_label')} "
            f"B={row.get('weather_com_limited_brier'):.4f} R={row.get('weather_com_limited_rps'):.4f}"
        )


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only backtest for weather_com limited posterior vs ensemble_qm.")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--end-date", default=date.today().isoformat())
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--city", default="chengdu")
    parser.add_argument("--station-id", default="ZUUU")
    parser.add_argument("--market-var", default="temperature_2m_max")
    parser.add_argument("--market-unit", default="C")
    parser.add_argument("--residual-history-days", type=int, default=7)
    parser.add_argument("--residual-clamp-c", type=float, default=1.5)
    parser.add_argument("--residual-prior", choices=["all", "non_boundary"], default="non_boundary")
    parser.add_argument("--weather-sigma-c", type=float, default=0.8)
    parser.add_argument("--base-evidence", type=float, default=36.0)
    parser.add_argument("--weather-com-evidence", type=float, default=12.0)
    parser.add_argument("--weather-com-pp-cap", type=float, default=0.10)
    parser.add_argument("--gate-min-segment-n", type=int, default=3)
    parser.add_argument("--gate-max-score-degradation", type=float, default=0.0)
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    samples, load_meta = load_samples(args)
    rows = score_samples(samples, args)
    report = build_report(rows, load_meta, args)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print_text_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
