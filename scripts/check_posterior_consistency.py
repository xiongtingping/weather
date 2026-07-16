#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _to_float(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _prob(value: Any, *, pct: bool = False) -> float | None:
    val = _to_float(value)
    if val is None:
        return None
    if pct:
        val /= 100.0
    return val if math.isfinite(val) else None


def _label(value: Any) -> str:
    return str(value or "").strip()


def _items_from_snapshot(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    facts = payload.get("facts")
    if isinstance(facts, Mapping) and isinstance(facts.get("items"), list):
        return [item for item in facts.get("items") or [] if isinstance(item, Mapping)]
    if isinstance(payload.get("items"), list):
        return [item for item in payload.get("items") or [] if isinstance(item, Mapping)]
    return []


def _snapshot_run_date(payload: Mapping[str, Any]) -> str:
    facts = payload.get("facts") if isinstance(payload.get("facts"), Mapping) else {}
    return str(payload.get("run_date") or facts.get("run_date") or "").strip().split("T", 1)[0]


def _snapshot_rebuild_in_flight(payload: Mapping[str, Any]) -> bool:
    facts = payload.get("facts") if isinstance(payload.get("facts"), Mapping) else {}
    return bool(
        payload.get("busy") is True
        or payload.get("cache_rebuild_in_flight") is True
        or str(facts.get("source") or payload.get("snapshot_source") or "") == "ui_snapshot_rebuild_in_flight"
    )


def _distribution_probabilities(item: Mapping[str, Any]) -> tuple[dict[str, float], list[str]]:
    warnings: list[str] = []
    distribution = item.get("distribution")
    if not isinstance(distribution, list) or not distribution:
        return {}, ["distribution_missing"]
    out: dict[str, float] = {}
    for index, row in enumerate(distribution):
        if not isinstance(row, Mapping):
            continue
        bucket = _label(row.get("outcome") or row.get("bucket") or row.get("label"))
        if not bucket:
            warnings.append(f"distribution[{index}].label_missing")
            continue
        posterior = _prob(row.get("q_temperature_bucket_posterior"))
        if posterior is None:
            warnings.append(f"distribution[{index}].q_temperature_bucket_posterior_missing")
            continue
        out[bucket] = posterior
    if not out:
        warnings.append("distribution_probability_map_empty")
    return out, warnings


def _distribution_probabilities_for_key(item: Mapping[str, Any], key: str) -> dict[str, float]:
    distribution = item.get("distribution")
    if not isinstance(distribution, list) or not distribution:
        return {}
    out: dict[str, float] = {}
    for row in distribution:
        if not isinstance(row, Mapping):
            continue
        bucket = _label(row.get("outcome") or row.get("bucket") or row.get("label"))
        if not bucket:
            continue
        prob = _prob(row.get(key))
        if prob is not None:
            out[bucket] = prob
    return out


def _target_bucket_outlook_probabilities(item: Mapping[str, Any]) -> dict[str, float]:
    outlook = item.get("target_bucket_outlook")
    if not isinstance(outlook, Mapping) or not _hard_source_outlook(outlook):
        return {}
    out: dict[str, float] = {}
    rows = outlook.get("rows")
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            bucket = _label(row.get("outcome") or row.get("bucket") or row.get("label"))
            if not bucket:
                continue
            prob = _prob(row.get("probability"))
            if prob is None:
                prob = _prob(row.get("probability_pct"), pct=True)
            if prob is not None:
                out[bucket] = max(0.0, min(1.0, float(prob)))

    labeled_fields = (
        ("target_bucket", "target_final_probability_pct"),
        ("final_bucket", "final_bucket_probability_pct"),
        ("posterior_final_bucket", "posterior_final_bucket_probability_pct"),
        ("cumulative_posterior_final_bucket", "cumulative_posterior_final_bucket_probability_pct"),
        ("second_target_bucket", "second_target_final_probability_pct"),
    )
    for label_field, prob_field in labeled_fields:
        bucket = _label(outlook.get(label_field))
        if not bucket or bucket in out:
            continue
        prob = _prob(outlook.get(prob_field), pct=True)
        if prob is not None:
            out[bucket] = max(0.0, min(1.0, float(prob)))
    return out


def _hard_source_probabilities(item: Mapping[str, Any]) -> dict[str, float]:
    constrained = _target_bucket_outlook_probabilities(item)
    if constrained:
        return constrained
    return _distribution_probabilities_for_key(item, "q_raw")


def _hard_source_outlook(container: Mapping[str, Any]) -> bool:
    if not isinstance(container, Mapping):
        return False
    text = " ".join(
        str(container.get(key) or "").lower()
        for key in ("target_bucket_source", "final_bucket_source", "source", "probability_source", "constraint_source")
    )
    hard_direct = container.get("hard_source_direct")
    return bool(
        "hard_source" in text
        or "observed" in text
        or (
            isinstance(hard_direct, Mapping)
            and (
                hard_direct.get("posterior_floor_constraint_applied") is True
                or str(hard_direct.get("source") or "").lower() == "hard_source_direct"
            )
        )
    )


def _hard_zero_labels(item: Mapping[str, Any]) -> set[str]:
    distribution = item.get("distribution")
    if not isinstance(distribution, list):
        return set()
    labels: set[str] = set()
    for row in distribution:
        if not isinstance(row, Mapping):
            continue
        reasons = row.get("posterior_settlement_candidate_bucket_hard_zero_reasons")
        if not isinstance(reasons, list):
            reasons = []
        reason_text = " ".join(str(reason or "").lower() for reason in reasons)
        hard_zero = bool(
            row.get("q_posterior_observed_constraints_hard_zero")
            or row.get("q_bayes_physical_likelihood_hard_zero")
            or row.get("posterior_settlement_candidate_bucket_hard_zero")
            or "observed_today_max_above_bucket" in reason_text
            or "above_bucket" in reason_text
        )
        if hard_zero:
            label = _label(row.get("outcome") or row.get("bucket") or row.get("label"))
            if label:
                labels.add(label)
    return labels


def _check_hard_zero_targets(
    container: Mapping[str, Any],
    hard_zero: set[str],
    *,
    prefix: str,
    mismatches: list[dict[str, Any]],
) -> int:
    if not hard_zero:
        return 0
    checked = 0
    target_fields = (
        "target_bucket",
        "final_bucket",
        "posterior_final_bucket",
        "cumulative_posterior_final_bucket",
        "second_target_bucket",
        "candidate_bucket",
    )
    for field in target_fields:
        bucket = _label(container.get(field))
        if not bucket:
            continue
        checked += 1
        if bucket in hard_zero:
            mismatches.append(
                {
                    "field": f"{prefix}.{field}",
                    "bucket": bucket,
                    "actual": "hard_zero_target",
                    "expected": "non_hard_zero_target",
                    "diff": None,
                    "expected_source": "distribution.posterior_settlement_candidate_bucket_hard_zero",
                }
            )
    return checked


def _item_identity(item: Mapping[str, Any], index: int) -> dict[str, Any]:
    return {
        "index": index,
        "slug": item.get("slug"),
        "event_date": item.get("event_date"),
        "run_date": item.get("run_date"),
    }


def _compare_probability(
    *,
    mismatches: list[dict[str, Any]],
    field_path: str,
    source_map: Mapping[str, float],
    bucket: Any,
    actual: Any,
    tolerance: float,
    pct: bool = False,
) -> bool:
    bucket_key = _label(bucket)
    if not bucket_key or actual is None:
        return False
    if bucket_key not in source_map:
        return False
    actual_prob = _prob(actual, pct=pct)
    if actual_prob is None:
        return False
    expected = source_map[bucket_key]
    if abs(actual_prob - expected) <= tolerance:
        return True
    mismatches.append(
        {
            "field": field_path,
            "bucket": bucket_key,
            "actual": actual_prob,
            "expected": expected,
            "diff": actual_prob - expected,
            "expected_source": "distribution.q_temperature_bucket_posterior",
        }
    )
    return True


def _check_target_bucket_outlook(
    container: Mapping[str, Any],
    source_map: Mapping[str, float],
    *,
    prefix: str,
    tolerance: float,
    mismatches: list[dict[str, Any]],
    hard_source_map: Mapping[str, float] | None = None,
) -> int:
    checked = 0
    use_hard_source = _hard_source_outlook(container) and bool(hard_source_map)
    pairs = (
        ("target_bucket", "target_probability"),
        ("target_bucket", "target_probability_pct", True),
        ("target_bucket", "target_final_probability"),
        ("target_bucket", "target_final_probability_pct", True),
        ("final_bucket", "final_bucket_probability"),
        ("final_bucket", "final_bucket_probability_pct", True),
        ("second_target_bucket", "second_target_probability"),
        ("second_target_bucket", "second_target_probability_pct", True),
        ("second_target_bucket", "second_target_final_probability"),
        ("second_target_bucket", "second_target_final_probability_pct", True),
    )
    for raw in pairs:
        label_field = raw[0]
        prob_field = raw[1]
        is_pct = bool(raw[2]) if len(raw) > 2 else False
        if prob_field not in container:
            continue
        effective_map = (
            hard_source_map
            if use_hard_source and prob_field in {
                "target_final_probability",
                "target_final_probability_pct",
                "target_bucket_probability",
                "target_bucket_probability_pct",
                "final_bucket_probability",
                "final_bucket_probability_pct",
                "second_target_final_probability",
                "second_target_final_probability_pct",
                "second_target_probability",
                "second_target_probability_pct",
            }
            else source_map
        )
        if _compare_probability(
            mismatches=mismatches,
            field_path=f"{prefix}.{prob_field}",
            source_map=effective_map or source_map,
            bucket=container.get(label_field),
            actual=container.get(prob_field),
            tolerance=tolerance,
            pct=is_pct,
        ):
            checked += 1
    return checked


def _check_temperature_path_distribution(
    container: Mapping[str, Any],
    source_map: Mapping[str, float],
    *,
    prefix: str,
    tolerance: float,
    mismatches: list[dict[str, Any]],
) -> int:
    checked = 0
    top_level_pairs = (
        ("target_bucket", "target_bucket_posterior", False),
        ("target_bucket", "target_bucket_posterior_pct", True),
        ("second_target_bucket", "second_target_bucket_posterior", False),
        ("second_target_bucket", "second_target_bucket_posterior_pct", True),
    )
    for label_field, prob_field, is_pct in top_level_pairs:
        if prob_field not in container:
            continue
        if _compare_probability(
            mismatches=mismatches,
            field_path=f"{prefix}.{prob_field}",
            source_map=source_map,
            bucket=container.get(label_field),
            actual=container.get(prob_field),
            tolerance=tolerance,
            pct=is_pct,
        ):
            checked += 1

    nested_pairs = (
        ("target", "posterior", False),
        ("target", "posterior_pct", True),
        ("second_target", "posterior", False),
        ("second_target", "posterior_pct", True),
    )
    for key, prob_field, is_pct in nested_pairs:
        nested = container.get(key)
        if not isinstance(nested, Mapping) or prob_field not in nested:
            continue
        if _compare_probability(
            mismatches=mismatches,
            field_path=f"{prefix}.{key}.{prob_field}",
            source_map=source_map,
            bucket=nested.get("bucket") or nested.get("target_bucket"),
            actual=nested.get(prob_field),
            tolerance=tolerance,
            pct=is_pct,
        ):
            checked += 1

    rows = container.get("bucket_distribution")
    if isinstance(rows, list):
        for index, row in enumerate(rows):
            if not isinstance(row, Mapping):
                continue
            bucket = row.get("bucket") or row.get("outcome")
            for prob_field, is_pct in (("posterior", False), ("posterior_pct", True)):
                if prob_field not in row:
                    continue
                if _compare_probability(
                    mismatches=mismatches,
                    field_path=f"{prefix}.bucket_distribution[{index}].{prob_field}",
                    source_map=source_map,
                    bucket=bucket,
                    actual=row.get(prob_field),
                    tolerance=tolerance,
                    pct=is_pct,
                ):
                    checked += 1
    return checked


def _check_capital_plan(
    container: Mapping[str, Any],
    source_map: Mapping[str, float],
    *,
    prefix: str,
    tolerance: float,
    mismatches: list[dict[str, Any]],
    hard_source_map: Mapping[str, float] | None = None,
) -> int:
    checked = 0
    effective_source_map = hard_source_map if hard_source_map else source_map
    pairs = (
        ("target_bucket", "target_posterior", False),
        ("target_bucket", "target_posterior_pct", True),
        ("second_target_bucket", "second_target_posterior", False),
        ("second_target_bucket", "second_target_posterior_pct", True),
    )
    for label_field, prob_field, is_pct in pairs:
        if prob_field not in container:
            continue
        if _compare_probability(
            mismatches=mismatches,
            field_path=f"{prefix}.{prob_field}",
            source_map=effective_source_map,
            bucket=container.get(label_field),
            actual=container.get(prob_field),
            tolerance=tolerance,
            pct=is_pct,
        ):
            checked += 1

    risk = container.get("ui_risk_explanation")
    if isinstance(risk, Mapping):
        for label_field, prob_field, is_pct in pairs:
            if prob_field not in risk:
                continue
            bucket = risk.get(label_field) or container.get(label_field)
            if _compare_probability(
                mismatches=mismatches,
                field_path=f"{prefix}.ui_risk_explanation.{prob_field}",
                source_map=effective_source_map,
                bucket=bucket,
                actual=risk.get(prob_field),
                tolerance=tolerance,
                pct=is_pct,
            ):
                checked += 1

    legs = container.get("legs")
    if isinstance(legs, list):
        for index, leg in enumerate(legs):
            if not isinstance(leg, Mapping):
                continue
            bucket = leg.get("bucket") or leg.get("target_bucket")
            for prob_field, is_pct in (("posterior", False), ("posterior_pct", True)):
                if prob_field not in leg:
                    continue
                if _compare_probability(
                    mismatches=mismatches,
                    field_path=f"{prefix}.legs[{index}].{prob_field}",
                    source_map=effective_source_map,
                    bucket=bucket,
                    actual=leg.get(prob_field),
                    tolerance=tolerance,
                    pct=is_pct,
                ):
                    checked += 1
    return checked


def _check_target_switch_context(
    container: Mapping[str, Any],
    source_map: Mapping[str, float],
    *,
    prefix: str,
    tolerance: float,
    mismatches: list[dict[str, Any]],
    hard_source_map: Mapping[str, float] | None = None,
) -> int:
    checked = 0
    effective_source_map = hard_source_map if hard_source_map else source_map
    pairs = (
        ("target_bucket", "target_probability_pct"),
        ("candidate_bucket", "candidate_probability_pct"),
        ("second_target_bucket", "second_target_probability_pct"),
    )
    for label_field, prob_field in pairs:
        if prob_field not in container:
            continue
        if _compare_probability(
            mismatches=mismatches,
            field_path=f"{prefix}.{prob_field}",
            source_map=effective_source_map,
            bucket=container.get(label_field),
            actual=container.get(prob_field),
            tolerance=tolerance,
            pct=True,
        ):
            checked += 1
    return checked


def audit_item(item: Mapping[str, Any], index: int, *, tolerance: float = 5e-4) -> dict[str, Any]:
    source_map, warnings = _distribution_probabilities(item)
    hard_source_map = _hard_source_probabilities(item)
    hard_zero = _hard_zero_labels(item)
    mismatches: list[dict[str, Any]] = []
    checked_fields = 0
    outlook = item.get("target_bucket_outlook")
    path = item.get("temperature_path_distribution")
    plan = item.get("target_realization_capital_plan")
    switch = item.get("target_switch_context")
    summary = item.get("chengdu_intraday_summary")
    summary_outlook = summary.get("target_bucket_outlook") if isinstance(summary, Mapping) else None
    summary_path = summary.get("temperature_path_distribution") if isinstance(summary, Mapping) else None
    hard_zero_containers = (
        ("target_bucket_outlook", outlook),
        ("temperature_path_distribution", path),
        ("target_realization_capital_plan", plan),
        ("target_switch_context", switch),
        ("chengdu_intraday_summary.target_bucket_outlook", summary_outlook),
        ("chengdu_intraday_summary.temperature_path_distribution", summary_path),
    )
    for prefix, container in hard_zero_containers:
        if isinstance(container, Mapping):
            checked_fields += _check_hard_zero_targets(
                container,
                hard_zero,
                prefix=prefix,
                mismatches=mismatches,
            )
    if source_map:
        if isinstance(outlook, Mapping):
            checked_fields += _check_target_bucket_outlook(
                outlook,
                source_map,
                prefix="target_bucket_outlook",
                tolerance=tolerance,
                mismatches=mismatches,
                hard_source_map=hard_source_map,
            )
        if isinstance(path, Mapping):
            checked_fields += _check_temperature_path_distribution(
                path,
                hard_source_map if _hard_source_outlook(outlook if isinstance(outlook, Mapping) else {}) and hard_source_map else source_map,
                prefix="temperature_path_distribution",
                tolerance=tolerance,
                mismatches=mismatches,
            )
        if isinstance(plan, Mapping):
            checked_fields += _check_capital_plan(
                plan,
                source_map,
                prefix="target_realization_capital_plan",
                tolerance=tolerance,
                mismatches=mismatches,
                hard_source_map=hard_source_map if _hard_source_outlook(outlook if isinstance(outlook, Mapping) else {}) else None,
            )
        if isinstance(switch, Mapping):
            checked_fields += _check_target_switch_context(
                switch,
                source_map,
                prefix="target_switch_context",
                tolerance=tolerance,
                mismatches=mismatches,
                hard_source_map=hard_source_map if _hard_source_outlook(outlook if isinstance(outlook, Mapping) else {}) else None,
            )
        if isinstance(summary, Mapping):
            if isinstance(summary_outlook, Mapping):
                checked_fields += _check_target_bucket_outlook(
                    summary_outlook,
                    source_map,
                    prefix="chengdu_intraday_summary.target_bucket_outlook",
                    tolerance=tolerance,
                    mismatches=mismatches,
                    hard_source_map=hard_source_map,
                )
            if isinstance(summary_path, Mapping):
                checked_fields += _check_temperature_path_distribution(
                    summary_path,
                    hard_source_map if _hard_source_outlook(summary_outlook if isinstance(summary_outlook, Mapping) else outlook if isinstance(outlook, Mapping) else {}) and hard_source_map else source_map,
                    prefix="chengdu_intraday_summary.temperature_path_distribution",
                    tolerance=tolerance,
                    mismatches=mismatches,
                )

    return {
        **_item_identity(item, index),
        "ok": not mismatches,
        "checked_fields": checked_fields,
        "warnings": warnings,
        "mismatches": mismatches,
    }


def audit_payload(payload: Mapping[str, Any], *, source: str = "payload", tolerance: float = 5e-4) -> dict[str, Any]:
    facts = payload.get("facts") if isinstance(payload.get("facts"), Mapping) else {}
    items = _items_from_snapshot(payload)
    temporary = _snapshot_rebuild_in_flight(payload)
    if temporary and not items:
        return {
            "ok": False,
            "temporary": True,
            "source": source,
            "snapshot_source": facts.get("source") or payload.get("source"),
            "blockers": ["ui_snapshot_rebuild_in_flight"],
            "checked_items": 0,
            "checked_fields": 0,
            "mismatches": [],
            "warnings": [],
            "items": [],
        }
    checked = [audit_item(item, index, tolerance=tolerance) for index, item in enumerate(items)]
    mismatches: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    checked_items = 0
    checked_fields = 0
    for item_result in checked:
        checked_fields += int(item_result.get("checked_fields") or 0)
        if item_result.get("checked_fields"):
            checked_items += 1
        for warning in item_result.get("warnings") or []:
            warnings.append({**_item_identity(item_result, int(item_result.get("index") or 0)), "warning": warning})
        for mismatch in item_result.get("mismatches") or []:
            mismatches.append({**_item_identity(item_result, int(item_result.get("index") or 0)), **mismatch})
    blockers: list[str] = []
    if not checked_items:
        blockers.append("no_posterior_fields_checked")
    if mismatches:
        blockers.append("posterior_mismatch")
    return {
        "ok": not blockers,
        "temporary": bool(temporary),
        "source": source,
        "snapshot_source": facts.get("source") or payload.get("source"),
        "run_date": _snapshot_run_date(payload),
        "item_count": len(items),
        "checked_items": checked_items,
        "checked_fields": checked_fields,
        "mismatch_count": len(mismatches),
        "blockers": blockers,
        "mismatches": mismatches,
        "warnings": warnings,
        "items": checked,
    }


def _fetch_runtime_snapshot(url: str, timeout: float) -> Mapping[str, Any]:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _fetch_runtime_snapshot_with_retries(
    url: str,
    timeout: float,
    *,
    rebuild_retries: int,
    rebuild_retry_delay_sec: float,
) -> Mapping[str, Any]:
    last_payload: Mapping[str, Any] = {}
    attempts = max(1, int(rebuild_retries) + 1)
    for index in range(attempts):
        payload = _fetch_runtime_snapshot(url, timeout)
        last_payload = payload
        if not _snapshot_rebuild_in_flight(payload):
            return payload
        if index < attempts - 1:
            time.sleep(max(0.0, float(rebuild_retry_delay_sec)))
    return last_payload


def _fetch_source_snapshot(run_date: str, limit: int) -> Mapping[str, Any]:
    from ui import server

    return server.ui_snapshot_summary(run_date, limit)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only audit that UI posterior derived fields match distribution.q_temperature_bucket_posterior."
    )
    parser.add_argument("--runtime", action="store_true", help="Check the running 3002 API instead of importing source.")
    parser.add_argument("--url", default="http://127.0.0.1:3002/api/ui_snapshot_summary?limit=24")
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--rebuild-retries", type=int, default=8)
    parser.add_argument("--rebuild-retry-delay-sec", type=float, default=1.5)
    parser.add_argument("--run-date", default="")
    parser.add_argument("--limit", type=int, default=24)
    parser.add_argument("--tolerance", type=float, default=5e-4)
    args = parser.parse_args()

    if args.runtime:
        payload = _fetch_runtime_snapshot_with_retries(
            args.url,
            args.timeout,
            rebuild_retries=args.rebuild_retries,
            rebuild_retry_delay_sec=args.rebuild_retry_delay_sec,
        )
        result = audit_payload(payload, source="runtime_api", tolerance=args.tolerance)
    else:
        payload = _fetch_source_snapshot(args.run_date, args.limit)
        result = audit_payload(payload, source="current_source_import", tolerance=args.tolerance)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
