#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
DEFAULT_DB = ROOT / "data" / "archive.db"


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _safe_json(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(str(raw or ""))
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _to_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _to_int(value: Any) -> int | None:
    parsed = _to_float(value)
    if parsed is None:
        return None
    return int(parsed)


def _prob01(value: Any) -> float | None:
    parsed = _to_float(value)
    if parsed is None:
        return None
    if 1.0 < parsed <= 100.0:
        parsed /= 100.0
    if parsed < 0.0 or parsed > 1.0:
        return None
    return float(parsed)


def _label_from_row(row: Mapping[str, Any]) -> str:
    return str(row.get("bucket") or row.get("outcome") or row.get("label") or "").strip()


def _prob_from_row(row: Mapping[str, Any]) -> float | None:
    for key in (
        "probability",
        "posterior",
        "q_temperature_bucket_posterior",
        "q_bayes_posterior",
    ):
        parsed = _prob01(row.get(key))
        if parsed is not None:
            return parsed
    return None


def _first_distribution_rows(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    for value in (
        payload.get("distribution"),
        payload.get("posterior_temperature_distribution"),
        _as_mapping(payload.get("posterior_ssot_fact")).get("bucket_probabilities"),
    ):
        if isinstance(value, list) and value:
            return [row for row in value if isinstance(row, Mapping)]
    return []


def _ordered_bucket_rows(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index, row in enumerate(_first_distribution_rows(payload)):
        label = _label_from_row(row)
        probability = _prob_from_row(row)
        if not label or probability is None:
            continue
        rows.append(
            {
                "bucket": label,
                "order_index": index,
                "posterior": float(probability),
                "prior": _prob01(row.get("prior")),
                "forecast_weak_likelihood": _prob01(row.get("forecast_weak_likelihood")),
                "metar_strong_likelihood": _prob01(row.get("metar_strong_likelihood")),
                "hard_mask": _to_float(row.get("hard_mask")),
            }
        )
    return rows


def _distribution_from_layer(payload: Mapping[str, Any], layer: str) -> dict[str, float]:
    layers = _as_mapping(payload.get("layer_contributions"))
    raw = _as_mapping(_as_mapping(layers.get(layer)).get("distribution"))
    out: dict[str, float] = {}
    for label, value in raw.items():
        parsed = _prob01(value)
        if str(label or "").strip() and parsed is not None:
            out[str(label).strip()] = float(parsed)
    return out


def _prior_map(payload: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]) -> dict[str, float]:
    raw = _as_mapping(payload.get("prior_distribution")) or _distribution_from_layer(payload, "prior")
    out: dict[str, float] = {}
    for label, value in raw.items():
        parsed = _prob01(value)
        if str(label or "").strip() and parsed is not None:
            out[str(label).strip()] = float(parsed)
    for row in rows:
        label = str(row.get("bucket") or "").strip()
        parsed = _prob01(row.get("prior"))
        if label and parsed is not None:
            out.setdefault(label, float(parsed))
    return out


def _likelihood_map(
    payload: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
    *,
    layer: str,
    row_key: str,
) -> dict[str, float]:
    out = _distribution_from_layer(payload, layer)
    for row in rows:
        label = str(row.get("bucket") or "").strip()
        parsed = _prob01(row.get(row_key))
        if label and parsed is not None:
            out.setdefault(label, float(parsed))
    return out


def _hard_mask_map(rows: Sequence[Mapping[str, Any]]) -> dict[str, float]:
    out: dict[str, float] = {}
    for row in rows:
        label = str(row.get("bucket") or "").strip()
        if not label:
            continue
        parsed = _to_float(row.get("hard_mask"))
        out[label] = float(parsed) if parsed is not None else 1.0
    return out


def _beta(payload: Mapping[str, Any], layer: str, fallback: float) -> float:
    layers = _as_mapping(payload.get("layer_contributions"))
    parsed = _to_float(_as_mapping(layers.get(layer)).get("beta"))
    if parsed is not None:
        return float(parsed)
    ssot = _as_mapping(payload.get("posterior_ssot_fact"))
    key = "beta_weak" if layer == "forecast_weak_likelihood" else "beta_strong"
    parsed = _to_float(ssot.get(key))
    return float(parsed) if parsed is not None else float(fallback)


def _ranked(rows: Sequence[Mapping[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    ranked = sorted(
        rows,
        key=lambda row: (-float(row.get("posterior") or 0.0), int(row.get("order_index") or 0)),
    )
    return [dict(row) for row in ranked[: max(0, int(limit))]]


def _top_continuity(rows: Sequence[Mapping[str, Any]], *, top_n: int) -> dict[str, Any]:
    top = _ranked(rows, limit=top_n)
    indices = sorted(int(row.get("order_index") or 0) for row in top)
    if len(indices) < top_n:
        return {
            "state": "not_applicable",
            "top_n": int(top_n),
            "top_buckets": [row.get("bucket") for row in top],
            "missing_buckets_between_top_range": [],
        }
    low, high = indices[0], indices[-1]
    top_index_set = set(indices)
    by_index = {int(row.get("order_index") or 0): row for row in rows}
    missing_indices = [idx for idx in range(low, high + 1) if idx not in top_index_set]
    missing = [
        {
            "bucket": by_index[idx].get("bucket"),
            "order_index": idx,
            "posterior": by_index[idx].get("posterior"),
        }
        for idx in missing_indices
        if idx in by_index
    ]
    return {
        "state": "contiguous" if not missing else "noncontiguous",
        "top_n": int(top_n),
        "top_buckets": [row.get("bucket") for row in top],
        "top_order_indices": indices,
        "missing_buckets_between_top_range": missing,
    }


def _prior_spike_evidence(
    *,
    top_rows: Sequence[Mapping[str, Any]],
    missing_rows: Sequence[Mapping[str, Any]],
    prior: Mapping[str, float],
    spike_ratio_threshold: float,
) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    for top in top_rows:
        top_label = str(top.get("bucket") or "").strip()
        top_prior = float(prior.get(top_label, 0.0) or 0.0)
        top_post = float(top.get("posterior") or 0.0)
        for missing in missing_rows:
            missing_label = str(missing.get("bucket") or "").strip()
            missing_prior = float(prior.get(missing_label, 0.0) or 0.0)
            if missing_prior <= 0.0:
                continue
            ratio = top_prior / missing_prior
            missing_post = float(missing.get("posterior") or 0.0)
            if ratio >= spike_ratio_threshold and top_post > missing_post:
                evidence.append(
                    {
                        "top_bucket": top_label,
                        "top_bucket_posterior": top_post,
                        "top_bucket_prior": top_prior,
                        "missing_bucket": missing_label,
                        "missing_bucket_posterior": missing_post,
                        "missing_bucket_prior": missing_prior,
                        "prior_ratio": ratio,
                    }
                )
    return evidence


def _normalize(values: Sequence[float]) -> list[float]:
    cleaned = [max(0.0, float(value)) for value in values]
    total = sum(cleaned)
    if total <= 0.0:
        return [1.0 / len(cleaned) for _ in cleaned] if cleaned else []
    return [value / total for value in cleaned]


def _posterior_from_prior(
    *,
    labels: Sequence[str],
    prior: Mapping[str, float],
    forecast_weak: Mapping[str, float],
    metar_strong: Mapping[str, float],
    hard_mask: Mapping[str, float],
    beta_weak: float,
    beta_strong: float,
) -> list[dict[str, Any]]:
    scores: list[float] = []
    for label in labels:
        score = (
            float(prior.get(label, 0.0) or 0.0)
            * float(forecast_weak.get(label, 0.0) or 0.0) ** float(beta_weak)
            * float(metar_strong.get(label, 0.0) or 0.0) ** float(beta_strong)
            * float(hard_mask.get(label, 1.0) or 0.0)
        )
        scores.append(score)
    posterior = _normalize(scores)
    return [
        {
            "bucket": label,
            "probability": posterior[index],
            "probability_pct": posterior[index] * 100.0,
            "prior": float(prior.get(label, 0.0) or 0.0),
            "order_index": index,
        }
        for index, label in enumerate(labels)
    ]


def _rank_scenario(rows: Sequence[Mapping[str, Any]], *, top_n: int) -> list[dict[str, Any]]:
    ranked = sorted(
        rows,
        key=lambda row: (-float(row.get("probability") or 0.0), int(row.get("order_index") or 0)),
    )
    return [
        {
            "bucket": row.get("bucket"),
            "probability": row.get("probability"),
            "probability_pct": row.get("probability_pct"),
            "prior": row.get("prior"),
        }
        for row in ranked[: max(1, int(top_n))]
    ]


def _flat_eligible_prior(labels: Sequence[str], hard_mask: Mapping[str, float]) -> dict[str, float]:
    eligible = [label for label in labels if float(hard_mask.get(label, 1.0) or 0.0) > 0.0]
    if not eligible:
        return {label: 1.0 / len(labels) for label in labels} if labels else {}
    return {label: (1.0 / len(eligible) if label in eligible else 0.0) for label in labels}


def _three_point_smoothed_prior(labels: Sequence[str], prior: Mapping[str, float]) -> dict[str, float]:
    raw = [float(prior.get(label, 0.0) or 0.0) for label in labels]
    smoothed: list[float] = []
    for index, value in enumerate(raw):
        parts: list[tuple[float, float]] = []
        if index > 0:
            parts.append((raw[index - 1], 0.25))
        parts.append((value, 0.5))
        if index + 1 < len(raw):
            parts.append((raw[index + 1], 0.25))
        denom = sum(weight for _val, weight in parts)
        smoothed.append(sum(val * weight for val, weight in parts) / denom if denom else value)
    normalized = _normalize(smoothed)
    return {label: normalized[index] for index, label in enumerate(labels)}


def _broad_history_prior(payload: Mapping[str, Any], labels: Sequence[str]) -> dict[str, float]:
    prior_meta = _as_mapping(payload.get("prior_meta"))
    counts = _as_mapping(prior_meta.get("broad_bucket_counts"))
    if not counts:
        return {}
    smoothing = max(0.0, float(_to_float(prior_meta.get("smoothing")) or 0.0))
    values = [max(0.0, float(_to_float(counts.get(label)) or 0.0)) + smoothing for label in labels]
    normalized = _normalize(values)
    return {label: normalized[index] for index, label in enumerate(labels)}


def _counterfactuals(
    payload: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
    *,
    top_n: int,
) -> dict[str, Any]:
    labels = [str(row.get("bucket") or "") for row in rows]
    if not labels:
        return {}
    prior = _prior_map(payload, rows)
    forecast_weak = _likelihood_map(
        payload,
        rows,
        layer="forecast_weak_likelihood",
        row_key="forecast_weak_likelihood",
    )
    metar_strong = _likelihood_map(
        payload,
        rows,
        layer="metar_strong_likelihood",
        row_key="metar_strong_likelihood",
    )
    hard_mask = _hard_mask_map(rows)
    beta_weak = _beta(payload, "forecast_weak_likelihood", 1.0)
    beta_strong = _beta(payload, "metar_strong_likelihood", 1.0)
    if not prior or not forecast_weak or not metar_strong:
        return {
            "state": "not_available",
            "reason": "prior_or_likelihood_layers_missing",
        }

    scenarios: dict[str, Any] = {}
    scenario_priors = {
        "actual_prior": prior,
        "flat_eligible_prior": _flat_eligible_prior(labels, hard_mask),
        "three_point_prior_smoothing": _three_point_smoothed_prior(labels, prior),
    }
    broad = _broad_history_prior(payload, labels)
    if broad:
        scenario_priors["broad_history_prior"] = broad
    for name, scenario_prior in scenario_priors.items():
        scenario_rows = _posterior_from_prior(
            labels=labels,
            prior=scenario_prior,
            forecast_weak=forecast_weak,
            metar_strong=metar_strong,
            hard_mask=hard_mask,
            beta_weak=beta_weak,
            beta_strong=beta_strong,
        )
        scenarios[name] = {
            "diagnostic_only": name != "actual_prior",
            "top_buckets": _rank_scenario(scenario_rows, top_n=top_n),
        }
    return {
        "state": "available",
        "formula": "prior * forecast_weak^beta_weak * metar_strong^beta_strong * hard_mask",
        "beta_weak": beta_weak,
        "beta_strong": beta_strong,
        "scenarios": scenarios,
    }


def audit_payload(
    payload: Mapping[str, Any],
    *,
    top_n: int = 3,
    spike_ratio_threshold: float = 3.0,
    min_effective_samples: int = 30,
) -> dict[str, Any]:
    rows = _ordered_bucket_rows(payload)
    prior = _prior_map(payload, rows)
    continuity = _top_continuity(rows, top_n=top_n)
    top_rows = _ranked(rows, limit=top_n)
    missing_rows = [
        row
        for row in rows
        if any(row.get("bucket") == missing.get("bucket") for missing in continuity.get("missing_buckets_between_top_range") or [])
    ]
    spike_evidence = _prior_spike_evidence(
        top_rows=top_rows,
        missing_rows=missing_rows,
        prior=prior,
        spike_ratio_threshold=spike_ratio_threshold,
    )
    prior_meta = _as_mapping(payload.get("prior_meta"))
    effective_sample_count = _to_int(prior_meta.get("effective_sample_count"))
    date_window_sample_count = _to_int(prior_meta.get("date_window_sample_count"))
    sample_count = _to_int(prior_meta.get("sample_count"))
    raw_candidate_count = _to_int(prior_meta.get("raw_candidate_count"))
    min_effective_sample_count = max(1, int(min_effective_samples))
    sample_rows = prior_meta.get("sample_rows") if isinstance(prior_meta.get("sample_rows"), list) else []
    source_table_counts: dict[str, int] = {}
    for row in sample_rows:
        if not isinstance(row, Mapping):
            continue
        source_table = str(row.get("source_table") or "unknown").strip() or "unknown"
        source_table_counts[source_table] = source_table_counts.get(source_table, 0) + 1

    blockers: list[str] = []
    if continuity.get("state") == "noncontiguous":
        blockers.append("posterior_top_buckets_noncontiguous")
    if spike_evidence:
        blockers.append("posterior_top_buckets_noncontiguous_prior_spike")
    if effective_sample_count is not None and effective_sample_count < min_effective_sample_count:
        blockers.append("prior_effective_sample_count_below_audit_min")
    return {
        "schema": "temperature_prior_continuity_audit_v1",
        "ok": not blockers,
        "read_only": True,
        "production_entry_enabled": False,
        "guards": {
            "writes_archive_db": False,
            "writes_facts_sqlite": False,
            "writes_auto_trade_decisions": False,
            "places_orders": False,
            "restarts_launchd": False,
            "historical_backfill": False,
            "changes_parameters": False,
            "reads_market_prices": False,
            "reads_forecast_sources": False,
            "reads_current_observations": False,
        },
        "identity": {
            "fact_id": payload.get("fact_id") or payload.get("id"),
            "slug": payload.get("slug"),
            "event_date": payload.get("event_date"),
            "fact_as_of": payload.get("fact_as_of"),
            "fact_snapshot_id": payload.get("fact_snapshot_id"),
            "posterior_run_id": payload.get("posterior_run_id"),
        },
        "top_n": int(top_n),
        "blockers": blockers,
        "sample_coverage": {
            "sample_count": sample_count,
            "raw_candidate_count": raw_candidate_count,
            "effective_sample_count": effective_sample_count,
            "date_window_sample_count": date_window_sample_count,
            "min_effective_samples": min_effective_sample_count,
            "state": (
                "low_effective_sample_count"
                if effective_sample_count is not None and effective_sample_count < min_effective_sample_count
                else "ok"
            ),
            "sample_rows_source_table_counts": source_table_counts,
            "sample_rows_are_limited_preview": True,
        },
        "top_continuity": continuity,
        "prior_spike_evidence": spike_evidence,
        "prior_meta_summary": {
            "sample_count": prior_meta.get("sample_count"),
            "effective_sample_count": prior_meta.get("effective_sample_count"),
            "date_window_sample_count": prior_meta.get("date_window_sample_count"),
            "date_window_weight": prior_meta.get("date_window_weight"),
            "smoothing": prior_meta.get("smoothing"),
            "bucket_counts": prior_meta.get("bucket_counts"),
            "broad_bucket_counts": prior_meta.get("broad_bucket_counts"),
            "sample_rows": prior_meta.get("sample_rows"),
        },
        "counterfactuals": _counterfactuals(payload, rows, top_n=top_n),
        "next_boundary": (
            "review_prior_continuity_policy_before_parameter_change"
            if blockers
            else "prior_continuity_ok_continue_posterior_ssot_review"
        ),
    }


def _connect_ro(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _load_fact_payloads(
    *,
    db_path: Path,
    fact_id: int | None,
    slug: str,
    event_date: str,
    run_date: str,
    limit: int,
) -> list[dict[str, Any]]:
    conn = _connect_ro(db_path)
    try:
        params: list[Any] = []
        if fact_id is not None:
            rows = conn.execute(
                "SELECT h.* FROM event_fact_history h WHERE h.id = ? LIMIT 1",
                (int(fact_id),),
            ).fetchall()
        else:
            where = ["h.slug LIKE '%chengdu%'"]
            if slug:
                where.append("h.slug = ?")
                params.append(slug)
            if event_date:
                where.append("h.event_date = ?")
                params.append(event_date)
            if run_date:
                where.append("h.run_date = ?")
                params.append(run_date)
            sql = f"""
                SELECT h.*
                FROM event_fact_latest l
                JOIN event_fact_history h ON h.id = l.fact_id
                WHERE {' AND '.join(where)}
                ORDER BY h.event_date DESC, h.fact_as_of DESC, h.id DESC
                LIMIT ?
            """
            params.append(max(1, int(limit)))
            rows = conn.execute(sql, tuple(params)).fetchall()
    finally:
        conn.close()
    payloads: list[dict[str, Any]] = []
    for row in rows:
        payload = _safe_json(row["fact_json"])
        if not payload:
            continue
        payload.setdefault("fact_id", int(row["id"] or 0))
        payload.setdefault("slug", str(row["slug"] or ""))
        payload.setdefault("event_date", str(row["event_date"] or ""))
        payload.setdefault("run_date", str(row["run_date"] or ""))
        payload.setdefault("fact_as_of", str(row["fact_as_of"] or ""))
        payload.setdefault("fact_snapshot_id", str(row["fact_snapshot_id"] or ""))
        payload.setdefault("posterior_run_id", str(row["posterior_run_id"] or ""))
        payloads.append(payload)
    return payloads


def build_report(
    *,
    db_path: Path = DEFAULT_DB,
    fact_id: int | None = None,
    slug: str = "",
    event_date: str = "",
    run_date: str = "",
    limit: int = 10,
    top_n: int = 3,
    spike_ratio_threshold: float = 3.0,
    min_effective_samples: int = 30,
) -> dict[str, Any]:
    payloads = _load_fact_payloads(
        db_path=db_path,
        fact_id=fact_id,
        slug=slug,
        event_date=event_date,
        run_date=run_date,
        limit=limit,
    )
    items = [
        audit_payload(
            payload,
            top_n=top_n,
            spike_ratio_threshold=spike_ratio_threshold,
            min_effective_samples=min_effective_samples,
        )
        for payload in payloads
    ]
    blockers = sorted({blocker for item in items for blocker in item.get("blockers") or []})
    if not items:
        blockers.append("no_fact_payloads_found")
    return {
        "schema": "temperature_prior_continuity_audit_report_v1",
        "ok": not blockers,
        "read_only": True,
        "db": str(db_path),
        "scope": {
            "fact_id": fact_id,
            "slug": slug,
            "event_date": event_date,
            "run_date": run_date,
            "limit": int(limit),
            "top_n": int(top_n),
            "spike_ratio_threshold": float(spike_ratio_threshold),
            "min_effective_samples": max(1, int(min_effective_samples)),
        },
        "coverage": {
            "checked_fact_count": len(items),
            "issue_fact_count": sum(1 for item in items if item.get("blockers")),
        },
        "blockers": blockers,
        "items": items,
        "next_boundary": (
            "review_prior_continuity_policy_before_parameter_change"
            if blockers
            else "prior_continuity_ok_continue_posterior_ssot_review"
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only audit for non-contiguous top posterior buckets caused by prior spikes."
    )
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--fact-id", type=int)
    parser.add_argument("--slug", default="")
    parser.add_argument("--event-date", default="")
    parser.add_argument("--run-date", default="")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--top-n", type=int, default=3)
    parser.add_argument("--spike-ratio-threshold", type=float, default=3.0)
    parser.add_argument("--min-effective-samples", type=int, default=30)
    args = parser.parse_args()
    report = build_report(
        db_path=Path(args.db),
        fact_id=args.fact_id,
        slug=str(args.slug or ""),
        event_date=str(args.event_date or ""),
        run_date=str(args.run_date or ""),
        limit=int(args.limit),
        top_n=int(args.top_n),
        spike_ratio_threshold=float(args.spike_ratio_threshold),
        min_effective_samples=int(args.min_effective_samples),
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
